"""Tests for ssh_pool.py - connection pool lifecycle and thread safety."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import paramiko
import pytest

from arthas_mcp_proxy.errors import DomainError
from arthas_mcp_proxy.models import ErrorCode
from arthas_mcp_proxy.ssh_pool import (
    SSHConnectionPool,
    SSHSession,
    _safe_close_client,
)


def _active_client() -> MagicMock:
    client = MagicMock()
    transport = MagicMock()
    transport.is_active.return_value = True
    client.get_transport.return_value = transport
    return client


class TestSafeCloseClient:
    """Tests for the _safe_close_client helper."""

    def test_safe_close_none(self) -> None:
        """Should not raise when client is None."""
        _safe_close_client(None)  # no exception

    def test_safe_close_with_transport(self) -> None:
        """Should close transport then client."""
        client = MagicMock(spec=paramiko.SSHClient)
        transport = MagicMock()
        client.get_transport.return_value = transport

        _safe_close_client(client)
        transport.close.assert_called_once()
        client.close.assert_called_once()

    def test_safe_close_no_transport(self) -> None:
        """Should handle when get_transport returns None."""
        client = MagicMock(spec=paramiko.SSHClient)
        client.get_transport.return_value = None

        _safe_close_client(client)
        client.close.assert_called_once()

    def test_safe_close_transport_raises(self) -> None:
        """Should continue to client.close even if transport.close raises."""
        client = MagicMock(spec=paramiko.SSHClient)
        transport = MagicMock()
        transport.close.side_effect = OSError("boom")
        client.get_transport.return_value = transport

        _safe_close_client(client)
        client.close.assert_called_once()


class TestSSHConnectionPool:
    """Tests for SSHConnectionPool lifecycle and behavior."""

    @patch("arthas_mcp_proxy.ssh_pool.paramiko.SSHClient")
    @patch("arthas_mcp_proxy.ssh_pool.threading.Thread")
    def test_connect_new_session(self, mock_thread: MagicMock, mock_sshclient: MagicMock) -> None:
        """Creating a new connection should return a session_id."""
        mock_transport = MagicMock()
        mock_transport.is_active.return_value = True
        mock_client = MagicMock()
        mock_client.get_transport.return_value = mock_transport
        mock_sshclient.return_value = mock_client

        pool = SSHConnectionPool(idle_timeout=300)
        sid = pool.connect("192.168.1.1", username="root", password="test")

        assert isinstance(sid, str)
        assert len(sid) == 8
        mock_client.connect.assert_called_once()
        mock_transport.set_keepalive.assert_called_once()

    @patch("arthas_mcp_proxy.ssh_pool.paramiko.SSHClient")
    @patch("arthas_mcp_proxy.ssh_pool.threading.Thread")
    def test_connect_reuses_existing(
        self, mock_thread: MagicMock, mock_sshclient: MagicMock
    ) -> None:
        """Same host+user should reuse the existing connection."""
        mock_transport = MagicMock()
        mock_transport.is_active.return_value = True
        mock_client = MagicMock()
        mock_client.get_transport.return_value = mock_transport
        mock_sshclient.return_value = mock_client

        pool = SSHConnectionPool(idle_timeout=300)
        sid1 = pool.connect("192.168.1.1", username="root", password="test")
        sid2 = pool.connect("192.168.1.1", username="root", password="test")

        assert sid1 == sid2
        assert mock_client.connect.call_count == 1

    def test_concurrent_same_key_connect_deduplicates(self) -> None:
        """Concurrent same-key connects must yield a single session.

        Two threads race to connect with identical credentials. The mocked
        client.connect() blocks on a barrier so both threads pass the pool's
        existence check and start establishing a connection before either has
        stored its session. If the pool creates connections without holding
        its lock across the connect, both threads return distinct session ids
        and the second store silently overwrites the first — the test exposes
        that duplicate.
        """
        first_connect_started = threading.Event()
        release_first_connect = threading.Event()

        def make_client() -> MagicMock:
            client = MagicMock()
            transport = MagicMock()
            transport.is_active.return_value = True
            client.get_transport.return_value = transport

            def connect(**kwargs):
                first_connect_started.set()
                release_first_connect.wait(timeout=5)

            client.connect.side_effect = connect
            return client

        with patch(
            "arthas_mcp_proxy.ssh_pool.paramiko.SSHClient",
            side_effect=make_client,
        ):
            pool = SSHConnectionPool(idle_timeout=300)

            results: list[str] = []

            def do_connect() -> None:
                results.append(pool.connect("192.168.1.1", username="root", password="test"))

            t1 = threading.Thread(target=do_connect)
            t2 = threading.Thread(target=do_connect)
            t1.start()
            assert first_connect_started.wait(timeout=5)
            t2.start()
            release_first_connect.set()
            t1.join(timeout=10)
            t2.join(timeout=10)

            assert not t1.is_alive() and not t2.is_alive(), "connect hung"
            assert len(results) == 2
            sid1, sid2 = results
            assert sid1 == sid2, f"duplicate session created: {sid1} != {sid2}"
            assert len(pool._sessions) == 1

    def test_max_sessions_enforced_after_serialized_connect(self) -> None:
        """Concurrent connects for different hosts must not exceed MAX_SESSIONS.

        MAX_SESSIONS is capped at 1. Two threads connect to different hosts and
        both pass the pool's pre-connect capacity check (the pool is empty at
        that point) before blocking inside the mocked client.connect(). When
        both complete, the pool must hold at most one session and the losing
        connection must be rejected with a pool-capacity error — the
        pre-connect check alone would let both insert and oversubscribe.
        """
        first_connect_started = threading.Event()
        second_connect_started = threading.Event()
        release_connects = threading.Event()

        def make_client() -> MagicMock:
            client = MagicMock()
            transport = MagicMock()
            transport.is_active.return_value = True
            client.get_transport.return_value = transport

            def connect(**kwargs):
                if kwargs.get("hostname") == "192.168.1.1":
                    first_connect_started.set()
                else:
                    second_connect_started.set()
                release_connects.wait(timeout=5)

            client.connect.side_effect = connect
            return client

        with (
            patch(
                "arthas_mcp_proxy.ssh_pool.paramiko.SSHClient",
                side_effect=make_client,
            ),
            patch("arthas_mcp_proxy.ssh_pool.MAX_SESSIONS", 1),
        ):
            pool = SSHConnectionPool(idle_timeout=300)

            results: list[str] = []
            errors: list[Exception] = []

            def do_connect(host: str, password: str) -> None:
                try:
                    results.append(pool.connect(host, username="root", password=password))
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            t1 = threading.Thread(target=do_connect, args=("192.168.1.1", "one"))
            t2 = threading.Thread(target=do_connect, args=("192.168.1.2", "two"))
            t1.start()
            assert first_connect_started.wait(timeout=5)
            t2.start()
            assert second_connect_started.wait(timeout=5)

            release_connects.set()
            t1.join(timeout=10)
            t2.join(timeout=10)

            assert not t1.is_alive() and not t2.is_alive(), "connect hung"
            assert len(pool._sessions) <= 1, f"pool oversubscribed: {len(pool._sessions)} sessions"
            assert len(results) == 1, f"expected exactly one success, got {results}"
            assert len(errors) == 1, f"expected one rejection, got {errors}"
            assert "exhaust" in str(errors[0]).lower() or "capacity" in str(errors[0]).lower()

    @patch("arthas_mcp_proxy.ssh_pool.threading.Thread")
    def test_pool_exhaustion_is_structured(self, mock_thread: MagicMock) -> None:
        with patch("arthas_mcp_proxy.ssh_pool.MAX_SESSIONS", 0):
            pool = SSHConnectionPool(idle_timeout=300)
            with pytest.raises(DomainError) as excinfo:
                pool.connect("host", username="root", password="pw")
        assert excinfo.value.code is ErrorCode.SSH_POOL_EXHAUSTED

    @patch("arthas_mcp_proxy.ssh_pool.threading.Thread")
    def test_broken_transport_has_structured_boundary(self, mock_thread: MagicMock) -> None:
        client = _active_client()
        with patch("arthas_mcp_proxy.ssh_pool.paramiko.SSHClient", return_value=client):
            pool = SSHConnectionPool(idle_timeout=300)
            session_id = pool.connect("host", username="root", password="pw")
            client.get_transport.return_value.is_active.return_value = False
            with pytest.raises(DomainError) as excinfo:
                pool.get_session_or_raise(session_id)
        assert excinfo.value.code is ErrorCode.SSH_TRANSPORT_LOST

    @patch("arthas_mcp_proxy.ssh_pool.paramiko.SSHClient")
    @patch("arthas_mcp_proxy.ssh_pool.threading.Thread")
    def test_get_session_not_found(self, mock_thread: MagicMock, mock_sshclient: MagicMock) -> None:
        """get_session should return None for unknown session_id."""
        pool = SSHConnectionPool(idle_timeout=300)
        result = pool.get_session("nonexistent")
        assert result is None

    @patch("arthas_mcp_proxy.ssh_pool.paramiko.SSHClient")
    @patch("arthas_mcp_proxy.ssh_pool.threading.Thread")
    def test_disconnect_removes_session(
        self, mock_thread: MagicMock, mock_sshclient: MagicMock
    ) -> None:
        """disconnect should remove the session and return True."""
        mock_transport = MagicMock()
        mock_transport.is_active.return_value = True
        mock_client = MagicMock()
        mock_client.get_transport.return_value = mock_transport
        mock_sshclient.return_value = mock_client

        pool = SSHConnectionPool(idle_timeout=300)
        sid = pool.connect("192.168.1.1", username="root", password="test")
        assert pool.disconnect(sid) is True
        assert pool.get_session(sid) is None

    @patch("arthas_mcp_proxy.ssh_pool.paramiko.SSHClient")
    @patch("arthas_mcp_proxy.ssh_pool.threading.Thread")
    def test_disconnect_unknown_session(
        self, mock_thread: MagicMock, mock_sshclient: MagicMock
    ) -> None:
        """disconnect should return False for unknown session_id."""
        pool = SSHConnectionPool(idle_timeout=300)
        assert pool.disconnect("unknown") is False

    def test_session_is_idle(self) -> None:
        """Session should report idle after timeout."""
        mock_client = MagicMock()
        session = SSHSession(
            session_id="test",
            host="h",
            port=22,
            username="u",
            client=mock_client,
        )
        session.last_used = time.time() - 400  # 400s ago
        assert session.is_idle() is True

        session.touch()
        assert session.is_idle() is False

    def test_connect_requires_auth(self) -> None:
        """connect should raise ValueError when no auth method is given."""
        pool = SSHConnectionPool(idle_timeout=300)
        with pytest.raises(ValueError, match="password, key_path, or key_string"):
            pool.connect("192.168.1.1", username="root")

    @patch("arthas_mcp_proxy.ssh_pool.threading.Thread")
    def test_inactive_transport_is_reaped_and_same_key_reconnects(
        self, mock_thread: MagicMock
    ) -> None:
        """A dead transport is removed, and a later connect creates a fresh session."""
        clients: list[MagicMock] = []

        def make_client() -> MagicMock:
            client = MagicMock()
            transport = MagicMock()
            transport.is_active.return_value = len(clients) > 0
            client.get_transport.return_value = transport
            clients.append(client)
            return client

        with patch("arthas_mcp_proxy.ssh_pool.paramiko.SSHClient", side_effect=make_client):
            pool = SSHConnectionPool(idle_timeout=300)
            first = pool.connect("host", username="root", password="pw")
            assert pool.get_session(first) is None
            second = pool.connect("host", username="root", password="pw")

        assert second != first
        assert len(pool._sessions) == 1
        assert clients[0].close.called

    @patch("arthas_mcp_proxy.ssh_pool.threading.Thread")
    def test_connect_lock_map_does_not_grow_without_bound(self, mock_thread: MagicMock) -> None:
        """Per-key serialization locks are retired after a connect completes."""
        with patch(
            "arthas_mcp_proxy.ssh_pool.paramiko.SSHClient",
            side_effect=_active_client,
        ):
            pool = SSHConnectionPool(idle_timeout=300)
            for _index in range(100):
                pool.connect("host", username="root", password="pw")

        assert pool._connect_locks == {}

    @patch("arthas_mcp_proxy.ssh_pool.threading.Thread")
    def test_disconnect_defers_close_until_lease_released(self, mock_thread: MagicMock) -> None:
        """Disconnect must not close a client while a caller still leases it."""
        client = _active_client()
        with patch("arthas_mcp_proxy.ssh_pool.paramiko.SSHClient", return_value=client):
            pool = SSHConnectionPool(idle_timeout=300)
            session_id = pool.connect("host", username="root", password="pw")
            with pool.lease(session_id) as session:
                assert pool.disconnect(session_id) is True
                client.close.assert_not_called()
                assert session.client is client
            client.close.assert_called_once()
            assert pool.disconnect(session_id) is False
