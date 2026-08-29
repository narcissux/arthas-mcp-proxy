from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from arthas_mcp_proxy.models import ErrorCode
from arthas_mcp_proxy.typed_executor import execute_typed_command


@pytest.mark.unit
def test_typed_executor_runs_catalog_command_through_client() -> None:
    client = MagicMock()
    client.execute_command.return_value = "thread output"

    result = execute_typed_command(client, pid=123, command="thread_dump", params={"top_n": 5})

    assert result.status == "success"
    data = cast("dict[str, Any]", result.data)
    assert data["output"] == "thread output"
    client.execute_command.assert_called_once_with(pid=123, command="thread -n 5", timeout=60)


@pytest.mark.unit
def test_typed_executor_returns_structured_invalid_argument() -> None:
    result = execute_typed_command(MagicMock(), pid=123, command="thread_dump", params={"top_n": 0})

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code is ErrorCode.INVALID_ARGUMENT


@pytest.mark.unit
def test_typed_executor_maps_backend_failure() -> None:
    client = MagicMock()
    client.execute_command.side_effect = RuntimeError("connection lost")

    result = execute_typed_command(client, pid=123, command="heap_info", params={})

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code is ErrorCode.ARTHAS_COMMAND_FAILED


@pytest.mark.unit
def test_typed_executor_copies_identity_complete_on_success() -> None:
    client = MagicMock()
    client.last_identity_complete = False

    def _execute(**kwargs: object) -> str:
        client.last_identity_complete = True
        return "ok"

    client.execute_command.side_effect = _execute
    result = execute_typed_command(client, pid=123, command="heap_info", params={})
    assert result.status == "success"
    assert result.meta.identity_complete is True


@pytest.mark.unit
def test_typed_executor_copies_identity_complete_on_exception() -> None:
    client = MagicMock()
    client.last_identity_complete = False
    client.execute_command.side_effect = RuntimeError("connection lost")

    result = execute_typed_command(client, pid=123, command="heap_info", params={})

    assert result.status == "error"
    assert result.meta.identity_complete is False

    client.last_identity_complete = True
    result = execute_typed_command(client, pid=123, command="heap_info", params={})
    assert result.status == "error"
    assert result.meta.identity_complete is True


@pytest.mark.unit
def test_typed_executor_preserves_backend_degradation_metadata() -> None:
    client = MagicMock()
    client.execute_command.return_value = "jvm output"
    client.last_backend = "arthas_cli"
    client.last_backend_degraded = True
    result = execute_typed_command(client, pid=123, command="thread_dump", params={"top_n": 1})
    assert result.meta.backend == "arthas_cli"
    assert result.meta.degraded is True


@pytest.mark.unit
def test_typed_executor_uses_new_catalog_command_without_raw_command_access() -> None:
    client = MagicMock()
    client.execute_command.return_value = "Java version output"

    result = execute_typed_command(client, pid=123, command="version", params={})

    assert result.status == "success"
    client.execute_command.assert_called_once_with(pid=123, command="version", timeout=60)
