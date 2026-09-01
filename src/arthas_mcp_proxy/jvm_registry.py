"""In-process opaque JVM handle registry (B3).

find_java_application mints `jvm_` + hex tokens. Bindings expire after a TTL
and are scoped to a session target_key so a handle from host A cannot be
resolved against host B.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from .errors import DomainError
from .models import ErrorCode
from .target_state import parse_target_key, target_key

if TYPE_CHECKING:
    from collections.abc import Callable

    from .ssh_pool import SSHConnectionPool, SSHSession

DEFAULT_TTL_SECONDS = 1800

IdentityKey = tuple[str, int, str | None, str | None]


@dataclass(frozen=True)
class JvmBinding:
    handle: str
    target_key: str
    pid: int
    start_time: str | None
    boot_id: str | None
    application_name: str
    created_at: float
    last_used_at: float
    expires_at: float


class JvmRegistry:
    """Mint and resolve opaque JVM handles with sliding-window expiry."""

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._by_handle: dict[str, JvmBinding] = {}
        self._by_identity: dict[IdentityKey, str] = {}

    def mint(
        self,
        *,
        target_key: str,
        pid: int,
        start_time: str | None,
        boot_id: str | None,
        application_name: str,
    ) -> str:
        """Reuse an unexpired binding for this JVM identity, otherwise mint new."""
        identity = (target_key, pid, start_time, boot_id)
        with self._lock:
            existing = self._by_identity.get(identity)
            if existing is not None:
                binding = self._by_handle.get(existing)
                now = self._clock()
                if binding is not None and now < binding.expires_at:
                    renewed = replace(
                        binding,
                        last_used_at=now,
                        expires_at=now + self._ttl_seconds,
                    )
                    self._by_handle[existing] = renewed
                    return existing
            now = self._clock()
            handle = f"jvm_{secrets.token_hex(8)}"
            binding = JvmBinding(
                handle=handle,
                target_key=target_key,
                pid=pid,
                start_time=start_time,
                boot_id=boot_id,
                application_name=application_name,
                created_at=now,
                last_used_at=now,
                expires_at=now + self._ttl_seconds,
            )
            self._by_handle[handle] = binding
            self._by_identity[identity] = handle
            return handle

    def lookup(self, handle: str) -> JvmBinding:
        """Look up a handle without a session key. Slides TTL on success."""
        return self._touch(handle, expected_target_key=None)

    def resolve(self, handle: str, *, target_key: str) -> JvmBinding:
        """Look up a handle, sliding last_used_at / expires_at on success."""
        return self._touch(handle, expected_target_key=target_key)

    def _touch(self, handle: str, *, expected_target_key: str | None) -> JvmBinding:
        with self._lock:
            binding = self._by_handle.get(handle)
            if binding is None:
                raise DomainError(ErrorCode.HANDLE_NOT_FOUND, "JVM handle not found")
            now = self._clock()
            if now >= binding.expires_at:
                raise DomainError(ErrorCode.HANDLE_EXPIRED, "JVM handle expired")
            if expected_target_key is not None and binding.target_key != expected_target_key:
                raise DomainError(
                    ErrorCode.HANDLE_SESSION_MISMATCH,
                    "JVM handle does not match this session",
                )
            renewed = replace(
                binding,
                last_used_at=now,
                expires_at=now + self._ttl_seconds,
            )
            self._by_handle[handle] = renewed
            return renewed

    def find_live(self, *, target_key: str, pid: int) -> JvmBinding | None:
        """Return the newest unexpired binding for this target+pid, if any."""
        now = self._clock()
        best: JvmBinding | None = None
        with self._lock:
            for binding in self._by_handle.values():
                if binding.target_key != target_key or binding.pid != pid:
                    continue
                if now >= binding.expires_at:
                    continue
                if best is None or binding.last_used_at > best.last_used_at:
                    best = binding
        return best


def _coerce_pid(pid: int | None) -> int | None:
    if pid is None:
        return None
    if isinstance(pid, bool) or not isinstance(pid, int):
        try:
            return int(pid)
        except (TypeError, ValueError) as exc:
            raise DomainError(
                ErrorCode.INVALID_ARGUMENT,
                "pid must be an integer",
                phase="resolve",
            ) from exc
    return pid


def _session_from_id(
    pool: SSHConnectionPool,
    session_id: str,
    fallback_getter: Callable[[str], dict[str, Any] | None] | None,
) -> SSHSession | None:
    session = pool.get_session(session_id)
    if session is None and fallback_getter is not None:
        creds = fallback_getter(session_id)
        if creds:
            session = pool.get_session_by_host(
                str(creds["host"]), int(creds["port"]), str(creds["username"])
            )
    return session


def _seed_identity(session: SSHSession, binding: JvmBinding) -> None:
    session.start_time = binding.start_time
    session.boot_id = binding.boot_id


def resolve_tool_target(
    *,
    jvm_handle: str | None,
    session_id: str | None,
    pid: int | None,
    pool: SSHConnectionPool,
    fallback_getter: Callable[[str], dict[str, Any] | None] | None = None,
    registry: JvmRegistry | None = None,
) -> tuple[SSHSession, int]:
    """Resolve a diagnostic tool target from jvm_handle and/or session_id+pid."""
    handle = jvm_handle if isinstance(jvm_handle, str) and jvm_handle else None
    sid = session_id if isinstance(session_id, str) and session_id else None
    pid = _coerce_pid(pid)
    has_session_pid = sid is not None and pid is not None
    if handle is None and not has_session_pid:
        raise DomainError(
            ErrorCode.INVALID_ARGUMENT,
            "jvm_handle or session_id+pid is required",
            phase="resolve",
            suggestion=(
                "Pass jvm_handle from find_java_application, or session_id and pid together."
            ),
        )
    reg = registry or get_jvm_registry()

    if handle is not None and sid is not None:
        session = _session_from_id(pool, sid, fallback_getter)
        if session is None:
            raise DomainError(
                ErrorCode.SESSION_NOT_FOUND,
                "Session not found or expired",
                phase="resolve",
                retryable=True,
                suggestion="Reconnect using connect_ssh, then retry the diagnostic tool.",
            )
        binding = reg.resolve(
            handle,
            target_key=target_key(session.host, session.port, session.username),
        )
        if pid is not None and pid != binding.pid:
            raise DomainError(
                ErrorCode.INVALID_ARGUMENT,
                "pid does not match jvm_handle",
                phase="resolve",
            )
        _seed_identity(session, binding)
        return session, binding.pid

    if handle is not None:
        binding = reg.lookup(handle)
        if pid is not None and pid != binding.pid:
            raise DomainError(
                ErrorCode.INVALID_ARGUMENT,
                "pid does not match jvm_handle",
                phase="resolve",
            )
        try:
            username, host, port = parse_target_key(binding.target_key)
        except ValueError as exc:
            raise DomainError(
                ErrorCode.SESSION_NOT_FOUND,
                "Session not found or expired",
                phase="resolve",
                retryable=True,
            ) from exc
        session = pool.get_session_by_host(host, port, username)
        if session is None:
            raise DomainError(
                ErrorCode.SESSION_NOT_FOUND,
                "Session not found or expired",
                phase="resolve",
                retryable=True,
                suggestion="Reconnect using connect_ssh, then retry the diagnostic tool.",
            )
        _seed_identity(session, binding)
        return session, binding.pid

    if sid is None or pid is None:
        raise DomainError(
            ErrorCode.INVALID_ARGUMENT,
            "jvm_handle or session_id+pid is required",
            phase="resolve",
        )
    session = _session_from_id(pool, sid, fallback_getter)
    if session is None:
        raise DomainError(
            ErrorCode.SESSION_NOT_FOUND,
            "Session not found or expired",
            phase="resolve",
            retryable=True,
            suggestion="Reconnect using connect_ssh, then retry the diagnostic tool.",
        )
    tk = target_key(session.host, session.port, session.username)
    live = (registry or get_jvm_registry()).find_live(target_key=tk, pid=pid)
    if live is not None:
        _seed_identity(session, live)
    return session, pid


_REGISTRY: JvmRegistry | None = None
_REGISTRY_GUARD = threading.Lock()


def get_jvm_registry() -> JvmRegistry:
    """Return the process-wide registry, creating it on first use."""
    global _REGISTRY
    if _REGISTRY is None:
        with _REGISTRY_GUARD:
            if _REGISTRY is None:
                _REGISTRY = JvmRegistry()
    return _REGISTRY


def reset_jvm_registry(registry: JvmRegistry | None = None) -> None:
    """Replace the process-wide registry so tests do not leak bindings."""
    global _REGISTRY
    with _REGISTRY_GUARD:
        _REGISTRY = registry if registry is not None else JvmRegistry()
