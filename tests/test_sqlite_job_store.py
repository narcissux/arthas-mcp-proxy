import json
from pathlib import Path

import pytest

from arthas_mcp_proxy.job_serialization import serialize_job
from arthas_mcp_proxy.job_store import SQLiteJobStore
from arthas_mcp_proxy.jobs import JobStatus


@pytest.mark.contract
def test_sqlite_store_preserves_completed_job_across_instances(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    first = SQLiteJobStore(database)
    job = first.create()
    first.update(job.job_id, status=JobStatus.SUCCEEDED, output="persisted", error=None)

    second = SQLiteJobStore(database)
    restored = second.get(job.job_id)
    assert restored.status is JobStatus.SUCCEEDED
    assert restored.output == "persisted"
    assert restored.completed_at is not None


@pytest.mark.contract
def test_sqlite_store_marks_running_jobs_failed_on_startup(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    first = SQLiteJobStore(database)
    job = first.create()

    second = SQLiteJobStore(database)
    restored = second.get(job.job_id)
    assert restored.status is JobStatus.FAILED
    assert restored.error == {
        "code": "JOB_RESTARTED",
        "message": "Job was running when proxy restarted",
        "phase": "startup",
        "retryable": False,
        "suggestion": "Start a new diagnostic job",
    }
    assert restored.completed_at is not None


@pytest.mark.contract
def test_sqlite_store_uses_same_job_json_contract(tmp_path: Path) -> None:
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    job = store.create()
    payload = json.loads(serialize_job(job))
    assert set(payload) == {"job_id", "status", "created_at", "completed_at", "output"}


@pytest.mark.unit
def test_job_store_path_is_optional_and_memory_store_remains_default() -> None:
    from arthas_mcp_proxy.job_store import JobStore

    assert isinstance(JobStore(), JobStore)
    assert not hasattr(JobStore(), "_database")
