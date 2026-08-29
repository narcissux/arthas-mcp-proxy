# Arthas MCP Proxy：AI 远程诊断增强实现设计与 TDD 执行计划

> **文档状态：已完成的实现计划/设计记录。** 下文保留 TDD 过程约束与契约，供维护和回归使用；不再是“待执行”清单。

**状态：** 计划内路径有 unit/contract 覆盖与 leftover live docker，未标 shipped；产品边界仍受下方明确限制，不能据此宣称完整产品能力  
**日期：** 2026-08-01  
**仓库：** `/home/ubuntu/arthas-mcp-proxy`  
**基线：** `main@9191aa8`  
**产品目标：** 用户只提供 SSH 服务器和 Java 应用名，MCP 负责定位 JVM、按需准备 Arthas，并向 AI 提供稳定、兼容、低侵入、可控制的在线诊断能力。

### 当前实现状态（2026-08-02）

当前仓库已实现并有测试覆盖：catalog-backed typed tools、结构化结果/错误的主要路径、基础 async job 创建/查询/取消、有限输出与签名 job-bound opaque cursor 分页、cookbook prompts、healthz，以及 stdio/SSE/Streamable HTTP 的协议级 transport E2E。真实 MCP job 集成已接入 `server→manager`：MCP start/get/cancel 调用创建并控制 manager-backed job，并由 server→manager contract tests 覆盖。当前 fixture 的 RED→GREEN 证据为：no-network Java `1 passed`、Docker `real_jvm` 已验证 `21 passed`、Docker multi-target `1 passed`、PID replacement `1 passed`；HTTP long-polling/interrupt 有 unit/contract coverage，leftover live docker 未标 shipped。

本计划内的 HTTP/CLI fallback、Arthas 长命令、interrupt/取消传播、PID replacement 与 authorized lifecycle cleanup 有 unit/contract 覆盖，C2-i leftover live docker 未标 shipped。官方 Arthas 没有 WebSocket 命令协议，因此长命令采用 HTTP long-polling（`init_session`/`async_exec`/`pull_results`），不是待完成的 WebSocket backend；通用 job-manager WebSocket contract 也不宣称是 Arthas WebSocket 集成。HTTP 在提交前连接失败时才可能降级 CLI（C2-i leftover），取消会发送 `interrupt_job` 并关闭 session；PID identity 包含 start time；cleanup 仅对 proxy-owned 实例且需要显式授权。

剩余边界明确不属于本计划交付：完整 RBAC、多租户隔离/身份管理、完整 Arthas command suite，以及生产级 durable job storage/observability。现有 loopback、DNS rebinding、allowlist、白名单和脱敏是安全默认值，不应描述为完整认证/RBAC。无网络 Java fixture 保持绝不联网，测试在 java/javac 缺失时明确失败而非静默 skip；远程 SSH 仍依赖环境配置。

---

## 0. 实施规则（实现 Agent 必读）

### 0.1 不可违反的规则

1. 使用严格 TDD：`RED → GREEN → REFACTOR`。
2. 每个生产行为必须先有一个明确失败的测试；记录 RED 命令与失败原因。
3. 命令目录只收录明确只读、短命令、结构化参数的有限子集（当前包括 `jvm`、`dashboard`、`memory`、`version`、`sysprop`、`sysenv`、`sc`/`sm` 搜索及既有线程诊断）；不得表述为完整 Arthas 套件。
4. `exec_command` 不是 arbitrary command executor：仅保留极少数显式只读兼容入口，目录 typed executor 才是结构化参数的统一路径；禁止 shell 元字符及副作用命令。
5. streaming 分支依据 catalog 的 `streaming` 元数据选择，避免另行维护一套命令集合。
6. 业务功能必须有 MCP 协议级集成用例，不能只测试内部 Python 类。
6. 原有测试若与新业务契约冲突，必须同步迁移或删除；不得为了“保留测试通过”而保留错误旧行为。
7. 一次只实现当前任务需要的最小代码；禁止顺手重构无关模块。
8. 每个阶段完成后运行该阶段门禁；门禁未通过不得进入下一阶段。
9. 不得提交真实 SSH 密码、私钥、token 或远端诊断输出。
10. 不得执行 `git commit`、`git push`，除非用户另行明确授权。

### 0.2 开工前基线检查

```bash
cd /home/ubuntu/arthas-mcp-proxy
git status --short --branch
git rev-parse HEAD
python3 -m pytest tests --ignore=tests/integration -q
python3 -m ruff check src tests
python3 -m ruff format --check src tests
python3 -m mypy src/arthas_mcp_proxy
```

预期：

- 工作树干净。
- HEAD 为计划基线或实现者明确记录的新基线。
- 原有非集成测试通过。
- 如果 clean install 解析到 `mcp==2.x` 导致 server import 失败，这是本计划第一批要修复的已知 RED，不得误判为环境偶发问题。

### 0.3 测试分层

| 层级 | 目录 | 目的 | 是否允许 mock |
|---|---|---|---|
| 单元测试 | `tests/unit/` | parser、policy、状态机、池并发边界 | 允许，仅隔离外部系统 |
| MCP 契约集成 | `tests/contract/` | `tools/list`、`prompts/list`、schema、`tools/call`、错误语义 | 不 mock MCP server；可注入 fake backend |
| 组件集成 | `tests/integration/` | 真 SSH、真 JVM、真 Arthas、真 HTTP long-polling | 不允许替代主路径 |
| 业务 E2E | `tests/e2e/` | MCP client → transport → SSH → JVM → Arthas 完整流程 | 不允许 mock |

测试标记：

```toml
markers = [
  "unit: isolated deterministic tests",
  "contract: MCP protocol/schema contract tests",
  "integration: real SSH/JVM/Arthas component tests",
  "e2e: end-to-end MCP business workflows",
  "slow: long-running or multi-container tests"
]
```

---

# 1. 产品范围与边界

## 1.1 用户业务流程

```text
用户提供 SSH 主机和认证信息
→ MCP 建立/复用 SSH 连接
→ 用户或 AI 提供 Java 应用名
→ MCP 找到 0/1/N 个 JVM 候选
→ 唯一匹配时签发 jvm_handle；多候选时要求选择
→ MCP 验证 JVM 身份，按需安装/Attach Arthas
→ AI 使用细粒度工具或 cookbook 完成诊断
→ 短命令同步返回；长命令短等待后转为 job
→ 空闲后仅清理由本 MCP 启动的 Arthas
```

## 1.2 “注册/引用目标”不代表 Arthas 常驻

本计划不建设长期服务注册平台。持久对象仅是可选 SSH 连接配置；诊断对象是有 TTL 的 handle。

Arthas 生命周期规则：

- `EXISTING`：诊断前已存在；可复用，禁止自动停止。
- `STARTED_BY_PROXY`：本 MCP Attach；空闲且无活动 job 后允许按策略停止。
- `UNKNOWN`：归属无法确认；禁止自动停止。
- 默认 `ARTHAS_IDLE_TTL_SECONDS=900`。
- SSH 连接断开不等于停止远端 Arthas。
- MCP 进程退出不盲目停止远端 Arthas。

## 1.3 非目标

第一至第三批不建设：

- 长期指标存储和监控平台。
- Kubernetes 调度控制面。
- 企业级资产 CMDB/RBAC 门户。
- 常驻自研 Java Agent。
- Heap/JFR/Jifa/TDA 完整分析平台。
- 自动执行高风险 OGNL、redefine、heapdump、profiler 工作流。

---

# 2. 当前基线与必须迁移的旧行为

当前 MCP 实际暴露 13 个工具（其中 job、resolver 和 diagnostic command 是近期增量实现）。Job storage 另有标准库 SQLite 单实例 MVP：默认仍为内存；配置 SQLite 后已完成 job 可跨重启读取，启动时 RUNNING 会变为带结构化 `JOB_RESTARTED` 错误的 FAILED。它不包含自动重跑、RBAC、多租户、分布式协调或完整 durable observability：

- `connect_ssh`
- `list_java_processes`
- `thread_dump`
- `heap_info`
- `watch_method`
- `exec_command`
- `install_arthas`
- `disconnect_ssh`

已实现底层能力：SSH、JVM 发现、在线/离线安装、owner 识别、`sudo -u`、Attach、端口探测、CLI 命令执行。

已知需要迁移：

1. `mcp>=1.6.0` 会解析到不兼容的 `mcp==2.x`。
2. `_PID_STATE`、`_ATTACH_LOCKS` 仅按 PID，跨主机污染。
3. SSH pool 达上限后可能继续建连；并发同目标可能重复建连；坏连接删除 key 错误。
4. `SSHSession.lock` 未用于命令/回收竞态保护。
5. 工具返回普通字符串；错误也是普通字符串。
6. `exec_command` 过大，细粒度工具不足。
7. `watch_method` 同步阻塞且缺少统一风险/TTL 策略。
8. 无 FastMCP `list_tools/call_tool` 契约测试，真实 JVM测试绕过 MCP。
9. SSE 默认 `0.0.0.0`；DNS rebinding protection 被关闭。
10. Docker healthcheck 调用不存在的 `/healthz`。

旧测试迁移策略：

- `tests/test_arthas_client.py`：拆到 `tests/unit/test_cli_backend.py`、`test_jvm_state.py`；旧 PID-only 断言必须改为 JVM identity。
- `tests/test_ssh_pool.py`：迁移并补充并发、硬上限、lease、回收测试。
- `tests/test_decorators.py`：统一结果后不再断言错误字符串，改断言 `ErrorCode.SESSION_NOT_FOUND` 与 `isError=true`。
- `tests/integration/test_real_jvm.py`：保留少量 backend 组件测试；用户业务流程迁到 MCP E2E，不能继续只调用 `ArthasClient`。
- README “26+ tools” 改为真实 MCP 工具数量和“支持的 Arthas 命令范围”，避免混淆。

---

# 3. 总体架构

```text
MCP Tools / Prompts
        │
        ▼
Tool Service（参数校验、风险策略、统一结果）
        │
        ├── SessionResolver / JVMRegistry
        ├── CommandCatalog / CommandBuilder
        ├── ArthasLifecycleManager
        └── JobManager
                 │
        ┌────────┴─────────┐
        ▼                  ▼
ShortCommandExecutor   StreamingCommandExecutor
HTTP → CLI fallback    HTTP long-polling + interrupt
        │                  │
        └────────┬─────────┘
                 ▼
         SSH Control Plane
 connect / discover / install / attach / port-forward / cleanup
```

## 3.1 建议模块

在保持 YAGNI 的前提下，将 `server.py` 和 `arthas_client.py` 拆分为以下职责：

```text
src/arthas_mcp_proxy/
├── server.py                  # CLI、transport、app 组装；不放业务实现
├── tools.py                   # MCP tool/prompt 注册薄层
├── models.py                  # Pydantic input/output/identity/job 模型
├── errors.py                  # ErrorCode、DomainError、错误映射
├── command_catalog.py         # CommandSpec、风险、参数验证、命令构造
├── diagnostic_service.py      # 细粒度工具业务编排
├── application_resolver.py    # 应用名 → JVM 候选
├── jvm_registry.py            # jvm_handle、身份验证、TTL、回收
├── arthas_lifecycle.py        # install/attach/reuse/origin/cleanup
├── ssh_pool.py                # SSH连接池与 lease
├── ssh_tunnel.py              # 本地端口转发生命周期
├── jobs.py                    # 长命令状态机、TTL、分页、取消
├── backends/
│   ├── base.py                # DiagnosticBackend Protocol
│   ├── cli.py                 # 现有 arthas-client.jar -c
│   ├── http.py                # Arthas /api short + long-polling
└── cookbooks.py               # MCP prompts 内容
```

实施时允许分阶段创建，禁止第一步生成全部空文件。

---

# 4. MCP 外部契约

## 4.1 统一结果模型

所有工具成功和业务失败都返回一致 envelope；同时映射为 MCP `content`、`structuredContent` 和 `isError`。

```python
class ResultMeta(BaseModel):
    request_id: str
    duration_ms: int
    backend: Literal["ssh", "arthas_cli", "arthas_http", "arthas_ws"] | None = None
    truncated: bool = False
    original_chars: int | None = None
    returned_chars: int | None = None
    next_cursor: str | None = None

class ErrorDetail(BaseModel):
    code: ErrorCode
    message: str
    phase: str | None = None
    retryable: bool = False
    suggestion: str | None = None

class ToolResult(BaseModel):
    status: Literal["success", "running", "error"]
    summary: str
    data: dict[str, Any] | list[Any] | None = None
    error: ErrorDetail | None = None
    meta: ResultMeta
```

约束：

- `status=error` 时 `error` 必填且 MCP `isError=true`。
- `status=success|running` 时 MCP `isError=false`。
- `content[0].text` 是简洁摘要，不重复完整 JSON。
- `structuredContent` 必须通过工具 `outputSchema` 验证。
- 无结果不是错误，例如“无死锁”返回 success。
- schema/参数错误由 MCP/JSON-RPC 层处理；SSH/Arthas/目标业务错误使用 tool error result。

## 4.2 统一错误码

第一批必须实现：

```text
INVALID_ARGUMENT
SESSION_NOT_FOUND
SESSION_EXPIRED
SSH_AUTH_FAILED
SSH_CONNECT_TIMEOUT
SSH_TRANSPORT_LOST
SSH_POOL_EXHAUSTED
SSH_COMMAND_TIMEOUT
JVM_NOT_FOUND
JVM_AMBIGUOUS
JVM_IDENTITY_CHANGED
JVM_EXITED
ARTHAS_NOT_INSTALLED
ARTHAS_INSTALL_FAILED
ARTHAS_ATTACH_FAILED
ARTHAS_UNREACHABLE
ARTHAS_COMMAND_FAILED
COMMAND_NOT_ALLOWED
COMMAND_TIMEOUT
OBSERVATION_LIMIT_EXCEEDED
JOB_NOT_FOUND
JOB_ALREADY_FINISHED
JOB_CANCELLED
OUTPUT_CURSOR_INVALID
INTERNAL_ERROR
```

错误映射必须集中在 `errors.py`，不得在每个 tool 内重复 `except Exception: return f"Error..."`。

## 4.3 第一批完成后的工具目录

### SSH/进程

- `connect_ssh`
- `disconnect_ssh`
- `list_java_processes`
- `install_arthas`（第二批由 `prepare_arthas` 逐步替代，但保留）

### 只读诊断

- `get_dashboard`
- `get_jvm_info`
- `get_memory_info`
- `get_thread_overview`
- `find_busy_threads`
- `check_deadlocks`
- `search_class`
- `search_method`
- `decompile_class`
- `get_system_properties`
- `get_system_environment`

### 动态观察

- `watch_method`
- `trace_method`
- `stack_method`
- `monitor_method`

### 专家兜底

- `execute_arthas_command`

兼容别名：

- `thread_dump`：保留一个小版本，deprecated，内部转 `find_busy_threads`。
- `heap_info`：保留一个小版本，deprecated，内部转 `get_dashboard`；文档明确旧名称语义不准确。
- `exec_command`：保留一个小版本，deprecated，内部转 `execute_arthas_command`。

禁止宣称每个 Arthas command 都是独立 MCP tool。

## 4.4 CommandSpec 与风险

```python
class RiskLevel(StrEnum):
    READ_ONLY = "read_only"
    OBSERVATION = "observation"
    MUTATING = "mutating"
    HIGH_IMPACT = "high_impact"
    ARBITRARY = "arbitrary"

class ExecutionMode(StrEnum):
    SHORT = "short"
    STREAMING = "streaming"

class CommandSpec(BaseModel):
    name: str
    risk: RiskLevel
    mode: ExecutionMode
    default_timeout_seconds: int
    hard_timeout_seconds: int
    max_output_chars: int
```

首批 catalog：

| Tool | Arthas command | 风险 | 模式 |
|---|---|---|---|
| get_dashboard | `dashboard -n 1` | READ_ONLY | SHORT |
| get_jvm_info | `jvm` | READ_ONLY | SHORT |
| get_memory_info | `memory` | READ_ONLY | SHORT |
| get_thread_overview | `thread --state ...` 或 `thread` | READ_ONLY | SHORT |
| find_busy_threads | `thread -n N` | READ_ONLY | SHORT |
| check_deadlocks | `thread -b` | READ_ONLY | SHORT |
| search_class | `sc` | READ_ONLY | SHORT |
| search_method | `sm` | READ_ONLY | SHORT |
| decompile_class | `jad --source-only` | READ_ONLY | SHORT |
| watch_method | `watch` | OBSERVATION | STREAMING |
| trace_method | `trace` | OBSERVATION | STREAMING |
| stack_method | `stack` | OBSERVATION | STREAMING |
| monitor_method | `monitor` | OBSERVATION | STREAMING |
| execute_arthas_command | allowlisted/controlled raw command | ARBITRARY | derived |

参数必须以字段接收并由 builder quoting；不得让 typed tool 拼完整 shell 字符串。

## 4.5 watch/trace 风险与 TTL 策略

默认策略：

```text
OBSERVATION_MAX_ACTIVE_PER_JVM=3
WATCH_DEFAULT_TIMES=5
WATCH_MAX_TIMES=20
TRACE_DEFAULT_TIMES=5
TRACE_MAX_TIMES=20
OBSERVATION_DEFAULT_TTL_SECONDS=60
OBSERVATION_MAX_TTL_SECONDS=120
OBSERVATION_DEFAULT_EXPAND=2
OBSERVATION_MAX_EXPAND=4
```

规则：

- class/method pattern 必填，长度受限；禁止换行/NUL。
- `times` 和 `ttl_seconds` 同时受硬上限约束，任一先到即停止。
- 默认不采集异常对象和超深对象图。
- 同 JVM 活动 observation 超过上限返回 `OBSERVATION_LIMIT_EXCEEDED`。
- 完成、超时、取消、JVM退出、SSH断开均必须释放配额。
- `execute_arthas_command` 中的 watch/trace/stack/monitor 也必须经过同一 policy，不能绕过。
- `redefine`、`retransform`、`heapdump`、`profiler start`、写 `vmoption`、写 logger、OGNL 默认拒绝；未来单独设计确认机制。

## 4.6 Cookbooks 使用 MCP Prompts

第一批注册四个 prompts，而非执行型工具：

- `diagnose_high_cpu`
- `diagnose_memory_pressure`
- `diagnose_deadlock`
- `diagnose_slow_method`

每个 prompt 必须：

1. 要求先确认目标 JVM。
2. 从只读、低成本工具开始。
3. 给出按证据升级到观察工具的条件。
4. 明确停止条件和风险。
5. 禁止直接使用 raw command，除非细粒度工具缺失。
6. 只根据工具结果下结论，不编造数据。

示例：`diagnose_high_cpu`

```text
1. get_dashboard
2. find_busy_threads(top_n=5)
3. 若相同业务栈持续出现，使用 trace_method(times<=5, ttl<=60)
4. 输出：现象、证据、置信度、建议；无证据时明确不足
```

## 4.7 MCP 协议与 transport 兼容策略

需求中的“SEE”按“SSE”处理。

事实边界：

- Python MCP SDK `1.29.x` 支持的最新协议是 `2025-11-25`。
- 官方站点当前最新规范为 `2026-07-28`，其中协议状态模型已变化。
- 在 `<2` 约束下不得声称支持 `2026-07-28`。

本批策略：

```toml
mcp>=1.29,<2
```

transport：

- `streamable-http`：默认远程 transport，路径 `/mcp`。
- `stdio`：保留。
- `sse`：兼容模式，路径保持 `/sse`，标记 legacy/deprecated，但本批不删除。

CLI：

```text
--transport streamable-http|sse|stdio
默认 streamable-http
默认 host 127.0.0.1
```

SDK 2.x/协议 2026-07-28 的适配必须另建兼容分支或后续阶段；先通过测试固定当前协议能力，避免“一边迁业务一边重写协议栈”。

---

# 5. 第二批：应用名适配设计

## 5.1 `find_java_application`

输入：

```json
{
  "session_id": "ssh_xxx",
  "application_name": "order-service",
  "match_mode": "auto"
}
```

候选采集优先顺序：

1. `/proc/<pid>/cmdline`（Linux主路径）。
2. `jps -lv`。
3. `ps -eo pid,user,lstart,args` fallback。

匹配证据：

- JAR basename 精确/规范化匹配。
- main class 全名或 simple name。
- `-Dspring.application.name=` 精确匹配。
- 完整 command line token 匹配。
- 不允许默认 regex；若未来支持，必须显式 `match_mode=regex`。

输出状态：

- `matched`：唯一候选，签发 `jvm_handle`。
- `not_found`：返回当前 Java 进程简要候选。
- `ambiguous`：返回多个候选及 match evidence，不 Attach、不自动选择。

## 5.2 JVM 稳定身份

```python
class JVMIdentity(BaseModel):
    target_key: str          # username@host:port 的不可逆/内部稳定标识
    pid: int
    start_time_ticks: int    # /proc/<pid>/stat field 22
    boot_id: str | None      # /proc/sys/kernel/random/boot_id

class JVMHandleRecord(BaseModel):
    handle: str              # jvm_<random>
    identity: JVMIdentity
    application_name: str
    owner: str
    command_line: str
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
```

规则：

- handle 是服务端签发的业务句柄，不依赖 MCP transport session。
- 每次诊断前重读 start time/boot ID。
- PID 相同但 start time 或 boot ID 改变：返回 `JVM_IDENTITY_CHANGED`，禁止自动操作新进程。
- handle 默认 TTL 30 分钟，使用时滑动续期；SSH session 失效时返回明确错误。
- 日志中不打印完整 command line 中可能存在的 secret 参数。

## 5.3 按需 Arthas 生命周期与归属

```python
class ArthasOrigin(StrEnum):
    EXISTING = "existing"
    STARTED_BY_PROXY = "started_by_proxy"
    UNKNOWN = "unknown"

class ArthasRuntime(BaseModel):
    jvm_identity: JVMIdentity
    origin: ArthasOrigin
    telnet_port: int
    http_port: int | None
    arthas_version: str | None
    attached_at: datetime | None
    last_used_at: datetime
    active_jobs: int
```

`prepare_arthas(jvm_handle)` 状态机：

```text
VALIDATE_JVM
→ DETECT_EXISTING
→ PROBE_CAPABILITIES
→ FIND_OR_INSTALL
→ ATTACH_IF_NEEDED
→ VERIFY_COMMAND
→ READY
```

规则：

- 探测到已有 Agent：origin=EXISTING，只复用不停止。
- 本 MCP Attach：origin=STARTED_BY_PROXY。
- attach 之后必须用 `version` 或等价最小命令验证，不能只看端口监听。
- 清理线程只停止 `STARTED_BY_PROXY && active_jobs==0 && idle>TTL`。
- 无法确认归属时 origin=UNKNOWN，不自动停止。
- JVM 重启后清除对应 runtime，但不作用于新 PID。

第二批完成后，所有诊断工具以 `jvm_handle` 为规范输入。为迁移旧客户端，可在一个小版本内接受 `session_id + pid`，但工具描述必须推荐 handle；下一大版本删除旧组合。

---

# 6. 第三批：HeavenC 执行模式

## 6.1 SSH port-forward

Arthas 仍只绑定远端 loopback：

```text
local 127.0.0.1:<ephemeral>
    → SSH direct-tcpip
remote 127.0.0.1:<arthas_http_port>
```

要求：

- 每个 JVM runtime 最多一个共享 tunnel。
- tunnel 引用计数；活动请求/job 持有 lease。
- SSH transport 断开时标记不可用并关闭本地 listener。
- 禁止绑定 `0.0.0.0`。
- 本地端口由 OS 分配，不使用固定范围。
- tunnel 关闭与新请求获取 lease 必须无竞态。

## 6.2 Attach 参数

当前 Attach 使用 `--http-port -1`，第三批必须改为同时分配 telnet/http 端口：

```text
--telnet-port <free_telnet>
--http-port <free_http>
--target-ip 127.0.0.1
```

必须先以目标 Arthas 版本真实验证参数；不得直接复制社区项目命令。

## 6.3 短命令 HTTP

`HttpArthasBackend`：

- POST `/api`
- body：`{"action":"exec","command":"..."}`
- connect/read/total timeout 分离。
- 解析 Arthas JSON 响应为 raw text + structured parser output。
- 仅对可安全重试的只读命令做一次 fallback，不在 HTTP 层盲目 retry。

fallback：

```text
HTTP capability 不可用/连接前失败
→ CLI backend 执行一次
→ meta.backend=arthas_cli, meta.degraded=true
```

以下情况禁止自动 fallback 重复执行：

- 服务端已接受但响应中断，且命令可能产生副作用。
- observation 命令已启动。
- cancellation 已发生。

## 6.4 长命令 HTTP long-polling

官方 Arthas 没有 WebSocket 命令协议。`ArthasHttpStreamingClient` 使用 HTTP
session、异步执行和结果拉取支持 watch、trace 等长命令，持续追加到有界 job
buffer，并在取消时发送 `interrupt_job`、关闭 session。

不得把无限日志直接存入内存。第一版使用有界内存 ring buffer：

```text
JOB_MAX_BUFFER_CHARS=1_000_000
JOB_PAGE_CHARS=16_000
JOB_TTL_SECONDS=900
JOB_MAX_ACTIVE_GLOBAL=50
JOB_MAX_ACTIVE_PER_JVM=3
```

## 6.5 3–5 秒混合返回

动态观察工具增加：

```text
await_ms: 0..5000，默认 5000
```

行为：

- 在 `await_ms` 内完成：`status=success`，直接返回结果。
- 未完成：`status=running`，返回 `job_id`、当前摘要、过期时间。
- `await_ms=0`：立即返回 job。
- 超过 5000 拒绝为 `INVALID_ARGUMENT`，避免 MCP请求长期占用。

## 6.6 Job API

- `get_arthas_job(job_id, cursor=None, limit_chars=16000)`
- `list_arthas_jobs(jvm_handle=None, status=None)`
- `stop_arthas_job(job_id)`

状态：

```text
QUEUED → RUNNING → SUCCEEDED
                 ↘ FAILED
                 ↘ CANCELLED
                 ↘ EXPIRED
```

取消规则：

- 发送 Arthas `interrupt_job`。
- 关闭 HTTP long-polling session。
- 等待 reader task 结束。
- 释放 observation quota 和 tunnel lease。
- 重复 stop 必须幂等：已终态返回当前终态，不报内部错误。
- 完成与取消竞态必须只产生一个终态。

## 6.7 输出上限与分页

统一 `OutputLimiter`：

- 普通 tool 默认 16,000 chars。
- 先保留头部摘要和尾部关键信息，不得静默切断错误结尾。
- 返回 `truncated/original_chars/returned_chars/next_cursor`。
- cursor 必须不透明、绑定 job/request、可校验；非法 cursor 返回 `OUTPUT_CURSOR_INVALID`。
- 分页读取不得重复或遗漏字符。
- job 过期后 cursor 同步失效。

---

# 7. TDD 业务用例总表

每个功能至少覆盖：成功主路径、输入边界、外部故障、资源清理、MCP契约。

## 7.1 第一批用例

### F1：细粒度 MCP 工具

契约用例：

- `tools/list` 包含规定工具，名称唯一。
- 每个 tool 有非空、面向诊断意图的 description。
- input schema 有 required、范围、enum；禁止 raw typed parameters 退化为字符串。
- `execute_arthas_command` 明确为专家兜底且标注风险。
- deprecated alias 存在且 description 标注替代工具。
- 每个细粒度工具构造正确 Arthas command，并经过同一 CommandCatalog。

真实业务 E2E：

- MCP client 连接 Docker SSH target，调用 `get_jvm_info`、`get_memory_info`、`find_busy_threads`、`check_deadlocks`、`search_class`、`search_method`、`decompile_class`，断言 structured result 和真实内容。
- 不允许通过直接调用 `ArthasClient` 替代该用例。

### F2：统一结构化结果/错误

- 成功结果通过 outputSchema。
- Arthas 未安装返回 `ARTHAS_NOT_INSTALLED`（或由自动准备策略安装），不是错误字符串。
- session 失效返回 `SESSION_EXPIRED` 且 `isError=true`。
- 无死锁返回 success、`deadlocks=[]`。
- 所有结果含 request_id、duration、backend。
- exception message 不泄漏 password/key_string。

### F3：Cookbooks

- `prompts/list` 包含四个 cookbook。
- `prompts/get` 返回顺序明确的低风险流程。
- high CPU prompt 先 dashboard/thread，再有条件 trace。
- memory prompt 不直接要求 heapdump。
- deadlock prompt 使用 `check_deadlocks`。
- slow method prompt 强制有限 times/TTL。
- prompt 中不引用不存在的工具。

### F4：watch/trace policy

- 默认 times/TTL 生效。
- times=0、超过上限、TTL 超限被拒绝。
- 同 JVM 第四个 observation 被拒绝。
- 完成/错误/取消后配额恢复。
- raw command 不能绕过 policy。
- class/method/condition 含换行/NUL/危险 shell 结构被安全拒绝或严格转义。

### F5：MCP/transport 兼容

- clean install 安装 `mcp>=1.29,<2`。
- server import 成功。
- SDK 报告支持协议包含 2025-11-25。
- stdio 可 list/call tools。
- Streamable HTTP 可 list/call tools。
- SSE legacy 可 list/call tools。
- 不支持的协议版本收到明确 protocol error。
- stdio stdout 无日志污染。

### F6：多服务器 PID 隔离

- 两个独立 SSH targets 均运行相同 PID（测试容器可通过 PID namespace 产生）；缓存、端口、owner、attach lock 不共享。
- 同一 JVM 并发 attach 仅执行一次。
- 不同主机同 PID 可并行 attach。
- disconnect A 不清除 B 的状态。

### F7：SSH pool

- 同 target 20 个并发 connect 只创建一个 SSHClient。
- 达硬上限且无 idle session 时返回 `SSH_POOL_EXHAUSTED`。
- 活动 lease 不被 idle cleanup 关闭。
- lease 释放后可回收。
- inactive transport 删除正确 key。
- shutdown 可终止 cleanup thread，并关闭所有 transport。
- failed connect 不占容量、不泄漏 Paramiko thread。

### F8：启动/schema/真实 MCP 调用

- 独立子进程启动 stdio 并完成 tools/list。
- 独立 HTTP server readiness 成功。
- `/healthz` 存在且区分 liveness/readiness（若只实现一个，文档明确语义）。
- Docker production image 启动后 MCP call 成功。
- 真实 MCP E2E 运行 `connect_ssh → list_java_processes → get_jvm_info → disconnect_ssh`。

## 7.2 第二批用例

### F9：find_java_application

测试容器运行：

- `inventory-service.jar` 唯一实例。
- `order-service.jar` 两个实例（模拟多候选）。
- 一个主类名匹配实例。
- 一个带 `-Dspring.application.name=billing-service` 的实例。

用例：

- jar basename 唯一匹配。
- Spring name 唯一匹配。
- main class 匹配。
- 不存在返回 not_found + candidates。
- 两实例返回 ambiguous，且不 Attach。
- 大小写/连字符规范化行为被明确测试，不做过度模糊匹配。

### F10：JVM handle 与 PID reuse

- 唯一匹配签发 `jvm_handle`。
- 使用 handle 可调用诊断工具。
- 杀死并以同应用名重启后，旧 handle 返回 `JVM_IDENTITY_CHANGED/JVM_EXITED`。
- 模拟 PID reuse：同 PID 不同 start_time 必须拒绝。
- handle TTL 过期返回明确错误。
- A host handle 不能解析到 B host JVM。

### F11：Arthas origin/lifecycle

- 预先启动 Arthas → origin=EXISTING → disconnect/idle cleanup 不停止。
- 无 Arthas → prepare 启动 → origin=STARTED_BY_PROXY。
- 活动 job 时 idle cleanup 不停止。
- job 完成且超 TTL → 只停止 STARTED_BY_PROXY。
- attach 后 probe 失败 → 返回 ARTHAS_UNREACHABLE，并清除本地错误状态。
- MCP 进程重启后未知归属不被盲目停止。

## 7.3 第三批用例

### F12：SSH tunnel

- tunnel 仅绑定 127.0.0.1。
- HTTP `/api` 通过 tunnel 可达。
- 两个并发短命令复用 tunnel。
- SSH断开后本地 tunnel 关闭且后续调用得到 SSH_TRANSPORT_LOST。
- tunnel cleanup 不影响其他 JVM。

### F13：短命令 HTTP + CLI fallback

- HTTP 成功时不调用 CLI。
- HTTP capability 不存在时只读命令 fallback CLI，meta 标记 degraded。
- HTTP 已提交但响应中断时不盲目重复不安全命令。
- HTTP/CLI 输出经过同一 result parser 和 limiter。

### F14：HTTP long-polling 长命令与混合返回

- 官方 Arthas 没有 WebSocket 命令协议；使用 HTTP long-polling session。
- 短 watch 在 await window 内完成，直接 success。
- 长 trace 超过 await window，返回 running + job_id。
- await_ms=0 立即返回。
- await_ms>5000 被拒绝。
- 后续 get job 获得真实输出。
- interrupt/取消发送 `interrupt_job`，关闭 session，并释放资源。

### F15：Job 查询/停止

- get/list/stop 覆盖全状态。
- stop running job 发送 `interrupt_job` 并关闭 HTTP session。
- stop 两次幂等。
- cancel/complete 竞态只保留一个终态。
- JVM退出使 job 失败并释放 quota/tunnel lease。
- TTL 到期删除/标记 EXPIRED。

### F16：分页

- 产生 >32K 输出，第一页 <=16K 且有 cursor。
- 连续分页拼接等于原始输出。
- 无重复/遗漏。
- cursor 不能用于另一 job。
- job 过期后 cursor 无效。
- truncate metadata 精确。

---

# 8. 测试基础设施改造

## 8.1 Java fixture 不依赖运行时网络

新增：

```text
tests/fixtures/java/DiagnosticTestApp.java
tests/fixtures/java/BusyCpu.java
tests/fixtures/java/DeadlockApp.java
```

构建时在 `Dockerfile.test-target` 编译/打包，避免测试镜像每次从网络下载 `math-game.jar`。可保留 math-game 作为 Arthas 官方兼容样例，但不能作为唯一 fixture。

`DiagnosticTestApp` 应提供：

- 稳定长运行主循环。
- `slowMethod(long millis)` 供 watch/trace。
- 可选 HTTP/文件信号触发方法调用。
- 可配置 application name 与 instance id。
- 不含外部依赖。

## 8.2 多服务器测试

将 `docker-compose.test.yml` 扩为至少两个 SSH target：

```text
ssh-target-a → host port 2222
ssh-target-b → host port 2223
```

两个容器分别处于独立 PID namespace，确保可出现相同 PID。不要通过 monkeypatch 伪造跨主机隔离业务用例。

## 8.3 MCP client fixture

新增：

```text
tests/contract/conftest.py
tests/e2e/conftest.py
```

fixture 必须使用官方 MCP Python client 建立 stdio/Streamable HTTP/SSE 会话，调用真实 `list_tools/get_prompt/call_tool`。业务断言只读取 MCP result，不直接访问 server 内部对象。

## 8.4 故障注入

单元/组件层提供可控故障：

- SSH connect barrier（验证单航班建连）。
- inactive transport。
- command timeout。
- HTTP connection refused。
- HTTP long-polling session close before completion。
- JVM identity provider 返回变化 start_time。
- fake clock 控制 TTL；禁止真实 sleep 60 秒。

---

# 9. 分阶段 TDD 执行任务

以下每个任务均执行：写失败测试 → 运行并确认预期失败 → 最小实现 → 目标测试通过 → 全部相关测试通过 → 重构。

## 阶段 A：第一批——稳定性与 MCP 契约

### Task A1：整理测试目录与 marker

**修改：** `pyproject.toml`  
**移动/创建：** `tests/unit/`、`tests/contract/`、`tests/e2e/`

RED：添加 marker/collection 测试，确认新 suite 能独立选择。  
GREEN：迁移现有 unit tests，不改变业务实现。  
验证：

```bash
python3 -m pytest tests/unit -m unit -q
python3 -m pytest tests/contract -m contract --collect-only
```

### Task A2：锁定 MCP SDK 并增加 clean import test

**修改：** `pyproject.toml`  
**创建：** `tests/contract/test_server_startup.py`

RED：在隔离环境安装项目后 import server、list tools；当前因 mcp 2.x 失败。  
GREEN：固定 `mcp>=1.29,<2`。  
验证：检查安装版本、SUPPORTED_PROTOCOL_VERSIONS、server import。

### Task A3：定义统一模型和错误

**创建：** `models.py`、`errors.py`  
**创建：** `tests/unit/test_models.py`、`test_errors.py`

RED：覆盖 envelope invariant、ErrorCode 序列化、异常映射、secret scrub。  
GREEN：最小 Pydantic 模型和 mapper。  
禁止此任务修改 MCP tools。

### Task A4：MCP result adapter

**创建：** `tests/contract/test_result_contract.py`  
**修改：** `tools.py`/`server.py`

RED：通过 MCP call 断言 `structuredContent`、outputSchema、isError。  
GREEN：实现 ToolResult → MCP result 适配；先迁一个试点工具 `get_jvm_info`。  
验证 client output schema 校验通过。

### Task A5：CommandCatalog 与安全 builder

**创建：** `command_catalog.py`、`tests/unit/test_command_catalog.py`

逐个 RED/GREEN：dashboard、jvm、memory、thread、deadlock、sc、sm、jad、watch、trace、stack、monitor。  
覆盖 quoting、范围、换行/NUL、风险/模式。

### Task A6：细粒度只读工具

**创建/修改：** `diagnostic_service.py`、`tools.py`、`backends/cli.py`  
**创建：** `tests/contract/test_readonly_tools.py`

每个工具单独一个 TDD cycle；不要一次实现全部再测试。  
完成后更新 deprecated alias。

### Task A7：观察工具与策略

**创建：** `tests/unit/test_observation_policy.py`、`tests/contract/test_observation_tools.py`  
**修改：** catalog/service/tools

先在现有 CLI backend 上实现严格 times/TTL/并发政策；第三批再替换 transport。TTL 必须由 manager 强制，不只写在描述中。

### Task A8：Cookbook prompts

**创建：** `cookbooks.py`、`tests/contract/test_cookbooks.py`

每个 prompt 先写 `prompts/list/get` 失败测试，再实现。测试引用工具集合，避免未来改名后 prompt 漂移。

### Task A9：多目标 JVM state key

**创建：** `jvm_state.py` 或在 `models.py` 定义临时 `TargetPidKey`  
**修改：** `arthas_client.py`  
**创建：** `tests/unit/test_jvm_state.py`

RED：两 host 相同 PID 状态/lock 隔离。  
GREEN：key 至少包含 target key + PID；第二批加入 start_time。  
迁移旧 `_PID_STATE` 测试，不能保留 PID-only API。

### Task A10：SSH pool single-flight、硬上限与 lease

**修改：** `ssh_pool.py`  
**创建：** `tests/unit/test_ssh_pool_concurrency.py`

顺序：

1. RED 同 target 并发重复 connect；实现 single-flight。
2. RED 硬上限失效；实现明确 PoolExhausted。
3. RED inactive key 删除错误；修复。
4. RED active lease 被 cleanup；实现 lease/in_use。
5. RED cleanup thread 无停止；实现 stop event + join。

避免持有全局 pool lock 执行网络 connect；使用 per-key condition/future。

### Task A11：transport 与 health

**修改：** `server.py`、`Dockerfile`、`docker-compose.yml`  
**创建：** `tests/contract/test_transports.py`

RED/GREEN 顺序：stdio → Streamable HTTP → SSE legacy → `/healthz`。  
默认 loopback；恢复 DNS rebinding protection；不再 `forwarded_allow_ips="*"`。

### Task A12：第一批真实 MCP E2E

**创建：** `tests/e2e/test_first_batch_workflow.py`  
**改造：** Docker test fixtures

业务流：

```text
MCP connect_ssh
→ list_java_processes
→ get_jvm_info/memory/busy_threads/deadlocks/class/method/jad
→ 获取 cookbook
→ disconnect_ssh
```

必须真实通过 transport。第一批门禁全部通过后才能进入第二批。

## 阶段 B：第二批——应用名适配

### Task B1：结构化 JavaProcess inventory

**创建：** `application_resolver.py`、`tests/unit/test_process_parser.py`  
**修改：** `list_java_processes` 返回结构。

先 parser fixtures，再真实 `/proc/jps/ps` 组件测试。旧字符串断言迁移。

### Task B2：应用匹配与多候选

**创建：** `tests/contract/test_find_java_application.py`  
**修改：** resolver/tools

依次实现 not_found、unique、ambiguous、证据排序。ambiguous 路径测试必须断言 attach 未发生。

### Task B3：JVMRegistry 与 handle

**创建：** `jvm_registry.py`、`tests/unit/test_jvm_registry.py`  
**创建：** `tests/contract/test_jvm_handle.py`

实现 handle mint/resolve/TTL/target binding；工具迁移到 handle。

### Task B4：PID restart/reuse 检测

**创建：** `tests/integration/test_jvm_identity_lifecycle.py`

真实停止/重启 fixture app；旧 handle 必须失效。使用 `/proc start_time + boot_id`。

### Task B5：Arthas lifecycle/origin

**创建：** `arthas_lifecycle.py`、`tests/unit/test_arthas_lifecycle.py`  
**创建：** `tests/integration/test_arthas_origin_cleanup.py`

按状态机逐步实现；先 EXISTING 不误停，再 STARTED_BY_PROXY TTL 清理，再 active job 保护。

### Task B6：第二批 MCP E2E

**创建：** `tests/e2e/test_application_name_workflow.py`

业务流：

```text
connect SSH
→ find unique app by name
→ receive jvm_handle
→ diagnose
→ restart app
→ old handle safely rejected
→ resolve new app
```

另建 ambiguous 流程，证明不会选错 JVM。

## 阶段 C：第三批——HTTP long-polling 与 Job（已完成）

### Task C1：Arthas HTTP capability probe

**创建：** `backends/http.py`、capability model/tests  
**修改：** Attach 参数以启用 loopback HTTP。

先真实 Arthas capability 测试，再写实现；不同 Arthas 版本不可假设相同能力。

### Task C2：SSH tunnel manager

**创建：** `ssh_tunnel.py`、`tests/unit/test_ssh_tunnel.py`、`tests/integration/test_ssh_tunnel.py`

RED/GREEN：loopback bind、forward API、共享 lease、SSH断开、cleanup。

### Task C3：短命令 HTTP backend

**创建：** `tests/integration/test_http_backend.py`

先用真实 `/api` 执行 `version/jvm/memory`。实现 timeout、parse、error mapping。

### Task C4：CLI fallback

**创建：** `tests/unit/test_backend_fallback.py`、`tests/integration/test_cli_fallback.py`

仅允许安全只读命令 fallback。测试证明 HTTP success 不调用 CLI，connection-before-submit failure 才 fallback。

### Task C5：JobRegistry 状态机

**创建：** `jobs.py`、`tests/unit/test_job_state_machine.py`

用 fake clock 和 controllable tasks 覆盖状态、TTL、配额、取消竞态；此时不接 WebSocket。

### Task C6：Arthas HTTP long-polling backend

官方 Arthas 没有 WebSocket 命令协议；使用 `/api` 的 session、async execute
与结果拉取实现长命令，并覆盖 interrupt、session close、超时和错误。

### Task C7：混合返回

**创建：** `tests/contract/test_async_observation_contract.py`

逐个实现 await=0、快速完成、转 job、上限拒绝。MCP request 本身不能因后台化报错。

### Task C8：Job tools

**创建：** `tests/contract/test_job_tools.py`

实现 get/list/stop，终态幂等，归属和 handle 校验。

### Task C9：输出分页

**创建：** `output_limiter.py`、`tests/unit/test_output_limiter.py`、`tests/contract/test_job_pagination.py`

先生成确定性 40K payload，逐页拼接断言完全相等，再实现 opaque cursor。

### Task C10：第三批业务 E2E（已由现有 fixture 与契约测试覆盖）

**创建：** `tests/e2e/test_heavenc_mode_workflow.py`

真实业务流：

```text
find app → prepare Arthas → HTTP jvm
→ start watch(await_ms=0) → 触发 Java 方法
→ get job pages → stop/idempotent stop
→ verify cleanup and CLI fallback
```

---

# 10. CI 与质量门禁

## 10.1 每个 PR 必须阻塞

```bash
python3 -m ruff check src tests
python3 -m ruff format --check src tests
python3 -m mypy src/arthas_mcp_proxy
python3 -m pytest tests/unit tests/contract -q
```

修改 `.github/workflows/ci.yml`：

- 移除 `ruff format ... || true`。
- integration 不再 `continue-on-error: true`。
- PR 上运行至少 JDK 17/21 E2E；nightly 扩到 8/11/17/21。
- clean install/startup/schema test 必须阻塞。
- Docker build + health + MCP call 必须阻塞。

## 10.2 Java/Arthas兼容矩阵

最低矩阵：

| JDK | PR | Nightly |
|---|---|---|
| 8 | 可后续加入 | 必须 |
| 11 | 可后续加入 | 必须 |
| 17 | 必须 | 必须 |
| 21 | 必须 | 必须 |

每个版本至少验证：发现、应用匹配、Attach、HTTP短命令、watch job、停止和清理。

## 10.3 完整阶段门禁

```bash
python3 -m pytest tests/unit -q
python3 -m pytest tests/contract -q
python3 -m pytest tests/integration -m integration --docker-target -q
python3 -m pytest tests/e2e -m e2e --docker-target -q
python3 -m ruff check src tests
python3 -m ruff format --check src tests
python3 -m mypy src/arthas_mcp_proxy
python3 -m build
python3 -m pip check
```

Docker：

```bash
docker compose build
docker compose up -d
# 等待 health，不使用盲目 sleep
docker compose ps
# 使用官方 MCP client 发起 tools/list 和一个真实 call
docker compose down --volumes
```

最终报告必须提供真实命令、exit code、通过/失败数量；不得只写“测试已通过”。

---

# 11. 文档与版本迁移

每批完成后更新：

- `README.md`：真实 tool catalog、Streamable HTTP 默认、SSE legacy、应用名工作流。
- `CHANGELOG.md`：新增/弃用/破坏性变化。
- 示例 MCP client 配置。
- 环境变量表：TTL、limits、transport、output。
- 错误码说明和排障建议。

版本建议：

- 第一批：`2.1.0`，新增 typed tools；旧 alias deprecated。
- 第二批：`2.2.0`，新增 app resolver/jvm_handle。
- 第三批：`2.3.0`，新增 HTTP long-polling/job。
- 下一大版本：删除 PID-only 和旧 alias；必须在此前至少保留一个小版本兼容窗口。

---

# 12. Definition of Done

功能只有同时满足以下条件才算完成：

1. 对应业务用例已在实现前编写并观察 RED。
2. 单元测试覆盖边界和故障。
3. MCP contract 测试覆盖工具名称、schema、result、isError。
4. 真实 SSH/JVM/Arthas 主路径通过。
5. 资源在成功、失败、超时、取消路径均释放。
6. 无跨主机/PID/JVM/job 状态污染。
7. 无凭据或敏感参数出现在日志和测试产物。
8. SDK、stdio、Streamable HTTP、SSE兼容行为有自动化测试。
9. README 与实际 tools/list 一致。
10. 全部质量门禁通过并报告真实输出。

---

# 13. 实现 Agent 的停机条件

出现以下情况必须停止当前阶段并报告，不得猜测或绕过：

- Arthas 官方版本的 HTTP/WS 命令与社区示例不一致。
- MCP SDK `<2` 无法表达所需 structured output/transport 契约。
- Docker/JDK版本存在真实不可兼容行为，需要调整产品范围。
- 要删除或改变公开工具参数且没有迁移方案。
- 真实集成用例无法稳定复现，试图改为 mock 才能通过。
- 同一文件 lint/type 修复连续三轮仍失败。
- 需要新增高风险远程写操作或默认暴露公网端口。

报告格式：阻塞功能、已验证事实、失败命令和原始错误、可选方案、需要用户做的决定。

---

## 最终执行顺序摘要（历史执行顺序；当前实现已收口）

```text
A1–A12：先固定 MCP 契约、稳定性、细粒度工具和真实 E2E（已完成）
       ↓ 门禁
B1–B6：应用名解析、稳定 JVM handle、PID 重用和按需 Arthas 生命周期（已完成）
       ↓ 门禁
C1–C10：SSH tunnel、HTTP短命令、HTTP long-polling 长命令、混合返回、job和分页（已完成）
       ↓ 全矩阵门禁
文档/版本/兼容迁移收尾
```

不得跳过 A 阶段；长任务必须绑定稳定 JVM identity。Arthas 长命令采用 HTTP long-polling，不实现不存在的官方 WebSocket 命令协议。

