"""Python models through the API: listing, DAG, create, preview, delete."""

from __future__ import annotations

import duckdb
import pytest
from fastapi.testclient import TestClient

SCORES = '''"""Score per customer."""
from havn import model


@model(materialized="table", tags=["daily"])
def scores(db, ref):
    print("previewing")
    return ref("bronze.orders").aggregate("customer, sum(amount) AS score")
'''


@pytest.fixture
def project(tmp_path):
    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: warehouse.duckdb\n")
    (tmp_path / "transform" / "bronze").mkdir(parents=True)
    (tmp_path / "transform" / "silver").mkdir(parents=True)
    (tmp_path / "transform" / "bronze" / "orders.sql").write_text(
        "@config materialized=table\nSELECT * FROM landing.orders\n"
    )
    (tmp_path / "transform" / "silver" / "scores.py").write_text(SCORES)
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA landing")
    conn.execute(
        "CREATE TABLE landing.orders AS SELECT * FROM (VALUES "
        "(1, 'a', 10.0), (2, 'b', 5.0), (3, 'a', 1.0)) t(id, customer, amount)"
    )
    conn.close()
    return tmp_path


@pytest.fixture
def client(project):
    import havn.server.app as server_app

    server_app.PROJECT_DIR = project
    return TestClient(server_app.app)


def test_models_and_dag_carry_language(client):
    models = {m["full_name"]: m for m in client.get("/api/models").json()}
    assert models["silver.scores"]["language"] == "python"
    assert models["silver.scores"]["path"] == "transform/silver/scores.py"
    assert models["bronze.orders"]["language"] == "sql"
    nodes = {n["id"]: n for n in client.get("/api/dag").json()["nodes"]}
    assert nodes["silver.scores"]["language"] == "python"
    edges = client.get("/api/dag").json()["edges"]
    assert {"source": "bronze.orders", "target": "silver.scores"} in edges
    picked = client.get("/api/models", params={"select": "config.language:python"}).json()
    assert [m["full_name"] for m in picked] == ["silver.scores"]


def test_transform_endpoint_builds_python_model(client, project):
    r = client.post("/api/transform", json={"targets": ["+silver.scores"]})
    assert r.status_code == 200, r.text
    q = client.post("/api/query", json={"sql": "SELECT count(*) AS n FROM silver.scores"})
    assert q.json()["rows"] == [[2]]


def test_workbench_for_python_model(client):
    client.post("/api/transform", json={"targets": ["+silver.scores"]})
    data = client.get("/api/models/workbench", params={"path": "transform/silver/scores.py"}).json()
    assert data["model"] == "silver.scores" and data["language"] == "python"
    assert [c["name"] for c in data["columns"]] == ["customer", "score"]
    assert data["state"]["built"] is True
    assert data["upstream"] == [{"name": "bronze.orders", "path": "transform/bronze/orders.sql"}]


def test_preview_runs_the_buffer(client):
    client.post("/api/transform", json={"targets": ["bronze.orders"]})
    r = client.post(
        "/api/models/preview-python",
        json={"path": "transform/silver/scores.py", "content": SCORES.replace("sum(", "max("), "limit": 1},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["model"] == "silver.scores"
    assert data["columns"] == ["customer", "score"]
    assert len(data["rows"]) == 1 and data["truncated"] is True
    assert data["output"] == "previewing\n"


def test_preview_reports_errors_and_refuses_bad_paths(client):
    bad = client.post(
        "/api/models/preview-python",
        json={"path": "transform/silver/scores.py", "content": "def model(db):\n    return 1 / 0\n"},
    )
    assert bad.status_code == 400 and "ZeroDivisionError" in bad.json()["detail"]
    assert "scores.py:2" in bad.json()["detail"]
    outside = client.post("/api/models/preview-python", json={"path": "ingest/x.py", "content": "x = 1"})
    assert outside.status_code == 400
    helper = client.post(
        "/api/models/preview-python", json={"path": "transform/silver/util.py", "content": "X = 1\n"}
    )
    assert helper.status_code == 400 and "No model function" in helper.json()["detail"]


def test_create_python_model(client, project):
    r = client.post(
        "/api/models/create",
        json={"name": "fresh", "schema_name": "gold", "materialized": "incremental", "language": "python"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["path"] == "transform/gold/fresh.py"
    text = (project / "transform" / "gold" / "fresh.py").read_text(encoding="utf-8")
    assert '@model(materialized="incremental", unique_key="id")' in text
    models = {m["full_name"]: m for m in client.get("/api/models").json()}
    assert models["gold.fresh"]["language"] == "python"
    # The starter template builds, twice (the second run is incremental).
    for _ in range(2):
        assert client.post("/api/transform", json={"targets": ["gold.fresh"]}).status_code == 200
    # One name, one model: neither spelling may be created again.
    dup = client.post("/api/models/create", json={"name": "fresh", "schema_name": "gold"})
    assert dup.status_code == 409
    view = client.post(
        "/api/models/create",
        json={"name": "v", "schema_name": "gold", "materialized": "view", "language": "python"},
    )
    assert view.status_code == 400


def test_delete_python_model_drops_its_table(client, project):
    client.post("/api/transform", json={"targets": ["+silver.scores"]})
    r = client.request("DELETE", "/api/files/transform/silver/scores.py", params={"drop_object": "true"})
    assert r.status_code == 200, r.text
    assert r.json().get("dropped") == "silver.scores"


def test_explain_refuses_python_model(client):
    r = client.get("/api/models/silver.scores/explain")
    assert r.status_code == 400 and "Python model" in r.json()["detail"]


def test_validate_endpoint_reports_python_errors(client, project):
    (project / "transform" / "silver" / "bad.py").write_text(
        "from havn import model\n@model(materialized='view')\ndef bad(db):\n    pass\n"
    )
    data = client.post("/api/validate").json()
    text = str(data)
    assert "cannot be materialized as view" in text
