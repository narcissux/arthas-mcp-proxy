"""Thin helpers for B6 handle-level PID-reuse MCP tests.

Does not call find_java_application / prepare_arthas / thread_dump — test
bodies must invoke those MCP tools themselves.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from arthas_mcp_proxy.process_inventory import ProcessRecord
from arthas_mcp_proxy.ssh_pool import get_connection_pool
from arthas_mcp_proxy.target_state import parse_handle

if TYPE_CHECKING:
    from collections.abc import Iterator

OPAQUE_HANDLE_RE = re.compile(r"^jvm_[0-9a-f]{16,}$")

HOST_A = "10.0.0.8"
HOST_B = "10.0.0.9"
PORT = 22
USER = "ops"
SESSION_ID = "sess-1"
PID = 4242
START_OLD = "17000"
START_NEW = "20000"
BOOT_OLD = "boot-old"
BOOT_NEW = "boot-new"
APP = "inventory-service.jar"
AMBIGUOUS_APP = "order-service.jar"
TELNET_PORT = 3658
HTTP_PORT = 8563
ARTHAS_VERSION = "3.7.2"
DUMP = "thread dump ok"


def session(*, host: str = HOST_A, session_id: str = SESSION_ID) -> MagicMock:
    sess = MagicMock()
    sess.session_id = session_id
    sess.host = host
    sess.port = PORT
    sess.username = USER
    sess.start_time = None
    sess.boot_id = None
    return sess


def record(
    *,
    pid: int = PID,
    command: str | None = None,
    start_time: str = START_OLD,
    boot_id: str = BOOT_OLD,
    owner: str = "appuser",
) -> ProcessRecord:
    return ProcessRecord(
        pid=pid,
        command=command or f"java -jar /opt/apps/{APP} --server.port=8080",
        owner=owner,
        start_time=start_time,
        boot_id=boot_id,
    )


def payload(raw: str) -> dict:
    return json.loads(raw)


def data_ok(raw: str) -> dict:
    body = payload(raw)
    assert body["isError"] is False
    return body["structuredContent"]["data"]


def error_code(raw: str) -> str:
    body = payload(raw)
    assert body["isError"] is True
    return str(body["structuredContent"]["error"]["code"])


def assert_opaque_handle(handle: str) -> None:
    assert OPAQUE_HANDLE_RE.fullmatch(handle), handle
    with pytest.raises(ValueError):
        parse_handle(handle)


@contextmanager
def patch_find(sess: MagicMock, records: list[ProcessRecord]) -> Iterator[tuple]:
    """Patch pool + inventory for find_java_application. Yields (ensure, attach, client)."""
    pool = MagicMock()
    pool.get_session.return_value = sess
    pool.get_session_by_host.return_value = None
    with (
        patch("arthas_mcp_proxy.server.get_connection_pool", return_value=pool),
        patch("arthas_mcp_proxy.server.collect_inventory_over_ssh", return_value=records),
        patch("arthas_mcp_proxy.arthas_client._ensure_agent") as ensure,
        patch("arthas_mcp_proxy.arthas_client._attach_agent") as attach,
        patch("arthas_mcp_proxy.server.ArthasClient") as client_cls,
    ):
        yield ensure, attach, client_cls


@contextmanager
def patch_prepare_existing(
    sess: MagicMock,
    records: list[ProcessRecord],
    *,
    by_host: object | None = None,
) -> Iterator[tuple]:
    """Existing-agent prepare_arthas mocks (B5-1-b). Yields (pool, attach, by_host_mock)."""
    pool = get_connection_pool()
    host_patch = (
        by_host
        if by_host is not None
        else patch.object(pool, "get_session_by_host", return_value=sess)
    )
    with (
        host_patch as by_host_mock,
        patch(
            "arthas_mcp_proxy.arthas_client.collect_inventory_over_ssh",
            return_value=records,
        ),
        patch("arthas_mcp_proxy.arthas_client._get_sudo_user", return_value=None),
        patch(
            "arthas_mcp_proxy.arthas_client._detect_listen_ports",
            return_value=[TELNET_PORT, HTTP_PORT],
        ),
        patch(
            "arthas_mcp_proxy.arthas_client._probe_arthas_version",
            return_value=ARTHAS_VERSION,
        ),
        patch("arthas_mcp_proxy.arthas_client._attach_agent") as attach,
    ):
        yield pool, attach, by_host_mock


@contextmanager
def patch_thread_dump_success(sess: MagicMock) -> Iterator[tuple]:
    """Handle resolve + mocked ArthasClient.thread_dump. Yields (client, by_host)."""
    client = MagicMock()
    client.thread_dump.return_value = DUMP
    client.last_backend = "arthas_http"
    client.last_backend_degraded = False
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=sess) as by_host,
        patch("arthas_mcp_proxy.server.ArthasClient", return_value=client),
    ):
        yield client, by_host


@contextmanager
def patch_identity_fail(sess: MagicMock, records: list[ProcessRecord]) -> Iterator[tuple]:
    """Live inventory for identity check; spies attach/_ensure_agent. Yields spies."""
    pool = get_connection_pool()
    with (
        patch.object(pool, "get_session_by_host", return_value=sess),
        patch("arthas_mcp_proxy.arthas_client._get_sudo_user", return_value=None),
        patch(
            "arthas_mcp_proxy.arthas_client._find_arthas_path",
            return_value="/tmp/as.sh",  # noqa: S108
        ),
        patch(
            "arthas_mcp_proxy.arthas_client.collect_inventory_over_ssh",
            return_value=records,
        ),
        patch("arthas_mcp_proxy.arthas_client._ensure_agent") as ensure,
        patch("arthas_mcp_proxy.arthas_client._attach_agent") as attach,
    ):
        yield ensure, attach
