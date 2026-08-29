"""B5-1: prepare_arthas(jvm_handle) contract tests."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.arthas_client import (
    _LIFECYCLE_REGISTRY,
    _PID_STATE,
    _PID_STATE_LOCK,
    _state_key,
)
from arthas_mcp_proxy.errors import DomainError
from arthas_mcp_proxy.jvm_registry import get_jvm_registry
from arthas_mcp_proxy.models import ErrorCode
from arthas_mcp_proxy.process_inventory import ProcessRecord
from arthas_mcp_proxy.server import mcp, prepare_arthas
from arthas_mcp_proxy.ssh_pool import get_connection_pool
from arthas_mcp_proxy.target_state import TargetIdentity

TARGET_KEY = "ops@10.0.0.8:22"
PID = 4242
START_TIME = "17000"
BOOT_ID = "boot-old"
APP_NAME = "inventory-service.jar"
TELNET_PORT = 3658
HTTP_PORT_NON_PLUS_ONE = 4012
ARTHAS_VERSION = "3.7.2"


def _mint() -> str:
    return get_jvm_registry().mint(
        target_key=TARGET_KEY,
        pid=PID,
        start_time=START_TIME,
        boot_id=BOOT_ID,
        application_name=APP_NAME,
    )


def _session() -> MagicMock:
    session = MagicMock()
    session.session_id = "sess-1"
    session.host = "10.0.0.8"
    session.port = 22
    session.username = "ops"
    session.start_time = None
    session.boot_id = None
    return session


def _matching_record(**overrides: object) -> ProcessRecord:
    kwargs: dict[str, object] = {
        "pid": PID,
        "command": "inventory-service.jar",
        "start_time": START_TIME,
        "boot_id": BOOT_ID,
    }
    kwargs.update(overrides)
    return ProcessRecord(**kwargs)  # type: ignore[arg-type]


def _payload(result: str) -> dict:
    return json.loads(result)


def _error_code(result: str) -> str:
    payload = _payload(result)
    assert payload["isError"] is True
    return str(payload["structuredContent"]["error"]["code"])


def _data(result: str) -> dict:
    payload = _payload(result)
    assert payload["isError"] is False
    return payload["structuredContent"]["data"]


def _identity(session: MagicMock) -> TargetIdentity:
    return TargetIdentity(session.host, session.port, session.username, PID, START_TIME)


@pytest.mark.contract
@pytest.mark.asyncio
async def test_b5_1_a_prepare_arthas_listed_with_required_jvm_handle() -> None:
    """B5-1-a: tools/list has prepare_arthas; required is jvm_handle only."""
    tools = await mcp.list_tools()
    by_name = {tool.name: tool for tool in tools}
    assert "prepare_arthas" in by_name
    tool = by_name["prepare_arthas"]
    assert (tool.description or "").strip()
    schema = tool.inputSchema
    assert schema.get("type") == "object"
    required = schema.get("required") or []
    assert "jvm_handle" in required
    assert required == ["jvm_handle"] or (
        "session_id" not in required and "pid" not in required
    )
    assert "session_id" not in required
    assert "pid" not in required


@pytest.mark.contract
def test_b5_1_b_existing_agent_reused_without_attach_or_stop() -> None:
    """B5-1-b: existing Agent → origin=existing; attach/stop not called."""
    handle = _mint()
    session = _session()
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=session),
        patch(
            "arthas_mcp_proxy.arthas_client.collect_inventory_over_ssh",
            return_value=[_matching_record()],
        ),
        patch("arthas_mcp_proxy.arthas_client._get_sudo_user", return_value=None),
        patch(
            "arthas_mcp_proxy.arthas_client._detect_listen_ports",
            return_value=[TELNET_PORT, 8563],
        ),
        patch(
            "arthas_mcp_proxy.arthas_client._probe_arthas_version",
            return_value=ARTHAS_VERSION,
        ),
        patch("arthas_mcp_proxy.arthas_client._attach_agent") as attach,
        patch("arthas_mcp_proxy.arthas_client.ArthasClient.detach") as detach,
    ):
        result = prepare_arthas(jvm_handle=handle)

    data = _data(result)
    assert data["origin"] == "existing"
    assert data["telnet_port"] == TELNET_PORT
    assert data["http_port"] == 8563
    assert data["arthas_version"] == ARTHAS_VERSION
    attach.assert_not_called()
    detach.assert_not_called()


@pytest.mark.contract
def test_b5_1_c_missing_agent_attaches_and_version_succeeds() -> None:
    """B5-1-c: no Agent → attach, version succeeds → origin=started_by_proxy."""
    handle = _mint()
    session = _session()
    pool = get_connection_pool()

    def fake_attach(sess, pid, arthas_path, owner=None, start_time=None):
        key = _state_key(sess, pid, start_time)
        with _PID_STATE_LOCK:
            _PID_STATE[key] = {"port": TELNET_PORT, "http_port": 3660, "owner": owner}
        return TELNET_PORT

    with (
        patch.object(pool, "get_session_by_host", return_value=session),
        patch(
            "arthas_mcp_proxy.arthas_client.collect_inventory_over_ssh",
            return_value=[_matching_record()],
        ),
        patch("arthas_mcp_proxy.arthas_client._get_sudo_user", return_value=None),
        patch(
            "arthas_mcp_proxy.arthas_client._find_arthas_path",
            return_value="/tmp/as.sh",  # noqa: S108
        ),
        patch("arthas_mcp_proxy.arthas_client._detect_listen_ports", return_value=[]),
        patch(
            "arthas_mcp_proxy.arthas_client._attach_agent",
            side_effect=fake_attach,
        ) as attach,
        patch(
            "arthas_mcp_proxy.arthas_client._probe_arthas_version",
            return_value=ARTHAS_VERSION,
        ) as probe,
        patch("arthas_mcp_proxy.arthas_client.ArthasClient.detach") as detach,
    ):
        result = prepare_arthas(jvm_handle=handle)

    data = _data(result)
    assert data["origin"] == "started_by_proxy"
    assert data["arthas_version"]
    assert str(data["arthas_version"]).strip()
    attach.assert_called_once()
    probe.assert_called_once()
    detach.assert_not_called()


@pytest.mark.contract
def test_b5_1_d_version_failure_is_unreachable_and_clears_half_ready() -> None:
    """B5-1-d: port listening but version fails → ARTHAS_UNREACHABLE, cache cleared."""
    handle = _mint()
    session = _session()
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=session),
        patch(
            "arthas_mcp_proxy.arthas_client.collect_inventory_over_ssh",
            return_value=[_matching_record()],
        ),
        patch("arthas_mcp_proxy.arthas_client._get_sudo_user", return_value=None),
        patch(
            "arthas_mcp_proxy.arthas_client._detect_listen_ports",
            return_value=[TELNET_PORT, 8563],
        ),
        patch(
            "arthas_mcp_proxy.arthas_client._probe_arthas_version",
            side_effect=DomainError(
                ErrorCode.ARTHAS_UNREACHABLE,
                "Arthas version probe failed",
                phase="verify",
            ),
        ),
        patch("arthas_mcp_proxy.arthas_client._attach_agent") as attach,
    ):
        result = prepare_arthas(jvm_handle=handle)

    assert _error_code(result) == "ARTHAS_UNREACHABLE"
    payload = _payload(result)
    assert payload["isError"] is True
    structured = payload["structuredContent"]
    assert structured["status"] == "error"
    data = structured.get("data")
    if isinstance(data, dict):
        assert data.get("origin") not in {"existing", "started_by_proxy"}
        assert data.get("ready") not in {True, "ready", "READY"}
    key = _state_key(session, PID, START_TIME)
    with _PID_STATE_LOCK:
        assert key not in _PID_STATE
        cached = _PID_STATE.get(key)
    assert cached is None
    instance = _LIFECYCLE_REGISTRY.get(_identity(session))
    assert instance is None
    attach.assert_not_called()


@pytest.mark.contract
def test_b5_1_e_identity_changed_does_not_attach() -> None:
    """B5-1-e: live start_time/boot_id change → JVM_IDENTITY_CHANGED; no attach."""
    handle = _mint()
    session = _session()
    current = _matching_record(start_time="20000")
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=session),
        patch("arthas_mcp_proxy.arthas_client._get_sudo_user", return_value=None),
        patch(
            "arthas_mcp_proxy.arthas_client._find_arthas_path",
            return_value="/tmp/as.sh",  # noqa: S108
        ),
        patch(
            "arthas_mcp_proxy.arthas_client.collect_inventory_over_ssh",
            return_value=[current],
        ),
        patch("arthas_mcp_proxy.arthas_client._attach_agent") as attach,
        patch("arthas_mcp_proxy.arthas_client._detect_listen_ports") as detect,
        patch("arthas_mcp_proxy.arthas_client._probe_arthas_version") as probe,
    ):
        result = prepare_arthas(jvm_handle=handle)

    assert _error_code(result) == "JVM_IDENTITY_CHANGED"
    attach.assert_not_called()
    detect.assert_not_called()
    probe.assert_not_called()


@pytest.mark.unit
def test_b5_1_k_existing_http_port_is_not_telnet_plus_one() -> None:
    """B5-1-k: existing telnet 3658 + http 4012; do not guess telnet+1=3659."""
    handle = _mint()
    session = _session()
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=session),
        patch(
            "arthas_mcp_proxy.arthas_client.collect_inventory_over_ssh",
            return_value=[_matching_record()],
        ),
        patch("arthas_mcp_proxy.arthas_client._get_sudo_user", return_value=None),
        patch(
            "arthas_mcp_proxy.arthas_client._detect_listen_ports",
            return_value=[TELNET_PORT, HTTP_PORT_NON_PLUS_ONE],
        ),
        patch(
            "arthas_mcp_proxy.arthas_client._probe_arthas_version",
            return_value=ARTHAS_VERSION,
        ),
        patch("arthas_mcp_proxy.arthas_client._attach_agent") as attach,
    ):
        result = prepare_arthas(jvm_handle=handle)

    data = _data(result)
    assert data["origin"] == "existing"
    assert data["telnet_port"] == TELNET_PORT
    assert data["http_port"] == HTTP_PORT_NON_PLUS_ONE
    assert data["http_port"] != TELNET_PORT + 1
    attach.assert_not_called()


@pytest.mark.integration
def test_b5_1_f_docker_prestarted_arthas_existing_not_stopped() -> None:
    """B5-1-f: Docker pre-started Arthas — specified-not-run (no docker)."""
    pytest.skip("specified-not-run: no docker on this machine")


@pytest.mark.integration
def test_b5_1_g_docker_no_arthas_started_by_proxy() -> None:
    """B5-1-g: Docker without Arthas — specified-not-run (no docker)."""
    pytest.skip("specified-not-run: no docker on this machine")


@pytest.mark.integration
def test_b5_1_h_docker_active_job_ttl_does_not_stop() -> None:
    """B5-1-h: active job when TTL elapses — specified-not-run (no docker)."""
    pytest.skip("specified-not-run: no docker on this machine")


@pytest.mark.integration
def test_b5_1_i_docker_authorized_cleanup_only_proxy_owned() -> None:
    """B5-1-i: authorized cleanup after idle — specified-not-run (no docker)."""
    pytest.skip("specified-not-run: no docker on this machine")


@pytest.mark.unit
def test_b5_1_l_attach_argv_includes_ports_and_target_ip() -> None:
    """B5-1-l: attach command includes --telnet-port, --http-port, --target-ip."""
    from arthas_mcp_proxy.arthas_client import _attach_agent

    session = MagicMock()
    captured: list[str] = []

    def fake_exec(_sess: object, cmd: str, timeout: int = 30, sudo_user: object = None):
        captured.append(cmd)
        return ("", "", 0)

    with (
        patch("arthas_mcp_proxy.arthas_client._find_free_port", side_effect=[3658, 3660]),
        patch("arthas_mcp_proxy.arthas_client._exec_ssh", side_effect=fake_exec),
        patch("arthas_mcp_proxy.arthas_client._detect_arthas_port", return_value=3658),
        patch("arthas_mcp_proxy.arthas_client.time.sleep"),
    ):
        port = _attach_agent(session, PID, "/tmp/as.sh")  # noqa: S108

    assert port == 3658
    assert captured
    cmd = captured[0]
    assert "--telnet-port 3658" in cmd
    assert "--http-port 3660" in cmd
    assert "--target-ip 127.0.0.1" in cmd
