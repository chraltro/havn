"""Tests for POST /v1/sql Databricks-style endpoint."""

from __future__ import annotations

import json

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


def test_execute_fast_sql_inline(client):
    r = client.post("/v1/sql", json={"sql": "SELECT 1 AS n", "wait_seconds": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["columns"] == ["n"]
    assert body["rows"] == [[1]]
    assert body["row_count"] == 1


def test_execute_invalid_sql_records_failed(client):
    r = client.post("/v1/sql", json={"sql": "NOT SQL", "wait_seconds": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert body["error"]


def test_get_statement_and_result_stream(client):
    r = client.post("/v1/sql", json={"sql": "SELECT 1 AS n, 'a' AS s", "wait_seconds": 5})
    sid = r.json()["statement_id"]
    s = client.get(f"/v1/sql/{sid}")
    assert s.status_code == 200
    assert s.json()["status"] == "succeeded"

    # NDJSON result stream
    ndjson = client.get(
        f"/v1/sql/{sid}/result",
        headers={"accept": "application/x-ndjson"},
    )
    assert ndjson.status_code == 200
    lines = [ln for ln in ndjson.text.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["s"] == "a"


def test_get_unknown_statement_404(client):
    r = client.get("/v1/sql/nonexistent")
    assert r.status_code == 404


def test_cancel_unknown_statement_404(client):
    r = client.delete("/v1/sql/nonexistent")
    assert r.status_code == 404
