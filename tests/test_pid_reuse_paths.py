from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.arthas_client import ArthasClient
from arthas_mcp_proxy.errors import DomainError
from arthas_mcp_proxy.models import ErrorCode


@pytest.fixture
def session() -> MagicMock:
    value = MagicMock()
    value.host = "target"
    value.port = 22
    value.username = "app"
    value.start_time = "2026-08-02T10:00:00"
    return value


@pytest.mark.parametrize(
    "method, args, kwargs",
    [
        ("thread_dump", (123,), {}),
        ("heap_info", (123,), {}),
        ("watch_method", (123, "Service", "run"), {}),
        ("trace_method", (123, "Service", "run"), {}),
        ("exec_command", (123, "jvm"), {}),
        ("execute_command", (123, "jvm"), {}),
    ],
)
def test_all_execute_watch_trace_paths_inherit_session_identity(
    session: MagicMock, method: str, args: tuple[object, ...], kwargs: dict[str, object]
) -> None:
    client = ArthasClient(session)
    with (
        patch.object(client, "_resolve_owner", return_value=None),
        patch.object(client, "_get_arthas_path", return_value="arthas/as.sh"),
        patch("arthas_mcp_proxy.arthas_client._exec_command", return_value="ok") as execute,
    ):
        assert getattr(client, method)(*args, **kwargs) == "ok"
    assert execute.call_args.kwargs["start_time"] == session.start_time


def test_detach_revalidates_identity_before_stopping_agent(session: MagicMock) -> None:
    client = ArthasClient(session)
    with (
        patch.object(
            client,
            "_validate_identity",
            side_effect=DomainError(ErrorCode.JVM_IDENTITY_CHANGED, "JVM identity changed"),
        ) as validate,
        patch("arthas_mcp_proxy.arthas_client._detect_arthas_port") as detect,
        pytest.raises(DomainError, match="identity changed"),
    ):
        client.detach(123)
    validate.assert_called_once_with(123)
    detect.assert_not_called()
