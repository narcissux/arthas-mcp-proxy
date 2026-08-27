from datetime import datetime, timezone

import pytest

from arthas_mcp_proxy.arthas_lifecycle import ArthasInstance, ArthasInstanceRegistry, ArthasOrigin
from arthas_mcp_proxy.target_state import TargetIdentity


def _identity(
    host: str = "host-a",
    pid: int = 4242,
    *,
    port: int = 22,
    username: str = "root",
    start_time: str | None = "100.0",
) -> TargetIdentity:
    return TargetIdentity(host=host, port=port, username=username, pid=pid, start_time=start_time)


@pytest.mark.contract
def test_arthas_origin_has_expected_members() -> None:
    assert {origin.name for origin in ArthasOrigin} == {"EXISTING", "STARTED_BY_PROXY", "UNKNOWN"}


@pytest.mark.contract
def test_arthas_instance_carries_ownership_fields() -> None:
    used_at = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    instance = ArthasInstance(3658, 4242, ArthasOrigin.STARTED_BY_PROXY, used_at)
    assert instance.port == 3658
    assert instance.pid == 4242
    assert instance.origin is ArthasOrigin.STARTED_BY_PROXY
    assert instance.last_used_at == used_at


@pytest.mark.contract
def test_can_auto_stop_true_only_for_started_by_proxy() -> None:
    used_at = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert ArthasInstance(3658, 4242, ArthasOrigin.STARTED_BY_PROXY, used_at).can_auto_stop is True
    assert ArthasInstance(3658, 4242, ArthasOrigin.EXISTING, used_at).can_auto_stop is False
    assert ArthasInstance(3658, 4242, ArthasOrigin.UNKNOWN, used_at).can_auto_stop is False


@pytest.mark.contract
def test_should_auto_stop_uses_ttl_and_origin() -> None:
    used_at = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 8, 1, 12, 5, 0, tzinfo=timezone.utc)
    started = ArthasInstance(3658, 4242, ArthasOrigin.STARTED_BY_PROXY, used_at)
    existing = ArthasInstance(3658, 4242, ArthasOrigin.EXISTING, used_at)
    assert started.idle_seconds(now) == 300
    assert started.should_auto_stop(now, ttl_seconds=60) is True
    assert existing.should_auto_stop(now, ttl_seconds=60) is False


@pytest.mark.contract
def test_registry_register_and_get_by_target_identity() -> None:
    registry = ArthasInstanceRegistry()
    used_at = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    instance = ArthasInstance(3658, 4242, ArthasOrigin.STARTED_BY_PROXY, used_at)
    registry.register(_identity(), instance)
    assert registry.get(_identity()) is instance


@pytest.mark.contract
def test_registry_get_unknown_identity_returns_none() -> None:
    registry = ArthasInstanceRegistry()
    assert registry.get(_identity(pid=9999)) is None


@pytest.mark.contract
def test_registry_keys_instances_by_target_process_identity() -> None:
    registry = ArthasInstanceRegistry()
    used_at = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    instance = ArthasInstance(3658, 4242, ArthasOrigin.STARTED_BY_PROXY, used_at)
    registry.register(_identity(host="host-a", pid=4242), instance)
    # Same PID on a different host is a distinct target -> not found here.
    assert registry.get(_identity(host="host-b", pid=4242)) is None
    # Restarted process (new start_time) is distinct from the original.
    assert registry.get(_identity(host="host-a", pid=4242, start_time="200.0")) is None


@pytest.mark.contract
def test_registry_cleanup_candidates_returns_only_proxy_owned_idle() -> None:
    registry = ArthasInstanceRegistry()
    used_at = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 8, 1, 12, 5, 0, tzinfo=timezone.utc)
    proxy_owned = ArthasInstance(3658, 4242, ArthasOrigin.STARTED_BY_PROXY, used_at)
    existing = ArthasInstance(3659, 4343, ArthasOrigin.EXISTING, used_at)
    unknown = ArthasInstance(3660, 4444, ArthasOrigin.UNKNOWN, used_at)
    registry.register(_identity(pid=4242), proxy_owned)
    registry.register(_identity(pid=4343), existing)
    registry.register(_identity(pid=4444), unknown)

    candidates = registry.cleanup_candidates(now, ttl_seconds=60)
    assert candidates == [proxy_owned]
    assert existing not in candidates
    assert unknown not in candidates


@pytest.mark.contract
def test_registry_cleanup_candidates_excludes_recently_used() -> None:
    registry = ArthasInstanceRegistry()
    used_at = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 8, 1, 12, 0, 30, tzinfo=timezone.utc)  # 30s idle, under ttl
    registry.register(
        _identity(),
        ArthasInstance(3658, 4242, ArthasOrigin.STARTED_BY_PROXY, used_at),
    )
    assert registry.cleanup_candidates(now, ttl_seconds=60) == []


@pytest.mark.contract
def test_registry_touch_updates_last_used_at() -> None:
    registry = ArthasInstanceRegistry()
    original = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    current = datetime(2026, 8, 1, 12, 3, 0, tzinfo=timezone.utc)
    instance = ArthasInstance(3658, 4242, ArthasOrigin.STARTED_BY_PROXY, original)
    registry.register(_identity(), instance)

    assert registry.touch(_identity(), current) is instance
    assert instance.last_used_at == current


@pytest.mark.contract
def test_registry_cleanup_removes_only_proxy_owned_instance() -> None:
    registry = ArthasInstanceRegistry()
    used_at = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    owned = ArthasInstance(3658, 4242, ArthasOrigin.STARTED_BY_PROXY, used_at)
    existing = ArthasInstance(3659, 4343, ArthasOrigin.EXISTING, used_at)
    registry.register(_identity(pid=4242), owned)
    registry.register(_identity(pid=4343), existing)

    assert registry.cleanup(_identity(pid=4242)) is owned
    assert registry.get(_identity(pid=4242)) is None
    assert registry.cleanup(_identity(pid=4343)) is None
    assert registry.get(_identity(pid=4343)) is existing


@pytest.mark.contract
def test_expired_cleanup_requires_explicit_authorization_and_callback() -> None:
    registry = ArthasInstanceRegistry()
    used_at = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 8, 1, 12, 5, 0, tzinfo=timezone.utc)
    owned = ArthasInstance(3658, 4242, ArthasOrigin.STARTED_BY_PROXY, used_at)
    existing = ArthasInstance(3659, 4343, ArthasOrigin.EXISTING, used_at)
    unknown = ArthasInstance(3660, 4444, ArthasOrigin.UNKNOWN, used_at)
    registry.register(_identity(pid=4242), owned)
    registry.register(_identity(pid=4343), existing)
    registry.register(_identity(pid=4444), unknown)
    stopped: list[ArthasInstance] = []

    assert registry.cleanup_expired(now, 60, stopped.append) == []
    assert stopped == []
    assert registry.get(_identity(pid=4242)) is owned

    assert registry.cleanup_expired(now, 60, stopped.append, authorized=True) == [owned]
    assert stopped == [owned]
    assert registry.get(_identity(pid=4242)) is None
    assert registry.get(_identity(pid=4343)) is existing
    assert registry.get(_identity(pid=4444)) is unknown
