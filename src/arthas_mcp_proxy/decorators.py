"""MCP tool decorators for Arthas MCP Proxy.

Provides:
    - @require_session: Injects ``session`` (SSHSession) by looking up
      ``session_id`` from the connection pool, eliminating repetitive
      session-retrieval code in every tool function.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from arthas_mcp_proxy.ssh_pool import SSHConnectionPool, SSHSession

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# Fallback credential store callback - set by server.py at import time
_fallback_credential_getter: Callable[[str], dict[str, Any] | None] | None = None


def set_fallback_credential_getter(
    getter: Callable[[str], dict[str, Any] | None] | None,
) -> None:
    """Register a callback to resolve session_id -> credentials for fallback.

    Called by server.py during startup to avoid circular imports.
    """
    global _fallback_credential_getter
    _fallback_credential_getter = getter


def require_session(
    *,
    pool_getter: Callable[[], SSHConnectionPool] | None = None,
    fallback: bool = True,
) -> Callable[[F], F]:
    """Decorator that resolves *session_id* -> *session* for MCP tools.

    The decorated function **must** accept ``session_id`` as its first
    positional argument (after ``self`` if present).  The decorator
    replaces it with an active :class:`SSHSession` injected as the
    keyword argument ``session``.

    Args:
        pool_getter: Callable that returns the :class:`SSHConnectionPool`.
            Defaults to :func:`arthas_mcp_proxy.ssh_pool.get_connection_pool`.
        fallback: If *True* and the session is not found by ID, attempt
            to fall back to host-based lookup using cached credentials.

    Example:
        @mcp.tool()
        @require_session()
        def thread_dump(session: SSHSession, pid: int, top_n: int = 20) -> str:
            ...

    Returns:
        A wrapper that returns ``"Error: Session not found ..."`` when
        the session cannot be resolved, avoiding uncaught exceptions.
    """
    if pool_getter is None:
        from arthas_mcp_proxy.ssh_pool import get_connection_pool

        pool_getter = get_connection_pool

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args: object, **kwargs: object) -> str:
            session_id = kwargs.get("session_id")
            if not session_id or not isinstance(session_id, str):
                return "Error: session_id is required"

            pool = pool_getter()
            session: SSHSession | None = pool.get_session(session_id)

            if not session and fallback and _fallback_credential_getter is not None:
                creds = _fallback_credential_getter(session_id)
                if creds:
                    session = pool.get_session_by_host(
                        creds["host"], creds["port"], creds["username"]
                    )

            if not session:
                return "Error: Session not found or expired. Please reconnect using connect_ssh."

            # Replace session_id with resolved session
            kwargs["session"] = session
            del kwargs["session_id"]

            result: str = func(*args, **kwargs)
            return result

        return wrapper  # type: ignore[return-value]

    return decorator
