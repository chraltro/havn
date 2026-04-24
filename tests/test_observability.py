"""Tests for the Prometheus endpoint and structlog wiring."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from havn.engine.observability import (
    QUERIES_TOTAL,
    STREAMING_EVENTS,
    render_prometheus,
)


def test_render_prometheus_contains_expected_metric_names():
    QUERIES_TOTAL.labels(category="query", status="succeeded").inc()
    text = render_prometheus().decode()
    assert "havn_query_duration_seconds" in text
    assert "havn_queries_total" in text
    assert "havn_active_tasks" in text
    assert 'category="query"' in text


def test_streaming_events_counter_labels():
    STREAMING_EVENTS.labels(source="demo", status="flushed").inc(3)
    text = render_prometheus().decode()
    assert 'source="demo"' in text
    assert "havn_streaming_events_total" in text


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import havn.server.app as server_app

    monkeypatch.setattr(server_app, "PROJECT_DIR", tmp_path)
    (tmp_path / "project.yml").write_text(
        "name: test\ndatabase:\n  backend: duckdb\n  path: warehouse.duckdb\n"
    )
    return TestClient(server_app.app)


def test_metrics_endpoint_returns_prometheus_text(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "# TYPE" in r.text


def test_health_alias_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_structlog_config_idempotent():
    from havn.logging_config import configure_logging

    configure_logging()
    configure_logging()  # should not error on second call
