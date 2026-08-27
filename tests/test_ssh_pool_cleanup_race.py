from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from arthas_mcp_proxy.ssh_pool import SSHConnectionPool


def _active_client() -> MagicMock:
    client = MagicMock()
    transport = MagicMock()
    transport.is_active.return_value = True
    client.get_transport.return_value = transport
    return client


@pytest.mark.unit
def test_idle_cleanup_skips_session_held_by_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = SSHConnectionPool(idle_timeout=1)
    client = _active_client()
    monkeypatch.setattr("arthas_mcp_proxy.ssh_pool.paramiko.SSHClient", lambda: client)
    session_id = pool.connect("host", username="root", password="pw")
    session = pool.get_session(session_id)
    assert session is not None
    session.last_used = 0

    with pool.lease(session_id):
        pool._cleanup_idle()
        assert pool.get_session(session_id) is not None

    session.last_used = 0
    pool._cleanup_idle()
    assert pool.get_session(session_id) is None
    pool.shutdown()


@pytest.mark.unit
def test_lease_acquires_pool_entry_atomically(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lease must not use a lookup that cleanup can invalidate before acquire."""
    pool = SSHConnectionPool(idle_timeout=1)
    client = _active_client()
    monkeypatch.setattr("arthas_mcp_proxy.ssh_pool.paramiko.SSHClient", lambda: client)
    session_id = pool.connect("host", username="root", password="pw")

    def forbidden_lookup(_session_id: str):
        raise AssertionError("lease used a non-atomic pool lookup")

    monkeypatch.setattr(pool, "get_session", forbidden_lookup)
    with pool.lease(session_id) as session:
        assert session.lease_count == 1
    assert session.lease_count == 0
    pool.shutdown()
