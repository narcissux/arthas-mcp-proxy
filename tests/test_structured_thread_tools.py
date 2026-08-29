import json
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.server import heap_info, thread_dump


@pytest.mark.unit
def test_thread_dump_missing_session_returns_structured_error() -> None:
    with patch("arthas_mcp_proxy.server.get_connection_pool") as pool:
        pool.return_value.get_session.return_value = None
        pool.return_value.get_session_by_host.return_value = None
        result = json.loads(thread_dump(session_id="missing", pid=1))

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "SESSION_NOT_FOUND"


@pytest.mark.unit
def test_heap_info_missing_session_returns_structured_error() -> None:
    with patch("arthas_mcp_proxy.server.get_connection_pool") as pool:
        pool.return_value.get_session.return_value = None
        pool.return_value.get_session_by_host.return_value = None
        result = json.loads(heap_info(session_id="missing", pid=1))

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "SESSION_NOT_FOUND"


@pytest.mark.unit
def test_thread_dump_success_remains_plain_text() -> None:
    session = MagicMock()
    client = MagicMock()
    client.thread_dump.return_value = "thread output"
    from arthas_mcp_proxy.ssh_pool import get_connection_pool

    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        raw = thread_dump(session_id="session", pid=1)
        payload = json.loads(raw)
        assert payload["isError"] is False
        assert payload["structuredContent"]["data"]["output"] == "thread output"
