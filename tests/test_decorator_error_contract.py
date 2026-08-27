"""Contract tests for @require_session error behavior.

Locks in the legacy behaviour: a decorated tool with an unresolvable
``session_id`` keeps returning the existing ``"Error: ..."`` string, so
current tools and clients are unaffected.  Alongside that, a focused mapper
helper (:func:`session_not_found_error_detail`) exposes the same condition as
a structured :class:`ErrorDetail` with ``ErrorCode.SESSION_NOT_FOUND`` for
tools that opt into structured errors later.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from arthas_mcp_proxy.decorators import (
    SESSION_NOT_FOUND_MESSAGE,
    require_session,
    session_not_found_error_detail,
)
from arthas_mcp_proxy.models import ErrorCode, ErrorDetail
from arthas_mcp_proxy.ssh_pool import SSHConnectionPool

LEGACY_MESSAGE = "Error: Session not found or expired. Please reconnect using connect_ssh."


class TestRequireSessionStringContract:
    """The decorated wrapper must keep returning the legacy string."""

    @pytest.mark.contract
    def test_missing_session_returns_existing_string(self) -> None:
        """A decorated tool must return the exact legacy string, unchanged."""
        pool = MagicMock(spec=SSHConnectionPool)
        pool.get_session.return_value = None

        @require_session(pool_getter=lambda: pool)
        def my_tool(session, pid: int = 1) -> str:
            return "success"

        result = my_tool(session_id="nope")
        assert result == LEGACY_MESSAGE

    @pytest.mark.contract
    def test_legacy_message_is_centralized(self) -> None:
        """The message constant must match the legacy string exactly."""
        assert SESSION_NOT_FOUND_MESSAGE == LEGACY_MESSAGE

    @pytest.mark.contract
    def test_default_behavior_still_returns_str(self) -> None:
        """The mapper must be purely additive: tools still get a str by default."""
        pool = MagicMock(spec=SSHConnectionPool)
        pool.get_session.return_value = None

        @require_session(pool_getter=lambda: pool)
        def my_tool(session) -> str:
            return "success"

        result = my_tool(session_id="nope")
        assert isinstance(result, str)
        assert not isinstance(result, ErrorDetail)


class TestSessionNotFoundMapper:
    """The mapper helper maps missing session -> ErrorCode.SESSION_NOT_FOUND."""

    @pytest.mark.contract
    def test_maps_missing_session_to_session_not_found(self) -> None:
        detail = session_not_found_error_detail()
        assert isinstance(detail, ErrorDetail)
        assert detail.code is ErrorCode.SESSION_NOT_FOUND

    @pytest.mark.contract
    def test_mapper_message_matches_legacy_string(self) -> None:
        """Structured and string paths must carry the same message."""
        detail = session_not_found_error_detail()
        assert detail.message == LEGACY_MESSAGE

    @pytest.mark.contract
    def test_mapper_exposes_structured_metadata(self) -> None:
        detail = session_not_found_error_detail()
        assert detail.phase == "resolve"
        assert detail.retryable is True
        assert detail.suggestion is not None

    @pytest.mark.contract
    def test_mapper_does_not_alter_decorator_behavior(self) -> None:
        """Calling the helper must not change what @require_session returns."""
        pool = MagicMock(spec=SSHConnectionPool)
        pool.get_session.return_value = None

        @require_session(pool_getter=lambda: pool)
        def my_tool(session) -> str:
            return "success"

        session_not_found_error_detail()
        assert my_tool(session_id="nope") == LEGACY_MESSAGE
