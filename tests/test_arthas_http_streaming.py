import json
import threading
from contextlib import suppress

from arthas_mcp_proxy.arthas_http import ArthasHttpStreamingClient


def test_http_long_polling_stream_and_interrupt() -> None:
    requests: list[dict[str, object]] = []

    def execute(command: str, timeout: int = 60) -> tuple[str, str, int]:
        payload = json.loads(command.split("-d ", 1)[1].split(" http", 1)[0].strip("'"))
        requests.append(payload)
        action = payload["action"]
        if action == "init_session":
            return '{"state":"SUCCEEDED","sessionId":"s","consumerId":"c"}', "", 0
        if action == "async_exec":
            return '{"state":"SCHEDULED","body":{"jobId":3}}', "", 0
        if action == "pull_results":
            return (
                '{"body":{"results":[{"type":"output","jobId":3,"value":"tick"},'
                '{"type":"status","jobId":3,"state":"SUCCEEDED"}]}}',
                "",
                0,
            )
        return '{"state":"SUCCEEDED"}', "", 0

    emitted: list[str] = []
    result = ArthasHttpStreamingClient(execute, 8563).execute_stream(
        "watch Foo bar", emitted.append, threading.Event(), timeout=2
    )
    assert result == "tick"
    assert emitted == ["tick"]
    async_request = requests[1]
    assert async_request["execTimeout"] == 2000
    assert [request["action"] for request in requests] == [
        "init_session",
        "async_exec",
        "pull_results",
        "close_session",
    ]


def test_http_long_polling_cancellation_calls_interrupt_job() -> None:
    requests: list[dict[str, object]] = []
    cancel = threading.Event()

    def execute(command: str, timeout: int = 60) -> tuple[str, str, int]:
        payload = json.loads(command.split("-d ", 1)[1].split(" http", 1)[0].strip("'"))
        requests.append(payload)
        if payload["action"] == "init_session":
            return '{"sessionId":"s","consumerId":"c"}', "", 0
        if payload["action"] == "async_exec":
            cancel.set()
            return '{"state":"SCHEDULED","body":{"jobId":3}}', "", 0
        if payload["action"] == "pull_results":
            return '{"body":{"results":[{"jobId":3,"state":"TERMINATED"}]}}', "", 0
        return '{"state":"SUCCEEDED"}', "", 0

    ArthasHttpStreamingClient(execute, 8563).execute_stream("watch Foo bar", lambda _: None, cancel)
    assert "interrupt_job" in [request["action"] for request in requests]
    assert "close_session" in [request["action"] for request in requests]


def test_http_long_polling_contract_uses_basic_auth_and_poll_timeout() -> None:
    calls: list[tuple[str, int]] = []

    def execute(command: str, timeout: int = 60) -> tuple[str, str, int]:
        calls.append((command, timeout))
        return '{"state":"FAILED","message":"bad command"}', "", 0

    client = ArthasHttpStreamingClient(execute, 8563, username="admin", password="test-secret")
    with suppress(ConnectionError):
        client._request({"action": "pull_results"}, 300)
    assert "--user admin:test-secret" in calls[0][0]
    assert calls[0][1] == 305
