import json

import pytest

from arthas_mcp_proxy.server import get_diagnostic_job, start_diagnostic_job


@pytest.mark.contract
def test_start_job_output_is_bounded() -> None:
    result = json.loads(start_diagnostic_job("thread_dump", {"top_n": 100}))
    assert len(result["output"]) <= 1000


@pytest.mark.contract
def test_get_job_preserves_bounded_output() -> None:
    started = json.loads(start_diagnostic_job("heap_info", {}))
    fetched = json.loads(get_diagnostic_job(started["job_id"]))
    assert fetched["output"] == started["output"]
