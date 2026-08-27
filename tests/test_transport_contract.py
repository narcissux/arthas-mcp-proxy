import pytest

from arthas_mcp_proxy.server import build_sse_app
from arthas_mcp_proxy.transport_contract import supported_transports


@pytest.mark.contract
def test_existing_transport_contract_is_explicit() -> None:
    assert callable(build_sse_app)
    assert set(supported_transports()) == {"stdio", "sse", "streamable-http"}
