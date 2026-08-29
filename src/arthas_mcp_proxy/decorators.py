"""MCP tool decorators for Arthas MCP Proxy.

Provides:
    - @require_session: Injects ``session`` (SSHSession) by looking up
      ``session_id`` from the connection pool, eliminating repetitive
      session-retrieval code in every tool function.

Signature rewriting:
    The original function declares ``session: object`` (the injected
    SSHSession).  FastMCP would expose this in the JSON schema, but
    clients can only pass a string *session_id*.  The decorator rewrites
    ``session`` → ``session_id: str`` in the wrapper's ``__signature__``
    so that FastMCP registers the correct parameter name while still
    injecting the real SSHSession object at runtime.

    When ``accept_jvm_handle=True``, the public signature also exposes
    optional ``jvm_handle`` and optional ``pid``.  The wrapper resolves
    handle and/or session_id+pid, then injects ``session`` and a concrete
    ``pid`` into the wrapped function.
"""

from __future__ import annotations

import inspect
import json
import logging
import uuid
from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar, cast

from arthas_mcp_proxy.errors import DomainError, SSHTransportLostError, to_error_detail
from arthas_mcp_proxy.jvm_registry import resolve_tool_target
from arthas_mcp_proxy.models import ErrorCode, ErrorDetail, ResultMeta, ToolResult
from arthas_mcp_proxy.result_adapter import to_mcp_result

if TYPE_CHECKING:
    from arthas_mcp_proxy.ssh_pool import SSHConnectionPool, SSHSession

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# Fallback credential store callback - set by server.py at import time
_fallback_credential_getter: Callable[[str], dict[str, Any] | None] | None = None
SESSION_NOT_FOUND_MESSAGE = (
    "Error: Session not found or expired. Please reconnect using connect_ssh."
)


def session_not_found_error_detail() -> ErrorDetail:
    """Return the structured form of the legacy missing-session error."""
    return ErrorDetail(
        code=ErrorCode.SESSION_NOT_FOUND,
        message=SESSION_NOT_FOUND_MESSAGE,
        phase="resolve",
        retryable=True,
        suggestion="Reconnect using connect_ssh, then retry the diagnostic tool.",
    )


def _ensure_transport_live(session: object) -> None:
    """Raise SSH_TRANSPORT_LOST when the SSH client is already gone."""
    client = getattr(session, "client", None)
    transport = None
    if client is not None:
        getter = getattr(client, "get_transport", None)
        if callable(getter):
            transport = getter()
    is_active = getattr(transport, "is_active", None)
    if transport is not None and callable(is_active) and is_active():
        return
    sid = getattr(session, "session_id", "unknown")
    raise SSHTransportLostError(f"SSH transport lost for session {sid}")


def set_fallback_credential_getter(
    getter: Callable[[str], dict[str, Any] | None] | None,
) -> None:
    """Register a callback to resolve session_id -> credentials for fallback.

    Called by server.py during startup to avoid circular imports.
    """
    global _fallback_credential_getter
    _fallback_credential_getter = getter


def _structured_domain_error(error: DomainError) -> str:
    return json.dumps(
        to_mcp_result(
            ToolResult(
                status="error",
                summary=error.message,
                error=to_error_detail(error),
                meta=ResultMeta(request_id=f"req-{uuid.uuid4().hex}", duration_ms=0),
            )
        )
    )


def _session_not_found_result(*, structured_errors: bool) -> str:
    if not structured_errors:
        return SESSION_NOT_FOUND_MESSAGE
    return json.dumps(
        to_mcp_result(
            ToolResult(
                status="error",
                summary=SESSION_NOT_FOUND_MESSAGE,
                error=session_not_found_error_detail(),
                meta=ResultMeta(request_id=f"req-{uuid.uuid4().hex}", duration_ms=0),
            )
        )
    )


def _public_signature_with_handle(old_sig: inspect.Signature) -> inspect.Signature:
    """Rewrite session → optional session_id + jvm_handle; make pid optional."""
    required: list[inspect.Parameter] = []
    optional: list[inspect.Parameter] = []
    for name, param in old_sig.parameters.items():
        if name == "session":
            optional.append(
                inspect.Parameter(
                    "session_id",
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=None,
                    annotation=str | None,
                )
            )
            optional.append(
                inspect.Parameter(
                    "jvm_handle",
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=None,
                    annotation=str | None,
                )
            )
        elif name == "pid":
            optional.append(
                inspect.Parameter(
                    "pid",
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=None,
                    annotation=int | None,
                )
            )
        elif param.default is inspect.Parameter.empty:
            required.append(param)
        else:
            optional.append(param)
    return old_sig.replace(parameters=required + optional)


def require_session(
    *,
    pool_getter: Callable[[], SSHConnectionPool] | None = None,
    fallback: bool = True,
    structured_errors: bool = False,
    accept_jvm_handle: bool = False,
) -> Callable[[F], Callable[..., Any]]:
    """Decorator that resolves *session_id* / *jvm_handle* -> *session* for MCP tools.

    The decorated function must accept ``session`` as its first parameter.
    The decorator rewrites the public signature so that FastMCP exposes
    ``session_id`` (and optionally ``jvm_handle``) instead, then resolves
    those to a real :class:`SSHSession` before calling the wrapped function.

    Example:
        @mcp.tool()
        @require_session(accept_jvm_handle=True)
        def thread_dump(session: object, pid: int | None = None, top_n: int = 20) -> str:
            ...

    Returns:
        A wrapper whose ``__signature__`` exposes the public MCP parameters
        and which returns ``"Error: ..."`` or a structured error when the
        session cannot be resolved.
    """
    if pool_getter is None:
        from arthas_mcp_proxy.ssh_pool import get_connection_pool

        pool_getter = get_connection_pool

    def decorator(func: F) -> F:
        old_sig = inspect.signature(func)
        if accept_jvm_handle:
            new_sig = _public_signature_with_handle(old_sig)
        else:
            new_params: list[inspect.Parameter] = []
            for name, param in old_sig.parameters.items():
                if name == "session":
                    new_params.append(
                        inspect.Parameter(
                            "session_id",
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                            default=inspect.Parameter.empty,
                            annotation=str,
                        )
                    )
                else:
                    new_params.append(param)
            new_sig = old_sig.replace(parameters=new_params)

        @wraps(func)
        def wrapper(*args: object, **kwargs: object) -> str:
            if accept_jvm_handle:
                bound = new_sig.bind_partial(*args, **kwargs)
                bound.apply_defaults()
                arguments = dict(bound.arguments)
                session_id = arguments.pop("session_id", None)
                jvm_handle = arguments.pop("jvm_handle", None)
                has_pid = "pid" in old_sig.parameters
                pid = arguments.pop("pid", None) if has_pid else None
                try:
                    session, resolved_pid = resolve_tool_target(
                        jvm_handle=jvm_handle if isinstance(jvm_handle, str) else None,
                        session_id=session_id if isinstance(session_id, str) else None,
                        pid=pid if isinstance(pid, (int, str)) or pid is None else None,
                        pool=pool_getter(),
                        fallback_getter=_fallback_credential_getter if fallback else None,
                    )
                    _ensure_transport_live(session)
                except DomainError as exc:
                    if exc.code is ErrorCode.SESSION_NOT_FOUND:
                        return _session_not_found_result(structured_errors=structured_errors)
                    if structured_errors:
                        return _structured_domain_error(exc)
                    return f"Error: {exc.message}"
                call_kwargs = arguments
                call_kwargs["session"] = session
                if has_pid:
                    call_kwargs["pid"] = resolved_pid
                result: str = func(**call_kwargs)
                return result

            session_id = kwargs.pop("session_id", None)
            if not session_id or not isinstance(session_id, str):
                return "Error: session_id is required"

            pool = pool_getter()
            session: SSHSession | None = pool.get_session(session_id)

            if not session and fallback and _fallback_credential_getter is not None:
                creds = _fallback_credential_getter(session_id)
                if creds:
                    session = pool.get_session_by_host(
                        str(creds["host"]), int(creds["port"]), str(creds["username"])
                    )

            if not session:
                return _session_not_found_result(structured_errors=structured_errors)

            try:
                _ensure_transport_live(session)
            except DomainError as exc:
                if structured_errors:
                    return _structured_domain_error(exc)
                return f"Error: {exc.message}"

            kwargs["session"] = session
            result = func(*args, **kwargs)
            return result

        # Override the signature that FastMCP (and inspect) sees.
        wrapper.__signature__ = new_sig  # type: ignore[attr-defined]
        return cast("F", wrapper)

    return decorator
