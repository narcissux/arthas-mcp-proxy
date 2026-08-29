"""B6: handle-level PID reuse through MCP find / prepare / thread_dump."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.arthas_client import _PID_STATE, _PID_STATE_LOCK, _state_key
from arthas_mcp_proxy.jvm_registry import get_jvm_registry
from arthas_mcp_proxy.server import find_java_application, prepare_arthas, thread_dump
from arthas_mcp_proxy.ssh_pool import get_connection_pool
from tests._b6_helpers import (
    AMBIGUOUS_APP,
    APP,
    ARTHAS_VERSION,
    BOOT_NEW,
    BOOT_OLD,
    DUMP,
    HOST_A,
    HOST_B,
    HTTP_PORT,
    PID,
    PORT,
    SESSION_ID,
    START_NEW,
    TELNET_PORT,
    USER,
    assert_opaque_handle,
    data_ok,
    error_code,
    patch_find,
    patch_identity_fail,
    patch_prepare_existing,
    patch_thread_dump_success,
    payload,
    record,
    session,
)


@pytest.mark.contract
def test_b6_a_old_handle_fails_after_same_pid_restart() -> None:
    """B6-a: find → prepare → dump → restart same pid → old handle identity error."""
    sess = session()
    live = record()
    with patch_find(sess, [live]) as (ensure, attach, client_cls):
        found = find_java_application(SESSION_ID, APP)
    body = payload(found)
    assert body["isError"] is False
    data = data_ok(found)
    assert data["status"] == "matched"
    handle = data["handle"]
    assert_opaque_handle(handle)
    ensure.assert_not_called()
    attach.assert_not_called()
    client_cls.assert_not_called()

    with patch_prepare_existing(sess, [live]):
        prepared = prepare_arthas(jvm_handle=handle)
    assert data_ok(prepared)["origin"] in {"existing", "started_by_proxy"}

    with patch_thread_dump_success(sess) as (client, by_host):
        dumped = thread_dump(jvm_handle=handle)
    dumped_body = payload(dumped)
    assert dumped_body["isError"] is False
    assert dumped_body["structuredContent"]["data"]["output"] == DUMP
    assert dumped_body["structuredContent"]["meta"]["backend"] == "arthas_http"
    client.thread_dump.assert_called_once()
    assert client.thread_dump.call_args.kwargs["pid"] == PID
    by_host.assert_called_with(HOST_A, PORT, USER)

    restarted = record(start_time=START_NEW, boot_id=BOOT_OLD)
    with patch_identity_fail(sess, [restarted]) as (ensure_fail, attach_fail):
        failed = thread_dump(jvm_handle=handle)
    assert error_code(failed) in {"JVM_IDENTITY_CHANGED", "JVM_EXITED"}
    ensure_fail.assert_not_called()
    attach_fail.assert_not_called()


@pytest.mark.contract
def test_b6_b_refind_mints_new_handle_that_diagnoses() -> None:
    """B6-b: find again after restart → new handle ≠ old; new dump succeeds."""
    sess = session()
    old = record()
    with patch_find(sess, [old]):
        first = find_java_application(SESSION_ID, APP)
    old_handle = data_ok(first)["handle"]
    assert_opaque_handle(old_handle)

    replacement = record(start_time=START_NEW, boot_id=BOOT_NEW)
    with patch_find(sess, [replacement]) as (ensure, attach, client_cls):
        second = find_java_application(SESSION_ID, APP)
    new_data = data_ok(second)
    assert new_data["status"] == "matched"
    new_handle = new_data["handle"]
    assert_opaque_handle(new_handle)
    assert new_handle != old_handle
    ensure.assert_not_called()
    attach.assert_not_called()
    client_cls.assert_not_called()

    with patch_prepare_existing(sess, [replacement]):
        prepared = prepare_arthas(jvm_handle=new_handle)
    assert data_ok(prepared)["origin"] in {"existing", "started_by_proxy"}

    with patch_thread_dump_success(sess) as (client, _by_host):
        dumped = thread_dump(jvm_handle=new_handle)
    dumped_body = payload(dumped)
    assert dumped_body["isError"] is False
    assert dumped_body["structuredContent"]["data"]["output"] == DUMP
    assert dumped_body["structuredContent"]["meta"]["backend"] == "arthas_http"
    assert client.thread_dump.call_args.kwargs["pid"] == PID

    with patch_identity_fail(sess, [replacement]) as (ensure_fail, attach_fail):
        stale = thread_dump(jvm_handle=old_handle)
    assert error_code(stale) in {"JVM_IDENTITY_CHANGED", "JVM_EXITED"}
    ensure_fail.assert_not_called()
    attach_fail.assert_not_called()


@pytest.mark.contract
def test_b6_c_same_pid_on_two_targets_cannot_cross_hit() -> None:
    """B6-c: two targets same pid → A's handle never hits B."""
    sess_a = session(host=HOST_A, session_id="sess-a")
    sess_b = session(host=HOST_B, session_id="sess-b")
    rec_a = record(boot_id="boot-a")
    rec_b = record(boot_id="boot-b")

    with patch_find(sess_a, [rec_a]):
        found_a = find_java_application("sess-a", APP)
    with patch_find(sess_b, [rec_b]):
        found_b = find_java_application("sess-b", APP)
    handle_a = data_ok(found_a)["handle"]
    handle_b = data_ok(found_b)["handle"]
    assert_opaque_handle(handle_a)
    assert_opaque_handle(handle_b)
    assert handle_a != handle_b

    def by_host(host: str, port: int, username: str) -> MagicMock | None:
        if (host, port, username) == (HOST_A, PORT, USER):
            return sess_a
        if (host, port, username) == (HOST_B, PORT, USER):
            return sess_b
        return None

    def get_session(session_id: str) -> MagicMock | None:
        return {"sess-a": sess_a, "sess-b": sess_b}.get(session_id)

    exec_hosts: list[str] = []
    attach_hosts: list[str] = []

    def spy_exec_ssh(
        sess: MagicMock,
        _command: str,
        timeout: int = 60,
        sudo_user: str | None = None,
    ) -> tuple[str, str, int]:
        del timeout, sudo_user
        exec_hosts.append(sess.host)
        if "curl" in _command and "/api" in _command:
            return (json.dumps({"output": DUMP}), "", 0)
        return (DUMP, "", 0)

    def fake_attach(
        sess: MagicMock,
        pid: int,
        _arthas_path: str,
        owner: str | None = None,
        start_time: str | None = None,
    ) -> int:
        attach_hosts.append(sess.host)
        key = _state_key(sess, pid, start_time)
        with _PID_STATE_LOCK:
            _PID_STATE[key] = {
                "port": TELNET_PORT,
                "http_port": HTTP_PORT,
                "owner": owner,
            }
        return TELNET_PORT

    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", side_effect=by_host),
        patch.object(pool, "get_session", side_effect=get_session),
        patch(
            "arthas_mcp_proxy.arthas_client.collect_inventory_over_ssh",
            return_value=[rec_a],
        ),
        patch("arthas_mcp_proxy.arthas_client._get_sudo_user", return_value=None),
        patch(
            "arthas_mcp_proxy.arthas_client._find_arthas_path",
            return_value="/tmp/as.sh",  # noqa: S108
        ),
        patch("arthas_mcp_proxy.arthas_client._detect_listen_ports", return_value=[]),
        patch(
            "arthas_mcp_proxy.arthas_client._find_arthas_client_jar",
            return_value="/tmp/arthas-client.jar",  # noqa: S108
        ),
        patch("arthas_mcp_proxy.arthas_client._get_java_home", return_value=""),
        patch("arthas_mcp_proxy.arthas_client._exec_ssh", side_effect=spy_exec_ssh),
        patch("arthas_mcp_proxy.arthas_client._attach_agent", side_effect=fake_attach),
    ):
        dumped = thread_dump(jvm_handle=handle_a)
        mismatch = thread_dump(jvm_handle=handle_a, session_id="sess-b")

    dumped_payload = json.loads(dumped)
    assert dumped_payload["isError"] is False
    dumped_data = dumped_payload["structuredContent"]["data"]
    assert dumped_data["output"] == DUMP
    assert dumped_payload["structuredContent"]["meta"]["backend"] == "arthas_http"
    assert dumped_payload["structuredContent"]["meta"]["degraded"] is False
    assert exec_hosts
    assert all(host == HOST_A for host in exec_hosts)
    assert HOST_B not in exec_hosts
    assert attach_hosts
    assert all(host == HOST_A for host in attach_hosts)
    assert HOST_B not in attach_hosts
    assert error_code(mismatch) == "HANDLE_SESSION_MISMATCH"

    attach_hosts.clear()
    with (
        patch.object(pool, "get_session_by_host", side_effect=by_host),
        patch(
            "arthas_mcp_proxy.arthas_client.collect_inventory_over_ssh",
            return_value=[rec_a],
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
        ),
    ):
        prepared = prepare_arthas(jvm_handle=handle_a)

    assert data_ok(prepared)["origin"] == "started_by_proxy"
    attach.assert_called()
    assert attach.call_args.args[0].host == HOST_A
    assert all(host == HOST_A for host in attach_hosts)
    assert HOST_B not in attach_hosts


@pytest.mark.contract
def test_b6_d_ambiguous_same_name_does_not_mint_or_attach() -> None:
    """B6-d: two order-service.jar → ambiguous, no handle, no attach."""
    sess = session()
    twins = [
        record(pid=2001, command="java -jar /opt/a/order-service.jar", start_time="100"),
        record(pid=2002, command="java -jar /opt/b/order-service.jar", start_time="200"),
    ]
    registry = get_jvm_registry()
    with (
        patch.object(registry, "mint", wraps=registry.mint) as mint,
        patch_find(sess, twins) as (ensure, attach, client_cls),
    ):
        found = find_java_application(SESSION_ID, AMBIGUOUS_APP)
    body = payload(found)
    assert body["isError"] is False
    data = data_ok(found)
    assert data["status"] == "ambiguous"
    assert "handle" not in data
    mint.assert_not_called()
    ensure.assert_not_called()
    attach.assert_not_called()
    client_cls.assert_not_called()
