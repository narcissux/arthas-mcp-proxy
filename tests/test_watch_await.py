"""C3: watch/trace HTTP long-poll + await_ms."""

from __future__ import annotations

import json
import threading
import time
from contextlib import suppress
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.arthas_client import ArthasClient as RealArthasClient
from arthas_mcp_proxy.arthas_http import ArthasHttpStreamingClient
from arthas_mcp_proxy.command_catalog import COMMANDS, build_command
from arthas_mcp_proxy.jobs import JobStatus
from arthas_mcp_proxy.observation_policy import ObservationPolicy
from arthas_mcp_proxy.server import (
    _job_store,
    _try_finish_job,
    cancel_diagnostic_job,
    get_diagnostic_job,
    mcp,
    trace_method,
    watch_method,
)

_watch_impl = cast("Any", watch_method).__wrapped__
_trace_impl = cast("Any", trace_method).__wrapped__

HOST = "10.0.0.8"
PORT = 22
USER = "ops"
PID = 4242


def _session() -> MagicMock:
    session = MagicMock()
    session.session_id = "c3-sess"
    session.host = HOST
    session.port = PORT
    session.username = USER
    session.start_time = "17000"
    session.boot_id = "boot-old"
    return session


def _payload(raw: str) -> dict[str, Any]:
    return json.loads(raw)


def _structured(raw: str) -> dict[str, Any]:
    payload = _payload(raw)
    structured = payload["structuredContent"]
    assert isinstance(structured, dict)
    return structured


def _error_code(raw: str) -> str:
    payload = _payload(raw)
    assert payload["isError"] is True
    return str(payload["structuredContent"]["error"]["code"])


def _running_job(raw: str) -> str:
    payload = _payload(raw)
    assert payload["isError"] is False
    structured = payload["structuredContent"]
    assert structured["status"] == "running"
    job_id = structured["data"]["job_id"]
    assert job_id
    return str(job_id)


@pytest.mark.unit
def test_c3_a_catalog_watch_method_is_streaming_with_structured_params() -> None:
    """C3-a: catalog watch_method is streaming with class/method/condition/times."""
    spec = COMMANDS["watch_method"]
    assert spec.streaming is True
    rendered = build_command(
        "watch_method",
        {
            "class_pattern": "com.Foo",
            "method_pattern": "bar",
            "condition": "x=1",
            "times": 3,
        },
    )
    assert rendered == "watch com.Foo bar 'x=1' -n 3"
    traced = build_command(
        "trace_method",
        {
            "class_pattern": "com.Foo",
            "method_pattern": "bar",
            "condition": "x=1",
            "times": 3,
        },
    )
    assert traced == "trace com.Foo bar 'x=1' -n 3"
    with pytest.raises(ValueError, match="unsupported characters"):
        build_command("watch_method", {"class_pattern": "com.Foo\nBar", "method_pattern": "bar"})


@pytest.mark.contract
@pytest.mark.asyncio
async def test_c3_b_await_ms_schema_on_watch_and_trace() -> None:
    """C3-b: await_ms schema is 0..5000 with default 5000."""
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    for name in ("watch_method", "trace_method"):
        props = tools[name].inputSchema.get("properties") or {}
        schema = props["await_ms"]
        assert schema.get("minimum") == 0
        assert schema.get("maximum") == 5000
        assert schema.get("default") == 5000


@pytest.mark.contract
def test_c3_c_await_ms_zero_returns_running_job() -> None:
    """C3-c: await_ms=0 immediately returns running + job_id, isError=false."""
    hold = threading.Event()
    client = MagicMock()

    def _block(*_args: object, **_kwargs: object) -> str:
        hold.wait(timeout=2)
        return "late"

    client.execute_streaming_command.side_effect = _block
    session = _session()
    policy = ObservationPolicy()
    with (
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", policy),
    ):
        started = time.monotonic()
        raw = _watch_impl(session, PID, "com.Foo", "bar", times=1, await_ms=0)
        elapsed_ms = (time.monotonic() - started) * 1000
        job_id = _running_job(raw)
        hold.set()
        for _ in range(50):
            current = _payload(get_diagnostic_job(job_id))
            if current["status"] != "RUNNING":
                break
            time.sleep(0.02)
    assert elapsed_ms < 500
    client.watch_method.assert_not_called()


@pytest.mark.contract
def test_c3_d_fast_finish_within_await_is_success() -> None:
    """C3-d: finishes within 200ms and await=5000 → success, job SUCCEEDED."""
    client = MagicMock()
    client.execute_streaming_command.return_value = "hit com.Foo.bar"
    session = _session()
    with (
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", ObservationPolicy()),
    ):
        raw = _watch_impl(session, PID, "com.Foo", "bar", times=1, await_ms=5000)
    payload = _payload(raw)
    assert payload["isError"] is False
    structured = payload["structuredContent"]
    assert structured["status"] == "success"
    assert structured["data"]["output"] == "hit com.Foo.bar"
    assert structured["meta"]["backend"] == ArthasHttpStreamingClient.backend_name
    job_id = structured["data"]["job_id"]
    retrieved = _payload(get_diagnostic_job(job_id))
    assert retrieved["status"] == "SUCCEEDED"
    assert "hit com.Foo.bar" in retrieved["output"]


@pytest.mark.contract
def test_c3_e_still_running_then_get_assembles_output() -> None:
    """C3-e: await window exceeded, still running; get_diagnostic_job sees RUNNING."""
    hold = threading.Event()
    client = MagicMock()

    def _stream(*_args: object, **_kwargs: object) -> str:
        hold.wait(timeout=5)
        return "late"

    client.execute_streaming_command.side_effect = _stream
    session = _session()
    with (
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", ObservationPolicy()),
    ):
        raw = _watch_impl(session, PID, "com.Foo", "bar", times=1, await_ms=80)
        payload = _payload(raw)
        assert payload["isError"] is False
        structured = payload["structuredContent"]
        assert structured["status"] == "running"
        job_id = structured["data"]["job_id"]
        assert job_id
        current = _payload(get_diagnostic_job(job_id))
        assert current["status"] == "RUNNING"
        hold.set()
        cancel_diagnostic_job(job_id)


@pytest.mark.contract
@pytest.mark.parametrize("await_ms", [5001, -1])
def test_c3_f_await_ms_out_of_range_is_invalid_argument(await_ms: int) -> None:
    """C3-f: await_ms=5001 or negative → INVALID_ARGUMENT."""
    client = MagicMock()
    session = _session()
    with (
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", ObservationPolicy()),
    ):
        raw = _watch_impl(session, PID, "com.Foo", "bar", times=1, await_ms=await_ms)
    assert _error_code(raw) == "INVALID_ARGUMENT"
    client.execute_streaming_command.assert_not_called()


@pytest.mark.contract
def test_c3_g_cancel_running_hits_interrupt_and_releases_quota() -> None:
    """C3-g: hold 3, cancel one, 4th succeeds; cancelled job hits interrupt+close."""
    actions: list[str] = []
    pull_started = threading.Event()
    interrupted = False
    holds: list[threading.Event] = []
    factory_lock = threading.Lock()
    constructed = 0

    def fake_request(self: object, payload: dict[str, object], timeout: int) -> dict[str, object]:
        nonlocal interrupted
        action = str(payload.get("action"))
        actions.append(action)
        if action == "init_session":
            return {"sessionId": "s1", "consumerId": "c1"}
        if action == "async_exec":
            return {"body": {"jobId": 9}}
        if action == "interrupt_job":
            interrupted = True
            return {}
        if action == "pull_results":
            pull_started.set()
            if interrupted:
                return {"body": {"results": [{"state": "TERMINATED", "jobId": 9}]}}
            time.sleep(0.05)
            return {"body": {"results": []}}
        return {}

    def factory(session: object) -> object:
        nonlocal constructed
        with factory_lock:
            constructed += 1
            idx = constructed
        if idx == 1:
            return RealArthasClient(session)
        mock = MagicMock()
        hold = threading.Event()
        holds.append(hold)

        def _block(*_args: object, **_kwargs: object) -> str:
            hold.wait(timeout=5)
            return "held"

        mock.execute_streaming_command.side_effect = _block
        return mock

    session = _session()
    leftover: list[str] = []
    with (
        patch("arthas_mcp_proxy.server.ArthasClient", side_effect=factory),
        patch("arthas_mcp_proxy.arthas_client._ensure_agent", return_value=3658),
        patch(
            "arthas_mcp_proxy.arthas_client._detect_existing_agent",
            return_value=(3658, 8563),
        ),
        patch("arthas_mcp_proxy.arthas_client._check_process_identity", return_value=True),
        patch("arthas_mcp_proxy.arthas_client._find_arthas_path", return_value="/tmp/as.sh"),  # noqa: S108
        patch("arthas_mcp_proxy.arthas_client._get_sudo_user", return_value=None),
        patch.object(ArthasHttpStreamingClient, "_request", fake_request),
        patch("arthas_mcp_proxy.server._watch_policy", ObservationPolicy()),
    ):
        try:
            job1 = _running_job(_watch_impl(session, PID, "com.Foo", "bar", times=1, await_ms=0))
            leftover.append(job1)
            assert pull_started.wait(2)
            leftover.append(
                _running_job(_watch_impl(session, PID, "com.Foo", "bar", times=1, await_ms=0))
            )
            leftover.append(
                _running_job(_watch_impl(session, PID, "com.Foo", "bar", times=1, await_ms=0))
            )
            assert (
                _error_code(_watch_impl(session, PID, "com.Foo", "bar", times=1, await_ms=0))
                == "OBSERVATION_LIMIT_EXCEEDED"
            )
            cancelled = _payload(cancel_diagnostic_job(job1))
            assert cancelled["status"] == "CANCELLED"
            for _ in range(50):
                if "interrupt_job" in actions and "close_session" in actions:
                    break
                time.sleep(0.05)
            later = None
            for _ in range(50):
                later = _watch_impl(session, PID, "com.Foo", "bar", times=1, await_ms=0)
                if _payload(later)["isError"] is False:
                    break
                time.sleep(0.05)
            assert later is not None
            later_payload = _payload(later)
            assert later_payload["isError"] is False
            assert later_payload["structuredContent"]["status"] in {"running", "success"}
            leftover.append(str(later_payload["structuredContent"]["data"]["job_id"]))
        finally:
            for job_id in leftover:
                with suppress(Exception):
                    cancel_diagnostic_job(job_id)
            for hold in holds:
                hold.set()
    assert "interrupt_job" in actions
    assert "close_session" in actions


@pytest.mark.contract
def test_c3_h_second_cancel_is_idempotent_json() -> None:
    """C3-h: second cancel returns current terminal JSON, not a raw Error: string."""
    hold = threading.Event()
    client = MagicMock()

    def _block(*_args: object, **_kwargs: object) -> str:
        hold.wait(timeout=2)
        return "late"

    client.execute_streaming_command.side_effect = _block
    session = _session()
    with (
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", ObservationPolicy()),
    ):
        raw = _watch_impl(session, PID, "com.Foo", "bar", times=1, await_ms=0)
        job_id = _running_job(raw)
        first = cancel_diagnostic_job(job_id)
        second = cancel_diagnostic_job(job_id)
        hold.set()
    assert not first.startswith("Error:")
    assert not second.startswith("Error:")
    assert _payload(first)["status"] == "CANCELLED"
    assert _payload(second)["status"] == "CANCELLED"
    assert _payload(second)["job_id"] == job_id


@pytest.mark.unit
def test_c3_i_try_finish_job_cas_first_writer_wins() -> None:
    """C3-i: _try_finish_job is CAS — second terminal write loses, status unchanged."""
    job = _job_store.create()
    assert _try_finish_job(job.job_id, JobStatus.SUCCEEDED, output="done") is True
    assert _try_finish_job(job.job_id, JobStatus.CANCELLED) is False
    stored = _job_store.get(job.job_id)
    assert stored.status is JobStatus.SUCCEEDED
    assert stored.output == "done"


@pytest.mark.unit
def test_c3_i_complete_vs_cancel_one_terminal() -> None:
    """C3-i: complete vs cancel race yields exactly one terminal status."""
    started = threading.Event()
    release = threading.Event()
    client = MagicMock()

    def _stream(*_args: object, **_kwargs: object) -> str:
        started.set()
        release.wait(timeout=2)
        return "done"

    client.execute_streaming_command.side_effect = _stream
    session = _session()
    with (
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", ObservationPolicy()),
    ):
        raw = _watch_impl(session, PID, "com.Foo", "bar", times=1, await_ms=0)
        job_id = _running_job(raw)
        assert started.wait(2)
        canceler = threading.Thread(target=cancel_diagnostic_job, args=(job_id,))
        canceler.start()
        release.set()
        canceler.join(timeout=2)
        current = _payload(get_diagnostic_job(job_id))
        for _ in range(50):
            current = _payload(get_diagnostic_job(job_id))
            if current["status"] != "RUNNING":
                break
            time.sleep(0.02)
    assert current["status"] in {"SUCCEEDED", "CANCELLED"}
    later = _payload(get_diagnostic_job(job_id))
    assert later["status"] == current["status"]
    assert later["status"] != "RUNNING"


@pytest.mark.contract
@pytest.mark.parametrize(
    ("kwargs", "tool"),
    [
        ({"times": 0}, "watch"),
        ({"times": 21}, "watch"),
        ({"times": 0}, "trace"),
        ({"times": 21}, "trace"),
        ({"times": 1, "ttl": 121}, "trace"),
    ],
)
def test_c3_j_observation_limits_use_one_code(kwargs: dict[str, int], tool: str) -> None:
    """C3-j: times=0 / times>20 / ttl>120 → OBSERVATION_LIMIT_EXCEEDED."""
    client = MagicMock()
    session = _session()
    with (
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", ObservationPolicy()),
    ):
        if tool == "watch":
            raw = _watch_impl(session, PID, "com.Foo", "bar", await_ms=0, **kwargs)
        else:
            raw = _trace_impl(session, PID, "com.Foo", "bar", await_ms=0, **kwargs)
    assert _error_code(raw) == "OBSERVATION_LIMIT_EXCEEDED"
    client.execute_streaming_command.assert_not_called()


@pytest.mark.contract
@pytest.mark.parametrize("bad", ["com.Foo\nBar", "com.Foo\x00Bar"])
def test_c3_k_newline_or_nul_class_is_invalid_argument(bad: str) -> None:
    """C3-k: class/method containing newline or NUL is rejected."""
    client = MagicMock()
    session = _session()
    with (
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", ObservationPolicy()),
    ):
        raw = _watch_impl(session, PID, bad, "bar", times=1, await_ms=0)
        other = _trace_impl(session, PID, "com.Foo", bad, times=1, ttl=5, await_ms=0)
    assert _error_code(raw) == "INVALID_ARGUMENT"
    assert _error_code(other) == "INVALID_ARGUMENT"
    client.execute_streaming_command.assert_not_called()


@pytest.mark.unit
def test_arthas_client_watch_method_cli_path_is_closed() -> None:
    """CLI ArthasClient.watch_method is closed; MCP streaming is the only path."""
    with pytest.raises(RuntimeError, match="MCP watch_method"):
        RealArthasClient(MagicMock()).watch_method(1, "com.Foo", "bar")
