"""B4-2: per-JVM concurrent observation limits."""

from __future__ import annotations

import json
import threading
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.observation_policy import (
    OBSERVATION_MAX_ACTIVE_PER_JVM,
    ObservationPolicy,
    is_observation_command,
    observation_jvm_key,
)
from arthas_mcp_proxy.server import cancel_diagnostic_job, exec_command, watch_method
from arthas_mcp_proxy.ssh_pool import get_connection_pool
from arthas_mcp_proxy.target_state import target_key

_watch_method_impl = cast("Any", watch_method).__wrapped__

HOST = "10.0.0.8"
PORT = 22
USER = "ops"
PID_A = 4242
PID_B = 9999
START_A = "17000"
START_B = "20000"
BOOT_A = "boot-old"
BOOT_B = "boot-new"


def _session(
    *,
    start_time: str | None = START_A,
    boot_id: str | None = BOOT_A,
    host: str = HOST,
    port: int = PORT,
    username: str = USER,
) -> MagicMock:
    session = MagicMock()
    session.host = host
    session.port = port
    session.username = username
    session.start_time = start_time
    session.boot_id = boot_id
    return session


def _key(
    pid: int = PID_A,
    start_time: str | None = START_A,
    boot_id: str | None = BOOT_A,
) -> tuple[str, int, str | None, str | None]:
    return (target_key(HOST, PORT, USER), pid, start_time, boot_id)


def _error_code(result: str) -> str:
    payload = json.loads(result)
    assert payload["isError"] is True
    return str(payload["structuredContent"]["error"]["code"])


@pytest.mark.unit
def test_is_observation_command_detects_watch_and_trace() -> None:
    assert is_observation_command("watch com.Foo bar -n 1") is True
    assert is_observation_command("trace com.Foo bar") is True
    assert is_observation_command("  TRACE com.Foo bar") is True
    assert is_observation_command("jvm") is False
    assert is_observation_command("thread -n 5") is False
    assert is_observation_command("") is False


@pytest.mark.unit
def test_observation_jvm_key_is_identity_aware() -> None:
    session = _session()
    assert observation_jvm_key(session, PID_A) == _key()
    reused = _session(start_time=START_B, boot_id=BOOT_B)
    assert observation_jvm_key(reused, PID_A) != observation_jvm_key(session, PID_A)
    other_pid = observation_jvm_key(session, PID_B)
    assert other_pid != observation_jvm_key(session, PID_A)
    assert other_pid[1] == PID_B


@pytest.mark.unit
def test_b4_2_a_fourth_acquire_same_jvm_raises() -> None:
    """B4-2-a: 3 acquires on one key succeed; the 4th raises immediately."""
    policy = ObservationPolicy()
    key = _key()
    assert OBSERVATION_MAX_ACTIVE_PER_JVM == 3
    policy.acquire_jvm(key)
    policy.acquire_jvm(key)
    policy.acquire_jvm(key)
    with pytest.raises(ValueError, match="OBSERVATION_LIMIT_EXCEEDED"):
        policy.acquire_jvm(key)


@pytest.mark.unit
def test_b4_2_a_for_jvm_fourth_raises() -> None:
    policy = ObservationPolicy()
    key = _key()
    with (
        policy.for_jvm(key),
        policy.for_jvm(key),
        policy.for_jvm(key),
        pytest.raises(ValueError, match="OBSERVATION_LIMIT_EXCEEDED"),
    ):
        policy.acquire_jvm(key)


def _success_output(raw: str) -> str:
    payload = json.loads(raw)
    assert payload["isError"] is False
    return str(payload["structuredContent"]["data"]["output"])


def _running_job_id(raw: str) -> str:
    payload = json.loads(raw)
    assert payload["isError"] is False
    assert payload["structuredContent"]["status"] == "running"
    return str(payload["structuredContent"]["data"]["job_id"])


@pytest.mark.unit
def test_b4_2_a_watch_method_fourth_held_slot_is_limit_exceeded() -> None:
    """B4-2-a: three running watch_method jobs hold the cap; a 4th fails fast."""
    policy = ObservationPolicy()
    session = _session()
    release = threading.Event()
    # 3 workers + main: do not hit the 4th until all 3 streams have entered.
    started = threading.Barrier(4)
    client = MagicMock()

    def _block(*_args: object, **_kwargs: object) -> str:
        started.wait(timeout=2)
        assert release.wait(timeout=2)
        return "watch output"

    client.execute_streaming_command.side_effect = _block
    job_ids: list[str] = []

    with (
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", policy),
    ):
        for _ in range(3):
            job_ids.append(
                _running_job_id(
                    _watch_method_impl(session, PID_A, "com.Foo", "bar", times=1, await_ms=0)
                )
            )
        started.wait(timeout=2)
        fourth = _watch_method_impl(session, PID_A, "com.Foo", "bar", times=1, await_ms=0)
        release.set()
        for job_id in job_ids:
            cancel_diagnostic_job(job_id)

    assert _error_code(fourth) == "OBSERVATION_LIMIT_EXCEEDED"
    assert client.execute_streaming_command.call_count == 3
    client.watch_method.assert_not_called()


@pytest.mark.unit
def test_b4_2_b_release_frees_slot() -> None:
    """B4-2-b: after releasing one of three holds, another acquire succeeds."""
    policy = ObservationPolicy()
    key = _key()
    policy.acquire_jvm(key)
    policy.acquire_jvm(key)
    policy.acquire_jvm(key)
    policy.release_jvm(key)
    policy.acquire_jvm(key)
    with pytest.raises(ValueError, match="OBSERVATION_LIMIT_EXCEEDED"):
        policy.acquire_jvm(key)


@pytest.mark.unit
def test_b4_2_b_context_exit_releases_slot() -> None:
    policy = ObservationPolicy()
    key = _key()
    policy.acquire_jvm(key)
    policy.acquire_jvm(key)
    with policy.for_jvm(key), pytest.raises(ValueError, match="OBSERVATION_LIMIT_EXCEEDED"):
        policy.acquire_jvm(key)
    policy.acquire_jvm(key)


@pytest.mark.unit
def test_b4_2_c_other_jvm_is_not_capped() -> None:
    """B4-2-c: a full JVM-A does not block JVM-B (different pid)."""
    policy = ObservationPolicy()
    key_a = _key(pid=PID_A)
    key_b = _key(pid=PID_B)
    policy.acquire_jvm(key_a)
    policy.acquire_jvm(key_a)
    policy.acquire_jvm(key_a)
    policy.acquire_jvm(key_b)
    with pytest.raises(ValueError, match="OBSERVATION_LIMIT_EXCEEDED"):
        policy.acquire_jvm(key_a)


@pytest.mark.unit
def test_b4_2_c_pid_reuse_different_identity_is_other_jvm() -> None:
    """B4-2-c: same pid with a new start_time/boot_id is a different JVM."""
    policy = ObservationPolicy()
    key_a = _key(start_time=START_A, boot_id=BOOT_A)
    key_reused = _key(start_time=START_B, boot_id=BOOT_B)
    assert key_a != key_reused
    policy.acquire_jvm(key_a)
    policy.acquire_jvm(key_a)
    policy.acquire_jvm(key_a)
    policy.acquire_jvm(key_reused)


@pytest.mark.unit
def test_b4_2_d_exec_command_watch_uses_per_jvm_limit() -> None:
    """B4-2-d: exec_command watch/trace cannot bypass the per-JVM cap."""
    policy = ObservationPolicy()
    session = _session()
    key = observation_jvm_key(session, PID_A)
    policy.acquire_jvm(key)
    policy.acquire_jvm(key)
    policy.acquire_jvm(key)

    client = MagicMock()
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", policy),
    ):
        result = exec_command(
            session_id="session",
            pid=PID_A,
            command="watch com.Foo bar -n 1",
        )

    assert _error_code(result) == "OBSERVATION_LIMIT_EXCEEDED"
    client.exec_command.assert_not_called()


@pytest.mark.unit
def test_b4_2_d_exec_command_non_watch_still_succeeds() -> None:
    """B4-2-d: a non-observation expert command is not gated by the cap."""
    policy = ObservationPolicy()
    session = _session()
    key = observation_jvm_key(session, PID_A)
    policy.acquire_jvm(key)
    policy.acquire_jvm(key)
    policy.acquire_jvm(key)

    client = MagicMock()
    client.exec_command.return_value = "jvm output"
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", policy),
    ):
        assert exec_command(session_id="session", pid=PID_A, command="jvm") == "jvm output"
    client.exec_command.assert_called_once()


@pytest.mark.unit
def test_b4_2_sequential_watch_method_releases_slot() -> None:
    """Completing watch_method must release so a later 4th sequential call works."""
    policy = ObservationPolicy()
    session = _session()
    client = MagicMock()
    client.execute_streaming_command.return_value = "watch output"
    with (
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", policy),
    ):
        for _ in range(4):
            assert (
                _success_output(_watch_method_impl(session, PID_A, "com.Foo", "bar", times=1))
                == "watch output"
            )
    assert client.execute_streaming_command.call_count == 4
    client.watch_method.assert_not_called()


@pytest.mark.unit
def test_b4_2_failed_watch_method_releases_slot() -> None:
    policy = ObservationPolicy()
    session = _session()
    client = MagicMock()
    client.execute_streaming_command.side_effect = TimeoutError("backend timeout")
    with (
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", policy),
    ):
        for _ in range(3):
            assert (
                _error_code(_watch_method_impl(session, PID_A, "com.Foo", "bar", times=1))
                == "COMMAND_TIMEOUT"
            )
        client.execute_streaming_command.side_effect = None
        client.execute_streaming_command.return_value = "watch output"
        assert (
            _success_output(_watch_method_impl(session, PID_A, "com.Foo", "bar", times=1))
            == "watch output"
        )


@pytest.mark.unit
def test_b4_2_ttl_releases_per_jvm_lease() -> None:
    policy = ObservationPolicy(ttl_seconds=0.05)
    key = _key()
    policy.acquire_jvm(key)
    policy.acquire_jvm(key)
    policy.acquire_jvm(key)
    with pytest.raises(ValueError, match="OBSERVATION_LIMIT_EXCEEDED"):
        policy.acquire_jvm(key)
    threading.Event().wait(0.2)
    policy.acquire_jvm(key)


@pytest.mark.unit
def test_b4_2_d_exec_command_trace_uses_per_jvm_limit() -> None:
    """B4-2-d: exec_command TRACE shares the same per-JVM cap as watch."""
    policy = ObservationPolicy()
    session = _session()
    key = observation_jvm_key(session, PID_A)
    policy.acquire_jvm(key)
    policy.acquire_jvm(key)
    policy.acquire_jvm(key)
    client = MagicMock()
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", policy),
    ):
        result = exec_command(
            session_id="session",
            pid=PID_A,
            command="TRACE com.Foo bar",
        )
    assert _error_code(result) == "OBSERVATION_LIMIT_EXCEEDED"
    client.exec_command.assert_not_called()


@pytest.mark.unit
def test_b4_2_c_watch_method_other_jvm_succeeds() -> None:
    """B4-2-c MCP: JVM-A holds 3 running watches; JVM-B first watch_method succeeds."""
    policy = ObservationPolicy()
    session_a = _session()
    session_b = _session()
    release = threading.Event()
    client = MagicMock()

    def _stream(*_args: object, **_kwargs: object) -> str:
        command = ""
        if len(_args) >= 2:
            command = str(_args[1])
        if "com.Bar" in command:
            return "watch-b"
        assert release.wait(timeout=2)
        return "watch-a"

    client.execute_streaming_command.side_effect = _stream
    job_ids: list[str] = []

    with (
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", policy),
    ):
        for _ in range(3):
            job_ids.append(
                _running_job_id(
                    _watch_method_impl(session_a, PID_A, "com.Foo", "bar", times=1, await_ms=0)
                )
            )
        other = _watch_method_impl(session_b, PID_B, "com.Bar", "baz", times=1)
        release.set()
        for job_id in job_ids:
            cancel_diagnostic_job(job_id)

    assert _success_output(other) == "watch-b"


@pytest.mark.unit
def test_b4_2_handle_watch_and_session_exec_share_key() -> None:
    """Handle-watch and session+pid exec share one identity key."""
    from arthas_mcp_proxy.jvm_registry import JvmRegistry, reset_jvm_registry

    registry = JvmRegistry()
    reset_jvm_registry(registry)
    registry.mint(
        target_key=target_key(HOST, PORT, USER),
        pid=PID_A,
        start_time=START_A,
        boot_id=BOOT_A,
        application_name="app.jar",
    )
    policy = ObservationPolicy()
    seeded = _session(start_time=START_A, boot_id=BOOT_A)
    bare = _session(start_time=None, boot_id=None)
    assert observation_jvm_key(seeded, PID_A) == observation_jvm_key(bare, PID_A)
    policy.acquire_jvm(observation_jvm_key(seeded, PID_A))
    policy.acquire_jvm(observation_jvm_key(seeded, PID_A))
    policy.acquire_jvm(observation_jvm_key(seeded, PID_A))
    client = MagicMock()
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session", return_value=bare),
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", policy),
    ):
        result = exec_command(
            session_id="session",
            pid=PID_A,
            command="watch com.Foo bar -n 1",
        )
    assert _error_code(result) == "OBSERVATION_LIMIT_EXCEEDED"
    client.exec_command.assert_not_called()


@pytest.mark.unit
def test_b4_2_a_trace_method_fourth_held_slot_is_limit_exceeded() -> None:
    """B4-2-a: three running trace_method jobs hold the cap; a 4th fails fast."""
    from arthas_mcp_proxy.server import trace_method

    _trace_impl = cast("Any", trace_method).__wrapped__
    policy = ObservationPolicy()
    session = _session()
    release = threading.Event()
    # 3 workers + main: do not hit the 4th until all 3 streams have entered.
    started = threading.Barrier(4)
    client = MagicMock()

    def _block(*_args: object, **_kwargs: object) -> str:
        started.wait(timeout=2)
        assert release.wait(timeout=2)
        return "trace output"

    client.execute_streaming_command.side_effect = _block
    job_ids: list[str] = []

    with (
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", policy),
    ):
        for _ in range(3):
            job_ids.append(
                _running_job_id(
                    _trace_impl(session, PID_A, "com.Foo", "bar", times=1, ttl=60, await_ms=0)
                )
            )
        started.wait(timeout=2)
        fourth = _trace_impl(session, PID_A, "com.Foo", "bar", times=1, ttl=60, await_ms=0)
        release.set()
        for job_id in job_ids:
            cancel_diagnostic_job(job_id)

    assert _error_code(fourth) == "OBSERVATION_LIMIT_EXCEEDED"
    assert client.execute_streaming_command.call_count == 3
    client.trace_method.assert_not_called()
