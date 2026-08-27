from __future__ import annotations

import pytest

from arthas_mcp_proxy.cookbook import COOKBOOK
from arthas_mcp_proxy.server import deadlock, high_cpu, mcp, memory, slow_method


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
