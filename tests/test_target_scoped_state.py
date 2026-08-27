"""Target-scoped attach lock state.

Verifies that attach locks and per-PID state are scoped per target identity
(host/port/user/pid) rather than keyed on PID alone, so concurrent diagnostics
against the same PID on different hosts do not contend on one lock or share
one cache entry.
"""

from contextlib import ExitStack
from typing import cast
from unittest.mock import MagicMock, patch

from arthas_mcp_proxy.arthas_client import (
    _ATTACH_LOCKS,
    _PID_STATE,
    _PID_STATE_LOCK,
    ArthasClient,
    _ensure_agent,
    _get_attach_lock,
    _state_key,
)
from arthas_mcp_proxy.target_state import TargetIdentity


def _fake_session(host: str, port: int, username: str) -> MagicMock:
    """Build a fake SSH session that exposes host/port/username."""
    session = MagicMock()
    session.host = host
    session.port = port
    session.username = username
    return session


def test_target_scoped_attach_locks() -> None:
    # Start from a clean lock map so earlier tests cannot leak keys in here.
    _ATTACH_LOCKS.clear()

    identity_a = TargetIdentity(host="host-a", port=22, username="u", pid=123)
    identity_b = TargetIdentity(host="host-b", port=22, username="u", pid=123)

    lock_a = _get_attach_lock(identity_a)
    lock_b = _get_attach_lock(identity_b)

    # Same PID on different hosts must resolve to distinct locks.
    assert lock_a is not lock_b

    # Bare-PID lookup must remain usable alongside target identities.
    lock_pid = _get_attach_lock(123)
    assert lock_pid is not None
    assert lock_pid is not lock_a
    assert lock_pid is not lock_b

    # Retrieving the same identity again returns the same lock (stable mapping).
    assert _get_attach_lock(identity_a) is lock_a
    assert _get_attach_lock(identity_b) is lock_b


def test_state_key_is_target_scoped_for_host_session() -> None:
    """Sessions exposing host/port/username yield a TargetIdentity key."""
    key = _state_key(_fake_session("host-a", 22, "root"), 123)
    assert key == TargetIdentity(host="host-a", port=22, username="root", pid=123)

    other = _state_key(_fake_session("host-b", 22, "root"), 123)
    assert key != other
    assert isinstance(key, TargetIdentity)
    assert isinstance(other, TargetIdentity)
    assert key.pid == other.pid == 123


def test_state_key_falls_back_to_bare_pid() -> None:
    """Sessions that do not expose host/port/username key on the PID alone."""
    assert _state_key(MagicMock(), 123) == 123


def test_state_key_includes_application_start_time_when_available() -> None:
    """Resolver metadata separates a restarted JVM reusing the same PID."""
    session = _fake_session("host-a", 22, "root")
    assert _state_key(session, 123, "100.0") == TargetIdentity(
        host="host-a", port=22, username="root", pid=123, start_time="100.0"
    )


def test_state_key_keeps_legacy_session_identity_without_start_time() -> None:
    """Missing resolver metadata preserves the legacy target key."""
    session = _fake_session("host-a", 22, "root")
    assert _state_key(session, 123) == TargetIdentity(
        host="host-a", port=22, username="root", pid=123
    )


def test_pid_state_cache_is_target_scoped() -> None:
    """Two clients, same PID, different hosts must not share _PID_STATE."""
    _PID_STATE.clear()

    client_a = ArthasClient(_fake_session("host-a", 22, "root"))
    client_b = ArthasClient(_fake_session("host-b", 22, "root"))

    with patch("arthas_mcp_proxy.arthas_client._detect_arthas_port", return_value=3661):
        port_a = _ensure_agent(client_a.session, 1234, "/tmp/as.sh")  # noqa: S108
        port_b = _ensure_agent(client_b.session, 1234, "/tmp/as.sh")  # noqa: S108

    assert port_a == 3661
    assert port_b == 3661

    with _PID_STATE_LOCK:
        # Same PID on different hosts resolves to two distinct cache entries.
        assert len(_PID_STATE) == 2
        keys = list(_PID_STATE)
        assert all(isinstance(k, TargetIdentity) for k in keys)
        identities = [cast("TargetIdentity", k) for k in keys]
        assert {k.host for k in identities} == {"host-a", "host-b"}
        assert {k.pid for k in identities} == {1234}


def _detach_remote_patches() -> tuple[object, ...]:
    """Patch every helper that would reach the network during a detach."""
    return (
        patch("arthas_mcp_proxy.arthas_client._exec_ssh", return_value=("", "", 0)),
        patch("arthas_mcp_proxy.arthas_client._detect_arthas_port", return_value=None),
        patch(
            "arthas_mcp_proxy.arthas_client._find_arthas_path",
            return_value="/tmp/as.sh",  # noqa: S108
        ),
        patch(
            "arthas_mcp_proxy.arthas_client._find_arthas_client_jar",
            return_value="/tmp/arthas-client.jar",  # noqa: S108
        ),
    )


def test_detach_lookup_and_removal_are_target_scoped() -> None:
    """Same PID on different hosts: detach must use and remove its own cache key."""
    _PID_STATE.clear()

    client_a = ArthasClient(_fake_session("host-a", 22, "root"))
    client_b = ArthasClient(_fake_session("host-b", 22, "root"))
    pid = 1234

    key_a = _state_key(client_a.session, pid)
    key_b = _state_key(client_b.session, pid)
    assert key_a != key_b

    with _PID_STATE_LOCK:
        _PID_STATE[key_a] = {"port": 3658, "owner": None}
        _PID_STATE[key_b] = {"port": 3659, "owner": None}

    with ExitStack() as stack:
        patches = [stack.enter_context(p) for p in _detach_remote_patches()]
        mock_detect = patches[1]
        result_a = client_a.detach(pid)

    # Cache lookup hit host-a's own key (port 3658); no ss scan was needed.
    assert "3658" in result_a
    mock_detect.assert_not_called()

    with _PID_STATE_LOCK:
        # Only host-a's entry is removed; host-b keeps its own cache entry.
        assert key_a not in _PID_STATE
        assert key_b in _PID_STATE
        assert _PID_STATE[key_b]["port"] == 3659


def test_detach_accepts_legacy_bare_pid_cache() -> None:
    """Callers that prepopulate _PID_STATE[pid] (bare int) are still honored."""
    _PID_STATE.clear()

    client = ArthasClient(_fake_session("host-a", 22, "root"))
    pid = 1234

    with _PID_STATE_LOCK:
        _PID_STATE[pid] = {"port": 3658, "owner": None}

    with ExitStack() as stack:
        patches = [stack.enter_context(p) for p in _detach_remote_patches()]
        mock_detect = patches[1]
        result = client.detach(pid)

    assert "3658" in result
    mock_detect.assert_not_called()

    with _PID_STATE_LOCK:
        assert pid not in _PID_STATE
