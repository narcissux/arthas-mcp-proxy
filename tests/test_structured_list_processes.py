import json
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.server import list_java_processes


@pytest.mark.unit
def test_list_java_processes_missing_session_returns_structured_error() -> None:
    pool = MagicMock()
    pool.get_session.return_value = None
    pool.get_session_by_host.return_value = None

    with patch("arthas_mcp_proxy.server.get_connection_pool", return_value=pool):
        result = json.loads(list_java_processes("missing"))

    assert result["isError"] is True
    assert result["structuredContent"]["status"] == "error"
    assert result["structuredContent"]["error"]["code"] == "SESSION_NOT_FOUND"


@pytest.mark.unit
def test_list_java_processes_success_remains_plain_text() -> None:
    pool = MagicMock()
    pool.get_session.return_value = MagicMock()
    client = MagicMock()
    client.list_java_processes.return_value = "PID 1: app.Main"

    with (
        patch("arthas_mcp_proxy.server.get_connection_pool", return_value=pool),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        assert list_java_processes("session") == "PID 1: app.Main"
