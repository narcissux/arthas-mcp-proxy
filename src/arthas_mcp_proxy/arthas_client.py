"""Arthas Client - Multi-PID concurrent diagnostic support.

CONCURRENCY MODEL:
    Each JVM PID has exactly ONE Arthas agent binding to one local port.
    All access to per-PID state is protected by _PID_STATE_LOCK to ensure
    thread safety when multiple MCP requests hit the same PID concurrently.

    Three-level reuse strategy (fastest to slowest):
    1. _PID_STATE cache hit: direct port lookup, zero SSH calls (~1ms)
    2. Cross-session agent reuse: detect existing agent via ss -tlnp (~100ms)
    3. Full attach: arthas-boot.jar --attach-only (~2-3s)

THREAD SAFETY:
    - _PID_STATE_LOCK (threading.Lock): protects all reads/writes to _PID_STATE
    - Per-PID attach serialization: only one thread can attach to a given PID
    - Port allocation is synchronized: two threads for different PIDs can
      allocate ports concurrently, but same-PID attach is serialized

CROSS-USER DIAGNOSIS (v4):
    JVM Attach requires executing user == process owner.
    Auto-detects via 'ps -o user= -p <pid>' and wraps with 'sudo -u <owner>'.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

if TYPE_CHECKING:
    from arthas_mcp_proxy.ssh_pool import SSHSession

from .application_resolver import identity_complete, validate_process_identity
from .arthas_http import ArthasHttpClient, ArthasHttpError, ArthasHttpStreamingClient
from .arthas_lifecycle import (
    ArthasInstance,
    ArthasInstanceRegistry,
    ArthasOrigin,
    PreparedArthas,
)
from .errors import DomainError
from .models import ErrorCode
from .observation_policy import is_observation_command
from .process_inventory import collect_inventory_over_ssh
from .target_state import TargetIdentity

_LIFECYCLE_REGISTRY = ArthasInstanceRegistry()

logger = logging.getLogger(__name__)

# Per-PID state: pid -> {"port": int, "owner": Optional[str]}
_PID_STATE: dict[int | TargetIdentity, dict[str, int | str | None]] = {}
_PID_STATE_LOCK = threading.Lock()

# Per-PID attach locks: ensures only one thread attaches to a given PID
_ATTACH_LOCKS: dict[int | TargetIdentity, threading.Lock] = {}
_ATTACH_LOCKS_MASTER = threading.Lock()

# Module-level jar cache to avoid re-finding arthas-client.jar
_jar_cache: dict[tuple[int | TargetIdentity, str | None], str] = {}


def _get_attach_lock(pid: int | TargetIdentity) -> threading.Lock:
    """Get or create a per-PID attach lock."""
    with _ATTACH_LOCKS_MASTER:
        if pid not in _ATTACH_LOCKS:
            _ATTACH_LOCKS[pid] = threading.Lock()
        return _ATTACH_LOCKS[pid]


def _state_key(
    session: SSHSession, pid: int, start_time: str | None = None
) -> int | TargetIdentity:
    """Return a target-scoped key, including optional JVM start metadata."""
    host = getattr(session, "host", None)
    port = getattr(session, "port", None)
    username = getattr(session, "username", None)
    session_start_time = getattr(session, "start_time", None)
    if start_time is None and isinstance(session_start_time, str):
        start_time = session_start_time
    if isinstance(host, str) and isinstance(port, int) and isinstance(username, str):
        return TargetIdentity(str(host), int(port), str(username), pid, start_time)
    return pid


def _jar_cache_key(
    session: SSHSession,
    pid: int,
    owner: str | None,
    start_time: str | None = None,
) -> tuple[int | TargetIdentity, str | None]:
    """Build a stable cache key from remote target/process identity."""
    return (_state_key(session, pid, start_time), owner)


def _exec_ssh(
    session: SSHSession,
    command: str,
    timeout: int = 60,
    sudo_user: str | None = None,
) -> tuple[str, str, int]:
    """Execute command over SSH, optionally with sudo -u <user>."""
    if sudo_user:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", sudo_user):
            raise ValueError("Invalid sudo user")
        cmd_trim = command.strip()
        if not cmd_trim.startswith(("bash ", "sudo ")):
            command = f"sudo -u {sudo_user} env HOME=/tmp {command}"
        else:
            command = f"sudo -u {sudo_user} {command}"

    logger.debug("[SSH-EXEC] %s", command[:120])
    _stdin, stdout, stderr = session.client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    rc = stdout.channel.recv_exit_status()

    if rc != 0:
        logger.warning("[SSH-EXEC] RC=%d, cmd=%.80s, stderr=%.200s", rc, command, err)
    return out, err, rc


def _get_current_user(session: SSHSession) -> str:
    stdout, _, _ = _exec_ssh(session, "whoami", timeout=5)
    return stdout.strip()


def _get_pid_owner(session: SSHSession, pid: int) -> str:
    stdout, _, _ = _exec_ssh(session, f"ps -o user= -p {pid}", timeout=5)
    return stdout.strip()


def _get_sudo_user(session: SSHSession, pid: int) -> str | None:
    """Determine if sudo is needed. Returns PID owner if current user != owner."""
    current = _get_current_user(session)
    owner = _get_pid_owner(session, pid)

    if not owner:
        logger.warning(
            "[SUDO] Could not determine owner of PID %d, assuming current=%s",
            pid,
            current,
        )
        return None

    if current != owner:
        logger.info("[SUDO] Cross-user: SSH=%s, owner=%s, using sudo", current, owner)
        return owner

    logger.debug("[SUDO] Same user: %s", current)
    return None


def _get_arthas_base_dir() -> str:
    """Universal Arthas installation directory shared by all users."""
    return "/tmp/arthas-all"  # noqa: S108


def _find_arthas_path(session: SSHSession, owner: str | None = None) -> str:
    search_paths = [
        "/tmp/arthas-all/as.sh",  # noqa: S108
        "~/.arthas/as.sh",
        "/opt/arthas/as.sh",
        "as.sh",
    ]
    for path in search_paths:
        cmd = f"test -f {path} && echo 'FOUND:{path}' || echo 'NOT_FOUND'"
        stdout, _, rc = _exec_ssh(session, cmd, timeout=10, sudo_user=owner)
        if rc == 0 and "FOUND:" in stdout:
            found = stdout.strip().split("FOUND:")[1]
            logger.info("[ARTHAS-PATH] Found as.sh at: %s", found)
            return found

    logger.error("[ARTHAS-PATH] as.sh not found")
    raise RuntimeError("Arthas not found. Use install_arthas tool to install.")


def _find_arthas_client_jar(session: SSHSession, arthas_path: str, owner: str | None = None) -> str:
    base_dir = arthas_path.rsplit("/", 1)[0] if "/" in arthas_path else "."

    search_paths = [
        f"{base_dir}/arthas-client.jar",
        f"{base_dir}/lib/*/arthas/arthas-client.jar",
        "/tmp/arthas-all/arthas-client.jar",  # noqa: S108
        "~/.arthas/arthas-client.jar",
    ]
    for pattern in search_paths:
        cmd = f"ls {pattern} 2>/dev/null | head -1"
        stdout, _, rc = _exec_ssh(session, cmd, timeout=10, sudo_user=owner)
        if rc == 0 and stdout.strip():
            jar_path = stdout.strip().split("\n")[0].strip()
            verify_cmd = f"test -f '{jar_path}' && echo 'OK' || echo 'FAIL'"
            vout, _, _ = _exec_ssh(session, verify_cmd, timeout=5, sudo_user=owner)
            if "OK" in vout:
                logger.info("[CLIENT-JAR] Found arthas-client.jar at: %s", jar_path)
                return jar_path

    fallback = f"{base_dir}/arthas-client.jar"
    logger.warning("[CLIENT-JAR] Using fallback path: %s", fallback)
    return fallback


def _find_local_arthas_bundle() -> str | None:
    paths = [
        "/app/arthas-bin.zip",
        "/app/packages/arthas-bin.zip",
        "/opt/arthas-bin.zip",
        "/arthas-bin.zip",
    ]
    for path in paths:
        if os.path.isfile(path):
            size_mb = os.path.getsize(path) / (1024 * 1024)
            logger.info("[BUNDLE] Found arthas-bin.zip: %s (%.1f MB)", path, size_mb)
            return path
    logger.warning("[BUNDLE] No arthas-bin.zip found on MCP server")
    return None


def _copy_arthas_to_target(session: SSHSession, owner: str | None = None) -> bool:
    local_path = _find_local_arthas_bundle()
    if not local_path:
        return False

    remote_path = "/tmp/arthas-bin.zip"  # noqa: S108
    try:
        logger.info("[SFTP] Pushing %s -> target:%s", local_path, remote_path)
        sftp = session.client.open_sftp()
        sftp.put(local_path, remote_path)
        sftp.close()

        if owner:
            _exec_ssh(
                session,
                f"chown {owner}:{owner} {remote_path} 2>/dev/null || true",
                timeout=5,
            )

        stdout, _, rc = _exec_ssh(
            session,
            f"test -f {remote_path} && echo 'OK' || echo 'FAIL'",
            timeout=10,
            sudo_user=owner,
        )
        if rc == 0 and "OK" in stdout:
            logger.info(
                "[SFTP] Push successful (%.1f MB)",
                os.path.getsize(local_path) / (1024 * 1024),
            )
            return True
        logger.error("[SFTP] File verification failed after put")
        return False
    except Exception as e:
        logger.error("[SFTP] Push failed: %s", e)
        return False


def _get_java_home(session: SSHSession, owner: str | None = None) -> str:
    cmd = (
        "echo ${JAVA_HOME:-$(readlink -f $(which java 2>/dev/null) 2>/dev/null "
        "| sed 's|/bin/java||; s|/jre||')}]"
    )
    stdout, _, rc = _exec_ssh(session, cmd, timeout=10, sudo_user=owner)
    if rc == 0 and stdout.strip():
        return stdout.strip()
    return ""


def _parse_ss_listen_ports(stdout: str) -> list[int]:
    """Extract unique listen ports from ``ss -tlnp`` output."""
    patterns = [
        r"127\.0\.0\.1:(\d+)",
        r"\[::ffff:127\.0\.0\.1\]:(\d+)",
        r"\*:(\d+)",
    ]
    ports_found: list[str] = []
    for pat in patterns:
        ports_found.extend(re.findall(pat, stdout))

    if not ports_found:
        for line in stdout.split("\n"):
            if "LISTEN" in line:
                m = re.search(r":(\d+)", line)
                if m:
                    ports_found.append(m.group(1))

    seen: set[int] = set()
    ports: list[int] = []
    for raw in ports_found:
        port = int(raw)
        if port not in seen:
            seen.add(port)
            ports.append(port)
    return ports


def _detect_listen_ports(session: SSHSession, pid: int, owner: str | None = None) -> list[int]:
    """Return every listen port owned by ``pid`` (telnet and http, no +1 guess)."""
    ss_cmd = "sudo ss -tlnp" if owner else "ss -tlnp"
    cmd = f"{ss_cmd} 2>/dev/null | grep 'pid={pid},'"
    stdout, _, rc = _exec_ssh(session, cmd, timeout=10, sudo_user=None if owner else None)
    if rc != 0 or not stdout.strip():
        return []
    return _parse_ss_listen_ports(stdout)


def _classify_arthas_ports(ports: list[int]) -> tuple[int, int | None] | None:
    """Pick telnet + the non-telnet listen port. Never invent telnet+1."""
    if not ports:
        return None
    unique = list(dict.fromkeys(ports))
    in_range = [port for port in unique if 3658 <= port <= 3665]
    telnet = in_range[0] if in_range else next((port for port in unique if port < 8000), unique[0])
    others = [port for port in unique if port != telnet]
    http_port = others[0] if others else None
    return telnet, http_port


def _detect_existing_agent(
    session: SSHSession, pid: int, owner: str | None = None
) -> tuple[int, int | None] | None:
    """Detect an already-listening Agent as (telnet_port, http_port)."""
    return _classify_arthas_ports(_detect_listen_ports(session, pid, owner))


def _detect_arthas_port(session: SSHSession, pid: int, owner: str | None = None) -> int | None:
    """Detect Arthas telnet port via ss -tlnp (with sudo for cross-user)."""
    for port in _detect_listen_ports(session, pid, owner):
        if port < 8000:
            logger.debug("[PORT-DETECT] PID %d -> port %d", pid, port)
            return port
    return None


def _find_free_port(session: SSHSession, exclude: int | None = None) -> int:
    """Find first available port in range 3658-3665."""
    for port in range(3658, 3666):
        out, _, _ = _exec_ssh(
            session, f"ss -tln 2>/dev/null | grep -q ':{port} '; echo $?", timeout=5
        )
        if out.strip() == "1" and port != exclude:
            logger.debug("[PORT-FIND] Port %d is free", port)
            return port
    logger.error("[PORT-FIND] No free port in range 3658-3665")
    raise RuntimeError("No free port in range 3658-3665")


def _attach_agent(
    session: SSHSession,
    pid: int,
    arthas_path: str,
    owner: str | None = None,
    start_time: str | None = None,
) -> int:
    """Attach Arthas agent to target PID.

    THREAD SAFETY:
        Caller must hold the per-PID attach lock (via _get_attach_lock(pid)).

    Flow:
        1. Find free port (3658-3665)
        2. Direct SSH exec: arthas-boot.jar --attach-only --telnet-port <port> <pid>
        3. Poll ss -tlnp until agent listening
    """
    logger.info("[ATTACH] START pid=%d, owner=%s", pid, owner)
    t0 = time.time()

    base_dir = arthas_path.rsplit("/", 1)[0] if "/" in arthas_path else "/tmp/arthas-all"  # noqa: S108

    port = _find_free_port(session)
    http_port = _find_free_port(session, exclude=port)
    logger.info("[ATTACH] Found free port %d (HTTP %d) for PID %d", port, http_port, pid)

    attach_cmd = (
        (
            f"sudo -u {owner} env HOME=/tmp java -jar {base_dir}/arthas-boot.jar "
            f"--attach-only --telnet-port {port} --http-port {http_port} "
            f"--target-ip 127.0.0.1 "
            f"--arthas-home {base_dir} {pid}"
        )
        if owner
        else (
            f"java -jar {base_dir}/arthas-boot.jar "
            f"--attach-only --telnet-port {port} --http-port {http_port} "
            f"--target-ip 127.0.0.1 "
            f"--arthas-home {base_dir} {pid}"
        )
    )

    logger.info("[ATTACH] Executing attach for PID %d on port %d", pid, port)
    stdout, stderr, rc = _exec_ssh(session, attach_cmd, timeout=30, sudo_user=None)

    if rc != 0:
        logger.error("[ATTACH] FAILED pid=%d, rc=%d, stderr=%.300s", pid, rc, stderr)
        raise RuntimeError(f"Arthas attach failed for PID {pid}: {stderr[:300]}")

    logger.info("[ATTACH] Command completed pid=%d, rc=%d", pid, rc)

    for attempt in range(15):
        time.sleep(1)
        detected = _detect_arthas_port(session, pid, owner)
        if detected is not None:
            elapsed = time.time() - t0
            logger.info(
                "[ATTACH] SUCCESS pid=%d, port=%d, elapsed=%.1fs",
                pid,
                detected,
                elapsed,
            )
            with _PID_STATE_LOCK:
                _PID_STATE[_state_key(session, pid, start_time)] = {
                    "port": detected,
                    "http_port": http_port,
                    "owner": owner,
                }
            return detected
        logger.debug("[ATTACH] Poll %d/15: port not ready for PID %d", attempt + 1, pid)

    elapsed = time.time() - t0
    logger.error("[ATTACH] TIMEOUT pid=%d, port=%d, elapsed=%.1fs", pid, port, elapsed)
    raise RuntimeError(f"Arthas agent for PID {pid} started on port {port} but detection timed out")


def _ensure_agent(
    session: SSHSession,
    pid: int,
    arthas_path: str,
    owner: str | None = None,
    start_time: str | None = None,
) -> int:
    """Ensure Arthas agent is running for PID. Returns port number.

    Three-level lookup (fastest to slowest):
        1. _PID_STATE cache hit: direct port return (~1ms)
        2. Cross-session agent reuse: detect via ss -tlnp (~100ms)
        3. Full attach: arthas-boot.jar (~2-3s, serialized per PID)
    """
    state_key = _state_key(session, pid, start_time)
    identity = state_key if isinstance(state_key, TargetIdentity) else None

    def register_instance(port: int, origin: ArthasOrigin) -> None:
        if identity is not None:
            _LIFECYCLE_REGISTRY.register(
                identity,
                ArthasInstance(
                    port=port,
                    pid=pid,
                    origin=origin,
                    last_used_at=datetime.now(timezone.utc),
                ),
            )

    # Level 1: Cache hit
    with _PID_STATE_LOCK:
        if state_key in _PID_STATE:
            cached_port = int(str(_PID_STATE[state_key]["port"]))
            logger.debug("[ENSURE] Cache hit PID %d -> port %d", pid, cached_port)
            # A cache hit is not evidence that this proxy started Arthas.  Do
            # not overwrite an ownership record established by the attach or
            # existing-agent paths: doing so silently turns STARTED_BY_PROXY
            # into UNKNOWN on the second request and makes authorized cleanup
            # leak the remote agent.  Legacy/cache-only state remains
            # deliberately non-stoppable (UNKNOWN).
            if identity is not None:
                known = _LIFECYCLE_REGISTRY.get(identity)
                if known is None:
                    register_instance(cached_port, ArthasOrigin.UNKNOWN)
                else:
                    _LIFECYCLE_REGISTRY.touch(identity, datetime.now(timezone.utc))
            return cached_port

    # Level 2: Detect existing agent (cross-session reuse)
    existing_port = _detect_arthas_port(session, pid, owner)
    if existing_port is not None:
        logger.info("[ENSURE] Reused existing agent PID %d -> port %d", pid, existing_port)
        with _PID_STATE_LOCK:
            _PID_STATE[state_key] = {
                "port": existing_port,
                "http_port": existing_port + 1,
                "owner": owner,
            }
        register_instance(existing_port, ArthasOrigin.EXISTING)
        return existing_port

    # Level 3: Full attach (serialized per PID)
    logger.info("[ENSURE] Need attach for PID %d, acquiring lock...", pid)
    attach_lock = _get_attach_lock(state_key)
    if not attach_lock.acquire(timeout=60):
        logger.error("[ENSURE] Attach lock timeout for PID %d", pid)
        raise RuntimeError(f"Attach lock timeout for PID {pid}")

    try:
        # Double-check after lock acquisition
        with _PID_STATE_LOCK:
            if state_key in _PID_STATE:
                port = int(str(_PID_STATE[state_key]["port"]))
                logger.info("[ENSURE] Another thread attached PID %d -> port %d", pid, port)
                if identity is not None:
                    known = _LIFECYCLE_REGISTRY.get(identity)
                    if known is None:
                        register_instance(port, ArthasOrigin.UNKNOWN)
                    else:
                        _LIFECYCLE_REGISTRY.touch(identity, datetime.now(timezone.utc))
                return port

        existing_port = _detect_arthas_port(session, pid, owner)
        if existing_port is not None:
            logger.info("[ENSURE] Agent appeared after lock PID %d -> port %d", pid, existing_port)
            with _PID_STATE_LOCK:
                _PID_STATE[state_key] = {
                    "port": existing_port,
                    "http_port": existing_port + 1,
                    "owner": owner,
                }
            register_instance(existing_port, ArthasOrigin.EXISTING)
            return existing_port

        logger.info("[ENSURE] Attaching new agent for PID %d...", pid)
        port = _attach_agent(session, pid, arthas_path, owner, start_time)
        register_instance(port, ArthasOrigin.STARTED_BY_PROXY)
        return port
    finally:
        attach_lock.release()
        logger.debug("[ENSURE] Released attach lock for PID %d", pid)


def _check_process_identity(
    session: SSHSession,
    pid: int,
    start_time: str | None,
    boot_id: str | None = None,
) -> bool:
    """Re-check live inventory identity. Legacy clients without start_time skip.

    Returns whether the recorded/live identity is complete (start_time + boot_id).
    """
    if start_time is None:
        return False
    records = collect_inventory_over_ssh(lambda command: _exec_ssh(session, command))
    candidate = validate_process_identity(records, pid, start_time, boot_id=boot_id)
    return identity_complete(candidate)


def _cache_agent_ports(
    session: SSHSession,
    pid: int,
    telnet_port: int,
    http_port: int | None,
    owner: str | None,
    origin: ArthasOrigin,
    start_time: str | None = None,
    *,
    ready: bool = False,
) -> None:
    """Record half-ready or verified ports. http_port is detected, never telnet+1."""
    state_key = _state_key(session, pid, start_time)
    with _PID_STATE_LOCK:
        _PID_STATE[state_key] = {
            "port": telnet_port,
            "http_port": http_port,
            "owner": owner,
            "ready": ready,
        }
    if isinstance(state_key, TargetIdentity):
        known = _LIFECYCLE_REGISTRY.get(state_key)
        if known is None:
            _LIFECYCLE_REGISTRY.register(
                state_key,
                ArthasInstance(
                    port=telnet_port,
                    pid=pid,
                    origin=origin,
                    last_used_at=datetime.now(timezone.utc),
                ),
            )
        else:
            _LIFECYCLE_REGISTRY.touch(state_key, datetime.now(timezone.utc))


def _clear_half_ready(session: SSHSession, pid: int, start_time: str | None = None) -> None:
    """Forget PID_STATE and lifecycle so a failed verify is not left READY."""
    state_key = _state_key(session, pid, start_time)
    with _PID_STATE_LOCK:
        _PID_STATE.pop(state_key, None)
        if start_time is None:
            _PID_STATE.pop(pid, None)
    if isinstance(state_key, TargetIdentity):
        _LIFECYCLE_REGISTRY.forget(state_key)


def _parse_arthas_version(output: str) -> str:
    """Extract a version token from Arthas ``version`` output."""
    text = _filter_output(output).strip()
    if not text:
        return ""
    match = re.search(r"\b(\d+\.\d+(?:\.\d+)?)\b", text)
    if match:
        return match.group(1)
    return text.splitlines()[0].strip()


def _probe_arthas_version(
    session: SSHSession,
    pid: int,
    telnet_port: int,
    http_port: int | None,
    owner: str | None = None,
    start_time: str | None = None,
    timeout: int = 15,
) -> str:
    """Run Arthas ``version`` over HTTP or CLI. Listening ports alone are not READY."""
    errors: list[str] = []
    if http_port is not None:
        try:
            result = ArthasHttpClient(
                lambda command, timeout=60: _exec_ssh(
                    session, command, timeout=timeout, sudo_user=owner
                ),
                int(http_port),
                tls=os.environ.get("ARTHAS_HTTP_TLS", "").lower() in {"1", "true", "yes"},
            ).execute("version", timeout)
            version = _parse_arthas_version(result.output)
            if version:
                return version
            errors.append("empty HTTP version output")
        except (ArthasHttpError, ConnectionError, TimeoutError, OSError) as exc:
            errors.append(str(exc))

    try:
        cache_key = _jar_cache_key(session, pid, owner, start_time)
        jar_path = _jar_cache.get(cache_key)
        if jar_path is None:
            arthas_path = _find_arthas_path(session, owner)
            jar_path = _find_arthas_client_jar(session, arthas_path, owner)
            _jar_cache[cache_key] = jar_path
        java_home = _get_java_home(session, owner)
        env_prefix = f"JAVA_HOME={java_home} " if java_home else ""
        exec_cmd = (
            f"{env_prefix}"
            f"java -jar '{jar_path}' "
            f"127.0.0.1 {telnet_port} -c 'version' --execution-timeout {timeout * 1000} "
            f"2>&1"
        )
        stdout, stderr, rc = _exec_ssh(session, exec_cmd, timeout=timeout + 10, sudo_user=owner)
        version = _parse_arthas_version(stdout or stderr)
        if version:
            return version
        errors.append(stderr or stdout or f"cli rc={rc}")
    except Exception as exc:
        errors.append(str(exc))

    message = (
        f"Arthas version probe failed: {'; '.join(errors)}"
        if errors
        else "Arthas version probe failed"
    )
    raise DomainError(
        ErrorCode.ARTHAS_UNREACHABLE,
        message,
        phase="verify",
        retryable=True,
        suggestion="Check that the Arthas agent is listening and retry prepare_arthas.",
    )


def prepare_agent(
    session: SSHSession,
    pid: int,
    *,
    start_time: str | None = None,
    boot_id: str | None = None,
    owner: str | None = None,
) -> PreparedArthas:
    """VALIDATE_JVM → DETECT_EXISTING → PROBE → FIND_OR_INSTALL → ATTACH → VERIFY → READY."""
    # VALIDATE_JVM
    _check_process_identity(session, pid, start_time, boot_id)

    # DETECT_EXISTING (real listen ports, not telnet+1)
    existing = _detect_existing_agent(session, pid, owner)
    origin: ArthasOrigin
    telnet_port: int
    http_port: int | None

    if existing is not None:
        telnet_port, http_port = existing
        origin = ArthasOrigin.EXISTING
    else:
        # FIND_OR_INSTALL (find as.sh; keep install_arthas as the installer tool)
        try:
            arthas_path = _find_arthas_path(session, owner)
        except RuntimeError as exc:
            raise DomainError(
                ErrorCode.ARTHAS_NOT_INSTALLED,
                str(exc),
                phase="find_or_install",
                suggestion="Use install_arthas, then retry prepare_arthas.",
            ) from exc
        # ATTACH_IF_NEEDED
        try:
            telnet_port = _attach_agent(session, pid, arthas_path, owner, start_time)
        except DomainError:
            raise
        except Exception as exc:
            raise DomainError(
                ErrorCode.ARTHAS_ATTACH_FAILED,
                str(exc),
                phase="attach",
                suggestion="Check JVM attach permissions and retry prepare_arthas.",
            ) from exc
        origin = ArthasOrigin.STARTED_BY_PROXY
        rediscovered = _detect_existing_agent(session, pid, owner)
        if rediscovered is not None:
            telnet_port, http_port = rediscovered
        else:
            state_key = _state_key(session, pid, start_time)
            with _PID_STATE_LOCK:
                cached_http = _PID_STATE.get(state_key, {}).get("http_port")
            http_port = int(str(cached_http)) if cached_http is not None else None

    # Half-ready cache: ports known, not READY until version succeeds
    _cache_agent_ports(session, pid, telnet_port, http_port, owner, origin, start_time, ready=False)

    # VERIFY(version)
    try:
        version = _probe_arthas_version(session, pid, telnet_port, http_port, owner, start_time)
        if not str(version).strip():
            raise DomainError(
                ErrorCode.ARTHAS_UNREACHABLE,
                "Arthas version probe returned an empty result",
                phase="verify",
                retryable=True,
            )
    except DomainError:
        _clear_half_ready(session, pid, start_time)
        raise
    except Exception as exc:
        _clear_half_ready(session, pid, start_time)
        raise DomainError(
            ErrorCode.ARTHAS_UNREACHABLE,
            str(exc),
            phase="verify",
            retryable=True,
        ) from exc

    # READY
    _cache_agent_ports(session, pid, telnet_port, http_port, owner, origin, start_time, ready=True)
    return PreparedArthas(
        origin=origin,
        telnet_port=telnet_port,
        http_port=http_port,
        arthas_version=str(version).strip(),
    )


_SAFE_FALLBACK_TOKENS = frozenset({"jvm", "version", "thread", "dashboard", "memory"})


def _cli_fallback_allowed(command: str, cancel: threading.Event | None) -> bool:
    """CLI is allowed only for safe short commands that HTTP never accepted."""
    if cancel is not None and cancel.is_set():
        return False
    if is_observation_command(command):
        return False
    token = command.strip().split(None, 1)[0].lower() if command.strip() else ""
    return token in _SAFE_FALLBACK_TOKENS


def _exec_command(
    session: SSHSession,
    pid: int,
    command: str,
    arthas_path: str,
    timeout: int = 60,
    owner: str | None = None,
    start_time: str | None = None,
    boot_id: str | None = None,
    backend_state: dict[str, object] | None = None,
    cancel: threading.Event | None = None,
) -> str:
    """Execute Arthas command via client on detected port."""
    t0 = time.time()

    # A discovered start time is a strict runtime identity contract.  Validate
    # immediately before attach/command execution to prevent PID reuse.
    # Legacy clients with no start_time skip the hard check.
    identity_ok = _check_process_identity(session, pid, start_time, boot_id)
    if backend_state is not None:
        backend_state["identity_complete"] = identity_ok

    port = _ensure_agent(session, pid, arthas_path, owner, start_time)

    state_key = _state_key(session, pid, start_time)
    with _PID_STATE_LOCK:
        http_port = _PID_STATE.get(state_key, {}).get("http_port")
    if http_port is not None:
        try:
            result = ArthasHttpClient(
                lambda command, timeout=60: _exec_ssh(
                    session, command, timeout=timeout, sudo_user=owner
                ),
                int(str(http_port)),
                tls=os.environ.get("ARTHAS_HTTP_TLS", "").lower() in {"1", "true", "yes"},
            ).execute(command, timeout)
            logger.info("[HTTP] command succeeded for PID %d", pid)
            if backend_state is not None:
                backend_state.update(backend="arthas_http", degraded=False)
            return _filter_output(result.output)
        except ArthasHttpError as exc:
            if exc.code == "command_failed":
                raise DomainError(
                    ErrorCode.ARTHAS_COMMAND_FAILED,
                    str(exc),
                    phase="execute",
                ) from exc
            if exc.code == "unreachable" and _cli_fallback_allowed(command, cancel):
                if backend_state is not None:
                    backend_state.update(backend="arthas_cli", degraded=True)
                logger.info("[HTTP-FALLBACK] HTTP unavailable; executing CLI once: %s", exc)
            else:
                raise

    cache_key = _jar_cache_key(session, pid, owner, start_time)
    jar_path = _jar_cache.get(cache_key)
    if jar_path is None:
        jar_path = _find_arthas_client_jar(session, arthas_path, owner)
        _jar_cache[cache_key] = jar_path
        logger.info("[CLIENT-JAR] Cached for target %s: %s", cache_key, jar_path)

    java_home = _get_java_home(session, owner)
    env_prefix = f"JAVA_HOME={java_home} " if java_home else ""

    exec_cmd = (
        f"{env_prefix}"
        f"java -jar '{jar_path}' "
        f"127.0.0.1 {port} -c '{command}' --execution-timeout {timeout * 1000} "
        f"2>&1"
    )

    logger.info("[CMD-EXEC] PID=%d, port=%d, cmd='%.60s'", pid, port, command)
    stdout, stderr, rc = _exec_ssh(session, exec_cmd, timeout=timeout + 10, sudo_user=owner)

    elapsed = time.time() - t0
    result_text = _filter_output(stdout)

    if rc != 0 and not result_text.strip():
        result_text = stderr.strip() or stdout.strip()

    out_len = len(result_text.strip())
    logger.info(
        "[CMD-EXEC] DONE PID=%d, rc=%d, output=%d chars, elapsed=%.1fs",
        pid,
        rc,
        out_len,
        elapsed,
    )

    if out_len < 50:
        logger.warning(
            "[CMD-EXEC] Short output (%d chars) for PID %d, cmd='%.40s'",
            out_len,
            pid,
            command,
        )

    return result_text


def _filter_output(output: str) -> str:
    """Clean up Arthas output by removing noise lines."""
    lines = output.split("\n")
    filtered: list[str] = []

    skip_patterns = [
        re.compile(r"^\s*[,.`'|/\\_\s*]+$"),
        re.compile(r"^Arthas script version:"),
        re.compile(r"^\[INFO\] JAVA_HOME:"),
        re.compile(r"^\[INFO\] Process \d+ already using port"),
        re.compile(r"^Arthas home:"),
        re.compile(r"^(real|user|sys)\s+\d+m"),
        re.compile(r"^Calculating attach execution time"),
        re.compile(r"^Attaching to \d+"),
        re.compile(r"^Attach success\."),
        re.compile(r"^\s*wiki\s+https?://"),
        re.compile(r"^\s*version\s+"),
        re.compile(r"^\s*pid\s+\d+"),
        re.compile(r"^\s*time\s+\d"),
    ]

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        skip = any(p.search(stripped) for p in skip_patterns)
        if skip:
            continue
        cleaned = re.sub(r"\x1b\[[0-9;]*m", "", line)
        if cleaned.strip():
            filtered.append(cleaned)

    return "\n".join(filtered)


def _parse_pid_line(line: str) -> tuple[int, str] | None:
    parts = line.strip().split(None, 1)
    if len(parts) >= 2 and parts[0].isdigit():
        return int(parts[0]), parts[1]
    return None


class ArthasClient:
    """High-level Arthas diagnostic client with thread-safe multi-PID support."""

    def __init__(
        self,
        session: SSHSession,
        start_time: str | None = None,
        boot_id: str | None = None,
    ) -> None:
        self.session = session
        session_start_time = getattr(session, "start_time", None)
        self.start_time = (
            start_time
            if start_time is not None
            else (session_start_time if isinstance(session_start_time, str) else None)
        )
        session_boot_id = getattr(session, "boot_id", None)
        self.boot_id = (
            boot_id
            if boot_id is not None
            else (session_boot_id if isinstance(session_boot_id, str) else None)
        )
        self._arthas_path: str | None = None
        self.last_backend: str = "arthas_cli"
        self.last_backend_degraded: bool = False
        self.last_identity_complete: bool = False
        self._owner_cache: dict[int, str | None] = {}
        self._client_lock = threading.Lock()

    def _get_arthas_path(self, owner: str | None = None) -> str:
        if self._arthas_path is None:
            with self._client_lock:
                if self._arthas_path is None:
                    self._arthas_path = _find_arthas_path(self.session, owner)
        return self._arthas_path

    def _resolve_owner(self, pid: int | None = None) -> str | None:
        if pid is None:
            return None
        if pid in self._owner_cache:
            return self._owner_cache[pid]
        owner = _get_sudo_user(self.session, pid)
        self._owner_cache[pid] = owner
        return owner

    def _validate_identity(self, pid: int) -> None:
        """Re-check a resolved JVM immediately before executing diagnostics."""
        self.last_identity_complete = _check_process_identity(
            self.session, pid, self.start_time, self.boot_id
        )

    def _execute_with_backend(
        self,
        pid: int,
        command: str,
        timeout: int = 60,
        cancel: threading.Event | None = None,
    ) -> str:
        """Run a short command and record last_backend / last_backend_degraded."""
        owner = self._resolve_owner(pid)
        state: dict[str, object] = {"backend": "arthas_cli", "degraded": False}
        result = _exec_command(
            self.session,
            pid,
            command,
            arthas_path=self._get_arthas_path(owner),
            timeout=timeout,
            owner=owner,
            start_time=self.start_time,
            boot_id=self.boot_id,
            backend_state=state,
            cancel=cancel,
        )
        self.last_backend = str(state["backend"])
        self.last_backend_degraded = bool(state["degraded"])
        self.last_identity_complete = bool(state.get("identity_complete", False))
        return result

    def prepare(self, pid: int) -> PreparedArthas:
        """Detect or attach Arthas and verify with ``version`` before READY."""
        self.last_identity_complete = _check_process_identity(
            self.session, pid, self.start_time, self.boot_id
        )
        owner = self._resolve_owner(pid)
        return prepare_agent(
            self.session,
            pid,
            start_time=self.start_time,
            boot_id=self.boot_id,
            owner=owner,
        )

    def list_java_processes(self) -> str:
        stdout, _, rc = _exec_ssh(
            self.session,
            "jps -l -m 2>/dev/null || ps -ef | grep java | grep -v grep",
            timeout=15,
        )
        if rc != 0:
            raise RuntimeError("Failed to list Java processes")

        lines = stdout.strip().split("\n")
        results: list[str] = []
        for line in lines:
            parsed = _parse_pid_line(line)
            if parsed:
                pid, name = parsed
                if "jps" not in name.lower() and "arthas" not in name.lower():
                    port = _detect_arthas_port(self.session, pid)
                    marker = f" [arthas:{port}]" if port else ""
                    results.append(f"PID {pid}: {name}{marker}")

        return "\n".join(results) if results else "No Java processes found."

    def thread_dump(self, pid: int, top_n: int = 20) -> str:
        owner = self._resolve_owner(pid)
        logger.info("[API] thread_dump pid=%d, top_n=%d, owner=%s", pid, top_n, owner)
        return self._execute_with_backend(pid, f"thread -n {top_n}", timeout=30)

    def heap_info(self, pid: int) -> str:
        owner = self._resolve_owner(pid)
        logger.info("[API] heap_info pid=%d, owner=%s", pid, owner)
        return self._execute_with_backend(pid, "dashboard -n 1", timeout=30)

    def watch_method(
        self,
        pid: int,
        class_pattern: str,
        method_pattern: str,
        watch_params: bool = True,
        watch_return: bool = True,
        condition: str | None = None,
        times: int = 5,
    ) -> str:
        """Closed CLI side path. Use MCP watch_method (HTTP streaming)."""
        raise RuntimeError("use MCP watch_method; CLI watch is not a supported path")

    def trace_method(
        self,
        pid: int,
        class_pattern: str,
        method_pattern: str,
        condition: str | None = None,
        times: int = 5,
        timeout: int = 60,
    ) -> str:
        """Closed CLI side path. Use MCP trace_method (HTTP streaming)."""
        raise RuntimeError("use MCP trace_method; CLI trace is not a supported path")

    def exec_command(
        self,
        pid: int,
        command: str,
        timeout: int = 60,
        cancel: threading.Event | None = None,
    ) -> str:
        owner = self._resolve_owner(pid)
        logger.info("[API] exec_command pid=%d, cmd='%.40s', owner=%s", pid, command, owner)
        return self._execute_with_backend(pid, command, timeout=timeout, cancel=cancel)

    def execute_command(
        self,
        pid: int,
        command: str,
        timeout: int = 60,
        cancel: threading.Event | None = None,
    ) -> str:
        """Execute a rendered Arthas command for typed diagnostic tools."""
        return self.exec_command(pid, command, timeout, cancel=cancel)

    def execute_streaming_command(
        self,
        pid: int,
        command: str,
        emit: Callable[[str], None],
        cancel: threading.Event,
        timeout: int = 60,
    ) -> str:
        """Execute a long command through Arthas HTTP long-polling."""
        owner = self._resolve_owner(pid)
        self.last_identity_complete = _check_process_identity(
            self.session, pid, self.start_time, self.boot_id
        )
        port = _ensure_agent(
            self.session, pid, self._get_arthas_path(owner), owner, self.start_time
        )
        state_key = _state_key(self.session, pid, self.start_time)
        with _PID_STATE_LOCK:
            http_port = _PID_STATE.get(state_key, {}).get("http_port", port + 1)
        client = ArthasHttpStreamingClient(
            lambda command, timeout=60: _exec_ssh(
                self.session, command, timeout=timeout, sudo_user=owner
            ),
            int(str(http_port)),
            tls=os.environ.get("ARTHAS_HTTP_TLS", "").lower() in {"1", "true", "yes"},
        )
        self.last_backend = ArthasHttpStreamingClient.backend_name
        self.last_backend_degraded = False
        return client.execute_stream(command, emit, cancel, timeout=timeout)

    def cleanup_expired(
        self,
        pid: int,
        now: datetime,
        ttl_seconds: int,
        *,
        authorized: bool = False,
    ) -> list[ArthasInstance]:
        """Detach an expired agent through the normal remote stop path.

        The registry remains authorization- and ownership-aware; this method
        only supplies the real ArthasClient ``detach`` callback for this exact
        target/process identity.  EXISTING and UNKNOWN instances are filtered
        out by ``should_auto_stop`` and therefore never invoke ``detach``.
        """
        identity = _state_key(self.session, pid, self.start_time)
        if not isinstance(identity, TargetIdentity):
            return []

        def detach_instance(instance: ArthasInstance) -> None:
            self.detach(instance.pid)

        return _LIFECYCLE_REGISTRY.cleanup_expired(
            now,
            ttl_seconds,
            detach_instance,
            authorized=authorized,
            target_identity=identity,
        )

    def detach(self, pid: int) -> str:
        # Detach is destructive: never stop an agent after the resolved PID has
        # been replaced by another JVM.  Keep this check before cache/port lookup
        # so a stale agent handle cannot cause an operation on the new process.
        self._validate_identity(pid)
        port: int | None = None
        state_key = _state_key(self.session, pid, self.start_time)
        with _PID_STATE_LOCK:
            cache_key: int | TargetIdentity | None = None
            if state_key in _PID_STATE:
                cache_key = state_key
                port = int(str(_PID_STATE[state_key]["port"]))
            elif pid in _PID_STATE and self.start_time is None:
                # PID-only legacy state is safe only for legacy PID-only clients;
                # identity-bound clients must never inherit a stale PID entry.
                cache_key = pid
                port = int(str(_PID_STATE[pid]["port"]))

        if port is None:
            port = _detect_arthas_port(self.session, pid)
        if port is None:
            logger.warning("[DETACH] No agent found for PID %d", pid)
            return f"No Arthas agent found for PID {pid}"

        owner = self._resolve_owner(pid)
        try:
            java_home = _get_java_home(self.session, owner)
            env_prefix = f"JAVA_HOME={java_home} " if java_home else ""
            arthas_path = self._get_arthas_path(owner)
            jar_path = _find_arthas_client_jar(self.session, arthas_path, owner)
            stop_cmd = (
                f"{env_prefix}"
                f"java -jar '{jar_path}' "
                f"127.0.0.1 {port} -c 'stop' --execution-timeout 5000 "
                f"2>&1"
            )
            stdout, _, _ = _exec_ssh(self.session, stop_cmd, timeout=10, sudo_user=owner)
            with _PID_STATE_LOCK:
                _PID_STATE.pop(cache_key if cache_key is not None else state_key, None)
            logger.info("[DETACH] Graceful detach PID %d port %d", pid, port)
            return _filter_output(stdout) or f"Arthas detached from PID {pid} (port {port})"
        except Exception as e:
            logger.warning("[DETACH] Graceful detach failed for PID %d: %s", pid, e)
            kill_cmd = (
                f"ps aux | grep 'arthas-core' | grep 'pid={pid}' | grep -v grep | "
                f"awk '{{print $2}}' | xargs -r kill -9 2>/dev/null || true"
            )
            _exec_ssh(self.session, kill_cmd, timeout=5, sudo_user=owner)
            with _PID_STATE_LOCK:
                _PID_STATE.pop(cache_key if cache_key is not None else state_key, None)
            return f"Arthas force-detached from PID {pid}"

    def install_arthas(self, install_type: str = "auto") -> str:
        try:
            path = _find_arthas_path(self.session)
            return f"Arthas already installed at: {path}"
        except RuntimeError:
            pass

        install_type = install_type.lower()
        if install_type == "online":
            return self._install_online()
        elif install_type == "offline":
            return self._install_offline()
        elif install_type == "auto":
            try:
                return self._install_online()
            except RuntimeError:
                logger.info("Online install failed, trying offline...")
                return self._install_offline()
        else:
            raise ValueError(f"Unknown install_type: {install_type}")

    def _install_online(self) -> str:
        logger.info("[INSTALL] Online mode: downloading from aliyun")
        cmd = (
            "rm -rf /tmp/arthas-install /tmp/arthas-all && "
            "mkdir -p /tmp/arthas-install /tmp/arthas-all && "
            "cd /tmp/arthas-install && "
            "(curl -L -o arthas-bin.zip -k "
            "'https://arthas.aliyun.com/download/latest_version?mirror=aliyun' "
            "2>/dev/null || "
            "wget --no-check-certificate -O arthas-bin.zip "
            "'https://arthas.aliyun.com/download/latest_version?mirror=aliyun' "
            "2>/dev/null) && "
            "test -f arthas-bin.zip && unzip -o -q arthas-bin.zip && "
            "(cp -rf /tmp/arthas-install/arthas/* /tmp/arthas-all/ 2>/dev/null "
            "|| cp -rf /tmp/arthas-install/* /tmp/arthas-all/) && "
            "chmod +x /tmp/arthas-all/as.sh && echo INSTALLED"
        )
        stdout, stderr, rc = _exec_ssh(self.session, cmd, timeout=120)
        if rc != 0 or "INSTALLED" not in stdout:
            raise RuntimeError(f"Online install failed: {stderr[:200]}")
        return "Arthas installed successfully to ~/.arthas/"

    def _install_offline(self) -> str:
        logger.info("[INSTALL] Offline mode")
        copied = _copy_arthas_to_target(self.session)
        if not copied:
            raise RuntimeError(
                "Offline install failed: no arthas-bin.zip found on MCP server. "
                "Place arthas-bin.zip at /app/arthas-bin.zip or use install_type='online'"
            )

        cmd = (
            "rm -rf /tmp/arthas-install && mkdir -p /tmp/arthas-install && "
            "cd /tmp/arthas-install && unzip -o -q /tmp/arthas-bin.zip && "
            "mkdir -p ~/.arthas && "
            "(cp -rf /tmp/arthas-install/arthas/* ~/.arthas/ 2>/dev/null \
            || cp -rf /tmp/arthas-install/* ~/.arthas/) && "
            "chmod +x ~/.arthas/as.sh && echo INSTALLED"
        )
        stdout, stderr, rc = _exec_ssh(self.session, cmd, timeout=60)
        if rc != 0 or "INSTALLED" not in stdout:
            raise RuntimeError(f"Offline install failed: {stderr[:200]}")
        return "Arthas installed successfully to ~/.arthas/ (offline mode)"
