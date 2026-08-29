"""B6 docker/real-target e2e for PID replacement via MCP tools.

Unit/contract locks for B6-a–d live in tests/test_handle_pid_reuse.py
(mocked inventory). This module only holds the docker/real-target path and
does not call validate_process_identity.
"""

from __future__ import annotations

import os

import pytest


@pytest.mark.integration
@pytest.mark.real_jvm
def test_b6_docker_pid_replacement_via_mcp_tools(request: pytest.FixtureRequest) -> None:
    """B6 docker e2e: find/prepare/thread_dump across a real PID replacement."""
    use_docker = bool(request.config.getoption("--docker-target", default=False))
    has_ssh_host = bool(os.environ.get("TEST_SSH_HOST"))
    if not use_docker and not has_ssh_host:
        pytest.skip("specified-not-run: no docker/target")
    pytest.fail(
        "B6 docker e2e not implemented; not green: "
        "live find/prepare/thread_dump kill+restart path is not wired"
    )
