"""Tests for the semantic layer: metric definitions, SQL compilation, API."""

from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from havn.engine.semantic import (
    SemanticError,
    compile_metric,
    get_metric,
    load_metrics,
)
from havn.engine.sql_safety import validate_read_only_query


METRICS_YML = """
metrics:
  - name: revenue
    description: Total order revenue
    model: gold.orders
    measure: SUM(amount)
    dimensions: [region, status]
    time_dimension: order_date
    filters:
      - status != 'cancelled'
  - name: order_count
    model: gold.orders
    measure: COUNT(*)
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "project.yml").write_text("name: test\ndatabase:\n  path: warehouse.duckdb\n")
    (tmp_path / "metrics").mkdir()
    (tmp_path / "metrics" / "orders.yml").write_text(METRICS_YML)

    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA IF NOT EXISTS gold")
    conn.execute("""
        CREATE TABLE gold.orders AS
        SELECT * FROM (VALUES
            ('eu', 'paid',      DATE '2026-01-01', 100.0),
            ('eu', 'paid',      DATE '2026-01-02', 50.0),
            ('us', 'paid',      DATE '2026-01-01', 200.0),
            ('us', 'cancelled', DATE '2026-01-01', 999.0)
        ) AS t(region, status, order_date, amount)
    """)
    conn.close()
    return tmp_path


# --- Loading ---


def test_load_metrics(project):
    metrics, errors = load_metrics(project)
    assert errors == []
    assert set(metrics) == {"revenue", "order_count"}
    rev = metrics["revenue"]
    assert rev.model == "gold.orders"
    assert rev.dimensions == ["region", "status"]
    assert rev.time_dimension == "order_date"
    assert rev.filters == ["status != 'cancelled'"]


def test_load_metrics_no_dir(tmp_path):
    metrics, errors = load_metrics(tmp_path)
    assert metrics == {} and errors == []


def test_load_reports_bad_definitions_without_failing_good_ones(project):
    (project / "metrics" / "bad.yml").write_text("""
metrics:
  - name: "bad name!"
    model: gold.orders
    measure: COUNT(*)
  - name: bad_model
    model: gold.orders.extra.deep
    measure: COUNT(*)
  - name: bad_filter
    model: gold.orders
    measure: COUNT(*)
    filters: ["1=1; DROP TABLE gold.orders"]
  - name: revenue
    model: gold.orders
    measure: COUNT(*)
""")
    metrics, errors = load_metrics(project)
    # The two valid metrics from orders.yml survive
    assert set(metrics) == {"revenue", "order_count"}
    assert len(errors) == 4  # bad name, bad model, bad filter, duplicate
    assert any("duplicate" in e for e in errors)


def test_load_invalid_yaml_reported(project):
    (project / "metrics" / "broken.yml").write_text("metrics: [unclosed")
    metrics, errors = load_metrics(project)
    assert set(metrics) == {"revenue", "order_count"}
    assert any("broken.yml" in e for e in errors)


def test_get_metric_unknown(project):
    with pytest.raises(SemanticError, match="unknown metric"):
        get_metric(project, "nope")


# --- Compilation ---


def test_compile_plain_aggregate(project):
    sql = compile_metric(get_metric(project, "order_count"))
    assert "COUNT(*) AS order_count" in sql
    assert "GROUP BY" not in sql
    validate_read_only_query(sql)


def test_compile_with_dimensions_and_grain(project):
    sql = compile_metric(
        get_metric(project, "revenue"), dimensions=["region"], grain="day",
    )
    assert "DATE_TRUNC('day', order_date) AS day" in sql
    assert "GROUP BY 1, 2" in sql
    assert "(status != 'cancelled')" in sql
    validate_read_only_query(sql)


def test_compile_rejects_undeclared_dimension(project):
    with pytest.raises(SemanticError, match="not declared"):
        compile_metric(get_metric(project, "revenue"), dimensions=["amount"])


def test_compile_rejects_bad_grain(project):
    with pytest.raises(SemanticError, match="invalid grain"):
        compile_metric(get_metric(project, "revenue"), grain="fortnight")


def test_compile_grain_requires_time_dimension(project):
    with pytest.raises(SemanticError, match="no time_dimension"):
        compile_metric(get_metric(project, "order_count"), grain="day")


def test_compile_deduplicates_dimensions(project):
    sql = compile_metric(get_metric(project, "revenue"), dimensions=["region", "region"])
    assert sql.count("region") == 1


def test_compile_escapes_time_literals(project):
    sql = compile_metric(get_metric(project, "revenue"), start="2026-01-01' OR '1'='1")
    # The embedded quote must be doubled, leaving a single string literal.
    assert "'2026-01-01'' OR ''1''=''1'" in sql
    validate_read_only_query(sql)


def test_compiled_sql_executes(project):
    sql = compile_metric(
        get_metric(project, "revenue"),
        dimensions=["region"],
        grain="day",
        start="2026-01-01",
        end="2026-01-02",
    )
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()
    # Only 2026-01-01, cancelled excluded: eu=100, us=200
    assert [(str(r[0])[:10], r[1], r[2]) for r in rows] == [
        ("2026-01-01", "eu", 100.0),
        ("2026-01-01", "us", 200.0),
    ]


# --- API ---


@pytest.fixture
def client(project):
    import havn.server.app as server_app

    server_app.PROJECT_DIR = project
    return TestClient(server_app.app)


def test_api_list_metrics(client):
    resp = client.get("/api/semantic/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert {m["name"] for m in data["metrics"]} == {"revenue", "order_count"}
    assert data["errors"] == []


def test_api_compile(client):
    resp = client.post(
        "/api/semantic/compile",
        json={"metric": "revenue", "dimensions": ["region"], "grain": "month"},
    )
    assert resp.status_code == 200
    assert "DATE_TRUNC('month'" in resp.json()["sql"]


def test_api_query_metric(client):
    resp = client.post(
        "/api/semantic/query",
        json={"metric": "revenue", "dimensions": ["region"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["columns"] == ["region", "revenue"]
    # DECIMAL sums are JSON-encoded as strings (same as /api/query)
    assert [[r, float(v)] for r, v in data["rows"]] == [["eu", 150.0], ["us", 200.0]]
    assert data["truncated"] is False


def test_api_query_unknown_metric_400(client):
    resp = client.post("/api/semantic/query", json={"metric": "nope"})
    assert resp.status_code == 400
    assert "unknown metric" in resp.json()["detail"]


def test_api_query_undeclared_dimension_400(client):
    resp = client.post(
        "/api/semantic/query", json={"metric": "revenue", "dimensions": ["amount"]}
    )
    assert resp.status_code == 400


def test_api_query_respects_limit(client):
    resp = client.post(
        "/api/semantic/query",
        json={"metric": "revenue", "dimensions": ["region", "status"], "limit": 1},
    )
    assert resp.status_code == 200
    assert len(resp.json()["rows"]) == 1
