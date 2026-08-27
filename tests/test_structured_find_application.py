import json
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.server import find_java_application


@pytest.mark.unit
def test_find_application_missing_session_returns_structured_error() -> None:
    pool = MagicMock()
    pool.get_session.return_value = None
    pool.get_session_by_host.return_value = None

    with patch("arthas_mcp_proxy.server.get_connection_pool", return_value=pool):
        result = json.loads(find_java_application("missing", "OrderService"))

    assert result["isError"] is True
    assert result["structuredContent"]["status"] == "error"
    assert result["structuredContent"]["error"]["code"] == "SESSION_NOT_FOUND"
