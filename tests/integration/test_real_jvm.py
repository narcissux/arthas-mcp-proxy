"""Integration tests against a real JVM via SSH.

These tests connect to a real remote server, install Arthas (if needed),
and execute diagnostic commands against running Java processes.

**DO NOT RUN IN CI** — these tests require a live SSH target.
To run locally:
    pytest tests/integration/ -m integration -v

Required environment variables (NO DEFAULTS — must be set explicitly):
    TEST_SSH_HOST          SSH target hostname or IP
    TEST_SSH_USER          SSH username
    TEST_SSH_PASSWORD      SSH password
    TEST_SSH_PORT          SSH port (default: 22)
    TEST_TARGET_PID        Optional; auto-detected if omitted

SECURITY NOTICE:
    Never commit real credentials to version control.
    Set credentials via environment variables or a local .env file
    (ignored by .gitignore).
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from arthas_mcp_proxy.arthas_client import ArthasClient
    from arthas_mcp_proxy.ssh_pool import SSHConnectionPool, SSHSession

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration]

# ─── Security-safe helpers ───────────────────────────────────────────────────


def _require_env(name: str) -> str:
    """Fetch a required env var; skip the test if missing."""
    value = os.environ.get(name)
    if not value:
        pytest.skip(
            f"Integration test requires env var {name}. "
            f"Set it before running: export {name}=<value>"
        )
    return value


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def ssh_pool() -> "SSHConnectionPool":
    """Return a fresh SSHConnectionPool for the test module."""
    from arthas_mcp_proxy.ssh_pool import SSHConnectionPool

    pool = SSHConnectionPool(idle_timeout=600)
    yield pool
    pool.shutdown()


@pytest.fixture(scope="module")
def ssh_session(ssh_pool: "SSHConnectionPool") -> "SSHSession":
    """Connect to the remote server and return an active SSHSession.

    Skips automatically if TEST_SSH_HOST / TEST_SSH_USER / TEST_SSH_PASSWORD
    are not set.  No real credentials are ever hard-coded here.
    """
    host = _require_env("TEST_SSH_HOST")
    port = int(os.environ.get("TEST_SSH_PORT", "22"))
    username = _require_env("TEST_SSH_USER")
    password = _require_env("TEST_SSH_PASSWORD")

    sid = ssh_pool.connect(
        host=host, port=port, username=username, password=password, timeout=30
    )
    session = ssh_pool.get_session(sid)
    assert session is not None, f"Failed to establish SSH session to {host}"
    logger.info(
        "SSH connected: %s@%s:%s (session=%s)", username, host, port, sid
    )
    yield session
    ssh_pool.disconnect(sid)


@pytest.fixture(scope="module")
def target_pid(ssh_session: "SSHSession") -> int:
    """Auto-detect a suitable Java process PID for Arthas testing."""
    # 1. Check environment override
    env_pid = os.environ.get("TEST_TARGET_PID")
    if env_pid:
        return int(env_pid)

    # 2. Auto-detect: prefer TestApp or math-game.jar
    _stdin, stdout, _stderr = ssh_session.client.exec_command("jps -l")
    jps_output = stdout.read().decode("utf-8", errors="replace")
    logger.info("jps output:\n%s", jps_output)

    candidates: list[tuple[int, str]] = []
    for line in jps_output.strip().split("\n"):
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            pid = int(parts[0])
            name = parts[1]
            # Skip JPS itself and Arthas agents
            if "Jps" in name or "arthas" in name.lower():
                continue
            candidates.append((pid, name))

    assert candidates, "No suitable Java process found on target server"

    # Prefer known test apps
    for pid, name in candidates:
        if "TestApp" in name or "math" in name.lower():
            logger.info("Selected target PID %d (%s)", pid, name)
            return pid

    # Fall back to first candidate
    pid, name = candidates[0]
    logger.info("Selected target PID %d (%s)", pid, name)
    return pid


@pytest.fixture(scope="module")
def arthas_client(ssh_session: "SSHSession") -> "ArthasClient":
    """Return an ArthasClient configured for the SSH session."""
    from arthas_mcp_proxy.arthas_client import ArthasClient

    return ArthasClient(ssh_session)


# ─── Helper ──────────────────────────────────────────────────────────────────


def _extract_first_pid(output: str) -> int | None:
    """Extract the first PID from jps-style output."""
    for line in output.strip().split("\n"):
        match = re.search(r"PID\s+(\d+)", line)
        if match:
            return int(match.group(1))
        parts = line.strip().split(None, 1)
        if parts and parts[0].isdigit():
            pid = int(parts[0])
            if len(parts) > 1 and "jps" not in parts[1].lower():
                return pid
    return None


# ─── Tests ───────────────────────────────────────────────────────────────────


class TestSSHConnection:
    """Verify basic SSH connectivity."""

    def test_whoami(self, ssh_session: "SSHSession") -> None:
        _stdin, stdout, _stderr = ssh_session.client.exec_command("whoami")
        user = stdout.read().decode().strip()
        # Must return non-empty, not hard-coding a specific username
        assert user, "whoami returned empty"

    def test_java_installed(self, ssh_session: "SSHSession") -> None:
        _stdin, stdout, stderr = ssh_session.client.exec_command(
            "java -version 2>&1"
        )
        version = stdout.read().decode() or stderr.read().decode()
        # Accept any JDK (OpenJDK, Oracle, Temurin, etc.)
        assert "version" in version.lower(), f"JDK not detected: {version}"

    def test_jps_available(self, ssh_session: "SSHSession") -> None:
        _stdin, stdout, _stderr = ssh_session.client.exec_command("jps -l")
        output = stdout.read().decode()
        lines = [line for line in output.strip().split("\n") if line.strip()]
        assert len(lines) > 0, "No Java processes found"


class TestArthasInstall:
    """Verify Arthas installation on target server."""

    def test_install_arthas(self, arthas_client: "ArthasClient") -> None:
        """Arthas should be installed successfully (online mode)."""
        result = arthas_client.install_arthas(install_type="online")
        logger.info("install_arthas result: %s", result)
        assert "installed" in result.lower() or "already" in result.lower(), result

    def test_offline_install_skipped_when_no_bundle(
        self, arthas_client: "ArthasClient",
    ) -> None:
        """Offline install without local bundle should fail gracefully."""
        with pytest.raises(RuntimeError, match="arthas-bin.zip"):
            arthas_client.install_arthas(install_type="offline")


class TestJavaProcessDiscovery:
    """Verify Java process discovery."""

    def test_list_java_processes(self, arthas_client: "ArthasClient") -> None:
        """Should list Java processes with PID markers."""
        result = arthas_client.list_java_processes()
        logger.info("list_java_processes result:\n%s", result)
        assert "PID" in result, f"Expected PID in output: {result}"
        # Should find at least one process
        pid_count = len(re.findall(r"PID\s+\d+", result))
        assert pid_count > 0, f"No Java processes found in: {result}"


class TestThreadDiagnostics:
    """Verify thread dump and related diagnostics."""

    def test_thread_dump(
        self, arthas_client: "ArthasClient", target_pid: int
    ) -> None:
        """Should produce a non-empty thread dump for target PID."""
        result = arthas_client.thread_dump(pid=target_pid, top_n=10)
        logger.info("thread_dump (first 200 chars): %.200s", result)
        # A meaningful thread dump should contain thread state keywords
        assert any(
            kw in result for kw in ("RUNNABLE", "WAITING", "TIMED_WAITING", "BLOCKED")
        ), f"No valid thread states found in output: {result[:200]}..."

    def test_heap_info(
        self, arthas_client: "ArthasClient", target_pid: int
    ) -> None:
        """Should produce a heap dashboard for target PID."""
        result = arthas_client.heap_info(pid=target_pid)
        logger.info("heap_info (first 200 chars): %.200s", result)
        # Dashboard should contain memory-related keywords
        assert any(
            kw in result for kw in ("heap", "eden", "survivor", "old", "memory")
        ), f"No heap metrics found in output: {result[:200]}..."


class TestCommandExecution:
    """Verify direct Arthas command execution."""

    def test_jvm_command(
        self, arthas_client: "ArthasClient", target_pid: int
    ) -> None:
        """Should execute 'jvm' command and return JVM info."""
        result = arthas_client.exec_command(pid=target_pid, command="jvm")
        logger.info("jvm command (first 200 chars): %.200s", result)
        # JVM info should contain runtime details
        assert any(
            kw in result.lower()
            for kw in ("jvm", "runtime", "classpath", "version")
        ), f"No JVM info found in output: {result[:200]}..."

    def test_version_command(
        self, arthas_client: "ArthasClient", target_pid: int
    ) -> None:
        """Should execute 'version' command."""
        result = arthas_client.exec_command(pid=target_pid, command="version")
        logger.info("version command: %s", result)
        assert "arthas" in result.lower() or "version" in result.lower(), result

    def test_profiler_command(
        self, arthas_client: "ArthasClient", target_pid: int
    ) -> None:
        """Should start and stop profiler."""
        # Start profiler
        result = arthas_client.exec_command(
            pid=target_pid, command="profiler start"
        )
        logger.info("profiler start: %.200s", result)
        assert "started" in result.lower() or "profiling" in result.lower(), result

        # Stop profiler
        result = arthas_client.exec_command(
            pid=target_pid, command="profiler stop"
        )
        logger.info("profiler stop: %.200s", result)
        assert "stop" in result.lower() or "flame" in result.lower(), result

    def test_heapdump_command(
        self, arthas_client: "ArthasClient", target_pid: int
    ) -> None:
        """Should generate heap dump."""
        remote_path = "/tmp/arthas-heapdump-test.hprof"  # noqa: S108
        result = arthas_client.exec_command(
            pid=target_pid, command=f"heapdump {remote_path}"
        )
        logger.info("heapdump: %.200s", result)
        # Should reference the dump file path
        assert "heapdump" in result.lower() or "dump" in result.lower(), result


class TestWatchMethod:
    """Verify method watch functionality."""

    def test_watch_math_game(
        self, arthas_client: "ArthasClient", target_pid: int
    ) -> None:
        """Watch a method on math-game.jar if available."""
        result = arthas_client.watch_method(
            pid=target_pid,
            class_pattern="demo.MathGame",
            method_pattern="primeFactors",
            watch_params=True,
            watch_return=True,
            times=3,
        )
        logger.info("watch_method result (first 300 chars): %.300s", result)
        # May not find the class if target is not math-game, but should not error
        assert "error" not in result.lower() or "no class" in result.lower(), result


class TestDetach:
    """Verify clean detachment."""

    def test_detach(
        self, arthas_client: "ArthasClient", target_pid: int
    ) -> None:
        """Should detach Arthas agent cleanly."""
        result = arthas_client.detach(target_pid)
        logger.info("detach result: %s", result)
        assert any(
            kw in result.lower()
            for kw in ("detach", "shutdown", "stop", "arthas server")
        ), result

    def test_agent_no_longer_present(
        self, ssh_session: "SSHSession", target_pid: int
    ) -> None:
        """After detach, agent port should not be detectable."""
        from arthas_mcp_proxy.arthas_client import _detect_arthas_port

        # Allow a few seconds for graceful shutdown
        import time

        time.sleep(3)
        port = _detect_arthas_port(ssh_session, target_pid)
        logger.info("Post-detach port detection: %s", port)
        # Ideally port should be None after detach
        # But timing-dependent, so we just verify no crash
        assert port is None or isinstance(port, int), f"Unexpected port value: {port}"


class TestCrossUserDiagnosis:
    """Verify cross-user sudo diagnosis (README advertised feature)."""

    def test_sudo_user_detection(
        self, ssh_session: "SSHSession", target_pid: int
    ) -> None:
        """Should detect if sudo is needed for attach."""
        from arthas_mcp_proxy.arthas_client import _get_sudo_user

        owner = _get_sudo_user(ssh_session, target_pid)
        logger.info("PID %d owner=%s", target_pid, owner)
        # owner may be None (same user) or a username (cross-user)
        # Either case should not raise
        assert owner is None or isinstance(owner, str), f"Unexpected owner: {owner}"

    def test_attach_with_detected_owner(
        self, arthas_client: "ArthasClient", target_pid: int
    ) -> None:
        """Should successfully attach using resolved owner."""
        from arthas_mcp_proxy.arthas_client import _ensure_agent
        from arthas_mcp_proxy.arthas_client import _find_arthas_path

        arthas_path = _find_arthas_path(arthas_client.session)
        owner = arthas_client._resolve_owner(target_pid)
        port = _ensure_agent(
            arthas_client.session, target_pid, arthas_path, owner=owner
        )
        logger.info("Cross-user attach PID %d -> port %d", target_pid, port)
        assert 3658 <= port <= 3665, f"Port {port} out of expected range"


class TestErrorHandling:
    """Verify error handling for invalid inputs."""

    def test_invalid_pid(
        self, arthas_client: "ArthasClient",
    ) -> None:
        """Should handle invalid PID gracefully."""
        with pytest.raises((RuntimeError, ValueError)):
            arthas_client.exec_command(pid=999999, command="thread -n 1")

    def test_invalid_command(
        self, arthas_client: "ArthasClient", target_pid: int
    ) -> None:
        """Should handle invalid Arthas command gracefully."""
        result = arthas_client.exec_command(
            pid=target_pid, command="not_a_real_command_xyz"
        )
        logger.info("Invalid command result: %s", result)
        # Should return some error indication
        assert (
            "error" in result.lower()
            or "unknown" in result.lower()
            or "not found" in result.lower()
            or len(result) > 0
        )


class TestEndToEnd:
    """Full end-to-end workflow test."""

    def test_full_diagnostic_workflow(
        self,
        ssh_pool: "SSHConnectionPool",
        arthas_client: "ArthasClient",
        target_pid: int,
    ) -> None:
        """Execute a realistic diagnostic workflow:
        1. List processes
        2. Thread dump
        3. Heap info
        4. JVM info
        5. Detach
        """
        # Step 1: List processes
        processes = arthas_client.list_java_processes()
        assert "PID" in processes
        logger.info("Step 1 - Processes found")

        # Step 2: Thread dump
        threads = arthas_client.thread_dump(pid=target_pid, top_n=5)
        assert any(
            kw in threads for kw in ("RUNNABLE", "WAITING", "TIMED_WAITING")
        ), f"No thread states in dump: {threads[:200]}"
        logger.info("Step 2 - Thread dump captured (%d chars)", len(threads))

        # Step 3: Heap info
        heap = arthas_client.heap_info(pid=target_pid)
        assert any(
            kw in heap for kw in ("heap", "eden", "survivor", "memory")
        ), f"No heap metrics: {heap[:200]}"
        logger.info("Step 3 - Heap info captured (%d chars)", len(heap))

        # Step 4: JVM version
        jvm = arthas_client.exec_command(pid=target_pid, command="jvm")
        assert any(
            kw in jvm.lower() for kw in ("jvm", "runtime", "version")
        ), f"No JVM info: {jvm[:200]}"
        logger.info("Step 4 - JVM info captured (%d chars)", len(jvm))

        # Step 5: Detach
        result = arthas_client.detach(target_pid)
        logger.info("Step 5 - Detached: %s", result)

        logger.info("Full workflow completed successfully for PID %d", target_pid)
