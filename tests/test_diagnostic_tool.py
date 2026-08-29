"""Contract tests for the execute_diagnostic_command MCP tool (A6a).

The tool renders a catalog-backed Arthas diagnostic command string without
executing it. This contract pins its registration and input schema.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.server import execute_diagnostic_command, mcp


@pytest.mark.contract
@pytest.mark.asyncio
async def test_execute_diagnostic_command_is_registered() -> None:
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    assert "execute_diagnostic_command" in names


@pytest.mark.contract
@pytest.mark.asyncio
async def test_execute_diagnostic_command_schema_requires_command() -> None:
    tools = await mcp.list_tools()
    by_name = {tool.name: tool for tool in tools}
    tool = by_name["execute_diagnostic_command"]

    assert tool.description.strip(), "execute_diagnostic_command must have a description"
    schema = tool.inputSchema
    assert schema.get("type") == "object"
    assert "command" in schema.get("required", [])
    assert schema["properties"]["command"]["type"] == "string"


@pytest.mark.contract
def test_execute_diagnostic_command_rejects_unknown_command() -> None:
    session = MagicMock()
    from arthas_mcp_proxy.ssh_pool import get_connection_pool

    pool = get_connection_pool()
    with patch.object(pool, "get_session", return_value=session):
        result = execute_diagnostic_command(session_id="session", pid=1, command="unknown")
    assert "unknown" in result.lower() or result.startswith("Error:")


@pytest.mark.contract
def test_exec_command_missing_session_returns_structured_error() -> None:
    from arthas_mcp_proxy.server import exec_command

    with patch("arthas_mcp_proxy.server.get_connection_pool") as pool:
        pool.return_value.get_session.return_value = None
        pool.return_value.get_session_by_host.return_value = None
        payload = json.loads(exec_command(session_id="missing", pid=1, command="jvm"))

    assert payload["isError"] is True
    assert payload["structuredContent"]["error"]["code"] == "SESSION_NOT_FOUND"


@pytest.mark.contract
def test_exec_command_timeout_returns_structured_error() -> None:
    from arthas_mcp_proxy.server import exec_command

    session = MagicMock()
    client = MagicMock()
    client.exec_command.side_effect = TimeoutError("backend timeout")
    from arthas_mcp_proxy.ssh_pool import get_connection_pool

    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        payload = json.loads(exec_command(session_id="session", pid=1, command="jvm"))

    assert payload["isError"] is True
    assert payload["structuredContent"]["error"]["code"] == "COMMAND_TIMEOUT"


@pytest.mark.contract
def test_exec_command_rejects_unsafe_command() -> None:
    from arthas_mcp_proxy.server import exec_command
    from arthas_mcp_proxy.ssh_pool import get_connection_pool

    session = MagicMock()
    client = MagicMock()
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        payload = json.loads(exec_command(session_id="session", pid=1, command="stop"))

    assert payload["structuredContent"]["error"]["code"] == "COMMAND_NOT_ALLOWED"
    client.exec_command.assert_not_called()


@pytest.mark.contract
@pytest.mark.parametrize("command", ["stop", "profiler stop", "jvm; stop", "jvm\nstop"])
def test_exec_command_rejects_lifecycle_and_command_injection(command: str) -> None:
    from arthas_mcp_proxy.server import exec_command
    from arthas_mcp_proxy.ssh_pool import get_connection_pool

    session = MagicMock()
    client = MagicMock()
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        payload = json.loads(exec_command(session_id="session", pid=1, command=command))

    assert payload["structuredContent"]["error"]["code"] == "COMMAND_NOT_ALLOWED"
    client.exec_command.assert_not_called()


@pytest.mark.contract
@pytest.mark.parametrize("command", ["jvm", "thread -n 5", "sysprop java.version"])
def test_exec_command_allows_exact_read_only_commands(command: str) -> None:
    from arthas_mcp_proxy.server import exec_command
    from arthas_mcp_proxy.ssh_pool import get_connection_pool

    session = MagicMock()
    client = MagicMock()
    client.exec_command.return_value = "read-only output"
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        assert exec_command(session_id="session", pid=1, command=command) == "read-only output"


@pytest.mark.contract
def test_exec_command_success_remains_plain_text() -> None:
    from arthas_mcp_proxy.server import exec_command

    session = MagicMock()
    client = MagicMock()
    client.exec_command.return_value = "jvm output"
    from arthas_mcp_proxy.ssh_pool import get_connection_pool

    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        assert exec_command(session_id="session", pid=1, command="jvm") == "jvm output"


@pytest.mark.contract
def test_install_arthas_missing_session_returns_structured_error() -> None:
    from arthas_mcp_proxy.server import install_arthas

    with patch("arthas_mcp_proxy.server.get_connection_pool") as pool:
        pool.return_value.get_session.return_value = None
        pool.return_value.get_session_by_host.return_value = None
        payload = json.loads(install_arthas(session_id="missing"))

    assert payload["structuredContent"]["error"]["code"] == "SESSION_NOT_FOUND"


@pytest.mark.contract
def test_install_arthas_success_remains_plain_text() -> None:
    from arthas_mcp_proxy.server import install_arthas
    from arthas_mcp_proxy.ssh_pool import get_connection_pool

    session = MagicMock()
    client = MagicMock()
    client.install_arthas.return_value = "Arthas already installed"
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        assert install_arthas(session_id="session") == "Arthas already installed"
