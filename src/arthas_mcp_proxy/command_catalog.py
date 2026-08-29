import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CommandSpec:
    name: str
    template: str
    risk: str
    description: str
    streaming: bool = False


# This is deliberately a small catalogue, not the complete Arthas command suite.
# Every entry is a read-only, bounded diagnostic command with structured params.
COMMANDS = {
    "thread_dump": CommandSpec("thread_dump", "thread -n {top_n}", "read_only", "Thread CPU dump"),
    "heap_info": CommandSpec("heap_info", "memory", "read_only", "Memory pool information"),
    "deadlock": CommandSpec("deadlock", "thread -b", "read_only", "Detect deadlocked threads"),
    "top_cpu": CommandSpec("top_cpu", "top -n {top_n}", "read_only", "Top CPU threads"),
    "jvm": CommandSpec("jvm", "jvm", "read_only", "JVM runtime information"),
    "dashboard": CommandSpec(
        "dashboard", "dashboard -n {top_n}", "read_only", "One JVM dashboard sample"
    ),
    "memory": CommandSpec("memory", "memory", "read_only", "Memory pool information"),
    "version": CommandSpec("version", "version", "read_only", "Arthas version"),
    "sysprop": CommandSpec("sysprop", "sysprop{pattern_suffix}", "read_only", "System properties"),
    "sysenv": CommandSpec("sysenv", "sysenv{key_suffix}", "read_only", "Environment variables"),
    "class_search": CommandSpec(
        "class_search", "sc -d {class_pattern}", "read_only", "Search class metadata"
    ),
    "method_search": CommandSpec(
        "method_search",
        "sm -d {class_pattern}{method_suffix}",
        "read_only",
        "Search method metadata",
    ),
    "trace_method": CommandSpec(
        "trace_method",
        "trace {class_pattern} {method_pattern}{condition_suffix} -n {times}",
        "read_only",
        "Method execution trace",
        streaming=True,
    ),
    "watch_method": CommandSpec(
        "watch_method",
        "watch {class_pattern} {method_pattern}{condition_suffix} -n {times}",
        "read_only",
        "Method parameter and return watch",
        streaming=True,
    ),
    "decompile_class": CommandSpec(
        "decompile_class",
        "jad --source-only {class}",
        "read_only",
        "Decompile class to Java source",
        streaming=False,
    ),
}

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.$*?\[\]:/=-]+$")


def _token(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or not _TOKEN_RE.fullmatch(value.strip()):
        raise ValueError(f"{field} contains unsupported characters")
    return value.strip()


def _bounded_int(params: dict[str, Any], field: str, default: int) -> int:
    value = params.get(field, default)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
        raise ValueError(f"{field} must be between 1 and 100")
    return value


def build_command(name: str, params: dict[str, Any]) -> str:
    try:
        spec = COMMANDS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown command: {name}") from exc

    allowed: set[str] = set()
    if name in {"thread_dump", "top_cpu", "dashboard"}:
        allowed.add("top_n")
    if name == "sysprop":
        allowed.add("pattern")
    if name == "sysenv":
        allowed.add("key")
    if name in {"class_search", "method_search"}:
        allowed.add("class_pattern")
    if name == "method_search":
        allowed.add("method_pattern")
    if name in {"trace_method", "watch_method"}:
        allowed = {"class_pattern", "method_pattern", "condition", "times"}
    if name == "decompile_class":
        allowed = {"class_pattern", "class"}
    unknown = set(params) - allowed
    if unknown:
        raise ValueError(f"unknown parameter: {sorted(unknown)[0]}")

    values: dict[str, Any] = {}
    if name in {"thread_dump", "top_cpu", "dashboard"}:
        values["top_n"] = _bounded_int(params, "top_n", 20 if name != "dashboard" else 1)
    if name == "sysprop":
        values["pattern_suffix"] = (
            f" {_token(params['pattern'], 'pattern')}" if "pattern" in params else ""
        )
    if name == "sysenv":
        values["key_suffix"] = f" {_token(params['key'], 'key')}" if "key" in params else ""
    if name in {"class_search", "method_search"}:
        values["class_pattern"] = _token(params.get("class_pattern"), "class_pattern")
    if name == "method_search":
        values["method_suffix"] = (
            f" {_token(params['method_pattern'], 'method_pattern')}"
            if "method_pattern" in params
            else ""
        )
    if name in {"trace_method", "watch_method"}:
        values["class_pattern"] = _token(params.get("class_pattern"), "class_pattern")
        values["method_pattern"] = _token(params.get("method_pattern"), "method_pattern")
        times = _bounded_int(params, "times", 5)
        condition = params.get("condition")
        if condition is not None:
            condition = _token(condition, "condition")
        values.update(times=times, condition_suffix=f" '{condition}'" if condition else "")
    if name == "decompile_class":
        raw = params.get("class_pattern", params.get("class"))
        field = "class_pattern" if "class_pattern" in params else "class"
        values["class"] = _token(raw, field)
    return spec.template.format(**values)
