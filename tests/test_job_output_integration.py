import json
import time
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.server import get_diagnostic_job, start_diagnostic_job
from arthas_mcp_proxy.ssh_pool import get_connection_pool


def _wait(job_id: str) -> dict:
    current = json.loads(get_diagnostic_job(job_id))
    for _ in range(50):
        if current["status"] != "RUNNING":
            break
        time.sleep(0.01)
        current = json.loads(get_diagnostic_job(job_id))
    return current


@pytest.mark.contract
def test_start_job_output_is_bounded() -> None:
    session = MagicMock()
    client = MagicMock()
    client.execute_command.return_value = "x" * 20_000
    with (
        patch.object(get_connection_pool(), "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        started = json.loads(start_diagnostic_job("thread_dump", {"top_n": 100}, "session", 123))
        result = _wait(started["job_id"])
    assert result["status"] == "SUCCEEDED"
    assert len(result["output"]) <= 16_384


@pytest.mark.contract
def test_get_job_preserves_bounded_output() -> None:
    session = MagicMock()
    client = MagicMock()
    client.execute_command.return_value = "heap-output"
    with (
        patch.object(get_connection_pool(), "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        started = json.loads(start_diagnostic_job("heap_info", {}, "session", 123))
        started = _wait(started["job_id"])
        fetched = json.loads(get_diagnostic_job(started["job_id"]))
    assert fetched["output"] == started["output"]
    assert fetched["output"] == "heap-output"
