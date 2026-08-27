"""Contract tests for the typed diagnostic job model (arthas_mcp_proxy.jobs).

``JobStatus`` enumerates the lifecycle states of a background diagnostic job and
``DiagnosticJob`` is the record handed to the caller: identity, current status,
timestamps, and accumulated output. ``is_finished`` reports whether the job has
reached a terminal state.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from arthas_mcp_proxy.jobs import DiagnosticJob, JobStatus


@pytest.mark.contract
def test_job_status_has_all_lifecycle_states() -> None:
    assert set(JobStatus) == {
        JobStatus.RUNNING,
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.EXPIRED,
    }


@pytest.mark.contract
def test_job_status_values_are_stable_strings() -> None:
    assert JobStatus.RUNNING.value == "RUNNING"
    assert JobStatus.SUCCEEDED.value == "SUCCEEDED"
    assert JobStatus.FAILED.value == "FAILED"
    assert JobStatus.CANCELLED.value == "CANCELLED"


@pytest.mark.contract
def test_diagnostic_job_carries_identity_status_and_created_at() -> None:
    created_at = datetime(2026, 8, 1, 12, 0, 0)
    job = DiagnosticJob(job_id="job-1", status=JobStatus.RUNNING, created_at=created_at)
    assert job.job_id == "job-1"
    assert job.status is JobStatus.RUNNING
    assert job.created_at == created_at


@pytest.mark.contract
def test_diagnostic_job_defaults_completed_at_and_output() -> None:
    job = DiagnosticJob(job_id="job-1", status=JobStatus.RUNNING, created_at=datetime(2026, 8, 1))
    assert job.completed_at is None
    assert job.output == ""


@pytest.mark.contract
def test_diagnostic_job_accepts_explicit_completed_at_and_output() -> None:
    completed_at = datetime(2026, 8, 1, 12, 0, 0)
    job = DiagnosticJob(
        job_id="job-1",
        status=JobStatus.SUCCEEDED,
        created_at=datetime(2026, 8, 1, 11, 0, 0),
        completed_at=completed_at,
        output="thread dump",
    )
    assert job.completed_at == completed_at
    assert job.output == "thread dump"


@pytest.mark.contract
@pytest.mark.parametrize("status", [JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED])
def test_terminal_statuses_are_finished(status: JobStatus) -> None:
    job = DiagnosticJob(job_id="job-1", status=status, created_at=datetime(2026, 8, 1))
    assert job.is_finished is True


@pytest.mark.contract
def test_running_status_is_not_finished() -> None:
    job = DiagnosticJob(job_id="job-1", status=JobStatus.RUNNING, created_at=datetime(2026, 8, 1))
    assert job.is_finished is False
