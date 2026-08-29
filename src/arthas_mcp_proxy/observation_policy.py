from __future__ import annotations

import threading
from dataclasses import dataclass

from arthas_mcp_proxy.target_state import target_key

OBSERVATION_MAX_ACTIVE_PER_JVM = 3

JvmObservationKey = tuple[str, int, str | None, str | None]


def is_observation_command(command: str) -> bool:
    """True when the first token is an Arthas watch or trace command."""
    token = command.strip().split(None, 1)[0].lower() if command.strip() else ""
    return token in {"watch", "trace"}


def observation_jvm_key(session: object, pid: int) -> JvmObservationKey:
    """Identity-aware JVM key so pid reuse is a different slot namespace."""
    host = getattr(session, "host", "")
    port = getattr(session, "port", 0)
    username = getattr(session, "username", "")
    start_time = getattr(session, "start_time", None)
    boot_id = getattr(session, "boot_id", None)
    host_s = host if isinstance(host, str) else ""
    username_s = username if isinstance(username, str) else ""
    port_i = port if isinstance(port, int) and not isinstance(port, bool) else 0
    start_s = start_time if isinstance(start_time, str) else None
    boot_s = boot_id if isinstance(boot_id, str) else None
    tk = target_key(host_s, port_i, username_s)
    if start_s is None or boot_s is None:
        from arthas_mcp_proxy.jvm_registry import get_jvm_registry

        binding = get_jvm_registry().find_live(target_key=tk, pid=int(pid))
        if binding is not None:
            start_s = start_s or binding.start_time
            boot_s = boot_s or binding.boot_id
            if start_s is not None and getattr(session, "start_time", None) in (None, ""):
                session.start_time = start_s
            if boot_s is not None and getattr(session, "boot_id", None) in (None, ""):
                session.boot_id = boot_s
    return (tk, int(pid), start_s, boot_s)


class _Lease:
    def __init__(self) -> None:
        self.active = True
        self.timer: threading.Timer | None = None


class _JvmLease:
    def __init__(self, key: JvmObservationKey) -> None:
        self.key = key
        self.active = True
        self.timer: threading.Timer | None = None


class _JvmGuard:
    def __init__(self, policy: ObservationPolicy, key: JvmObservationKey) -> None:
        self._policy = policy
        self._key = key
        self._lease: _JvmLease | None = None

    def __enter__(self) -> ObservationPolicy:
        self._lease = self._policy.acquire_jvm(self._key)
        return self._policy

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self._lease is not None:
            self._policy.release_jvm(self._key, self._lease)


@dataclass
class ObservationPolicy:
    max_times: int = 5
    max_concurrent: int = 2
    ttl_seconds: float = 60

    def __post_init__(self) -> None:
        if self.max_times < 1 or self.max_concurrent < 1 or self.ttl_seconds <= 0:
            raise ValueError("observation policy limits must be positive")
        self._semaphore = threading.Semaphore(self.max_concurrent)
        self._leases: dict[int, _Lease] = {}
        self._leases_lock = threading.Lock()
        self._local = threading.local()
        self._jvm_lock = threading.Lock()
        self._jvm_leases: dict[JvmObservationKey, list[_JvmLease]] = {}

    def _expire(self, lease: _Lease) -> None:
        with self._leases_lock:
            if not lease.active:
                return
            lease.active = False
            self._leases.pop(id(lease), None)
        self._semaphore.release()

    def _expire_jvm(self, lease: _JvmLease) -> None:
        self.release_jvm(lease.key, lease)

    def validate_times(self, start: int, times: int) -> None:
        if start < 1 or times < 1 or start + times - 1 > self.max_times:
            raise ValueError("observation count exceeds policy")

    def validate_watch_times(self, times: int) -> None:
        """Convenience alias for a watch starting at observation 1."""
        self.validate_times(1, times)

    def validate_trace(self, times: int, concurrency: int, ttl: float) -> None:
        self.validate_times(1, times)
        if concurrency < 1 or concurrency > self.max_concurrent:
            raise ValueError("trace concurrency exceeds policy")
        if ttl <= 0 or ttl > self.ttl_seconds:
            raise ValueError("trace ttl exceeds policy")

    def acquire(self) -> None:
        self._semaphore.acquire()
        lease = _Lease()
        lease.timer = threading.Timer(self.ttl_seconds, self._expire, args=(lease,))
        lease.timer.daemon = True
        with self._leases_lock:
            self._leases[id(lease)] = lease
        lease.timer.start()
        self._local.lease = lease

    def release(self) -> None:
        lease = getattr(self._local, "lease", None)
        if lease is None:
            return
        with self._leases_lock:
            if not lease.active:
                self._local.lease = None
                return
            lease.active = False
            self._leases.pop(id(lease), None)
        if lease.timer is not None:
            lease.timer.cancel()
        self._semaphore.release()
        self._local.lease = None

    def acquire_jvm(self, key: JvmObservationKey) -> _JvmLease:
        """Take one per-JVM observation slot. Fail-fast when the cap is full."""
        lease = _JvmLease(key)
        with self._jvm_lock:
            held = self._jvm_leases.setdefault(key, [])
            if len(held) >= OBSERVATION_MAX_ACTIVE_PER_JVM:
                raise ValueError("OBSERVATION_LIMIT_EXCEEDED")
            held.append(lease)
            lease.timer = threading.Timer(self.ttl_seconds, self._expire_jvm, args=(lease,))
            lease.timer.daemon = True
        lease.timer.start()
        return lease

    def release_jvm(self, key: JvmObservationKey, lease: _JvmLease | None = None) -> None:
        """Release one per-JVM slot (a specific lease, or the most recent)."""
        timer: threading.Timer | None = None
        with self._jvm_lock:
            held = self._jvm_leases.get(key)
            if not held:
                return
            target = lease
            if target is None:
                target = held[-1]
            if not target.active:
                return
            target.active = False
            if target in held:
                held.remove(target)
            if not held:
                self._jvm_leases.pop(key, None)
            timer = target.timer
        if timer is not None:
            timer.cancel()

    def for_jvm(self, key: JvmObservationKey) -> _JvmGuard:
        """Context manager that acquires and releases one per-JVM slot."""
        return _JvmGuard(self, key)

    def __enter__(self) -> ObservationPolicy:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()
