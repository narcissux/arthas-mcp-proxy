#!/usr/bin/env python3
"""Arthas MCP Proxy Server.

Provides MCP tools for JVM diagnostics via SSH + Arthas.
Supports both SSE and stdio transport modes.

Usage:
    # SSE mode (default)
    arthas-mcp-proxy --transport sse --host 0.0.0.0 --port 8000

    # stdio mode
    arthas-mcp-proxy --transport stdio

    # Or via Python module
    python -m arthas_mcp_proxy --transport sse
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import secrets
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from arthas_mcp_proxy.ssh_pool import SSHSession

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.routing import Route

from arthas_mcp_proxy.application_resolver import find_java_application as resolve_java_application
from arthas_mcp_proxy.application_resolver import identity_complete
from arthas_mcp_proxy.arthas_client import ArthasClient, _exec_ssh
from arthas_mcp_proxy.command_catalog import COMMANDS, build_command
from arthas_mcp_proxy.cookbook import COOKBOOK
from arthas_mcp_proxy.decorators import require_session, set_fallback_credential_getter
from arthas_mcp_proxy.errors import DomainError, map_exception
from arthas_mcp_proxy.health import health_payload
from arthas_mcp_proxy.job_manager import ThreadPoolJobManager
from arthas_mcp_proxy.job_serialization import serialize_job
from arthas_mcp_proxy.job_store import JobStore, SQLiteJobStore
from arthas_mcp_proxy.jobs import JobStatus
from arthas_mcp_proxy.models import ErrorCode, ErrorDetail, ResultMeta, ToolResult
from arthas_mcp_proxy.observation_policy import ObservationPolicy
from arthas_mcp_proxy.output_limit import limit_output, paginate_output
from arthas_mcp_proxy.process_inventory import (
    collect_inventory_over_ssh,
    process_record_to_dict,
)
from arthas_mcp_proxy.result_adapter import to_mcp_result
from arthas_mcp_proxy.ssh_pool import get_connection_pool
from arthas_mcp_proxy.target_state import TargetIdentity
from arthas_mcp_proxy.typed_tool import typed_command_json


class _BearerAuthMiddleware:
    """Require one exact bearer token without exposing it in responses."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        expected = f"Bearer {self.token}".encode("ascii")
        if not secrets.compare_digest(headers.get(b"authorization", b""), expected):
            response = JSONResponse({"error": "Unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def validate_transport_security(host: str, token: str | None) -> None:
    """Reject network exposure when no authentication is configured."""
    loopback_hosts = {"127.0.0.1", "localhost", "::1", "[::1]"}
    if not token and host not in loopback_hosts:
        raise ValueError("HTTP transport without auth must bind to loopback")


def build_auth_middleware(app: Any, token: str | None) -> Any:
    """Wrap an HTTP app when a token is configured."""
    return _BearerAuthMiddleware(app, token) if token else app


# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)


# ─── Global state ────────────────────────────────────────────────────────────
_transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["localhost", "127.0.0.1", "[::1]"],
    allowed_origins=["http://localhost", "http://127.0.0.1"],
)
mcp = FastMCP("arthas-mcp-proxy", transport_security=_transport_security)


def _cookbook_prompt(name: str) -> str:
    entry = COOKBOOK[name]
    return f"{entry.title}: " + "; ".join(entry.steps)


@mcp.prompt()
def high_cpu() -> str:
    """Guide for diagnosing high CPU usage."""
    return _cookbook_prompt("high_cpu")


@mcp.prompt()
def memory() -> str:
    """Guide for diagnosing memory pressure."""
    return _cookbook_prompt("memory")


@mcp.prompt()
def deadlock() -> str:
    """Guide for diagnosing deadlocks."""
    return _cookbook_prompt("deadlock")


@mcp.prompt()
def slow_method() -> str:
    """Guide for diagnosing slow methods."""
    return _cookbook_prompt("slow_method")


# Session credential cache (for fallback reconnection)
_session_store: dict[str, dict[str, str | int]] = {}
_store_lock = threading.Lock()
_job_store = (
    SQLiteJobStore(os.environ["ARTHAS_JOB_STORE_SQLITE"])
    if os.environ.get("ARTHAS_JOB_STORE_SQLITE")
    else JobStore()
)
_watch_policy = ObservationPolicy()
_JOB_OUTPUT_MAX_CHARS = 16_384
_job_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="diagnostic-job")
_job_cancel_events: dict[str, threading.Event] = {}
_job_timeout_timers: dict[str, threading.Timer] = {}
_job_cancel_lock = threading.Lock()
_EXPERT_COMMANDS = {"dashboard", "jvm", "sysprop", "sysenv", "memory", "thread", "version"}


def _propagate_manager_cancel(job_id: str) -> None:
    """Bridge transport cancellation into the public diagnostic job store."""
    with _job_cancel_lock:
        event = _job_cancel_events.get(job_id)
        if event is not None:
            event.set()
    with suppress(DomainError):
        _job_store.cancel(job_id)


_job_manager = ThreadPoolJobManager(max_workers=4, on_cancel=_propagate_manager_cancel)


def _validate_expert_command(command: str) -> None:
    """Allow only explicitly read-only expert commands through the fallback tool."""
    command = command.strip()
    first = re.match(r"^[A-Za-z]+(?=\s|$)", command)
    if (
        any(char in command for char in ";\n\r|&$`()<>")
        or first is None
        or first.group(0) not in _EXPERT_COMMANDS
    ):
        raise DomainError(
            ErrorCode.COMMAND_NOT_ALLOWED,
            "Command is not allowed through exec_command; use a typed diagnostic tool.",
            suggestion="Use execute_diagnostic_command for supported diagnostics.",
        )


def _store_session(session_id: str, host: str, port: int, username: str) -> None:
    with _store_lock:
        _session_store[session_id] = {
            "host": host,
            "port": port,
            "username": username,
        }


def _get_session_credentials(session_id: str) -> dict[str, str | int] | None:
    with _store_lock:
        return _session_store.get(session_id)


# Register fallback credential getter with the decorator module
set_fallback_credential_getter(_get_session_credentials)


def _coerce_params(kwargs: dict[str, object]) -> dict[str, object]:
    """Coerce parameter types to handle clients that send strings instead of ints."""
    coerced = dict(kwargs)
    int_fields = ["port", "pid", "top_n", "times", "timeout"]
    bool_fields = ["watch_params", "watch_return"]

    for field in int_fields:
        if field in coerced and coerced[field] is not None:
            val = coerced[field]
            if isinstance(val, (int, str, float)):
                try:
                    coerced[field] = int(val)
                except (ValueError, TypeError):
                    logger.warning("Cannot coerce %s=%s to int", field, val)

    for field in bool_fields:
        if field in coerced and coerced[field] is not None:
            val = coerced[field]
            if isinstance(val, str):
                coerced[field] = val.lower() in ("true", "1", "yes", "on")

    return coerced


def _dump_params(tool_name: str, kwargs: dict[str, object]) -> None:
    """Log incoming parameters for debugging."""
    secret_names = {"password", "key_string", "key_path", "private_key", "token", "secret"}
    safe = {}
    for key, value in kwargs.items():
        if key.lower() in secret_names:
            safe[key] = "[REDACTED]"
        elif isinstance(value, str) and len(value) > 50:
            safe[key] = f"{value[:20]}..."
        else:
            safe[key] = cast("str", value)
    logger.info("[PARAMS] %s: %s", tool_name, safe)


def _structured_error(code: ErrorCode, message: str, *, suggestion: str | None = None) -> str:
    result = ToolResult(
        status="error",
        summary=message,
        error=ErrorDetail(code=code, message=message, suggestion=suggestion),
        meta=ResultMeta(request_id=f"req-{uuid.uuid4().hex}", duration_ms=0),
    )
    return json.dumps(to_mcp_result(result))


# ─── MCP Tools ───────────────────────────────────────────────────────────────


@mcp.tool()
def start_diagnostic_job(
    command: str,
    params: dict[str, Any] | None = None,
    session_id: str | None = None,
    pid: int | None = None,
    timeout: int = 60,
) -> str:
    """Start a catalog-backed diagnostic job."""
    try:
        if (session_id is None) != (pid is None):
            raise ValueError("session_id and pid must be supplied together")
        rendered = None if session_id is not None else build_command(command, params or {})
        job = _job_store.create()
        if session_id is None:
            assert rendered is not None
            limited = limit_output(rendered, _JOB_OUTPUT_MAX_CHARS)
            job = _job_store.update(job.job_id, status=JobStatus.SUCCEEDED, output=limited.text)
        else:
            cancel_event = threading.Event()
            timeout_timer = threading.Timer(
                max(0, int(timeout)), _timeout_diagnostic_job, args=(job.job_id, cancel_event)
            )
            with _job_cancel_lock:
                _job_cancel_events[job.job_id] = cancel_event
                _job_timeout_timers[job.job_id] = timeout_timer
            _job_manager.start(
                lambda emit, manager_cancel: _managed_diagnostic_backend(
                    job.job_id,
                    session_id,
                    int(pid or 0),
                    command,
                    params or {},
                    int(timeout),
                    cancel_event,
                    manager_cancel,
                    emit,
                ),
                job_id=job.job_id,
            )
            timeout_timer.start()
        return serialize_job(job)
    except Exception as exc:
        return f"Error: {exc}"


def _managed_diagnostic_backend(
    job_id: str,
    session_id: str,
    pid: int,
    command: str,
    params: dict[str, Any],
    timeout: int,
    cancel_event: threading.Event,
    manager_cancel: threading.Event,
    emit: Any,
) -> str:
    """Execute the typed MCP command and bridge its bounded result to streaming."""
    try:
        if cancel_event.is_set() or manager_cancel.is_set():
            return ""
        session = get_connection_pool().get_session(session_id)
        if session is None:
            raise DomainError(ErrorCode.SESSION_NOT_FOUND, "Session not found or expired")
        client = ArthasClient(session)
        rendered = build_command(command, params)
        if COMMANDS[command].streaming:
            chunks: list[str] = []

            def forward_chunk(chunk: str) -> None:
                chunks.append(chunk)
                emit(chunk)

            result_text = client.execute_streaming_command(
                pid, rendered, forward_chunk, manager_cancel, timeout
            )
            if cancel_event.is_set() or manager_cancel.is_set():
                return ""
            result_text = result_text or "\n".join(chunks)
        else:
            output = typed_command_json(
                client, pid=pid, command=command, params=params, timeout=timeout
            )
            payload = json.loads(output)
            structured = payload.get("structuredContent", {})
            if payload.get("isError"):
                error = structured.get("error") or {
                    "code": ErrorCode.ARTHAS_COMMAND_FAILED.value,
                    "message": structured.get("summary", "Diagnostic command failed"),
                }
                raise DomainError(
                    ErrorCode(error["code"]), str(error.get("message", "Diagnostic command failed"))
                )
            result_text = str(structured.get("data", {}).get("output", output))
        result = limit_output(result_text, _JOB_OUTPUT_MAX_CHARS)
        _job_store.update(job_id, status=JobStatus.SUCCEEDED, output=result.text)
        emit(result.text)
        return result.text
    except Exception as exc:
        error = map_exception(exc)
        with suppress(DomainError):
            _job_store.update(
                job_id,
                status=JobStatus.FAILED,
                output=error.message,
                error=error.model_dump(mode="json"),
            )

        raise
    finally:
        with _job_cancel_lock:
            _job_cancel_events.pop(job_id, None)
            timer = _job_timeout_timers.pop(job_id, None)
        if timer is not None:
            timer.cancel()


def _run_diagnostic_job(
    job_id: str,
    session_id: str,
    pid: int,
    command: str,
    params: dict[str, Any],
    timeout: int,
    cancel_event: threading.Event,
) -> None:
    try:
        if cancel_event.is_set():
            return
        session = get_connection_pool().get_session(session_id)
        if session is None:
            raise DomainError(ErrorCode.SESSION_NOT_FOUND, "Session not found or expired")
        output = typed_command_json(
            ArthasClient(session), pid=pid, command=command, params=params, timeout=timeout
        )
        if cancel_event.is_set():
            return
        result_payload = json.loads(output)
        structured = result_payload.get("structuredContent", {})
        if result_payload.get("isError"):
            error = structured.get("error") or {
                "code": ErrorCode.ARTHAS_COMMAND_FAILED.value,
                "message": structured.get("summary", "Diagnostic command failed"),
            }
            raise DomainError(
                ErrorCode(error["code"]),
                str(error.get("message", "Diagnostic command failed")),
            )
        job_output = structured.get("data", {}).get("output", output)
        limited = limit_output(str(job_output), _JOB_OUTPUT_MAX_CHARS)
        _job_store.update(job_id, status=JobStatus.SUCCEEDED, output=limited.text)
    except Exception as exc:
        logger.error("diagnostic job %s failed: %s", job_id, exc)
        try:
            error = map_exception(exc)
            _job_store.update(
                job_id,
                status=JobStatus.FAILED,
                output=error.message,
                error=error.model_dump(mode="json"),
            )
        except DomainError:
            logger.info("diagnostic job %s was already terminal", job_id)
    finally:
        with _job_cancel_lock:
            _job_cancel_events.pop(job_id, None)
            timer = _job_timeout_timers.pop(job_id, None)
        if timer is not None:
            timer.cancel()


def _timeout_diagnostic_job(job_id: str, cancel_event: threading.Event) -> None:
    cancel_event.set()
    try:
        error = map_exception(TimeoutError("diagnostic job timed out"))
        _job_store.update(
            job_id,
            status=JobStatus.FAILED,
            output=error.message,
            error=error.model_dump(mode="json"),
        )
    except DomainError:
        logger.info("diagnostic job %s completed before timeout", job_id)
    finally:
        with _job_cancel_lock:
            _job_timeout_timers.pop(job_id, None)
            _job_cancel_events.pop(job_id, None)


@mcp.tool()
def get_diagnostic_job(job_id: str, cursor: str | None = None, max_chars: int = 16_384) -> str:
    """Get job state and a bounded output page."""
    try:
        job = _job_store.get(job_id)
        payload = json.loads(serialize_job(job))
        page = paginate_output(job.output, cursor, max_chars, job_id=job.job_id)
        if job.error is not None:
            payload["error"] = job.error
        payload["output"] = page.text
        payload["next_cursor"] = page.next_cursor
        return json.dumps(payload)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def list_diagnostic_jobs(status: str | None = None, limit: int = 50) -> str:
    """List recent diagnostic jobs, optionally filtered by lifecycle status."""
    try:
        parsed_status = JobStatus(status.upper()) if status else None
        jobs = _job_store.list(status=parsed_status, limit=int(limit))
        return json.dumps({"jobs": [json.loads(serialize_job(job)) for job in jobs]})
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def cancel_diagnostic_job(job_id: str) -> str:
    """Cancel a running diagnostic job."""
    try:
        _propagate_manager_cancel(job_id)
        with suppress(KeyError):
            _job_manager.cancel(job_id)
        with suppress(DomainError):
            _job_store.cancel(job_id)
        return serialize_job(_job_store.get(job_id))
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
@require_session()
def execute_diagnostic_command(
    session: object,
    pid: int,
    command: str,
    params: dict[str, Any] | None = None,
    timeout: int = 60,
) -> str:
    """Execute a catalog-backed diagnostic command on a target JVM."""
    try:
        return typed_command_json(
            ArthasClient(cast("SSHSession", session)),
            pid=pid,
            command=command,
            params=params or {},
            timeout=timeout,
        )
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def connect_ssh(
    host: str,
    username: str = "root",
    port: int = 22,
    password: str | None = None,
    key_path: str | None = None,
    key_string: str | None = None,
) -> str:
    """Establish an SSH connection to a remote server."""
    if isinstance(port, str):
        port = int(port)

    try:
        pool = get_connection_pool()
        session_id = pool.connect(
            host=host,
            port=port,
            username=username,
            password=password,
            key_path=key_path,
            key_string=key_string,
        )
        _store_session(session_id, host, port, username)
        return f"SSH connection established. Session ID: {session_id}"
    except Exception as e:
        logger.error("SSH connection failed to %s:%d: %s", host, port, e)
        return _structured_error(map_exception(e).code, f"SSH connection failed: {e}")


@mcp.tool()
def list_java_processes(session_id: str) -> str:
    """List all Java processes on the remote server."""
    pool = get_connection_pool()
    session = pool.get_session(session_id)

    if not session:
        creds = _get_session_credentials(session_id)
        if creds:
            session = pool.get_session_by_host(
                str(creds["host"]), int(creds["port"]), str(creds["username"])
            )
        if not session:
            return _structured_error(
                ErrorCode.SESSION_NOT_FOUND,
                "Session not found or expired",
                suggestion="Reconnect using connect_ssh.",
            )

    try:
        records = collect_inventory_over_ssh(lambda command: _exec_ssh(session, command))
        processes = [process_record_to_dict(record, session) for record in records]
        count = len(processes)
        summary = (
            "No Java processes found."
            if count == 0
            else f"Found {count} Java process{'es' if count != 1 else ''}."
        )
        return json.dumps(
            to_mcp_result(
                ToolResult(
                    status="success",
                    summary=summary,
                    data={"processes": processes},
                    meta=ResultMeta(request_id=f"req-{uuid.uuid4().hex}", duration_ms=0),
                )
            )
        )
    except DomainError as exc:
        logger.error("list_java_processes failed: %s", exc)
        return _structured_error(exc.code, exc.message, suggestion=exc.suggestion)
    except Exception as e:
        logger.error("list_java_processes failed: %s", e)
        return _structured_error(ErrorCode.INTERNAL_ERROR, f"Error listing Java processes: {e}")


@mcp.tool()
def find_java_application(session_id: str, application_name: str) -> str:
    """Resolve a Java application name to one matching remote JVM candidate."""
    pool = get_connection_pool()
    session = pool.get_session(session_id)
    if not session:
        creds = _get_session_credentials(session_id)
        if creds:
            session = pool.get_session_by_host(
                str(creds["host"]), int(creds["port"]), str(creds["username"])
            )
        if not session:
            return _structured_error(
                ErrorCode.SESSION_NOT_FOUND,
                "Session not found or expired",
                suggestion="Reconnect using connect_ssh.",
            )

    try:
        records = collect_inventory_over_ssh(lambda command: _exec_ssh(session, command))
        candidate = resolve_java_application(records, application_name)
        # Preserve the discovered identity for the caller's next Arthas operation.
        session.start_time = candidate.start_time
        return json.dumps(
            {
                "pid": candidate.pid,
                "command": candidate.command,
                "owner": candidate.owner,
                "start_time": candidate.start_time,
                "boot_id": candidate.boot_id,
                "identity_key": candidate.identity_key(),
                "handle": TargetIdentity(
                    host=str(session.host),
                    port=int(session.port),
                    username=str(session.username),
                    pid=candidate.pid,
                    start_time=candidate.start_time,
                ).handle,
                "identity_complete": identity_complete(candidate),
            }
        )
    except DomainError as exc:
        logger.error("find_java_application failed for %s: %s", application_name, exc)
        return _structured_error(exc.code, exc.message, suggestion=exc.suggestion)
    except Exception as exc:
        logger.error("find_java_application failed for %s: %s", application_name, exc)
        return f"Error: {exc}"


@mcp.tool()
@require_session(pool_getter=get_connection_pool, structured_errors=True)
def thread_dump(session: object, pid: int, top_n: int = 20) -> str:
    """Get thread dump (top N threads by CPU usage) for a Java process."""
    if isinstance(pid, str):
        pid = int(pid)
    if isinstance(top_n, str):
        top_n = int(top_n)

    try:
        client = ArthasClient(session)  # type: ignore[arg-type]
        return client.thread_dump(pid=pid, top_n=top_n)
    except Exception as e:
        logger.error("thread_dump failed for PID %d: %s", pid, e)
        return json.dumps(
            to_mcp_result(
                ToolResult(
                    status="error",
                    summary=f"Error getting thread dump: {e}",
                    error=map_exception(e),
                    meta=ResultMeta(request_id=f"req-{uuid.uuid4().hex}", duration_ms=0),
                )
            )
        )


@mcp.tool()
@require_session(pool_getter=get_connection_pool, structured_errors=True)
def heap_info(session: object, pid: int) -> str:
    """Get heap and memory dashboard for a Java process."""
    if isinstance(pid, str):
        pid = int(pid)

    try:
        client = ArthasClient(session)  # type: ignore[arg-type]
        return client.heap_info(pid=pid)
    except Exception as e:
        logger.error("heap_info failed for PID %d: %s", pid, e)
        return json.dumps(
            to_mcp_result(
                ToolResult(
                    status="error",
                    summary=f"Error getting heap info: {e}",
                    error=map_exception(e),
                    meta=ResultMeta(request_id=f"req-{uuid.uuid4().hex}", duration_ms=0),
                )
            )
        )


@mcp.tool()
@require_session(structured_errors=True)
def watch_method(
    session: object,
    pid: int,
    class_pattern: str,
    method_pattern: str,
    watch_params: bool = True,
    watch_return: bool = True,
    condition: str | None = None,
    times: int = 5,
) -> str:
    """Watch method execution - monitor input parameters and/or return values."""
    if isinstance(pid, str):
        pid = int(pid)
    if isinstance(times, str):
        times = int(times)
    if isinstance(watch_params, str):
        watch_params = watch_params.lower() in ("true", "1", "yes")
    if isinstance(watch_return, str):
        watch_return = watch_return.lower() in ("true", "1", "yes")

    try:
        try:
            _watch_policy.validate_watch_times(times)
        except ValueError as exc:
            raise DomainError(
                code=ErrorCode.OBSERVATION_LIMIT_EXCEEDED,
                message=str(exc),
                phase="policy",
                suggestion="Reduce times or retry after the observation limit window.",
            ) from exc
        with _watch_policy:
            client = ArthasClient(session)  # type: ignore[arg-type]
            return client.watch_method(
                pid=pid,
                class_pattern=class_pattern,
                method_pattern=method_pattern,
                watch_params=watch_params,
                watch_return=watch_return,
                condition=condition,
                times=times,
            )
    except Exception as e:
        logger.error("watch_method failed: %s", e)
        error = map_exception(e)
        return json.dumps(
            to_mcp_result(
                ToolResult(
                    status="error",
                    summary=f"Error watching method: {e}",
                    error=error,
                    meta=ResultMeta(request_id=f"req-{uuid.uuid4().hex}", duration_ms=0),
                )
            )
        )


@mcp.tool()
@require_session(structured_errors=True)
def trace_method(
    session: object,
    pid: int,
    class_pattern: str,
    method_pattern: str,
    condition: str | None = None,
    times: int = 5,
    concurrency: int = 1,
    ttl: int = 60,
    max_chars: int = 16_384,
) -> str:
    """Run real Arthas trace with bounded observations and output."""
    try:
        pid, times, concurrency, ttl, max_chars = map(
            int, (pid, times, concurrency, ttl, max_chars)
        )
        if max_chars < 0:
            raise ValueError("max_chars must be non-negative")
        try:
            _watch_policy.validate_trace(times, concurrency, ttl)
        except ValueError as exc:
            raise DomainError(
                ErrorCode.OBSERVATION_LIMIT_EXCEEDED, str(exc), phase="policy"
            ) from exc
        with _watch_policy:
            output = ArthasClient(cast("SSHSession", session)).trace_method(
                pid, class_pattern, method_pattern, condition, times, ttl
            )
        return limit_output(output, max_chars).text
    except Exception as exc:
        logger.error("trace_method failed: %s", exc)
        return json.dumps(
            to_mcp_result(
                ToolResult(
                    status="error",
                    summary=f"Error tracing method: {exc}",
                    error=map_exception(exc),
                    meta=ResultMeta(request_id=f"req-{uuid.uuid4().hex}", duration_ms=0),
                )
            )
        )


@mcp.tool()
@require_session(structured_errors=True)
def exec_command(session: object, pid: int, command: str, timeout: int = 60) -> str:
    """
    Execute an explicitly allowlisted read-only expert command on the target JVM.

    This is a constrained executor for the explicitly allowlisted read-only
    expert commands; it is not a complete Arthas command suite.

    --- JVM & Runtime Diagnostics ---
    dashboard -n 1              JVM overview: threads, memory, GC, runtime
    jvm                         JVM runtime info, classpath, arguments
    sysprop [pattern]           System properties
    sysenv [key]                Environment variables
    vmoption [key] [value]      JVM options (read/set)
    memory                      Memory pool details
    perfcounter [name]          JVM performance counters
    mbean [name]                MBean information
    getstatic class field       Read static field value

    --- Thread Diagnostics ---
    thread -n 20                Top 20 threads by CPU
    thread <tid>                Thread detail and stack trace
    thread -b                   Detect deadlocks
    thread --state RUNNABLE     Filter by state

    --- Class & Bytecode ---
    sc -d com.example.Service   Search class info
    sm -d com.example.Service   Search method info
    jad --source-only Class     Decompile to Java source
    classloader -t              ClassLoader tree
    dump -d /tmp Class          Dump class bytecode
    redefine /tmp/Class.class   Hot-reload (emergency only)

    --- Method Tracing ---
    trace Class method '#cost>100' -n 5    Execution trace with timing
    stack Class method -n 5                Call stack
    monitor -c 5 Class method -n 3         QPS/RT monitor
    watch Class method '{params,returnObj}' -n 5 -x 3
    tt -t Class method -n 10               Record invocations

    --- Heap & Profiler ---
    heapdump /tmp/heap.hprof    Generate heap dump
    profiler start              Start CPU profiling
    profiler stop --file /tmp/flame.html   Flame graph
    profiler start --event alloc           Memory allocation profile

    --- Management ---
    version                     Arthas agent version
    help                        Show help
    stop                        Detach agent

    Examples:
        trace com.order.Service createOrder '#cost>500' -n 10
        heapdump /tmp/heap.hprof
        jad --source-only com.order.ServiceImpl
        monitor -c 60 com.api.Controller query -n 3
    """
    if isinstance(pid, str):
        pid = int(pid)
    if isinstance(timeout, str):
        timeout = int(timeout)

    try:
        _validate_expert_command(command)
        client = ArthasClient(session)  # type: ignore[arg-type]
        return client.exec_command(pid=pid, command=command, timeout=timeout)
    except Exception as e:
        logger.error("exec_command failed: %s", e)
        return json.dumps(
            to_mcp_result(
                ToolResult(
                    status="error",
                    summary=f"Error executing command: {e}",
                    error=map_exception(e),
                    meta=ResultMeta(request_id=f"req-{uuid.uuid4().hex}", duration_ms=0),
                )
            )
        )


@mcp.tool()
@require_session(structured_errors=True)
def install_arthas(session: object, install_type: str = "auto") -> str:
    """Install Arthas on the target server if not already present."""
    try:
        client = ArthasClient(session)  # type: ignore[arg-type]
        return client.install_arthas(install_type=install_type)
    except Exception as e:
        logger.error("install_arthas failed: %s", e)
        return json.dumps(
            to_mcp_result(
                ToolResult(
                    status="error",
                    summary=f"Arthas installation failed: {e}",
                    error=map_exception(e),
                    meta=ResultMeta(request_id=f"req-{uuid.uuid4().hex}", duration_ms=0),
                )
            )
        )


@mcp.tool()
def disconnect_ssh(session_id: str) -> str:
    """Disconnect an SSH session and release resources."""
    pool = get_connection_pool()
    if pool.disconnect(session_id):
        with _store_lock:
            _session_store.pop(session_id, None)
        return f"Session {session_id} disconnected successfully."
    return _structured_error(ErrorCode.SESSION_NOT_FOUND, f"Session {session_id} not found.")


# ─── Transport ───────────────────────────────────────────────────────────────


def validate_tls_config(certfile: str | None, keyfile: str | None) -> None:
    """Require a complete TLS key pair when HTTP TLS is requested."""
    if bool(certfile) != bool(keyfile):
        raise ValueError("TLS requires both certificate and key files")


def run_sse(
    host: str = "127.0.0.1",
    port: int = 8000,
    log_level: str = "info",
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
) -> None:
    token = os.environ.get("MCP_AUTH_TOKEN")
    validate_transport_security(host, token)
    validate_tls_config(ssl_certfile, ssl_keyfile)
    app = build_sse_app(token)

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )


def build_sse_app(token: str | None = None) -> Any:
    app = mcp.sse_app()

    async def healthz(_request: Any) -> JSONResponse:
        return JSONResponse(health_payload())

    app.router.routes.insert(0, Route("/healthz", healthz, methods=["GET"]))
    app.router.routes.append(_job_manager.websocket_app().routes[0])
    auth_token = token if token is not None else os.environ.get("MCP_AUTH_TOKEN")
    return build_auth_middleware(app, auth_token)


def build_streamable_http_app(token: str | None = None) -> Any:
    app = mcp.streamable_http_app()
    app.router.routes.append(_job_manager.websocket_app().routes[0])
    auth_token = token if token is not None else os.environ.get("MCP_AUTH_TOKEN")
    return build_auth_middleware(app, auth_token)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Arthas MCP Proxy Server")
    parser.add_argument("--transport", choices=["sse", "stdio", "streamable-http"], default="sse")
    parser.add_argument(
        "--host",
        default=os.environ.get("MCP_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MCP_PORT", "8000")),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument("--ssl-certfile", default=os.environ.get("MCP_SSL_CERTFILE"))
    parser.add_argument("--ssl-keyfile", default=os.environ.get("MCP_SSL_KEYFILE"))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.getLogger().setLevel(getattr(logging, args.log_level))

    logger.info("Starting Arthas MCP Proxy (transport=%s)", args.transport)

    if args.transport == "sse":
        run_sse(
            host=args.host,
            port=args.port,
            log_level=args.log_level.lower(),
            ssl_certfile=args.ssl_certfile,
            ssl_keyfile=args.ssl_keyfile,
        )
    elif args.transport == "streamable-http":
        token = os.environ.get("MCP_AUTH_TOKEN")
        validate_transport_security(args.host, token)
        validate_tls_config(args.ssl_certfile, args.ssl_keyfile)
        uvicorn.run(
            build_streamable_http_app(token),
            host=args.host,
            port=args.port,
            log_level=args.log_level.lower(),
            ssl_certfile=args.ssl_certfile,
            ssl_keyfile=args.ssl_keyfile,
        )
    else:
        asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
