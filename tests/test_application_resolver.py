import pytest

from arthas_mcp_proxy.application_resolver import (
    ApplicationCandidate,
    find_java_application,
    is_same_process,
    validate_process_identity,
)
from arthas_mcp_proxy.errors import DomainError
from arthas_mcp_proxy.models import ErrorCode


@pytest.mark.contract
def test_find_java_application_returns_matching_candidate() -> None:
    lines = ["1234 com.example.Other", "5678 com.example.OrderService"]
    result = find_java_application(lines, "OrderService")
    assert result.pid == 5678
    assert result.command == "com.example.OrderService"


@pytest.mark.contract
def test_find_java_application_returns_application_candidate() -> None:
    result = find_java_application(["5678 com.example.OrderService"], "OrderService")
    assert isinstance(result, ApplicationCandidate)
    assert result.pid == 5678
    assert result.command == "com.example.OrderService"
    assert result.owner is None
    assert result.start_time is None


@pytest.mark.contract
def test_find_java_application_parses_owner_and_start_time() -> None:
    result = find_java_application(
        ["5678 appuser 2026-08-01T10:00:00 com.example.OrderService"], "OrderService"
    )
    assert result.pid == 5678
    assert result.owner == "appuser"
    assert result.start_time == "2026-08-01T10:00:00"
    assert result.command == "com.example.OrderService"


@pytest.mark.contract
def test_application_candidate_declares_all_fields() -> None:
    candidate = ApplicationCandidate(
        pid=42, command="com.example.Service", owner="appuser", start_time="2026-08-01 10:00:00"
    )
    assert candidate.pid == 42
    assert candidate.command == "com.example.Service"
    assert candidate.owner == "appuser"
    assert candidate.start_time == "2026-08-01 10:00:00"


@pytest.mark.contract
def test_find_java_application_raises_not_found() -> None:
    with pytest.raises(DomainError) as exc_info:
        find_java_application(["1234 com.example.Other"], "Missing")
    assert exc_info.value.code is ErrorCode.JVM_NOT_FOUND


@pytest.mark.contract
def test_find_java_application_raises_ambiguous_when_name_appears_twice() -> None:
    lines = ["1234 com.example.OrderService", "5678 com.example.OrderService"]
    with pytest.raises(DomainError) as exc_info:
        find_java_application(lines, "OrderService")
    assert exc_info.value.code is ErrorCode.JVM_AMBIGUOUS
    assert "1234" in exc_info.value.message
    assert "5678" in exc_info.value.message


@pytest.mark.contract
def test_application_candidate_identity_key_includes_pid_and_start_time() -> None:
    candidate = ApplicationCandidate(
        pid=42, command="com.example.Service", start_time="2026-08-01 10:00:00"
    )
    key = candidate.identity_key()
    assert key == (42, "2026-08-01 10:00:00")


@pytest.mark.contract
def test_application_candidate_identity_key_differs_for_same_pid_different_start_time() -> None:
    first = ApplicationCandidate(
        pid=42, command="com.example.Service", start_time="2026-08-01 10:00:00"
    )
    second = ApplicationCandidate(
        pid=42, command="com.example.Service", start_time="2026-08-01 10:00:01"
    )
    assert first.identity_key() == (42, "2026-08-01 10:00:00")
    assert second.identity_key() == (42, "2026-08-01 10:00:01")
    assert first.identity_key() != second.identity_key()


@pytest.mark.contract
def test_find_java_application_parses_process_metadata() -> None:
    result = find_java_application(
        ["5678 appuser 2026-08-01T10:00:00 com.example.OrderService"], "OrderService"
    )
    assert result.pid == 5678
    assert result.owner == "appuser"
    assert result.start_time == "2026-08-01T10:00:00"
    assert "OrderService" in result.command


@pytest.mark.contract
def test_is_same_process_true_when_pid_and_start_time_match() -> None:
    candidate = ApplicationCandidate(
        pid=42, command="com.example.Service", start_time="2026-08-01 10:00:00"
    )
    assert is_same_process(candidate, pid=42, start_time="2026-08-01 10:00:00") is True


@pytest.mark.contract
def test_is_same_process_false_when_start_time_differs() -> None:
    candidate = ApplicationCandidate(
        pid=42, command="com.example.Service", start_time="2026-08-01 10:00:00"
    )
    assert is_same_process(candidate, pid=42, start_time="2026-08-01 10:00:01") is False


@pytest.mark.contract
def test_is_same_process_false_when_pid_differs() -> None:
    candidate = ApplicationCandidate(
        pid=42, command="com.example.Service", start_time="2026-08-01 10:00:00"
    )
    assert is_same_process(candidate, pid=43, start_time="2026-08-01 10:00:00") is False


@pytest.mark.contract
def test_is_same_process_raises_when_candidate_start_time_missing() -> None:
    candidate = ApplicationCandidate(pid=42, command="com.example.Service", start_time=None)
    with pytest.raises(DomainError) as exc_info:
        is_same_process(candidate, pid=42, start_time="2026-08-01 10:00:00")
    assert exc_info.value.code is ErrorCode.JVM_IDENTITY_CHANGED


@pytest.mark.unit
def test_validate_process_identity_accepts_same_jvm() -> None:
    result = validate_process_identity(
        ["PID 42: appuser 2026-08-01T10:00:00 com.example.Service"],
        42,
        "2026-08-01T10:00:00",
    )
    assert result.pid == 42


@pytest.mark.unit
def test_validate_process_identity_detects_pid_reuse() -> None:
    with pytest.raises(DomainError) as exc_info:
        validate_process_identity(
            ["PID 42: appuser 2026-08-01T10:00:01 com.example.Service"],
            42,
            "2026-08-01T10:00:00",
        )
    assert exc_info.value.code is ErrorCode.JVM_IDENTITY_CHANGED


@pytest.mark.unit
def test_validate_process_identity_detects_exit() -> None:
    with pytest.raises(DomainError) as exc_info:
        validate_process_identity([], 42, "2026-08-01T10:00:00")
    assert exc_info.value.code is ErrorCode.JVM_EXITED


@pytest.mark.unit
def test_validate_process_identity_rejects_missing_runtime_start_time() -> None:
    with pytest.raises(DomainError) as exc_info:
        validate_process_identity(["42 com.example.Service"], 42, "2026-08-01T10:00:00")
    assert exc_info.value.code is ErrorCode.JVM_IDENTITY_CHANGED
