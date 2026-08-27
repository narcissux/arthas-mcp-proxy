import pytest

from arthas_mcp_proxy.errors import DomainError
from arthas_mcp_proxy.job_store import JobStore
from arthas_mcp_proxy.jobs import JobStatus
from arthas_mcp_proxy.models import ErrorCode


@pytest.mark.contract
def test_create_get_update_delete_job() -> None:
    store = JobStore()
    job = store.create()
    assert job.status is JobStatus.RUNNING
    assert store.get(job.job_id) is job
    updated = store.update(job.job_id, status=JobStatus.SUCCEEDED, output="done")
    assert updated.status is JobStatus.SUCCEEDED
    assert updated.output == "done"
    assert updated.completed_at is not None
    assert store.delete(job.job_id) is True
    with pytest.raises(DomainError):
        store.get(job.job_id)


@pytest.mark.contract
def test_create_job_ids_are_unique() -> None:
    store = JobStore()
    assert store.create().job_id != store.create().job_id


@pytest.mark.contract
def test_cancel_job_marks_cancelled_and_completed() -> None:
    store = JobStore()
    job = store.create()
    assert job.completed_at is None
    cancelled = store.cancel(job.job_id)
    assert cancelled is job
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.completed_at is not None


@pytest.mark.contract
def test_cancel_already_finished_job_raises_domain_error() -> None:
    store = JobStore()
    job = store.create()
    store.update(job.job_id, status=JobStatus.SUCCEEDED, output="done")
    with pytest.raises(DomainError) as excinfo:
        store.cancel(job.job_id)
    assert excinfo.value.code is ErrorCode.JOB_ALREADY_FINISHED
