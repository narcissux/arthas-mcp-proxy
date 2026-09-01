import json

import pytest

from arthas_mcp_proxy.server import execute_diagnostic_command


@pytest.mark.contract
def test_execute_diagnostic_command_renders_catalog_command() -> None:
    output = execute_diagnostic_command(
        session_id="missing", pid=1, command="thread_dump", params={"top_n": 5}
    )
    assert not output.startswith("Error:")
    payload = json.loads(output)
    assert payload.get("isError") is True
    assert payload["structuredContent"]["error"]["code"] == "SESSION_NOT_FOUND"
