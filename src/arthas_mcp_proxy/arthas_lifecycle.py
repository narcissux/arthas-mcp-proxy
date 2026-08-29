from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .target_state import TargetIdentity


class ArthasInstanceRegistry:
    def __init__(self) -> None:
        self._instances: dict[TargetIdentity, ArthasInstance] = {}

    def register(self, identity: TargetIdentity, instance: "ArthasInstance") -> None:
        self._instances[identity] = instance

    def get(self, identity: TargetIdentity) -> "ArthasInstance | None":
        return self._instances.get(identity)

    def touch(self, identity: TargetIdentity, used_at: datetime) -> "ArthasInstance | None":
        """Record use without changing ownership or contacting the target."""
        instance = self.get(identity)
        if instance is not None:
            instance.last_used_at = used_at
        return instance

    def cleanup(self, identity: TargetIdentity) -> "ArthasInstance | None":
        """Forget a proxy-owned instance; never stop a remote Arthas process."""
        instance = self.get(identity)
        if instance is None or not instance.can_auto_stop:
            return None
        return self._instances.pop(identity)

    def forget(self, identity: TargetIdentity) -> "ArthasInstance | None":
        """Drop a cached instance regardless of origin (failed verify / half-ready)."""
        return self._instances.pop(identity, None)

    def cleanup_candidates(self, now: datetime, ttl_seconds: int) -> list["ArthasInstance"]:
        return [
            instance
            for instance in self._instances.values()
            if instance.should_auto_stop(now, ttl_seconds)
        ]

    def cleanup_expired(
        self,
        now: datetime,
        ttl_seconds: int,
        cleanup_callback: Callable[["ArthasInstance"], None],
        *,
        authorized: bool = False,
        target_identity: TargetIdentity | None = None,
    ) -> list["ArthasInstance"]:
        """Explicitly clean up expired proxy-owned instances.

        This is opt-in: without authorization it performs no callback and
        leaves the registry unchanged. The callback owns remote stopping.
        """
        if not authorized:
            return []

        cleaned: list[ArthasInstance] = []
        for identity, instance in list(self._instances.items()):
            if target_identity is not None and identity != target_identity:
                continue
            if not instance.should_auto_stop(now, ttl_seconds):
                continue
            cleanup_callback(instance)
            self._instances.pop(identity, None)
            cleaned.append(instance)
        return cleaned


class ArthasOrigin(Enum):
    EXISTING = "existing"
    STARTED_BY_PROXY = "started_by_proxy"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PreparedArthas:
    origin: ArthasOrigin
    telnet_port: int
    http_port: int | None
    arthas_version: str


@dataclass
class ArthasInstance:
    port: int
    pid: int
    origin: ArthasOrigin
    last_used_at: datetime

    @property
    def can_auto_stop(self) -> bool:
        return self.origin is ArthasOrigin.STARTED_BY_PROXY

    def idle_seconds(self, now: datetime) -> float:
        return (now - self.last_used_at).total_seconds()

    def should_auto_stop(self, now: datetime, ttl_seconds: int) -> bool:
        return self.can_auto_stop and self.idle_seconds(now) > ttl_seconds
