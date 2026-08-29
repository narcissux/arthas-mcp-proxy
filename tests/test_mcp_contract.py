import pytest

from arthas_mcp_proxy.server import mcp


@pytest.mark.contract
@pytest.mark.asyncio
async def test_tool_names():
    tools = await mcp.list_tools()
    names = sorted(tool.name for tool in tools)
    assert names == sorted(
        [
            "connect_ssh",
            "list_java_processes",
            "find_java_application",
            "thread_dump",
            "heap_info",
            "watch_method",
            "trace_method",
            "exec_command",
            "install_arthas",
            "prepare_arthas",
            "disconnect_ssh",
            "execute_diagnostic_command",
            "get_diagnostic_job",
            "cancel_diagnostic_job",
            "start_diagnostic_job",
            "list_diagnostic_jobs",
        ]
    )


@pytest.mark.contract
@pytest.mark.asyncio
async def test_tools_have_description_and_object_schema():
    tools = await mcp.list_tools()
    by_name = {tool.name: tool for tool in tools}
    for name in [
        "connect_ssh",
        "list_java_processes",
        "thread_dump",
        "heap_info",
        "watch_method",
        "trace_method",
        "exec_command",
        "install_arthas",
        "disconnect_ssh",
    ]:
        tool = by_name[name]
        assert tool.description.strip(), f"{name} must have a non-empty description"
        assert tool.inputSchema.get("type") == "object", (
            f"{name} must declare input schema type 'object', got {tool.inputSchema!r}"
        )
