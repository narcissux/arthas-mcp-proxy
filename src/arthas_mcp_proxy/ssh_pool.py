"""SSH Connection Pool for Arthas MCP Proxy.

Supports dynamic connection creation, session reuse, and idle timeout cleanup.

THREAD SAFETY FIX (v2):
    Paramiko Transport creates a background thread for SSH protocol handling.
    SSHClient.close() does NOT always terminate this thread immediately.
    We explicitly close the transport to prevent thread leaks.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import paramiko

logger = logging.getLogger(__name__)

IDLE_TIMEOUT = int(os.environ.get("SSH_IDLE_TIMEOUT", "300"))
MAX_SESSIONS = int(os.environ.get("SSH_MAX_SESSIONS", "20"))


@dataclass
class SSHSession:
    """Represents a pooled SSH session."""

    session_id: str
    host: str
    port: int
    username: str
    client: paramiko.SSHClient
    last_used: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def touch(self) -> None:
        """Update last used timestamp."""
        self.last_used = time.time()

    def is_idle(self) -> bool:
        """Check if session has been idle for longer than IDLE_TIMEOUT."""
        return (time.time() - self.last_used) > IDLE_TIMEOUT


def _safe_close_client(client: paramiko.SSHClient | None) -> None:
    """Safely close SSHClient AND its underlying Transport thread.

    CRITICAL: paramiko SSHClient.close() does NOT always kill the
    Transport's background thread. Explicit transport.close() is required
    to prevent 'can't start new thread' errors from thread leaks.
    """
    if client is None:
        return
    with contextlib.suppress(Exception):
        transport = client.get_transport()
        if transport is not None:
            transport.close()
    with contextlib.suppress(Exception):
        client.close()


def _count_threads() -> int:
    """Return current active thread count (for diagnostics)."""
    return threading.active_count()


class SSHConnectionPool:
    """Thread-safe SSH connection pool with idle timeout cleanup.

    Connections are keyed by user@host:port and cached for reuse.
    A background thread periodically cleans up idle connections.
    """

    def __init__(self, idle_timeout: int = IDLE_TIMEOUT) -> None:
        self.idle_timeout = idle_timeout
        self._sessions: dict[str, SSHSession] = {}
        self._lock = threading.Lock()
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()
        logger.info(
            "SSH connection pool initialized (idle_timeout=%ds, max_sessions=%d, threads=%d)",
            idle_timeout,
            MAX_SESSIONS,
            _count_threads(),
        )

    def _make_key(self, host: str, port: int, username: str) -> str:
        """Generate cache key from connection parameters."""
        return f"{username}@{host}:{port}"

    def connect(
        self,
        host: str,
        port: int = 22,
        username: str = "root",
        password: str | None = None,
        key_path: str | None = None,
        key_string: str | None = None,
        timeout: int = 30,
    ) -> str:
        """Create or reuse an SSH connection.

        Args:
            host: Target hostname or IP address.
            port: SSH port.
            username: SSH username.
            password: Password for authentication (mutually exclusive with key).
            key_path: Path to private key file.
            key_string: Private key content as string.
            timeout: Connection timeout in seconds.

        Returns:
            session_id: Unique identifier for the established session.

        Raises:
            ValueError: If no authentication method is provided.
        """
        key = self._make_key(host, port, username)

        with self._lock:
            if len(self._sessions) >= MAX_SESSIONS and key not in self._sessions:
                logger.warning(
                    "Session pool full (%d/%d), cleaning idle sessions",
                    len(self._sessions),
                    MAX_SESSIONS,
                )
                self._cleanup_idle_unsafe()

            if key in self._sessions:
                session = self._sessions[key]
                transport = session.client.get_transport()
                if transport is not None and transport.is_active():
                    session.touch()
                    logger.info(
                        "Reusing SSH session: %s (id=%s, threads=%d)",
                        key,
                        session.session_id,
                        _count_threads(),
                    )
                    return session.session_id

                logger.warning("Stale SSH session removed: %s", key)
                _safe_close_client(session.client)
                del self._sessions[key]

        # Create new SSH connection
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # noqa: S507

        connect_kwargs: dict[str, Any] = {
            "hostname": host,
            "port": port,
            "username": username,
            "timeout": timeout,
            "look_for_keys": False,
        }

        if password:
            connect_kwargs["password"] = password
            logger.info("Connecting to %s using password auth", key)
        elif key_path:
            connect_kwargs["key_filename"] = key_path
            logger.info("Connecting to %s using key file: %s", key, key_path)
        elif key_string:
            key_io = __import__("io").StringIO(key_string)
            private_key: paramiko.PKey | None = None
            for key_cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
                key_io.seek(0)
                try:
                    private_key = key_cls.from_private_key(key_io)
                    break
                except (paramiko.SSHException, ValueError):
                    continue
            if private_key is None:
                raise ValueError("Unable to parse SSH private key. Supported: RSA, Ed25519, ECDSA.")
            connect_kwargs["pkey"] = private_key
            logger.info("Connecting to %s using key string (%s)", key, private_key.get_name())
        else:
            raise ValueError("One of password, key_path, or key_string must be provided")

        try:
            client.connect(**connect_kwargs)
        except Exception:
            _safe_close_client(client)
            logger.error("SSH connection failed to %s (threads=%d)", key, _count_threads())
            raise

        session_id = str(uuid.uuid4())[:8]
        session = SSHSession(
            session_id=session_id,
            host=host,
            port=port,
            username=username,
            client=client,
        )

        with self._lock:
            self._sessions[key] = session

        logger.info(
            "New SSH session: %s (id=%s, threads=%d)",
            key,
            session_id,
            _count_threads(),
        )
        return session_id

    def get_session(self, session_id: str) -> SSHSession | None:
        """Look up a session by its session_id."""
        with self._lock:
            for session in self._sessions.values():
                if session.session_id == session_id:
                    transport = session.client.get_transport()
                    if transport is not None and transport.is_active():
                        session.touch()
                        return session

                    logger.warning("Session %s transport inactive", session_id)
                    _safe_close_client(session.client)
                    del self._sessions[session.session_id]
                    return None
        logger.warning("Session %s not found", session_id)
        return None

    def get_session_by_host(self, host: str, port: int, username: str) -> SSHSession | None:
        """Look up a session by host credentials."""
        key = self._make_key(host, port, username)
        with self._lock:
            session = self._sessions.get(key)
            if session:
                transport = session.client.get_transport()
                if transport is not None and transport.is_active():
                    session.touch()
                    return session

                logger.warning("Session %s transport dead, removing", key)
                _safe_close_client(session.client)
                del self._sessions[key]
        return None

    def disconnect(self, session_id: str) -> bool:
        """Close and remove a specific session."""
        with self._lock:
            for key, session in list(self._sessions.items()):
                if session.session_id == session_id:
                    _safe_close_client(session.client)
                    del self._sessions[key]
                    logger.info(
                        "Session %s disconnected (threads=%d)",
                        session_id,
                        _count_threads(),
                    )
                    return True
        return False

    def _cleanup_loop(self) -> None:
        """Background thread: periodically clean up idle connections."""
        while True:
            time.sleep(30)
            try:
                self._cleanup_idle()
            except Exception as e:
                logger.error("Cleanup loop error: %s", e)

    def _cleanup_idle(self) -> None:
        """Remove sessions idle longer than idle_timeout."""
        with self._lock:
            self._cleanup_idle_unsafe()

    def _cleanup_idle_unsafe(self) -> None:
        """Internal: cleanup without lock (caller must hold lock)."""
        idle_keys = [key for key, session in self._sessions.items() if session.is_idle()]
        for key in idle_keys:
            session = self._sessions[key]
            _safe_close_client(session.client)
            del self._sessions[key]
            logger.info(
                "Idle session removed: %s (id=%s, idle=%ds, threads=%d)",
                key,
                session.session_id,
                self.idle_timeout,
                _count_threads(),
            )

    def shutdown(self) -> None:
        """Close all connections and stop cleanup thread."""
        with self._lock:
            for _key, session in self._sessions.items():
                _safe_close_client(session.client)
            self._sessions.clear()
        logger.info("Pool shutdown complete (threads=%d)", _count_threads())


_pool: SSHConnectionPool | None = None
_pool_lock = threading.Lock()


def get_connection_pool() -> SSHConnectionPool:
    """Get or create the global SSH connection pool singleton."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = SSHConnectionPool()
    return _pool
