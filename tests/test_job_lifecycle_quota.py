from datetime import datetime, timedelta, timezone

import pytest

from arthas_mcp_proxy.errors import DomainError
from arthas_mcp_proxy.job_store import JobStore
from arthas_mcp_proxy.jobs import JobStatus
from arthas_mcp_proxy.models import ErrorCode


@pytest.mark.unit
def test_list_jobs_returns_newest_first_and_expires_stale_running_jobs() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = JobStore(max_jobs=2, ttl_seconds=60, clock=lambda: now)
    stale = store.create()
    now = now + timedelta(seconds=61)
    fresh = store.create()

    jobs = store.list()

    assert [job.job_id for job in jobs] == [fresh.job_id, stale.job_id]
    assert stale.status is JobStatus.EXPIRED
    assert stale.completed_at == now


@pytest.mark.unit
def test_running_job_quota_is_a_lease_released_by_terminal_transition() -> None:
    store = JobStore(max_jobs=1, ttl_seconds=60)
    first = store.create()

    with pytest.raises(DomainError) as excinfo:
        store.create()
    assert excinfo.value.code is ErrorCode.JOB_QUOTA_EXCEEDED

    store.update(first.job_id, status=JobStatus.SUCCEEDED, output="done")
    second = store.create()
    assert second.status is JobStatus.RUNNING


@pytest.mark.unit
def test_get_expired_job_is_visible_as_expired() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = JobStore(ttl_seconds=10, clock=lambda: now)
    job = store.create()
    now = now + timedelta(seconds=11)

    assert store.get(job.job_id).status is JobStatus.EXPIRED


@pytest.mark.unit
def test_list_can_filter_status() -> None:
    store = JobStore()
    running = store.create()
    done = store.create()
    store.update(done.job_id, status=JobStatus.SUCCEEDED)

    assert store.list(status=JobStatus.RUNNING) == [running]
    assert store.list(status=JobStatus.SUCCEEDED) == [done]
