from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from arthas_mcp_proxy.job_manager import ThreadPoolJobManager

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.mark.unit
def test_thread_pool_manager_streams_real_backend_output_and_terminal_event() -> None:
    manager = ThreadPoolJobManager(max_workers=1)
    started = threading.Event()

    def backend(emit: Callable[[str], None], cancel: threading.Event) -> str:
        started.set()
        emit("one")
        emit("two")
        return "done"

    job = manager.start(backend)
    assert started.wait(1)

    async def collect() -> list[dict[str, object]]:
        return [event async for event in manager.stream(job.job_id)]

    events = asyncio.run(collect())
    assert [event["type"] for event in events] == ["output", "output", "terminal"]
    assert events[-1]["status"] == "SUCCEEDED"
    manager.shutdown()


@pytest.mark.unit
def test_streaming_websocket_is_a_real_json_protocol() -> None:
    manager = ThreadPoolJobManager(max_workers=1)

    def backend(emit: Callable[[str], None], cancel: threading.Event) -> str:
        emit("hello")
        return "world"

    job = manager.start(backend)
    app = manager.websocket_app()
    with (
        TestClient(app) as client,
        client.websocket_connect(f"/jobs/{job.job_id}/stream") as socket,
    ):
        events = [json.loads(socket.receive_text()) for _ in range(2)]
    assert events[0] == {"type": "output", "data": "hello"}
    assert events[-1]["type"] == "terminal"
    assert events[-1]["status"] == "SUCCEEDED"
    manager.shutdown()


@pytest.mark.unit
def test_cancel_sets_cancel_event_and_terminal_state() -> None:
    manager = ThreadPoolJobManager(max_workers=1)
    release = threading.Event()

    def backend(emit: Callable[[str], None], cancel: threading.Event) -> str:
        while not cancel.is_set():
            release.wait(0.01)
        return "cancelled cooperatively"

    job = manager.start(backend)
    assert manager.cancel(job.job_id) is True
    assert manager.get(job.job_id).status.value == "CANCELLED"
    release.set()
    manager.shutdown()


@pytest.mark.unit
def test_websocket_disconnect_cancels_long_running_backend() -> None:
    manager = ThreadPoolJobManager(max_workers=1)
    started = threading.Event()
    cancelled = threading.Event()

    def backend(emit: Callable[[str], None], cancel: threading.Event) -> str:
        started.set()
        while not cancel.wait(0.01):
            pass
        cancelled.set()
        return "late result must not win"

    job = manager.start(backend)
    app = manager.websocket_app()
    with (
        TestClient(app) as client,
        client.websocket_connect(f"/jobs/{job.job_id}/stream") as socket,
    ):
        assert started.wait(1)
        socket.close()
        assert cancelled.wait(1)

    assert manager.get(job.job_id).status.value == "CANCELLED"
    assert manager.get(job.job_id).result is None
    manager.shutdown()
