"""Contract tests for the MCP result adapter (arthas_mcp_proxy.result_adapter).

``to_mcp_result`` converts a :class:`~arthas_mcp_proxy.models.ToolResult`
into the MCP tool-result shape (``isError`` + ``structuredContent``) so
diagnostics can read a structured status or error code.
"""

from __future__ import annotations

import pytest

from arthas_mcp_proxy.models import ErrorCode, ErrorDetail, ResultMeta, ToolResult
from arthas_mcp_proxy.result_adapter import to_mcp_result


@pytest.mark.contract
def test_success_result_conversion() -> None:
    result = ToolResult(
        status="success",
        summary="OK",
        data={"pid": 1234},
        meta=ResultMeta(request_id="req-1", duration_ms=5),
    )
    converted = to_mcp_result(result)
    assert converted["isError"] is False
    assert converted["structuredContent"]["status"] == "success"


@pytest.mark.contract
def test_error_result_conversion() -> None:
    result = ToolResult(
        status="error",
        summary="session s-1 not found",
        error=ErrorDetail(code=ErrorCode.SESSION_NOT_FOUND, message="session s-1 not found"),
        meta=ResultMeta(request_id="req-1", duration_ms=3),
    )
    converted = to_mcp_result(result)
    assert converted["isError"] is True
    assert converted["structuredContent"]["error"]["code"] == "SESSION_NOT_FOUND"
