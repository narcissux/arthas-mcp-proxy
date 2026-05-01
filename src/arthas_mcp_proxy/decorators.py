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
"""

from __future__ import annotations

import inspect
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

    The decorated function must accept ``session`` as its first parameter.
    The decorator rewrites the public signature so that FastMCP exposes
    ``session_id: str`` instead, then resolves the ID to a real
    :class:`SSHSession` before calling the wrapped function.

    Example:
        @mcp.tool()
        @require_session()
        def thread_dump(session: object, pid: int, top_n: int = 20) -> str:
            ...

    Returns:
        A wrapper whose ``__signature__`` exposes ``session_id`` and
        which returns ``"Error: ..."`` when the session cannot be resolved.
    """
    if pool_getter is None:
        from arthas_mcp_proxy.ssh_pool import get_connection_pool

        pool_getter = get_connection_pool

    def decorator(func: F) -> F:
        # ------------------------------------------------------------------
        # Rewrite signature:  session: object  →  session_id: str
        # This ensures FastMCP generates a JSON schema that expects a
        # session *string* from the client, not an opaque SSHSession object.
        # ------------------------------------------------------------------
        old_sig = inspect.signature(func)
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
                return "Error: Session not found or expired. " "Please reconnect using connect_ssh."

            kwargs["session"] = session
            result: str = func(*args, **kwargs)
            return result

        # Override the signature that FastMCP (and inspect) sees.
        wrapper.__signature__ = new_sig  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorator
