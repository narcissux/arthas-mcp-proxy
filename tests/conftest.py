"""Pytest fixtures and configuration."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture
def mock_ssh_session() -> MagicMock:
    """Return a mock SSHSession with a mock paramiko client."""
    session = MagicMock()
    session.session_id = "test1234"
    session.host = "192.168.1.1"
    session.port = 22
    session.username = "root"
    session.last_used = 0.0
    session.lock = threading.Lock()

    # Default: command succeeds with empty output
    mock_stdout = MagicMock()
    mock_stdout.read.return_value = b""
    mock_stdout.channel.recv_exit_status.return_value = 0
    mock_stderr = MagicMock()
    mock_stderr.read.return_value = b""

    session.client.exec_command.return_value = (None, mock_stdout, mock_stderr)
    return session


@pytest.fixture(autouse=True)
def clear_pid_state() -> Generator[None, None, None]:
    """Clear global PID state before each test."""
    from arthas_mcp_proxy.arthas_client import (
        _ATTACH_LOCKS,
        _ATTACH_LOCKS_MASTER,
        _PID_STATE,
        _PID_STATE_LOCK,
    )
    from arthas_mcp_proxy.jvm_registry import reset_jvm_registry

    with _PID_STATE_LOCK:
        _PID_STATE.clear()
    with _ATTACH_LOCKS_MASTER:
        _ATTACH_LOCKS.clear()
    reset_jvm_registry()
    yield
