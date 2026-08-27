import json

import pytest

from arthas_mcp_proxy.server import _job_store, get_diagnostic_job, start_diagnostic_job


@pytest.mark.contract
def test_start_diagnostic_job_returns_succeeded_job() -> None:
    payload = json.loads(start_diagnostic_job("thread_dump", {"top_n": 5}))
    assert payload["job_id"]
    assert payload["status"] == "SUCCEEDED"
    assert payload["completed_at"] is not None
    assert "thread" in payload["output"]
    assert "5" in payload["output"]


@pytest.mark.contract
def test_start_diagnostic_job_exposes_completed_at() -> None:
    payload = json.loads(start_diagnostic_job("heap_info", {}))
    assert payload["status"] == "SUCCEEDED"
    assert payload["completed_at"] is not None
    retrieved = json.loads(get_diagnostic_job(payload["job_id"]))
    assert retrieved["completed_at"] is not None


@pytest.mark.contract
def test_start_diagnostic_job_rejects_unknown_command() -> None:
    assert start_diagnostic_job("unknown", {}).startswith("Error:")


@pytest.mark.contract
def test_start_diagnostic_job_invalid_command_leaves_no_orphan_job() -> None:
    """A rejected command must not persist a RUNNING job (C1)."""
    store = _job_store
    before = {job.job_id for job in store._jobs.values()}
    result = start_diagnostic_job("unknown", {})
    assert result.startswith("Error:")
    after = {job.job_id for job in store._jobs.values()}
    assert after == before


@pytest.mark.contract
def test_start_diagnostic_job_output_is_retrievable() -> None:
    payload = json.loads(start_diagnostic_job("thread_dump", {"top_n": 5}))
    retrieved = json.loads(get_diagnostic_job(payload["job_id"]))
    assert retrieved["output"] == "thread -n 5"
    assert retrieved["next_cursor"] is None


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
