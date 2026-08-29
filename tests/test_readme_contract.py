"""Contract tests for the README documentation (A-doc).

The user-facing surface — the MCP tool registry and the cookbook
prompts — must be documented in README.md so operators and AI clients can
discover them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

README_PATH = Path(__file__).resolve().parents[1] / "README.md"
DESIGN_PATH = (
    README_PATH.parent
    / "docs"
    / "plans"
    / "2026-08-01-arthas-mcp-ai-diagnostics-implementation-design.md"
)
_D_I_BANNED_SHIP_CLAIMS = (
    "B/C 已完成",
    "计划内路径已完成 focused contract 验收",
    "计划内路径：已完成 focused contract 验收",
    "focused contract passed",
    "计划内已实现路径已完成 focused contract 验收",
)

# Full registry as registered in server.py (mcp.list_tools()).
ALL_TOOLS = [
    "connect_ssh",
    "list_java_processes",
    "find_java_application",
    "prepare_arthas",
    "thread_dump",
    "heap_info",
    "watch_method",
    "trace_method",
    "exec_command",
    "install_arthas",
    "disconnect_ssh",
    "execute_diagnostic_command",
    "start_diagnostic_job",
    "get_diagnostic_job",
    "cancel_diagnostic_job",
    "list_diagnostic_jobs",
]

# Cookbook prompts registered in server.py (mcp.list_prompts()).
ALL_PROMPTS = ["high_cpu", "memory", "deadlock", "slow_method"]


@pytest.mark.contract
@pytest.mark.parametrize("tool", ALL_TOOLS)
def test_readme_documents_all_registered_tools(tool: str) -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    assert tool in readme


@pytest.mark.contract
@pytest.mark.parametrize("prompt", ALL_PROMPTS)
def test_readme_documents_all_cookbook_prompts(prompt: str) -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    assert prompt in readme


@pytest.mark.contract
def test_readme_mentions_execute_diagnostic_command() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    assert "execute_diagnostic_command" in readme


@pytest.mark.contract
def test_readme_mentions_healthz() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    assert "/healthz" in readme


@pytest.mark.contract
def test_readme_documents_external_ssh_authentication() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    assert "TEST_SSH_HOST" in readme
    assert "TEST_SSH_USER" in readme
    assert "TEST_SSH_PASSWORD" in readme
    assert "TEST_SSH_KEY_PATH" in readme
    assert "不会在未配置时 `skip`" in readme


@pytest.mark.contract
def test_readme_states_current_capability_boundaries() -> None:
    """Documentation must distinguish shipped paths from explicit roadmap items."""
    readme = README_PATH.read_text(encoding="utf-8")
    assert "Streamable HTTP" in readme
    assert "DNS rebinding protection" in readme
    assert "signed, job-bound opaque cursors" in readme
    assert "HTTP / CLI fallback" in readme
    assert "HTTP long-polling" in readme
    assert "interrupt" in readme
    assert "PID replacement" in readme
    assert "authorized" in readme
    assert "官方 Arthas 没有 WebSocket 命令协议" in readme
    assert "完整 RBAC、多租户" in readme
    assert "不是待完成的 WebSocket backend" in readme
    assert "真实 MCP job 集成已接入" in readme
    assert "完整 RBAC、多租户" in readme
    assert "HTTP/CLI fallback 全链路仍未完成" not in readme
    assert "PID reuse" not in readme


@pytest.mark.contract
def test_readme_does_not_repeat_obsolete_fixture_unavailability_claim() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    assert "not currently available" not in readme
    assert "No production claim is made for" not in readme
    assert "21 passed" in readme


@pytest.mark.contract
def test_readme_does_not_claim_full_arthas() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    assert "Full Arthas command suite" not in readme
    assert "math-game.jar" not in readme


@pytest.mark.contract
def test_design_doc_has_current_status_and_roadmap_boundary() -> None:
    design = (
        README_PATH.parents[0]
        / "docs"
        / "plans"
        / "2026-08-01-arthas-mcp-ai-diagnostics-implementation-design.md"
    ).read_text(encoding="utf-8")
    assert "当前实现状态（2026-" in design
    assert "已完成的实现计划/设计记录" in design
    assert "不再是“待执行”清单" in design
    assert "签名 job-bound opaque cursor" in design
    assert "real_jvm` 已验证 `21 passed`" in design
    assert "HTTP/CLI fallback" in design
    assert "HTTP long-polling" in design
    assert "interrupt/取消传播" in design
    assert "PID replacement" in design
    assert "authorized lifecycle cleanup" in design
    assert "官方 Arthas 没有 WebSocket 命令协议" in design
    assert "完整 RBAC、多租户" in design
    assert "真实 SSH/JVM fixture 尚未落地" not in design
    assert "当前环境因无 SSH 配置及 Docker socket 权限不能执行" not in design
    assert "阶段 C：第三批——HTTP/WS 与 Job" not in design
    assert "HTTP long-polling 与 Job（已完成）" in design
    assert "HTTP/WS capability probe" not in design
    assert "HTTP long-polling session" in design


@pytest.mark.contract
def test_documentation_does_not_claim_unimplemented_product_boundaries() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    design = (
        README_PATH.parent
        / "docs"
        / "plans"
        / "2026-08-01-arthas-mcp-ai-diagnostics-implementation-design.md"
    ).read_text(encoding="utf-8")
    for document in (readme, design):
        assert "完整 RBAC、多租户" in document
        assert "full Arthas command suite" in document or "完整 Arthas command suite" in document
        assert "durable" in document and "job" in document
    assert "All 26+ native Arthas MCP tools" not in readme
    assert "arbitrary Arthas command" not in readme


@pytest.mark.contract
def test_readme_describes_jobs_as_process_local_not_durable() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    assert "process-local in-memory state" in readme
    assert "not durable storage or a durable API" in readme


@pytest.mark.contract
def test_readme_does_not_lock_bc_or_focused_contract_as_shipped() -> None:
    """D-i: README and design.md must not lock B/C or focused contract as shipped."""
    readme = README_PATH.read_text(encoding="utf-8")
    design = DESIGN_PATH.read_text(encoding="utf-8")
    for document in (readme, design):
        for banned in _D_I_BANNED_SHIP_CLAIMS:
            assert banned not in document
    assert "heapdump" not in readme
    assert "redefine" not in readme
    assert "profiler" not in readme
