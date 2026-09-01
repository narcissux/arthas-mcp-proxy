"""C1: freeze the remote-curl control plane (no SSH tunnel)."""

from __future__ import annotations

import inspect
import json
import shlex
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from arthas_mcp_proxy.arthas_client import _attach_agent
from arthas_mcp_proxy.arthas_http import ArthasHttpClient, ArthasHttpStreamingClient
from arthas_mcp_proxy.jvm_registry import get_jvm_registry
from arthas_mcp_proxy.server import prepare_arthas, thread_dump
from arthas_mcp_proxy.ssh_pool import get_connection_pool

REPO_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPO_ROOT / "README.md"
SRC_TREE = REPO_ROOT / "src"
SRC_ROOT = SRC_TREE / "arthas_mcp_proxy"


@pytest.mark.unit
def test_c1_a_http_client_url_host_is_loopback_only() -> None:
    """C1-a: ArthasHttpClient curl URL host is only 127.0.0.1 or localhost."""
    sig = inspect.signature(ArthasHttpClient.__init__)
    assert "host" not in sig.parameters

    calls: list[str] = []

    def execute(command: str, timeout: int = 60) -> tuple[str, str, int]:
        calls.append(command)
        return '{"output":"jvm ok"}', "", 0

    ArthasHttpClient(execute, 8563).execute("jvm")
    assert calls
    argv = shlex.split(calls[0])
    urls = [token for token in argv if token.startswith(("http://", "https://"))]
    assert urls, f"curl argv had no URL: {calls[0]}"
    host = urlparse(urls[0]).hostname
    assert host in {"127.0.0.1", "localhost"}, host


@pytest.mark.unit
def test_c1_b_no_ssh_tunnel_and_no_wildcard_bind_on_remote_http() -> None:
    """C1-b: no ssh_tunnel.py; arthas_http/attach stay on loopback, never 0.0.0.0."""
    assert (SRC_TREE / "ssh_tunnel.py").exists() is False
    assert (SRC_TREE / "port_forward.py").exists() is False
    assert (SRC_ROOT / "ssh_tunnel.py").exists() is False
    assert (SRC_ROOT / "port_forward.py").exists() is False
    wildcard = ".".join(("0", "0", "0", "0"))
    http_src = (SRC_ROOT / "arthas_http.py").read_text(encoding="utf-8")
    assert wildcard not in http_src
    attach_src = inspect.getsource(_attach_agent)
    assert wildcard not in attach_src
    assert "--target-ip 127.0.0.1" in attach_src
    for path in SRC_TREE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert path.name not in {"ssh_tunnel.py", "port_forward.py"}
        if "socket.bind" in text or "sock.bind" in text:
            raise AssertionError(f"new listen helper in {path}")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if wildcard not in line:
                continue
            mcp_sse_host = path.name == "server.py" and "--host" in line
            assert mcp_sse_host, f"{path}:{lineno} binds or advertises {wildcard}"

    captured: list[str] = []

    def fake_exec(_sess: object, cmd: str, timeout: int = 30, sudo_user: object = None):
        captured.append(cmd)
        return ("", "", 0)

    with (
        patch("arthas_mcp_proxy.arthas_client._find_free_port", side_effect=[3658, 3660]),
        patch("arthas_mcp_proxy.arthas_client._exec_ssh", side_effect=fake_exec),
        patch("arthas_mcp_proxy.arthas_client._detect_arthas_port", return_value=3658),
        patch("arthas_mcp_proxy.arthas_client.time.sleep"),
    ):
        port = _attach_agent(MagicMock(), 4242, "/tmp/as.sh")  # noqa: S108

    assert port == 3658
    assert captured
    assert "--target-ip 127.0.0.1" in captured[0]
    assert wildcard not in captured[0]


@pytest.mark.contract
def test_c1_c_dead_ssh_short_command_is_transport_lost() -> None:
    """C1-c: SSH transport already dead, then a short command → SSH_TRANSPORT_LOST."""
    session = MagicMock()
    session.session_id = "c1-dead"
    session.host = "10.0.0.8"
    session.port = 22
    session.username = "ops"
    session.start_time = None
    session.boot_id = None
    session.client.get_transport.return_value = None

    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session", return_value=session),
        patch.object(pool, "get_session_by_host", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient") as client_cls,
    ):
        raw = thread_dump(session_id="c1-dead", pid=1)

    payload = json.loads(raw)
    assert payload["isError"] is True
    assert payload["structuredContent"]["error"]["code"] == "SSH_TRANSPORT_LOST"
    client_cls.assert_not_called()


@pytest.mark.contract
def test_c1_d_readme_says_http_is_curl_to_loopback_inside_ssh() -> None:
    """C1-d: README documents SSH-side curl loopback and does not claim a tunnel."""
    readme = README_PATH.read_text(encoding="utf-8")
    marker = "## HTTP / lifecycle notes"
    start = readme.find(marker)
    assert start != -1
    section = readme[start : start + 900]
    assert "curl" in section
    assert "SSH" in section
    assert "127.0.0.1" in section or "localhost" in section
    assert "SSH tunnel" not in readme
    assert "ssh_tunnel" not in readme
    assert "已做 SSH tunnel" not in readme


@pytest.mark.contract
def test_c1_e_readme_does_not_lock_oversold_fallback_or_tunnel() -> None:
    """C1-e: README must not lock tunnel / full HTTP/CLI fallback as already done."""
    readme = README_PATH.read_text(encoding="utf-8")
    assert "SSH" in readme
    assert "curl" in readme
    assert "127.0.0.1" in readme or "localhost" in readme
    assert "HTTP / CLI fallback" in readme
    assert "SSH tunnel" not in readme
    assert "ssh_tunnel" not in readme
    assert "已做 SSH tunnel" not in readme
    assert "HTTP/CLI fallback 全链路已完成" not in readme
    assert "HTTP/CLI fallback 全链路仍未完成" not in readme
    assert "one CLI fallback only if HTTP fails before submission" not in readme
    assert "result is marked degraded" not in readme
    assert "**before submission**" not in readme
    assert "结果标记 degraded" not in readme
    assert "对安全只读命令 fallback CLI" not in readme


@pytest.mark.unit
def test_c1_a_streaming_client_url_host_is_loopback_only() -> None:
    """C1-a: ArthasHttpStreamingClient curl URL host is only loopback."""
    calls: list[str] = []

    def execute(command: str, timeout: int = 60) -> tuple[str, str, int]:
        calls.append(command)
        return '{"state":"SUCCEEDED"}', "", 0

    ArthasHttpStreamingClient(execute, 8563)._request({"action": "exec"}, timeout=7)
    assert calls
    argv = shlex.split(calls[0])
    urls = [token for token in argv if token.startswith(("http://", "https://"))]
    assert urls
    host = urlparse(urls[0]).hostname
    assert host in {"127.0.0.1", "localhost"}, host


@pytest.mark.contract
def test_c1_c_inactive_transport_is_transport_lost() -> None:
    """C1-c: transport present but is_active() is False → SSH_TRANSPORT_LOST."""
    session = MagicMock()
    session.session_id = "c1-inactive"
    session.host = "10.0.0.8"
    session.port = 22
    session.username = "ops"
    session.start_time = None
    session.boot_id = None
    transport = MagicMock()
    transport.is_active.return_value = False
    session.client.get_transport.return_value = transport

    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient") as client_cls,
    ):
        raw = thread_dump(session_id="c1-inactive", pid=1)

    payload = json.loads(raw)
    assert payload["isError"] is True
    assert payload["structuredContent"]["error"]["code"] == "SSH_TRANSPORT_LOST"
    client_cls.assert_not_called()


@pytest.mark.contract
def test_c1_c_prepare_arthas_dead_ssh_is_transport_lost() -> None:
    """C1-c: dead SSH before prepare_arthas → SSH_TRANSPORT_LOST."""
    handle = get_jvm_registry().mint(
        target_key="ops@10.0.0.8:22",
        pid=4242,
        start_time="17000",
        boot_id="boot-old",
        application_name="inventory-service.jar",
    )
    session = MagicMock()
    session.session_id = "c1-prep"
    session.host = "10.0.0.8"
    session.port = 22
    session.username = "ops"
    session.client.get_transport.return_value = None
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=session),
        patch("arthas_mcp_proxy.server.ArthasClient") as client_cls,
    ):
        raw = prepare_arthas(jvm_handle=handle)
    payload = json.loads(raw)
    assert payload["isError"] is True
    assert payload["structuredContent"]["error"]["code"] == "SSH_TRANSPORT_LOST"
    client_cls.assert_not_called()
