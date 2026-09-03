"""B6-c docker live: two SSH targets, handles must not cross-hit."""

from __future__ import annotations

import pytest

from tests.integration.test_pid_replacement import (
    APP,
    OPAQUE_HANDLE_RE,
    _data_ok,
    _ensure_single_math_game,
    _error_code,
    _kill_math_game,
    _payload,
)

pytestmark = [pytest.mark.integration, pytest.mark.real_jvm]


def test_b6_c_two_docker_targets_do_not_cross_hit(
    docker_test_targets: dict[str, dict[str, str]],
) -> None:
    """B6-c live: A's handle never dumps B; killing A leaves B intact."""
    from arthas_mcp_proxy.server import (
        connect_ssh,
        find_java_application,
        prepare_arthas,
        thread_dump,
    )
    from arthas_mcp_proxy.ssh_pool import get_connection_pool

    if not docker_test_targets:
        pytest.fail("B6-c live requires --docker-targets; refusing to skip")

    target_a = docker_test_targets["target-a"]
    target_b = docker_test_targets["target-b"]

    line_a = connect_ssh(
        host=target_a["host"],
        port=int(target_a["port"]),
        username=target_a["username"],
        password=target_a["password"],
    )
    line_b = connect_ssh(
        host=target_b["host"],
        port=int(target_b["port"]),
        username=target_b["username"],
        password=target_b["password"],
    )
    assert "Session ID:" in line_a and "Session ID:" in line_b
    session_id_a = line_a.rsplit("Session ID:", 1)[1].strip()
    session_id_b = line_b.rsplit("Session ID:", 1)[1].strip()
    pool = get_connection_pool()
    ssh_a = pool.get_session(session_id_a)
    ssh_b = pool.get_session(session_id_b)
    assert ssh_a is not None and ssh_b is not None

    _ensure_single_math_game(ssh_a)
    _ensure_single_math_game(ssh_b)

    found_a = find_java_application(session_id_a, APP)
    found_b = find_java_application(session_id_b, APP)
    data_a = _data_ok(found_a)
    data_b = _data_ok(found_b)
    assert data_a["status"] == "matched", found_a
    assert data_b["status"] == "matched", found_b
    handle_a = data_a["handle"]
    handle_b = data_b["handle"]
    assert OPAQUE_HANDLE_RE.fullmatch(handle_a), handle_a
    assert OPAQUE_HANDLE_RE.fullmatch(handle_b), handle_b
    assert handle_a != handle_b

    pid_a = data_a["candidates"][0]["pid"]
    pid_b = data_b["candidates"][0]["pid"]

    assert _data_ok(prepare_arthas(jvm_handle=handle_a))["origin"] in {
        "existing",
        "started_by_proxy",
    }
    assert _data_ok(prepare_arthas(jvm_handle=handle_b))["origin"] in {
        "existing",
        "started_by_proxy",
    }

    dump_a = _payload(thread_dump(jvm_handle=handle_a))
    dump_b = _payload(thread_dump(jvm_handle=handle_b))
    assert dump_a["isError"] is False, dump_a
    assert dump_b["isError"] is False, dump_b
    assert dump_a["structuredContent"]["data"]["output"]
    assert dump_b["structuredContent"]["data"]["output"]

    mismatch = thread_dump(jvm_handle=handle_a, session_id=session_id_b)
    assert _error_code(mismatch) == "HANDLE_SESSION_MISMATCH", mismatch

    _kill_math_game(ssh_a)
    stale = thread_dump(jvm_handle=handle_a)
    assert _error_code(stale) in {"JVM_IDENTITY_CHANGED", "JVM_EXITED"}

    still_b = _payload(thread_dump(jvm_handle=handle_b))
    assert still_b["isError"] is False, still_b
    assert still_b["structuredContent"]["data"]["output"]

    assert isinstance(pid_a, int) and isinstance(pid_b, int)
