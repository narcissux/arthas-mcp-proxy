"""D: catalog / cookbook / docs / CI aligned with facts."""

from __future__ import annotations

import inspect
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from arthas_mcp_proxy.command_catalog import _TOKEN_RE, COMMANDS, build_command
from arthas_mcp_proxy.cookbook import COOKBOOK
from arthas_mcp_proxy.errors import DomainError
from arthas_mcp_proxy.models import ErrorCode
from arthas_mcp_proxy.server import (
    _EXPERT_COMMANDS,
    _validate_expert_command,
    exec_command,
    mcp,
)
from tests.test_readme_contract import ALL_PROMPTS, ALL_TOOLS

REPO_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPO_ROOT / "README.md"
DESIGN_PATH = (
    REPO_ROOT / "docs" / "plans" / "2026-08-01-arthas-mcp-ai-diagnostics-implementation-design.md"
)
GITIGNORE_PATH = REPO_ROOT / ".gitignore"
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
_D_I_BANNED_SHIP_CLAIMS = (
    "B/C 已完成",
    "计划内路径已完成 focused contract 验收",
    "计划内路径：已完成 focused contract 验收",
    "focused contract passed",
    "计划内已实现路径已完成 focused contract 验收",
)

_BANNED_PRODUCT_NAMES = ("heapdump", "redefine", "profiler", "stack_method", "monitor_method")
_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_ALLOWED_SNAKE = set(ALL_TOOLS) | {"jvm_handle"}
_NAMED_STEP_TOKENS = set(ALL_TOOLS) | set(COMMANDS) | {"jvm_handle"}
_OVERSOLD_EXEC_VERBS = (
    "vmoption",
    "perfcounter",
    "mbean",
    "getstatic",
    "sc",
    "sm",
    "jad",
    "dump",
    "redefine",
    "trace",
    "stack",
    "monitor",
    "watch",
    "profiler",
    "heapdump",
    "classloader",
    "tt",
    "help",
    "stop",
)


def _snake_names(text: str) -> set[str]:
    return set(_SNAKE.findall(text))


def _format_step_block(ci_text: str) -> str:
    start = ci_text.index("Ruff format check")
    rest = ci_text[start:]
    nxt = rest.find("\n      - name:")
    return rest if nxt < 0 else rest[:nxt]


@pytest.mark.contract
def test_d_a_watch_method_renders_like_trace_and_rejects_bad_tokens() -> None:
    """D-a: watch_method catalog render + newline/NUL reject, same quoting as trace."""
    spec = COMMANDS["watch_method"]
    assert spec.streaming is True
    assert spec.risk == "read_only"
    params = {
        "class_pattern": "com.Foo",
        "method_pattern": "bar",
        "condition": "x=1",
        "times": 3,
    }
    watched = build_command("watch_method", params)
    traced = build_command("trace_method", params)
    assert watched == "watch com.Foo bar 'x=1' -n 3"
    assert traced == "trace com.Foo bar 'x=1' -n 3"
    assert watched.replace("watch", "trace", 1) == traced
    for bad in ("com.Foo\nBar", "com.Foo\x00Bar", "x; id"):
        with pytest.raises(ValueError, match="unsupported characters"):
            build_command("watch_method", {"class_pattern": bad, "method_pattern": "bar"})
        assert not _TOKEN_RE.fullmatch(bad)


@pytest.mark.contract
def test_d_b_decompile_class_is_read_only_jad_and_rejects_bad_tokens() -> None:
    """D-b: decompile_class = jad --source-only {class}; catalog-only; no heapdump/redefine."""
    spec = COMMANDS["decompile_class"]
    assert spec.risk == "read_only"
    assert spec.streaming is False
    assert spec.template == "jad --source-only {class}"
    assert build_command("decompile_class", {"class": "com.example.Foo"}) == (
        "jad --source-only com.example.Foo"
    )
    assert build_command("decompile_class", {"class_pattern": "com.example.Foo"}) == (
        "jad --source-only com.example.Foo"
    )
    for bad in ("com.Foo\nBar", "com.Foo\x00Bar", "x $(id)"):
        with pytest.raises(ValueError, match="unsupported characters"):
            build_command("decompile_class", {"class": bad})
    assert "heapdump" not in COMMANDS
    assert "redefine" not in COMMANDS
    assert "stack_method" not in COMMANDS
    assert "monitor_method" not in COMMANDS


@pytest.mark.contract
@pytest.mark.asyncio
async def test_d_b_decompile_class_is_catalog_only_not_standalone_mcp_tool() -> None:
    names = {tool.name for tool in await mcp.list_tools()}
    assert names == set(ALL_TOOLS)
    assert "decompile_class" not in names
    assert "decompile_class" in COMMANDS


@pytest.mark.contract
def test_d_c_to_f_cookbook_names_real_tools_and_policy_facts() -> None:
    high = " ".join(COOKBOOK["high_cpu"].steps)
    assert "find_java_application" in high or "jvm_handle" in high
    assert "thread_dump" in high or "dashboard" in high
    if "trace_method" in high:
        assert "only if" in high

    mem = " ".join(COOKBOOK["memory"].steps)
    assert "heapdump" not in mem.lower()
    assert "heap_info" in mem or "memory" in mem or "dashboard" in mem
    assert "find_java_application" in mem or "jvm_handle" in mem

    dead = " ".join(COOKBOOK["deadlock"].steps)
    assert "execute_diagnostic_command" in dead or "thread -b" in dead or "deadlock" in dead
    assert "find_java_application" in dead or "jvm_handle" in dead

    slow = " ".join(COOKBOOK["slow_method"].steps)
    assert "times<=5" in slow.replace(" ", "")
    assert "ttl<=60" in slow.replace(" ", "")
    assert "watch_method" in slow or "trace_method" in slow
    assert "find_java_application" in slow or "jvm_handle" in slow


@pytest.mark.contract
def test_cookbook_and_readme_ban_oversold_or_unknown_tool_names() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    for name, entry in COOKBOOK.items():
        text = " ".join(entry.steps)
        for step in entry.steps:
            assert any(token in step for token in _NAMED_STEP_TOKENS), (
                f"{name} step has no real tool/catalog name: {step!r}"
            )
        unknown = _snake_names(text) - _ALLOWED_SNAKE
        assert not unknown, f"{name} names tools not in ALL_TOOLS: {sorted(unknown)}"
        lowered = text.lower()
        for banned in _BANNED_PRODUCT_NAMES:
            assert banned not in lowered, f"{name} mentions {banned}"
    lowered_readme = readme.lower()
    for banned in ("heapdump", "redefine", "profiler"):
        assert banned not in lowered_readme


@pytest.mark.contract
def test_d_g_exec_command_docstring_first_tokens_are_runtime_seven() -> None:
    """D-g: every first token in the exec_command docstring is in the runtime 7."""
    doc = inspect.getdoc(exec_command) or ""
    assert {
        "dashboard",
        "jvm",
        "sysprop",
        "sysenv",
        "memory",
        "thread",
        "version",
    } == _EXPERT_COMMANDS
    mentioned: set[str] = set()
    allow = re.search(r"exactly these 7\):\s*(.+?)\.", doc, flags=re.S)
    assert allow is not None
    for token in re.findall(r"[a-z][a-z0-9_]*", allow.group(1)):
        mentioned.add(token)
    for line in doc.splitlines():
        stripped = line.strip()
        match = re.match(r"^([a-z][a-z0-9_]*)\b", stripped)
        if match:
            mentioned.add(match.group(1))
    assert mentioned == _EXPERT_COMMANDS
    # B4-2 requires exec watch/trace; docstring may name that observation exception.
    for verb in _OVERSOLD_EXEC_VERBS:
        if verb in {"watch", "trace"}:
            continue
        assert re.search(rf"\b{re.escape(verb)}\b", doc) is None, verb
    assert "observation" in doc.lower()


@pytest.mark.contract
@pytest.mark.parametrize(
    "command",
    [
        "jad --source-only Foo",
        "heapdump",
        "redefine /tmp/X.class",
    ],
)
def test_d_g_exec_command_rejects_jad_heapdump_redefine(command: str) -> None:
    """D-g: jad/heapdump/redefine stay COMMAND_NOT_ALLOWED on exec_command."""
    with pytest.raises(DomainError) as exc_info:
        _validate_expert_command(command)
    assert exc_info.value.code == ErrorCode.COMMAND_NOT_ALLOWED


@pytest.mark.contract
def test_d_g_exec_command_watch_trace_remain_observation_exception() -> None:
    """B4-2 requires exec watch/trace; they stay the documented observation exception."""
    _validate_expert_command("watch a b")
    _validate_expert_command("trace a b")


@pytest.mark.contract
def test_d_h_gitignore_has_star_log_and_recipe_does_not_commit_server_log() -> None:
    gitignore = GITIGNORE_PATH.read_text(encoding="utf-8")
    assert "*.log" in gitignore
    readme = README_PATH.read_text(encoding="utf-8")
    assert "git add server.log" not in readme
    assert "commit server.log" not in readme
    assert "server.log" not in readme


@pytest.mark.contract
def test_d_h_server_log_is_not_tracked_by_git() -> None:
    """D leftover: server.log must stay out of the index even if the file exists."""
    git = shutil.which("git")
    assert git is not None
    tracked = subprocess.check_output(  # noqa: S603
        [git, "ls-files", "--", "server.log"],
        cwd=REPO_ROOT,
        text=True,
    ).strip()
    assert tracked == ""


@pytest.mark.contract
def test_d_i_readme_does_not_lock_bc_or_plan_path_as_shipped() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    design = DESIGN_PATH.read_text(encoding="utf-8")
    for document in (readme, design):
        for banned in _D_I_BANNED_SHIP_CLAIMS:
            assert banned not in document
    assert "tools/list" in readme
    assert "C2-i" in readme
    assert "C3-l/m" in readme
    assert "B6" in readme
    for tool in ALL_TOOLS:
        assert tool in readme
    for prompt in ALL_PROMPTS:
        assert prompt in readme


@pytest.mark.contract
def test_d_j_ci_covers_dev_branch_and_format_check_fails_the_job() -> None:
    ci = CI_PATH.read_text(encoding="utf-8")
    push = re.search(r"push:\s*\n\s*branches:\s*\[([^\]]+)\]", ci)
    pull = re.search(r"pull_request:\s*\n\s*branches:\s*\[([^\]]+)\]", ci)
    assert push is not None and pull is not None
    for block in (push.group(1), pull.group(1)):
        assert "main" in block
        assert "master" in block
        assert "dev/ai-diagnostics" in block
    format_block = _format_step_block(ci)
    assert "ruff format --check src/ tests/" in format_block
    assert "|| true" not in format_block


@pytest.mark.contract
def test_d_k_default_transport_remains_sse() -> None:
    """D-k leftover skip: default transport remains SSE."""
    from tests.test_streamable_transport import test_cli_transport_default_remains_sse

    test_cli_transport_default_remains_sse()
