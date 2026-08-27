import json
from unittest.mock import MagicMock, patch

from arthas_mcp_proxy.server import trace_method

_trace_method_impl = trace_method.__wrapped__


def test_trace_method_is_real_trace_and_governed():
    client = MagicMock()
    client.trace_method.return_value = "trace output"
    session = MagicMock()
    with patch("arthas_mcp_proxy.server.ArthasClient", return_value=client):
        result = _trace_method_impl(
            session,
            123,
            "demo.MathGame",
            "primeFactors",
            times=3,
            concurrency=1,
            ttl=7,
            max_chars=100,
        )
    assert result == "trace output"
    client.trace_method.assert_called_once_with(123, "demo.MathGame", "primeFactors", None, 3, 7)


def test_trace_method_caps_success_output():
    client = MagicMock()
    client.trace_method.return_value = "x" * 20
    with patch("arthas_mcp_proxy.server.ArthasClient", return_value=client):
        result = _trace_method_impl(MagicMock(), 123, "C", "m", max_chars=5)
    assert result == "xxxxx"


def test_trace_method_rejects_ungoverned_times_without_backend_call():
    client = MagicMock()
    with patch("arthas_mcp_proxy.server.ArthasClient", return_value=client):
        result = _trace_method_impl(MagicMock(), 123, "C", "m", times=6)
    payload = json.loads(result)
    assert payload["structuredContent"]["error"]["code"] == "OBSERVATION_LIMIT_EXCEEDED"
    client.trace_method.assert_not_called()


def test_arthas_client_trace_method_does_not_use_watch():
    from arthas_mcp_proxy.arthas_client import ArthasClient

    assert "watch" not in ArthasClient.trace_method.__name__
    assert "trace" in ArthasClient.trace_method.__doc__.lower()


def test_trace_is_in_command_catalog():
    from arthas_mcp_proxy.command_catalog import COMMANDS

    assert "trace_method" in COMMANDS
    assert "watch" not in COMMANDS["trace_method"].template
    assert "trace" in COMMANDS["trace_method"].template


def test_trace_tool_is_registered():
    import asyncio

    from arthas_mcp_proxy.server import mcp

    tools = asyncio.run(mcp.list_tools())
    assert "trace_method" in {tool.name for tool in tools}


# Keep this test intentionally strict: trace must remain distinct from watch.
