"""Contract tests for structured JSON serialization of diagnostic jobs.

``serialize_job`` converts a :class:`DiagnosticJob` into a JSON string with a
stable, client-friendly shape: the status as a plain string, ISO-8601
timestamps, the job identity, and the accumulated output. This is the single
serialization path used by the job MCP tools so callers always see the same
structure.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from arthas_mcp_proxy.job_serialization import serialize_job
from arthas_mcp_proxy.jobs import DiagnosticJob, JobStatus


@pytest.mark.contract
def test_serialize_job_returns_json_with_status_string_and_identity() -> None:
    job = DiagnosticJob(
        job_id="job-1",
        status=JobStatus.SUCCEEDED,
        created_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 8, 1, 12, 0, 5, tzinfo=timezone.utc),
        output="thread -n 5",
    )
    payload = json.loads(serialize_job(job))
    assert payload["job_id"] == "job-1"
    assert payload["status"] == "SUCCEEDED"
    assert payload["output"] == "thread -n 5"


@pytest.mark.contract
def test_serialize_job_exposes_iso_timestamps() -> None:
    created_at = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    completed_at = datetime(2026, 8, 1, 12, 0, 5, tzinfo=timezone.utc)
    job = DiagnosticJob(
        job_id="job-1",
        status=JobStatus.FAILED,
        created_at=created_at,
        completed_at=completed_at,
        output="boom",
    )
    payload = json.loads(serialize_job(job))
    assert payload["created_at"] == created_at.isoformat()
    assert payload["completed_at"] == completed_at.isoformat()


@pytest.mark.contract
def test_serialize_job_completed_at_is_null_while_running() -> None:
    job = DiagnosticJob(
        job_id="job-1",
        status=JobStatus.RUNNING,
        created_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    payload = json.loads(serialize_job(job))
    assert payload["completed_at"] is None
    assert payload["output"] == ""


@pytest.mark.contract
def test_serialize_job_status_is_string_not_enum_repr() -> None:
    job = DiagnosticJob(
        job_id="job-1",
        status=JobStatus.CANCELLED,
        created_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    payload = json.loads(serialize_job(job))
    assert isinstance(payload["status"], str)
    assert payload["status"] == JobStatus.CANCELLED.value
    assert "JobStatus" not in serialize_job(job)


@pytest.mark.contract
def test_serialize_job_shape_is_stable() -> None:
    job = DiagnosticJob(
        job_id="job-1",
        status=JobStatus.SUCCEEDED,
        created_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 8, 1, 12, 0, 5, tzinfo=timezone.utc),
        output="thread -n 5",
    )
    payload = json.loads(serialize_job(job))
    assert set(payload) == {"job_id", "status", "created_at", "completed_at", "output"}
