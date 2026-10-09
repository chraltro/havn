"""Telemetry exporters: Prometheus /metrics, OpenLineage events, OpenTelemetry spans."""

from __future__ import annotations

import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from havn.engine.database import ensure_meta_table
from havn.engine.instrumentation import clear_settings_cache
from havn.engine.transform import run_transform


@pytest.fixture(autouse=True)
def _fresh_settings():
    clear_settings_cache()
    yield
    clear_settings_cache()


def _project(tmp_path: Path, telemetry_yml: str = "") -> Path:
    (tmp_path / "project.yml").write_text(
        "name: teleproj\ndatabase:\n  path: warehouse.duckdb\n" + telemetry_yml, encoding="utf-8"
    )
    (tmp_path / "transform" / "bronze").mkdir(parents=True)
    (tmp_path / "transform" / "silver").mkdir(parents=True)
    (tmp_path / "transform" / "bronze" / "orders.sql").write_text(
        "@config materialized=table\nSELECT range AS order_id, range % 3 AS customer_id, range * 1.5 AS amount FROM range(30)\n",
        encoding="utf-8",
    )
    (tmp_path / "transform" / "silver" / "revenue.sql").write_text(
        "@config materialized=table\n"
        "@assert row_count > 100\n"
        "SELECT customer_id, sum(amount) AS revenue FROM bronze.orders GROUP BY customer_id\n",
        encoding="utf-8",
    )
    return tmp_path


def _build(project: Path) -> dict:
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        ensure_meta_table(conn)
        return run_transform(conn, project / "transform", project_dir=project)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------


def _client(project: Path, auth: bool = False) -> TestClient:
    import havn.server.app as server_app
    from havn.server.deps import _clear_config_cache, reset_shared_conn

    reset_shared_conn()
    _clear_config_cache()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = auth
    return TestClient(server_app.app)


def _families(text: str) -> dict:
    from prometheus_client.parser import text_string_to_metric_families

    return {f.name: f for f in text_string_to_metric_families(text)}


def test_metrics_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("HAVN_METRICS_TOKEN", raising=False)
    project = _project(tmp_path)
    r = _client(project).get("/metrics")
    assert r.status_code == 404
    assert "telemetry.prometheus.enabled" in r.json()["detail"]


def test_metrics_parse_and_carry_warehouse_series(tmp_path, monkeypatch):
    monkeypatch.delenv("HAVN_METRICS_TOKEN", raising=False)
    project = _project(tmp_path, "telemetry:\n  prometheus:\n    enabled: true\n")
    _build(project)
    r = _client(project).get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    fams = _families(r.text)   # the whole body parses as Prometheus text format

    def samples(name):
        return {tuple(sorted(s.labels.items())): s.value for s in fams[name].samples}

    durations = samples("havn_model_last_build_duration_seconds")
    assert (("materialized", "table"), ("model", "bronze.orders"), ("schema", "bronze")) in durations
    rows = samples("havn_model_rows")
    assert rows[(("model", "bronze.orders"), ("schema", "bronze"))] == 30
    assert samples("havn_model_freshness_age_seconds")
    status = samples("havn_model_last_build_success")
    # silver.revenue failed its error assertion, so its last build is an error.
    assert status[(("model", "silver.revenue"), ("schema", "silver"), ("status", "error"))] == 0
    assert status[(("model", "bronze.orders"), ("schema", "bronze"), ("status", "success"))] == 1
    fails = samples("havn_assertion_failures")
    assert fails[(("model", "silver.revenue"), ("severity", "error"))] == 1
    assert "havn_runs" in fams and "havn_query_duration_seconds" in fams
    assert "havn_job_runs" in fams
    assert "havn_write_queue_depth" in fams


def test_metrics_without_per_model_series(tmp_path, monkeypatch):
    monkeypatch.delenv("HAVN_METRICS_TOKEN", raising=False)
    project = _project(tmp_path, "telemetry:\n  prometheus:\n    enabled: true\n    include_models: false\n")
    _build(project)
    fams = _families(_client(project).get("/metrics").text)
    assert "havn_model_rows" not in fams and "havn_runs" in fams


def test_metrics_auth(tmp_path, monkeypatch):
    monkeypatch.delenv("HAVN_METRICS_TOKEN", raising=False)
    project = _project(
        tmp_path, "telemetry:\n  prometheus:\n    enabled: true\n    token: s3cret-scrape\n"
    )
    _build(project)
    client = _client(project, auth=True)
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/metrics", headers={"Authorization": "Bearer s3cret-scrape"})
    assert ok.status_code == 200 and "havn_model_rows" in ok.text


def test_metrics_allow_unauthenticated(tmp_path, monkeypatch):
    monkeypatch.delenv("HAVN_METRICS_TOKEN", raising=False)
    project = _project(
        tmp_path, "telemetry:\n  prometheus:\n    enabled: true\n    allow_unauthenticated: true\n"
    )
    assert _client(project, auth=True).get("/metrics").status_code == 200


def test_metrics_localhost_only(tmp_path, monkeypatch):
    from havn.server.routes import prometheus as prom

    monkeypatch.delenv("HAVN_METRICS_TOKEN", raising=False)
    project = _project(
        tmp_path, "telemetry:\n  prometheus:\n    enabled: true\n    allow_unauthenticated: localhost\n"
    )
    client = _client(project, auth=True)
    # TestClient connects from "testclient", which is not loopback.
    assert client.get("/metrics").status_code == 401
    monkeypatch.setattr(prom, "_LOCAL_HOSTS", {"testclient"})
    assert client.get("/metrics").status_code == 200


def test_env_token_still_switches_metrics_on(tmp_path, monkeypatch):
    monkeypatch.setenv("HAVN_METRICS_TOKEN", "legacy-token")
    project = _project(tmp_path)
    client = _client(project)
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer legacy-token"}).status_code == 200


# ---------------------------------------------------------------------------
# OpenLineage
# ---------------------------------------------------------------------------


class _Collector(BaseHTTPRequestHandler):
    events: list = []
    paths: list = []
    auth: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        type(self).events.append(json.loads(self.rfile.read(length)))
        type(self).paths.append(self.path)
        type(self).auth.append(self.headers.get("Authorization"))
        self.send_response(201)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def lineage_server():
    handler = type("H", (_Collector,), {"events": [], "paths": [], "auth": []})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", handler
    server.shutdown()


def test_openlineage_events_over_http(tmp_path, lineage_server):
    url, handler = lineage_server
    project = _project(
        tmp_path,
        "telemetry:\n  openlineage:\n    enabled: true\n"
        f"    url: {url}\n    api_key: ol-key\n    namespace: analytics\n",
    )
    _build(project)   # run end flushes the queue
    events = handler.events
    by = {(e["job"]["name"], e["eventType"]) for e in events}
    assert by == {
        ("bronze.orders", "START"), ("bronze.orders", "COMPLETE"),
        ("silver.revenue", "START"), ("silver.revenue", "COMPLETE"),
    }
    assert set(handler.paths) == {"/api/v1/lineage"}
    assert set(handler.auth) == {"Bearer ol-key"}

    complete = next(e for e in events if e["job"]["name"] == "silver.revenue" and e["eventType"] == "COMPLETE")
    start = next(e for e in events if e["job"]["name"] == "silver.revenue" and e["eventType"] == "START")
    assert complete["run"]["runId"] == start["run"]["runId"]
    uuid.UUID(complete["run"]["runId"])
    assert complete["schemaURL"].startswith("https://openlineage.io/spec/")
    assert complete["producer"].startswith("https://github.com/")
    assert complete["job"]["namespace"] == "analytics"
    assert start["job"]["facets"]["sql"]["query"].startswith("SELECT customer_id")
    assert complete["job"]["facets"]["jobType"]["jobType"] == "MODEL"
    assert [i["name"] for i in complete["inputs"]] == ["bronze.orders"]
    (out,) = complete["outputs"]
    assert out["name"] == "silver.revenue" and out["namespace"].startswith("duckdb://")
    fields = {f["name"] for f in out["facets"]["schema"]["fields"]}
    assert fields == {"customer_id", "revenue"}
    lineage = out["facets"]["columnLineage"]["fields"]
    assert lineage["revenue"]["inputFields"][0]["name"] == "bronze.orders"
    assert lineage["revenue"]["inputFields"][0]["field"] == "amount"
    assert out["outputFacets"]["outputStatistics"]["rowCount"] == 3
    for facet in [*complete["run"]["facets"].values(), *out["facets"].values()]:
        assert facet["_producer"] and facet["_schemaURL"].startswith("https://openlineage.io/spec/facets/")
    parent = complete["run"]["facets"]["parent"]
    uuid.UUID(parent["run"]["runId"])
    assert parent["job"]["name"] == "transform"


def test_openlineage_fail_event_to_a_file(tmp_path):
    project = _project(
        tmp_path, "telemetry:\n  openlineage:\n    enabled: true\n    transport: file\n    path: lineage/events.jsonl\n"
    )
    (project / "transform" / "silver" / "broken.sql").write_text(
        "@config materialized=table\nSELECT * FROM landing.not_there\n", encoding="utf-8"
    )
    _build(project)
    lines = (project / "lineage" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    fail = [e for e in events if e["eventType"] == "FAIL"]
    assert [e["job"]["name"] for e in fail] == ["silver.broken"]
    assert "not_there" in fail[0]["run"]["facets"]["errorMessage"]["message"]


def test_openlineage_down_does_not_fail_the_build(tmp_path):
    project = _project(
        tmp_path, "telemetry:\n  openlineage:\n    enabled: true\n    url: http://127.0.0.1:9\n    timeout_s: 0.5\n"
    )
    assert _build(project)["bronze.orders"] == "built"


# ---------------------------------------------------------------------------
# OpenTelemetry
# ---------------------------------------------------------------------------


@pytest.fixture
def spans():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from havn.engine.telemetry import otel

    exporter = InMemorySpanExporter()
    otel.use_test_exporter(exporter)
    yield exporter
    otel.use_test_exporter(None)


def test_otel_run_step_model_spans(tmp_path, spans):
    project = _project(tmp_path, "telemetry:\n  opentelemetry:\n    enabled: true\n")
    _build(project)
    finished = {s.name: s for s in spans.get_finished_spans()}
    run = finished["havn transform"]
    step = finished["step transform"]
    model = finished["model silver.revenue"]
    assert step.parent.span_id == run.context.span_id
    assert model.parent.span_id == step.context.span_id
    assert model.context.trace_id == run.context.trace_id
    assert model.attributes["havn.model"] == "silver.revenue"
    assert model.attributes["havn.rows"] == 3
    assert model.attributes["havn.plan_captured"] is True
    assert run.attributes["havn.run.kind"] == "transform"
    assert run.resource.attributes["service.name"] == "havn"


def test_otel_failed_model_span_has_error_status(tmp_path, spans):
    from opentelemetry.trace import StatusCode

    project = _project(tmp_path, "telemetry:\n  opentelemetry:\n    enabled: true\n")
    (project / "transform" / "silver" / "broken.sql").write_text(
        "@config materialized=table\nSELECT * FROM landing.not_there\n", encoding="utf-8"
    )
    _build(project)
    broken = next(s for s in spans.get_finished_spans() if s.name == "model silver.broken")
    assert broken.status.status_code == StatusCode.ERROR


def test_otel_job_steps(tmp_path, spans):
    from havn.engine.orchestration import Job, execute_job, resolve_execution_plan
    from havn.engine.transform.discovery import build_dag, discover_all_models

    project = _project(tmp_path, "telemetry:\n  opentelemetry:\n    enabled: true\n")
    job = Job(name="nightly", target="bronze.orders", file_path=project / "orchestration" / "nightly.yml")
    dag = build_dag(discover_all_models(project))
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        ensure_meta_table(conn)
        plan = resolve_execution_plan(job.target, dag, project, conn=conn)
        assert execute_job(job, plan, conn, project).status == "success"
    finally:
        conn.close()
    finished = {s.name: s for s in spans.get_finished_spans()}
    run = finished["havn job nightly"]
    step = finished["step bronze.orders"]
    model = finished["model bronze.orders"]
    assert step.parent.span_id == run.context.span_id
    assert model.parent.span_id == step.context.span_id


def test_otel_api_request_spans(tmp_path, spans):
    project = _project(tmp_path, "telemetry:\n  opentelemetry:\n    enabled: true\n")
    _build(project)
    client = _client(project)
    assert client.get("/api/perf/models/bronze.orders").status_code == 200
    req = next(s for s in spans.get_finished_spans() if s.name.startswith("GET /api/perf/models"))
    assert req.name == "GET /api/perf/models/{model}"
    assert req.attributes["http.response.status_code"] == 200
    # A traceparent header continues the caller's trace.
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    client.get("/api/perf/runs", headers={"traceparent": f"00-{trace_id}-00f067aa0ba902b7-01"})
    runs = next(s for s in spans.get_finished_spans() if s.name == "GET /api/perf/runs")
    assert format(runs.context.trace_id, "032x") == trace_id


def test_otel_is_a_no_op_without_the_packages(tmp_path, monkeypatch):
    from havn.engine.telemetry import otel

    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    assert otel.otel_available() is False
    project = _project(tmp_path, "telemetry:\n  opentelemetry:\n    enabled: true\n")
    from havn.config import load_project

    assert otel.get_tracer(load_project(project).telemetry.opentelemetry) is None
    assert _build(project)["bronze.orders"] == "built"
    assert _client(project).get("/api/perf/runs").status_code == 200


def test_otel_disabled_is_a_no_op(tmp_path, spans):
    project = _project(tmp_path)
    _build(project)
    assert spans.get_finished_spans() == ()
