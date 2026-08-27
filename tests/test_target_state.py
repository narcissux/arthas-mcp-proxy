import pytest

from arthas_mcp_proxy.target_state import TargetIdentity, make_identity, parse_handle, state_key


@pytest.mark.contract
def test_same_pid_different_targets_are_distinct() -> None:
    first = TargetIdentity("host-a", 22, "root", 1234)
    second = TargetIdentity("host-b", 22, "root", 1234)
    assert first != second
    assert state_key(first) != state_key(second)


@pytest.mark.contract
def test_make_identity_returns_target_identity() -> None:
    identity = make_identity("host-a", 22, "root", 1234)
    assert isinstance(identity, TargetIdentity)
    assert identity.host == "host-a"
    assert identity.port == 22
    assert identity.username == "root"
    assert identity.pid == 1234
    assert identity == TargetIdentity("host-a", 22, "root", 1234)
    assert state_key(identity) == ("host-a", 22, "root", 1234)


@pytest.mark.contract
def test_jvm_handle_round_trips_identity() -> None:
    identity = TargetIdentity("host", 22, "user", 123, "1000")
    assert parse_handle(identity.handle) == identity


@pytest.mark.contract
def test_jvm_handle_rejects_malformed_value() -> None:
    with pytest.raises(ValueError, match="invalid jvm handle"):
        parse_handle("not-a-handle")
