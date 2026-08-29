from dataclasses import dataclass


@dataclass(frozen=True)
class CookbookEntry:
    title: str
    steps: tuple[str, ...]


COOKBOOK = {
    "high_cpu": CookbookEntry(
        "High CPU",
        (
            "Call find_java_application or confirm the existing jvm_handle first",
            "Then call thread_dump, or execute_diagnostic_command dashboard, for CPU evidence",
            "Use trace_method only if a specific hot method is already identified",
        ),
    ),
    "memory": CookbookEntry(
        "Memory pressure",
        (
            "Call find_java_application or confirm the existing jvm_handle first",
            "Then call heap_info, or execute_diagnostic_command memory or dashboard",
        ),
    ),
    "deadlock": CookbookEntry(
        "Deadlock",
        (
            "Call find_java_application or confirm the existing jvm_handle first",
            "Then call execute_diagnostic_command deadlock (catalog deadlock / thread -b)",
        ),
    ),
    "slow_method": CookbookEntry(
        "Slow method",
        (
            "Call find_java_application or confirm the existing jvm_handle first",
            "Then call watch_method or trace_method with times<=5 and ttl<=60",
        ),
    ),
}
