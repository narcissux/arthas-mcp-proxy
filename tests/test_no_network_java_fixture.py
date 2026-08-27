"""Local Java fixture smoke test that never needs network access."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# The executable paths come from PATH and the fixture source is repository-owned.
# ruff's subprocess taint rule cannot infer those constraints.


FIXTURE = Path(__file__).parent / "fixtures" / "java" / "NoNetworkFixture.java"


def test_no_network_java_fixture_compiles_and_runs(tmp_path: Path) -> None:
    """Compile and run the fixture locally, without Docker or external downloads."""
    javac = shutil.which("javac")
    java = shutil.which("java")
    if not javac or not java:
        missing = [name for name, path in (("javac", javac), ("java", java)) if not path]
        pytest.fail(
            "no-network Java fixture prerequisite missing: "
            f"{', '.join(missing)}; install a JDK/JRE or provide local PATH entries. "
            "This test intentionally fails instead of silently skipping."
        )

    # Executables are resolved from the local PATH; no network-capable process is used.
    subprocess.run([javac, "-d", str(tmp_path), str(FIXTURE)], check=True)  # noqa: S603
    result = subprocess.run(  # noqa: S603
        [java, "-cp", str(tmp_path), "NoNetworkFixture"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "no-network-java-fixture:ready"
