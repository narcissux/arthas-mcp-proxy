from unittest.mock import MagicMock, patch

from arthas_mcp_proxy.arthas_client import (
    _PID_STATE,
    _PID_STATE_LOCK,
    _attach_agent,
    _state_key,
)


def test_attach_agent_stores_target_scoped_state() -> None:
    session = MagicMock()
    session.host = "host-a"
    session.port = 22
    session.username = "root"
    pid = 4321
    _PID_STATE.clear()

    with (
        patch("arthas_mcp_proxy.arthas_client._find_free_port", side_effect=[3658, 3659]),
        patch("arthas_mcp_proxy.arthas_client._exec_ssh", return_value=("", "", 0)),
        patch("arthas_mcp_proxy.arthas_client._detect_arthas_port", return_value=3658),
        patch("arthas_mcp_proxy.arthas_client.time.sleep"),
    ):
        assert _attach_agent(session, pid, "/tmp/arthas.sh") == 3658  # noqa: S108

    key = _state_key(session, pid)
    with _PID_STATE_LOCK:
        assert _PID_STATE[key]["port"] == 3658
        assert pid not in _PID_STATE
