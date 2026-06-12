"""Shared pytest configuration.

We mark long-running tests (stress benchmarks, hot-reload watchers,
maintenance workers, in-process Flight SQL servers) with ``@pytest.mark.slow``
so the default `pytest tests/` run stays under a couple of minutes. The full
suite runs on the nightly workflow (.github/workflows/nightly.yml) and on
demand via ``pytest tests/ --runslow``.
"""

from __future__ import annotations

import duckdb
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def shared_project(tmp_path):
    """Minimal havn project on disk: project.yml, transform dirs, warehouse."""
    (tmp_path / "project.yml").write_text(
        "name: test\n"
        "database:\n"
        "  path: warehouse.duckdb\n"
        "connections: {}\n"
        "streams:\n"
        "  full-refresh:\n"
        "    description: test\n"
        "    steps:\n"
        "      - ingest: [all]\n"
    )
    (tmp_path / "transform" / "bronze").mkdir(parents=True)
    (tmp_path / "ingest").mkdir()
    (tmp_path / ".env").write_text("")

    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
    conn.close()

    return tmp_path


@pytest.fixture(autouse=True)
def _reset_server_singletons():
    """Reset the server deps singletons after any test that initialized them.

    The backend, write queue, and read pool in ``havn.server.deps`` are
    module-level singletons keyed to whichever project first touched them.
    Without this, a test that sets ``server_app.PROJECT_DIR`` but forgets to
    call ``reset_shared_conn()`` silently runs against the previous test's
    warehouse (see the test_version_detail nightly flake). Teardown-only is
    enough: every test then starts with clean singletons, and they re-create
    lazily from the current PROJECT_DIR on the next request.
    """
    yield
    import havn.server.deps as deps

    if (
        deps._backend is not None
        or deps._write_queue is not None
        or deps._read_pool is not None
    ):
        deps.reset_shared_conn()


@pytest.fixture
def shared_client(shared_project):
    """TestClient against the shared minimal project."""
    import havn.server.app as server_app
    from havn.server.deps import reset_shared_conn

    reset_shared_conn()
    server_app.PROJECT_DIR = shared_project
    server_app.AUTH_ENABLED = False
    yield TestClient(server_app.app)
    reset_shared_conn()


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
