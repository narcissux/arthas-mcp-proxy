"""B4-1: diagnostic tools accept jvm_handle."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.jvm_registry import JvmRegistry, get_jvm_registry, reset_jvm_registry
from arthas_mcp_proxy.process_inventory import ProcessRecord
from arthas_mcp_proxy.server import (
    execute_diagnostic_command,
    heap_info,
    mcp,
    start_diagnostic_job,
    thread_dump,
    trace_method,
    watch_method,
)
from arthas_mcp_proxy.ssh_pool import get_connection_pool

HANDLE_TOOLS = (
    "thread_dump",
    "heap_info",
    "watch_method",
    "trace_method",
    "execute_diagnostic_command",
    "start_diagnostic_job",
)

TARGET_KEY = "ops@10.0.0.8:22"
PID = 4242
START_TIME = "17000"
BOOT_ID = "boot-old"
APP_NAME = "inventory-service.jar"


class _FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _mint(
    registry: JvmRegistry | None = None,
    **overrides: object,
) -> str:
    kwargs: dict[str, object] = {
        "target_key": TARGET_KEY,
        "pid": PID,
        "start_time": START_TIME,
        "boot_id": BOOT_ID,
        "application_name": APP_NAME,
    }
    kwargs.update(overrides)
    return (registry or get_jvm_registry()).mint(**kwargs)  # type: ignore[arg-type]


def _session(*, start_time: str | None = None, boot_id: str | None = None) -> MagicMock:
    session = MagicMock()
    session.session_id = "sess-1"
    session.host = "10.0.0.8"
    session.port = 22
    session.username = "ops"
    session.start_time = start_time
    session.boot_id = boot_id
    return session


def _error_code(result: str) -> str:
    payload = json.loads(result)
    assert payload["isError"] is True
    return str(payload["structuredContent"]["error"]["code"])


def _success_output(result: str) -> str:
    payload = json.loads(result)
    assert payload["isError"] is False
    return str(payload["structuredContent"]["data"]["output"])


@pytest.mark.contract
@pytest.mark.asyncio
async def test_b4_1_a_six_tools_accept_optional_jvm_handle() -> None:
    """B4-1-a: six tools expose jvm_handle; session_id and pid are not required."""
    tools = await mcp.list_tools()
    by_name = {tool.name: tool for tool in tools}
    for name in HANDLE_TOOLS:
        tool = by_name[name]
        schema = tool.inputSchema
        assert schema.get("type") == "object"
        assert "jvm_handle" in schema.get("properties", {}), f"{name} must accept jvm_handle"
        required = schema.get("required") or []
        assert "session_id" not in required, f"{name} session_id must not be required"
        assert "pid" not in required, f"{name} pid must not be required"
        description = (tool.description or "").lower()
        assert "deprecated" in description, f"{name} must mark session_id/pid deprecated"
    exec_schema = by_name["exec_command"].inputSchema
    assert "jvm_handle" not in (exec_schema.get("properties") or {})


@pytest.mark.unit
def test_b4_1_b_handle_only_thread_dump_succeeds() -> None:
    """B4-1-b: handle-only thread_dump succeeds and uses the binding pid."""
    handle = _mint()
    session = _session()
    client = MagicMock()
    client.thread_dump.return_value = "thread output"
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=session) as by_host,
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        result = thread_dump(jvm_handle=handle)

    assert _success_output(result) == "thread output"
    client.thread_dump.assert_called_once()
    assert client.thread_dump.call_args.kwargs["pid"] == PID
    by_host.assert_called_once_with("10.0.0.8", 22, "ops")
    assert session.start_time == START_TIME
    assert session.boot_id == BOOT_ID


@pytest.mark.unit
def test_b4_1_c_expired_handle_is_handle_expired() -> None:
    """B4-1-c: expired handle → HANDLE_EXPIRED; ArthasClient.thread_dump is not called."""
    clock = _FakeClock()
    registry = JvmRegistry(ttl_seconds=1_800, clock=clock)
    reset_jvm_registry(registry)
    handle = _mint(registry)
    clock.advance(1_800.0)
    client = MagicMock()
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=_session()),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        result = thread_dump(jvm_handle=handle)

    assert _error_code(result) == "HANDLE_EXPIRED"
    client.thread_dump.assert_not_called()


@pytest.mark.unit
def test_b4_1_d_session_id_and_pid_thread_dump_still_succeeds() -> None:
    """B4-1-d: existing thread_dump(session_id=..., pid=...) still succeeds."""
    session = _session()
    client = MagicMock()
    client.thread_dump.return_value = "thread output"
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        assert _success_output(thread_dump(session_id="session", pid=1)) == "thread output"
    client.thread_dump.assert_called_once_with(pid=1, top_n=20)


@pytest.mark.unit
def test_b4_1_e_handle_and_disagreeing_pid_is_invalid_argument() -> None:
    """B4-1-e: handle pid 4242 + caller pid 9999 → INVALID_ARGUMENT; command not run."""
    handle = _mint()
    client = MagicMock()
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=_session()),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        result = thread_dump(jvm_handle=handle, pid=9999)

    assert _error_code(result) == "INVALID_ARGUMENT"
    client.thread_dump.assert_not_called()


@pytest.mark.unit
def test_b4_1_f_identity_changed_does_not_run_command() -> None:
    """B4-1-f: live start_time/boot_id change → JVM_IDENTITY_CHANGED; no attach."""
    handle = _mint()
    session = _session()
    current = ProcessRecord(
        pid=PID,
        command="inventory-service.jar",
        start_time="20000",
        boot_id=BOOT_ID,
    )
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=session),
        patch("arthas_mcp_proxy.arthas_client._get_sudo_user", return_value=None),
        patch("arthas_mcp_proxy.arthas_client._find_arthas_path", return_value="/tmp/as.sh"),  # noqa: S108
        patch(
            "arthas_mcp_proxy.arthas_client.collect_inventory_over_ssh",
            return_value=[current],
        ),
        patch("arthas_mcp_proxy.arthas_client._ensure_agent") as ensure,
    ):
        result = thread_dump(jvm_handle=handle)

    assert _error_code(result) == "JVM_IDENTITY_CHANGED"
    ensure.assert_not_called()


@pytest.mark.unit
def test_b4_1_g_missing_pid_is_jvm_exited() -> None:
    """B4-1-g: pid gone → JVM_EXITED; command not run."""
    handle = _mint()
    session = _session()
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=session),
        patch("arthas_mcp_proxy.arthas_client._get_sudo_user", return_value=None),
        patch("arthas_mcp_proxy.arthas_client._find_arthas_path", return_value="/tmp/as.sh"),  # noqa: S108
        patch("arthas_mcp_proxy.arthas_client.collect_inventory_over_ssh", return_value=[]),
        patch("arthas_mcp_proxy.arthas_client._ensure_agent") as ensure,
    ):
        result = thread_dump(jvm_handle=handle)

    assert _error_code(result) == "JVM_EXITED"
    ensure.assert_not_called()


@pytest.mark.unit
def test_b4_1_unknown_handle_is_handle_not_found() -> None:
    """Unknown handle → HANDLE_NOT_FOUND; command not run."""
    client = MagicMock()
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=_session()),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        result = thread_dump(jvm_handle="jvm_deadbeefdeadbeef")

    assert _error_code(result) == "HANDLE_NOT_FOUND"
    client.thread_dump.assert_not_called()


@pytest.mark.unit
def test_b4_1_neither_handle_nor_session_is_invalid_argument() -> None:
    """Neither jvm_handle nor session_id+pid → INVALID_ARGUMENT."""
    client = MagicMock()
    with patch("arthas_mcp_proxy.server.ArthasClient", return_value=client):
        result = thread_dump(top_n=5)

    assert _error_code(result) == "INVALID_ARGUMENT"
    client.thread_dump.assert_not_called()


@pytest.mark.unit
def test_b4_1_handle_plus_agreeing_pid_succeeds() -> None:
    """Handle plus the same pid still succeeds."""
    handle = _mint()
    session = _session()
    client = MagicMock()
    client.thread_dump.return_value = "thread output"
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        result = thread_dump(jvm_handle=handle, pid=PID)
    assert _success_output(result) == "thread output"
    client.thread_dump.assert_called_once()
    assert client.thread_dump.call_args.kwargs["pid"] == PID


@pytest.mark.unit
def test_b4_1_handle_plus_wrong_session_is_mismatch() -> None:
    """Handle from target A plus session B → HANDLE_SESSION_MISMATCH."""
    handle = _mint()
    other = _session()
    other.host = "10.0.0.9"
    other.username = "other"
    client = MagicMock()
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session", return_value=other),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        result = thread_dump(jvm_handle=handle, session_id="other-session")
    assert _error_code(result) == "HANDLE_SESSION_MISMATCH"
    client.thread_dump.assert_not_called()


@pytest.mark.unit
def test_b4_1_c_execute_expired_handle_is_structured() -> None:
    """B4-1-c on execute_diagnostic_command: expired handle is structured HANDLE_EXPIRED."""
    clock = _FakeClock()
    registry = JvmRegistry(ttl_seconds=1_800, clock=clock)
    reset_jvm_registry(registry)
    handle = _mint(registry)
    clock.advance(1_800.0)
    client = MagicMock()
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=_session()),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server.typed_command_json") as typed,
    ):
        result = execute_diagnostic_command(jvm_handle=handle, command="thread_dump")
    assert _error_code(result) == "HANDLE_EXPIRED"
    typed.assert_not_called()
    client.thread_dump.assert_not_called()


@pytest.mark.unit
def test_b4_1_handle_only_heap_info_smoke() -> None:
    handle = _mint()
    client = MagicMock()
    client.heap_info.return_value = "heap output"
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=_session()),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        assert _success_output(heap_info(jvm_handle=handle)) == "heap output"
    client.heap_info.assert_called_once()
    assert client.heap_info.call_args.kwargs["pid"] == PID


@pytest.mark.unit
def test_b4_1_handle_only_watch_method_smoke() -> None:
    handle = _mint()
    client = MagicMock()
    client.execute_streaming_command.return_value = "watch output"
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=_session()),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        result = watch_method(jvm_handle=handle, class_pattern="Foo", method_pattern="bar")
        assert _success_output(result) == "watch output"
    assert client.execute_streaming_command.call_args.args[0] == PID
    client.watch_method.assert_not_called()


@pytest.mark.unit
def test_b4_1_handle_only_trace_method_smoke() -> None:
    handle = _mint()
    client = MagicMock()
    client.execute_streaming_command.return_value = "trace output"
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=_session()),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        result = trace_method(jvm_handle=handle, class_pattern="Foo", method_pattern="bar")
        assert _success_output(result) == "trace output"
    client.trace_method.assert_not_called()


@pytest.mark.unit
def test_b4_1_handle_only_execute_smoke() -> None:
    handle = _mint()
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=_session()),
        patch("arthas_mcp_proxy.server.typed_command_json", return_value="typed-ok") as typed,
    ):
        assert execute_diagnostic_command(jvm_handle=handle, command="thread_dump") == "typed-ok"
    assert typed.call_args.kwargs["pid"] == PID


@pytest.mark.unit
def test_b4_1_handle_only_start_job_smoke() -> None:
    handle = _mint()
    session = _session()
    client = MagicMock()
    client.execute_command.return_value = "job output"
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=session),
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        payload = json.loads(start_diagnostic_job("thread_dump", {"top_n": 5}, jvm_handle=handle))
        import time

        current = payload
        for _ in range(50):
            from arthas_mcp_proxy.server import get_diagnostic_job

            current = json.loads(get_diagnostic_job(payload["job_id"]))
            if current["status"] != "RUNNING":
                break
            time.sleep(0.01)
    assert current["status"] == "SUCCEEDED"
    assert "job output" in current["output"]
    assert current.get("jvm_handle") == handle
    assert payload.get("jvm_handle") == handle
