"""B1-4: PID-reuse check when inventory identity is present."""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.application_resolver import ApplicationCandidate, validate_process_identity
from arthas_mcp_proxy.arthas_client import ArthasClient, _exec_command
from arthas_mcp_proxy.errors import DomainError
from arthas_mcp_proxy.models import ErrorCode
from arthas_mcp_proxy.process_inventory import ProcessRecord
from arthas_mcp_proxy.server import find_java_application
from arthas_mcp_proxy.typed_tool import typed_command_json

BOOT_OLD = "boot-old"
BOOT_NEW = "boot-new"


def _record(
    *,
    pid: int = 5678,
    start_time: str | None = "17000",
    boot_id: str | None = BOOT_OLD,
    command: str = "com.example.OrderService",
) -> ProcessRecord:
    return ProcessRecord(
        pid=pid,
        command=command,
        owner="appuser",
        start_time=start_time,
        boot_id=boot_id,
    )


def _session(
    *,
    start_time: str | None = "17000",
    boot_id: str | None = BOOT_OLD,
) -> MagicMock:
    session = MagicMock()
    session.host = "target"
    session.port = 22
    session.username = "app"
    session.start_time = start_time
    session.boot_id = boot_id
    return session


@pytest.mark.unit
def test_b1_4_a_same_pid_different_start_time_is_identity_changed() -> None:
    """B1-4-a: same pid, different start_time → JVM_IDENTITY_CHANGED."""
    current = _record(start_time="20000")
    with pytest.raises(DomainError) as exc:
        validate_process_identity([current], pid=5678, start_time="17000", boot_id=BOOT_OLD)
    assert exc.value.code is ErrorCode.JVM_IDENTITY_CHANGED


@pytest.mark.unit
def test_b1_4_a_client_does_not_skip_when_start_time_present() -> None:
    """Inventory start_time must be checked; _validate_identity must not skip."""
    client = ArthasClient(_session(start_time="17000", boot_id=BOOT_OLD))
    current = _record(pid=123, start_time="20000")
    with (
        patch(
            "arthas_mcp_proxy.arthas_client.collect_inventory_over_ssh",
            return_value=[current],
        ) as collect,
        pytest.raises(DomainError) as exc,
    ):
        client._validate_identity(123)
    collect.assert_called_once()
    assert exc.value.code is ErrorCode.JVM_IDENTITY_CHANGED


@pytest.mark.unit
def test_b1_4_b_missing_pid_is_exited() -> None:
    """B1-4-b: pid gone → JVM_EXITED."""
    with pytest.raises(DomainError) as exc:
        validate_process_identity(
            [_record(pid=4242)],
            pid=5678,
            start_time="17000",
            boot_id=BOOT_OLD,
        )
    assert exc.value.code is ErrorCode.JVM_EXITED


@pytest.mark.unit
def test_b1_4_b_exec_command_uses_inventory_when_pid_gone() -> None:
    session = _session()
    with (
        patch("arthas_mcp_proxy.arthas_client.collect_inventory_over_ssh", return_value=[]),
        pytest.raises(DomainError) as exc,
    ):
        _exec_command(
            session,
            5678,
            "jvm",
            "/tmp/as.sh",  # noqa: S108
            start_time="17000",
            boot_id=BOOT_OLD,
        )
    assert exc.value.code is ErrorCode.JVM_EXITED


@pytest.mark.unit
def test_b1_4_c_boot_id_change_is_identity_changed() -> None:
    """B1-4-c: start_time matches, boot_id changed → JVM_IDENTITY_CHANGED."""
    current = _record(start_time="17000", boot_id=BOOT_NEW)
    with pytest.raises(DomainError) as exc:
        validate_process_identity([current], pid=5678, start_time="17000", boot_id=BOOT_OLD)
    assert exc.value.code is ErrorCode.JVM_IDENTITY_CHANGED


@pytest.mark.unit
def test_b1_4_c_client_boot_id_change() -> None:
    """B1-4-c client-level: session boot_id must be forwarded into the check."""
    client = ArthasClient(_session(start_time="17000", boot_id=BOOT_OLD))
    current = _record(pid=5678, start_time="17000", boot_id=BOOT_NEW)
    with (
        patch(
            "arthas_mcp_proxy.arthas_client.collect_inventory_over_ssh",
            return_value=[current],
        ),
        pytest.raises(DomainError) as exc,
    ):
        client._validate_identity(5678)
    assert exc.value.code is ErrorCode.JVM_IDENTITY_CHANGED


@pytest.mark.unit
def test_b1_4_d_legacy_none_start_time_skips_hard_check() -> None:
    """B1-4-d: start_time is None → no identity error, identity_complete=false."""
    client = ArthasClient(_session(start_time=None, boot_id=None))
    with patch("arthas_mcp_proxy.arthas_client.collect_inventory_over_ssh") as collect:
        client._validate_identity(5678)
    collect.assert_not_called()
    assert client.last_identity_complete is False


@pytest.mark.unit
def test_b1_4_d_legacy_exec_command_skips_hard_check() -> None:
    session = _session(start_time=None, boot_id=None)
    with (
        patch("arthas_mcp_proxy.arthas_client.collect_inventory_over_ssh") as collect,
        patch("arthas_mcp_proxy.arthas_client._ensure_agent", return_value=3658),
        patch("arthas_mcp_proxy.arthas_client._exec_ssh", return_value=("ok", "", 0)),
        patch("arthas_mcp_proxy.arthas_client._filter_output", return_value="ok"),
        patch("arthas_mcp_proxy.arthas_client._find_arthas_client_jar", return_value="client.jar"),
        patch("arthas_mcp_proxy.arthas_client._get_java_home", return_value=""),
    ):
        result = _exec_command(session, 5678, "jvm", "/tmp/as.sh")  # noqa: S108
    collect.assert_not_called()
    assert result == "ok"


@pytest.mark.unit
def test_b1_4_d_legacy_typed_result_marks_identity_incomplete() -> None:
    """B1-4-d: legacy typed envelope must mark identity_complete=false."""
    client = ArthasClient(_session(start_time=None, boot_id=None))
    client.last_identity_complete = True
    with (
        patch.object(client, "_resolve_owner", return_value=None),
        patch.object(client, "_get_arthas_path", return_value="/tmp/as.sh"),  # noqa: S108
        patch("arthas_mcp_proxy.arthas_client.collect_inventory_over_ssh") as collect,
        patch("arthas_mcp_proxy.arthas_client._ensure_agent", return_value=3658),
        patch("arthas_mcp_proxy.arthas_client._exec_ssh", return_value=("ok", "", 0)),
        patch("arthas_mcp_proxy.arthas_client._filter_output", return_value="ok"),
        patch("arthas_mcp_proxy.arthas_client._find_arthas_client_jar", return_value="client.jar"),
        patch("arthas_mcp_proxy.arthas_client._get_java_home", return_value=""),
    ):
        payload = json.loads(typed_command_json(client, pid=5678, command="jvm", params={}))
    collect.assert_not_called()
    assert payload["structuredContent"]["meta"]["identity_complete"] is False
    assert client.last_identity_complete is False


@pytest.mark.unit
def test_streaming_command_gates_identity_before_ensure_agent() -> None:
    """Streaming path must reject identity mismatch before attach."""
    client = ArthasClient(_session(start_time="17000", boot_id=BOOT_OLD))
    current = _record(pid=5678, start_time="20000", boot_id=BOOT_OLD)
    with (
        patch.object(client, "_resolve_owner", return_value=None),
        patch(
            "arthas_mcp_proxy.arthas_client.collect_inventory_over_ssh",
            return_value=[current],
        ),
        patch("arthas_mcp_proxy.arthas_client._ensure_agent") as ensure,
        pytest.raises(DomainError) as exc,
    ):
        client.execute_streaming_command(
            5678, "trace Foo bar", emit=lambda _: None, cancel=threading.Event()
        )
    assert exc.value.code is ErrorCode.JVM_IDENTITY_CHANGED
    ensure.assert_not_called()


@pytest.mark.unit
def test_b1_4_e_validate_accepts_process_record() -> None:
    """B1-4-e: validate_process_identity accepts ProcessRecord, not only PID text."""
    current = _record(start_time="17000", boot_id=BOOT_OLD)
    result = validate_process_identity(
        [current],
        pid=5678,
        start_time="17000",
        boot_id=BOOT_OLD,
    )
    assert isinstance(result, ApplicationCandidate)
    assert result.pid == 5678
    assert result.start_time == "17000"
    assert result.boot_id == BOOT_OLD


@pytest.mark.unit
def test_b1_4_e_validate_still_accepts_text_lines() -> None:
    """B1-4-e: existing text-line listings keep working."""
    result = validate_process_identity(
        ["PID 42: appuser 2026-08-01T10:00:00 com.example.Service"],
        42,
        "2026-08-01T10:00:00",
    )
    assert result.pid == 42
    assert result.start_time == "2026-08-01T10:00:00"


@pytest.mark.contract
def test_b1_4_find_stores_boot_id_on_session() -> None:
    session = _session(start_time=None, boot_id=None)
    pool = MagicMock()
    pool.get_session.return_value = session
    record = _record(pid=4242, start_time="17000", boot_id=BOOT_OLD)
    with (
        patch("arthas_mcp_proxy.server.get_connection_pool", return_value=pool),
        patch("arthas_mcp_proxy.server.collect_inventory_over_ssh", return_value=[record]),
    ):
        payload = json.loads(find_java_application("sess-1", "OrderService"))

    assert payload["boot_id"] == BOOT_OLD
    assert session.boot_id == BOOT_OLD
    assert session.start_time == "17000"
