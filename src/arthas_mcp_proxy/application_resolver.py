from collections.abc import Iterable
from dataclasses import dataclass

from .errors import DomainError
from .models import ErrorCode


@dataclass(frozen=True)
class ApplicationCandidate:
    pid: int
    command: str
    owner: str | None = None
    start_time: str | None = None

    def identity_key(self) -> tuple[int, str | None]:
        return self.pid, self.start_time


def find_java_application(lines: Iterable[str], application_name: str) -> ApplicationCandidate:
    candidates: list[ApplicationCandidate] = []
    for line in lines:
        normalized = line.strip()
        if normalized.startswith("PID ") and ":" in normalized:
            normalized = normalized[4:].replace(":", " ", 1)
        parts = normalized.split()
        if len(parts) >= 2 and application_name in parts[-1]:
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            if len(parts) >= 4:
                candidates.append(
                    ApplicationCandidate(
                        pid=pid, owner=parts[1], start_time=parts[2], command=" ".join(parts[3:])
                    )
                )
            else:
                candidates.append(ApplicationCandidate(pid=pid, command=" ".join(parts[1:])))
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        pids = ", ".join(str(candidate.pid) for candidate in candidates)
        raise DomainError(
            ErrorCode.JVM_AMBIGUOUS, f"Multiple matches for {application_name}: {pids}"
        )
    raise DomainError(ErrorCode.JVM_NOT_FOUND, f"Java application not found: {application_name}")


def is_same_process(candidate: ApplicationCandidate, pid: int, start_time: str) -> bool:
    if candidate.start_time is None:
        raise DomainError(ErrorCode.JVM_IDENTITY_CHANGED, "Process start time is unavailable")
    return candidate.pid == pid and candidate.start_time == start_time


def validate_process_identity(
    lines: Iterable[str], pid: int, start_time: str
) -> ApplicationCandidate:
    """Validate a previously resolved JVM against a fresh process listing.

    A missing PID means the JVM exited.  A present PID with a different (or
    unavailable) start time is treated as PID reuse/identity loss.  Callers
    without a recorded start time deliberately do not call this function,
    preserving the legacy PID-only behavior.
    """
    candidates: list[ApplicationCandidate] = []
    for line in lines:
        normalized = line.strip()
        if normalized.startswith("PID ") and ":" in normalized:
            normalized = normalized[4:].replace(":", " ", 1)
        parts = normalized.split()
        if not parts or not parts[0].isdigit() or int(parts[0]) != pid:
            continue
        if len(parts) >= 4:
            candidates.append(
                ApplicationCandidate(
                    pid=pid, owner=parts[1], start_time=parts[2], command=" ".join(parts[3:])
                )
            )
        else:
            candidates.append(ApplicationCandidate(pid=pid, command=" ".join(parts[1:])))

    if not candidates:
        raise DomainError(ErrorCode.JVM_EXITED, f"JVM process {pid} has exited")
    candidate = candidates[0]
    if not is_same_process(candidate, pid, start_time):
        raise DomainError(
            ErrorCode.JVM_IDENTITY_CHANGED,
            f"JVM identity changed for PID {pid}",
        )
    return candidate
