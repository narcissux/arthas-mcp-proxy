"""Small, transport-neutral manager for controlled long-running jobs.

The backend is deliberately an injected callable.  It is not an Arthas fake: a
production caller supplies the callable that performs the real operation.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect


class ManagerJobStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


Backend = Callable[[Callable[[str], None], threading.Event], str | None]


@dataclass
class _ManagedJob:
    job_id: str
    status: ManagerJobStatus = ManagerJobStatus.RUNNING
    result: str | None = None
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event)
    subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]] = field(
        default_factory=list
    )
    future: Future[None] | None = None


class ThreadPoolJobManager:
    """Run injected backends and expose a JSON event stream over WebSocket."""

    def __init__(
        self,
        *,
        max_workers: int = 4,
        on_cancel: Callable[[str], None] | None = None,
    ) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="job")
        self._jobs: dict[str, _ManagedJob] = {}
        self._lock = threading.RLock()
        self._on_cancel = on_cancel

    def start(self, backend: Backend, *, job_id: str | None = None) -> _ManagedJob:
        job = _ManagedJob(job_id=job_id or f"job-{uuid.uuid4().hex}")
        with self._lock:
            self._jobs[job.job_id] = job
        job.future = self._executor.submit(self._run, job, backend)
        return job

    def get(self, job_id: str) -> _ManagedJob:
        with self._lock:
            return self._jobs[job_id]

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs[job_id]
            if job.status is not ManagerJobStatus.RUNNING:
                return False
            job.cancel.set()
            # This also removes a queued job before it starts.  A running
            # thread cannot be force-killed; the cooperative Event above is
            # the cancellation contract for real backends.
            if job.future is not None:
                job.future.cancel()
            self._finish_locked(job, ManagerJobStatus.CANCELLED, error="cancelled")
        if self._on_cancel is not None:
            self._on_cancel(job_id)
        return True

    def _run(self, job: _ManagedJob, backend: Backend) -> None:
        try:
            result = backend(lambda data: self._emit(job, data), job.cancel)
            with self._lock:
                if job.status is ManagerJobStatus.RUNNING:
                    self._finish_locked(job, ManagerJobStatus.SUCCEEDED, result=result)
        except Exception as exc:  # backend failures are part of the wire protocol
            with self._lock:
                if job.status is ManagerJobStatus.RUNNING:
                    self._finish_locked(job, ManagerJobStatus.FAILED, error=str(exc))

    def _emit(self, job: _ManagedJob, data: str) -> None:
        with self._lock:
            if job.status is not ManagerJobStatus.RUNNING:
                return
            event = {"type": "output", "data": data}
            job.events.append(event)
            subscribers = tuple(job.subscribers)
        for loop, queue in subscribers:
            loop.call_soon_threadsafe(queue.put_nowait, event)

    def _finish_locked(
        self,
        job: _ManagedJob,
        status: ManagerJobStatus,
        *,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        job.status, job.result, job.error = status, result, error
        event: dict[str, Any] = {"type": "terminal", "status": status.value}
        if result is not None:
            event["result"] = result
        if error is not None:
            event["error"] = error
        job.events.append(event)
        for loop, queue in tuple(job.subscribers):
            loop.call_soon_threadsafe(queue.put_nowait, event)

    async def stream(self, job_id: str) -> AsyncIterator[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        with self._lock:
            job = self._jobs[job_id]
            history = list(job.events)
            if job.status is ManagerJobStatus.RUNNING:
                job.subscribers.append((loop, queue))
        for event in history:
            yield event
        if history and history[-1]["type"] == "terminal":
            return
        try:
            while True:
                event = await queue.get()
                yield event
                if event["type"] == "terminal":
                    return
        finally:
            with self._lock:
                if (loop, queue) in job.subscribers:
                    job.subscribers.remove((loop, queue))

    def websocket_app(self) -> Starlette:
        """Mount the proxy-side job event stream (not an Arthas command channel)."""

        async def stream_socket(websocket: WebSocket) -> None:
            """Proxy job event stream: JSON output/terminal events, not Arthas WS.

            ``/jobs/{job_id}/stream`` is a local proxy transport. It is not an
            Arthas command channel, not a tunnel, and not an Arthas WebSocket.
            """
            await websocket.accept()
            job_id = websocket.path_params["job_id"]
            stream = self.stream(job_id)

            async def send_events() -> None:
                async for event in stream:
                    await websocket.send_text(json.dumps(event))

            async def detect_disconnect() -> None:
                while True:
                    await websocket.receive()

            sender = asyncio.create_task(send_events())
            receiver = asyncio.create_task(detect_disconnect())
            try:
                done, _ = await asyncio.wait(
                    (sender, receiver), return_when=asyncio.FIRST_COMPLETED
                )
                if receiver in done:
                    raise WebSocketDisconnect
                sender_exception = sender.exception()
                if sender_exception is not None:
                    raise sender_exception
            except (KeyError, WebSocketDisconnect):
                # A dropped stream is an explicit cancellation request.  Do
                # this before closing so a long-running backend receives the
                # same cancellation Event as cancel(job_id).
                with suppress(KeyError):
                    self.cancel(job_id)
                if websocket.client_state.name != "DISCONNECTED":
                    await websocket.close(code=1008)
            finally:
                for task in (sender, receiver):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(sender, receiver, return_exceptions=True)

        return Starlette(routes=[WebSocketRoute("/jobs/{job_id}/stream", stream_socket)])

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)
