"""Tests for ssh_pool.py - connection pool lifecycle and thread safety."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import paramiko
import pytest

from arthas_mcp_proxy.ssh_pool import (
    SSHConnectionPool,
    SSHSession,
    _safe_close_client,
)


class TestSafeCloseClient:
    """Tests for the _safe_close_client helper."""

    def test_safe_close_none(self):
        """Should not raise when client is None."""
        _safe_close_client(None)  # no exception

    def test_safe_close_with_transport(self):
        """Should close transport then client."""
        client = MagicMock(spec=paramiko.SSHClient)
        transport = MagicMock()
        client.get_transport.return_value = transport

        _safe_close_client(client)
        transport.close.assert_called_once()
        client.close.assert_called_once()

    def test_safe_close_no_transport(self):
        """Should handle when get_transport returns None."""
        client = MagicMock(spec=paramiko.SSHClient)
        client.get_transport.return_value = None

        _safe_close_client(client)
        client.close.assert_called_once()

    def test_safe_close_transport_raises(self):
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
    def test_connect_new_session(self, mock_thread, mock_sshclient):
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

    @patch("arthas_mcp_proxy.ssh_pool.paramiko.SSHClient")
    @patch("arthas_mcp_proxy.ssh_pool.threading.Thread")
    def test_connect_reuses_existing(self, mock_thread, mock_sshclient):
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

    @patch("arthas_mcp_proxy.ssh_pool.paramiko.SSHClient")
    @patch("arthas_mcp_proxy.ssh_pool.threading.Thread")
    def test_get_session_not_found(self, mock_thread, mock_sshclient):
        """get_session should return None for unknown session_id."""
        pool = SSHConnectionPool(idle_timeout=300)
        result = pool.get_session("nonexistent")
        assert result is None

    @patch("arthas_mcp_proxy.ssh_pool.paramiko.SSHClient")
    @patch("arthas_mcp_proxy.ssh_pool.threading.Thread")
    def test_disconnect_removes_session(self, mock_thread, mock_sshclient):
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
    def test_disconnect_unknown_session(self, mock_thread, mock_sshclient):
        """disconnect should return False for unknown session_id."""
        pool = SSHConnectionPool(idle_timeout=300)
        assert pool.disconnect("unknown") is False

    def test_session_is_idle(self):
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

    def test_connect_requires_auth(self):
        """connect should raise ValueError when no auth method is given."""
        pool = SSHConnectionPool(idle_timeout=300)
        with pytest.raises(ValueError, match="password, key_path, or key_string"):
            pool.connect("192.168.1.1", username="root")
