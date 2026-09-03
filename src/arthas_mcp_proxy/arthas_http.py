"""Small remote HTTP client for Arthas' ``/api`` endpoint.

The endpoint is on the SSH target, so requests are made with the target's
``curl`` through the already authenticated SSH control plane.  This keeps the
HTTP path real (rather than mocking it) and avoids exposing an Arthas port.
"""

from __future__ import annotations

import json
import shlex
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable


class RemoteExecutor(Protocol):
    def __call__(self, command: str, timeout: int = 60) -> tuple[str, str, int]: ...


@dataclass(frozen=True)
class HttpResult:
    output: str
    accepted: bool = True
    backend: str = "arthas_http"


class ArthasHttpError(Exception):
    """HTTP failure with a stable mapping hint for the MCP error contract."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


_PRE_POST_CONNECT_MARKERS = (
    "failed to connect",
    "couldn't connect",
    "connection refused",
    "connection timed out",
)


def _curl_failure_code(rc: int, stderr: str, stdout: str) -> str:
    """Map curl rc to PRE-POST ``unreachable`` vs post-accept ``protocol_error``.

    Honesty: CLI fallback is only for connect refuse / connect timeout
    (POST never left). ``--fail`` 4xx/5xx (rc 22) and post-submit
    ``--max-time`` (rc 28 without connect language) are not unreachable.
    """
    if rc in {6, 7}:
        return "unreachable"
    blob = f"{stderr} {stdout}".lower()
    if rc == 28 and any(marker in blob for marker in _PRE_POST_CONNECT_MARKERS):
        return "unreachable"
    return "protocol_error"


class ArthasHttpClient:
    """Execute one short, read-only command through Arthas HTTP."""

    def __init__(self, execute: RemoteExecutor, port: int, *, tls: bool = False) -> None:
        self._execute = execute
        self.port = port
        self.tls = tls

    def execute(self, command: str, timeout: int = 60) -> HttpResult:
        payload = json.dumps(
            {"action": "exec", "command": command, "execTimeout": int(timeout * 1000)},
            separators=(",", ":"),
        )
        scheme = "https" if self.tls else "http"
        curl = (
            f"curl --silent --show-error --fail --max-time {int(timeout)} "
            f"{'--insecure ' if self.tls else ''}"
            f"-H 'Content-Type: application/json' -d {shlex.quote(payload)} "
            f"{shlex.quote(f'{scheme}://127.0.0.1:{self.port}/api')}"
        )
        stdout, stderr, rc = self._execute(curl, timeout=timeout + 5)
        if rc != 0:
            raise ArthasHttpError(
                stderr or stdout or "Arthas HTTP request failed",
                code=_curl_failure_code(rc, stderr, stdout),
            )
        if not stdout.strip():
            raise ArthasHttpError("Arthas HTTP response body was empty", code="empty_body")
        try:
            body: object = json.loads(stdout)
            # Some Arthas releases return an escaped JSON document as the
            # response body. Decode the extra envelope before inspecting it.
            if isinstance(body, str):
                with suppress(json.JSONDecodeError):
                    body = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ArthasHttpError(
                "Arthas HTTP response was not JSON", code="protocol_error"
            ) from exc
        if isinstance(body, dict):
            state = str(body.get("state", body.get("status", ""))).upper()
            if state in {"FAILED", "ERROR", "FAILURE"} or body.get("success") is False:
                message = body.get("message") or body.get("error") or body.get("result")
                raise ArthasHttpError(
                    str(message or "Arthas HTTP command failed"), code="command_failed"
                )
            nested = body.get("body") if isinstance(body.get("body"), dict) else body
            if isinstance(nested, dict):
                for key in ("output", "result", "message"):
                    value = nested.get(key)
                    if isinstance(value, str):
                        return HttpResult(value)
                # Structured Arthas commands (for example ``thread`` and
                # ``jvm``) return their payload under ``results``. Preserve
                # that payload instead of falling back to the escaped wire text.
                if "results" in nested:
                    return HttpResult(json.dumps(body, ensure_ascii=False))
        return HttpResult(stdout)


class ArthasHttpStreamingClient:
    """Controlled HTTP long-polling backend; Arthas has no command WebSocket."""

    backend_name = "arthas_http_long_polling"

    def __init__(
        self,
        execute: RemoteExecutor,
        port: int,
        *,
        tls: bool = False,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._execute, self.port, self.tls = execute, port, tls
        self.username, self.password = username, password

    def _auth_option(self) -> str:
        if self.username is None or self.password is None:
            return ""
        return f"--user {shlex.quote(f'{self.username}:{self.password}')} "

    def _request(self, payload: dict[str, object], timeout: int) -> dict[str, object]:
        scheme = "https" if self.tls else "http"
        body = json.dumps(payload, separators=(",", ":"))
        curl = (
            f"curl --silent --show-error --fail --max-time {int(timeout)} "
            f"{'--insecure ' if self.tls else ''}{self._auth_option()}"
            "-H 'Content-Type: application/json' "
            f"-d {shlex.quote(body)} {shlex.quote(f'{scheme}://127.0.0.1:{self.port}/api')}"
        )
        stdout, stderr, rc = self._execute(curl, timeout=timeout + 5)
        if rc != 0:
            raise ArthasHttpError(
                stderr or stdout or "Arthas HTTP streaming request failed",
                code=_curl_failure_code(rc, stderr, stdout),
            )
        try:
            value = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ArthasHttpError(
                "Arthas HTTP streaming response was not JSON", code="protocol_error"
            ) from exc
        if not isinstance(value, dict):
            raise ArthasHttpError(
                "Arthas HTTP streaming response was not an object", code="protocol_error"
            )
        if str(value.get("state", "")).upper() in {"FAILED", "REFUSED", "ERROR"}:
            raise ArthasHttpError(
                str(value.get("message") or "Arthas command failed"), code="command_failed"
            )
        return value

    _TERMINAL_STATES = {"SUCCEEDED", "FAILED", "TERMINATED", "REFUSED"}
    _WATCH_TYPES = {"watch", "trace", "stack", "tt"}

    @staticmethod
    def _text(item: object) -> str | None:
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            kind = str(item.get("type", "")).lower()
            if kind in {"status", "notification"}:
                return None
            for key in ("output", "result", "message", "value"):
                if key in item and item[key] is not None:
                    value = item[key]
                    return (
                        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                    )
            if kind in ArthasHttpStreamingClient._WATCH_TYPES or any(
                key in item for key in ("params", "returnObj", "cost")
            ):
                return json.dumps(item, ensure_ascii=False)
        return None

    @classmethod
    def _is_terminal(cls, result: object, envelope_body: object) -> bool:
        if isinstance(envelope_body, dict):
            job_status = str(envelope_body.get("jobStatus", "")).upper()
            if job_status in cls._TERMINAL_STATES:
                return True
        if not isinstance(result, dict):
            return False
        state = str(result.get("state") or result.get("jobStatus") or "").upper()
        if state in cls._TERMINAL_STATES:
            return True
        return str(result.get("type", "")).lower() == "status"

    def execute_stream(
        self,
        command: str,
        emit: Callable[[str], None],
        cancel: threading.Event,
        *,
        timeout: int = 60,
        poll_timeout: int = 300,
    ) -> str:
        """Run ``async_exec`` and poll ``pull_results``; cancel via ``interrupt_job``."""
        session = self._request({"action": "init_session"}, 10)
        session_id = str(session.get("sessionId") or "")
        if not session_id:
            raise ConnectionError("Arthas did not return an HTTP sessionId")
        consumer_id = str(session.get("consumerId") or "")
        if not consumer_id:
            joined = self._request({"action": "join_session", "sessionId": session_id}, 10)
            consumer_id = str(joined.get("consumerId") or "")
        if not consumer_id:
            raise ConnectionError("Arthas did not return an HTTP consumerId")
        scheduled = self._request(
            {
                "action": "async_exec",
                "sessionId": session_id,
                "command": command,
                "execTimeout": int(timeout * 1000),
            },
            10,
        )
        body = scheduled.get("body")
        job_id = body.get("jobId") if isinstance(body, dict) else None
        output: list[str] = []
        deadline, interrupted = time.monotonic() + max(1, timeout), False
        try:
            while time.monotonic() < deadline:
                if cancel.is_set() and not interrupted:
                    self._request({"action": "interrupt_job", "sessionId": session_id}, 10)
                    interrupted = True
                response = self._request(
                    {
                        "action": "pull_results",
                        "sessionId": session_id,
                        "consumerId": consumer_id,
                    },
                    poll_timeout,
                )
                raw = response.get("body")
                results = raw.get("results", []) if isinstance(raw, dict) else []
                if not isinstance(results, list):
                    results = [results]
                terminal = False
                for result in results:
                    if (
                        isinstance(result, dict)
                        and job_id is not None
                        and result.get("jobId") not in {None, job_id, 0}
                    ):
                        continue
                    text = self._text(result)
                    if text:
                        output.append(text)
                        emit(text)
                    terminal |= self._is_terminal(result, raw)
                    status_code = result.get("statusCode") if isinstance(result, dict) else None
                    if status_code not in (None, 0, "0"):
                        raise ArthasHttpError(
                            str(result.get("message") or "Arthas command failed"),
                            code="command_failed",
                        )
                if terminal or (interrupted and results):
                    return "\n".join(output)
            if cancel.is_set():
                return "\n".join(output)
            raise TimeoutError("Arthas HTTP streaming command timed out")
        finally:
            with suppress(Exception):
                self._request({"action": "close_session", "sessionId": session_id}, 10)
