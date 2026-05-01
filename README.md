# Arthas MCP Proxy

MCP Server for JVM diagnostics via SSH + [Arthas](https://arthas.aliyun.com/).

Provides 26+ diagnostic tools through the Model Context Protocol (MCP), enabling
AI assistants to perform thread dumps, heap analysis, method tracing, CPU
profiling, and more on remote Java processes.

## Features

- **Full Arthas command suite** — `thread`, `trace`, `watch`, `heapdump`, `profiler`, `jad`, etc.
- **Multi-target SSH** — connect to multiple JVM hosts from a single server
- **Cross-user diagnosis** — automatically uses `sudo -u <owner>` when SSH user != process owner
- **Concurrent-safe** — per-PID attach locks + three-level reuse (cache → detect → attach)
- **SSE & stdio transports** — works with Cursor, Claude Desktop, and other MCP clients

## Quick Start

### Docker (recommended)

```bash
docker run -p 8000:8000 ghcr.io/narcissux/arthas-mcp-proxy:latest
```

### From source

```bash
git clone https://github.com/narcissux/arthas-mcp-proxy.git
cd arthas-mcp-proxy
pip install -e ".[dev]"
python -m arthas_mcp_proxy --transport sse --port 8000
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
| `thread_dump` | Thread dump (top N by CPU) |
| `heap_info` | Memory dashboard |
| `watch_method` | Watch method params/return values |
| `exec_command` | Universal Arthas command executor (26+ commands) |
| `install_arthas` | Install Arthas on target server |
| `disconnect_ssh` | Disconnect and release resources |

## Development

### Running tests

```bash
# Unit tests only (mocked, no external dependencies)
pytest tests/ --ignore=tests/integration/

# Integration tests with auto-managed Docker target (recommended)
pytest tests/integration/ -m integration -v --docker-target

# Integration tests against a remote target (env vars required)
export TEST_SSH_HOST=your-server
export TEST_SSH_USER=your-username
export TEST_SSH_PASSWORD=your-password
pytest tests/integration/ -m integration -v

# Manual two-step Docker target
docker compose -f docker-compose.test.yml up --build -d
export TEST_SSH_HOST=localhost TEST_SSH_USER=testuser TEST_SSH_PASSWORD=testpass
pytest tests/integration/ -m integration -v
docker compose -f docker-compose.test.yml down --volumes
```

### Code quality

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run lint
ruff check src/ tests/
ruff format --check src/ tests/

# Run type check
mypy src/arthas_mcp_proxy

# Run tests
pytest -v

# Run with coverage
pytest --cov=arthas_mcp_proxy --cov-report=html
```

## Project Structure

```
.
├── src/
│   └── arthas_mcp_proxy/
│       ├── __init__.py          # Package init
│       ├── __main__.py          # python -m arthas_mcp_proxy
│       ├── server.py            # MCP server & tools
│       ├── arthas_client.py     # Arthas attach & command execution
│       ├── ssh_pool.py          # SSH connection pool
│       └── decorators.py        # @require_session and other decorators
├── tests/
│   ├── conftest.py              # Shared fixtures (mock SSH session, state cleanup)
│   ├── test_decorators.py       # @require_session tests
│   ├── test_arthas_client.py    # Concurrency & logic tests
│   ├── test_ssh_pool.py         # Connection pool tests
│   └── integration/
│       ├── conftest.py          # Integration env validation & Docker check
│       └── test_real_jvm.py     # Real JVM diagnostic tests via SSH
├── pyproject.toml               # Project config, deps, tool settings
├── Dockerfile
├── docker-compose.yml           # Production deployment
├── docker-compose.test.yml      # Test infrastructure (SSH + Java container)
├── Dockerfile.test-target       # Test target image (Java + math-game.jar)
└── README.md
```

### Test categories

| Category | Command | Requirements |
|----------|---------|-------------|
| Unit tests | `pytest tests/ --ignore=tests/integration/` | None (fully mocked) |
| Integration (remote) | `pytest tests/integration/ -m integration` | SSH target with Java |
| Integration (Docker) | `docker compose -f docker-compose.test.yml up` + pytest | Docker daemon |

### Environment variables for integration tests

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TEST_SSH_HOST` | Yes | — | Target hostname or IP |
| `TEST_SSH_USER` | Yes | — | SSH username |
| `TEST_SSH_PASSWORD` | Yes | — | SSH password |
| `TEST_SSH_PORT` | No | 22 | SSH port |
| `TEST_TARGET_PID` | No | auto | Specific PID to diagnose |

**Security**: Never commit credentials. Use environment variables or a `.env`
file (ignored by `.gitignore`).

## License

MIT
