"""B5 live Agent lifecycle: existing vs started_by_proxy, no accidental stop."""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import pytest

from tests.integration.test_pid_replacement import (
    APP,
    OPAQUE_HANDLE_RE,
    _data_ok,
    _math_game_listing,
    _math_game_pids,
    _restart_math_game,
)

if TYPE_CHECKING:
    from arthas_mcp_proxy.ssh_pool import SSHSession

pytestmark = [pytest.mark.integration, pytest.mark.real_jvm]


def _connect(ssh_session: SSHSession) -> str:
    from arthas_mcp_proxy.server import connect_ssh
    from arthas_mcp_proxy.ssh_pool import get_connection_pool

    password = os.environ.get("TEST_SSH_PASSWORD") or "testpass"
    line = connect_ssh(
        host=str(ssh_session.host),
        port=int(ssh_session.port),
        username=str(ssh_session.username),
        password=password,
    )
    assert "Session ID:" in line
    session_id = line.rsplit("Session ID:", 1)[1].strip()
    assert get_connection_pool().get_session(session_id) is not None
    return session_id


def _find_handle(session_id: str) -> str:
    from arthas_mcp_proxy.server import find_java_application

    found = find_java_application(session_id, APP)
    data = _data_ok(found)
    assert data["status"] == "matched", found
    handle = data["handle"]
    assert OPAQUE_HANDLE_RE.fullmatch(handle), handle
    return handle


def _ss_for_pid(ssh_session: SSHSession, pid: int) -> str:
    _, stdout, _ = ssh_session.client.exec_command(
        f"ss -tlnp 2>/dev/null | grep 'pid={pid},' || true"
    )
    return stdout.read().decode("utf-8", errors="replace")


def _prestart_arthas(ssh_session: SSHSession, pid: int) -> None:
    _, stdout, stderr = ssh_session.client.exec_command(
        "boot=$(find /tmp/arthas-all -name arthas-boot.jar | head -1); "
        'home=$(dirname "$boot"); '
        f'java -jar "$boot" --attach-only --telnet-port 3658 --http-port 8563 '
        f'--target-ip 127.0.0.1 --arthas-home "$home" {pid} >/tmp/arthas-prestart.out 2>&1; '
        "echo rc:$?"
    )
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    deadline = time.time() + 20
    last = ""
    while time.time() < deadline:
        last = _ss_for_pid(ssh_session, pid)
        if ":3658" in last and ":8563" in last:
            return
        time.sleep(0.5)
    log, _, _ = ssh_session.client.exec_command(
        "tail -n 40 /tmp/arthas-prestart.out 2>/dev/null || true"
    )
    pytest.fail(
        "pre-started Arthas did not listen on 3658/8563:\n"
        + last
        + "\nattach:\n"
        + out
        + err
        + log.read().decode("utf-8", errors="replace")
    )


@pytest.mark.integration
@pytest.mark.real_jvm
def test_b5_1_g_docker_no_arthas_started_by_proxy(
    request: pytest.FixtureRequest,
    ssh_session: SSHSession,
) -> None:
    """B5-1-g: no Agent on the JVM → prepare origin started_by_proxy, version set."""
    from arthas_mcp_proxy.server import prepare_arthas

    if not request.config.getoption("--docker-target", default=False) and not os.environ.get(
        "TEST_SSH_HOST"
    ):
        pytest.skip("specified-not-run: no docker/target")

    _restart_math_game(ssh_session)
    pids = _math_game_pids(_math_game_listing(ssh_session))
    assert len(pids) == 1, pids
    assert ":3658" not in _ss_for_pid(ssh_session, pids[0])

    session_id = _connect(ssh_session)
    handle = _find_handle(session_id)
    prepared = _data_ok(prepare_arthas(jvm_handle=handle))
    assert prepared["origin"] == "started_by_proxy", prepared
    assert str(prepared["arthas_version"]).strip()
    assert 3658 <= int(prepared["telnet_port"]) <= 3665


@pytest.mark.integration
@pytest.mark.real_jvm
def test_b5_1_f_docker_prestarted_arthas_existing_not_stopped(
    request: pytest.FixtureRequest,
    ssh_session: SSHSession,
) -> None:
    """B5-1-f: pre-started Agent → existing; disconnect does not stop it."""
    from arthas_mcp_proxy.server import disconnect_ssh, prepare_arthas

    if not request.config.getoption("--docker-target", default=False) and not os.environ.get(
        "TEST_SSH_HOST"
    ):
        pytest.skip("specified-not-run: no docker/target")

    _restart_math_game(ssh_session)
    pids = _math_game_pids(_math_game_listing(ssh_session))
    assert len(pids) == 1, pids
    pid = pids[0]
    _prestart_arthas(ssh_session, pid)

    session_id = _connect(ssh_session)
    handle = _find_handle(session_id)
    prepared = _data_ok(prepare_arthas(jvm_handle=handle))
    assert prepared["origin"] == "existing", prepared
    assert str(prepared["arthas_version"]).strip()
    assert int(prepared["telnet_port"]) == 3658
    assert int(prepared["http_port"] or 0) == 8563

    disconnect_ssh(session_id)
    still = _ss_for_pid(ssh_session, pid)
    assert ":3658" in still, still
    assert ":8563" in still, still


def _need_docker(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--docker-target", default=False) and not os.environ.get(
        "TEST_SSH_HOST"
    ):
        pytest.skip("specified-not-run: no docker/target")


def _backdate_and_cleanup(session_id: str, pid: int, *, authorized: bool):
    from datetime import datetime, timedelta, timezone

    from arthas_mcp_proxy.arthas_client import _LIFECYCLE_REGISTRY, ArthasClient, _state_key
    from arthas_mcp_proxy.ssh_pool import get_connection_pool

    session = get_connection_pool().get_session(session_id)
    assert session is not None
    client = ArthasClient(session)
    identity = _state_key(session, pid, client.start_time)
    instance = _LIFECYCLE_REGISTRY.get(identity)
    assert instance is not None, "lifecycle registry missing instance after prepare"
    instance.last_used_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    return client.cleanup_expired(
        pid, datetime.now(timezone.utc), ttl_seconds=60, authorized=authorized
    )


@pytest.mark.integration
@pytest.mark.real_jvm
def test_b5_1_i_docker_authorized_cleanup_only_proxy_owned(
    request: pytest.FixtureRequest,
    ssh_session: SSHSession,
) -> None:
    """B5-1-i: authorized idle cleanup stops STARTED_BY_PROXY only."""
    from arthas_mcp_proxy.server import find_java_application, prepare_arthas

    _need_docker(request)

    _restart_math_game(ssh_session)
    session_id = _connect(ssh_session)
    handle = _find_handle(session_id)
    data = _data_ok(find_java_application(session_id, APP))
    pid = int(data["candidates"][0]["pid"])
    prepared = _data_ok(prepare_arthas(jvm_handle=handle))
    assert prepared["origin"] == "started_by_proxy", prepared
    assert _backdate_and_cleanup(session_id, pid, authorized=False) == []
    assert ":3658" in _ss_for_pid(ssh_session, pid) or ":8563" in _ss_for_pid(ssh_session, pid)

    cleaned = _backdate_and_cleanup(session_id, pid, authorized=True)
    assert cleaned, cleaned
    still = _ss_for_pid(ssh_session, pid)
    assert ":3658" not in still, still
    assert ":8563" not in still, still

    _restart_math_game(ssh_session)
    pids = _math_game_pids(_math_game_listing(ssh_session))
    pid = pids[0]
    _prestart_arthas(ssh_session, pid)
    session_id = _connect(ssh_session)
    handle = _find_handle(session_id)
    prepared = _data_ok(prepare_arthas(jvm_handle=handle))
    assert prepared["origin"] == "existing", prepared
    assert _backdate_and_cleanup(session_id, pid, authorized=True) == []
    still = _ss_for_pid(ssh_session, pid)
    assert ":3658" in still, still
    assert ":8563" in still, still


@pytest.mark.integration
@pytest.mark.real_jvm
def test_b5_1_h_docker_active_job_ttl_does_not_stop(
    request: pytest.FixtureRequest,
    ssh_session: SSHSession,
) -> None:
    """B5-1-h: a RUNNING job blocks authorized idle cleanup."""
    from arthas_mcp_proxy.job_store import get_job_store
    from arthas_mcp_proxy.jobs import JobStatus
    from arthas_mcp_proxy.server import find_java_application, prepare_arthas

    _need_docker(request)
    _restart_math_game(ssh_session)
    session_id = _connect(ssh_session)
    handle = _find_handle(session_id)
    data = _data_ok(find_java_application(session_id, APP))
    pid = int(data["candidates"][0]["pid"])
    prepared = _data_ok(prepare_arthas(jvm_handle=handle))
    assert prepared["origin"] == "started_by_proxy", prepared

    store = get_job_store()
    assert store is not None
    job = store.create(jvm_handle=handle)
    try:
        assert _backdate_and_cleanup(session_id, pid, authorized=True) == []
        still = _ss_for_pid(ssh_session, pid)
        assert ":3658" in still or ":8563" in still, still
    finally:
        store.update(
            job.job_id,
            status=JobStatus.SUCCEEDED,
        )
