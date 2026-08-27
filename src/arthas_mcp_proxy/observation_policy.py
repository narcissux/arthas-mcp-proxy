from __future__ import annotations

import threading
from dataclasses import dataclass


class _Lease:
    def __init__(self) -> None:
        self.active = True
        self.timer: threading.Timer | None = None


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

    def _expire(self, lease: _Lease) -> None:
        with self._leases_lock:
            if not lease.active:
                return
            lease.active = False
            self._leases.pop(id(lease), None)
        self._semaphore.release()

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

    def __enter__(self) -> ObservationPolicy:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()
