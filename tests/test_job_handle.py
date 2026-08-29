"""C4: jobs bind a JVM handle."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.errors import DomainError
from arthas_mcp_proxy.job_manager import ThreadPoolJobManager
from arthas_mcp_proxy.job_store import JOB_MAX_ACTIVE_PER_JVM, JobStore
from arthas_mcp_proxy.jobs import JobStatus
from arthas_mcp_proxy.jvm_registry import get_jvm_registry
from arthas_mcp_proxy.models import ErrorCode
from arthas_mcp_proxy.server import (
    _job_store,
    _mcp_short_success,
    _observation_mcp_result,
    cancel_diagnostic_job,
    get_diagnostic_job,
    list_diagnostic_jobs,
    mcp,
    start_diagnostic_job,
    thread_dump,
    watch_method,
)
from arthas_mcp_proxy.ssh_pool import get_connection_pool
from arthas_mcp_proxy.typed_tool import typed_command_json

README_PATH = Path(__file__).resolve().parents[1] / "README.md"
TARGET_KEY = "ops@10.0.0.8:22"
PID = 4242
START_TIME = "17000"
BOOT_ID = "boot-c4"
APP_NAME = "inventory-service.jar"
PRODUCT_BACKENDS = frozenset(
    {"ssh", "arthas_cli", "arthas_http", "arthas_http_long_polling"}
)


def _mint(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "target_key": TARGET_KEY,
        "pid": PID,
        "start_time": START_TIME,
        "boot_id": BOOT_ID,
        "application_name": APP_NAME,
    }
    kwargs.update(overrides)
    return get_jvm_registry().mint(**kwargs)  # type: ignore[arg-type]


def _session() -> MagicMock:
    session = MagicMock()
    session.session_id = "c4-sess"
    session.host = "10.0.0.8"
    session.port = 22
    session.username = "ops"
    session.start_time = START_TIME
    session.boot_id = BOOT_ID
    return session


def _error_payload(raw: str) -> dict[str, Any]:
    payload = json.loads(raw)
    assert payload["isError"] is True
    return payload


def _error_code(raw: str) -> str:
    return str(_error_payload(raw)["structuredContent"]["error"]["code"])


def _wait_job(job_id: str, *, timeout: float = 1.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    current = json.loads(get_diagnostic_job(job_id))
    while current["status"] == "RUNNING" and time.monotonic() < deadline:
        time.sleep(0.01)
        current = json.loads(get_diagnostic_job(job_id))
    return current


# ── C4-a ─────────────────────────────────────────────────────────────────────


@pytest.mark.contract
def test_c4_a_start_without_target_is_invalid_argument() -> None:
    """C4-a: no handle and no session+pid → INVALID_ARGUMENT, not SUCCEEDED."""
    raw = start_diagnostic_job("thread_dump", {"top_n": 5})
    assert _error_code(raw) == "INVALID_ARGUMENT"
    assert "SUCCEEDED" not in raw
    structured = json.loads(raw)["structuredContent"]
    assert structured["status"] == "error"
    assert structured.get("data") is None or "job_id" not in (structured.get("data") or {})


@pytest.mark.contract
def test_c4_a_thread_dump_without_target_leaves_no_orphan() -> None:
    """C4-a: valid catalog command without a target must not create a job."""
    before = {job.job_id for job in _job_store._jobs.values()}
    raw = start_diagnostic_job("thread_dump", {})
    assert _error_code(raw) == "INVALID_ARGUMENT"
    after = {job.job_id for job in _job_store._jobs.values()}
    assert after == before


@pytest.mark.contract
def test_c4_a_unknown_command_without_target_is_invalid_argument() -> None:
    """Missing target is rejected first even for an unknown command."""
    raw = start_diagnostic_job("unknown", {})
    assert _error_code(raw) == "INVALID_ARGUMENT"


# ── C4-b ─────────────────────────────────────────────────────────────────────


@pytest.mark.contract
def test_c4_b_start_with_handle_records_jvm_handle() -> None:
    """C4-b: start with handle → serialize includes jvm_handle."""
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
        current = _wait_job(payload["job_id"])
    assert payload["jvm_handle"] == handle
    assert current["jvm_handle"] == handle
    assert current["status"] == "SUCCEEDED"


# ── C4-c ─────────────────────────────────────────────────────────────────────


@pytest.mark.contract
def test_c4_c_list_filters_by_jvm_handle() -> None:
    """C4-c: list_diagnostic_jobs(jvm_handle=...) returns only that JVM's jobs."""
    handle_a = _mint(pid=PID)
    handle_b = _mint(pid=PID + 1, application_name="other.jar")
    session = _session()
    client = MagicMock()
    client.execute_command.return_value = "job output"
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=session),
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        job_a = json.loads(start_diagnostic_job("thread_dump", {}, jvm_handle=handle_a))
        job_b = json.loads(start_diagnostic_job("heap_info", {}, jvm_handle=handle_b))
        _wait_job(job_a["job_id"])
        _wait_job(job_b["job_id"])
    listed = json.loads(list_diagnostic_jobs(jvm_handle=handle_a))
    ids = {item["job_id"] for item in listed["jobs"]}
    assert job_a["job_id"] in ids
    assert job_b["job_id"] not in ids
    assert all(item.get("jvm_handle") == handle_a for item in listed["jobs"])


# ── C4-d ─────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_c4_d_fourth_running_job_on_same_jvm_is_quota_exceeded() -> None:
    """C4-d: 4th RUNNING on the same JVM → JOB_QUOTA_EXCEEDED (3 per JVM)."""
    handle = _mint()
    other = _mint(pid=PID + 7, application_name="other.jar")
    session = _session()
    started = threading.Event()
    release = threading.Event()
    hold_count = {"n": 0}
    hold_lock = threading.Lock()

    def hold(*_args: object, **_kwargs: object) -> str:
        with hold_lock:
            hold_count["n"] += 1
            if hold_count["n"] >= 3:
                started.set()
        release.wait(2)
        return json.dumps(
            {"structuredContent": {"data": {"output": "held"}, "summary": "ok"}}
        )

    pool = get_connection_pool()
    running_ids: list[str] = []
    with (
        patch.object(pool, "get_session_by_host", return_value=session),
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.typed_command_json", side_effect=hold),
    ):
        try:
            for _ in range(JOB_MAX_ACTIVE_PER_JVM):
                payload = json.loads(
                    start_diagnostic_job("thread_dump", {}, jvm_handle=handle)
                )
                assert payload["status"] == "RUNNING"
                running_ids.append(payload["job_id"])
            assert started.wait(1)
            fourth = start_diagnostic_job("thread_dump", {}, jvm_handle=handle)
            assert _error_code(fourth) == "JOB_QUOTA_EXCEEDED"
            other_job = json.loads(
                start_diagnostic_job("thread_dump", {}, jvm_handle=other)
            )
            assert other_job["status"] == "RUNNING"
            running_ids.append(other_job["job_id"])
        finally:
            release.set()
            for job_id in running_ids:
                cancel_diagnostic_job(job_id)


# ── C4-e ─────────────────────────────────────────────────────────────────────


@pytest.mark.contract
def test_c4_e_cancel_succeeded_job_is_idempotent() -> None:
    """C4-e: stop/cancel a job already SUCCEEDED → SUCCEEDED JSON, not Error:."""
    job = _job_store.create(jvm_handle=_mint())
    _job_store.update(job.job_id, status=JobStatus.SUCCEEDED, output="done")
    raw = cancel_diagnostic_job(job.job_id)
    assert not raw.startswith("Error:")
    payload = json.loads(raw)
    assert payload["status"] == "SUCCEEDED"
    assert payload["job_id"] == job.job_id
    again = json.loads(cancel_diagnostic_job(job.job_id))
    assert again["status"] == "SUCCEEDED"


@pytest.mark.contract
def test_c4_e_live_start_succeeded_then_cancel_stays_succeeded() -> None:
    """C4-e live path: start finishes immediately, cancel returns SUCCEEDED JSON."""
    handle = _mint()
    session = _session()
    client = MagicMock()
    client.execute_command.return_value = "done immediately"
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=session),
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        payload = json.loads(start_diagnostic_job("thread_dump", {}, jvm_handle=handle))
        current = _wait_job(payload["job_id"])
    assert current["status"] == "SUCCEEDED"
    raw = cancel_diagnostic_job(payload["job_id"])
    assert not raw.startswith("Error:")
    cancelled = json.loads(raw)
    assert cancelled["status"] == "SUCCEEDED"
    assert cancelled["job_id"] == payload["job_id"]
    assert json.loads(get_diagnostic_job(payload["job_id"]))["status"] == "SUCCEEDED"


# ── C4-f ─────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_c4_f_store_expires_handle_bound_job_and_rejects_stale_cursor() -> None:
    """C4-f: fake clock past TTL → EXPIRED; stale cursor → OUTPUT_CURSOR_INVALID."""

    class _Clock:
        def __init__(self) -> None:
            self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

        def __call__(self) -> datetime:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += timedelta(seconds=seconds)

    clock = _Clock()
    store = JobStore(ttl_seconds=10, clock=clock)
    job = store.create(jvm_handle="jvm_c4expire0000001")
    assert job.status is JobStatus.RUNNING
    clock.advance(11)
    expired = store.get(job.job_id)
    assert expired.status is JobStatus.EXPIRED
    assert expired.jvm_handle == "jvm_c4expire0000001"
    listed = store.list(jvm_handle="jvm_c4expire0000001")
    assert listed[0].status is JobStatus.EXPIRED

    live = _job_store.create(jvm_handle="jvm_c4cursor0000001")
    _job_store.update(live.job_id, status=JobStatus.SUCCEEDED, output="abcdefghij")
    stale = get_diagnostic_job(live.job_id, cursor="not-a-valid-cursor")
    assert _error_code(stale) == "OUTPUT_CURSOR_INVALID"


# ── C4-g ─────────────────────────────────────────────────────────────────────


@pytest.mark.contract
def test_c4_g_product_path_never_stamps_arthas_ws() -> None:
    """C4-g: thread_dump / watch / start success meta is never arthas_ws."""
    handle = _mint()
    session = _session()
    client = MagicMock()
    client.thread_dump.return_value = "thread output"
    client.last_backend = "arthas_ws"
    client.last_backend_degraded = False
    client.execute_streaming_command.return_value = "watch output"
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=session),
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        dump = json.loads(thread_dump(jvm_handle=handle))
        watch = json.loads(
            watch_method(jvm_handle=handle, class_pattern="Foo", method_pattern="bar")
        )
        job = json.loads(start_diagnostic_job("thread_dump", {}, jvm_handle=handle))
        _wait_job(job["job_id"])
    dump_backend = dump["structuredContent"]["meta"]["backend"]
    watch_backend = watch["structuredContent"]["meta"]["backend"]
    assert dump["isError"] is False
    assert watch["isError"] is False
    assert dump_backend != "arthas_ws"
    assert watch_backend != "arthas_ws"
    assert dump_backend in PRODUCT_BACKENDS
    assert watch_backend in PRODUCT_BACKENDS
    assert "arthas_ws" not in json.dumps(job)


@pytest.mark.unit
def test_c4_g_product_helpers_remap_arthas_ws() -> None:
    client = MagicMock()
    client.last_backend = "arthas_ws"
    client.last_backend_degraded = False
    payload = json.loads(_mcp_short_success(client, "out", "Thread dump"))
    assert payload["structuredContent"]["meta"]["backend"] != "arthas_ws"
    assert payload["structuredContent"]["meta"]["backend"] in PRODUCT_BACKENDS
    watch = json.loads(
        _observation_mcp_result(
            status="success",
            summary="Watch completed",
            data={"output": "ok"},
            backend="arthas_ws",
        )
    )
    assert watch["structuredContent"]["meta"]["backend"] == "arthas_http_long_polling"


# ── C4-h ─────────────────────────────────────────────────────────────────────


@pytest.mark.contract
def test_c4_h_job_stream_is_proxy_event_stream_not_arthas() -> None:
    """C4-h: /jobs/{id}/stream is the proxy job event stream, not Arthas WS."""
    readme = README_PATH.read_text(encoding="utf-8")
    assert "/jobs/{id}/stream" in readme
    assert "proxy" in readme.lower()
    stream_idx = readme.find("/jobs/{id}/stream")
    window = readme[max(0, stream_idx - 200) : stream_idx + 400].lower()
    assert "proxy" in window
    assert "not an arthas command channel" in window or "not an arthas" in window
    assert "tunnel" in window

    manager = ThreadPoolJobManager(max_workers=1)
    try:
        app = manager.websocket_app()
        route = app.routes[0]
        assert route.path == "/jobs/{job_id}/stream"
        doc = (route.endpoint.__doc__ or "") + (manager.websocket_app.__doc__ or "")
        combined = doc.lower()
        assert "proxy" in combined
        assert "not an arthas" in combined
        assert "tunnel" in combined or "not an arthas command channel" in combined
    finally:
        manager.shutdown()


@pytest.mark.contract
@pytest.mark.asyncio
async def test_c4_list_diagnostic_jobs_schema_accepts_jvm_handle() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    schema = tools["list_diagnostic_jobs"].inputSchema
    assert "jvm_handle" in schema.get("properties", {})
    required = schema.get("required") or []
    assert "jvm_handle" not in required


@pytest.mark.unit
def test_c4_store_create_stamps_handle() -> None:
    store = JobStore()
    job = store.create(jvm_handle="jvm_abc")
    assert job.jvm_handle == "jvm_abc"
    assert store.list(jvm_handle="jvm_abc") == [job]
    assert store.list(jvm_handle="jvm_other") == []


@pytest.mark.unit
def test_c4_store_per_jvm_quota_is_separate_from_global_cap() -> None:
    store = JobStore(max_jobs=10)
    handle = "jvm_quota_one"
    for _ in range(JOB_MAX_ACTIVE_PER_JVM):
        store.create(jvm_handle=handle)
    with pytest.raises(DomainError) as excinfo:
        store.create(jvm_handle=handle)
    assert excinfo.value.code is ErrorCode.JOB_QUOTA_EXCEEDED
    other = store.create(jvm_handle="jvm_quota_two")
    assert other.status is JobStatus.RUNNING


@pytest.mark.unit
def test_c4_d_session_pid_without_handle_still_has_per_jvm_quota() -> None:
    """session+pid without a minted handle still hits the 3 RUNNING cap."""
    session = _session()
    other_session = _session()
    other_session.session_id = "c4-other"
    other_session.host = "10.0.0.9"
    started = threading.Event()
    release = threading.Event()
    hold_count = {"n": 0}
    hold_lock = threading.Lock()

    def hold(*_args: object, **_kwargs: object) -> str:
        with hold_lock:
            hold_count["n"] += 1
            if hold_count["n"] >= 3:
                started.set()
        release.wait(2)
        return json.dumps(
            {"structuredContent": {"data": {"output": "held"}, "summary": "ok"}}
        )

    def get_session(session_id: str) -> MagicMock | None:
        if session_id == "c4-sess":
            return session
        if session_id == "c4-other":
            return other_session
        return None

    pool = get_connection_pool()
    running_ids: list[str] = []
    with (
        patch.object(pool, "get_session", side_effect=get_session),
        patch("arthas_mcp_proxy.server.typed_command_json", side_effect=hold),
    ):
        try:
            for _ in range(JOB_MAX_ACTIVE_PER_JVM):
                payload = json.loads(
                    start_diagnostic_job("thread_dump", {}, "c4-sess", PID)
                )
                assert payload["status"] == "RUNNING"
                assert "jvm_handle" not in payload
                running_ids.append(payload["job_id"])
            assert started.wait(1)
            fourth = start_diagnostic_job("thread_dump", {}, "c4-sess", PID)
            assert _error_code(fourth) == "JOB_QUOTA_EXCEEDED"
            other = json.loads(
                start_diagnostic_job("thread_dump", {}, "c4-other", PID)
            )
            assert other["status"] == "RUNNING"
            running_ids.append(other["job_id"])
            other_pid = json.loads(
                start_diagnostic_job("thread_dump", {}, "c4-sess", PID + 1)
            )
            assert other_pid["status"] == "RUNNING"
            running_ids.append(other_pid["job_id"])
        finally:
            release.set()
            for job_id in running_ids:
                cancel_diagnostic_job(job_id)


@pytest.mark.unit
def test_c4_d_three_watches_then_start_job_hits_job_quota() -> None:
    """C3×C4: 3 watch jobs on H, 4th start_diagnostic_job on H is JOB_QUOTA_EXCEEDED.

    Locked: watches create jobs via _job_store.create after observation acquire;
    they stamp the live handle from _live_handle_for_session so they share the
    same quota bucket as start_diagnostic_job(jvm_handle=H).
    """
    handle = _mint()
    session = _session()
    started = threading.Event()
    release = threading.Event()
    hold_count = {"n": 0}
    hold_lock = threading.Lock()

    def hold(*_args: object, **_kwargs: object) -> str:
        with hold_lock:
            hold_count["n"] += 1
            if hold_count["n"] >= 3:
                started.set()
        release.wait(2)
        return "held"

    client = MagicMock()
    client.execute_streaming_command.side_effect = hold
    pool = get_connection_pool()
    job_ids: list[str] = []
    with (
        patch.object(pool, "get_session_by_host", return_value=session),
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        try:
            for _ in range(JOB_MAX_ACTIVE_PER_JVM):
                raw = watch_method(
                    jvm_handle=handle,
                    class_pattern="Foo",
                    method_pattern="bar",
                    await_ms=0,
                    times=1,
                )
                payload = json.loads(raw)
                assert payload["isError"] is False
                assert payload["structuredContent"]["status"] == "running"
                job_ids.append(str(payload["structuredContent"]["data"]["job_id"]))
            assert started.wait(1)
            fourth = start_diagnostic_job("thread_dump", {}, jvm_handle=handle)
            assert _error_code(fourth) == "JOB_QUOTA_EXCEEDED"
        finally:
            release.set()
            for job_id in job_ids:
                cancel_diagnostic_job(job_id)


@pytest.mark.unit
def test_c4_d_three_start_jobs_then_watch_hits_job_quota() -> None:
    """C3×C4 reverse: 3 held start jobs on H, 4th watch is JOB_QUOTA_EXCEEDED.

    Locked: _run_watch_or_trace acquires observation first, then
    JobStore.create. After 3 start jobs, observation is free so the 4th
    watch fails at create with JOB_QUOTA_EXCEEDED (not OBSERVATION_LIMIT).
    The observation lease is released if create fails.
    """
    handle = _mint()
    session = _session()
    started = threading.Event()
    release = threading.Event()
    hold_count = {"n": 0}
    hold_lock = threading.Lock()

    def hold(*_args: object, **_kwargs: object) -> str:
        with hold_lock:
            hold_count["n"] += 1
            if hold_count["n"] >= 3:
                started.set()
        release.wait(2)
        return json.dumps(
            {"structuredContent": {"data": {"output": "held"}, "summary": "ok"}}
        )

    pool = get_connection_pool()
    running_ids: list[str] = []
    with (
        patch.object(pool, "get_session_by_host", return_value=session),
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.typed_command_json", side_effect=hold),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=MagicMock()),
    ):
        try:
            for _ in range(JOB_MAX_ACTIVE_PER_JVM):
                payload = json.loads(
                    start_diagnostic_job("thread_dump", {}, jvm_handle=handle)
                )
                assert payload["status"] == "RUNNING"
                running_ids.append(payload["job_id"])
            assert started.wait(1)
            fourth = watch_method(
                jvm_handle=handle,
                class_pattern="Foo",
                method_pattern="bar",
                await_ms=0,
                times=1,
            )
            assert _error_code(fourth) == "JOB_QUOTA_EXCEEDED"
        finally:
            release.set()
            for job_id in running_ids:
                cancel_diagnostic_job(job_id)


@pytest.mark.unit
def test_c4_f_ttl_stale_signed_cursor_is_invalid() -> None:
    """C4-f: a real next_cursor that has passed its TTL is OUTPUT_CURSOR_INVALID."""
    job = _job_store.create(jvm_handle="jvm_c4cursor_ttl")
    _job_store.update(job.job_id, status=JobStatus.SUCCEEDED, output="abcdefghij" * 20)
    first = json.loads(get_diagnostic_job(job.job_id, max_chars=7))
    cursor = first["next_cursor"]
    assert cursor
    future = time.time() + 400
    with patch("arthas_mcp_proxy.output_limit.time.time", return_value=future):
        stale = get_diagnostic_job(job.job_id, cursor=cursor, max_chars=7)
    assert _error_code(stale) == "OUTPUT_CURSOR_INVALID"


@pytest.mark.unit
def test_c4_f_mcp_get_expired_job_is_expired_not_not_found() -> None:
    """C4-f extra: MCP get of a TTL-expired job is EXPIRED, not JOB_NOT_FOUND."""

    class _Clock:
        def __init__(self) -> None:
            self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

        def __call__(self) -> datetime:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += timedelta(seconds=seconds)

    clock = _Clock()
    original_clock = _job_store._clock
    original_ttl = _job_store._ttl
    try:
        _job_store._clock = clock
        _job_store._ttl = timedelta(seconds=10)
        job = _job_store.create(jvm_handle="jvm_c4expire_mcp")
        clock.advance(11)
        raw = get_diagnostic_job(job.job_id)
        assert not raw.startswith("Error:")
        payload = json.loads(raw)
        assert payload["status"] == "EXPIRED"
        assert payload["job_id"] == job.job_id
        assert "JOB_NOT_FOUND" not in raw
    finally:
        _job_store._clock = original_clock
        _job_store._ttl = original_ttl


@pytest.mark.unit
def test_c4_g_execute_diagnostic_command_never_stamps_arthas_ws() -> None:
    """C4-g: typed_command_json / execute_typed_command remap arthas_ws."""
    client = MagicMock()
    client.execute_command.return_value = "ok"
    client.last_backend = "arthas_ws"
    raw = typed_command_json(client, pid=1, command="thread_dump", params={"top_n": 1})
    payload = json.loads(raw)
    backend = payload["structuredContent"]["meta"]["backend"]
    assert backend != "arthas_ws"
    assert backend in PRODUCT_BACKENDS


@pytest.mark.unit
def test_c4_store_quota_key_without_handle_caps_running() -> None:
    store = JobStore(max_jobs=10)
    key = "ops@10.0.0.8:22|4242|17000|boot-c4"
    for _ in range(JOB_MAX_ACTIVE_PER_JVM):
        job = store.create(quota_key=key)
        assert job.jvm_handle is None
        assert job.quota_key == key
    with pytest.raises(DomainError) as excinfo:
        store.create(quota_key=key)
    assert excinfo.value.code is ErrorCode.JOB_QUOTA_EXCEEDED
    other = store.create(quota_key=key + "|other")
    assert other.status is JobStatus.RUNNING

