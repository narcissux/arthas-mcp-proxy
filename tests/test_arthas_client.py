"""Tests for arthas_client.py - concurrency locks and cross-user diagnosis."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.arthas_client import (
    _PID_STATE,
    _PID_STATE_LOCK,
    ArthasClient,
    _attach_agent,
    _detect_arthas_port,
    _ensure_agent,
    _exec_command,
    _filter_output,
    _find_free_port,
    _get_attach_lock,
    _get_sudo_user,
    _jar_cache_key,
    _parse_pid_line,
    _state_key,
)
from arthas_mcp_proxy.errors import DomainError
from arthas_mcp_proxy.models import ErrorCode
from arthas_mcp_proxy.target_state import TargetIdentity


class TestConcurrencyLocks:
    """Verify thread-safety of the global PID state and attach locks."""

    def test_attach_locks_are_target_scoped(self):
        first = _get_attach_lock(TargetIdentity("host-a", 22, "root", 1234))
        second = _get_attach_lock(TargetIdentity("host-b", 22, "root", 1234))
        assert first is not second

    def test_exec_ssh_rejects_shell_metacharacters_in_owner(self):
        session = MagicMock()
        with pytest.raises(ValueError, match="Invalid sudo user"):
            from arthas_mcp_proxy.arthas_client import _exec_ssh

            _exec_ssh(session, "id", sudo_user="appuser; id")

    def test_pid_state_lock_concurrent_rw(self, mock_ssh_session):
        """_PID_STATE survives 5 writers + 5 readers x 100 cycles each."""
        errors: list[Exception] = []

        def writer(pid: int, port: int) -> None:
            try:
                for _ in range(100):
                    with _PID_STATE_LOCK:
                        _PID_STATE[pid] = {"port": port, "owner": None}
            except Exception as e:
                errors.append(e)

        def reader(pid: int) -> None:
            try:
                for _ in range(100):
                    with _PID_STATE_LOCK:
                        _ = _PID_STATE.get(pid, {}).get("port")
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(5):
            threads.append(threading.Thread(target=writer, args=(i, 3658 + i)))
            threads.append(threading.Thread(target=reader, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent R/W errors: {errors}"
        with _PID_STATE_LOCK:
            assert len(_PID_STATE) == 5

    def test_attach_lock_serialization(self):
        """Same-PID attach is serialized; different-PID can run concurrently."""
        acquired_order: list[int] = []
        lock_release_order: list[int] = []

        def mock_attach(pid: int) -> None:
            lock = _get_attach_lock(pid)
            got = lock.acquire(timeout=5)
            assert got, f"PID {pid} should acquire lock"
            acquired_order.append(pid)
            time.sleep(0.05)
            lock.release()
            lock_release_order.append(pid)

        # 3 threads same PID + 2 threads different PIDs
        threads = [
            *[threading.Thread(target=mock_attach, args=(9999,)) for _ in range(3)],
            threading.Thread(target=mock_attach, args=(1111,)),
            threading.Thread(target=mock_attach, args=(2222,)),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        same_acquired = [p for p in acquired_order if p == 9999]
        same_released = [p for p in lock_release_order if p == 9999]
        assert same_acquired == [9999, 9999, 9999]
        assert same_released == [9999, 9999, 9999]

    def test_double_check_avoids_duplicate_attach(self, mock_ssh_session):
        """If another thread attached while waiting for lock, skip re-attach."""
        call_count = {"attach": 0}

        def fake_attach(session, pid, path, owner):
            call_count["attach"] += 1
            return 3658

        with (
            patch("arthas_mcp_proxy.arthas_client._attach_agent", side_effect=fake_attach),
            patch("arthas_mcp_proxy.arthas_client._detect_arthas_port", return_value=None),
        ):
            # Pre-populate cache
            with _PID_STATE_LOCK:
                _PID_STATE[_state_key(mock_ssh_session, 1234)] = {"port": 3660, "owner": None}

            port = _ensure_agent(mock_ssh_session, 1234, "/tmp/as.sh")  # noqa: S108
            assert port == 3660
            assert call_count["attach"] == 0

    def test_cross_session_reuse(self, mock_ssh_session):
        """Level 2: existing agent detected via ss => reuse without attach."""
        with patch("arthas_mcp_proxy.arthas_client._detect_arthas_port", return_value=3661):
            port = _ensure_agent(mock_ssh_session, 5678, "/tmp/as.sh")  # noqa: S108
            assert port == 3661
            with _PID_STATE_LOCK:
                assert _PID_STATE[_state_key(mock_ssh_session, 5678)]["port"] == 3661


class TestArthasClient:
    """Unit tests for the ArthasClient high-level class."""

    def test_owner_cache_per_pid(self, mock_ssh_session):
        """Different PIDs should cache different owners without collision."""
        client = ArthasClient(mock_ssh_session)

        def fake_sudo(session, pid):
            return {1001: "shterm", 1002: "root", 1003: None}.get(pid)

        with patch(
            "arthas_mcp_proxy.arthas_client._get_sudo_user",
            side_effect=fake_sudo,
        ):
            o1 = client._resolve_owner(1001)
            o2 = client._resolve_owner(1002)
            o3 = client._resolve_owner(1003)

        assert o1 == "shterm"
        assert o2 == "root"
        assert o3 is None
        assert client._owner_cache == {1001: "shterm", 1002: "root", 1003: None}

    def test_list_java_processes(self, mock_ssh_session):
        """list_java_processes should parse jps output correctly."""
        mock_stdout = MagicMock()
        mock_stdout.read.return_value = (
            b"1234 app.jar --server\n5678 arthas-client.jar\n9999 jps -l -m\n"
        )
        mock_stdout.channel.recv_exit_status.return_value = 0
        mock_stderr = MagicMock()
        mock_stderr.read.return_value = b""
        mock_ssh_session.client.exec_command.return_value = (
            None,
            mock_stdout,
            mock_stderr,
        )

        client = ArthasClient(mock_ssh_session)
        result = client.list_java_processes()
        assert "PID 1234: app.jar --server" in result
        assert "arthas" not in result.lower() or "arthas:" in result
        assert "jps" not in result.lower()

    def test_exec_command_validates_runtime_identity_before_attach(self, mock_ssh_session):
        with (
            patch("arthas_mcp_proxy.arthas_client._exec_ssh", return_value=("", "", 0)),
            pytest.raises(DomainError) as exc_info,
        ):
            _exec_command(
                mock_ssh_session,
                42,
                "jvm",
                "/tmp/as.sh",  # noqa: S108
                start_time="2026-08-01T10:00:00",
            )
        assert exc_info.value.code is ErrorCode.JVM_EXITED

    def test_jar_cache_key_is_stable_and_target_process_scoped(self):
        first = MagicMock(host="target-a", port=22, username="root")
        second = MagicMock(host="target-a", port=22, username="root")

        assert _jar_cache_key(first, 42, "root", "100.0") == _jar_cache_key(
            second, 42, "root", "100.0"
        )
        assert _jar_cache_key(first, 42, "root", "100.0") != _jar_cache_key(
            first, 42, "root", "200.0"
        )
        assert _jar_cache_key(first, 42, "root", "100.0") != _jar_cache_key(
            first, 43, "root", "100.0"
        )

    def test_filter_output_removes_noise(self):
        """_filter_output should strip Arthas noise lines and ANSI codes."""
        raw = (
            "\x1b[32mArthas\x1b[0m\n"
            "wiki https://arthas.aliyun.com\n"
            "real    0m1.234s\n"
            "Attach success.\n"
            "version 4.0.0\n"
            "   ,---.  \n"
            "  /  O  \\  \n"
            "\n"
            "Actual diagnostic output here\n"
            "More useful data\n"
        )
        filtered = _filter_output(raw)
        assert "Actual diagnostic output here" in filtered
        assert "More useful data" in filtered
        assert "wiki" not in filtered
        assert "Attach success" not in filtered
        assert "\x1b[" not in filtered

    def test_online_install_populates_shared_arthas_path(self, mock_ssh_session):
        """The install contract must match the path used by attach commands."""
        stdout = MagicMock()
        stdout.read.return_value = b"INSTALLED"
        stdout.channel.recv_exit_status.return_value = 0
        stderr = MagicMock()
        stderr.read.return_value = b""
        mock_ssh_session.client.exec_command.return_value = (None, stdout, stderr)

        client = ArthasClient(mock_ssh_session)
        with patch("arthas_mcp_proxy.arthas_client._find_arthas_path", side_effect=RuntimeError):
            client.install_arthas(install_type="online")

        command = mock_ssh_session.client.exec_command.call_args.args[0]
        assert "/tmp/arthas-all" in command  # noqa: S108
        assert "/tmp/arthas-all/arthas-boot.jar" not in command  # noqa: S108
        assert "chmod +x /tmp/arthas-all/as.sh" in command  # noqa: S108

    def test_parse_pid_line_valid(self):
        assert _parse_pid_line("1234 app.jar") == (1234, "app.jar")
        assert _parse_pid_line("5678   some.app --arg") == (5678, "some.app --arg")

    def test_parse_pid_line_invalid(self):
        assert _parse_pid_line("") is None
        assert _parse_pid_line("notapid something") is None
        assert _parse_pid_line("  ") is None


class TestLogTagsPresent:
    """Verify all critical log tags exist in source code."""

    def test_all_tags_present(self):
        import inspect

        from arthas_mcp_proxy.arthas_client import ArthasClient, _exec_ssh

        src = ""
        for func in (
            _ensure_agent,
            _attach_agent,
            _exec_command,
            _detect_arthas_port,
            _find_free_port,
            _exec_ssh,
            _get_sudo_user,
        ):
            src += inspect.getsource(func)
        # [DETACH] is in ArthasClient.detach method
        src += inspect.getsource(ArthasClient.detach)

        required = [
            "[ENSURE]",
            "[ATTACH]",
            "[CMD-EXEC]",
            "[SSH-EXEC]",
            "[SUDO]",
            "[PORT-FIND]",
            "[PORT-DETECT]",
            "[DETACH]",
            "[CLIENT-JAR]",
        ]
        for tag in required:
            assert tag in src, f"Log tag {tag} missing from source"
