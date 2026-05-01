#!/usr/bin/env python3
"""Arthas MCP Proxy Server.

Provides MCP tools for JVM diagnostics via SSH + Arthas.
Supports both SSE and stdio transport modes.

Usage:
    # SSE mode (default)
    arthas-mcp-proxy --transport sse --host 0.0.0.0 --port 8000

    # stdio mode
    arthas-mcp-proxy --transport stdio

    # Or via Python module
    python -m arthas_mcp_proxy --transport sse
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from arthas_mcp_proxy.arthas_client import ArthasClient
from arthas_mcp_proxy.decorators import require_session, set_fallback_credential_getter
from arthas_mcp_proxy.ssh_pool import get_connection_pool

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)

# ─── Global state ────────────────────────────────────────────────────────────
_transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
mcp = FastMCP("arthas-mcp-proxy", transport_security=_transport_security)

# Session credential cache (for fallback reconnection)
_session_store: dict[str, dict[str, str | int]] = {}
_store_lock = threading.Lock()


def _store_session(session_id: str, host: str, port: int, username: str) -> None:
    with _store_lock:
        _session_store[session_id] = {
            "host": host,
            "port": port,
            "username": username,
        }


def _get_session_credentials(session_id: str) -> dict[str, str | int] | None:
    with _store_lock:
        return _session_store.get(session_id)


# Register fallback credential getter with the decorator module
set_fallback_credential_getter(_get_session_credentials)


def _coerce_params(kwargs: dict[str, object]) -> dict[str, object]:
    """Coerce parameter types to handle clients that send strings instead of ints."""
    coerced = dict(kwargs)
    int_fields = ["port", "pid", "top_n", "times", "timeout"]
    bool_fields = ["watch_params", "watch_return"]

    for field in int_fields:
        if field in coerced and coerced[field] is not None:
            val = coerced[field]
            if isinstance(val, (int, str, float)):
                try:
                    coerced[field] = int(val)
                except (ValueError, TypeError):
                    logger.warning("Cannot coerce %s=%s to int", field, val)

    for field in bool_fields:
        if field in coerced and coerced[field] is not None:
            val = coerced[field]
            if isinstance(val, str):
                coerced[field] = val.lower() in ("true", "1", "yes", "on")

    return coerced


def _dump_params(tool_name: str, kwargs: dict[str, object]) -> None:
    """Log incoming parameters for debugging."""
    safe = {
        k: f"{v[:20]}..." if isinstance(v, str) and len(v) > 50 else v for k, v in kwargs.items()
    }
    logger.info("[PARAMS] %s: %s", tool_name, safe)


# ─── MCP Tools ───────────────────────────────────────────────────────────────


@mcp.tool()
def connect_ssh(
    host: str,
    username: str = "root",
    port: int = 22,
    password: str | None = None,
    key_path: str | None = None,
    key_string: str | None = None,
) -> str:
    """Establish an SSH connection to a remote server."""
    if isinstance(port, str):
        port = int(port)

    try:
        pool = get_connection_pool()
        session_id = pool.connect(
            host=host,
            port=port,
            username=username,
            password=password,
            key_path=key_path,
            key_string=key_string,
        )
        _store_session(session_id, host, port, username)
        return f"SSH connection established. Session ID: {session_id}"
    except Exception as e:
        logger.error("SSH connection failed to %s:%d: %s", host, port, e)
        return f"SSH connection failed: {e}"


@mcp.tool()
def list_java_processes(session_id: str) -> str:
    """List all Java processes on the remote server."""
    pool = get_connection_pool()
    session = pool.get_session(session_id)

    if not session:
        creds = _get_session_credentials(session_id)
        if creds:
            session = pool.get_session_by_host(
                str(creds["host"]), int(creds["port"]), str(creds["username"])
            )
        if not session:
            return "Error: Session not found or expired. Please reconnect using connect_ssh."

    try:
        client = ArthasClient(session)
        return client.list_java_processes()
    except Exception as e:
        logger.error("list_java_processes failed: %s", e)
        return f"Error listing Java processes: {e}"


@mcp.tool()
@require_session()
def thread_dump(session: object, pid: int, top_n: int = 20) -> str:
    """Get thread dump (top N threads by CPU usage) for a Java process."""
    if isinstance(pid, str):
        pid = int(pid)
    if isinstance(top_n, str):
        top_n = int(top_n)

    try:
        client = ArthasClient(session)  # type: ignore[arg-type]
        return client.thread_dump(pid=pid, top_n=top_n)
    except Exception as e:
        logger.error("thread_dump failed for PID %d: %s", pid, e)
        return f"Error getting thread dump: {e}"


@mcp.tool()
@require_session()
def heap_info(session: object, pid: int) -> str:
    """Get heap and memory dashboard for a Java process."""
    if isinstance(pid, str):
        pid = int(pid)

    try:
        client = ArthasClient(session)  # type: ignore[arg-type]
        return client.heap_info(pid=pid)
    except Exception as e:
        logger.error("heap_info failed for PID %d: %s", pid, e)
        return f"Error getting heap info: {e}"


@mcp.tool()
@require_session()
def watch_method(
    session: object,
    pid: int,
    class_pattern: str,
    method_pattern: str,
    watch_params: bool = True,
    watch_return: bool = True,
    condition: str | None = None,
    times: int = 5,
) -> str:
    """Watch method execution - monitor input parameters and/or return values."""
    if isinstance(pid, str):
        pid = int(pid)
    if isinstance(times, str):
        times = int(times)
    if isinstance(watch_params, str):
        watch_params = watch_params.lower() in ("true", "1", "yes")
    if isinstance(watch_return, str):
        watch_return = watch_return.lower() in ("true", "1", "yes")

    try:
        client = ArthasClient(session)  # type: ignore[arg-type]
        return client.watch_method(
            pid=pid,
            class_pattern=class_pattern,
            method_pattern=method_pattern,
            watch_params=watch_params,
            watch_return=watch_return,
            condition=condition,
            times=times,
        )
    except Exception as e:
        logger.error("watch_method failed: %s", e)
        return f"Error watching method: {e}"


@mcp.tool()
@require_session()
def exec_command(session: object, pid: int, command: str, timeout: int = 60) -> str:
    """
    Execute an arbitrary Arthas command on the target JVM.

    This is the universal Arthas command executor. All 26+ native Arthas MCP tools
    are available through this single command.

    --- JVM & Runtime Diagnostics ---
    dashboard -n 1              JVM overview: threads, memory, GC, runtime
    jvm                         JVM runtime info, classpath, arguments
    sysprop [pattern]           System properties
    sysenv [key]                Environment variables
    vmoption [key] [value]      JVM options (read/set)
    memory                      Memory pool details
    perfcounter [name]          JVM performance counters
    mbean [name]                MBean information
    getstatic class field       Read static field value

    --- Thread Diagnostics ---
    thread -n 20                Top 20 threads by CPU
    thread <tid>                Thread detail and stack trace
    thread -b                   Detect deadlocks
    thread --state RUNNABLE     Filter by state

    --- Class & Bytecode ---
    sc -d com.example.Service   Search class info
    sm -d com.example.Service   Search method info
    jad --source-only Class     Decompile to Java source
    classloader -t              ClassLoader tree
    dump -d /tmp Class          Dump class bytecode
    redefine /tmp/Class.class   Hot-reload (emergency only)

    --- Method Tracing ---
    trace Class method '#cost>100' -n 5    Execution trace with timing
    stack Class method -n 5                Call stack
    monitor -c 5 Class method -n 3         QPS/RT monitor
    watch Class method '{params,returnObj}' -n 5 -x 3
    tt -t Class method -n 10               Record invocations

    --- Heap & Profiler ---
    heapdump /tmp/heap.hprof    Generate heap dump
    profiler start              Start CPU profiling
    profiler stop --file /tmp/flame.html   Flame graph
    profiler start --event alloc           Memory allocation profile

    --- Management ---
    version                     Arthas agent version
    help                        Show help
    stop                        Detach agent

    Examples:
        trace com.order.Service createOrder '#cost>500' -n 10
        heapdump /tmp/heap.hprof
        jad --source-only com.order.ServiceImpl
        monitor -c 60 com.api.Controller query -n 3
    """
    if isinstance(pid, str):
        pid = int(pid)
    if isinstance(timeout, str):
        timeout = int(timeout)

    try:
        client = ArthasClient(session)  # type: ignore[arg-type]
        return client.exec_command(pid=pid, command=command, timeout=timeout)
    except Exception as e:
        logger.error("exec_command failed: %s", e)
        return f"Error executing command: {e}"


@mcp.tool()
@require_session()
def install_arthas(session: object, install_type: str = "auto") -> str:
    """Install Arthas on the target server if not already present."""
    try:
        client = ArthasClient(session)  # type: ignore[arg-type]
        return client.install_arthas(install_type=install_type)
    except Exception as e:
        logger.error("install_arthas failed: %s", e)
        return f"Arthas installation failed: {e}"


@mcp.tool()
def disconnect_ssh(session_id: str) -> str:
    """Disconnect an SSH session and release resources."""
    pool = get_connection_pool()
    if pool.disconnect(session_id):
        with _store_lock:
            _session_store.pop(session_id, None)
        return f"Session {session_id} disconnected successfully."
    return f"Session {session_id} not found."


# ─── Transport ───────────────────────────────────────────────────────────────


def run_sse(host: str = "0.0.0.0", port: int = 8000, log_level: str = "info") -> None:  # noqa: S104
    app = mcp.sse_app()
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level,
        forwarded_allow_ips="*",
    )


async def run_stdio() -> None:
    await mcp.run_stdio_async()


def main() -> None:
    parser = argparse.ArgumentParser(description="Arthas MCP Proxy Server")
    parser.add_argument("--transport", choices=["sse", "stdio"], default="sse")
    parser.add_argument(
        "--host",
        default=os.environ.get("MCP_HOST", "0.0.0.0"),  # noqa: S104
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MCP_PORT", "8000")),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    args = parser.parse_args()
    logging.getLogger().setLevel(getattr(logging, args.log_level))

    logger.info("Starting Arthas MCP Proxy (transport=%s)", args.transport)

    if args.transport == "sse":
        run_sse(host=args.host, port=args.port, log_level=args.log_level.lower())
    else:
        asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
