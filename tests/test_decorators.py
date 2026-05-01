"""Tests for the @require_session decorator."""

from __future__ import annotations

from unittest.mock import MagicMock

from arthas_mcp_proxy.decorators import (
    require_session,
    set_fallback_credential_getter,
)
from arthas_mcp_proxy.ssh_pool import SSHConnectionPool


class TestRequireSession:
    """Unit tests for the require_session decorator."""

    def setup_method(self):
        """Clear fallback getter before each test."""
        set_fallback_credential_getter(None)

    def test_returns_error_when_session_not_found(self):
        """Decorator should return error string when session_id cannot be resolved."""
        pool = MagicMock(spec=SSHConnectionPool)
        pool.get_session.return_value = None

        @require_session(pool_getter=lambda: pool)
        def my_tool(session, pid: int = 1) -> str:
            return "success"

        result = my_tool(session_id="invalid_id")
        assert "Session not found" in result
        assert "Please reconnect" in result

    def test_injects_session_when_found(self):
        """Decorator should inject SSHSession and remove session_id from kwargs."""
        pool = MagicMock(spec=SSHConnectionPool)
        mock_session = MagicMock()
        mock_session.session_id = "abc123"
        pool.get_session.return_value = mock_session

        captured = {}

        @require_session(pool_getter=lambda: pool)
        def my_tool(session, pid: int = 1) -> str:
            captured["session"] = session
            return f"success with {session.session_id}"

        result = my_tool(session_id="abc123", pid=42)
        assert result == "success with abc123"
        assert captured["session"] is mock_session

    def test_fallback_to_host_lookup(self):
        """Decorator should try host-based fallback when session not found by ID."""
        pool = MagicMock(spec=SSHConnectionPool)
        pool.get_session.return_value = None

        mock_session = MagicMock()
        mock_session.session_id = "fallback_session"
        pool.get_session_by_host.return_value = mock_session

        # Register fallback getter
        set_fallback_credential_getter(
            lambda sid: {"host": "192.168.1.1", "port": 22, "username": "root"}
        )

        @require_session(pool_getter=lambda: pool)
        def my_tool(session, pid: int = 1) -> str:
            return f"ok: {session.session_id}"

        result = my_tool(session_id="expired_id")
        assert "fallback_session" in result

    def test_no_fallback_when_disabled(self):
        """When fallback=False, decorator should not try host-based lookup."""
        pool = MagicMock(spec=SSHConnectionPool)
        pool.get_session.return_value = None

        @require_session(pool_getter=lambda: pool, fallback=False)
        def my_tool(session, pid: int = 1) -> str:
            return "success"

        result = my_tool(session_id="missing")
        assert "Session not found" in result
        pool.get_session_by_host.assert_not_called()

    def test_pool_getter_called_each_time(self):
        """Pool getter should be called fresh on each invocation."""
        call_count = 0

        def counting_getter():
            nonlocal call_count
            call_count += 1
            pool = MagicMock(spec=SSHConnectionPool)
            pool.get_session.return_value = None
            return pool

        @require_session(pool_getter=counting_getter)
        def my_tool(session, pid: int = 1) -> str:
            return "success"

        my_tool(session_id="id1")
        my_tool(session_id="id2")
        assert call_count == 2

    def test_missing_session_id_param(self):
        """Decorator should handle missing session_id gracefully."""
        pool = MagicMock(spec=SSHConnectionPool)

        @require_session(pool_getter=lambda: pool)
        def my_tool(session, pid: int = 1) -> str:
            return "success"

        # Call without session_id at all
        result = my_tool(pid=42)
        assert "session_id is required" in result


class TestFallbackCredentialGetter:
    """Tests for the fallback credential getter registration."""

    def test_set_and_retrieve(self):
        """Setting and retrieving the fallback getter should work."""
        # Clear first
        set_fallback_credential_getter(None)
        import arthas_mcp_proxy.decorators as dec_mod

        assert dec_mod._fallback_credential_getter is None

        def getter(sid):
            return {"host": "test"}

        set_fallback_credential_getter(getter)
        assert dec_mod._fallback_credential_getter is getter

        # Cleanup
        set_fallback_credential_getter(None)
