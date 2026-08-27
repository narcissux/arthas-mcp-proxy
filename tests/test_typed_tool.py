import json
from unittest.mock import MagicMock

import pytest

from arthas_mcp_proxy.typed_tool import typed_command_json


@pytest.mark.unit
def test_typed_command_json_contains_mcp_structured_result() -> None:
    client = MagicMock()
    client.execute_command.return_value = "ok"

    payload = json.loads(
        typed_command_json(client, pid=7, command="thread_dump", params={"top_n": 2})
    )

    assert payload["isError"] is False
    assert payload["structuredContent"]["status"] == "success"
    assert payload["structuredContent"]["data"]["output"] == "ok"
