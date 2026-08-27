from datetime import datetime, timezone
from unittest.mock import MagicMock

from arthas_mcp_proxy.arthas_client import _LIFECYCLE_REGISTRY, ArthasClient
from arthas_mcp_proxy.arthas_lifecycle import ArthasInstance, ArthasOrigin
from arthas_mcp_proxy.target_state import TargetIdentity


def test_authorized_cleanup_uses_client_detach_and_never_stops_unowned(monkeypatch):
    session = MagicMock(host="target-a", port=22, username="root")
    client = ArthasClient(session, start_time="100.0")
    identity = TargetIdentity("target-a", 22, "root", 4242, "100.0")
    used_at = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    owned = ArthasInstance(3658, 4242, ArthasOrigin.STARTED_BY_PROXY, used_at)
    existing = ArthasInstance(3659, 4343, ArthasOrigin.EXISTING, used_at)
    unknown = ArthasInstance(3660, 4444, ArthasOrigin.UNKNOWN, used_at)
    _LIFECYCLE_REGISTRY.register(identity, owned)
    _LIFECYCLE_REGISTRY.register(TargetIdentity("target-a", 22, "root", 4343, "100.0"), existing)
    _LIFECYCLE_REGISTRY.register(TargetIdentity("target-a", 22, "root", 4444, "100.0"), unknown)
    detached: list[int] = []
    monkeypatch.setattr(client, "detach", lambda pid: detached.append(pid) or "detached")

    now = datetime(2026, 8, 1, 12, 5, tzinfo=timezone.utc)
    assert client.cleanup_expired(4242, now, 60, authorized=False) == []
    assert client.cleanup_expired(4242, now, 60, authorized=True) == [owned]
    assert detached == [4242]
    assert _LIFECYCLE_REGISTRY.get(identity) is None
    assert (
        _LIFECYCLE_REGISTRY.get(TargetIdentity("target-a", 22, "root", 4343, "100.0")) is existing
    )
    assert _LIFECYCLE_REGISTRY.get(TargetIdentity("target-a", 22, "root", 4444, "100.0")) is unknown
