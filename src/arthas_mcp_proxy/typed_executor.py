from __future__ import annotations

import time
import uuid
from typing import Any, Literal, cast

from .command_catalog import build_command
from .models import ResultMeta, ToolResult

BackendName = Literal["ssh", "arthas_cli", "arthas_http", "arthas_ws"]


def _identity_complete_from_client(client: Any) -> bool | None:
    flag = getattr(client, "last_identity_complete", None)
    return flag if isinstance(flag, bool) else None


def execute_typed_command(
    client: Any,
    *,
    pid: int,
    command: str,
    params: dict[str, Any],
    timeout: int = 60,
) -> ToolResult:
    started = time.perf_counter()
    request_id = f"req-{uuid.uuid4().hex}"
    try:
        rendered = build_command(command, params)
        output = client.execute_command(pid=pid, command=rendered, timeout=timeout)
        backend = cast("str", getattr(client, "last_backend", "arthas_cli"))
        if backend not in {"ssh", "arthas_cli", "arthas_http", "arthas_ws"}:
            backend = "arthas_cli"
        backend_name = cast("BackendName", backend)
        return ToolResult(
            status="success",
            summary=f"Executed {command}",
            data={"command": command, "rendered_command": rendered, "output": output},
            meta=ResultMeta(
                request_id=request_id,
                duration_ms=int((time.perf_counter() - started) * 1000),
                backend=backend_name,
                degraded=bool(getattr(client, "last_backend_degraded", False)),
                identity_complete=_identity_complete_from_client(client),
            ),
        )
    except ValueError as exc:
        from .models import ErrorCode, ErrorDetail

        return ToolResult(
            status="error",
            summary=str(exc),
            error=ErrorDetail(code=ErrorCode.INVALID_ARGUMENT, message=str(exc)),
            meta=ResultMeta(
                request_id=request_id,
                duration_ms=int((time.perf_counter() - started) * 1000),
            ),
        )
    except Exception as exc:
        from .models import ErrorCode, ErrorDetail

        return ToolResult(
            status="error",
            summary=f"Diagnostic command failed: {exc}",
            error=ErrorDetail(code=ErrorCode.ARTHAS_COMMAND_FAILED, message=str(exc)),
            meta=ResultMeta(
                request_id=request_id,
                duration_ms=int((time.perf_counter() - started) * 1000),
                backend="arthas_cli",
                identity_complete=_identity_complete_from_client(client),
            ),
        )
