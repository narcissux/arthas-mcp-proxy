from dataclasses import dataclass


@dataclass(frozen=True)
class TargetIdentity:
    host: str
    port: int
    username: str
    pid: int
    start_time: str | None = None

    @property
    def handle(self) -> str:
        """Stable opaque handle for an identified JVM target."""
        suffix = self.start_time or "unknown-start"
        return f"jvm:{self.host}:{self.port}:{self.username}:{self.pid}:{suffix}"


def make_identity(
    host: str,
    port: int,
    username: str,
    pid: int,
    start_time: str | None = None,
) -> TargetIdentity:
    return TargetIdentity(host, port, username, pid, start_time)


def state_key(
    identity: TargetIdentity,
) -> tuple[str, int, str, int] | tuple[str, int, str, int, str]:
    base = (identity.host, identity.port, identity.username, identity.pid)
    if identity.start_time is None:
        return base
    return (*base, identity.start_time)


def target_key(host: str, port: int, username: str) -> str:
    return f"{username}@{host}:{port}"


def parse_handle(handle: str) -> TargetIdentity:
    """Parse and validate an opaque JVM handle."""
    parts = handle.split(":", 5)
    if len(parts) != 6 or parts[0] != "jvm":
        raise ValueError("invalid jvm handle")
    _, host, port, username, pid, start_time = parts
    if not host or not username or not start_time:
        raise ValueError("invalid jvm handle")
    try:
        return TargetIdentity(host, int(port), username, int(pid), start_time)
    except ValueError as exc:
        raise ValueError("invalid jvm handle") from exc
