"""B2-2: three-state find_java_application MCP envelope."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.process_inventory import ProcessRecord
from arthas_mcp_proxy.server import find_java_application
from arthas_mcp_proxy.target_state import TargetIdentity

BOOT_ID = "2f4c1b6a-9d3e-4a10-8c2b-77e0d1a2b3c4"

_CANDIDATE_FIELDS = ("pid", "owner", "start_time", "boot_id", "command", "match_evidence")


def _session() -> MagicMock:
    session = MagicMock()
    session.host = "10.0.0.8"
    session.port = 22
    session.username = "ops"
    return session


def _pool(session: MagicMock | None) -> MagicMock:
    pool = MagicMock()
    pool.get_session.return_value = session
    pool.get_session_by_host.return_value = None
    return pool


def _call(records: list[ProcessRecord], application_name: str, *, session: MagicMock | None = None):
    sess = session if session is not None else _session()
    with (
        patch("arthas_mcp_proxy.server.get_connection_pool", return_value=_pool(sess)),
        patch("arthas_mcp_proxy.server.collect_inventory_over_ssh", return_value=records),
        patch("arthas_mcp_proxy.arthas_client._ensure_agent") as ensure,
        patch("arthas_mcp_proxy.arthas_client._attach_agent") as attach,
        patch("arthas_mcp_proxy.server.ArthasClient") as client_cls,
    ):
        raw = find_java_application("sess-1", application_name)
    return json.loads(raw), ensure, attach, client_cls, sess


def _data(payload: dict) -> dict:
    return payload["structuredContent"]["data"]


@pytest.mark.contract
def test_b2_2_a_unique_hit_is_matched_envelope() -> None:
    """B2-2-a: unique inventory-service.jar → matched, 1 candidate, handle, isError false."""
    record = ProcessRecord(
        pid=4242,
        command="java -jar /opt/apps/inventory-service.jar --server.port=8080",
        owner="appuser",
        start_time="17000",
        boot_id=BOOT_ID,
    )
    payload, ensure, attach, client_cls, session = _call([record], "inventory-service.jar")

    assert payload["isError"] is False
    assert payload["structuredContent"]["status"] == "success"
    data = _data(payload)
    assert data["status"] == "matched"
    assert len(data["candidates"]) == 1
    candidate = data["candidates"][0]
    for field in _CANDIDATE_FIELDS:
        assert field in candidate
    assert candidate["pid"] == 4242
    assert candidate["owner"] == "appuser"
    assert candidate["start_time"] == "17000"
    assert candidate["boot_id"] == BOOT_ID
    assert candidate["command"] == record.command
    assert candidate["match_evidence"] == "jar_basename"
    assert data["handle"] == TargetIdentity("10.0.0.8", 22, "ops", 4242, "17000").handle
    assert data["identity_complete"] is True
    assert candidate["identity_complete"] is True
    assert session.start_time == "17000"
    assert session.boot_id == BOOT_ID
    ensure.assert_not_called()
    attach.assert_not_called()
    client_cls.assert_not_called()


@pytest.mark.contract
def test_b2_2_b_zero_hits_with_other_java_is_not_found() -> None:
    """B2-2-b: no match, other Java exists → not_found, candidates brief list, isError=false."""
    others = [
        ProcessRecord(
            pid=1001,
            command="java -jar billing-service.jar",
            owner="appuser",
            start_time="111",
            boot_id=BOOT_ID,
        ),
        ProcessRecord(
            pid=1002,
            command="java com.foo.OtherApp",
            owner="appuser",
            start_time="222",
            boot_id=BOOT_ID,
        ),
    ]
    payload, ensure, attach, client_cls, _session_obj = _call(others, "inventory-service")

    assert payload["isError"] is False
    structured = payload["structuredContent"]
    assert structured["status"] == "success"
    error = structured.get("error")
    assert error is None or (
        isinstance(error, dict) and error.get("code") not in {"JVM_NOT_FOUND", "INTERNAL_ERROR"}
    )
    data = _data(payload)
    assert data["status"] == "not_found"
    assert data["candidates"], "not_found must list current Java processes"
    assert {c["pid"] for c in data["candidates"]} == {1001, 1002}
    for candidate in data["candidates"]:
        for field in _CANDIDATE_FIELDS:
            assert field in candidate
        assert candidate["match_evidence"] is None
    assert "handle" not in data
    ensure.assert_not_called()
    attach.assert_not_called()
    client_cls.assert_not_called()


@pytest.mark.contract
def test_b2_2_c_two_order_service_jars_is_ambiguous_without_attach() -> None:
    """B2-2-c: two order-service.jar → ambiguous, 2 candidates, isError=false, no attach."""
    records = [
        ProcessRecord(
            pid=2001,
            command="java -jar /opt/a/order-service.jar",
            owner="appuser",
            start_time="100",
            boot_id=BOOT_ID,
        ),
        ProcessRecord(
            pid=2002,
            command="java -jar /opt/b/order-service.jar",
            owner="appuser",
            start_time="200",
            boot_id=BOOT_ID,
        ),
    ]
    payload, ensure, attach, client_cls, _session_obj = _call(records, "order-service.jar")

    assert payload["isError"] is False
    structured = payload["structuredContent"]
    assert structured["status"] == "success"
    error = structured.get("error")
    assert error is None or (
        isinstance(error, dict) and error.get("code") not in {"JVM_AMBIGUOUS", "INTERNAL_ERROR"}
    )
    data = _data(payload)
    assert data["status"] == "ambiguous"
    assert len(data["candidates"]) == 2
    assert {c["pid"] for c in data["candidates"]} == {2001, 2002}
    for candidate in data["candidates"]:
        for field in _CANDIDATE_FIELDS:
            assert field in candidate
        assert candidate["match_evidence"] == "jar_basename"
    assert "handle" not in data
    ensure.assert_not_called()
    attach.assert_not_called()
    client_cls.assert_not_called()


@pytest.mark.contract
def test_b2_2_d_missing_session_is_error() -> None:
    """B2-2-d: no session → SESSION_NOT_FOUND, isError=true."""
    pool = _pool(None)
    with patch("arthas_mcp_proxy.server.get_connection_pool", return_value=pool):
        payload = json.loads(find_java_application("no-such-session", "OrderService"))

    assert payload["isError"] is True
    assert payload["structuredContent"]["status"] == "error"
    assert payload["structuredContent"]["error"]["code"] == "SESSION_NOT_FOUND"
