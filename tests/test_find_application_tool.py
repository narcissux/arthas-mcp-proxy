"""Contract tests for the find_java_application MCP tool (B1 / B2-2).

The tool resolves a Java application name to its process details by listing
Java processes over the existing SSH session and matching against the
application resolver. This contract pins its registration, input schema, and
mocked resolution behavior. Success-shaped results use the B2-2 envelope
(status + candidates).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.process_inventory import ProcessRecord
from arthas_mcp_proxy.server import find_java_application, mcp


def _data(result: str) -> dict:
    payload = json.loads(result)
    return payload["structuredContent"]["data"]


def _first_candidate(result: str) -> dict:
    return _data(result)["candidates"][0]


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

    data = _data(result)
    assert data["status"] == "matched"
    payload = _first_candidate(result)
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

    payload = _first_candidate(result)
    assert payload["start_time"] == "2026-08-01T10:00:00"
    assert session.start_time == "2026-08-01T10:00:00"
    assert payload["boot_id"] is None
    assert payload["identity_complete"] is False
    handle = _data(result).get("handle") or payload.get("handle")
    assert handle
    assert "unknown-start" not in handle


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

    payload = _first_candidate(result)
    handle = _data(result).get("handle") or payload.get("handle")
    assert handle == "jvm:example.test:2222:deploy:5678:2026-08-01T10:00:00"


@pytest.mark.contract
def test_find_java_application_not_found_is_success_envelope() -> None:
    """Zero matches with other Java processes is not_found, not JVM_NOT_FOUND error."""
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
    assert payload["isError"] is False
    data = payload["structuredContent"]["data"]
    assert data["status"] == "not_found"
    assert data["candidates"]
    error = payload["structuredContent"].get("error")
    assert error is None or error.get("code") != "JVM_NOT_FOUND"


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
