"""Tests for GET /api/home (the Home page's health aggregation)."""
from __future__ import annotations

import json

import duckdb
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def project(tmp_path):
    (tmp_path / "project.yml").write_text("name: harbour\ndatabase:\n  path: warehouse.duckdb\n")
    for layer in ("bronze", "silver", "gold"):
        (tmp_path / "transform" / layer).mkdir(parents=True)
    (tmp_path / "transform" / "bronze" / "orders.sql").write_text(
        "@config materialized=table, schema=bronze\n\n"
        "SELECT * FROM landing.orders\n"
    )
    (tmp_path / "transform" / "silver" / "enriched.sql").write_text(
        "@config materialized=table, schema=silver\n"
        "@assert no_nulls(customer_id)\n"
        "@assert order_id > 0\n"
        "@assert order_id < 3, severity=warn\n\n"
        "SELECT order_id, customer_id FROM bronze.orders\n"
    )
    (tmp_path / "transform" / "gold" / "report.sql").write_text(
        "@config materialized=table, schema=gold\n\n"
        "SELECT COUNT(*) AS n FROM bronze.orders\n"
    )
    # Reads the model whose error-level check fails, so the engine blocks it.
    (tmp_path / "transform" / "gold" / "blocked.sql").write_text(
        "@config materialized=table, schema=gold\n\n"
        "SELECT * FROM silver.enriched\n"
    )
    (tmp_path / "transform" / "gold" / "broken.sql").write_text(
        "@config materialized=table, schema=gold\n\n"
        "SELECT no_such_column FROM bronze.orders\n"
    )
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA landing")
    conn.execute(
        "CREATE TABLE landing.orders AS SELECT * FROM "
        "(VALUES (1, 10), (2, NULL), (3, 30)) t(order_id, customer_id)"
    )
    conn.close()
    return tmp_path


@pytest.fixture
def client(project):
    import havn.server.app as server_app

    server_app.PROJECT_DIR = project
    return TestClient(server_app.app)


def test_home_before_build(client):
    data = client.get("/api/home").json()
    assert data["project_name"] == "harbour"
    assert data["has_data"] is True  # landing.orders exists
    assert data["tiles"]["models"] == {"total": 5, "changed": 0, "never_built": 5, "up_to_date": 0}
    assert data["attention"] == []
    schemas = [layer["schema"] for layer in data["layers"]]
    assert schemas == ["landing", "bronze", "silver", "gold"]
    assert data["layers"][0]["models"][0]["status"] == "source"


def test_home_after_build(client, project):
    client.post("/api/transform", json={"force": True})

    # A late source, a broken contract and an anomaly, written the way the
    # engine writes them.
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    conn.execute(
        "INSERT INTO _havn.source_freshness (id, model_path, source_table, on_column, "
        "max_age_seconds, age_seconds, is_stale, severity, checked_at) VALUES "
        "('f1', 'bronze.orders', 'landing.orders', 'loaded_at', 21600, 93600, true, 'warn', now())"
    )
    from havn.engine.contracts import _ensure_contracts_table

    _ensure_contracts_table(conn)
    conn.execute(
        "INSERT INTO _havn.contract_results (contract_name, model, passed, severity, detail) "
        "VALUES ('report_ok', 'gold.report', false, 'error', ?)",
        [json.dumps([{"expression": "n > 5", "passed": False, "detail": "got 3"}])],
    )
    conn.execute(
        "INSERT INTO _havn.anomaly_log (id, model_name, metric, current_value, mean_value, "
        "stddev_value, z_score, direction, message, detected_at) VALUES "
        "('a1', 'silver.enriched', 'row_count', 3, 50, 5, -9.4, 'low', 'row count 3 vs mean 50', now())"
    )
    conn.close()

    data = client.get("/api/home").json()
    tiles = data["tiles"]
    assert tiles["models"]["up_to_date"] == 3  # bronze, silver, gold.report
    assert tiles["models"]["never_built"] == 2  # gold.broken failed, gold.blocked skipped
    assert tiles["checks"] == {"passed": 1, "failed": 1, "warned": 1, "contracts_failed": 1}
    assert tiles["last_run"]["status"] == "failed"
    assert tiles["warehouse"]["size_bytes"] > 0

    kinds = [(a["kind"], a["severity"], a["subject"]) for a in data["attention"]]
    # Errors first (build, assertion, contract), then warnings.
    assert kinds[:3] == [
        ("build", "error", "gold.broken"),
        ("assertion", "error", "silver.enriched"),
        ("contract", "error", "gold.report"),
    ]
    assert set(kinds[3:]) == {
        ("assertion", "warn", "silver.enriched"),
        ("freshness", "warn", "landing.orders"),
        ("anomaly", "warn", "silver.enriched"),
    }
    failed_check = data["attention"][1]
    assert failed_check["path"] == "transform/silver/enriched.sql"
    assert failed_check["sql"] == 'SELECT * FROM silver.enriched WHERE "customer_id" IS NULL'
    contract = data["attention"][2]
    assert contract["detail"] == "n > 5 got 3"
    fresh = next(a for a in data["attention"] if a["kind"] == "freshness")
    assert fresh["detail"].startswith("last load 26h ago, expected within 6h")

    assert data["runs"] and data["runs"][-1]["status"] == "failed"

    layers = {layer["schema"]: {m["name"]: m["status"] for m in layer["models"]} for layer in data["layers"]}
    assert layers["silver"]["enriched"] == "failing"
    assert layers["gold"]["broken"] == "failing"
    assert layers["gold"]["report"] == "fresh"
    assert layers["gold"]["blocked"] == "blocked"
    # Failing models sort first within a layer.
    assert data["layers"][-1]["models"][0]["status"] == "failing"


def test_home_marks_edited_models_changed(client, project):
    client.post("/api/transform", json={"force": True})
    f = project / "transform" / "gold" / "report.sql"
    f.write_text(f.read_text().replace("COUNT(*) AS n", "COUNT(*) AS n, 1 AS extra"))
    data = client.get("/api/home").json()
    assert data["tiles"]["models"]["changed"] == 1
    gold = next(layer for layer in data["layers"] if layer["schema"] == "gold")
    assert {m["name"]: m["status"] for m in gold["models"]}["report"] == "changed"


def test_home_ignores_results_for_removed_checks(client, project):
    client.post("/api/transform", json={"force": True})
    f = project / "transform" / "silver" / "enriched.sql"
    f.write_text(f.read_text().replace("@assert no_nulls(customer_id)\n", ""))
    data = client.get("/api/home").json()
    assert not any(
        a["kind"] == "assertion" and "no_nulls" in a["detail"] for a in data["attention"]
    )
