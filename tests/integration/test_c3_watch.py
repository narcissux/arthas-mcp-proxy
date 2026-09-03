"""C3-l/m live: Docker short watch and long-trace job pagination."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest

from tests.integration.test_b5_lifecycle import _connect, _find_handle, _need_docker
from tests.integration.test_pid_replacement import (
    APP,
    _data_ok,
    _ensure_single_math_game,
    _payload,
)

if TYPE_CHECKING:
    from arthas_mcp_proxy.ssh_pool import SSHSession

pytestmark = [pytest.mark.integration, pytest.mark.real_jvm]

CLASS = "demo.MathGame"
METHOD = "primeFactors"


def _pid_for(session_id: str) -> int:
    from arthas_mcp_proxy.server import find_java_application

    data = _data_ok(find_java_application(session_id, APP))
    return int(data["candidates"][0]["pid"])


def _job_payload(raw: str) -> dict:
    body = json.loads(raw)
    assert isinstance(body, dict)
    return body


def _cancel_running() -> None:
    from arthas_mcp_proxy.server import cancel_diagnostic_job, list_diagnostic_jobs

    listed = _job_payload(list_diagnostic_jobs(status="RUNNING"))
    for job in listed.get("jobs") or []:
        job_id = job.get("job_id")
        if job_id:
            cancel_diagnostic_job(str(job_id))


def _collect_pages(job_id: str, max_chars: int) -> tuple[str, int]:
    from arthas_mcp_proxy.server import get_diagnostic_job

    chunks: list[str] = []
    cursor = None
    pages = 0
    for _ in range(64):
        page = _job_payload(get_diagnostic_job(job_id, cursor=cursor, max_chars=max_chars))
        chunks.append(str(page.get("output") or ""))
        pages += 1
        cursor = page.get("next_cursor")
        if not cursor:
            break
    else:
        pytest.fail(f"pagination did not terminate for {job_id}")
    return "".join(chunks), pages


def _wait_job(job_id: str, timeout_s: float = 40.0) -> dict:
    from arthas_mcp_proxy.server import get_diagnostic_job

    deadline = time.time() + timeout_s
    last: dict = {}
    while time.time() < deadline:
        last = _job_payload(get_diagnostic_job(job_id, max_chars=16_384))
        if last.get("status") not in {"RUNNING", "QUEUED"}:
            return last
        time.sleep(0.2)
    pytest.fail(f"job {job_id} still {last.get('status')}: {last}")


def _hit_text(text: str) -> bool:
    return any(token in text for token in (METHOD, CLASS, "ts=", "cost", "params"))


def _ready(ssh_session: SSHSession, request: pytest.FixtureRequest) -> tuple[str, str, int]:
    from arthas_mcp_proxy.server import prepare_arthas

    _need_docker(request)
    _cancel_running()
    _ensure_single_math_game(ssh_session)
    session_id = _connect(ssh_session)
    handle = _find_handle(session_id)
    pid = _pid_for(session_id)
    prepared = _data_ok(prepare_arthas(jvm_handle=handle))
    assert prepared["origin"] in {"existing", "started_by_proxy"}
    time.sleep(1)
    return session_id, handle, pid


@pytest.mark.integration
@pytest.mark.real_jvm
def test_c3_l_docker_short_watch(
    request: pytest.FixtureRequest,
    ssh_session: SSHSession,
) -> None:
    """C3-l: Docker short watch finishes with a method hit (await window or job)."""
    from arthas_mcp_proxy.server import cancel_diagnostic_job, watch_method

    _session_id, handle, pid = _ready(ssh_session, request)
    raw = watch_method(
        jvm_handle=handle,
        pid=pid,
        class_pattern=CLASS,
        method_pattern=METHOD,
        times=1,
        await_ms=5000,
    )
    body = _payload(raw)
    assert body["isError"] is False, raw
    structured = body["structuredContent"]
    job_id = str((structured.get("data") or {}).get("job_id") or "")
    try:
        if structured["status"] == "success":
            output = str(structured["data"].get("output") or "")
        else:
            assert structured["status"] == "running", raw
            assert job_id
            finished = _wait_job(job_id, timeout_s=25)
            assert finished["status"] == "SUCCEEDED", finished
            output = str(finished.get("output") or "")
        assert _hit_text(output), output
    finally:
        if job_id:
            cancel_diagnostic_job(job_id)


@pytest.mark.integration
@pytest.mark.real_jvm
def test_c3_m_docker_long_trace_paginates(
    request: pytest.FixtureRequest,
    ssh_session: SSHSession,
) -> None:
    """C3-m: long trace returns running, then paged output concatenates without gaps."""
    from arthas_mcp_proxy.server import (
        cancel_diagnostic_job,
        get_diagnostic_job,
        trace_method,
    )

    _session_id, handle, pid = _ready(ssh_session, request)
    raw = trace_method(
        jvm_handle=handle,
        pid=pid,
        class_pattern=CLASS,
        method_pattern=METHOD,
        times=5,
        ttl=30,
        max_chars=16_384,
        await_ms=0,
    )
    body = _payload(raw)
    assert body["isError"] is False, raw
    structured = body["structuredContent"]
    assert structured["status"] == "running", raw
    job_id = str(structured["data"]["job_id"])
    try:
        finished = _wait_job(job_id, timeout_s=40)
        assert finished["status"] == "SUCCEEDED", finished
        full = str(finished.get("output") or "")
        paged, pages = _collect_pages(job_id, max_chars=48)
        assert paged == full, (pages, paged[:200], full[:200])
        assert pages >= 1
        if len(full) > 48:
            assert pages >= 2, (pages, len(full))
        assert _hit_text(full), full
        once = _job_payload(get_diagnostic_job(job_id, max_chars=16_384))
        assert once.get("next_cursor") in (None, "")
        assert str(once.get("output") or "") == full
    finally:
        cancel_diagnostic_job(job_id)
