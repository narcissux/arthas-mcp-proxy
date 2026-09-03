"""B6 docker/real-target e2e for PID replacement via MCP tools.

Unit/contract locks for B6-a–d live in tests/test_handle_pid_reuse.py
(mocked inventory). This module exercises find → prepare → thread_dump
across a real JVM kill+restart on the Docker test target.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from arthas_mcp_proxy.ssh_pool import SSHSession

pytestmark = [pytest.mark.integration, pytest.mark.real_jvm]

OPAQUE_HANDLE_RE = re.compile(r"^jvm_[0-9a-f]{16,}$")
APP = "math-game.jar"


def _payload(raw: str) -> dict:
    return json.loads(raw)


def _data_ok(raw: str) -> dict:
    body = _payload(raw)
    assert body["isError"] is False, body
    return body["structuredContent"]["data"]


def _error_code(raw: str) -> str:
    body = _payload(raw)
    assert body["isError"] is True, body
    return str(body["structuredContent"]["error"]["code"])


_LIST_MATH_GAME = (
    "for d in /proc/[0-9]*; do "
    "pid=${d#/proc/}; "
    "cmd=$(tr '\\0' ' ' < \"$d/cmdline\" 2>/dev/null) || continue; "
    'case "$cmd" in *[j]ava*math-game.jar*) echo "$pid $cmd";; esac; '
    "done"
)


def _math_game_listing(ssh_session: SSHSession) -> str:
    _, stdout, _ = ssh_session.client.exec_command(_LIST_MATH_GAME)
    return stdout.read().decode("utf-8", errors="replace")


def _math_game_pids(listing: str) -> list[int]:
    pids: list[int] = []
    for line in listing.splitlines():
        parts = line.split(None, 1)
        if parts and parts[0].isdigit():
            pids.append(int(parts[0]))
    return pids


def _kill_math_game(ssh_session: SSHSession) -> None:
    """Kill math-game JVMs by /proc cmdline. Never pkill -f (matches this SSH cmdline)."""
    pids = _math_game_pids(_math_game_listing(ssh_session))
    if pids:
        pid_list = " ".join(str(pid) for pid in pids)
        _, stdout, _ = ssh_session.client.exec_command(f"kill -9 {pid_list} || true")
        stdout.channel.recv_exit_status()
    deadline = time.time() + 15
    last = ""
    while time.time() < deadline:
        last = _math_game_listing(ssh_session)
        if not _math_game_pids(last):
            return
        time.sleep(0.3)
    pytest.fail("math-game.jar still alive after kill:\\n" + last)


def _start_math_game(ssh_session: SSHSession) -> None:
    """Start a detached math-game JVM that survives the SSH exec closing."""
    _, stdout, stderr = ssh_session.client.exec_command(
        "setsid nohup java -jar /opt/math-game.jar </dev/null "
        ">/tmp/math-game.out 2>&1 & echo started:$!"
    )
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if "started:" not in out:
        pytest.fail(f"failed to spawn math-game.jar: {out} {err}")
    deadline = time.time() + 30
    last = ""
    while time.time() < deadline:
        last = _math_game_listing(ssh_session)
        if len(_math_game_pids(last)) == 1:
            return
        time.sleep(0.5)
    _, log_out, _ = ssh_session.client.exec_command(
        "tail -n 40 /tmp/math-game.out 2>/dev/null || true"
    )
    log = log_out.read().decode("utf-8", errors="replace")
    pytest.fail("math-game.jar did not come up:\\n" + last + "\\nlog:\\n" + log)


def _ensure_single_math_game(ssh_session: SSHSession) -> None:
    """Leave exactly one /opt/math-game.jar JVM running."""
    pids = _math_game_pids(_math_game_listing(ssh_session))
    if len(pids) == 1:
        return
    if pids:
        _kill_math_game(ssh_session)
    _start_math_game(ssh_session)


def _restart_math_game(ssh_session: SSHSession) -> None:
    """Kill the live math-game JVM and start a fresh one."""
    _kill_math_game(ssh_session)
    _start_math_game(ssh_session)


@pytest.mark.integration
@pytest.mark.real_jvm
def test_b6_docker_pid_replacement_via_mcp_tools(
    request: pytest.FixtureRequest,
    ssh_session: SSHSession,
) -> None:
    """B6 docker e2e: find/prepare/thread_dump across a real JVM restart."""
    from arthas_mcp_proxy.server import (
        connect_ssh,
        find_java_application,
        prepare_arthas,
        thread_dump,
    )
    from arthas_mcp_proxy.ssh_pool import get_connection_pool

    use_docker = bool(request.config.getoption("--docker-target", default=False))
    has_ssh_host = bool(os.environ.get("TEST_SSH_HOST"))
    if not use_docker and not has_ssh_host:
        pytest.skip("specified-not-run: no docker/target")

    host = str(ssh_session.host)
    port = int(ssh_session.port)
    username = str(ssh_session.username)
    password = os.environ.get("TEST_SSH_PASSWORD") or "testpass"

    session_line = connect_ssh(host=host, port=port, username=username, password=password)
    assert "Session ID:" in session_line
    session_id = session_line.rsplit("Session ID:", 1)[1].strip()
    assert get_connection_pool().get_session(session_id) is not None

    _ensure_single_math_game(ssh_session)
    found = find_java_application(session_id, APP)
    data = _data_ok(found)
    assert data["status"] == "matched", found
    old_handle = data["handle"]
    assert OPAQUE_HANDLE_RE.fullmatch(old_handle), old_handle

    prepared = prepare_arthas(jvm_handle=old_handle)
    assert _data_ok(prepared)["origin"] in {"existing", "started_by_proxy"}

    dumped = thread_dump(jvm_handle=old_handle)
    dump_body = _payload(dumped)
    assert dump_body["isError"] is False, dump_body
    assert dump_body["structuredContent"]["data"]["output"]

    _restart_math_game(ssh_session)

    stale = thread_dump(jvm_handle=old_handle)
    assert _error_code(stale) in {"JVM_IDENTITY_CHANGED", "JVM_EXITED"}

    found_again = find_java_application(session_id, APP)
    new_data = _data_ok(found_again)
    assert new_data["status"] == "matched"
    new_handle = new_data["handle"]
    assert OPAQUE_HANDLE_RE.fullmatch(new_handle), new_handle
    assert new_handle != old_handle

    prepared_new = prepare_arthas(jvm_handle=new_handle)
    assert _data_ok(prepared_new)["origin"] in {"existing", "started_by_proxy"}
    dumped_new = thread_dump(jvm_handle=new_handle)
    new_dump = _payload(dumped_new)
    assert new_dump["isError"] is False, new_dump
    assert new_dump["structuredContent"]["data"]["output"]
