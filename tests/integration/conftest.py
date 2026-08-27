"""Integration test fixtures and environment checks.

Provides:
    - Docker test target lifecycle (auto start/stop via fixture)
    - SSH session fixtures (connect to real or Docker target)
    - PID auto-detection
    - Environment variable validation

Usage::

    # Remote target (env vars required)
    export TEST_SSH_HOST=remote.server
    export TEST_SSH_USER=ubuntu
    export TEST_SSH_PASSWORD=secret
    pytest tests/integration/ -m integration -v

    # Docker target (auto-managed, no env vars needed)
    pytest tests/integration/ -m integration -v --docker-target
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
import uuid
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from arthas_mcp_proxy.arthas_client import ArthasClient
    from arthas_mcp_proxy.ssh_pool import SSHConnectionPool, SSHSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1.  Pytest hook: register CLI option
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--docker-target",
        action="store_true",
        default=False,
        help="Automatically start a Docker test target (SSH + Java) "
        "before running integration tests",
    )
    parser.addoption(
        "--docker-targets",
        action="store_true",
        default=False,
        help="Start both local Docker SSH targets for multi-target tests",
    )


# ---------------------------------------------------------------------------
# 2.  Env-var helper
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(
            f"Integration test requires env var {name}. "
            f"Set it before running: export {name}=<value>"
        )
    return value


# ---------------------------------------------------------------------------
# 3.  Docker lifecycle fixture
# ---------------------------------------------------------------------------


def _docker_compose_cmd() -> list[str]:
    """Return the correct docker compose command for this system.

    Docker Compose v2: ``docker compose`` (plugin, space)
    Docker Compose v1: ``docker-compose`` (standalone, hyphen)
    """
    # Prefer v2 plugin.  The integration fixture may run on hosts where the
    # invoking user cannot access /var/run/docker.sock, so use the explicitly
    # supported sudo path when available.
    try:
        subprocess.run(
            ["sudo", "docker", "compose", "version"],
            capture_output=True,
            timeout=5,
            check=True,
        )
        return ["sudo", "docker", "compose"]
    except (subprocess.CalledProcessError, FileNotFoundError):
        try:
            subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True,
                timeout=5,
                check=True,
            )
            return ["docker", "compose"]
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    # Fall back to v1 standalone
    docker_compose = shutil.which("docker-compose")
    if docker_compose:
        try:
            subprocess.run(
                [docker_compose, "version"],
                capture_output=True,
                timeout=5,
                check=True,
            )
            return [docker_compose]
        except (subprocess.CalledProcessError, OSError):
            pass
    pytest.fail("Neither 'docker compose' nor 'docker-compose' is available")


def _wait_for_ssh(host: str, port: int, timeout: int = 60) -> bool:
    """Poll until the endpoint emits a real SSH protocol banner.

    A published Docker port can accept TCP connections before sshd is ready;
    merely connecting and closing caused intermittent Paramiko banner failures.
    """
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2) as sock:
                sock.settimeout(2)
                banner = sock.recv(256)
                if banner.startswith(b"SSH-"):
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


def _compose_project() -> str:
    """Return an isolated project name for each integration fixture run."""
    return f"arthas-mcp-proxy-it-{os.getpid()}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="session")
def docker_test_target(request: pytest.FixtureRequest) -> dict[str, str]:
    """Start the Docker test target and yield connection parameters.

    When ``--docker-target`` is passed on the CLI, this fixture:
        1. Builds and starts the container via docker compose
        2. Waits for SSH to be ready
        3. Sets environment variables for the test session
        4. Tears down the container after all tests finish

    Yields a dict with keys: host, port, user, password.
    """
    use_docker = request.config.getoption("--docker-target")
    if not use_docker:
        # Not using Docker target; tests will rely on env vars
        yield {}
        return

    compose_cmd = _docker_compose_cmd()
    project_name = f"arthas-mcp-proxy-it-{os.getpid()}-{uuid.uuid4().hex[:8]}"

    def compose(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run([*compose_cmd, "-p", project_name, *args], **kwargs)  # type: ignore[arg-type]

    compose_file = os.path.join(
        os.path.dirname(__file__), "..", "..", "docker-compose.test.yml"
    )
    if not os.path.isfile(compose_file):
        pytest.fail(f"Docker compose file not found: {compose_file}")

    logger.info("Building test target container...")
    try:
        compose(
            "-f", compose_file, "up", "--build", "-d",
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"Failed to start Docker test target: {exc.stderr}")
    except FileNotFoundError:
        pytest.fail("docker compose command not found")

    # Wait for SSH
    logger.info("Waiting for SSH on localhost:2222 ...")
    if not _wait_for_ssh("localhost", 2222, timeout=60):
        compose("-f", compose_file, "down", "--volumes", capture_output=True)
        pytest.fail("SSH port 2222 not ready after 60s")

    logger.info("Docker test target ready (localhost:2222)")

    # Set env vars so ssh_session fixture picks them up
    os.environ.setdefault("TEST_SSH_HOST", "localhost")
    os.environ.setdefault("TEST_SSH_USER", "testuser")
    os.environ.setdefault("TEST_SSH_PASSWORD", "testpass")
    os.environ.setdefault("TEST_SSH_PORT", "2222")

    yield {
        "host": "localhost",
        "port": "2222",
        "user": "testuser",
        "password": "testpass",
    }

    # Teardown
    logger.info("Tearing down Docker test target...")
    compose("-f", compose_file, "down", "--volumes", capture_output=True)


@pytest.fixture(scope="session")
def docker_test_targets(request: pytest.FixtureRequest) -> dict[str, dict[str, str]]:
    """Start two local real SSH/JVM targets; unavailable infrastructure fails."""
    if not request.config.getoption("--docker-targets"):
        yield {}
        return
    compose_cmd = _docker_compose_cmd()
    project_name = _compose_project()
    compose_file = os.path.join(os.path.dirname(__file__), "..", "..", "docker-compose.test.yml")
    compose = lambda *args, **kwargs: subprocess.run(
        [*compose_cmd, "-p", project_name, *args], **kwargs
    )
    try:
        compose(
            "-f", compose_file, "up", "--build", "-d", "--wait", "test-target", "test-target-b",
            check=True, capture_output=True, text=True, timeout=300,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        pytest.fail(f"Failed to start multi-target Docker fixture: {exc}")
    targets = {
        "target-a": {"host": "localhost", "port": "2222", "username": "testuser", "password": "testpass"},
        "target-b": {"host": "localhost", "port": "2223", "username": "testuser", "password": "testpass"},
    }
    for name, target in targets.items():
        if not _wait_for_ssh(target["host"], int(target["port"]), timeout=60):
            compose("-f", compose_file, "down", "--volumes", capture_output=True)
            pytest.fail(f"Docker SSH endpoint {name} was not ready")
    try:
        yield targets
    finally:
        compose("-f", compose_file, "down", "--volumes", capture_output=True)


# ---------------------------------------------------------------------------
# 4.  SSH fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ssh_pool() -> "SSHConnectionPool":
    """Return a fresh SSHConnectionPool for the test module."""
    from arthas_mcp_proxy.ssh_pool import SSHConnectionPool

    pool = SSHConnectionPool(idle_timeout=600)
    yield pool
    pool.shutdown()


@pytest.fixture(scope="module")
def ssh_session(
    ssh_pool: "SSHConnectionPool",
    docker_test_target: dict[str, str],
) -> "SSHSession":
    """Connect to the target server and return an active SSHSession.

    Priority:
        1. If ``--docker-target`` was used, connect to localhost:2222
        2. Otherwise, read credentials from environment variables
    """
    host = os.environ.get("TEST_SSH_HOST") or docker_test_target.get("host")
    if not host:
        pytest.fail("No SSH target configured. Use --docker-target or set TEST_SSH_HOST")

    port = int(os.environ.get("TEST_SSH_PORT", docker_test_target.get("port", "22")))
    username = os.environ.get("TEST_SSH_USER") or docker_test_target.get("user")
    password = os.environ.get("TEST_SSH_PASSWORD") or docker_test_target.get("password")
    key_path = os.environ.get("TEST_SSH_KEY_PATH") or os.environ.get("TEST_SSH_KEY_FILE")

    if not username or (not password and not key_path):
        pytest.fail(
            "SSH authentication is not configured. Set TEST_SSH_USER and either "
            "TEST_SSH_PASSWORD or TEST_SSH_KEY_PATH (private-key path)."
        )

    sid = ssh_pool.connect(
        host=host,
        port=port,
        username=username,
        password=password,
        key_path=key_path,
        timeout=30,
    )
    session = ssh_pool.get_session(sid)
    assert session is not None, f"Failed to establish SSH session to {host}"
    logger.info("SSH connected: %s@%s:%s (session=%s)", username, host, port, sid)
    yield session
    ssh_pool.disconnect(sid)


@pytest.fixture(scope="module")
def target_pid(ssh_session: "SSHSession") -> int:
    """Auto-detect a suitable Java process PID for Arthas testing."""
    env_pid = os.environ.get("TEST_TARGET_PID")
    if env_pid:
        return int(env_pid)

    _stdin, stdout, _stderr = ssh_session.client.exec_command("jps -l")
    jps_output = stdout.read().decode("utf-8", errors="replace")
    logger.info("jps output:\n%s", jps_output)

    candidates: list[tuple[int, str]] = []
    for line in jps_output.strip().split("\n"):
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            pid = int(parts[0])
            name = parts[1]
            if "Jps" in name or "arthas" in name.lower():
                continue
            candidates.append((pid, name))

    assert candidates, "No suitable Java process found on target server"

    for pid, name in candidates:
        if "TestApp" in name or "math" in name.lower():
            logger.info("Selected target PID %d (%s)", pid, name)
            return pid

    pid, name = candidates[0]
    logger.info("Selected target PID %d (%s)", pid, name)
    return pid


@pytest.fixture(scope="module")
def pid_replacement_target(ssh_session: "SSHSession") -> dict[str, object]:
    """Create real JVMs with the same namespace PID but different start times."""
    import shlex

    token = uuid.uuid4().hex
    old_file = f"/tmp/pid-replacement-old-{token}"
    new_file = f"/tmp/pid-replacement-new-{token}"

    def start(path: str) -> int:
        script = (
            "java -jar /opt/math-game.jar >/dev/null 2>&1 & p=$!; "
            "while kill -0 $p 2>/dev/null; do s=$(awk '{print $22}' /proc/$p/stat); "
            f"printf '2 testuser %s math-game.jar\\n' \"$s\" > {shlex.quote(path)}; "
            "sleep 0.1; done"
        )
        command = (
            f"nohup sudo -n unshare -pf --mount-proc sh -c {shlex.quote(script)} "
            ">/dev/null 2>&1 & echo $!"
        )
        _, stdout, stderr = ssh_session.client.exec_command(command)
        outer_pid = stdout.read().decode().strip()
        if not outer_pid.isdigit():
            pytest.fail(f"Could not start real PID-namespace JVM: {stderr.read().decode().strip()}")
        return int(outer_pid)

    def read_line(path: str) -> str:
        deadline = time.time() + 30
        while time.time() < deadline:
            _, stdout, _ = ssh_session.client.exec_command(f"test -s {path} && cat {path}")
            line = stdout.read().decode().strip()
            if line:
                return line
            time.sleep(0.5)
        pytest.fail(f"Real JVM did not publish identity file {path}")

    old_outer_pid = start(old_file)
    old_line = read_line(old_file)
    _, stdout, _ = ssh_session.client.exec_command(f"kill {old_outer_pid}; rm -f {old_file}")
    stdout.channel.recv_exit_status()
    new_outer_pid = start(new_file)
    try:
        new_line = read_line(new_file)
        old_parts = old_line.split()
        new_parts = new_line.split()
        assert len(old_parts) >= 4 and len(new_parts) >= 4
        assert old_parts[0] == new_parts[0] == "2"
        return {
            "old_pid": 2,
            "old_start": old_parts[2],
            "replacement_pid": 2,
            "replacement_start": new_parts[2],
            "replacement_line": new_line,
        }
    finally:
        _, stdout, _ = ssh_session.client.exec_command(
            f"kill {new_outer_pid} 2>/dev/null || true; rm -f {new_file}"
        )
        stdout.channel.recv_exit_status()


@pytest.fixture(scope="module")
def arthas_client(ssh_session: "SSHSession") -> "ArthasClient":
    """Return an ArthasClient configured for the SSH session."""
    from arthas_mcp_proxy.arthas_client import ArthasClient

    return ArthasClient(ssh_session)


# ---------------------------------------------------------------------------
# 5.  Pytest hooks
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    marker_expr = config.getoption("-m", "")
    if "integration" not in str(marker_expr):
        return

    # When --docker-target is used, env vars are auto-set by the fixture
    if config.getoption("--docker-target") or config.getoption("--docker-targets"):
        return

    missing: list[str] = []
    auth_configured = bool(os.environ.get("TEST_SSH_PASSWORD")) or bool(
        os.environ.get("TEST_SSH_KEY_PATH") or os.environ.get("TEST_SSH_KEY_FILE")
    )
    for name in ("TEST_SSH_HOST", "TEST_SSH_USER"):
        if not os.environ.get(name):
            missing.append(name)
    if not auth_configured:
        missing.append("TEST_SSH_PASSWORD or TEST_SSH_KEY_PATH")

    if missing:
        pytest.exit(
            "\nIntegration tests require an explicitly configured SSH target and authentication.\n"
            f"Missing configuration: {', '.join(missing)}\n"
            "\nPassword authentication:\n"
            "  export TEST_SSH_HOST=<your-server> TEST_SSH_USER=<username>\n"
            "  export TEST_SSH_PASSWORD=<password>\n"
            "\nPrivate-key authentication (the path is passed to Paramiko; the key is never read by this check):\n"
            "  export TEST_SSH_HOST=<your-server> TEST_SSH_USER=<username>\n"
            "  export TEST_SSH_KEY_PATH=$HOME/.ssh/id_ed25519\n"
            "  export TEST_SSH_PORT=22          # optional\n"
            "\nOr use the built-in Docker test target:\n"
            "  pytest tests/integration/ -m integration -v --docker-target\n",
            returncode=1,
        )


def pytest_collection_finish(session: pytest.Session) -> None:
    integration_items = [
        item for item in session.items if item.get_closest_marker("integration")
    ]
    if integration_items:
        logger.info("Collected %d integration test(s)", len(integration_items))
