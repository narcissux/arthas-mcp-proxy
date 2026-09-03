"""C2-i live: version/jvm/memory go through Arthas HTTP /api, not CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from tests.integration.test_b5_lifecycle import _connect, _find_handle, _need_docker
from tests.integration.test_pid_replacement import (
    APP,
    _data_ok,
    _payload,
    _restart_math_game,
)

if TYPE_CHECKING:
    from arthas_mcp_proxy.ssh_pool import SSHSession

pytestmark = [pytest.mark.integration, pytest.mark.real_jvm]


@pytest.mark.integration
@pytest.mark.real_jvm
def test_c2_i_real_http_api_does_not_cli(
    request: pytest.FixtureRequest,
    ssh_session: SSHSession,
) -> None:
    """C2-i: live HTTP /api version/jvm/memory must not fall through to CLI."""
    from arthas_mcp_proxy import arthas_client as client_mod
    from arthas_mcp_proxy.server import (
        execute_diagnostic_command,
        find_java_application,
        heap_info,
        prepare_arthas,
    )

    _need_docker(request)
    _restart_math_game(ssh_session)
    session_id = _connect(ssh_session)
    handle = _find_handle(session_id)
    data = _data_ok(find_java_application(session_id, APP))
    pid = int(data["candidates"][0]["pid"])
    prepared = _data_ok(prepare_arthas(jvm_handle=handle))
    assert prepared["origin"] in {"existing", "started_by_proxy"}
    assert str(prepared["arthas_version"]).strip()

    real_exec = client_mod._exec_ssh
    seen: list[str] = []

    def spy(session: object, command: str, timeout: int = 60, sudo_user: str | None = None):
        seen.append(command)
        return real_exec(session, command, timeout=timeout, sudo_user=sudo_user)

    with patch("arthas_mcp_proxy.arthas_client._exec_ssh", side_effect=spy):
        dumped = _payload(heap_info(jvm_handle=handle))
        jvm = execute_diagnostic_command(jvm_handle=handle, pid=pid, command="jvm")
        mem = execute_diagnostic_command(jvm_handle=handle, pid=pid, command="memory")
        ver = execute_diagnostic_command(jvm_handle=handle, pid=pid, command="version")

    assert dumped["isError"] is False, dumped
    meta = dumped["structuredContent"]["meta"]
    assert meta["backend"] == "arthas_http", dumped
    assert meta.get("degraded") is False, dumped
    for raw in (jvm, mem, ver):
        body = _payload(raw)
        assert body["isError"] is False, raw
        meta = body["structuredContent"]["meta"]
        assert meta["backend"] == "arthas_http", raw
        assert meta.get("degraded") is False, raw

    diagnostic = [cmd for cmd in seen if "/api" in cmd or "arthas-client.jar" in cmd]
    assert diagnostic, "expected HTTP /api calls, got: " + repr(seen)
    assert all("/api" in cmd for cmd in diagnostic), diagnostic
    assert all("arthas-client.jar" not in cmd for cmd in diagnostic), diagnostic
