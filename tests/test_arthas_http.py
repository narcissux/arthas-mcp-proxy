from arthas_mcp_proxy.arthas_http import ArthasHttpClient


def test_http_client_uses_remote_curl_api_contract() -> None:
    calls: list[tuple[str, int]] = []

    def execute(command: str, timeout: int = 60) -> tuple[str, str, int]:
        calls.append((command, timeout))
        return '{"output":"jvm ok"}', "", 0

    result = ArthasHttpClient(execute, 8563).execute("jvm", timeout=7)
    assert result.output == "jvm ok"
    assert calls[0][1] == 12
    assert "http://127.0.0.1:8563/api" in calls[0][0]
    assert '"action":"exec"' in calls[0][0]


def test_http_failure_is_explicit_fallback_signal() -> None:
    def execute(command: str, timeout: int = 60) -> tuple[str, str, int]:
        return "", "connection refused", 7

    try:
        ArthasHttpClient(execute, 8563).execute("jvm")
    except ConnectionError as exc:
        assert "connection refused" in str(exc)
    else:
        raise AssertionError("HTTP failure must be surfaced to the CLI fallback")


def test_http_error_response_is_not_reported_as_success() -> None:
    def execute(command: str, timeout: int = 60) -> tuple[str, str, int]:
        return '{"state":"FAILED","message":"unknown command"}', "", 0

    import pytest

    with pytest.raises(ConnectionError, match="unknown command"):
        ArthasHttpClient(execute, 8563).execute("not-a-command")


def test_http_nested_success_response_is_unwrapped() -> None:
    def execute(command: str, timeout: int = 60) -> tuple[str, str, int]:
        return '{"state":"SUCCEEDED","body":{"output":"jvm ok"}}', "", 0

    result = ArthasHttpClient(execute, 8563).execute("jvm")
    assert result.output == "jvm ok"
    assert result.backend == "arthas_http"


def test_http_exec_contract_sends_millisecond_timeout() -> None:
    calls: list[str] = []

    def execute(command: str, timeout: int = 60) -> tuple[str, str, int]:
        calls.append(command)
        return '{"state":"SUCCEEDED","body":{"output":"ok"}}', "", 0

    ArthasHttpClient(execute, 8563).execute("version", timeout=7)
    assert '"execTimeout":7000' in calls[0]
