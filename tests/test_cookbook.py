import pytest

from arthas_mcp_proxy.command_catalog import COMMANDS
from arthas_mcp_proxy.cookbook import COOKBOOK
from tests.test_readme_contract import ALL_TOOLS

REQUIRED_SCENARIOS = {"high_cpu", "memory", "deadlock", "slow_method"}
_BANNED = ("heapdump", "redefine", "profiler")
_NAMED = set(ALL_TOOLS) | set(COMMANDS) | {"jvm_handle"}


@pytest.mark.contract
def test_cookbook_has_required_scenarios() -> None:
    assert set(COOKBOOK) >= REQUIRED_SCENARIOS


@pytest.mark.contract
def test_cookbook_entries_have_title_and_steps() -> None:
    for scenario, entry in COOKBOOK.items():
        assert entry.title.strip(), f"{scenario} must have a non-empty title"
        assert entry.steps, f"{scenario} must have non-empty steps"
        assert all(isinstance(step, str) and step.strip() for step in entry.steps)


@pytest.mark.contract
def test_cookbook_steps_name_real_tools_not_empty_english() -> None:
    for scenario, entry in COOKBOOK.items():
        for step in entry.steps:
            assert any(name in step for name in _NAMED), (
                f"{scenario} step has no real tool/catalog name: {step!r}"
            )
            lowered = step.lower()
            for banned in _BANNED:
                assert banned not in lowered, f"{scenario} mentions {banned}"
