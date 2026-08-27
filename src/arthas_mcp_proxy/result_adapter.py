from typing import Any

from arthas_mcp_proxy.models import ToolResult


def to_mcp_result(result: ToolResult) -> dict[str, Any]:
    return {
        "isError": result.status == "error",
        "structuredContent": result.model_dump(mode="json"),
        "content": [{"type": "text", "text": result.summary}],
    }
