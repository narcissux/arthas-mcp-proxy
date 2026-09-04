"""Real discovery and diagnostic coverage across two local SSH targets."""

from __future__ import annotations

import pytest

from arthas_mcp_proxy.arthas_client import ArthasClient
from arthas_mcp_proxy.ssh_pool import SSHConnectionPool

pytestmark = [pytest.mark.integration, pytest.mark.real_jvm]


def test_discovery_and_diagnostic_are_target_scoped(
    docker_test_targets: dict[str, dict[str, str]],
) -> None:
    """Exercise real SSH, JVM discovery, and a diagnostic on both targets."""
    if not docker_test_targets:
        pytest.skip("specified-not-run: need --docker-targets")

    pool = SSHConnectionPool(idle_timeout=120)
    sessions = []
    try:
        for target in docker_test_targets.values():
            session_id = pool.connect(
                host=target["host"],
                port=int(target["port"]),
                username=target["username"],
                password=target["password"],
                timeout=30,
            )
            session = pool.get_session(session_id)
            assert session is not None
            sessions.append((session_id, session))

        assert len(sessions) == 2
        for _session_id, session in sessions:
            client = ArthasClient(session)
            processes = client.list_java_processes()
            assert "PID" in processes, processes
            pid = next(
                int(line.split()[1].rstrip(":"))
                for line in processes.splitlines()
                if len(line.split()) > 1 and line.split()[1].rstrip(":").isdigit()
            )
            result = client.thread_dump(pid=pid, top_n=5)
            assert any(
                state in result for state in ("RUNNABLE", "WAITING", "TIMED_WAITING", "BLOCKED")
            )
    finally:
        for session_id, _session in sessions:
            pool.disconnect(session_id)
        pool.shutdown()
