"""B1-3: find_java_application carries live inventory identity."""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.jvm_registry import reset_jvm_registry
from arthas_mcp_proxy.process_inventory import ProcessRecord
from arthas_mcp_proxy.server import find_java_application
from arthas_mcp_proxy.target_state import TargetIdentity

OPAQUE_HANDLE_RE = re.compile(r"^jvm_[0-9a-f]{16,}$")

BOOT_ID = "2f4c1b6a-9d3e-4a10-8c2b-77e0d1a2b3c4"


def _session() -> MagicMock:
    s = MagicMock()
    s.host, s.port, s.username = "10.0.0.8", 22, "ops"
    return s


def _data(result: str) -> dict:
    payload = json.loads(result)
    return payload["structuredContent"]["data"]


def _first_candidate(result: str) -> dict:
    return _data(result)["candidates"][0]


@pytest.mark.contract
def test_b1_3_a_find_carries_inventory_start_time_and_boot_id() -> None:
    """B1-3-a: mock inventory with start_time+boot_id appears in find JSON."""
    session = _session()
    pool = MagicMock()
    pool.get_session.return_value = session
    record = ProcessRecord(
        pid=4242,
        command="java -jar OrderService.jar",
        owner="appuser",
        start_time="17000",
        boot_id=BOOT_ID,
    )
    reset_jvm_registry()
    with (
        patch("arthas_mcp_proxy.server.get_connection_pool", return_value=pool),
        patch(
            "arthas_mcp_proxy.server.collect_inventory_over_ssh",
            return_value=[record],
        ),
    ):
        result = find_java_application("sess-1", "OrderService.jar")

    data = _data(result)
    payload = _first_candidate(result)
    assert data["status"] == "matched"
    assert payload["pid"] == 4242
    assert payload["start_time"] == "17000"
    assert payload["boot_id"] == BOOT_ID
    assert payload.get("identity_complete") is True
    assert payload["identity_key"] != [4242, None]
    handle = data.get("handle") or payload.get("handle")
    assert handle
    assert "unknown-start" not in handle
    reversible = TargetIdentity("10.0.0.8", 22, "ops", 4242, "17000").handle
    assert OPAQUE_HANDLE_RE.fullmatch(handle)
    assert handle != reversible
    assert not handle.startswith("jvm:")


@pytest.mark.contract
def test_b1_3_c_jps_only_marks_identity_incomplete() -> None:
    """B1-3-c: jps-only inventory → null identity + identity_complete=false."""
    session = _session()
    pool = MagicMock()
    pool.get_session.return_value = session
    record = ProcessRecord(
        pid=4242,
        command="com.example.OrderService",
        owner=None,
        start_time=None,
        boot_id=None,
    )
    reset_jvm_registry()
    with (
        patch("arthas_mcp_proxy.server.get_connection_pool", return_value=pool),
        patch(
            "arthas_mcp_proxy.server.collect_inventory_over_ssh",
            return_value=[record],
        ),
    ):
        result = find_java_application("sess-1", "OrderService")

    payload = _first_candidate(result)
    assert payload["pid"] == 4242
    assert payload["start_time"] is None
    assert payload["boot_id"] is None
    assert payload["identity_complete"] is False
