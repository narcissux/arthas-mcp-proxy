import pytest

from arthas_mcp_proxy.cookbook import COOKBOOK

REQUIRED_SCENARIOS = {"high_cpu", "memory", "deadlock", "slow_method"}


@pytest.mark.contract
def test_cookbook_has_required_scenarios() -> None:
    assert set(COOKBOOK) >= REQUIRED_SCENARIOS


@pytest.mark.contract
def test_cookbook_entries_have_title_and_steps() -> None:
    for scenario, entry in COOKBOOK.items():
        assert entry.title.strip(), f"{scenario} must have a non-empty title"
        assert entry.steps, f"{scenario} must have non-empty steps"
        assert all(isinstance(step, str) and step.strip() for step in entry.steps)
