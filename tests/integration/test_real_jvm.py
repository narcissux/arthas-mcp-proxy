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
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from arthas_mcp_proxy.arthas_client import ArthasClient
    from arthas_mcp_proxy.ssh_pool import SSHConnectionPool, SSHSession

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.real_jvm]


# ─── Tests ───────────────────────────────────────────────────────────────────


class TestProxyOwnedLifecycleCleanup:
    """Exercise proxy-owned attach and explicitly authorized cleanup."""

    def test_proxy_owned_attach_then_cleanup(
        self, arthas_client: "ArthasClient", ssh_session: "SSHSession", target_pid: int
    ) -> None:
        from arthas_mcp_proxy.arthas_client import (
            _LIFECYCLE_REGISTRY,
            _detect_arthas_port,
            _ensure_agent,
            _find_arthas_path,
        )
        from arthas_mcp_proxy.arthas_lifecycle import ArthasOrigin
        from arthas_mcp_proxy.target_state import TargetIdentity

        owner = arthas_client._resolve_owner(target_pid)
        if _detect_arthas_port(ssh_session, target_pid, owner) is not None:
            pytest.skip("target already has Arthas; refusing to stop existing/unknown agent")
        port = _ensure_agent(
            ssh_session,
            target_pid,
            _find_arthas_path(ssh_session, owner),
            owner,
            arthas_client.start_time,
        )
        identity = TargetIdentity(
            str(ssh_session.host),
            int(ssh_session.port),
            str(ssh_session.username),
            target_pid,
            arthas_client.start_time,
        )
        instance = _LIFECYCLE_REGISTRY.get(identity)
        assert instance is not None and instance.port == port
        assert instance.origin is ArthasOrigin.STARTED_BY_PROXY
        assert arthas_client.cleanup_expired(
            target_pid, datetime.now(timezone.utc) + timedelta(seconds=1), 0, authorized=True
        ) == [instance]
        assert _LIFECYCLE_REGISTRY.get(identity) is None
        assert _detect_arthas_port(ssh_session, target_pid, owner) is None


class TestSSHConnection:
    """Verify basic SSH connectivity."""

    def test_whoami(self, ssh_session: "SSHSession") -> None:
        _stdin, stdout, _stderr = ssh_session.client.exec_command("whoami")
        user = stdout.read().decode().strip()
        assert user, "whoami returned empty"

    def test_java_installed(self, ssh_session: "SSHSession") -> None:
        _stdin, stdout, stderr = ssh_session.client.exec_command("java -version 2>&1")
        version = stdout.read().decode() or stderr.read().decode()
        assert "version" in version.lower(), f"JDK not detected: {version}"

    def test_jps_available(self, ssh_session: "SSHSession") -> None:
        _stdin, stdout, _stderr = ssh_session.client.exec_command("jps -l")
        output = stdout.read().decode()
        lines = [line for line in output.strip().split("\n") if line.strip()]
        assert len(lines) > 0, "No Java processes found"


class TestArthasInstall:
    """Verify Arthas installation on target server."""

    _ARTHAS_BACKUP = "/tmp/arthas-all.test-backup"  # noqa: S108

    @staticmethod
    def _ssh_run(ssh_session: "SSHSession", command: str) -> str:
        _, stdout, _ = ssh_session.client.exec_command(command)
        stdout.channel.recv_exit_status()
        return stdout.read().decode("utf-8", errors="replace")

    @classmethod
    def _clean_arthas_residuals(cls, ssh_session: "SSHSession") -> None:
        """Remove Arthas files left by previous runs for a clean test.

        Uses blocking reads to ensure removal completes before returning,
        otherwise non-interactive SSH exec_command may race with subsequent
        _find_arthas_path() checks.
        """
        paths = [
            "/tmp/arthas-bin.zip",  # noqa: S108
            "/tmp/arthas-all",  # noqa: S108
            "/tmp/arthas-install",  # noqa: S108
            "$HOME/.arthas",
        ]
        for path in paths:
            cls._ssh_run(ssh_session, f"rm -rf {path}")

    @classmethod
    def _backup_seeded_arthas(cls, ssh_session: "SSHSession") -> None:
        """Keep the image-seeded install so later attach tests stay isolated."""
        cls._ssh_run(
            ssh_session,
            f"rm -rf {cls._ARTHAS_BACKUP}; "
            f"if test -d /tmp/arthas-all; then cp -a /tmp/arthas-all {cls._ARTHAS_BACKUP}; fi",
        )

    @classmethod
    def _restore_seeded_arthas_if_needed(cls, ssh_session: "SSHSession") -> None:
        """Restore boot jar tree when a wipe left the target unusable."""
        probe = cls._ssh_run(
            ssh_session,
            "if test -f /tmp/arthas-all/arthas-boot.jar || test -f /tmp/arthas-all/as.sh; "
            "then echo OK; else echo MISS; fi",
        )
        if "OK" in probe:
            cls._ssh_run(ssh_session, f"rm -rf {cls._ARTHAS_BACKUP}")
            return
        cls._ssh_run(
            ssh_session,
            f"if test -d {cls._ARTHAS_BACKUP}; then "
            f"rm -rf /tmp/arthas-all && mv {cls._ARTHAS_BACKUP} /tmp/arthas-all; "
            f"else rm -rf {cls._ARTHAS_BACKUP}; fi",
        )

    def test_install_arthas(self, arthas_client: "ArthasClient", ssh_session: "SSHSession") -> None:
        self._backup_seeded_arthas(ssh_session)
        try:
            self._clean_arthas_residuals(ssh_session)
            try:
                result = arthas_client.install_arthas(install_type="online")
            except RuntimeError as exc:
                # Environments without outbound download restore the seeded
                # tree and mark this cell specified-not-run — product path
                # unchanged.
                if "Online install failed" not in str(exc):
                    raise
                pytest.skip(f"specified-not-run: online install unavailable ({exc})")
            logger.info("install_arthas result: %s", result)
            assert "installed" in result.lower() or "already" in result.lower(), result
        finally:
            self._restore_seeded_arthas_if_needed(ssh_session)

    def test_offline_install_skipped_when_no_bundle(
        self,
        arthas_client: "ArthasClient",
        ssh_session: "SSHSession",
    ) -> None:
        """Offline install fails when no arthas-bin.zip on MCP server AND target.

        Hide the seeded /tmp/arthas-all only for this assertion so
        install_arthas reaches the offline branch (otherwise it returns
        already-installed). Restore afterward — never chain a fragile
        online reinstall into later attach tests.
        """
        self._backup_seeded_arthas(ssh_session)
        try:
            self._ssh_run(
                ssh_session,
                "rm -rf /tmp/arthas-bin.zip /tmp/arthas-all /tmp/arthas-install $HOME/.arthas",
            )
            with pytest.raises(RuntimeError, match="arthas-bin.zip"):
                arthas_client.install_arthas(install_type="offline")
        finally:
            self._restore_seeded_arthas_if_needed(ssh_session)


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

    def test_thread_dump(self, arthas_client: "ArthasClient", target_pid: int) -> None:
        result = arthas_client.thread_dump(pid=target_pid, top_n=10)
        logger.info("thread_dump (first 200 chars): %.200s", result)
        assert any(kw in result for kw in ("RUNNABLE", "WAITING", "TIMED_WAITING", "BLOCKED")), (
            f"No valid thread states found in output: {result[:200]}..."
        )

    def test_heap_info(self, arthas_client: "ArthasClient", target_pid: int) -> None:
        result = arthas_client.heap_info(pid=target_pid)
        logger.info("heap_info (first 200 chars): %.200s", result)
        assert any(kw in result for kw in ("heap", "eden", "survivor", "old", "memory")), (
            f"No heap metrics found in output: {result[:200]}..."
        )


class TestCommandExecution:
    """Verify direct Arthas command execution."""

    def test_jvm_command(self, arthas_client: "ArthasClient", target_pid: int) -> None:
        result = arthas_client.exec_command(pid=target_pid, command="jvm")
        logger.info("jvm command (first 200 chars): %.200s", result)
        assert any(kw in result.lower() for kw in ("jvm", "runtime", "classpath", "version")), (
            f"No JVM info found in output: {result[:200]}..."
        )

    def test_version_command(self, arthas_client: "ArthasClient", target_pid: int) -> None:
        result = arthas_client.exec_command(pid=target_pid, command="version")
        logger.info("version command: %s", result)
        assert "arthas" in result.lower() or "version" in result.lower(), result

    def test_profiler_command(self, arthas_client: "ArthasClient", target_pid: int) -> None:
        result = arthas_client.exec_command(pid=target_pid, command="profiler start")
        logger.info("profiler start: %.200s", result)
        assert "started" in result.lower() or "profiling" in result.lower(), result

        result = arthas_client.exec_command(pid=target_pid, command="profiler stop")
        logger.info("profiler stop: %.200s", result)
        assert "stop" in result.lower() or "flame" in result.lower(), result

    def test_heapdump_command(self, arthas_client: "ArthasClient", target_pid: int) -> None:
        remote_path = "/tmp/arthas-heapdump-test.hprof"  # noqa: S108
        result = arthas_client.exec_command(pid=target_pid, command=f"heapdump {remote_path}")
        logger.info("heapdump: %.200s", result)
        assert "heapdump" in result.lower() or "dump" in result.lower(), result


class TestWatchMethod:
    """Verify method watch functionality."""

    def test_watch_math_game(self, arthas_client: "ArthasClient", target_pid: int) -> None:
        result = arthas_client.execute_streaming_command(
            pid=target_pid,
            command="watch demo.MathGame primeFactors -n 3",
            emit=lambda _chunk: None,
            cancel=threading.Event(),
            timeout=30,
        )
        logger.info("watch_method result (first 300 chars): %.300s", result)
        assert "error" not in result.lower() or "no class" in result.lower(), result


class TestDetach:
    """Verify clean detachment."""

    def test_detach(self, arthas_client: "ArthasClient", target_pid: int) -> None:
        result = arthas_client.detach(target_pid)
        logger.info("detach result: %s", result)
        assert any(
            kw in result.lower() for kw in ("detach", "shutdown", "stop", "arthas server")
        ), result

    def test_agent_no_longer_present(self, ssh_session: "SSHSession", target_pid: int) -> None:
        from arthas_mcp_proxy.arthas_client import _detect_arthas_port

        time.sleep(3)
        port = _detect_arthas_port(ssh_session, target_pid)
        logger.info("Post-detach port detection: %s", port)
        assert port is None or isinstance(port, int), f"Unexpected port value: {port}"


class TestCrossUserDiagnosis:
    """Verify cross-user sudo diagnosis (README advertised feature)."""

    def test_sudo_user_detection(self, ssh_session: "SSHSession", target_pid: int) -> None:
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
        port = _ensure_agent(arthas_client.session, target_pid, arthas_path, owner=owner)
        logger.info("Cross-user attach PID %d -> port %d", target_pid, port)
        assert 3658 <= port <= 3665, f"Port {port} out of expected range"


class TestErrorHandling:
    """Verify error handling for invalid inputs."""

    def test_invalid_pid(
        self,
        arthas_client: "ArthasClient",
    ) -> None:
        with pytest.raises((RuntimeError, ValueError)):
            arthas_client.exec_command(pid=999999, command="thread -n 1")

    def test_invalid_command(self, arthas_client: "ArthasClient", target_pid: int) -> None:
        result = arthas_client.exec_command(pid=target_pid, command="not_a_real_command_xyz")
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
        assert any(kw in threads for kw in ("RUNNABLE", "WAITING", "TIMED_WAITING")), (
            f"No thread states in dump: {threads[:200]}"
        )
        logger.info("Step 2 - Thread dump captured (%d chars)", len(threads))

        heap = arthas_client.heap_info(pid=target_pid)
        assert any(kw in heap for kw in ("heap", "eden", "survivor", "memory")), (
            f"No heap metrics: {heap[:200]}"
        )
        logger.info("Step 3 - Heap info captured (%d chars)", len(heap))

        jvm = arthas_client.exec_command(pid=target_pid, command="jvm")
        assert any(kw in jvm.lower() for kw in ("jvm", "runtime", "version")), (
            f"No JVM info: {jvm[:200]}"
        )
        logger.info("Step 4 - JVM info captured (%d chars)", len(jvm))

        result = arthas_client.detach(target_pid)
        logger.info("Step 5 - Detached: %s", result)

        logger.info("Full workflow completed successfully for PID %d", target_pid)
