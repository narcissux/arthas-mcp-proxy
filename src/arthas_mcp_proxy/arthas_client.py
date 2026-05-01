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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arthas_mcp_proxy.ssh_pool import SSHSession

logger = logging.getLogger(__name__)

# Per-PID state: pid -> {"port": int, "owner": Optional[str]}
_PID_STATE: dict[int, dict[str, int | str | None]] = {}
_PID_STATE_LOCK = threading.Lock()

# Per-PID attach locks: ensures only one thread attaches to a given PID
_ATTACH_LOCKS: dict[int, threading.Lock] = {}
_ATTACH_LOCKS_MASTER = threading.Lock()

# Module-level jar cache to avoid re-finding arthas-client.jar
_jar_cache: dict[str, str] = {}


def _get_attach_lock(pid: int) -> threading.Lock:
    """Get or create a per-PID attach lock."""
    with _ATTACH_LOCKS_MASTER:
        if pid not in _ATTACH_LOCKS:
            _ATTACH_LOCKS[pid] = threading.Lock()
        return _ATTACH_LOCKS[pid]


def _exec_ssh(
    session: SSHSession,
    command: str,
    timeout: int = 60,
    sudo_user: str | None = None,
) -> tuple[str, str, int]:
    """Execute command over SSH, optionally with sudo -u <user>."""
    if sudo_user:
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


def _detect_arthas_port(session: SSHSession, pid: int, owner: str | None = None) -> int | None:
    """Detect Arthas telnet port via ss -tlnp (with sudo for cross-user)."""
    ss_cmd = "sudo ss -tlnp" if owner else "ss -tlnp"
    cmd = f"{ss_cmd} 2>/dev/null | grep 'pid={pid},'"
    stdout, _, rc = _exec_ssh(session, cmd, timeout=10, sudo_user=None if owner else None)
    if rc != 0 or not stdout.strip():
        return None

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

    for p in ports_found:
        p_int = int(p)
        if p_int < 8000:
            logger.debug("[PORT-DETECT] PID %d -> port %d", pid, p_int)
            return p_int
    return None


def _find_free_port(session: SSHSession) -> int:
    """Find first available port in range 3658-3665."""
    for port in range(3658, 3666):
        out, _, _ = _exec_ssh(
            session, f"ss -tln 2>/dev/null | grep -q ':{port} '; echo $?", timeout=5
        )
        if out.strip() == "1":
            logger.debug("[PORT-FIND] Port %d is free", port)
            return port
    logger.error("[PORT-FIND] No free port in range 3658-3665")
    raise RuntimeError("No free port in range 3658-3665")


def _attach_agent(session: SSHSession, pid: int, arthas_path: str, owner: str | None = None) -> int:
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
    logger.info("[ATTACH] Found free port %d for PID %d", port, pid)

    attach_cmd = (
        (
            f"sudo -u {owner} env HOME=/tmp java -jar {base_dir}/arthas-boot.jar "
            f"--attach-only --telnet-port {port} --http-port -1 --arthas-home {base_dir} {pid}"
        )
        if owner
        else (
            f"java -jar {base_dir}/arthas-boot.jar "
            f"--attach-only --telnet-port {port} --http-port -1 --arthas-home {base_dir} {pid}"
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
                _PID_STATE[pid] = {"port": detected, "owner": owner}
            return detected
        logger.debug("[ATTACH] Poll %d/15: port not ready for PID %d", attempt + 1, pid)

    elapsed = time.time() - t0
    logger.error("[ATTACH] TIMEOUT pid=%d, port=%d, elapsed=%.1fs", pid, port, elapsed)
    raise RuntimeError(f"Arthas agent for PID {pid} started on port {port} but detection timed out")


def _ensure_agent(session: SSHSession, pid: int, arthas_path: str, owner: str | None = None) -> int:
    """Ensure Arthas agent is running for PID. Returns port number.

    Three-level lookup (fastest to slowest):
        1. _PID_STATE cache hit: direct port return (~1ms)
        2. Cross-session agent reuse: detect via ss -tlnp (~100ms)
        3. Full attach: arthas-boot.jar (~2-3s, serialized per PID)
    """
    # Level 1: Cache hit
    with _PID_STATE_LOCK:
        if pid in _PID_STATE:
            cached_port = int(str(_PID_STATE[pid]["port"]))
            logger.debug("[ENSURE] Cache hit PID %d -> port %d", pid, cached_port)
            return cached_port

    # Level 2: Detect existing agent (cross-session reuse)
    existing_port = _detect_arthas_port(session, pid, owner)
    if existing_port is not None:
        logger.info("[ENSURE] Reused existing agent PID %d -> port %d", pid, existing_port)
        with _PID_STATE_LOCK:
            _PID_STATE[pid] = {"port": existing_port, "owner": owner}
        return existing_port

    # Level 3: Full attach (serialized per PID)
    logger.info("[ENSURE] Need attach for PID %d, acquiring lock...", pid)
    attach_lock = _get_attach_lock(pid)
    if not attach_lock.acquire(timeout=60):
        logger.error("[ENSURE] Attach lock timeout for PID %d", pid)
        raise RuntimeError(f"Attach lock timeout for PID {pid}")

    try:
        # Double-check after lock acquisition
        with _PID_STATE_LOCK:
            if pid in _PID_STATE:
                port = int(str(_PID_STATE[pid]["port"]))
                logger.info("[ENSURE] Another thread attached PID %d -> port %d", pid, port)
                return port

        existing_port = _detect_arthas_port(session, pid, owner)
        if existing_port is not None:
            logger.info("[ENSURE] Agent appeared after lock PID %d -> port %d", pid, existing_port)
            with _PID_STATE_LOCK:
                _PID_STATE[pid] = {"port": existing_port, "owner": owner}
            return existing_port

        logger.info("[ENSURE] Attaching new agent for PID %d...", pid)
        return _attach_agent(session, pid, arthas_path, owner)
    finally:
        attach_lock.release()
        logger.debug("[ENSURE] Released attach lock for PID %d", pid)


def _exec_command(
    session: SSHSession,
    pid: int,
    command: str,
    arthas_path: str,
    timeout: int = 60,
    owner: str | None = None,
) -> str:
    """Execute Arthas command via client on detected port."""
    t0 = time.time()

    port = _ensure_agent(session, pid, arthas_path, owner)

    cache_key = f"{id(session)}_{owner}"
    jar_path = _jar_cache.get(cache_key)
    if jar_path is None:
        jar_path = _find_arthas_client_jar(session, arthas_path, owner)
        _jar_cache[cache_key] = jar_path
        logger.info("[CLIENT-JAR] Cached for session %s: %s", cache_key, jar_path)

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
    result = _filter_output(stdout)

    if rc != 0 and not result.strip():
        result = stderr.strip() or stdout.strip()

    out_len = len(result.strip())
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

    return result


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

    def __init__(self, session: SSHSession) -> None:
        self.session = session
        self._arthas_path: str | None = None
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
        return _exec_command(
            self.session,
            pid,
            f"thread -n {top_n}",
            arthas_path=self._get_arthas_path(owner),
            timeout=30,
            owner=owner,
        )

    def heap_info(self, pid: int) -> str:
        owner = self._resolve_owner(pid)
        logger.info("[API] heap_info pid=%d, owner=%s", pid, owner)
        return _exec_command(
            self.session,
            pid,
            "dashboard -n 1",
            arthas_path=self._get_arthas_path(owner),
            timeout=30,
            owner=owner,
        )

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
        expressions = []
        if watch_params:
            expressions.append("params")
        if watch_return:
            expressions.append("returnObj")
        expr_str = "#{" + ",".join(expressions) + "}"
        command = f"watch {class_pattern} {method_pattern} '{expr_str}' -n {times} -x 3"
        if condition:
            command += f" '{condition}'"
        owner = self._resolve_owner(pid)
        return _exec_command(
            self.session,
            pid,
            command,
            arthas_path=self._get_arthas_path(owner),
            timeout=30,
            owner=owner,
        )

    def exec_command(self, pid: int, command: str, timeout: int = 60) -> str:
        owner = self._resolve_owner(pid)
        logger.info("[API] exec_command pid=%d, cmd='%.40s', owner=%s", pid, command, owner)
        return _exec_command(
            self.session,
            pid,
            command,
            arthas_path=self._get_arthas_path(owner),
            timeout=timeout,
            owner=owner,
        )

    def detach(self, pid: int) -> str:
        port: int | None = None
        with _PID_STATE_LOCK:
            if pid in _PID_STATE:
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
                _PID_STATE.pop(pid, None)
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
                _PID_STATE.pop(pid, None)
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
            "rm -rf /tmp/arthas-install && mkdir -p /tmp/arthas-install && "
            "cd /tmp/arthas-install && "
            "(curl -L -o arthas-bin.zip -k "
            "'https://arthas.aliyun.com/download/latest_version?mirror=aliyun' "
            "2>/dev/null || "
            "wget --no-check-certificate -O arthas-bin.zip "
            "'https://arthas.aliyun.com/download/latest_version?mirror=aliyun' "
            "2>/dev/null) && "
            "test -f arthas-bin.zip && unzip -o -q arthas-bin.zip && "
            "mkdir -p ~/.arthas && "
            "(cp -rf /tmp/arthas-install/arthas/* ~/.arthas/ 2>/dev/null \
            || cp -rf /tmp/arthas-install/* ~/.arthas/) && "
            "chmod +x ~/.arthas/as.sh && echo INSTALLED"
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
