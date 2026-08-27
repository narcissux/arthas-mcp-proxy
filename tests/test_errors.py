"""Contract tests for the error mapping layer (arthas_mcp_proxy.errors).

Establishes the public contract for ``DomainError`` and the
exception -> ``ErrorDetail`` mapping used across the MCP tools, so tool
failures carry a stable ``ErrorCode`` plus a retryability/phase hint
instead of opaque ``"Error: ..."`` strings.  A3a: errors module contract
only - no integration yet.
"""

from __future__ import annotations

import pytest

from arthas_mcp_proxy.errors import (
    DomainError,
    SSHPoolExhaustedError,
    SSHTransportLostError,
    map_exception,
    to_error_detail,
)
from arthas_mcp_proxy.models import ErrorCode, ErrorDetail

# ── Exception mapping ─────────────────────────────────────────────────────────


@pytest.mark.contract
def test_value_error_maps_to_invalid_argument() -> None:
    error = map_exception(ValueError("bad argument"))
    assert error.code is ErrorCode.INVALID_ARGUMENT


@pytest.mark.contract
def test_timeout_error_maps_to_command_timeout() -> None:
    error = map_exception(TimeoutError("command timed out"))
    assert error.code is ErrorCode.COMMAND_TIMEOUT


@pytest.mark.contract
def test_ssh_pool_boundary_errors_have_stable_codes() -> None:
    assert map_exception(SSHPoolExhaustedError("pool full")).code is ErrorCode.SSH_POOL_EXHAUSTED
    assert (
        map_exception(SSHTransportLostError("transport closed")).code
        is ErrorCode.SSH_TRANSPORT_LOST
    )


# ── DomainError -> ErrorDetail ────────────────────────────────────────────────


@pytest.mark.contract
def test_domain_error_preserves_fields_through_to_error_detail() -> None:
    error = DomainError(
        code=ErrorCode.SSH_COMMAND_TIMEOUT,
        message="timed out after 30s",
        retryable=True,
        phase="execute",
        suggestion="raise the timeout or retry the command",
    )
    detail = to_error_detail(error)
    assert isinstance(detail, ErrorDetail)
    assert detail.code is ErrorCode.SSH_COMMAND_TIMEOUT
    assert detail.message == "timed out after 30s"
    assert detail.retryable is True
    assert detail.phase == "execute"
    assert detail.suggestion == "raise the timeout or retry the command"
