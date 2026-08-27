from dataclasses import dataclass


@dataclass(frozen=True)
class CookbookEntry:
    title: str
    steps: tuple[str, ...]


COOKBOOK = {
    "high_cpu": CookbookEntry("High CPU", ("Inspect thread CPU usage", "Capture a thread dump")),
    "memory": CookbookEntry("Memory pressure", ("Inspect heap usage", "Review GC activity")),
    "deadlock": CookbookEntry("Deadlock", ("Capture thread dump", "Inspect lock ownership")),
    "slow_method": CookbookEntry("Slow method", ("Watch method latency", "Review returned traces")),
}
