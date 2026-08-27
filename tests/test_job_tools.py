import pytest

from arthas_mcp_proxy.server import mcp


@pytest.mark.contract
@pytest.mark.asyncio
async def test_job_tools_are_registered_with_job_id_schema() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    for name in ("get_diagnostic_job", "cancel_diagnostic_job"):
        assert name in tools
        assert "job_id" in tools[name].inputSchema.get("required", [])
