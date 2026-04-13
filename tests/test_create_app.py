"""Tests for create_app() backend factory injection hook (SPEC 01)."""

from __future__ import annotations

import pytest

import havn.server.app as server_app
from havn.config import DatabaseConfig
from havn.engine.backends import DuckDBBackend, create_backend


@pytest.fixture(autouse=True)
def _clear_factory():
    """Reset app.state between tests."""
    prev = getattr(server_app.app.state, "backend_factory", None)
    if hasattr(server_app.app.state, "backend_factory"):
        delattr(server_app.app.state, "backend_factory")
    yield
    if prev is not None:
        server_app.app.state.backend_factory = prev
    elif hasattr(server_app.app.state, "backend_factory"):
        delattr(server_app.app.state, "backend_factory")


def test_create_app_noop_without_factory():
    a = server_app.create_app()
    assert a is server_app.app
    assert not hasattr(a.state, "backend_factory")


def test_create_app_sets_factory():
    def factory(project_dir, config):
        return create_backend(config.database, project_dir=project_dir)

    a = server_app.create_app(backend_factory=factory)
    assert a is server_app.app
    assert a.state.backend_factory is factory


def test_get_backend_prefers_factory(tmp_path):
    """When a factory is installed, deps._get_backend uses it."""
    from havn.server import deps

    # Seed a project so _get_config works
    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: w.duckdb\n")

    called = {"count": 0}

    def factory(project_dir, config):
        called["count"] += 1
        return DuckDBBackend(DatabaseConfig(path="custom.duckdb"), project_dir=project_dir)

    server_app.PROJECT_DIR = tmp_path
    server_app.create_app(backend_factory=factory)

    try:
        b = deps._get_backend()
        assert called["count"] == 1
        # Factory-returned backend is used
        assert b._db_path.name == "custom.duckdb"
    finally:
        deps.reset_shared_conn()
