"""Tests for /v1/export/duckdb."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import havn.server.app as server_app
    from havn.engine.resource_manager import reset_resource_manager
    from havn.server.deps import reset_shared_conn

    monkeypatch.setattr(server_app, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(server_app, "AUTH_ENABLED", False)
    (tmp_path / "project.yml").write_text(
        "name: test\ndatabase:\n  backend: duckdb\n  path: warehouse.duckdb\n"
    )
    reset_shared_conn()
    reset_resource_manager()
    yield TestClient(server_app.app)
    reset_shared_conn()


def test_export_duckdb_streams_warehouse_file(client):
    # Seed the warehouse so the file exists.
    r = client.post("/v1/sql", json={"sql": "CREATE TABLE main.t AS SELECT 1 AS n"})
    assert r.status_code == 200

    # Close the shared conn so FileResponse can stream the file on Windows/POSIX.
    from havn.server.deps import reset_shared_conn

    reset_shared_conn()

    r = client.get("/v1/export/duckdb")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    # DuckDB file format contains the "DUCK" magic in the header.
    assert b"DUCK" in r.content[:64]


def test_export_duckdb_404_when_warehouse_missing(client, tmp_path, monkeypatch):
    """Pointing the config at a non-existent path returns 404 rather than crashing."""
    (tmp_path / "project.yml").write_text(
        "name: test\ndatabase:\n  backend: duckdb\n  path: missing.duckdb\n"
    )
    from havn.server.deps import reset_shared_conn

    reset_shared_conn()
    r = client.get("/v1/export/duckdb")
    assert r.status_code == 404


def test_export_duckdb_requires_execute_permission(tmp_path, monkeypatch):
    """A read-only viewer cannot download the raw (unmasked) warehouse file."""
    import havn.server.app as server_app
    from havn.engine.resource_manager import reset_resource_manager
    from havn.server.deps import _get_shared_conn, reset_shared_conn

    monkeypatch.setattr(server_app, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(server_app, "AUTH_ENABLED", True)
    (tmp_path / "project.yml").write_text(
        "name: test\ndatabase:\n  backend: duckdb\n  path: warehouse.duckdb\n"
    )
    reset_shared_conn()
    reset_resource_manager()

    from havn.engine.auth import authenticate, create_user
    from havn.engine.write_queue import cursor_for

    cur = cursor_for(_get_shared_conn())
    try:
        create_user(cur, "viewer_user", "pw", role="viewer")
        token = authenticate(cur, "viewer_user", "pw")
    finally:
        cur.close()

    client = TestClient(server_app.app)
    try:
        r = client.get(
            "/v1/export/duckdb",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403
    finally:
        reset_shared_conn()
