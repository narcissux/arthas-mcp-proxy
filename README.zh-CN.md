# Arthas MCP Proxy

[English](README.md) | [简体中文](README.zh-CN.md)

一个通过 SSH 和 [Arthas](https://arthas.aliyun.com/) 实现 JVM 诊断的 MCP 服务器。

本项目通过模型上下文协议（Model Context Protocol，MCP）提供 26 种以上的诊断工具，
让 AI 助手能够对远程 Java 进程执行线程转储、堆分析、方法追踪、CPU 性能分析等操作。

## 功能特性

- **完整的 Arthas 命令集**：支持 `thread`、`trace`、`watch`、`heapdump`、`profiler`、`jad` 等命令
- **多目标 SSH**：通过单个服务器连接多个 JVM 主机
- **跨用户诊断**：当 SSH 用户与进程所有者不同时，自动使用 `sudo -u <owner>` 执行操作
- **并发安全**：采用基于 PID 的附加锁，并通过“缓存 → 检测 → 附加”三级机制复用连接
- **支持 SSE 和 stdio 传输**：可与 Cursor、Claude Desktop 及其他 MCP 客户端配合使用

## 快速开始

### Docker（推荐）

```bash
docker run -p 8000:8000 ghcr.io/narcissux/arthas-mcp-proxy:latest
```

### 从源码运行

```bash
git clone https://github.com/narcissux/arthas-mcp-proxy.git
cd arthas-mcp-proxy
pip install -e ".[dev]"
python -m arthas_mcp_proxy --transport sse --port 8000
```

### Cursor / MCP 客户端配置

```json
{
  "mcpServers": {
    "arthas": {
      "url": "http://localhost:8000/sse"
    }
  }
}
```

## 可用工具

| 工具 | 用途 |
|------|------|
| `connect_ssh` | 与目标服务器建立 SSH 连接 |
| `list_java_processes` | 列出 Java 进程及其 Arthas 状态 |
| `thread_dump` | 获取线程转储（按 CPU 使用率显示前 N 个线程） |
| `heap_info` | 查看内存信息面板 |
| `watch_method` | 观察方法参数和返回值 |
| `exec_command` | 通用 Arthas 命令执行器（支持 26 种以上命令） |
| `install_arthas` | 在目标服务器上安装 Arthas |
| `disconnect_ssh` | 断开连接并释放资源 |

## 开发

### 运行测试

```bash
# 仅运行单元测试（使用模拟对象，无外部依赖）
pytest tests/ --ignore=tests/integration/

# 使用自动管理的 Docker 目标运行集成测试（推荐）
pytest tests/integration/ -m integration -v --docker-target

# 针对远程目标运行集成测试（需要设置环境变量）
export TEST_SSH_HOST=your-server
export TEST_SSH_USER=your-username
export TEST_SSH_PASSWORD=your-password
pytest tests/integration/ -m integration -v

# 手动分两步启动 Docker 测试目标
docker compose -f docker-compose.test.yml up --build -d
export TEST_SSH_HOST=localhost TEST_SSH_USER=testuser TEST_SSH_PASSWORD=testpass
pytest tests/integration/ -m integration -v
docker compose -f docker-compose.test.yml down --volumes
```

### 代码质量

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行代码检查
ruff check src/ tests/
ruff format --check src/ tests/

# 运行类型检查
mypy src/arthas_mcp_proxy

# 运行测试
pytest -v

# 运行测试并生成覆盖率报告
pytest --cov=arthas_mcp_proxy --cov-report=html
```

## 项目结构

```text
.
├── src/
│   └── arthas_mcp_proxy/
│       ├── __init__.py          # 包初始化文件
│       ├── __main__.py          # python -m arthas_mcp_proxy 入口
│       ├── server.py            # MCP 服务器和工具
│       ├── arthas_client.py     # Arthas 附加和命令执行
│       ├── ssh_pool.py          # SSH 连接池
│       └── decorators.py        # @require_session 等装饰器
├── tests/
│   ├── conftest.py              # 共享测试夹具（模拟 SSH 会话、状态清理）
│   ├── test_decorators.py       # @require_session 测试
│   ├── test_arthas_client.py    # 并发和逻辑测试
│   ├── test_ssh_pool.py         # 连接池测试
│   └── integration/
│       ├── conftest.py          # 集成测试环境校验和 Docker 检查
│       └── test_real_jvm.py     # 通过 SSH 诊断真实 JVM 的测试
├── pyproject.toml               # 项目配置、依赖和工具设置
├── entrypoint.sh                # 测试目标容器启动脚本
├── Dockerfile
├── docker-compose.yml           # 生产部署配置
├── docker-compose.test.yml      # 测试基础设施（SSH + Java 容器）
├── Dockerfile.test-target       # 测试目标镜像（Java + math-game.jar）
├── README.md                    # 英文说明文档
└── README.zh-CN.md              # 简体中文说明文档
```

### 测试类别

| 类别 | 命令 | 要求 |
|------|------|------|
| 单元测试 | `pytest tests/ --ignore=tests/integration/` | 无（完全使用模拟对象） |
| 集成测试（远程） | `pytest tests/integration/ -m integration` | 运行 Java 的 SSH 目标服务器 |
| 集成测试（Docker） | `docker compose -f docker-compose.test.yml up` 后运行 pytest | Docker 守护进程 |

### 集成测试环境变量

| 变量 | 必需 | 默认值 | 说明 |
|------|------|--------|------|
| `TEST_SSH_HOST` | 是 | 无 | 目标主机名或 IP 地址 |
| `TEST_SSH_USER` | 是 | 无 | SSH 用户名 |
| `TEST_SSH_PASSWORD` | 是 | 无 | SSH 密码 |
| `TEST_SSH_PORT` | 否 | `22` | SSH 端口 |
| `TEST_TARGET_PID` | 否 | 自动检测 | 要诊断的特定进程 PID |

**安全提示**：请勿提交任何凭据。请使用环境变量或已被 `.gitignore` 忽略的 `.env` 文件。

## 许可证

MIT
