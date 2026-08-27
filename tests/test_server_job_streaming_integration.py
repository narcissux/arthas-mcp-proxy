from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

import arthas_mcp_proxy.server as server


@pytest.mark.unit
def test_start_diagnostic_job_is_streamed_by_server_job_endpoint(monkeypatch) -> None:
    typed_result = json.dumps(
        {
            "structuredContent": {
                "data": {"output": "typed-thread-output"},
                "summary": "thread dump",
            }
        }
    )
    monkeypatch.setattr(server.get_connection_pool(), "get_session", lambda _sid: object())
    monkeypatch.setattr(server, "typed_command_json", lambda *args, **kwargs: typed_result)

    started = json.loads(server.start_diagnostic_job("thread_dump", {"top_n": 5}, "session", 42))
    assert started["job_id"]

    with (
        TestClient(server.build_sse_app()) as client,
        client.websocket_connect(f"/jobs/{started['job_id']}/stream") as socket,
    ):
        events = [json.loads(socket.receive_text()) for _ in range(2)]

    assert events[0] == {"type": "output", "data": "typed-thread-output"}
    assert events[1] == {
        "type": "terminal",
        "status": "SUCCEEDED",
        "result": "typed-thread-output",
    }
    fetched = json.loads(server.get_diagnostic_job(started["job_id"]))
    assert fetched["output"] == "typed-thread-output"
