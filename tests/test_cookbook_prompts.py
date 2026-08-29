from __future__ import annotations

import re

import pytest

from arthas_mcp_proxy.cookbook import COOKBOOK
from arthas_mcp_proxy.server import deadlock, high_cpu, mcp, memory, slow_method
from tests.test_readme_contract import ALL_TOOLS

_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_ALLOWED_SNAKE = set(ALL_TOOLS) | {"jvm_handle"}


def _snake_names(text: str) -> set[str]:
    return set(_SNAKE.findall(text))


@pytest.mark.contract
@pytest.mark.asyncio
async def test_cookbook_prompts_are_registered() -> None:
    prompts = await mcp.list_prompts()
    names = {prompt.name for prompt in prompts}
    assert names == {"high_cpu", "memory", "deadlock", "slow_method"}
    assert all(prompt.description for prompt in prompts)


@pytest.mark.contract
@pytest.mark.parametrize(
    ("name", "prompt"),
    [
        ("high_cpu", high_cpu),
        ("memory", memory),
        ("deadlock", deadlock),
        ("slow_method", slow_method),
    ],
)
def test_cookbook_prompt_contains_steps(name: str, prompt) -> None:
    result = prompt()
    assert result
    assert any(step in result for step in COOKBOOK[name].steps)


@pytest.mark.contract
@pytest.mark.parametrize(
    ("name", "prompt"),
    [
        ("high_cpu", high_cpu),
        ("memory", memory),
        ("deadlock", deadlock),
        ("slow_method", slow_method),
    ],
)
def test_cookbook_prompt_names_real_tools_not_empty_english(name: str, prompt) -> None:
    result = prompt()
    assert result
    assert any(tool in result for tool in ALL_TOOLS)
    unknown = _snake_names(result) - _ALLOWED_SNAKE
    assert not unknown, f"{name} prompt names tools not in ALL_TOOLS: {sorted(unknown)}"
    for banned in ("heapdump", "redefine", "profiler"):
        assert banned not in result.lower()
