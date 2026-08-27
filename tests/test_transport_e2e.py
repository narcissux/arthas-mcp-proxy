"""Real MCP protocol checks for every supported transport."""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import pytest
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from arthas_mcp_proxy.server import build_parser


async def _wait_for_port(port: int) -> None:
    for _ in range(100):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.05)
        else:
            writer.close()
            await writer.wait_closed()
            return
    raise AssertionError(f"server did not listen on port {port}")


@asynccontextmanager
async def _running_http_server(transport: str) -> AsyncIterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "arthas_mcp_proxy",
        "--transport",
        transport,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await _wait_for_port(port)
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        await process.wait()


async def _assert_mcp_lifecycle(session: ClientSession) -> None:
    initialized = await session.initialize()
    assert initialized.serverInfo.name == "arthas-mcp-proxy"
    tools = await session.list_tools()
    names = {tool.name for tool in tools.tools}
    assert "get_diagnostic_job" in names
    result = await session.call_tool("get_diagnostic_job", {"job_id": "missing-e2e-job"})
    assert result.content


@pytest.mark.asyncio
@pytest.mark.e2e
@pytest.mark.contract
async def test_streamable_http_real_initialize_list_and_call() -> None:
    async with (
        _running_http_server("streamable-http") as base_url,
        streamablehttp_client(f"{base_url}/mcp", headers={"host": "localhost"}) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await _assert_mcp_lifecycle(session)


@pytest.mark.asyncio
@pytest.mark.e2e
@pytest.mark.contract
async def test_sse_real_initialize_list_and_call() -> None:
    async with (
        _running_http_server("sse") as base_url,
        sse_client(f"{base_url}/sse", headers={"host": "localhost"}) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await _assert_mcp_lifecycle(session)


@pytest.mark.asyncio
@pytest.mark.e2e
@pytest.mark.contract
async def test_stdio_real_initialize_list_and_call() -> None:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "arthas_mcp_proxy", "--transport", "stdio"],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    async with stdio_client(server) as streams, ClientSession(streams[0], streams[1]) as session:
        await _assert_mcp_lifecycle(session)


def test_transport_contract_still_lists_all_real_transports() -> None:
    parser = build_parser()
    assert {
        parser.parse_args(["--transport", value]).transport
        for value in ("stdio", "sse", "streamable-http")
    } == {
        "stdio",
        "sse",
        "streamable-http",
    }
