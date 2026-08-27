"""Remote JVM process inventory with start_time / boot_id (B1-1).

Linux primary path is /proc. jps is fallback and must not invent identity fields.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .errors import DomainError
from .models import ErrorCode

Runner = Callable[[str], tuple[str, str, int]]


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    command: str
    owner: str | None = None
    start_time: str | None = None
    boot_id: str | None = None


def parse_cmdline(raw: str) -> str:
    return raw.replace("\x00", " ").strip()


def parse_stat_start_time(stat: str) -> str:
    """Field 22 of /proc/<pid>/stat (starttime ticks), comm may contain spaces."""
    close = stat.rfind(")")
    if close == -1:
        raise ValueError("invalid /proc stat")
    rest = stat[close + 1 :].split()
    # After comm: state(3) ... starttime is overall field 22 → index 19 in rest
    if len(rest) < 20:
        raise ValueError("invalid /proc stat")
    start_time = rest[19]
    if not start_time.isdigit():
        raise ValueError("invalid start_time ticks")
    return start_time


def parse_status_uid(status: str) -> int | None:
    for line in status.splitlines():
        if line.startswith("Uid:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return None


def owner_from_passwd(uid: int, passwd: str) -> str | None:
    for line in passwd.splitlines():
        fields = line.split(":")
        if len(fields) >= 3 and fields[2].isdigit() and int(fields[2]) == uid and fields[0]:
            return fields[0]
    return None


def is_filtered_command(command: str) -> bool:
    lowered = command.lower()
    return "jps" in lowered or "arthas" in lowered


def collect_from_proc(
    *,
    boot_id: str | None,
    processes: list[tuple[int, str, str, str]],
    passwd: str,
) -> list[ProcessRecord]:
    """processes items: (pid, cmdline_raw, stat, status)."""
    records: list[ProcessRecord] = []
    for pid, cmdline_raw, stat, status in processes:
        command = parse_cmdline(cmdline_raw)
        if "java" not in command.lower() or is_filtered_command(command):
            continue
        uid = parse_status_uid(status)
        owner = owner_from_passwd(uid, passwd) if uid is not None else None
        records.append(
            ProcessRecord(
                pid=pid,
                command=command,
                owner=owner,
                start_time=parse_stat_start_time(stat),
                boot_id=boot_id,
            )
        )
    return records


def collect_from_jps(lines: list[str]) -> list[ProcessRecord]:
    records: list[ProcessRecord] = []
    for line in lines:
        parts = line.strip().split(None, 1)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        command = parts[1]
        if is_filtered_command(command):
            continue
        if "java" not in command.lower() and not command.endswith(".jar") and "/" not in command:
            # jps -l prints main class or jar; still a JVM
            pass
        records.append(
            ProcessRecord(
                pid=int(parts[0]),
                command=command,
                owner=None,
                start_time=None,
                boot_id=None,
            )
        )
    return records


def collect_inventory(
    *,
    proc_available: bool,
    boot_id: str | None = None,
    proc_processes: list[tuple[int, str, str, str]] | None = None,
    passwd: str = "",
    jps_available: bool = False,
    jps_lines: list[str] | None = None,
) -> list[ProcessRecord]:
    if proc_available:
        return collect_from_proc(
            boot_id=boot_id,
            processes=proc_processes or [],
            passwd=passwd,
        )
    if jps_available:
        return collect_from_jps(jps_lines or [])
    raise DomainError(
        ErrorCode.JVM_NOT_FOUND,
        "Failed to list Java processes from /proc and jps",
        phase="discover",
    )


def collect_inventory_over_ssh(run: Runner) -> list[ProcessRecord]:
    """Best-effort remote collect using an SSH exec runner (stdout, stderr, rc)."""
    boot_out, _, boot_rc = run("cat /proc/sys/kernel/random/boot_id")
    proc_probe, _, proc_rc = run("test -d /proc && echo ok")
    proc_available = proc_rc == 0 and proc_probe.strip() == "ok"
    if proc_available:
        pids_out, _, pids_rc = run("ls -1 /proc")
        if pids_rc != 0:
            proc_available = False
        else:
            passwd_out, _, _ = run("cat /etc/passwd")
            processes: list[tuple[int, str, str, str]] = []
            for token in pids_out.split():
                if not token.isdigit():
                    continue
                pid = int(token)
                cmd_out, _, cmd_rc = run(f"cat /proc/{pid}/cmdline")
                if cmd_rc != 0 or "java" not in parse_cmdline(cmd_out).lower():
                    continue
                stat_out, _, stat_rc = run(f"cat /proc/{pid}/stat")
                status_out, _, status_rc = run(f"cat /proc/{pid}/status")
                if stat_rc != 0 or status_rc != 0:
                    continue
                processes.append((pid, cmd_out, stat_out, status_out))
            return collect_inventory(
                proc_available=True,
                boot_id=boot_out.strip() if boot_rc == 0 and boot_out.strip() else None,
                proc_processes=processes,
                passwd=passwd_out,
            )
    jps_out, _, jps_rc = run("jps -l -m 2>/dev/null")
    if jps_rc == 0:
        return collect_inventory(
            proc_available=False,
            jps_available=True,
            jps_lines=jps_out.splitlines(),
        )
    return collect_inventory(proc_available=False, jps_available=False)
