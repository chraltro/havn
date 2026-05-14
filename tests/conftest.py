"""Shared pytest configuration.

We mark long-running tests (stress benchmarks, hot-reload watchers,
maintenance workers, in-process Flight SQL servers) with ``@pytest.mark.slow``
so the default `pytest tests/` run stays under a couple of minutes. The full
suite runs on the nightly workflow (.github/workflows/nightly.yml) and on
demand via ``pytest tests/ --runslow``.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--runslow",
        action="store_true",
        default=False,
        help="Run tests marked @pytest.mark.slow (otherwise skipped).",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "slow: long-running test (stress / soak / cron). Skipped by default; "
        "pass --runslow or run the nightly workflow to include them.",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--runslow"):
        return
    skip_slow = pytest.mark.skip(reason="slow test (use --runslow to enable)")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)
