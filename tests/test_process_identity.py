from arthas_mcp_proxy.target_state import TargetIdentity, state_key


def test_process_start_time_separates_same_pid_identity() -> None:
    first = TargetIdentity("host", 22, "root", 123, "100.0")
    restarted = TargetIdentity("host", 22, "root", 123, "200.0")

    assert first != restarted
    assert state_key(first) != state_key(restarted)


def test_legacy_identity_without_start_time_remains_valid() -> None:
    identity = TargetIdentity("host", 22, "root", 123)
    assert identity.start_time is None
