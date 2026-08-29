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
from typing import TYPE_CHECKING

from .errors import DomainError
from .models import ErrorCode

if TYPE_CHECKING:
    from collections.abc import Callable

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

    def resolve(self, handle: str, *, target_key: str) -> JvmBinding:
        """Look up a handle, sliding last_used_at / expires_at on success."""
        with self._lock:
            binding = self._by_handle.get(handle)
            if binding is None:
                raise DomainError(ErrorCode.HANDLE_NOT_FOUND, "JVM handle not found")
            now = self._clock()
            if now >= binding.expires_at:
                raise DomainError(ErrorCode.HANDLE_EXPIRED, "JVM handle expired")
            if binding.target_key != target_key:
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
