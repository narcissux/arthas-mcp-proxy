"""Integration test fixtures and environment checks.

This conftest provides:
    - Environment variable validation (fails fast if misconfigured)
    - Shared integration test utilities
    - Docker / testcontainers lifecycle helpers (future use)
"""

from __future__ import annotations

import logging
import os

import pytest

logger = logging.getLogger(__name__)


# --- Mandatory env-var check (runs once when pytest starts collecting) ---


def pytest_configure(config: pytest.Config) -> None:
    """Fail fast if a user runs integration tests without env vars.

    We do *not* want to silently skip and let the user think tests passed.
    When ``-m integration`` is used, every required env var must be present.
    """
    # Only enforce when the user explicitly selected integration tests
    marker_expr = config.getoption("-m", "")
    if "integration" not in str(marker_expr):
        return

    missing: list[str] = []
    for name in ("TEST_SSH_HOST", "TEST_SSH_USER", "TEST_SSH_PASSWORD"):
        if not os.environ.get(name):
            missing.append(name)

    if missing:
        pytest.exit(
            "\n"
            "Integration tests require SSH target credentials.\n"
            f"Missing env vars: {', '.join(missing)}\n"
            "\n"
            "Set them before running integration tests:\n"
            "  export TEST_SSH_HOST=<your-server>\n"
            "  export TEST_SSH_USER=<username>\n"
            "  export TEST_SSH_PASSWORD=<password>\n"
            "  export TEST_SSH_PORT=22          # optional\n"
            "\n"
            "Or skip integration tests and run unit tests only:\n"
            "  pytest tests/ --ignore=tests/integration/\n",
            returncode=1,
        )


# --- Shared helper for containerised test target (future: testcontainers) ---


def _check_docker_available() -> bool:
    """Return True if Docker daemon is reachable."""
    import subprocess

    try:
        subprocess.run(
            ["docker", "version"],
            capture_output=True,
            timeout=5,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


@pytest.fixture(scope="session")
def docker_available() -> bool:
    """Report whether Docker is available on the current machine."""
    return _check_docker_available()


# --- Collect-only summary ---


def pytest_collection_finish(session: pytest.Session) -> None:
    """Log a summary of collected integration tests."""
    integration_items = [
        item for item in session.items
        if item.get_closest_marker("integration")
    ]
    if integration_items:
        logger.info(
            "Collected %d integration test(s)",
            len(integration_items),
        )
