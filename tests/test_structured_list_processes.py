import json
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.errors import DomainError
from arthas_mcp_proxy.models import ErrorCode
from arthas_mcp_proxy.process_inventory import ProcessRecord
from arthas_mcp_proxy.server import list_java_processes
from arthas_mcp_proxy.target_state import TargetIdentity

BOOT_ID = "2f4c1b6a-9d3e-4a10-8c2b-77e0d1a2b3c4"


def _session(*, host: str = "10.0.0.8", port: int = 22, username: str = "ops") -> MagicMock:
    session = MagicMock()
    session.host = host
    session.port = port
    session.username = username
    return session


def _pool_with_session(session: MagicMock) -> MagicMock:
    pool = MagicMock()
    pool.get_session.return_value = session
    return pool


def _full_record() -> ProcessRecord:
    return ProcessRecord(
        pid=4242,
        command="java -jar app.jar",
        owner="appuser",
        start_time="17000",
        boot_id=BOOT_ID,
    )


@pytest.mark.unit
def test_list_java_processes_missing_session_returns_structured_error() -> None:
    pool = MagicMock()
    pool.get_session.return_value = None
    pool.get_session_by_host.return_value = None

    with patch("arthas_mcp_proxy.server.get_connection_pool", return_value=pool):
        result = json.loads(list_java_processes("missing"))

    assert result["isError"] is True
    assert result["structuredContent"]["status"] == "error"
    assert result["structuredContent"]["error"]["code"] == "SESSION_NOT_FOUND"


@pytest.mark.unit
def test_list_java_processes_success_returns_structured_envelope() -> None:
    session = _session()
    with (
        patch(
            "arthas_mcp_proxy.server.get_connection_pool",
            return_value=_pool_with_session(session),
        ),
        patch(
            "arthas_mcp_proxy.server.collect_inventory_over_ssh",
            return_value=[_full_record()],
        ),
    ):
        result = json.loads(list_java_processes("session"))

    assert result["isError"] is False
    assert result["structuredContent"]["status"] == "success"
    assert isinstance(result["structuredContent"]["data"]["processes"], list)


@pytest.mark.unit
def test_list_java_processes_includes_identity_fields_and_handle() -> None:
    session = _session(host="10.0.0.8", port=22, username="ops")
    with (
        patch(
            "arthas_mcp_proxy.server.get_connection_pool",
            return_value=_pool_with_session(session),
        ),
        patch(
            "arthas_mcp_proxy.server.collect_inventory_over_ssh",
            return_value=[_full_record()],
        ),
    ):
        result = json.loads(list_java_processes("session"))

    process = result["structuredContent"]["data"]["processes"][0]
    assert process["pid"] == 4242
    assert process["command"] == "java -jar app.jar"
    assert process["owner"] == "appuser"
    assert process["start_time"] == "17000"
    assert process["boot_id"] == BOOT_ID
    expected_handle = TargetIdentity(
        host="10.0.0.8",
        port=22,
        username="ops",
        pid=4242,
        start_time="17000",
    ).handle
    assert expected_handle == "jvm:10.0.0.8:22:ops:4242:17000"
    assert process["handle"] == expected_handle


@pytest.mark.unit
def test_list_java_processes_missing_start_time_uses_unknown_start() -> None:
    session = _session()
    record = ProcessRecord(pid=4242, command="com.example.App")
    with (
        patch(
            "arthas_mcp_proxy.server.get_connection_pool",
            return_value=_pool_with_session(session),
        ),
        patch(
            "arthas_mcp_proxy.server.collect_inventory_over_ssh",
            return_value=[record],
        ),
    ):
        result = json.loads(list_java_processes("session"))

    process = result["structuredContent"]["data"]["processes"][0]
    assert process["owner"] is None
    assert process["start_time"] is None
    assert process["boot_id"] is None
    expected_handle = TargetIdentity(
        host="10.0.0.8",
        port=22,
        username="ops",
        pid=4242,
        start_time=None,
    ).handle
    assert expected_handle.endswith(":unknown-start")
    assert process["handle"] == expected_handle


@pytest.mark.unit
def test_list_java_processes_empty_inventory_is_success() -> None:
    session = _session()
    with (
        patch(
            "arthas_mcp_proxy.server.get_connection_pool",
            return_value=_pool_with_session(session),
        ),
        patch("arthas_mcp_proxy.server.collect_inventory_over_ssh", return_value=[]),
    ):
        result = json.loads(list_java_processes("session"))

    assert result["isError"] is False
    assert result["structuredContent"]["status"] == "success"
    assert result["structuredContent"]["data"]["processes"] == []
    error = result["structuredContent"].get("error")
    assert error is None or (isinstance(error, dict) and error.get("code") != "JVM_NOT_FOUND")


@pytest.mark.unit
def test_list_java_processes_collector_domain_error_is_structured() -> None:
    session = _session()
    with (
        patch(
            "arthas_mcp_proxy.server.get_connection_pool",
            return_value=_pool_with_session(session),
        ),
        patch(
            "arthas_mcp_proxy.server.collect_inventory_over_ssh",
            side_effect=DomainError(
                ErrorCode.JVM_NOT_FOUND,
                "Failed to list Java processes from /proc and jps",
            ),
        ),
    ):
        result = json.loads(list_java_processes("session"))

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "JVM_NOT_FOUND"
