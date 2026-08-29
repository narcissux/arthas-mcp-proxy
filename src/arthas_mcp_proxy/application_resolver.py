from collections.abc import Iterable
from dataclasses import dataclass, replace

from .errors import DomainError
from .models import ErrorCode
from .process_inventory import ProcessRecord

_SPRING_APP_NAME_PREFIX = "-Dspring.application.name="


@dataclass(frozen=True)
class ApplicationCandidate:
    pid: int
    command: str
    owner: str | None = None
    start_time: str | None = None
    boot_id: str | None = None
    match_evidence: str | None = None

    def identity_key(self) -> tuple[int, str | None]:
        return self.pid, self.start_time


def identity_complete(record: ApplicationCandidate | ProcessRecord) -> bool:
    """True iff start_time and boot_id are both non-empty strings."""
    start_time = record.start_time
    boot_id = record.boot_id
    return (
        isinstance(start_time, str)
        and start_time != ""
        and isinstance(boot_id, str)
        and boot_id != ""
    )


def normalize_application_name(name: str) -> str:
    """Lowercase and treat '-' / '_' as equivalent (locked by B2-1-e)."""
    return name.lower().replace("-", "_")


def _require_auto_match_mode(match_mode: str) -> None:
    if match_mode != "auto":
        raise DomainError(
            ErrorCode.INVALID_ARGUMENT,
            f"Unsupported match_mode: {match_mode}",
        )


def _command_tokens(command: str) -> list[str]:
    return command.split()


def _token_basename(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1]


def _strip_jar_suffix(name: str) -> str:
    if name.lower().endswith(".jar"):
        return name[:-4]
    return name


def _looks_like_fqcn(token: str) -> bool:
    return "." in token and not token.lower().endswith(".jar")


def _match_jar_basename(tokens: list[str], application_name: str) -> bool:
    wanted = normalize_application_name(_strip_jar_suffix(application_name))
    for token in tokens:
        base = _token_basename(token)
        if not base.lower().endswith(".jar"):
            continue
        if normalize_application_name(_strip_jar_suffix(base)) == wanted:
            return True
    return False


def _match_main_fqcn(tokens: list[str], application_name: str) -> bool:
    return any(_looks_like_fqcn(token) and token == application_name for token in tokens)


def _match_main_simple(tokens: list[str], application_name: str) -> bool:
    for token in tokens:
        if _looks_like_fqcn(token) and token.rsplit(".", 1)[-1] == application_name:
            return True
    return False


def _match_spring_application_name(tokens: list[str], application_name: str) -> bool:
    for token in tokens:
        if token.startswith(_SPRING_APP_NAME_PREFIX):
            value = token[len(_SPRING_APP_NAME_PREFIX) :]
            if value == application_name:
                return True
    return False


def match_application(
    command: str,
    application_name: str,
    match_mode: str = "auto",
) -> tuple[bool, str | None]:
    """Match a process command against application_name. First evidence wins."""
    _require_auto_match_mode(match_mode)
    tokens = _command_tokens(command)
    if _match_jar_basename(tokens, application_name):
        return True, "jar_basename"
    if _match_main_fqcn(tokens, application_name):
        return True, "main_fqcn"
    if _match_main_simple(tokens, application_name):
        return True, "main_simple"
    if _match_spring_application_name(tokens, application_name):
        return True, "spring_application_name"
    if application_name in tokens:
        return True, "token"
    return False, None


def _candidate_from_record(record: ProcessRecord) -> ApplicationCandidate:
    return ApplicationCandidate(
        pid=record.pid,
        command=record.command,
        owner=record.owner,
        start_time=record.start_time,
        boot_id=record.boot_id,
    )


def _parse_listing_line(line: str) -> ApplicationCandidate | None:
    normalized = line.strip()
    if normalized.startswith("PID ") and ":" in normalized:
        normalized = normalized[4:].replace(":", " ", 1)
    parts = normalized.split()
    if len(parts) < 2:
        return None
    try:
        pid = int(parts[0])
    except ValueError:
        return None
    if len(parts) >= 4:
        return ApplicationCandidate(
            pid=pid, owner=parts[1], start_time=parts[2], command=" ".join(parts[3:])
        )
    return ApplicationCandidate(pid=pid, command=" ".join(parts[1:]))


def find_java_application(
    sources: Iterable[str] | Iterable[ProcessRecord],
    application_name: str,
    match_mode: str = "auto",
) -> ApplicationCandidate:
    _require_auto_match_mode(match_mode)
    candidates: list[ApplicationCandidate] = []
    for item in sources:
        if isinstance(item, ProcessRecord):
            parsed: ApplicationCandidate | None = _candidate_from_record(item)
        else:
            parsed = _parse_listing_line(item)
        if parsed is None:
            continue
        matched, evidence = match_application(
            parsed.command, application_name, match_mode=match_mode
        )
        if matched:
            candidates.append(replace(parsed, match_evidence=evidence))
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        pids = ", ".join(str(candidate.pid) for candidate in candidates)
        raise DomainError(
            ErrorCode.JVM_AMBIGUOUS, f"Multiple matches for {application_name}: {pids}"
        )
    raise DomainError(ErrorCode.JVM_NOT_FOUND, f"Java application not found: {application_name}")


def is_same_process(
    candidate: ApplicationCandidate,
    pid: int,
    start_time: str,
    boot_id: str | None = None,
) -> bool:
    if candidate.start_time is None:
        raise DomainError(ErrorCode.JVM_IDENTITY_CHANGED, "Process start time is unavailable")
    return (
        candidate.pid == pid
        and candidate.start_time == start_time
        and (not boot_id or candidate.boot_id == boot_id)
    )


def _candidate_from_listing_item(
    item: str | ProcessRecord, pid: int
) -> ApplicationCandidate | None:
    if isinstance(item, ProcessRecord):
        return _candidate_from_record(item) if item.pid == pid else None
    normalized = item.strip()
    if normalized.startswith("PID ") and ":" in normalized:
        normalized = normalized[4:].replace(":", " ", 1)
    parts = normalized.split()
    if not parts or not parts[0].isdigit() or int(parts[0]) != pid:
        return None
    if len(parts) >= 4:
        return ApplicationCandidate(
            pid=pid, owner=parts[1], start_time=parts[2], command=" ".join(parts[3:])
        )
    return ApplicationCandidate(pid=pid, command=" ".join(parts[1:]))


def validate_process_identity(
    sources: Iterable[str | ProcessRecord],
    pid: int,
    start_time: str,
    boot_id: str | None = None,
) -> ApplicationCandidate:
    """Validate a previously resolved JVM against a fresh process listing.

    Accepts either legacy text lines or structured ProcessRecord inventory.
    A missing PID means the JVM exited.  A present PID with a different (or
    unavailable) start time, or a changed boot_id when one was recorded, is
    treated as PID reuse/identity loss.  Callers without a recorded start time
    deliberately do not call this function, preserving the legacy PID-only
    behavior.
    """
    candidates: list[ApplicationCandidate] = []
    for item in sources:
        parsed = _candidate_from_listing_item(item, pid)
        if parsed is not None:
            candidates.append(parsed)

    if not candidates:
        raise DomainError(ErrorCode.JVM_EXITED, f"JVM process {pid} has exited")
    candidate = candidates[0]
    if not is_same_process(candidate, pid, start_time, boot_id=boot_id):
        raise DomainError(
            ErrorCode.JVM_IDENTITY_CHANGED,
            f"JVM identity changed for PID {pid}",
        )
    return candidate
