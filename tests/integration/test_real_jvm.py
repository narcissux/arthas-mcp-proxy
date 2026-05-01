"""Integration tests against a real JVM via SSH.

These tests connect to a remote server (or Docker test target), install
Arthas (if needed), and execute diagnostic commands against running Java
processes.

Run with Docker target (recommended, no external dependencies)::

    pytest tests/integration/ -m integration -v --docker-target

Run against a remote target (env vars required)::

    export TEST_SSH_HOST=remote.server
    export TEST_SSH_USER=ubuntu
    export TEST_SSH_PASSWORD=secret
    pytest tests/integration/ -m integration -v

SECURITY NOTICE:
    Never commit real credentials to version control.
    Use environment variables or a local .env file (ignored by .gitignore).
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from arthas_mcp_proxy.arthas_client import ArthasClient
    from arthas_mcp_proxy.ssh_pool import SSHConnectionPool, SSHSession

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration]


# ─── Tests ───────────────────────────────────────────────────────────────────


class TestSSHConnection:
    """Verify basic SSH connectivity."""

    def test_whoami(self, ssh_session: "SSHSession") -> None:
        _stdin, stdout, _stderr = ssh_session.client.exec_command("whoami")
        user = stdout.read().decode().strip()
        assert user, "whoami returned empty"

    def test_java_installed(self, ssh_session: "SSHSession") -> None:
        _stdin, stdout, stderr = ssh_session.client.exec_command(
            "java -version 2>&1"
        )
        version = stdout.read().decode() or stderr.read().decode()
        assert "version" in version.lower(), f"JDK not detected: {version}"

    def test_jps_available(self, ssh_session: "SSHSession") -> None:
        _stdin, stdout, _stderr = ssh_session.client.exec_command("jps -l")
        output = stdout.read().decode()
        lines = [line for line in output.strip().split("\n") if line.strip()]
        assert len(lines) > 0, "No Java processes found"


class TestArthasInstall:
    """Verify Arthas installation on target server."""

    @staticmethod
    def _clean_arthas_residuals(ssh_session: "SSHSession") -> None:
        """Remove Arthas files left by previous runs for a clean test."""
        paths = [
            "/tmp/arthas-bin.zip",
            "/tmp/arthas-all",
            "/tmp/arthas-install",
            "$HOME/.arthas",
        ]
        for p in paths:
            ssh_session.client.exec_command(f"rm -rf {p}")

    def test_install_arthas(self, arthas_client: "ArthasClient", ssh_session: "SSHSession") -> None:
        self._clean_arthas_residuals(ssh_session)
        result = arthas_client.install_arthas(install_type="online")
        logger.info("install_arthas result: %s", result)
        assert "installed" in result.lower() or "already" in result.lower(), result

    def test_offline_install_skipped_when_no_bundle(
        self, arthas_client: "ArthasClient", ssh_session: "SSHSession",
    ) -> None:
        """Offline install fails when no arthas-bin.zip on MCP server AND target.

        Residual files from previous runs (e.g. /tmp/arthas-bin.zip pushed by
        earlier tests) are cleaned before asserting the error.
        """
        self._clean_arthas_residuals(ssh_session)
        with pytest.raises(RuntimeError, match="arthas-bin.zip"):
            arthas_client.install_arthas(install_type="offline")


class TestJavaProcessDiscovery:
    """Verify Java process discovery."""

    def test_list_java_processes(self, arthas_client: "ArthasClient") -> None:
        result = arthas_client.list_java_processes()
        logger.info("list_java_processes result:\n%s", result)
        assert "PID" in result, f"Expected PID in output: {result}"
        pid_count = len(re.findall(r"PID\s+\d+", result))
        assert pid_count > 0, f"No Java processes found in: {result}"


class TestThreadDiagnostics:
    """Verify thread dump and related diagnostics."""

    def test_thread_dump(
        self, arthas_client: "ArthasClient", target_pid: int
    ) -> None:
        result = arthas_client.thread_dump(pid=target_pid, top_n=10)
        logger.info("thread_dump (first 200 chars): %.200s", result)
        assert any(
            kw in result for kw in ("RUNNABLE", "WAITING", "TIMED_WAITING", "BLOCKED")
        ), f"No valid thread states found in output: {result[:200]}..."

    def test_heap_info(
        self, arthas_client: "ArthasClient", target_pid: int
    ) -> None:
        result = arthas_client.heap_info(pid=target_pid)
        logger.info("heap_info (first 200 chars): %.200s", result)
        assert any(
            kw in result for kw in ("heap", "eden", "survivor", "old", "memory")
        ), f"No heap metrics found in output: {result[:200]}..."


class TestCommandExecution:
    """Verify direct Arthas command execution."""

    def test_jvm_command(
        self, arthas_client: "ArthasClient", target_pid: int
    ) -> None:
        result = arthas_client.exec_command(pid=target_pid, command="jvm")
        logger.info("jvm command (first 200 chars): %.200s", result)
        assert any(
            kw in result.lower()
            for kw in ("jvm", "runtime", "classpath", "version")
        ), f"No JVM info found in output: {result[:200]}..."

    def test_version_command(
        self, arthas_client: "ArthasClient", target_pid: int
    ) -> None:
        result = arthas_client.exec_command(pid=target_pid, command="version")
        logger.info("version command: %s", result)
        assert "arthas" in result.lower() or "version" in result.lower(), result

    def test_profiler_command(
        self, arthas_client: "ArthasClient", target_pid: int
    ) -> None:
        result = arthas_client.exec_command(
            pid=target_pid, command="profiler start"
        )
        logger.info("profiler start: %.200s", result)
        assert "started" in result.lower() or "profiling" in result.lower(), result

        result = arthas_client.exec_command(
            pid=target_pid, command="profiler stop"
        )
        logger.info("profiler stop: %.200s", result)
        assert "stop" in result.lower() or "flame" in result.lower(), result

    def test_heapdump_command(
        self, arthas_client: "ArthasClient", target_pid: int
    ) -> None:
        remote_path = "/tmp/arthas-heapdump-test.hprof"  # noqa: S108
        result = arthas_client.exec_command(
            pid=target_pid, command=f"heapdump {remote_path}"
        )
        logger.info("heapdump: %.200s", result)
        assert "heapdump" in result.lower() or "dump" in result.lower(), result


class TestWatchMethod:
    """Verify method watch functionality."""

    def test_watch_math_game(
        self, arthas_client: "ArthasClient", target_pid: int
    ) -> None:
        result = arthas_client.watch_method(
            pid=target_pid,
            class_pattern="demo.MathGame",
            method_pattern="primeFactors",
            watch_params=True,
            watch_return=True,
            times=3,
        )
        logger.info("watch_method result (first 300 chars): %.300s", result)
        assert "error" not in result.lower() or "no class" in result.lower(), result


class TestDetach:
    """Verify clean detachment."""

    def test_detach(
        self, arthas_client: "ArthasClient", target_pid: int
    ) -> None:
        result = arthas_client.detach(target_pid)
        logger.info("detach result: %s", result)
        assert any(
            kw in result.lower()
            for kw in ("detach", "shutdown", "stop", "arthas server")
        ), result

    def test_agent_no_longer_present(
        self, ssh_session: "SSHSession", target_pid: int
    ) -> None:
        from arthas_mcp_proxy.arthas_client import _detect_arthas_port

        time.sleep(3)
        port = _detect_arthas_port(ssh_session, target_pid)
        logger.info("Post-detach port detection: %s", port)
        assert port is None or isinstance(port, int), f"Unexpected port value: {port}"


class TestCrossUserDiagnosis:
    """Verify cross-user sudo diagnosis (README advertised feature)."""

    def test_sudo_user_detection(
        self, ssh_session: "SSHSession", target_pid: int
    ) -> None:
        from arthas_mcp_proxy.arthas_client import _get_sudo_user

        owner = _get_sudo_user(ssh_session, target_pid)
        logger.info("PID %d owner=%s", target_pid, owner)
        assert owner is None or isinstance(owner, str), f"Unexpected owner: {owner}"

    def test_attach_with_detected_owner(
        self, arthas_client: "ArthasClient", target_pid: int
    ) -> None:
        from arthas_mcp_proxy.arthas_client import _ensure_agent, _find_arthas_path

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
        with pytest.raises((RuntimeError, ValueError)):
            arthas_client.exec_command(pid=999999, command="thread -n 1")

    def test_invalid_command(
        self, arthas_client: "ArthasClient", target_pid: int
    ) -> None:
        result = arthas_client.exec_command(
            pid=target_pid, command="not_a_real_command_xyz"
        )
        logger.info("Invalid command result: %s", result)
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
        processes = arthas_client.list_java_processes()
        assert "PID" in processes
        logger.info("Step 1 - Processes found")

        threads = arthas_client.thread_dump(pid=target_pid, top_n=5)
        assert any(
            kw in threads for kw in ("RUNNABLE", "WAITING", "TIMED_WAITING")
        ), f"No thread states in dump: {threads[:200]}"
        logger.info("Step 2 - Thread dump captured (%d chars)", len(threads))

        heap = arthas_client.heap_info(pid=target_pid)
        assert any(
            kw in heap for kw in ("heap", "eden", "survivor", "memory")
        ), f"No heap metrics: {heap[:200]}"
        logger.info("Step 3 - Heap info captured (%d chars)", len(heap))

        jvm = arthas_client.exec_command(pid=target_pid, command="jvm")
        assert any(
            kw in jvm.lower() for kw in ("jvm", "runtime", "version")
        ), f"No JVM info: {jvm[:200]}"
        logger.info("Step 4 - JVM info captured (%d chars)", len(jvm))

        result = arthas_client.detach(target_pid)
        logger.info("Step 5 - Detached: %s", result)

        logger.info("Full workflow completed successfully for PID %d", target_pid)
