import json

import pytest

from arthas_mcp_proxy.server import _job_store, get_diagnostic_job, start_diagnostic_job


def _start_job_error_code(raw: str) -> str:
    payload = json.loads(raw)
    assert payload["isError"] is True
    return str(payload["structuredContent"]["error"]["code"])


@pytest.mark.contract
def test_start_diagnostic_job_returns_succeeded_job() -> None:
    """C4-a: start without a JVM target is INVALID_ARGUMENT, not fake SUCCEEDED."""
    raw = start_diagnostic_job("thread_dump", {"top_n": 5})
    assert _start_job_error_code(raw) == "INVALID_ARGUMENT"
    assert "SUCCEEDED" not in raw


@pytest.mark.contract
def test_start_diagnostic_job_exposes_completed_at() -> None:
    """C4-a: heap_info without a target cannot fake a completed job."""
    raw = start_diagnostic_job("heap_info", {})
    assert _start_job_error_code(raw) == "INVALID_ARGUMENT"
    assert "completed_at" not in raw or json.loads(raw).get("status") != "SUCCEEDED"


@pytest.mark.contract
def test_start_diagnostic_job_rejects_unknown_command() -> None:
    """Missing target is rejected first even for an unknown command (C4-a)."""
    raw = start_diagnostic_job("unknown", {})
    assert _start_job_error_code(raw) == "INVALID_ARGUMENT"


@pytest.mark.contract
def test_start_diagnostic_job_invalid_command_leaves_no_orphan_job() -> None:
    """A rejected start must not persist a RUNNING job (C4-a)."""
    store = _job_store
    before = {job.job_id for job in store._jobs.values()}
    result = start_diagnostic_job("unknown", {})
    assert _start_job_error_code(result) == "INVALID_ARGUMENT"
    after = {job.job_id for job in store._jobs.values()}
    assert after == before


@pytest.mark.contract
def test_start_diagnostic_job_valid_command_without_target_leaves_no_orphan() -> None:
    """C4-a: thread_dump without a target must not create a job."""
    before = {job.job_id for job in _job_store._jobs.values()}
    result = start_diagnostic_job("thread_dump", {})
    assert _start_job_error_code(result) == "INVALID_ARGUMENT"
    after = {job.job_id for job in _job_store._jobs.values()}
    assert after == before


@pytest.mark.contract
def test_start_diagnostic_job_unknown_with_target_leaves_no_orphan() -> None:
    before = {job.job_id for job in _job_store._jobs.values()}
    result = start_diagnostic_job("unknown", {}, "session", 123)
    assert result.startswith("Error:") or (json.loads(result).get("isError") is True)
    after = {job.job_id for job in _job_store._jobs.values()}
    assert after == before
    running = [job for job in _job_store._jobs.values() if job.job_id not in before]
    assert all(job.status.value != "RUNNING" for job in running)


@pytest.mark.contract
def test_start_diagnostic_job_output_is_retrievable() -> None:
    """C4-a: no-target start is not a retrievable SUCCEEDED render."""
    raw = start_diagnostic_job("thread_dump", {"top_n": 5})
    assert _start_job_error_code(raw) == "INVALID_ARGUMENT"


@pytest.mark.contract
def test_real_job_path_runs_in_background_and_can_be_queried() -> None:
    from unittest.mock import MagicMock, patch

    from arthas_mcp_proxy.ssh_pool import get_connection_pool

    session = MagicMock()
    client = MagicMock()
    client.execute_command.return_value = "real diagnostic output"
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        payload = json.loads(start_diagnostic_job("thread_dump", {"top_n": 5}, "session", 123))
        import time

        current = json.loads(get_diagnostic_job(payload["job_id"]))
        for _ in range(50):
            current = json.loads(get_diagnostic_job(payload["job_id"]))
            if current["status"] != "RUNNING":
                break
            time.sleep(0.01)

    assert current["status"] == "SUCCEEDED"
    assert "real diagnostic output" in current["output"]


@pytest.mark.contract
def test_real_job_output_is_paged_from_complete_output() -> None:
    from unittest.mock import MagicMock, patch

    from arthas_mcp_proxy.ssh_pool import get_connection_pool

    session = MagicMock()
    client = MagicMock()
    client.execute_command.return_value = "0123456789" * 3
    with (
        patch.object(get_connection_pool(), "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        payload = json.loads(start_diagnostic_job("thread_dump", {}, "session", 123))
        import time

        for _ in range(50):
            current = json.loads(get_diagnostic_job(payload["job_id"], max_chars=7))
            if current["status"] != "RUNNING":
                break
            time.sleep(0.01)
        first = current
        second = json.loads(
            get_diagnostic_job(payload["job_id"], cursor=first["next_cursor"], max_chars=7)
        )

    assert first["output"] == "0123456"
    assert second["output"] == "7890123"
    assert first["next_cursor"]
    assert first["next_cursor"] != "7"


@pytest.mark.contract
def test_failed_job_exposes_structured_error() -> None:
    from unittest.mock import patch

    from arthas_mcp_proxy.ssh_pool import get_connection_pool

    with patch.object(get_connection_pool(), "get_session", return_value=None):
        payload = json.loads(start_diagnostic_job("thread_dump", {}, "missing", 123))
        import time

        for _ in range(50):
            payload = json.loads(get_diagnostic_job(payload["job_id"]))
            if payload["status"] != "RUNNING":
                break
            time.sleep(0.01)

    assert payload["status"] == "FAILED"
    assert payload["error"]["code"] == "SESSION_NOT_FOUND"
    assert payload["error"]["message"] == "Session not found or expired"


@pytest.mark.unit
def test_running_job_cancel_stays_cancelled_and_cleans_event() -> None:
    import threading
    import time
    from unittest.mock import MagicMock, patch

    from arthas_mcp_proxy.server import _job_cancel_events, cancel_diagnostic_job
    from arthas_mcp_proxy.ssh_pool import get_connection_pool

    started = threading.Event()
    release = threading.Event()

    def run_command(*args: object, **kwargs: object) -> str:
        started.set()
        release.wait(2)
        return "late result"

    with (
        patch.object(get_connection_pool(), "get_session", return_value=MagicMock()),
        patch("arthas_mcp_proxy.server.typed_command_json", side_effect=run_command),
    ):
        payload = json.loads(start_diagnostic_job("thread_dump", {}, "session", 123))
        assert started.wait(1)
        cancelled = json.loads(cancel_diagnostic_job(payload["job_id"]))
        assert cancelled["status"] == "CANCELLED"
        release.set()
        time.sleep(0.05)

    assert payload["job_id"] not in _job_cancel_events
    assert json.loads(get_diagnostic_job(payload["job_id"]))["status"] == "CANCELLED"


@pytest.mark.unit
def test_job_timeout_marks_terminal_and_cleans_event() -> None:
    import time
    from unittest.mock import MagicMock, patch

    from arthas_mcp_proxy.server import _job_cancel_events
    from arthas_mcp_proxy.ssh_pool import get_connection_pool

    with (
        patch.object(get_connection_pool(), "get_session", return_value=MagicMock()),
        patch(
            "arthas_mcp_proxy.server.typed_command_json",
            side_effect=lambda *a, **k: time.sleep(0.1),
        ),
    ):
        payload = json.loads(start_diagnostic_job("thread_dump", {}, "session", 123, timeout=0))
        for _ in range(30):
            payload = json.loads(get_diagnostic_job(payload["job_id"]))
            if payload["status"] != "RUNNING":
                break
            time.sleep(0.01)

    assert payload["status"] == "FAILED"
    assert payload["error"]["code"] == "COMMAND_TIMEOUT"
    assert payload["job_id"] not in _job_cancel_events
