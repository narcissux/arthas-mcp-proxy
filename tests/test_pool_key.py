import pytest

from arthas_mcp_proxy.target_state import target_key


@pytest.mark.contract
def test_target_key_is_stable_and_host_scoped() -> None:
    assert target_key("host-a", 22, "root") == "root@host-a:22"
    assert target_key("host-a", 22, "root") != target_key("host-b", 22, "root")
