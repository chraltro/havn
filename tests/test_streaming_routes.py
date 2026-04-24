"""End-to-end tests for the staged webhook ingest route and status endpoints."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import havn.server.app as server_app
    from havn.engine.resource_manager import reset_resource_manager
    from havn.server.deps import reset_shared_conn
    from havn.server.routes import streaming as streaming_route

    monkeypatch.setattr(server_app, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(server_app, "AUTH_ENABLED", False)
    (tmp_path / "project.yml").write_text(
        "name: test\ndatabase:\n  backend: duckdb\n  path: wh.duckdb\n"
    )
    reset_shared_conn()
    reset_resource_manager()
    streaming_route._worker = None
    yield TestClient(server_app.app)
    reset_shared_conn()
    streaming_route.shutdown_flush_worker()


def test_ingest_webhook_stages_event(client):
    r = client.post("/api/ingest/webhook/orders", json={"id": 1, "total": 9.99})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "staged"
    assert body["source"] == "orders"


def test_ingest_webhook_rejects_invalid_source(client):
    r = client.post("/api/ingest/webhook/bad--source", json={"x": 1})
    assert r.status_code == 400


def test_ingest_webhook_rejects_invalid_json(client):
    r = client.post(
        "/api/ingest/webhook/orders",
        content=b"{not valid",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400


def test_status_reports_backlog(client):
    client.post("/api/ingest/webhook/events", json={"hello": "world"})
    r = client.get("/api/streaming/webhook/status")
    assert r.status_code == 200
    body = r.json()
    assert body["backlog"] >= 1


def test_manual_flush_empties_backlog(client):
    for i in range(3):
        client.post("/api/ingest/webhook/events", json={"i": i})
    # Manual flush — the background worker may have already drained some rows.
    # What matters is that the backlog is zero afterwards and all 3 events
    # landed in landing.events.
    client.post("/api/streaming/webhook/flush")

    status = client.get("/api/streaming/webhook/status").json()
    assert status["backlog"] == 0

    from havn.server.deps import _get_write_queue
    rows = _get_write_queue().conn.execute(
        "SELECT COUNT(*) FROM landing.events"
    ).fetchone()[0]
    assert rows == 3
