from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import threading

from .result_adapter import to_mcp_result
from .typed_executor import execute_typed_command


def typed_command_json(
    client: Any,
    *,
    pid: int,
    command: str,
    params: dict[str, Any],
    timeout: int = 60,
    cancel: threading.Event | None = None,
) -> str:
    """Execute a catalog command and serialize its structured MCP result."""
    return json.dumps(
        to_mcp_result(
            execute_typed_command(
                client,
                pid=pid,
                command=command,
                params=params,
                timeout=timeout,
                cancel=cancel,
            )
        ),
        default=str,
    )
