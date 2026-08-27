"""Contract tests for the watch observation policy (arthas_mcp_proxy.observation_policy).

``ObservationPolicy`` bounds a watch observation session: a maximum number
of observations (``max_times``), a concurrency cap on simultaneous
observations (``max_concurrent``), and a session time-to-live
(``ttl_seconds``).  A7a: policy module contract only - no integration yet.
"""

from __future__ import annotations

import threading
import time

import pytest

from arthas_mcp_proxy.observation_policy import ObservationPolicy


@pytest.mark.contract
def test_policy_holds_configuration() -> None:
    policy = ObservationPolicy(max_times=5, max_concurrent=2, ttl_seconds=60)
    assert policy.max_times == 5
    assert policy.max_concurrent == 2
    assert policy.ttl_seconds == 60


@pytest.mark.contract
def test_validate_times_within_bounds_succeeds() -> None:
    policy = ObservationPolicy(max_times=5, max_concurrent=2, ttl_seconds=60)
    policy.validate_times(1, 5)


@pytest.mark.contract
def test_watch_times_alias() -> None:
    policy = ObservationPolicy(max_times=5, max_concurrent=2, ttl_seconds=60)
    policy.validate_watch_times(5)  # equivalent to validate_times(1, 5)
    with pytest.raises(ValueError):
        policy.validate_watch_times(6)


@pytest.mark.contract
def test_validate_times_out_of_bounds_raises_value_error() -> None:
    policy = ObservationPolicy(max_times=5, max_concurrent=2, ttl_seconds=60)
    with pytest.raises(ValueError):
        policy.validate_times(0, 6)


@pytest.mark.contract
def test_acquire_blocks_at_concurrency_limit() -> None:
    policy = ObservationPolicy(max_times=5, max_concurrent=2, ttl_seconds=60)
    policy.acquire()
    policy.acquire()

    acquired = threading.Event()

    def try_acquire() -> None:
        policy.acquire()
        acquired.set()

    thread = threading.Thread(target=try_acquire)
    thread.start()
    time.sleep(0.05)
    assert not acquired.is_set(), "third acquire must block at the concurrency limit"

    policy.release()
    thread.join(timeout=2)
    assert acquired.is_set(), "blocked acquire proceeds once a slot is released"


@pytest.mark.contract
def test_context_manager_acquires_and_releases() -> None:
    policy = ObservationPolicy(max_times=5, max_concurrent=2, ttl_seconds=60)
    policy.acquire()  # occupy the first concurrent slot

    with policy:  # second slot - now at the concurrency limit
        blocked = threading.Event()

        def try_acquire() -> None:
            policy.acquire()
            blocked.set()

        thread = threading.Thread(target=try_acquire)
        thread.start()
        time.sleep(0.05)
        assert not blocked.is_set(), "context-held slot must count toward concurrency"

    # exiting the context released its slot
    thread.join(timeout=2)
    assert blocked.is_set(), "released slot unblocks a pending acquire"


@pytest.mark.contract
def test_context_manager_releases_slot_after_ttl() -> None:
    policy = ObservationPolicy(max_times=5, max_concurrent=1, ttl_seconds=0.05)
    acquired = threading.Event()

    with policy:

        def try_acquire() -> None:
            policy.acquire()
            acquired.set()

        thread = threading.Thread(target=try_acquire)
        thread.start()
        assert not acquired.wait(timeout=0.02)
        assert acquired.wait(timeout=0.2), "TTL must release a hung observation slot"

    thread.join(timeout=1)
