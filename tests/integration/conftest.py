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


# ---------------------------------------------------------------------------
# 2.  Env-var helper
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(
            f"Integration test requires env var {name}. "
            f"Set it before running: export {name}=<value>"
        )
    return value


# ---------------------------------------------------------------------------
# 3.  Docker lifecycle fixture
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    try:
        subprocess.run(
            [docker, "version"],
            capture_output=True,
            timeout=5,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


def _wait_for_ssh(host: str, port: int, timeout: int = 60) -> bool:
    """Poll until SSH port is accepting connections."""
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(1)
    return False


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

    if not _docker_available():
        pytest.skip("Docker daemon not available; cannot start test target")

    compose_file = os.path.join(
        os.path.dirname(__file__), "..", "..", "docker-compose.test.yml"
    )
    if not os.path.isfile(compose_file):
        pytest.skip(f"Docker compose file not found: {compose_file}")

    logger.info("Building test target container...")
    try:
        subprocess.run(
            ["docker", "compose", "-f", compose_file, "up", "--build", "-d"],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"Failed to start Docker test target: {exc.stderr}")
    except FileNotFoundError:
        pytest.skip("docker compose command not found")

    # Wait for SSH
    logger.info("Waiting for SSH on localhost:2222 ...")
    if not _wait_for_ssh("localhost", 2222, timeout=60):
        subprocess.run(
            ["docker", "compose", "-f", compose_file, "down", "--volumes"],
            capture_output=True,
        )
        pytest.skip("SSH port 2222 not ready after 60s")

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
    subprocess.run(
        ["docker", "compose", "-f", compose_file, "down", "--volumes"],
        capture_output=True,
    )


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
        pytest.skip("No SSH target configured. Use --docker-target or set TEST_SSH_HOST")

    port = int(os.environ.get("TEST_SSH_PORT", docker_test_target.get("port", "22")))
    username = os.environ.get("TEST_SSH_USER") or docker_test_target.get("user")
    password = os.environ.get("TEST_SSH_PASSWORD") or docker_test_target.get("password")

    if not username or not password:
        pytest.skip("SSH username/password not configured")

    sid = ssh_pool.connect(
        host=host, port=port, username=username, password=password, timeout=30
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
    if config.getoption("--docker-target"):
        return

    missing: list[str] = []
    for name in ("TEST_SSH_HOST", "TEST_SSH_USER", "TEST_SSH_PASSWORD"):
        if not os.environ.get(name):
            missing.append(name)

    if missing:
        pytest.exit(
            "\nIntegration tests require SSH target credentials.\n"
            f"Missing env vars: {', '.join(missing)}\n"
            "\nSet them before running integration tests:\n"
            "  export TEST_SSH_HOST=<your-server>\n"
            "  export TEST_SSH_USER=<username>\n"
            "  export TEST_SSH_PASSWORD=<password>\n"
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
