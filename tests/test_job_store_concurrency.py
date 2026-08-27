from __future__ import annotations

import threading

import pytest

from arthas_mcp_proxy.errors import DomainError
from arthas_mcp_proxy.job_store import JobStore
from arthas_mcp_proxy.jobs import JobStatus
from arthas_mcp_proxy.models import ErrorCode


@pytest.mark.unit
def test_cancel_and_update_cannot_overwrite_terminal_state() -> None:
    store = JobStore()
    job = store.create()
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def cancel() -> None:
        try:
            barrier.wait()
            store.cancel(job.job_id)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def update() -> None:
        try:
            barrier.wait()
            store.update(job.job_id, status=JobStatus.SUCCEEDED, output="done")
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=cancel), threading.Thread(target=update)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors or all(
        isinstance(error, DomainError) and error.code is ErrorCode.JOB_ALREADY_FINISHED
        for error in errors
    )
    assert store.get(job.job_id).status in {JobStatus.CANCELLED, JobStatus.SUCCEEDED}
    assert store.get(job.job_id).completed_at is not None


@pytest.mark.unit
def test_terminal_job_rejects_later_update() -> None:
    store = JobStore()
    job = store.create()
    store.cancel(job.job_id)

    with pytest.raises(DomainError) as excinfo:
        store.update(job.job_id, status=JobStatus.SUCCEEDED, output="overwrite")

    assert excinfo.value.code is ErrorCode.JOB_ALREADY_FINISHED
    current = store.get(job.job_id)
    assert current.status is JobStatus.CANCELLED
    assert current.output == ""
