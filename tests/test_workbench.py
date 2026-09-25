"""Tests for the editor workbench endpoint and failing-row SQL."""
from __future__ import annotations

import duckdb
import pytest
from fastapi.testclient import TestClient

from havn.engine.transform import _evaluate_assertion, failing_rows_sql
from havn.engine.transform.models import SQLModel


@pytest.fixture
def project(tmp_path):
    (tmp_path / "project.yml").write_text(
        "name: test\ndatabase:\n  path: warehouse.duckdb\n"
    )
    for layer in ("bronze", "silver", "gold"):
        (tmp_path / "transform" / layer).mkdir(parents=True)
    (tmp_path / "transform" / "bronze" / "orders.sql").write_text(
        "@config materialized=table, schema=bronze\n\n"
        "SELECT * FROM (VALUES (1, 10), (2, NULL), (3, 30), (4, NULL)) t(order_id, customer_id)\n"
    )
    (tmp_path / "transform" / "silver" / "enriched.sql").write_text(
        "@config materialized=table, schema=silver\n"
        "@description Orders with customers\n"
        "@assert no_nulls(customer_id)\n"
        "@assert order_id > 0\n"
        "@col order_id: Primary key\n"
        "@col future_col: Not built yet\n\n"
        "SELECT order_id, customer_id FROM bronze.orders\n"
    )
    (tmp_path / "transform" / "gold" / "report.sql").write_text(
        "@config materialized=view, schema=gold\n\n"
        "SELECT COUNT(*) AS n FROM silver.enriched\n"
    )
    (tmp_path / "transform" / "gold" / "report2.sql").write_text(
        "@config materialized=view, schema=gold\n\n"
        "SELECT n FROM gold.report\n"
    )
    duckdb.connect(str(tmp_path / "warehouse.duckdb")).close()
    return tmp_path


@pytest.fixture
def client(project):
    import havn.server.app as server_app

    server_app.PROJECT_DIR = project
    return TestClient(server_app.app)


def test_workbench_before_build(client):
    resp = client.get("/api/models/workbench", params={"path": "transform/silver/enriched.sql"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "silver.enriched"
    assert data["description"] == "Orders with customers"
    assert data["upstream"] == [{"name": "bronze.orders", "path": "transform/bronze/orders.sql"}]
    assert data["downstream"] == [{"name": "gold.report", "path": "transform/gold/report.sql"}]
    # Transitive, nearest first.
    assert data["downstream_all"] == ["gold.report", "gold.report2"]
    assert data["state"]["built"] is False
    assert [c["expression"] for c in data["checks"]] == ["no_nulls(customer_id)", "order_id > 0"]
    assert all(c["passed"] is None for c in data["checks"])
    # Documented but unbuilt columns still show up.
    assert {"name": "future_col", "type": None, "description": "Not built yet"} in data["columns"]


def test_workbench_after_build(client):
    resp = client.post("/api/transform", json={"force": True})
    assert resp.status_code == 200

    data = client.get(
        "/api/models/workbench", params={"path": "transform/silver/enriched.sql"}
    ).json()
    assert data["state"]["built"] is True
    assert data["state"]["changed_since_build"] is False
    assert data["state"]["row_count"] == 4

    checks = {c["expression"]: c for c in data["checks"]}
    assert checks["no_nulls(customer_id)"]["passed"] is False
    assert checks["order_id > 0"]["passed"] is True

    cols = {c["name"]: c for c in data["columns"]}
    assert cols["order_id"]["description"] == "Primary key"
    assert cols["order_id"]["type"]
    assert cols["customer_id"]["description"] == ""

    assert data["runs"] and data["runs"][0]["status"] in ("success", "warn", "failed")

    # The failing-rows SQL runs through the normal query endpoint and returns
    # exactly the violating rows.
    q = client.post("/api/query", json={"sql": checks["no_nulls(customer_id)"]["failing_sql"]})
    assert q.status_code == 200
    ids = sorted(r[0] for r in q.json()["rows"])
    assert ids == [2, 4]


def test_workbench_flags_edit_since_build(client, project):
    client.post("/api/transform", json={"force": True})
    f = project / "transform" / "silver" / "enriched.sql"
    f.write_text(f.read_text().replace("FROM bronze.orders", "FROM bronze.orders WHERE order_id < 4"))
    data = client.get(
        "/api/models/workbench", params={"path": "transform/silver/enriched.sql"}
    ).json()
    assert data["state"]["changed_since_build"] is True


def test_workbench_unknown_path_is_404(client):
    resp = client.get("/api/models/workbench", params={"path": "transform/silver/nope.sql"})
    assert resp.status_code == 404
    resp = client.get("/api/models/workbench", params={"path": "../../etc/passwd"})
    assert resp.status_code == 404


# --- failing_rows_sql agrees with the evaluator ---


@pytest.fixture
def conn():
    c = duckdb.connect()
    c.execute("CREATE SCHEMA s")
    c.execute(
        "CREATE TABLE s.t AS SELECT * FROM (VALUES "
        "(1, 'a', 5), (2, 'b', -1), (2, 'z', NULL), (3, NULL, 7), (4, 'a', 0)"
        ") v(id, cat, amount)"
    )
    yield c
    c.close()


def _model(**kw) -> SQLModel:
    return SQLModel(
        path=None, name="t", schema="s", full_name="s.t", sql="", query="",
        materialized="table", **kw,
    )


@pytest.mark.parametrize(
    "expr, expected",
    [
        ("no_nulls(cat)", 1),
        ("unique(id)", 2),
        ("accepted_values(cat, ['a', 'b'])", 1),
        ("amount >= 0", 1),
        ("grain(id)", 2),
    ],
)
def test_failing_rows_sql_matches_evaluator(conn, expr, expected):
    model = _model(grain=["id"]) if expr.startswith("grain") else _model()
    sql = failing_rows_sql(model, expr)
    rows = conn.execute(sql).fetchall()
    assert len(rows) == expected
    if not expr.startswith("grain"):
        assert _evaluate_assertion(conn, model, expr).passed is False


def test_failing_rows_sql_multi_column_grain(conn):
    sql = failing_rows_sql(_model(), "grain(id, cat)")
    assert conn.execute(sql).fetchall() == []


def test_failing_rows_sql_row_count_has_no_rows():
    assert failing_rows_sql(_model(), "row_count > 0") is None
    assert failing_rows_sql(_model(), "row_count > 0 AND amount > 0") is None
