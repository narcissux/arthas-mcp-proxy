"""B1-3-b: find_java_application live identity against a real Docker/SSH JVM."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from arthas_mcp_proxy.ssh_pool import SSHSession

pytestmark = [pytest.mark.integration, pytest.mark.real_jvm]


@pytest.mark.integration
def test_b1_3_b_find_returns_live_start_time_and_boot_id(ssh_session: SSHSession) -> None:
    """B1-3-b: Docker real JVM → non-empty start_time; Linux non-empty boot_id.

    Asserts find_java_application returns a non-empty start_time, a non-empty
    Linux boot_id, and a handle that does not contain unknown-start.
    Requires the existing --docker-target / TEST_SSH_* fixtures.
    """
    from arthas_mcp_proxy.server import find_java_application

    pool = MagicMock()
    pool.get_session.return_value = ssh_session
    with patch("arthas_mcp_proxy.server.get_connection_pool", return_value=pool):
        payload = json.loads(find_java_application("sess-it", "math-game.jar"))

    assert payload.get("start_time"), f"expected non-empty start_time, got {payload!r}"
    assert payload.get("boot_id"), f"expected non-empty Linux boot_id, got {payload!r}"
    assert "unknown-start" not in payload["handle"]
