"""Contract tests for the public error model (arthas_mcp_proxy.models).

The MCP tools currently communicate failures as opaque ``"Error: ..."``
strings.  This contract establishes a structured, machine-readable shape
for tool results so diagnostics can carry a stable error code and a
retryability hint.  A3a: model contract only - no integration yet.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from arthas_mcp_proxy.models import ErrorCode, ErrorDetail, ResultMeta, ToolResult

# ── ErrorCode ────────────────────────────────────────────────────────────────


@pytest.mark.contract
def test_error_code_defines_minimal_members() -> None:
    assert ErrorCode.INVALID_ARGUMENT.value == "INVALID_ARGUMENT"
    assert ErrorCode.SESSION_NOT_FOUND.value == "SESSION_NOT_FOUND"
    assert ErrorCode.INTERNAL_ERROR.value == "INTERNAL_ERROR"


@pytest.mark.contract
def test_error_code_is_str_enum_and_json_serializable() -> None:
    assert isinstance(ErrorCode.INVALID_ARGUMENT, str)
    assert json.dumps(ErrorCode.INVALID_ARGUMENT) == '"INVALID_ARGUMENT"'


# ── ErrorDetail ──────────────────────────────────────────────────────────────


@pytest.mark.contract
def test_error_detail_exposes_contract_fields() -> None:
    detail = ErrorDetail(
        code=ErrorCode.SESSION_NOT_FOUND,
        message="session s-1 not found",
        phase="resolve",
        retryable=True,
        suggestion="reconnect then retry",
    )
    assert detail.code is ErrorCode.SESSION_NOT_FOUND
    assert detail.message == "session s-1 not found"
    assert detail.phase == "resolve"
    assert detail.retryable is True
    assert detail.suggestion == "reconnect then retry"


@pytest.mark.contract
def test_error_detail_defaults() -> None:
    detail = ErrorDetail(code=ErrorCode.INTERNAL_ERROR, message="boom")
    assert detail.phase is None
    assert detail.retryable is False
    assert detail.suggestion is None


@pytest.mark.contract
def test_error_detail_serializes_code_as_plain_string() -> None:
    detail = ErrorDetail(code=ErrorCode.INVALID_ARGUMENT, message="bad")
    assert detail.model_dump()["code"] == "INVALID_ARGUMENT"


# ── ResultMeta ───────────────────────────────────────────────────────────────


@pytest.mark.contract
def test_result_meta_exposes_contract_fields() -> None:
    meta = ResultMeta(
        request_id="req-1",
        duration_ms=12,
        backend="ssh",
        truncated=True,
        original_chars=1000,
        returned_chars=500,
        next_cursor="abc",
    )
    assert meta.request_id == "req-1"
    assert meta.duration_ms == 12
    assert meta.backend == "ssh"
    assert meta.truncated is True
    assert meta.original_chars == 1000
    assert meta.returned_chars == 500
    assert meta.next_cursor == "abc"


@pytest.mark.contract
def test_result_meta_defaults() -> None:
    meta = ResultMeta(request_id="req-1", duration_ms=0)
    assert meta.backend is None
    assert meta.truncated is False
    assert meta.original_chars is None
    assert meta.returned_chars is None
    assert meta.next_cursor is None
    assert meta.degraded is False


@pytest.mark.contract
def test_result_meta_backend_is_literal() -> None:
    meta = ResultMeta(request_id="req-1", duration_ms=1, backend="arthas_ws")
    assert meta.backend == "arthas_ws"
    with pytest.raises(ValidationError):
        ResultMeta(request_id="req-1", duration_ms=1, backend="http")  # type: ignore[arg-type]


# ── ToolResult ───────────────────────────────────────────────────────────────


@pytest.mark.contract
def test_tool_result_success() -> None:
    result = ToolResult(
        status="success",
        summary="OK",
        data={"pid": 1234},
        meta=ResultMeta(request_id="req-1", duration_ms=5),
    )
    assert result.status == "success"
    assert result.summary == "OK"
    assert result.data == {"pid": 1234}
    assert result.error is None
    assert result.meta.request_id == "req-1"


@pytest.mark.contract
def test_tool_result_accepts_list_data() -> None:
    result = ToolResult(
        status="success",
        summary="OK",
        data=[1, 2, 3],
        meta=ResultMeta(request_id="req-1", duration_ms=1),
    )
    assert result.data == [1, 2, 3]


@pytest.mark.contract
def test_tool_result_status_is_literal() -> None:
    with pytest.raises(ValidationError):
        ToolResult(
            status="failed",  # type: ignore[arg-type]
            summary="x",
            meta=ResultMeta(request_id="req-1", duration_ms=1),
        )


@pytest.mark.contract
def test_tool_result_error_status_requires_error_detail() -> None:
    meta = ResultMeta(request_id="req-1", duration_ms=1)
    with pytest.raises(ValidationError):
        ToolResult(status="error", summary="failed", meta=meta)

    result = ToolResult(
        status="error",
        summary="failed",
        error=ErrorDetail(code=ErrorCode.SESSION_NOT_FOUND, message="gone"),
        meta=meta,
    )
    assert result.error is not None
    assert result.error.code is ErrorCode.SESSION_NOT_FOUND
