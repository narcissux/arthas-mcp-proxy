"""Contract tests for the health/readiness payload (arthas_mcp_proxy.health).

``health_payload`` returns a pure dict describing process liveness and
readiness so the transport layer can serve it at a health endpoint without
coupling this contract to any particular HTTP framework.
"""

from __future__ import annotations

import pytest

from arthas_mcp_proxy.health import health_payload


@pytest.mark.contract
def test_health_payload_reports_status_ok() -> None:
    payload = health_payload()
    assert payload["status"] == "ok"


@pytest.mark.contract
def test_health_payload_ready_is_boolean() -> None:
    payload = health_payload()
    assert isinstance(payload["ready"], bool)


@pytest.mark.contract
def test_health_payload_ready_is_true() -> None:
    payload = health_payload()
    assert payload["ready"] is True
