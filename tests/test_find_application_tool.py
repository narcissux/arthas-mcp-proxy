"""Contract tests for the find_java_application MCP tool (B1).

The tool resolves a Java application name to its process details by listing
Java processes over the existing SSH session and matching against the
application resolver. This contract pins its registration, input schema, and
mocked resolution behavior.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.process_inventory import ProcessRecord
from arthas_mcp_proxy.server import find_java_application, mcp


@pytest.mark.contract
@pytest.mark.asyncio
async def test_find_java_application_is_registered() -> None:
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    assert "find_java_application" in names


@pytest.mark.contract
@pytest.mark.asyncio
async def test_find_java_application_schema_requires_session_and_application() -> None:
    tools = await mcp.list_tools()
    by_name = {tool.name: tool for tool in tools}
    tool = by_name["find_java_application"]

    assert tool.description.strip(), "find_java_application must have a description"
    schema = tool.inputSchema
    assert schema.get("type") == "object"
    assert "session_id" in schema.get("required", [])
    assert "application_name" in schema.get("required", [])
    assert schema["properties"]["session_id"]["type"] == "string"
    assert schema["properties"]["application_name"]["type"] == "string"


@pytest.mark.contract
def test_find_java_application_returns_json_candidate() -> None:
    """Resolves a matching inventory record to a JSON candidate."""
    session = MagicMock()
    pool = MagicMock()
    pool.get_session.return_value = session
    record = ProcessRecord(pid=5678, command="com.example.OrderService")

    with (
        patch("arthas_mcp_proxy.server.get_connection_pool", return_value=pool),
        patch("arthas_mcp_proxy.server.collect_inventory_over_ssh", return_value=[record]),
    ):
        result = find_java_application("sess-1", "OrderService")

    payload = json.loads(result)
    assert payload["pid"] == 5678
    assert payload["command"] == "com.example.OrderService"
    assert payload["owner"] is None
    assert payload["start_time"] is None
    assert payload["identity_key"] == [5678, None]


@pytest.mark.contract
def test_find_java_application_passes_start_time_to_arthas_state() -> None:
    """Resolver metadata is carried into session state for later Arthas ops."""
    session = MagicMock()
    pool = MagicMock()
    pool.get_session.return_value = session
    record = ProcessRecord(
        pid=5678,
        command="com.example.OrderService",
        owner="appuser",
        start_time="2026-08-01T10:00:00",
        boot_id=None,
    )

    with (
        patch("arthas_mcp_proxy.server.get_connection_pool", return_value=pool),
        patch("arthas_mcp_proxy.server.collect_inventory_over_ssh", return_value=[record]),
    ):
        result = find_java_application("sess-1", "OrderService")

    payload = json.loads(result)
    assert payload["start_time"] == "2026-08-01T10:00:00"
    assert session.start_time == "2026-08-01T10:00:00"
    assert payload["boot_id"] is None
    assert payload["identity_complete"] is False
    assert "unknown-start" not in payload["handle"]


@pytest.mark.contract
def test_find_java_application_returns_stable_jvm_handle() -> None:
    """A resolved target exposes a handle suitable for subsequent MCP calls."""
    session = MagicMock(host="example.test", port=2222, username="deploy")
    pool = MagicMock()
    pool.get_session.return_value = session
    record = ProcessRecord(
        pid=5678,
        command="com.example.OrderService",
        owner="appuser",
        start_time="2026-08-01T10:00:00",
        boot_id=None,
    )

    with (
        patch("arthas_mcp_proxy.server.get_connection_pool", return_value=pool),
        patch("arthas_mcp_proxy.server.collect_inventory_over_ssh", return_value=[record]),
    ):
        result = find_java_application("sess-1", "OrderService")

    payload = json.loads(result)
    assert payload["handle"] == "jvm:example.test:2222:deploy:5678:2026-08-01T10:00:00"


@pytest.mark.contract
def test_find_java_application_maps_not_found_to_error() -> None:
    """A DomainError from the resolver must surface as an Error: result."""
    session = MagicMock()
    pool = MagicMock()
    pool.get_session.return_value = session
    record = ProcessRecord(pid=1234, command="com.example.Other")

    with (
        patch("arthas_mcp_proxy.server.get_connection_pool", return_value=pool),
        patch("arthas_mcp_proxy.server.collect_inventory_over_ssh", return_value=[record]),
    ):
        result = find_java_application("sess-1", "Missing")

    payload = json.loads(result)
    assert payload["isError"] is True
    assert payload["structuredContent"]["error"]["code"] == "JVM_NOT_FOUND"
    assert "Missing" in payload["structuredContent"]["summary"]


@pytest.mark.contract
def test_find_java_application_requires_session() -> None:
    """A missing/expired session must return an Error: result."""
    pool = MagicMock()
    pool.get_session.return_value = None
    pool.get_session_by_host.return_value = None

    with patch("arthas_mcp_proxy.server.get_connection_pool", return_value=pool):
        result = find_java_application("no-such-session", "OrderService")

    payload = json.loads(result)
    assert payload["isError"] is True
    assert payload["structuredContent"]["error"]["code"] == "SESSION_NOT_FOUND"
