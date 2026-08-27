"""Real PID-reuse/identity checks against the Docker JVM target."""

from __future__ import annotations

import pytest

from arthas_mcp_proxy.application_resolver import validate_process_identity
from arthas_mcp_proxy.errors import DomainError
from arthas_mcp_proxy.models import ErrorCode

pytestmark = [pytest.mark.integration, pytest.mark.real_jvm]


def test_real_jvm_exit_and_pid_replacement(
    pid_replacement_target: dict[str, str | int],
) -> None:
    """A real JVM exit is distinguished from a replacement with new identity."""
    old_pid = int(pid_replacement_target["old_pid"])
    old_start = str(pid_replacement_target["old_start"])
    replacement_pid = int(pid_replacement_target["replacement_pid"])
    replacement_start = str(pid_replacement_target["replacement_start"])
    replacement_lines = [str(pid_replacement_target["replacement_line"])]

    assert replacement_pid == old_pid
    assert replacement_start != old_start

    with pytest.raises(DomainError) as exited:
        validate_process_identity([], old_pid, old_start)
    assert exited.value.code is ErrorCode.JVM_EXITED

    with pytest.raises(DomainError) as changed:
        validate_process_identity(replacement_lines, old_pid, old_start)
    assert changed.value.code is ErrorCode.JVM_IDENTITY_CHANGED
