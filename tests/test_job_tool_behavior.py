import json

import pytest

from arthas_mcp_proxy.jobs import JobStatus
from arthas_mcp_proxy.server import _job_store, cancel_diagnostic_job, get_diagnostic_job


@pytest.mark.contract
def test_get_diagnostic_job_returns_json_for_existing_job() -> None:
    job = _job_store.create()
    payload = json.loads(get_diagnostic_job(job.job_id))
    assert payload["job_id"] == job.job_id
    assert payload["status"] == "RUNNING"


@pytest.mark.contract
def test_cancel_diagnostic_job_returns_cancelled_json() -> None:
    job = _job_store.create()
    payload = json.loads(cancel_diagnostic_job(job.job_id))
    assert payload["job_id"] == job.job_id
    assert payload["status"] == JobStatus.CANCELLED.value


@pytest.mark.contract
def test_job_tool_unknown_job_is_error() -> None:
    assert get_diagnostic_job("missing-job").startswith("Error:")
