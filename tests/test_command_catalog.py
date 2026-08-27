import pytest

from arthas_mcp_proxy.command_catalog import COMMANDS, build_command


@pytest.mark.contract
def test_commands_have_read_only_metadata() -> None:
    for spec in COMMANDS.values():
        assert spec.risk == "read_only"
        assert spec.description


@pytest.mark.contract
def test_safe_commands_are_registered() -> None:
    assert {
        "thread_dump",
        "heap_info",
        "deadlock",
        "top_cpu",
        "jvm",
        "dashboard",
        "memory",
        "version",
        "sysprop",
        "sysenv",
        "class_search",
        "method_search",
    } <= set(COMMANDS)


@pytest.mark.contract
@pytest.mark.parametrize(
    ("name", "params", "expected"),
    [
        ("jvm", {}, "jvm"),
        ("dashboard", {}, "dashboard -n 1"),
        ("dashboard", {"top_n": 2}, "dashboard -n 2"),
        ("memory", {}, "memory"),
        ("version", {}, "version"),
        ("sysprop", {}, "sysprop"),
        ("sysprop", {"pattern": "java.version"}, "sysprop java.version"),
        ("sysenv", {}, "sysenv"),
        ("sysenv", {"key": "PATH"}, "sysenv PATH"),
        ("class_search", {"class_pattern": "com.example.Service"}, "sc -d com.example.Service"),
        (
            "method_search",
            {"class_pattern": "com.example.Service", "method_pattern": "run"},
            "sm -d com.example.Service run",
        ),
    ],
)
def test_read_only_catalog_commands_render(name: str, params: dict, expected: str) -> None:
    assert build_command(name, params) == expected


@pytest.mark.contract
def test_catalog_commands_reject_shell_metacharacters() -> None:
    with pytest.raises(ValueError):
        build_command("sysprop", {"pattern": "x; id"})
    with pytest.raises(ValueError):
        build_command("class_search", {"class_pattern": "x $(id)"})


@pytest.mark.contract
def test_heap_info_is_a_short_read_only_memory_command() -> None:
    assert build_command("heap_info", {}) == "memory"


@pytest.mark.contract
def test_deadlock_command_is_read_only_and_renders() -> None:
    assert COMMANDS["deadlock"].risk == "read_only"
    assert build_command("deadlock", {}) == "thread -b"


@pytest.mark.contract
def test_top_cpu_command_renders_and_validates_bounds() -> None:
    assert build_command("top_cpu", {"top_n": 10}) == "top -n 10"
    with pytest.raises(ValueError):
        build_command("top_cpu", {"top_n": 0})


@pytest.mark.contract
def test_unknown_command_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_command("unknown", {})


@pytest.mark.contract
def test_thread_dump_command_uses_parameters() -> None:
    command = build_command("thread_dump", {"top_n": 5})
    rendered = " ".join(command) if isinstance(command, list) else command
    assert "thread" in rendered
    assert "5" in rendered


@pytest.mark.contract
@pytest.mark.parametrize("top_n", [0, 101])
def test_thread_dump_top_n_out_of_range_is_rejected(top_n: int) -> None:
    with pytest.raises(ValueError):
        build_command("thread_dump", {"top_n": top_n})


@pytest.mark.contract
def test_catalog_rejects_unknown_parameters_instead_of_ignoring_them() -> None:
    with pytest.raises(ValueError, match="unknown parameter"):
        build_command("top_cpu", {"top_n": 5, "condition": "x' ; id #"})
