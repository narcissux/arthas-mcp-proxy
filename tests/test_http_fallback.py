"""C2: honest HTTP → CLI fallback (only PRE-POST connect failures retry on CLI)."""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.arthas_client import (
    _PID_STATE,
    _PID_STATE_LOCK,
    ArthasClient,
    _exec_command,
    _state_key,
)
from arthas_mcp_proxy.arthas_http import ArthasHttpClient, ArthasHttpError, HttpResult
from arthas_mcp_proxy.errors import DomainError, map_exception
from arthas_mcp_proxy.models import ErrorCode
from arthas_mcp_proxy.server import (
    _job_cancel_events,
    _job_cancel_lock,
    _job_store,
    _managed_diagnostic_backend,
    cancel_diagnostic_job,
    heap_info,
    thread_dump,
    trace_method,
    watch_method,
)
from arthas_mcp_proxy.ssh_pool import get_connection_pool
from arthas_mcp_proxy.typed_executor import execute_typed_command

if TYPE_CHECKING:
    from collections.abc import Iterator

PID = 42
TELNET = 3658
HTTP_PORT = 8563
ARTHAS_SH = "/tmp/as.sh"  # noqa: S108


def _session() -> MagicMock:
    session = MagicMock()
    session.host = "10.0.0.8"
    session.port = 22
    session.username = "ops"
    session.start_time = None
    session.boot_id = None
    session.session_id = "c2-sess"
    return session


def _seed_http(session: MagicMock, pid: int = PID) -> None:
    with _PID_STATE_LOCK:
        _PID_STATE[_state_key(session, pid)] = {
            "port": TELNET,
            "http_port": HTTP_PORT,
            "owner": None,
        }


def _ssh_command(call: object) -> str:
    args = getattr(call, "args", ())
    kwargs = getattr(call, "kwargs", {})
    if len(args) >= 2:
        return str(args[1])
    if args:
        return str(args[0])
    return str(kwargs.get("command", ""))


def _cli_called(ssh: MagicMock) -> bool:
    for call in ssh.call_args_list:
        cmd = _ssh_command(call)
        if "arthas-client.jar" in cmd and " -c " in cmd:
            return True
    return False


def _mcp_is_error(raw: str) -> bool:
    payload = json.loads(raw)
    if payload.get("isError") is True:
        return True
    structured = payload.get("structuredContent") or {}
    return isinstance(structured, dict) and structured.get("status") == "error"


@contextmanager
def _patched_http_exec(
    session: MagicMock,
    *,
    execute_side_effect: Any = None,
    execute_result: HttpResult | None = None,
) -> Iterator[MagicMock]:
    """Patch HTTP + CLI spies for ``_exec_command``. Yields the ``_exec_ssh`` mock."""
    _seed_http(session)
    with (
        patch("arthas_mcp_proxy.arthas_client._ensure_agent", return_value=TELNET),
        patch(
            "arthas_mcp_proxy.arthas_client._find_arthas_client_jar",
            return_value="/tmp/arthas-client.jar",  # noqa: S108
        ),
        patch("arthas_mcp_proxy.arthas_client._get_java_home", return_value=""),
        patch(
            "arthas_mcp_proxy.arthas_client._exec_ssh",
            return_value=("cli-out", "", 0),
        ) as ssh,
        patch("arthas_mcp_proxy.arthas_client.ArthasHttpClient") as http_cls,
    ):
        http = http_cls.return_value
        if execute_side_effect is not None:
            http.execute.side_effect = execute_side_effect
        else:
            http.execute.return_value = execute_result or HttpResult("http-ok")
        yield ssh


def _run_exec(
    session: MagicMock,
    command: str,
    *,
    execute_side_effect: Any = None,
    execute_result: HttpResult | None = None,
    cancel: threading.Event | None = None,
) -> tuple[str, dict[str, object], MagicMock]:
    state: dict[str, object] = {"backend": "arthas_cli", "degraded": False}
    with _patched_http_exec(
        session,
        execute_side_effect=execute_side_effect,
        execute_result=execute_result,
    ) as ssh:
        output = _exec_command(
            session,
            PID,
            command,
            ARTHAS_SH,
            backend_state=state,
            cancel=cancel,
        )
    return output, state, ssh


def _exec_command_in(
    session: MagicMock,
    command: str,
    ssh: MagicMock,
    *,
    cancel: threading.Event | None = None,
    backend_state: dict[str, object] | None = None,
) -> str:
    del ssh
    return _exec_command(
        session,
        PID,
        command,
        ARTHAS_SH,
        backend_state=backend_state,
        cancel=cancel,
    )


# ── C2-a ─────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_c2_a_arthas_http_error_is_not_connection_error() -> None:
    assert not issubclass(ArthasHttpError, ConnectionError)
    err = ArthasHttpError("connection refused", code="unreachable")
    assert not isinstance(err, ConnectionError)
    assert err.code == "unreachable"


# ── C2-b ─────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_c2_b_http_success_does_not_call_cli() -> None:
    session = _session()
    output, state, ssh = _run_exec(session, "jvm", execute_result=HttpResult("jvm ok"))
    assert output == "jvm ok"
    assert state["backend"] == "arthas_http"
    assert state["degraded"] is False
    assert _cli_called(ssh) is False


# ── C2-c ─────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_c2_c_connect_refused_falls_back_once_for_safe_command() -> None:
    session = _session()
    output, state, ssh = _run_exec(
        session,
        "version",
        execute_side_effect=ArthasHttpError("connection refused", code="unreachable"),
    )
    assert "cli-out" in output
    assert state["backend"] == "arthas_cli"
    assert state["degraded"] is True
    assert _cli_called(ssh) is True
    assert sum(1 for c in ssh.call_args_list if "arthas-client.jar" in str(c)) == 1


@pytest.mark.unit
def test_c2_c_connect_timeout_falls_back_for_safe_command() -> None:
    session = _session()
    output, state, ssh = _run_exec(
        session,
        "thread -n 5",
        execute_side_effect=ArthasHttpError("connection timed out", code="unreachable"),
    )
    assert "cli-out" in output
    assert state["degraded"] is True
    assert state["backend"] == "arthas_cli"
    assert _cli_called(ssh) is True


@pytest.mark.unit
def test_c2_c_connect_refused_curl_is_unreachable() -> None:
    def execute(command: str, timeout: int = 60) -> tuple[str, str, int]:
        del command, timeout
        return "", "Failed to connect to 127.0.0.1: connection refused", 7

    with pytest.raises(ArthasHttpError) as exc_info:
        ArthasHttpClient(execute, 8563).execute("jvm")
    assert exc_info.value.code == "unreachable"
    assert not isinstance(exc_info.value, ConnectionError)


@pytest.mark.unit
def test_c2_c_http_fail_4xx_after_accept_is_not_unreachable() -> None:
    def execute(command: str, timeout: int = 60) -> tuple[str, str, int]:
        del command, timeout
        return "", "The requested URL returned error: 500", 22

    with pytest.raises(ArthasHttpError) as exc_info:
        ArthasHttpClient(execute, 8563).execute("jvm")
    assert exc_info.value.code == "protocol_error"
    assert exc_info.value.code != "unreachable"

    session = _session()
    with _patched_http_exec(session, execute_side_effect=exc_info.value) as ssh:
        with pytest.raises(ArthasHttpError) as run_info:
            _exec_command_in(session, "jvm", ssh)
        assert run_info.value.code == "protocol_error"
        assert _cli_called(ssh) is False


@pytest.mark.unit
def test_c2_c_post_submit_max_time_is_not_unreachable() -> None:
    def execute(command: str, timeout: int = 60) -> tuple[str, str, int]:
        del command, timeout
        return "", "Operation timed out after 1000 milliseconds with 0 bytes received", 28

    with pytest.raises(ArthasHttpError) as exc_info:
        ArthasHttpClient(execute, 8563).execute("jvm")
    assert exc_info.value.code != "unreachable"
    assert exc_info.value.code == "protocol_error"

    session = _session()
    with _patched_http_exec(session, execute_side_effect=exc_info.value) as ssh:
        with pytest.raises(ArthasHttpError):
            _exec_command_in(session, "jvm", ssh)
        assert _cli_called(ssh) is False


@pytest.mark.unit
def test_c2_c_connect_timeout_curl_is_unreachable() -> None:
    def execute(command: str, timeout: int = 60) -> tuple[str, str, int]:
        del command, timeout
        return "", "Failed to connect to 127.0.0.1 port 8563: Connection timed out", 28

    with pytest.raises(ArthasHttpError) as exc_info:
        ArthasHttpClient(execute, 8563).execute("version")
    assert exc_info.value.code == "unreachable"

    session = _session()
    output, state, ssh = _run_exec(session, "version", execute_side_effect=exc_info.value)
    assert "cli-out" in output
    assert state["degraded"] is True
    assert state["backend"] == "arthas_cli"
    assert _cli_called(ssh) is True
    assert sum(1 for c in ssh.call_args_list if "arthas-client.jar" in str(c)) == 1


@pytest.mark.unit
def test_c2_c_connection_error_after_accept_does_not_cli() -> None:
    session = _session()
    with _patched_http_exec(
        session,
        execute_side_effect=ConnectionError("reset after accept"),
    ) as ssh:
        with pytest.raises(ConnectionError):
            _exec_command_in(session, "jvm", ssh)
        assert _cli_called(ssh) is False


# ── C2-d ─────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_c2_d_command_failed_does_not_cli() -> None:
    session = _session()
    with _patched_http_exec(
        session,
        execute_side_effect=ArthasHttpError("unknown command", code="command_failed"),
    ) as ssh:
        with pytest.raises(DomainError) as exc_info:
            _exec_command_in(session, "jvm", ssh)
        assert exc_info.value.code is ErrorCode.ARTHAS_COMMAND_FAILED
        assert map_exception(exc_info.value).code is ErrorCode.ARTHAS_COMMAND_FAILED
        assert _cli_called(ssh) is False


@pytest.mark.unit
def test_c2_d_http_failed_state_is_command_failed_not_fallback() -> None:
    def execute(command: str, timeout: int = 60) -> tuple[str, str, int]:
        del command, timeout
        return '{"state":"FAILED","message":"unknown command"}', "", 0

    with pytest.raises(ArthasHttpError) as exc_info:
        ArthasHttpClient(execute, 8563).execute("not-a-command")
    assert exc_info.value.code == "command_failed"
    assert "unknown command" in str(exc_info.value)
    assert not isinstance(exc_info.value, ConnectionError)


# ── C2-e ─────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_c2_e_empty_body_does_not_cli() -> None:
    def execute(command: str, timeout: int = 60) -> tuple[str, str, int]:
        del command, timeout
        return "", "", 0

    with pytest.raises(ArthasHttpError) as exc_info:
        ArthasHttpClient(execute, 8563).execute("jvm")
    assert exc_info.value.code in {"empty_body", "protocol_error"}
    assert exc_info.value.code != "unreachable"

    session = _session()
    with _patched_http_exec(
        session,
        execute_side_effect=ArthasHttpError("empty", code=exc_info.value.code),
    ) as ssh:
        with pytest.raises(ArthasHttpError) as run_info:
            _exec_command_in(session, "jvm", ssh)
        assert run_info.value.code != "unreachable"
        assert _cli_called(ssh) is False


@pytest.mark.unit
def test_c2_e_half_packet_does_not_cli() -> None:
    def execute(command: str, timeout: int = 60) -> tuple[str, str, int]:
        del command, timeout
        return '{"state":"SUCC', "", 0

    with pytest.raises(ArthasHttpError) as exc_info:
        ArthasHttpClient(execute, 8563).execute("jvm")
    assert exc_info.value.code in {"empty_body", "protocol_error"}
    assert exc_info.value.code != "unreachable"

    session = _session()
    with _patched_http_exec(
        session,
        execute_side_effect=ArthasHttpError("half packet", code=exc_info.value.code),
    ) as ssh:
        with pytest.raises(ArthasHttpError):
            _exec_command_in(session, "memory", ssh)
        assert _cli_called(ssh) is False


# ── C2-f ─────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_c2_f_watch_already_started_does_not_fallback() -> None:
    session = _session()
    with _patched_http_exec(
        session,
        execute_side_effect=ArthasHttpError("connection refused", code="unreachable"),
    ) as ssh:
        with pytest.raises(ArthasHttpError) as exc_info:
            _exec_command_in(session, "watch demo.MathGame prime", ssh)
        assert exc_info.value.code == "unreachable"
        assert _cli_called(ssh) is False


@pytest.mark.unit
def test_c2_f_trace_does_not_fallback() -> None:
    session = _session()
    with _patched_http_exec(
        session,
        execute_side_effect=ArthasHttpError("connection refused", code="unreachable"),
    ) as ssh:
        with pytest.raises(ArthasHttpError):
            _exec_command_in(session, "trace demo.MathGame prime", ssh)
        assert _cli_called(ssh) is False


@contextmanager
def _patched_mcp_observation(session: MagicMock) -> Iterator[MagicMock]:
    _seed_http(session)
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.arthas_client._ensure_agent", return_value=TELNET),
        patch(
            "arthas_mcp_proxy.arthas_client._find_arthas_path",
            return_value=ARTHAS_SH,
        ),
        patch("arthas_mcp_proxy.arthas_client._get_sudo_user", return_value=None),
        patch(
            "arthas_mcp_proxy.arthas_client._find_arthas_client_jar",
            return_value="/tmp/arthas-client.jar",  # noqa: S108
        ),
        patch("arthas_mcp_proxy.arthas_client._get_java_home", return_value=""),
        patch("arthas_mcp_proxy.arthas_client.ArthasHttpClient") as http_cls,
        patch(
            "arthas_mcp_proxy.arthas_client._exec_ssh",
            return_value=("cli-out", "", 0),
        ) as ssh,
    ):
        http_cls.return_value.execute.side_effect = ArthasHttpError(
            "connection refused", code="unreachable"
        )
        yield ssh


@pytest.mark.unit
def test_c2_f_watch_method_mcp_does_not_cli() -> None:
    session = _session()
    with _patched_mcp_observation(session) as ssh:
        raw = watch_method(
            session_id="c2-sess",
            pid=PID,
            class_pattern="demo.MathGame",
            method_pattern="prime",
        )
    assert _mcp_is_error(raw)
    assert _cli_called(ssh) is False


@pytest.mark.unit
def test_c2_f_trace_method_mcp_does_not_cli() -> None:
    session = _session()
    with _patched_mcp_observation(session) as ssh:
        raw = trace_method(
            session_id="c2-sess",
            pid=PID,
            class_pattern="demo.MathGame",
            method_pattern="prime",
        )
    assert _mcp_is_error(raw)
    assert _cli_called(ssh) is False


# ── C2-g ─────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_c2_g_cancelled_does_not_fallback() -> None:
    session = _session()
    cancel = threading.Event()
    cancel.set()
    with _patched_http_exec(
        session,
        execute_side_effect=ArthasHttpError("connection refused", code="unreachable"),
    ) as ssh:
        with pytest.raises(ArthasHttpError) as exc_info:
            _exec_command_in(session, "jvm", ssh, cancel=cancel)
        assert exc_info.value.code == "unreachable"
        assert _cli_called(ssh) is False


@pytest.mark.unit
def test_c2_g_cancel_diagnostic_job_does_not_cli() -> None:
    """cancel_diagnostic_job Event reaches typed short path; unreachable HTTP does not CLI."""
    job = _job_store.create()
    cancel_event = threading.Event()
    manager_cancel = threading.Event()
    with _job_cancel_lock:
        _job_cancel_events[job.job_id] = cancel_event

    session = _session()
    pool = get_connection_pool()

    def refuse_after_cancel(*_args: object, **_kwargs: object) -> HttpResult:
        cancelled = json.loads(cancel_diagnostic_job(job.job_id))
        assert cancelled["status"] == "CANCELLED"
        assert cancel_event.is_set()
        raise ArthasHttpError("connection refused", code="unreachable")

    with (
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.arthas_client._get_sudo_user", return_value=None),
        patch(
            "arthas_mcp_proxy.arthas_client._find_arthas_path",
            return_value=ARTHAS_SH,
        ),
        _patched_http_exec(session, execute_side_effect=refuse_after_cancel) as ssh,
    ):
        with pytest.raises(DomainError):
            _managed_diagnostic_backend(
                job.job_id,
                "c2-sess",
                PID,
                "jvm",
                {},
                60,
                cancel_event,
                manager_cancel,
                lambda _chunk: None,
            )
        assert _cli_called(ssh) is False


# ── C2-h ─────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_c2_h_client_thread_dump_records_http_backend() -> None:
    session = _session()
    _seed_http(session)
    client = ArthasClient(session)
    with (
        patch("arthas_mcp_proxy.arthas_client._get_sudo_user", return_value=None),
        patch(
            "arthas_mcp_proxy.arthas_client._find_arthas_path",
            return_value=ARTHAS_SH,
        ),
        patch("arthas_mcp_proxy.arthas_client._ensure_agent", return_value=TELNET),
        patch("arthas_mcp_proxy.arthas_client.ArthasHttpClient") as http_cls,
        patch("arthas_mcp_proxy.arthas_client._exec_ssh") as ssh,
    ):
        http_cls.return_value.execute.return_value = HttpResult("thread output")
        output = client.thread_dump(pid=PID, top_n=5)
    assert output == "thread output"
    assert client.last_backend == "arthas_http"
    assert client.last_backend_degraded is False
    assert _cli_called(ssh) is False


@pytest.mark.unit
def test_c2_h_client_heap_info_records_degraded_cli() -> None:
    session = _session()
    _seed_http(session)
    client = ArthasClient(session)
    with (
        patch("arthas_mcp_proxy.arthas_client._get_sudo_user", return_value=None),
        patch(
            "arthas_mcp_proxy.arthas_client._find_arthas_path",
            return_value=ARTHAS_SH,
        ),
        patch("arthas_mcp_proxy.arthas_client._ensure_agent", return_value=TELNET),
        patch(
            "arthas_mcp_proxy.arthas_client._find_arthas_client_jar",
            return_value="/tmp/arthas-client.jar",  # noqa: S108
        ),
        patch("arthas_mcp_proxy.arthas_client._get_java_home", return_value=""),
        patch("arthas_mcp_proxy.arthas_client.ArthasHttpClient") as http_cls,
        patch(
            "arthas_mcp_proxy.arthas_client._exec_ssh",
            return_value=("dash ok", "", 0),
        ) as ssh,
    ):
        http_cls.return_value.execute.side_effect = ArthasHttpError(
            "connection refused", code="unreachable"
        )
        output = client.heap_info(pid=PID)
    assert "dash ok" in output
    assert client.last_backend == "arthas_cli"
    assert client.last_backend_degraded is True
    assert _cli_called(ssh) is True


@pytest.mark.contract
def test_c2_h_thread_dump_success_carries_meta_backend() -> None:
    session = _session()
    client = MagicMock()
    client.thread_dump.return_value = "thread output"
    client.last_backend = "arthas_http"
    client.last_backend_degraded = False
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        raw = thread_dump(session_id="c2-sess", pid=PID)
    payload = json.loads(raw)
    assert payload["isError"] is False
    meta = payload["structuredContent"]["meta"]
    assert meta["backend"] == "arthas_http"
    assert meta["degraded"] is False


@pytest.mark.contract
def test_c2_h_heap_info_degraded_carries_meta() -> None:
    session = _session()
    client = MagicMock()
    client.heap_info.return_value = "heap output"
    client.last_backend = "arthas_cli"
    client.last_backend_degraded = True
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        raw = heap_info(session_id="c2-sess", pid=PID)
    payload = json.loads(raw)
    assert payload["isError"] is False
    meta = payload["structuredContent"]["meta"]
    assert meta["backend"] == "arthas_cli"
    assert meta["degraded"] is True


@pytest.mark.unit
def test_c2_h_typed_short_command_carries_backend() -> None:
    client = MagicMock()
    client.execute_command.return_value = "jvm output"
    client.last_backend = "arthas_http"
    client.last_backend_degraded = False
    result = execute_typed_command(client, pid=PID, command="jvm", params={})
    assert result.status == "success"
    assert result.meta.backend == "arthas_http"
    assert result.meta.degraded is False


# ── C2-i ─────────────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.real_jvm
def test_c2_i_real_http_api_does_not_cli(request: pytest.FixtureRequest) -> None:
    """C2-i: live HTTP /api version/jvm/memory must not fall through to CLI."""
    use_docker = bool(request.config.getoption("--docker-target", default=False))
    has_ssh_host = bool(os.environ.get("TEST_SSH_HOST"))
    if not use_docker and not has_ssh_host:
        pytest.skip("specified-not-run: no docker/target")
    pytest.fail("C2-i live HTTP /api version/jvm/memory path is not wired; not green")
