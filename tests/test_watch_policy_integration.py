from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.observation_policy import ObservationPolicy
from arthas_mcp_proxy.server import watch_method


def _json_result(value: str) -> dict[str, Any]:
    import json

    return json.loads(value)


_watch_method_impl = cast("Any", watch_method).__wrapped__


@pytest.mark.contract
def test_watch_method_rejects_times_outside_policy() -> None:
    client = MagicMock()
    session = MagicMock()
    policy = ObservationPolicy(max_times=5)
    with (
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", policy),
    ):
        result = _watch_method_impl(
            session,
            123,
            "com.example.Service",
            "run",
            times=6,
        )
    payload = _json_result(result)
    assert payload["structuredContent"]["error"]["code"] == "OBSERVATION_LIMIT_EXCEEDED"
    client.execute_streaming_command.assert_not_called()
    client.watch_method.assert_not_called()


@pytest.mark.contract
def test_watch_method_uses_policy_for_valid_times() -> None:
    client = MagicMock()
    client.execute_streaming_command.return_value = "watch output"
    session = MagicMock()
    session.session_id = "policy-sess"
    session.host = "10.0.0.8"
    session.port = 22
    session.username = "ops"
    session.start_time = "17000"
    session.boot_id = "boot-old"
    policy = ObservationPolicy(max_times=5)
    with (
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", policy),
    ):
        result = _watch_method_impl(
            session,
            123,
            "com.example.Service",
            "run",
            times=3,
        )
    payload = _json_result(result)
    assert payload["isError"] is False
    assert payload["structuredContent"]["data"]["output"] == "watch output"
    client.execute_streaming_command.assert_called_once()
    client.watch_method.assert_not_called()


@pytest.mark.contract
def test_watch_method_policy_error_is_structured() -> None:
    client = MagicMock()
    session = MagicMock()
    policy = ObservationPolicy(max_times=5)
    with (
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", policy),
    ):
        result = _watch_method_impl(
            session,
            123,
            "com.example.Service",
            "run",
            times=6,
        )
    payload = _json_result(result)
    assert payload["isError"] is True
    assert payload["structuredContent"]["error"]["code"] == "OBSERVATION_LIMIT_EXCEEDED"


@pytest.mark.contract
def test_watch_method_backend_error_is_structured() -> None:
    client = MagicMock()
    client.execute_streaming_command.side_effect = TimeoutError("backend timeout")
    session = MagicMock()
    session.session_id = "policy-sess"
    session.host = "10.0.0.8"
    session.port = 22
    session.username = "ops"
    session.start_time = "17000"
    session.boot_id = "boot-old"
    policy = ObservationPolicy(max_times=5)
    with (
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
        patch("arthas_mcp_proxy.server._watch_policy", policy),
    ):
        result = _watch_method_impl(
            session,
            123,
            "com.example.Service",
            "run",
            times=3,
        )
    payload = _json_result(result)
    assert payload["isError"] is True
    assert payload["structuredContent"]["error"]["code"] == "COMMAND_TIMEOUT"
    client.watch_method.assert_not_called()
