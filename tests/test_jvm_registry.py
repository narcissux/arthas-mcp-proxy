"""B3-1: opaque jvm_ handle registry."""

from __future__ import annotations

import re

import pytest

from arthas_mcp_proxy.errors import DomainError
from arthas_mcp_proxy.jvm_registry import JvmRegistry
from arthas_mcp_proxy.models import ErrorCode
from arthas_mcp_proxy.target_state import TargetIdentity

OPAQUE_HANDLE_RE = re.compile(r"^jvm_[0-9a-f]{16,}$")

TARGET_KEY = "ops@10.0.0.8:22"
PID = 4242
START_TIME = "17000"
BOOT_ID = "2f4c1b6a-9d3e-4a10-8c2b-77e0d1a2b3c4"
APP_NAME = "inventory-service.jar"


class _FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _mint(registry: JvmRegistry, **overrides: object) -> str:
    kwargs: dict[str, object] = {
        "target_key": TARGET_KEY,
        "pid": PID,
        "start_time": START_TIME,
        "boot_id": BOOT_ID,
        "application_name": APP_NAME,
    }
    kwargs.update(overrides)
    return registry.mint(**kwargs)  # type: ignore[arg-type]


@pytest.mark.unit
def test_b3_1_a_unique_mint_is_opaque_not_reversible() -> None:
    """B3-1-a: minted handle is jvm_ + hex and not the TargetIdentity string."""
    registry = JvmRegistry()
    handle = _mint(registry)
    reversible = TargetIdentity("10.0.0.8", 22, "ops", PID, START_TIME).handle

    assert OPAQUE_HANDLE_RE.fullmatch(handle)
    assert handle != reversible
    assert not handle.startswith("jvm:")


@pytest.mark.unit
def test_b3_1_b_resolve_hit_returns_binding_and_slides_expiry() -> None:
    """B3-1-b: resolve returns identity fields and renews last_used / expires."""
    clock = _FakeClock(1_000.0)
    registry = JvmRegistry(ttl_seconds=1_800, clock=clock)
    handle = _mint(registry)

    first = registry.resolve(handle, target_key=TARGET_KEY)
    assert first.handle == handle
    assert first.pid == PID
    assert first.start_time == START_TIME
    assert first.boot_id == BOOT_ID
    assert first.application_name == APP_NAME
    assert first.target_key == TARGET_KEY
    first_used = first.last_used_at
    first_expires = first.expires_at
    assert first_expires == first_used + 1_800

    clock.advance(50.0)
    second = registry.resolve(handle, target_key=TARGET_KEY)
    assert second.pid == PID
    assert second.start_time == START_TIME
    assert second.boot_id == BOOT_ID
    assert second.application_name == APP_NAME
    assert second.target_key == TARGET_KEY
    assert second.last_used_at > first_used
    assert second.expires_at > first_expires
    assert second.last_used_at == clock.now
    assert second.expires_at == clock.now + 1_800


@pytest.mark.unit
def test_b3_1_c_unknown_handle_is_handle_not_found() -> None:
    """B3-1-c: unknown handle raises HANDLE_NOT_FOUND, not INVALID_ARGUMENT."""
    registry = JvmRegistry()
    with pytest.raises(DomainError) as exc_info:
        registry.resolve("jvm_deadbeefdeadbeef", target_key=TARGET_KEY)
    assert exc_info.value.code is ErrorCode.HANDLE_NOT_FOUND
    assert exc_info.value.code is not ErrorCode.INVALID_ARGUMENT


@pytest.mark.unit
def test_b3_1_d_expired_handle_is_handle_expired_without_pid() -> None:
    """B3-1-d: clock past TTL → HANDLE_EXPIRED and must not return the old pid."""
    clock = _FakeClock(1_000.0)
    registry = JvmRegistry(ttl_seconds=1_800, clock=clock)
    handle = _mint(registry)
    minted = registry.resolve(handle, target_key=TARGET_KEY)
    clock.advance(1_800.0)
    assert clock.now >= minted.expires_at

    with pytest.raises(DomainError) as exc_info:
        leaked = registry.resolve(handle, target_key=TARGET_KEY)
        pytest.fail(f"resolve must not return expired binding pid={leaked.pid}")
    assert exc_info.value.code is ErrorCode.HANDLE_EXPIRED
    assert str(PID) not in exc_info.value.message


@pytest.mark.unit
def test_b3_1_e_wrong_session_is_handle_session_mismatch_without_leak() -> None:
    """B3-1-e: target A handle vs session B → HANDLE_SESSION_MISMATCH, no leak."""
    registry = JvmRegistry()
    handle = _mint(registry)

    with pytest.raises(DomainError) as exc_info:
        leaked = registry.resolve(handle, target_key="other@10.0.0.9:22")
        pytest.fail(f"resolve must not return mismatched binding pid={leaked.pid}")
    assert exc_info.value.code is ErrorCode.HANDLE_SESSION_MISMATCH
    assert str(PID) not in exc_info.value.message
    assert APP_NAME not in exc_info.value.message
    assert getattr(exc_info.value, "binding", None) is None


@pytest.mark.unit
def test_b3_1_f_same_jvm_reuses_unexpired_handle() -> None:
    """B3-1-f: two mint/find calls for the same JVM return the same handle."""
    registry = JvmRegistry()
    first = _mint(registry)
    second = _mint(registry)
    assert first == second
    assert OPAQUE_HANDLE_RE.fullmatch(first)

@pytest.mark.unit
def test_b3_1_identity_key_includes_boot_id_and_start_time() -> None:
    """Different start_time or boot_id is a different JVM and gets a new handle."""
    registry = JvmRegistry()
    base = _mint(registry)
    other_boot = _mint(registry, boot_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    other_start = _mint(registry, start_time="99999")
    assert other_boot != base
    assert other_start != base
    assert other_boot != other_start
    assert OPAQUE_HANDLE_RE.fullmatch(other_boot)
    assert OPAQUE_HANDLE_RE.fullmatch(other_start)


@pytest.mark.unit
def test_b3_1_expired_identity_remints_different_handle() -> None:
    """After TTL, minting the same identity issues a new handle."""
    clock = _FakeClock(1_000.0)
    registry = JvmRegistry(ttl_seconds=1_800, clock=clock)
    first = _mint(registry)
    clock.advance(1_800.0)
    second = _mint(registry)
    assert first != second
    assert OPAQUE_HANDLE_RE.fullmatch(second)
    with pytest.raises(DomainError) as exc_info:
        registry.resolve(first, target_key=TARGET_KEY)
    assert exc_info.value.code is ErrorCode.HANDLE_EXPIRED
    rebound = registry.resolve(second, target_key=TARGET_KEY)
    assert rebound.pid == PID
    assert rebound.handle == second


@pytest.mark.unit
def test_b3_1_mint_reuse_slides_ttl() -> None:
    """Reusing an unexpired handle on mint slides last_used / expires."""
    clock = _FakeClock(1_000.0)
    registry = JvmRegistry(ttl_seconds=1_800, clock=clock)
    handle = _mint(registry)
    first = registry.resolve(handle, target_key=TARGET_KEY)
    clock.advance(1_799.0)
    reused = _mint(registry)
    assert reused == handle
    clock.advance(1.0)
    still_live = registry.resolve(handle, target_key=TARGET_KEY)
    assert still_live.expires_at == clock.now + 1_800
    assert still_live.last_used_at == clock.now
    assert still_live.expires_at > first.expires_at

