# Arthas MCP Proxy

MCP Server for JVM diagnostics via SSH + [Arthas](https://arthas.aliyun.com/).

AI 只需 SSH 目标和应用名；proxy 负责定位 JVM、按需准备 Arthas，并提供**有限、只读、可取消、可分页**的在线诊断。

当前版本：`2.0.0`（相对 GitHub `main@9191aa8` 的未发布开发快照）。

---

## 开发交接（给接手 Agent）

**分支意图：** 本 README 对应开发分支 `dev/ai-diagnostics`，供其他 agent 继续开发，**不要当成已发布的 `main`。**

| 项 | 值 |
|---|---|
| 基线 | `main@9191aa8`（2026-05-02，已推送） |
| 本轮工作 | 2026-08-01 ~ 2026-08-03，本地 TDD 实现 AI 远程诊断增强 |
| 设计记录 | [`docs/plans/2026-08-01-arthas-mcp-ai-diagnostics-implementation-design.md`](docs/plans/2026-08-01-arthas-mcp-ai-diagnostics-implementation-design.md) |
| 产品定位 | AI 远程 JVM **便捷运维**，不要做成重型可观测性平台 |
| 约束 | 严格 TDD（RED→最小实现→GREEN）；skipped ≠ 通过；禁止提交真实 SSH 密码/私钥/token/远端诊断输出 |

### 修改方向

1. **窄而稳的诊断面**：只收录明确只读、短命令、结构化参数的 catalog；不是完整 Arthas 套件。`exec_command` 不是任意命令执行器。
2. **AI 可编排**：应用名 → JVM 候选；typed tools + 结构化错误；async job（创建/查询/取消/列表/配额/TTL）；有界输出 + 签名 job-bound opaque cursor 分页。
3. **传输兼容**：stdio / SSE / Streamable HTTP 都要有协议级 E2E，而不是只测 Python 类。
4. **低侵入生命周期**：HTTP `/api` 为主；提交前连接失败才 fallback CLI 还不是已完成的路径；长命令用 HTTP long-polling（`init_session` / `async_exec` / `pull_results`）。官方 Arthas **没有** WebSocket 命令协议，不要再去做 Arthas WebSocket backend。
5. **安全默认值 ≠ 完整安全产品**：loopback、DNS rebinding、allowlist、脱敏。不要写成完整认证/RBAC/多租户。

### 改了什么（相对 `9191aa8`）

**新增模块（`src/arthas_mcp_proxy/`）**

| 文件 | 职责 |
|---|---|
| `command_catalog.py` | 白名单命令与结构化参数渲染 |
| `typed_tool.py` / `typed_executor.py` | catalog-backed typed 执行 |
| `result_adapter.py` / `errors.py` / `models.py` | 结构化结果与错误码 |
| `jobs.py` / `job_manager.py` / `job_store.py` / `job_serialization.py` | Job 生命周期、配额、序列化 |
| `output_limit.py` | 有界输出 + 签名 cursor |
| `arthas_http.py` | Arthas HTTP API 与 long-polling |
| `arthas_lifecycle.py` | 仅 proxy-owned + 显式授权的清理 |
| `application_resolver.py` | 应用名 → JVM 候选 |
| `observation_policy.py` | watch/trace 观察上限 |
| `target_state.py` | 目标/PID 身份（含 start time） |
| `cookbook.py` | 只读诊断 prompt |
| `health.py` | `GET /healthz` |
| `transport_contract.py` | transport 契约辅助 |

**既有文件增强：** `server.py`（MCP tools / job 接入 / transport）、`arthas_client.py`（HTTP 路径、PID 身份）、`ssh_pool.py`（多目标、清理竞态）、`decorators.py`（错误契约）、测试镜像与 compose、`pyproject.toml`（`mcp>=1.29,<2`，mypy 钉 `1.11.2`）。

**Catalog 当前命令（有意保持小）：**  
`thread_dump`, `heap_info`, `deadlock`, `top_cpu`, `jvm`, `dashboard`, `memory`, `version`, `sysprop`, `sysenv`, `class_search`, `method_search`, `trace_method`, `watch_method`, `decompile_class`。

**Job 存储：** 默认仍是进程内内存。设置 `ARTHAS_JOB_STORE_SQLITE=/path/to/jobs.sqlite3` 后启用标准库 SQLite MVP：已完成 job 可跨重启恢复；启动时发现仍为 `RUNNING` 的 job 会标 `FAILED` + `JOB_RESTARTED`。单实例 only，无自动重跑、无分布式协调、不是生产级 durable observability。

### 验证了哪些

收口时（2026-08-02/03）本地质量门禁：

| 门禁 | 结果（当时） |
|---|---|
| 非集成 pytest（`tests/` ignore `tests/integration`） | `328 passed` |
| `ruff check src tests` | 通过 |
| `mypy src tests` | 通过 |
| `git diff --check` | 通过 |

Fixture / 集成证据（RED→GREEN，**不是**完整产品能力声明）：

| Fixture | 结果 | 契约 |
|---|---|---|
| no-network Java | `1 passed` | 仓库自带 Java 源码编译运行，禁止联网；`java`/`javac` 缺失必须失败，不得 skip |
| Docker `real_jvm` | `21 passed` | 真 SSH/JVM/Arthas，含 lifecycle cleanup |
| Docker multi-target | `1 passed` | 两个真实 target 发现/诊断隔离 |
| PID replacement | `1 passed` | 同 PID、新进程身份拒绝 |
| HTTP long-polling / interrupt | unit/contract coverage; leftover live docker | 流式输出、取消 interrupt、session 清理 |
| MCP job `server→manager` | contract covered | start/get/cancel 走 manager-backed job |
| stdio / SSE / Streamable HTTP | protocol E2E | list/call 生命周期 |

远程真机 SSH 仍依赖显式环境变量，未配置必须明确失败，不能静默 skip。

接手后**不要沿用上述数字当当前事实**：先重跑门禁，以本次执行为准。

### 进度如何

**当前事实（以 `tools/list` 与 leftover live Docker 为准，不是 GitHub shipped）：** MCP 已注册工具见 Available Tools；本地 unit/contract 覆盖 find+handle、`prepare_arthas`、`await_ms`、job 绑 JVM。仍 leftover、未标 shipped 的 live Docker：C2-i 真 HTTP `/api`、C3-l/m Docker watch/trace、B6 e2e 杀进程同 pid 重启。不要把 B/C 写成已完成产品事实。设计文档是契约/回归记录。真实 MCP job 集成已接入 `server→manager`。官方 Arthas 没有 WebSocket 命令协议；HTTP long-polling 不是待完成的 WebSocket backend。

默认 job 与输出是 process-local in-memory state，not durable storage or a durable API。SQLite 仅在显式环境变量下作为单实例 MVP。

**明确不在本计划、也不要顺手做大的：**

- 完整 RBAC、多租户隔离 / 身份管理
- 完整 Arthas command suite / full Arthas command suite（禁止把 `exec_command` 扩成任意执行器）
- 生产级 durable job storage / 可观测性平台
- Arthas WebSocket 命令通道（官方不存在）

**建议接手顺序：**

1. `git clone` 本开发分支，`.venv` 或 `pip install -e '.[dev]'`（或 `uv sync --extra dev`）。
2. 重跑下方 Development 门禁；集成用 `--docker-target`，不要拿 skipped 当绿。
3. 改行为先补失败测试，再最小实现。原测试与新契约冲突时迁移/删除，不要为了保绿保留错误旧行为。
4. 需要扩展命令时只加只读、有界、结构化参数条目，并补 MCP 协议级用例。
5. 未经用户明确授权：不要 commit/push `main`、不要写真实凭据、不要重启/部署远程进程。

本地运维备忘（本机/已授权远端）：重启 proxy 进程名是下划线 `arthas_mcp_proxy`，不要用连字符。

---

## Features

- **Catalog-backed diagnostics** — small set of explicit, read-only, short-lived, structured-parameter commands; not the full Arthas suite
- **Multi-target SSH** — multiple JVM hosts from one server
- **Cross-user diagnosis** — `sudo -u <owner>` when SSH user != process owner
- **Concurrent-safe** — per-PID attach locks + three-level reuse (cache → detect → attach)
- **SSE, Streamable HTTP & stdio** — protocol lifecycle/list/call E2E on all three
- **Security defaults** — loopback bind, DNS rebinding protection, localhost allowlist
- **Async jobs** — create/get/cancel/list, TTL/EXPIRED, quota leases, bounded paging
- **Optional SQLite job store** — opt-in restart recovery MVP (see below)

## Quick Start

### Docker (recommended)

```bash
docker run -p 8000:8000 ghcr.io/narcissux/arthas-mcp-proxy:latest
```

镜像 tag 仍可能对应 `main` 旧代码；开发分支请从源码运行。

### From source

```bash
git clone https://github.com/narcissux/arthas-mcp-proxy.git
cd arthas-mcp-proxy
pip install -e ".[dev]"
python3 -m arthas_mcp_proxy --transport sse --port 8000
```

### Cursor / MCP Client configuration

```json
{
  "mcpServers": {
    "arthas": {
      "url": "http://localhost:8000/sse"
    }
  }
}
```

## Available Tools

| Tool | Purpose |
|------|---------|
| `connect_ssh` | Establish SSH connection to target server |
| `list_java_processes` | List Java processes with Arthas status |
| `find_java_application` | Resolve an application name to a JVM candidate |
| `prepare_arthas` | Attach or reuse Arthas on a JVM handle and return origin + version |
| `thread_dump` | Thread dump (top N by CPU) |
| `heap_info` | Memory dashboard |
| `watch_method` | Watch method params/return values; waits `await_ms` (default 5000) then returns `job_id` if still running |
| `trace_method` | Trace method execution; waits `await_ms` (default 5000) then returns `job_id` if still running |
| `exec_command` | Execute the explicitly allowlisted read-only expert commands |
| `install_arthas` | Install Arthas on target server |
| `disconnect_ssh` | Disconnect and release resources |
| `execute_diagnostic_command` | Render a safe catalog-backed diagnostic command |
| `start_diagnostic_job` | Create a catalog-backed diagnostic job; requires `jvm_handle` or `session_id`+`pid` |
| `get_diagnostic_job` | Read diagnostic job status and output |
| `cancel_diagnostic_job` | Cancel a running diagnostic job |
| `list_diagnostic_jobs` | List diagnostic jobs by status, limit, or `jvm_handle` |

### Cookbook prompts

Read-only guides: `high_cpu`, `memory`, `deadlock`, `slow_method`.

SSE app also exposes `GET /healthz`, e.g. `{ "status": "ok", "ready": true }`.

### Job storage (SQLite MVP)

Jobs remain in-memory by default. Set `ARTHAS_JOB_STORE_SQLITE=/path/to/jobs.sqlite3` to opt into the stdlib SQLite store. Completed jobs survive a proxy restart; jobs found `RUNNING` at startup are marked `FAILED` with structured `JOB_RESTARTED`. Single-instance MVP only.

Pagination uses signed, job-bound opaque cursors (`ARTHAS_MCP_CURSOR_SECRET` optional). Memory store is process-local; SQLite is still not multi-tenant durable observability.

## HTTP / lifecycle notes

- **HTTP / CLI fallback** — Arthas HTTP is `curl` to `127.0.0.1` (loopback) inside the existing SSH session. The proxy does not open a local forwarded port. Safe read-only short commands use `/api`. CLI is used only when HTTP cannot accept the request (for example connection refused). A command the HTTP API already accepted is not retried on the CLI.
- **Long-running commands** — HTTP long-polling (`init_session` / `async_exec` / `pull_results`). Cancellation sends `interrupt_job` and closes the session.
- **PID replacement** — identity includes start time.
- **Authorized lifecycle cleanup** — only proxy-owned, expired Arthas instances; remote stop only via explicit authorized callback.

The standalone WebSocket job-manager contract is a generic transport contract, **not** an Arthas WebSocket integration.

`start_diagnostic_job` requires `jvm_handle` (from `find_java_application`) or deprecated `session_id`+`pid` together; it cannot succeed by only rendering a catalog command. `list_diagnostic_jobs` can filter by `jvm_handle`. `/jobs/{id}/stream` is the **proxy** job event stream (JSON output/terminal events). It is not an Arthas command channel, does not open a local forwarded port, and is not an Arthas WebSocket.

## Development

建议使用 venv / uv（系统 Python 可能受 PEP 668 限制）：

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

### Quality gates

```bash
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/mypy src tests
.venv/bin/pytest tests --ignore=tests/integration -q
```

### Running tests

```bash
# Unit / contract (mocked, no external SSH)
pytest tests/ --ignore=tests/integration/

# Integration with auto-managed Docker target (recommended)
pytest tests/integration/ -m integration -v --docker-target

# Integration against an authorized remote target (env vars required)
export TEST_SSH_HOST=your-server
export TEST_SSH_USER=your-username
export TEST_SSH_PASSWORD='<password>'
pytest tests/integration/ -m integration -v
```

### 外部 SSH 真实验证（显式配置后运行）

集成测试不会读取、打印或自动发现凭据，也不会在未配置时 `skip`：没有目标/认证配置会在 pytest 收集阶段以明确错误退出。禁止把真实密码或私钥内容写入仓库。

密码认证：

```bash
export TEST_SSH_HOST=<已获授权的目标主机>
export TEST_SSH_USER=<SSH 用户>
export TEST_SSH_PASSWORD='<通过安全渠道提供的密码>'
export TEST_SSH_PORT=22                 # 可选
pytest tests/integration/ -m integration -v
```

私钥认证（仅传递路径给 Paramiko；测试不会读取或输出密钥内容）：

```bash
export TEST_SSH_HOST=<已获授权的目标主机>
export TEST_SSH_USER=<SSH 用户>
export TEST_SSH_KEY_PATH="$HOME/.ssh/id_ed25519"
export TEST_SSH_PORT=22                 # 可选
pytest tests/integration/ -m integration -v
```

目标主机应提供可诊断的 Java 进程及 `jps`/JDK；`TEST_TARGET_PID` 可选。未设置 `TEST_SSH_HOST`、`TEST_SSH_USER`，或未设置 `TEST_SSH_PASSWORD`/`TEST_SSH_KEY_PATH` 之一时，命令会给出缺失配置提示并返回非零状态。无外部目标时请使用 Docker fixture。

```bash
ruff check src/ tests/
ruff format --check src/ tests/
mypy src tests
pytest -v
pytest --cov=arthas_mcp_proxy --cov-report=html
```

## Project Structure

```
.
├── src/arthas_mcp_proxy/     # MCP server, catalog, jobs, HTTP, SSH
├── tests/                    # unit + contract；integration/ 为真 SSH/JVM
├── tests/fixtures/java/      # 无网络 Java fixture
├── docs/plans/               # 已完成的设计/TDD 契约记录
├── pyproject.toml
├── uv.lock                   # optional lockfile for uv
├── Dockerfile
├── docker-compose.yml
├── docker-compose.test.yml
├── Dockerfile.test-target
└── README.md
```

### Test categories

| Category | Command | Requirements |
|----------|---------|-------------|
| Unit / contract | `pytest tests/ --ignore=tests/integration/` | None (fully mocked) |
| Integration (Docker) | `pytest tests/integration/ -m integration -v --docker-target` | Docker daemon |
| Integration (remote) | `pytest tests/integration/ -m integration` | Authorized SSH target with Java |

### Environment variables for integration tests

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TEST_SSH_HOST` | Yes | — | Target hostname or IP |
| `TEST_SSH_USER` | Yes | — | SSH username |
| `TEST_SSH_PASSWORD` | 二选一 | — | SSH 密码（与私钥路径二选一） |
| `TEST_SSH_KEY_PATH` | 二选一 | — | SSH 私钥路径；只传路径，不读/打印密钥 |
| `TEST_SSH_KEY_FILE` | 否 | — | `TEST_SSH_KEY_PATH` 的兼容别名 |
| `TEST_SSH_PORT` | No | 22 | SSH port |
| `TEST_TARGET_PID` | No | auto | Specific PID to diagnose |

**Security**: Never commit credentials. Use environment variables or a `.env` file (gitignored).

## License

MIT
