"""Live models through the server: the runner in `havn serve`, the API, SSE, DAG.

The runner is started by the app's lifespan (``with TestClient(app)``) because
the project has a live model. Data arrives the way it does in production: a
webhook POST, staged and flushed by the webhook worker, which advances the
landing table and wakes the runner.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

BRONZE = (
    "@config materialized=incremental, live=true, incremental_strategy=merge, unique_key=id, "
    "incremental_filter=WHERE _havn_seq > {watermark}\n"
    "SELECT CAST(payload->>'id' AS INTEGER) AS id, CAST(payload->>'total' AS DOUBLE) AS total,\n"
    "       _havn_seq\n"
    "FROM landing.orders\n"
)
GOLD = (
    "@config materialized=incremental, live=true, incremental_strategy=delete+insert, unique_key=k\n"
    "SELECT 1 AS k, COUNT(*) AS orders, SUM(total) AS revenue FROM bronze.orders\n"
)


def _wait(pred, timeout=20.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f"timed out (last={last!r})")


@pytest.fixture()
def project(tmp_path, monkeypatch):
    import havn.server.app as server_app
    from havn.engine.resource_manager import reset_resource_manager
    from havn.server.deps import invalidate_config_cache, reset_shared_conn
    from havn.server.routes import live as live_route
    from havn.server.routes import streaming as streaming_route

    (tmp_path / "project.yml").write_text(
        "name: live_api\ndatabase:\n  path: warehouse.duckdb\n"
        "live:\n  min_interval: 50ms\n  debounce: 20ms\n  max_latency: 300ms\n  poll_interval: 300ms\n",
        encoding="utf-8",
    )
    for rel, body in {"bronze/orders.sql": BRONZE, "gold/revenue.sql": GOLD}.items():
        p = tmp_path / "transform" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    monkeypatch.setattr(server_app, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(server_app, "AUTH_ENABLED", False)
    invalidate_config_cache()
    reset_shared_conn()
    reset_resource_manager()
    streaming_route._worker = None
    yield tmp_path
    live_route.stop_live_runner()
    streaming_route.shutdown_flush_worker()
    reset_shared_conn()
    invalidate_config_cache()


def _query(sql):
    from havn.server.deps import _get_write_queue

    cur = _get_write_queue().cursor()
    try:
        return cur.execute(sql).fetchall()
    finally:
        cur.close()


def _exists(schema, name):
    return bool(_query(
        f"SELECT 1 FROM information_schema.tables WHERE table_schema='{schema}' AND table_name='{name}'"
    ))


def test_webhook_to_gold_through_the_server(project):
    import havn.server.app as server_app

    with TestClient(server_app.app) as client:
        status = client.get("/api/live/status").json()
        assert status["runner"]["running"] is True
        assert [m["model"] for m in status["models"]] == ["bronze.orders", "gold.revenue"]

        for i, total in enumerate([10.0, 20.5, 4.5], start=1):
            assert client.post("/api/ingest/webhook/orders", json={"id": i, "total": total}).status_code == 202
        t0 = time.monotonic()
        assert client.post("/api/streaming/webhook/flush").json()["rows_flushed"] == 3
        _wait(lambda: _exists("gold", "revenue") and _query("SELECT orders, revenue FROM gold.revenue") == [(3, 35.0)])
        assert time.monotonic() - t0 < 15

        # An update to order 2 replaces it (merge on id).
        client.post("/api/ingest/webhook/orders", json={"id": 2, "total": 25.5})
        client.post("/api/streaming/webhook/flush")
        _wait(lambda: _query("SELECT orders, revenue FROM gold.revenue") == [(3, 40.0)])

        status = client.get("/api/live/status").json()
        by = {m["model"]: m for m in status["models"]}
        assert by["bronze.orders"]["status"] == "live"
        assert by["gold.revenue"]["lag_seconds"] == 0
        assert by["bronze.orders"]["refreshes"] >= 2
        src = {s["source"]: s for s in status["sources"]}
        assert src["landing.orders"]["watermark"] == 4

        dag = client.get("/api/dag").json()
        live_nodes = {n["id"] for n in dag["nodes"] if n.get("live")}
        assert live_nodes == {"bronze.orders", "gold.revenue"}


def test_pause_resume_refresh_and_advance_endpoints(project):
    import havn.server.app as server_app

    with TestClient(server_app.app) as client:
        assert client.post("/api/live/models/nope.missing/pause").status_code == 404
        r = client.post("/api/live/models/bronze.orders/pause")
        assert r.status_code == 200 and r.json()["paused"] is True

        # An external writer lands rows itself and announces them over HTTP.
        _query("CREATE SCHEMA IF NOT EXISTS landing")
        _query("CREATE TABLE landing.orders (id VARCHAR, received_at TIMESTAMP, payload JSON)")
        _query("""INSERT INTO landing.orders VALUES ('a', now(), '{"id": 1, "total": 5}')""")
        r = client.post("/api/live/sources/landing.orders/advance")
        assert r.json() == {"source": "landing.orders", "rows": 1, "advanced": True,
                            "watermark_from": 0, "watermark": 1}
        assert client.post("/api/live/sources/landing.orders/advance").json()["advanced"] is False
        assert client.post("/api/live/sources/bad/advance").status_code == 400

        time.sleep(0.8)
        assert not _exists("bronze", "orders")  # paused
        status = {m["model"]: m for m in client.get("/api/live/status").json()["models"]}
        assert status["bronze.orders"]["status"] == "paused"
        assert status["gold.revenue"]["status"] == "waiting"

        assert client.post("/api/live/models/bronze.orders/resume").json()["paused"] is False
        _wait(lambda: _exists("gold", "revenue") and _query("SELECT orders FROM gold.revenue") == [(1,)])
        assert client.post("/api/live/models/bronze.orders/refresh").status_code == 200


def test_sse_stream_reports_advances_and_refreshes(project):
    import havn.server.app as server_app

    with TestClient(server_app.app) as client:
        client.post("/api/ingest/webhook/orders", json={"id": 1, "total": 1})
        client.post("/api/streaming/webhook/flush")
        _wait(lambda: _exists("gold", "revenue"))
        events = []
        with client.stream("GET", "/api/live/events?max_idle=0.5") as resp:
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    events.append(line[7:])
        assert "hello" in events
        assert "advance" in events
        assert "refresh" in events


def test_stop_and_start_runner(project):
    import havn.server.app as server_app

    with TestClient(server_app.app) as client:
        assert client.post("/api/live/stop").json() == {"running": False}
        assert client.get("/api/live/status").json()["runner"]["running"] is False
        # Pause works without a runner too: it is stored in the warehouse.
        assert client.post("/api/live/models/gold.revenue/pause").json()["paused"] is True
        assert client.post("/api/live/models/gold.revenue/refresh").status_code == 409
        assert client.post("/api/live/start").json() == {"running": True}
        status = {m["model"]: m for m in client.get("/api/live/status").json()["models"]}
        assert status["gold.revenue"]["paused"] is True


def test_runner_not_started_without_live_models(tmp_path, monkeypatch):
    import havn.server.app as server_app
    from havn.server.deps import invalidate_config_cache, reset_shared_conn
    from havn.server.routes import live as live_route

    (tmp_path / "project.yml").write_text("name: plain\n", encoding="utf-8")
    (tmp_path / "transform" / "bronze").mkdir(parents=True)
    (tmp_path / "transform" / "bronze" / "a.sql").write_text("SELECT 1 AS x\n", encoding="utf-8")
    monkeypatch.setattr(server_app, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(server_app, "AUTH_ENABLED", False)
    invalidate_config_cache()
    reset_shared_conn()
    try:
        with TestClient(server_app.app) as client:
            assert live_route.get_live_runner() is None
            body = client.get("/api/live/status").json()
            assert body["models"] == [] and body["runner"]["running"] is False
    finally:
        reset_shared_conn()
        invalidate_config_cache()
