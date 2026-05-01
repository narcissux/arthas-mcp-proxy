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
│   ├── conftest.py              # Shared fixtures
│   ├── test_decorators.py       # @require_session tests
│   ├── test_arthas_client.py    # Concurrency & logic tests
│   └── test_ssh_pool.py         # Connection pool tests
├── pyproject.toml               # Project config, deps, tool settings
├── Dockerfile
└── docker-compose.yml
```

## License

MIT
