"""Tests for the FastAPI backend."""

import json
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def project(tmp_path):
    """Create a minimal test project."""
    (tmp_path / "project.yml").write_text("""
name: test
database:
  path: warehouse.duckdb
streams:
  test-stream:
    description: "Test"
    steps:
      - transform: [all]
""")
    # Create dirs
    (tmp_path / "transform" / "bronze").mkdir(parents=True)
    (tmp_path / "ingest").mkdir()
    (tmp_path / "export").mkdir()

    # Create a model
    (tmp_path / "transform" / "bronze" / "test.sql").write_text(
        "-- config: materialized=view, schema=bronze\n\n"
        "SELECT 1 AS id, 'hello' AS msg\n"
    )

    # Create warehouse with some data
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
    conn.execute("CREATE TABLE landing.data AS SELECT 1 AS x")
    conn.close()

    return tmp_path


@pytest.fixture
def client(project):
    import havn.server.app as server_app

    server_app.PROJECT_DIR = project
    return TestClient(server_app.app)


def test_list_files(client):
    resp = client.get("/api/files")
    assert resp.status_code == 200
    data = resp.json()
    names = [f["name"] for f in data]
    assert "transform" in names
    assert "project.yml" in names


def test_list_models(client):
    resp = client.get("/api/models")
    assert resp.status_code == 200
    models = resp.json()
    assert len(models) == 1
    assert models[0]["full_name"] == "bronze.test"


def test_get_dag(client):
    resp = client.get("/api/dag")
    assert resp.status_code == 200
    dag = resp.json()
    assert "nodes" in dag
    assert "edges" in dag
    assert any(n["id"] == "bronze.test" for n in dag["nodes"])


def test_run_query(client):
    resp = client.post("/api/query", json={"sql": "SELECT 42 AS answer"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["columns"] == ["answer"]
    assert data["rows"] == [[42]]


def test_run_query_invalid_sql(client):
    resp = client.post("/api/query", json={"sql": "INVALID SQL FOOBAR"})
    assert resp.status_code == 400


def test_run_query_empty_rejected(client):
    resp = client.post("/api/query", json={"sql": ""})
    assert resp.status_code == 422  # pydantic min_length=1


def test_run_query_with_params(client):
    resp = client.post(
        "/api/query",
        json={"sql": "SELECT $a + 1 AS x, $name AS n", "params": {"a": 41, "name": "duck"}},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["columns"] == ["x", "n"]
    assert data["rows"] == [[42, "duck"]]


def test_run_query_with_params_and_limit(client):
    resp = client.post(
        "/api/query",
        json={
            "sql": "SELECT * FROM range(10) WHERE range < $cap",
            "params": {"cap": 5},
            "limit": 3,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["rows"]) == 3
    assert data["truncated"] is True


def test_run_query_missing_param_is_400(client):
    resp = client.post("/api/query", json={"sql": "SELECT $missing AS x"})
    assert resp.status_code == 400


def test_run_query_trailing_line_comment_with_limit(client):
    resp = client.post(
        "/api/query",
        json={"sql": "SELECT 1 AS x -- trailing comment", "limit": 10},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["rows"] == [[1]]


def test_explain_with_params(client):
    resp = client.post(
        "/api/query/explain",
        json={"sql": "SELECT $a::INT AS x", "params": {"a": 1}},
    )
    assert resp.status_code == 200, resp.text
    assert "plan" in resp.json()


def test_export_csv_with_params(client):
    resp = client.post(
        "/api/query/export-csv",
        json={"sql": "SELECT $a AS x", "params": {"a": 7}},
    )
    assert resp.status_code == 200, resp.text
    assert "7" in resp.text


def test_transform_accepts_empty_body(client):
    """POST /api/transform with no body must run all models, not 422."""
    resp = client.post("/api/transform")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "results" in data


def test_transform_accepts_empty_json(client):
    """POST /api/transform with empty {} body must run all models."""
    resp = client.post("/api/transform", json={})
    assert resp.status_code == 200, resp.text


def test_unknown_api_endpoint_returns_404(client):
    """Unknown /api/* paths must 404, not be masked by the SPA shell."""
    resp = client.get("/api/does-not-exist")
    assert resp.status_code == 404
    # And the body must not be the SPA index.html.
    assert "<!doctype" not in resp.text.lower()
    assert "<html" not in resp.text.lower()


def test_list_tables(client):
    resp = client.get("/api/tables")
    assert resp.status_code == 200
    tables = resp.json()
    assert any(t["name"] == "data" and t["schema"] == "landing" for t in tables)


def test_list_tables_with_schema_filter(client):
    resp = client.get("/api/tables?schema=landing")
    assert resp.status_code == 200
    tables = resp.json()
    assert all(t["schema"] == "landing" for t in tables)

    resp = client.get("/api/tables?schema=nonexistent")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_streams(client):
    resp = client.get("/api/streams")
    assert resp.status_code == 200
    data = resp.json()
    assert "test-stream" in data


def test_docs_endpoint(client):
    resp = client.get("/api/docs/markdown")
    assert resp.status_code == 200
    data = resp.json()
    assert "markdown" in data
    assert "landing.data" in data["markdown"]


def test_scheduler_endpoint(client):
    resp = client.get("/api/scheduler")
    assert resp.status_code == 200
    data = resp.json()
    assert "scheduled_streams" in data


def test_overview_endpoint(client):
    resp = client.get("/api/overview")
    assert resp.status_code == 200
    data = resp.json()
    # Should have all expected keys
    assert "recent_runs" in data
    assert "schemas" in data
    assert "total_tables" in data
    assert "total_rows" in data
    assert "connectors" in data
    assert "has_data" in data
    assert "streams" in data
    # The test project has a landing.data table
    assert data["has_data"] is True
    assert data["total_tables"] >= 1
    # Should have a landing schema
    schema_names = [s["name"] for s in data["schemas"]]
    assert "landing" in schema_names
    # Should include streams from project.yml
    assert "test-stream" in data["streams"]


# --- Tests for warehouse existence handling ---


@pytest.fixture
def no_warehouse_project(tmp_path):
    """Create a project with no warehouse database."""
    (tmp_path / "project.yml").write_text("""
name: test
database:
  path: warehouse.duckdb
streams:
  test-stream:
    description: "Test"
    steps:
      - transform: [all]
""")
    (tmp_path / "transform" / "bronze").mkdir(parents=True)
    (tmp_path / "ingest").mkdir()
    (tmp_path / "export").mkdir()
    return tmp_path


@pytest.fixture
def no_db_client(no_warehouse_project):
    import havn.server.app as server_app
    from havn.server.deps import reset_shared_conn
    reset_shared_conn()
    server_app.PROJECT_DIR = no_warehouse_project
    yield TestClient(server_app.app)
    reset_shared_conn()


def test_query_no_warehouse(no_db_client):
    resp = no_db_client.post("/api/query", json={"sql": "SELECT 1"})
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_describe_table_no_warehouse(no_db_client):
    resp = no_db_client.get("/api/tables/landing/data")
    assert resp.status_code == 404


def test_sample_table_no_warehouse(no_db_client):
    resp = no_db_client.get("/api/tables/landing/data/sample")
    assert resp.status_code == 404


def test_profile_table_no_warehouse(no_db_client):
    resp = no_db_client.get("/api/tables/landing/data/profile")
    assert resp.status_code == 404


# --- Tests for upload path safety ---


def test_upload_rejects_path_traversal(client):
    import io
    # Simulate a file upload with path traversal name
    resp = client.post(
        "/api/upload",
        files={"file": ("../../../etc/passwd", io.BytesIO(b"evil"), "text/plain")},
    )
    # Should either reject the name or strip the path components
    if resp.status_code == 200:
        data = resp.json()
        assert ".." not in data["name"]
        assert "/" not in data["name"]
    else:
        assert resp.status_code == 400


def test_upload_rejects_dotfile(client):
    import io
    resp = client.post(
        "/api/upload",
        files={"file": (".env", io.BytesIO(b"SECRET=x"), "text/plain")},
    )
    assert resp.status_code == 400


# --- Notebook API endpoint tests ---


def test_run_sql_cell_endpoint(client):
    """Run a SQL cell via the API."""
    resp = client.post(
        "/api/notebooks/run-cell/test_nb",
        json={"source": "SELECT 42 AS answer", "cell_type": "sql"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["outputs"]) == 1
    assert data["outputs"][0]["type"] == "table"
    assert data["outputs"][0]["rows"] == [[42]]
    assert "duration_ms" in data


def test_run_code_cell_endpoint(client):
    """Run a Python code cell via the API."""
    resp = client.post(
        "/api/notebooks/run-cell/test_nb",
        json={"source": "1 + 1", "cell_type": "code"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["outputs"]) == 1
    assert "2" in data["outputs"][0]["text"]


def test_run_cell_namespace_persistence(client):
    """Variables persist across code cells in the same notebook."""
    # Set a variable
    resp1 = client.post(
        "/api/notebooks/run-cell/ns_test",
        json={"source": "x = 42", "cell_type": "code", "reset": True},
    )
    assert resp1.status_code == 200

    # Read it back
    resp2 = client.post(
        "/api/notebooks/run-cell/ns_test",
        json={"source": "x", "cell_type": "code"},
    )
    assert resp2.status_code == 200
    assert "42" in resp2.json()["outputs"][0]["text"]


def test_run_cell_reset_namespace(client):
    """Reset flag clears the namespace."""
    # Set a variable
    client.post(
        "/api/notebooks/run-cell/reset_test",
        json={"source": "y = 99", "cell_type": "code"},
    )

    # Reset and try to read
    resp = client.post(
        "/api/notebooks/run-cell/reset_test",
        json={"source": "y", "cell_type": "code", "reset": True},
    )
    assert resp.status_code == 200
    assert any(o["type"] == "error" for o in resp.json()["outputs"])


def test_run_sql_cell_error_endpoint(client):
    """SQL cell errors are returned, not raised as HTTP errors."""
    resp = client.post(
        "/api/notebooks/run-cell/test_nb",
        json={"source": "SELECT * FROM nonexistent_xyzzy", "cell_type": "sql"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert any(o["type"] == "error" for o in data["outputs"])


def test_promote_to_model_endpoint(client, project):
    """Promote SQL to model via the API."""
    resp = client.post(
        "/api/notebooks/promote-to-model",
        json={
            "sql_source": "SELECT * FROM landing.data",
            "model_name": "clean_data",
            "target_schema": "bronze",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "created"
    assert data["full_name"] == "bronze.clean_data"
    assert "path" in data

    # Verify the file was created
    model_file = project / data["path"]
    assert model_file.exists()
    content = model_file.read_text()
    assert "SELECT * FROM landing.data" in content
    assert "@config" in content


def test_promote_to_model_conflict(client, project):
    """Promote returns 409 when model already exists."""
    # Create first
    client.post(
        "/api/notebooks/promote-to-model",
        json={
            "sql_source": "SELECT 1",
            "model_name": "conflict_model",
            "target_schema": "bronze",
        },
    )
    # Try again — should get 409
    resp = client.post(
        "/api/notebooks/promote-to-model",
        json={
            "sql_source": "SELECT 2",
            "model_name": "conflict_model",
            "target_schema": "bronze",
        },
    )
    assert resp.status_code == 409

    # With overwrite — should succeed
    resp = client.post(
        "/api/notebooks/promote-to-model",
        json={
            "sql_source": "SELECT 2",
            "model_name": "conflict_model",
            "target_schema": "bronze",
            "overwrite": True,
        },
    )
    assert resp.status_code == 200


def test_promote_validates_identifiers(client):
    """Promote rejects invalid model names and schemas."""
    resp = client.post(
        "/api/notebooks/promote-to-model",
        json={
            "sql_source": "SELECT 1",
            "model_name": "DROP TABLE users--",
            "target_schema": "bronze",
        },
    )
    assert resp.status_code == 422  # Pydantic pattern validation


def test_model_to_notebook_endpoint(client, project):
    """Create notebook from model via the API."""
    resp = client.post("/api/notebooks/model-to-notebook/bronze.test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "created"
    assert "notebook" in data
    assert data["notebook"]["title"] == "Debug: bronze.test"


def test_model_to_notebook_not_found(client):
    """Model-to-notebook returns 404 for nonexistent model."""
    resp = client.post("/api/notebooks/model-to-notebook/nonexistent.model")
    assert resp.status_code == 404


def test_debug_notebook_endpoint(client, project):
    """Generate debug notebook via the API."""
    resp = client.post("/api/notebooks/debug/bronze.test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "created"
    assert "notebook" in data
    assert "Debug" in data["notebook"]["title"]


def test_debug_notebook_not_found(client):
    """Debug notebook returns 404 for nonexistent model."""
    resp = client.post("/api/notebooks/debug/nonexistent.model")
    assert resp.status_code == 404


def test_list_notebooks(client, project):
    """List notebooks endpoint."""
    # Create a notebook
    (project / "notebooks").mkdir(exist_ok=True)
    import json
    nb = {"title": "Test NB", "cells": []}
    (project / "notebooks" / "test.dpnb").write_text(json.dumps(nb))

    resp = client.get("/api/notebooks")
    assert resp.status_code == 200
    data = resp.json()
    assert any(n["name"] == "test" for n in data)


# --- Notebook path traversal tests ---


def test_resolve_notebook_rejects_path_traversal(project):
    """_resolve_notebook rejects paths that escape the project directory."""
    from fastapi import HTTPException
    import havn.server.app as server_app

    with pytest.raises(HTTPException) as exc_info:
        server_app._resolve_notebook(project, "../../../etc/passwd")
    assert exc_info.value.status_code == 400

    with pytest.raises(HTTPException) as exc_info:
        server_app._resolve_notebook(project, "notebooks/../../../etc/passwd.dpnb")
    assert exc_info.value.status_code == 400


def test_run_cell_ingest_rejects_injection(client):
    """Ingest cell via API rejects SQL injection in identifiers."""
    import json as _json
    resp = client.post(
        "/api/notebooks/run-cell/test_nb",
        json={
            "source": _json.dumps({
                "source_type": "csv",
                "source_path": "/data/test.csv",
                "target_schema": "landing; DROP TABLE--",
                "target_table": "data",
            }),
            "cell_type": "ingest",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert any(o["type"] == "error" for o in data["outputs"])
    assert any("Invalid" in o.get("text", "") for o in data["outputs"])


# --- Validate endpoint tests ---


def test_validate_endpoint_passes(client):
    """POST /api/validate returns passed for valid models."""
    resp = client.post("/api/validate")
    assert resp.status_code == 200
    data = resp.json()
    assert "models_checked" in data
    assert "errors" in data
    assert "passed" in data
    assert data["passed"] is True


def test_validate_endpoint_catches_landing_schema(client, project):
    """POST /api/validate catches models writing to landing schema."""
    (project / "transform" / "landing").mkdir(parents=True, exist_ok=True)
    (project / "transform" / "landing" / "bad.sql").write_text(
        "-- config: materialized=table, schema=landing\n\nSELECT 1 AS id\n"
    )
    resp = client.post("/api/validate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["passed"] is False
    landing_errors = [e for e in data["errors"] if "landing" in e["message"] and e["severity"] == "error"]
    assert len(landing_errors) >= 1


def test_run_query_offset_without_limit(client):
    """Regression: offset with no limit (and no LIMIT in the SQL) was
    silently dropped, re-serving page 1 forever."""
    resp = client.post(
        "/api/query",
        json={"sql": "SELECT * FROM range(10) ORDER BY 1", "offset": 5},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [r[0] for r in data["rows"]] == [5, 6, 7, 8, 9]
    assert data["row_count"] == 5


def test_run_query_offset_with_inner_limit(client):
    resp = client.post(
        "/api/query",
        json={"sql": "SELECT * FROM range(10) ORDER BY 1 LIMIT 8", "offset": 5},
    )
    assert resp.status_code == 200, resp.text
    assert [r[0] for r in resp.json()["rows"]] == [5, 6, 7]


# --- Editor intelligence: /api/bind and /api/sql/ctes ------------------------


@pytest.fixture
def bind_project(project):
    """The base project plus a silver model over the landing table."""
    (project / "transform" / "silver").mkdir(parents=True, exist_ok=True)
    (project / "transform" / "gold").mkdir(parents=True, exist_ok=True)
    (project / "transform" / "silver" / "orders.sql").write_text(
        "@config materialized=table, schema=silver\n\n"
        "SELECT order_id, amount FROM landing.orders\n"
    )
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
    conn.execute("CREATE TABLE landing.orders (order_id INTEGER, amount DOUBLE)")
    conn.close()
    return project


@pytest.fixture
def bind_client(bind_project):
    import havn.server.app as server_app

    server_app.PROJECT_DIR = bind_project
    return TestClient(server_app.app)


@pytest.fixture
def viewer(monkeypatch):
    """Make every request authenticate as a viewer (read permission only)."""
    import havn.server.deps as deps

    monkeypatch.setattr(
        deps,
        "_get_user",
        lambda request: {"username": "v", "role": "viewer", "display_name": "V"},
    )


def test_bind_endpoint_returns_schema_and_upstream(bind_client):
    resp = bind_client.post("/api/bind", json={
        "path": "transform/gold/summary.sql",
        "content": (
            "@config materialized=table, schema=gold\n\n"
            "SELECT order_id, amount * 2 AS doubled FROM silver.orders\n"
        ),
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["model"] == "gold.summary"
    assert data["ok"] is True
    assert data["errors"] == []
    assert data["columns"] == [
        {"name": "order_id", "type": "INTEGER"},
        {"name": "doubled", "type": "DOUBLE"},
    ]
    assert data["upstream"]["silver.orders"] == [
        {"name": "order_id", "type": "INTEGER"},
        {"name": "amount", "type": "DOUBLE"},
    ]
    assert isinstance(data["duration_ms"], int)


def test_bind_endpoint_reports_a_positioned_bind_error(bind_client):
    resp = bind_client.post("/api/bind", json={
        "path": "transform/gold/summary.sql",
        "content": (
            "@config materialized=table, schema=gold\n"
            "\n"
            "SELECT\n"
            "  no_such_column\n"
            "FROM silver.orders\n"
        ),
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is False
    bind_errors = [e for e in data["errors"] if e["source"] == "bind"]
    assert len(bind_errors) == 1
    err = bind_errors[0]
    assert err["severity"] == "error"
    assert err["message"].startswith("bind error: ")
    assert (err["line"], err["col"]) == (4, 3)
    assert (err["end_line"], err["end_col"]) == (4, 3 + len("no_such_column"))


def test_bind_endpoint_reports_name_level_findings_with_source_validate(bind_client):
    resp = bind_client.post("/api/bind", json={
        "path": "transform/gold/summary.sql",
        "content": (
            "@config materialized=table, schema=gold, nonsense=1\n\n"
            "SELECT order_id FROM silver.orders\n"
        ),
    })
    assert resp.status_code == 200, resp.text
    assert "validate" in {e["source"] for e in resp.json()["errors"]}


def test_bind_endpoint_returns_landing_table_columns(bind_client):
    """A base table is not a model, so it comes from the catalog."""
    resp = bind_client.post("/api/bind", json={
        "path": "transform/silver/other.sql",
        "content": "@config schema=silver\n\nSELECT order_id FROM landing.orders\n",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["upstream"]["landing.orders"] == [
        {"name": "order_id", "type": "INTEGER"},
        {"name": "amount", "type": "DOUBLE"},
    ]


def test_bind_endpoint_accepts_a_pathless_buffer(bind_client):
    resp = bind_client.post("/api/bind", json={
        "path": None,
        "content": "SELECT order_id FROM silver.orders\n",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["model"] is None
    assert data["columns"] == [{"name": "order_id", "type": "INTEGER"}]


def test_bind_endpoint_rejects_a_path_outside_the_project(bind_client):
    resp = bind_client.post(
        "/api/bind", json={"path": "../escape.sql", "content": "SELECT 1\n"}
    )
    assert resp.status_code == 400


def test_bind_endpoint_allows_a_viewer(bind_client, viewer):
    resp = bind_client.post("/api/bind", json={
        "path": "transform/gold/summary.sql",
        "content": "@config schema=gold\n\nSELECT order_id FROM silver.orders\n",
    })
    assert resp.status_code == 200, resp.text


# --- /api/bind is not a way to run SQL ---------------------------------------
#
# The contract for a buffer that fails the read-only validator is HTTP 200
# with ``ok: false`` and exactly one ``source: "bind"`` error carrying the
# validator's reason and a null line. That is what the editor already renders
# as a whole-file diagnostic, and it keeps a rejected buffer from looking like
# a transport failure. The buffer is never bound, and never executed.


def _rejected(resp) -> dict:
    """Assert a bind response is a validator rejection and return its error."""
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is False, data
    assert data["columns"] == []
    bind_errors = [e for e in data["errors"] if e["source"] == "bind"]
    assert len(bind_errors) == 1, data["errors"]
    assert bind_errors[0]["severity"] == "error"
    assert bind_errors[0]["line"] is None
    return bind_errors[0]


def test_bind_endpoint_refuses_a_second_statement(bind_client, viewer, bind_project):
    """A trailing CREATE TABLE must not reach the warehouse."""
    resp = bind_client.post("/api/bind", json={
        "path": "transform/gold/summary.sql",
        "content": (
            "@config schema=gold\n\n"
            "SELECT 1 AS a; CREATE TABLE warehouse.landing.pwned AS SELECT 99\n"
        ),
    })
    err = _rejected(resp)
    assert "Multi-statement" in err["message"]

    conn = duckdb.connect(str(bind_project / "warehouse.duckdb"))
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE lower(table_schema) = 'landing'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert "pwned" not in names


def test_bind_endpoint_refuses_copy_to_a_server_path(
    bind_client, viewer, tmp_path
):
    """COPY ... TO would write warehouse rows to disk, bypassing masking."""
    target = tmp_path / "exfiltrated.csv"
    resp = bind_client.post("/api/bind", json={
        "path": "transform/gold/summary.sql",
        "content": (
            "@config schema=gold\n\n"
            f"COPY (SELECT * FROM silver.orders) TO '{target}'\n"
        ),
    })
    err = _rejected(resp)
    assert "Only SELECT queries" in err["message"]
    assert not target.exists()


def test_bind_endpoint_refuses_reading_a_server_file(bind_client, viewer):
    """read_csv leaked /etc/passwd's first line as column names."""
    resp = bind_client.post("/api/bind", json={
        "path": "transform/gold/summary.sql",
        "content": "@config schema=gold\n\nSELECT * FROM read_csv('/etc/passwd')\n",
    })
    err = _rejected(resp)
    assert "File-access functions" in err["message"]


@pytest.mark.parametrize(
    "label,sql",
    [
        ("read_text", "SELECT read_text('/etc/passwd') AS x"),
        ("glob", "SELECT * FROM glob('/*')"),
        ("replacement scan", "SELECT * FROM '/etc/passwd'"),
        ("quoted path", 'SELECT * FROM "/etc/passwd"'),
    ],
)
def test_bind_endpoint_never_returns_file_contents(bind_client, viewer, label, sql):
    resp = bind_client.post("/api/bind", json={
        "path": "transform/gold/summary.sql",
        "content": f"@config schema=gold\n\n{sql}\n",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is False, (label, data)
    blob = json.dumps(data)
    assert "root:" not in blob, (label, blob)
    assert "/bin/bash" not in blob, (label, blob)
    assert data["columns"] == [], (label, data["columns"])


def test_bind_endpoint_refuses_attach(bind_client, viewer, tmp_path):
    """ATTACH + CREATE TABLE wrote a whole database to a server path."""
    target = tmp_path / "dump.db"
    resp = bind_client.post("/api/bind", json={
        "path": "transform/gold/summary.sql",
        "content": (
            "@config schema=gold\n\n"
            f"ATTACH '{target}' AS ex; CREATE TABLE ex.dump AS SELECT * FROM silver.orders\n"
        ),
    })
    _rejected(resp)
    assert not target.exists()


def test_two_concurrent_binds_do_not_interfere(bind_client):
    """Each request owns its shadow, so parallel binds cannot collide."""
    import threading

    payloads = [
        (
            "transform/gold/a.sql",
            "@config schema=gold\n\nSELECT order_id FROM silver.orders\n",
            [{"name": "order_id", "type": "INTEGER"}],
        ),
        (
            "transform/gold/b.sql",
            "@config schema=gold\n\nSELECT amount FROM silver.orders\n",
            [{"name": "amount", "type": "DOUBLE"}],
        ),
    ]
    results: list = []
    lock = threading.Lock()

    def run(path, content, expected):
        resp = bind_client.post("/api/bind", json={"path": path, "content": content})
        with lock:
            results.append((path, resp.status_code, resp.json(), expected))

    threads = [
        threading.Thread(target=run, args=payloads[i % 2])
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    for path, status, data, expected in results:
        assert status == 200, (path, data)
        assert data["ok"] is True, (path, data["errors"])
        assert data["columns"] == expected, (path, data["columns"])


def test_ctes_endpoint_lists_and_previews(bind_client):
    resp = bind_client.post("/api/sql/ctes", json={
        "content": (
            "WITH base AS (\n"
            "    SELECT order_id FROM silver.orders\n"
            "),\n"
            "agg AS (\n"
            "    SELECT COUNT(*) AS n FROM base\n"
            ")\n"
            "SELECT * FROM agg\n"
        ),
        "line": 5,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [c["name"] for c in data["ctes"]] == ["base", "agg"]
    assert data["active"] == 1
    assert data["ctes"][0]["start_line"] == 1
    assert data["ctes"][0]["end_line"] == 3
    assert data["ctes"][0]["preview_sql"].endswith("SELECT * FROM base")
    assert "LIMIT" not in data["ctes"][1]["preview_sql"]


def test_ctes_endpoint_refuses_recursive(bind_client):
    resp = bind_client.post("/api/sql/ctes", json={
        "content": "WITH RECURSIVE t AS (SELECT 1 AS n) SELECT * FROM t",
        "line": None,
    })
    assert resp.status_code == 400
    assert "recursive" in resp.json()["detail"].lower()


def test_ctes_endpoint_on_a_buffer_without_ctes(bind_client):
    resp = bind_client.post(
        "/api/sql/ctes", json={"content": "@config schema=gold\n\nSELECT 1 AS x\n", "line": 3}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ctes": [], "active": None}


def test_model_columns_endpoint(bind_client, bind_project):
    from havn.engine.database import ensure_meta_table
    from havn.engine.transform import run_transform

    conn = duckdb.connect(str(bind_project / "warehouse.duckdb"))
    ensure_meta_table(conn)
    run_transform(conn, bind_project / "transform", project_dir=bind_project)
    conn.close()

    resp = bind_client.get("/api/models/silver.orders/columns")
    assert resp.status_code == 200, resp.text
    assert resp.json()["columns"] == [
        {"name": "order_id", "type": "INTEGER"},
        {"name": "amount", "type": "DOUBLE"},
    ]


def test_lint_file_check_allows_a_viewer(bind_client, viewer):
    resp = bind_client.post("/api/lint/file", json={
        "path": "transform/silver/orders.sql",
        "fix": False,
        "content": "SELECT 1 AS x\n",
    })
    assert resp.status_code == 200, resp.text


def test_lint_file_check_returns_no_file_contents(bind_client, viewer):
    """A check is a diagnostics poll, not a file read.

    `final_content` is the whole file, and the endpoint dropped to read
    permission, so returning it handed a viewer any .sql file in the project.
    """
    resp = bind_client.post("/api/lint/file", json={
        "path": "transform/silver/orders.sql",
        "fix": False,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] is None


def test_lint_file_refuses_a_sibling_directory(bind_client, viewer, bind_project):
    """`/proj-backup` starts with `/proj`, so a prefix test let it through."""
    sibling = bind_project.parent / f"{bind_project.name}-backup"
    (sibling / "transform").mkdir(parents=True)
    secret = sibling / "transform" / "secret.sql"
    secret.write_text("SELECT 'classified' AS leaked\n")

    resp = bind_client.post("/api/lint/file", json={
        "path": f"../{sibling.name}/transform/secret.sql",
        "fix": False,
    })
    assert resp.status_code in (400, 404), resp.text
    assert "classified" not in resp.text


def test_bind_refuses_a_sibling_directory(bind_client, viewer, bind_project):
    sibling = bind_project.parent / f"{bind_project.name}-backup"
    sibling.mkdir(exist_ok=True)
    resp = bind_client.post("/api/bind", json={
        "path": f"../{sibling.name}/transform/x.sql",
        "content": "SELECT 1 AS a\n",
    })
    assert resp.status_code in (400, 404), resp.text


def test_prebuild_gate_runs_after_ingest_and_blocks_the_transform(tmp_path):
    """A run with ingest used to skip the gate entirely.

    The landing tables do not exist before ingest, which is why the gate was
    skipped, so it now runs between ingest and the first transform instead.
    """
    import havn.server.app as server_app
    from havn.server.routes.pipeline import (
        _pipeline_state,
        _run_selective_pipeline_thread,
    )

    (tmp_path / "project.yml").write_text(
        "name: gate\ndatabase:\n  path: warehouse.duckdb\n"
    )
    (tmp_path / "ingest").mkdir()
    (tmp_path / "transform" / "silver").mkdir(parents=True)
    (tmp_path / "ingest" / "load.py").write_text(
        "db.execute('CREATE SCHEMA IF NOT EXISTS landing')\n"
        "db.execute(\"CREATE OR REPLACE TABLE landing.orders AS "
        "SELECT 1 AS order_id, 2.0 AS amount\")\n"
    )
    (tmp_path / "transform" / "silver" / "bad.sql").write_text(
        "@config materialized=table, schema=silver\n"
        "\n"
        "SELECT\n"
        "  no_such_column\n"
        "FROM landing.orders\n"
    )
    server_app.PROJECT_DIR = tmp_path
    _pipeline_state["events"] = []

    _run_selective_pipeline_thread(
        ["ingest", "transform"], False, tmp_path, {"username": "local"}
    )
    events = list(_pipeline_state["events"])
    kinds = [e["event"] for e in events]

    # Ingest ran first, so the gate had real landing tables to bind against.
    ingest_end = next(
        i for i, e in enumerate(events)
        if e["event"] == "model_end" and e["data"]["action"] == "ingest"
    )
    gate = next(i for i, e in enumerate(events) if e["event"] == "validation")
    assert ingest_end < gate
    assert events[gate]["data"]["severity"] == "error"
    assert events[gate]["data"]["message"].startswith("bind error: ")
    assert events[gate]["data"]["line"] == 4

    # The transform never started, and the run reports failure.
    assert not any(
        e["event"] == "model_start" and e["data"]["action"] == "transform"
        for e in events
    )
    assert kinds[-1] == "complete"
    assert events[-1]["data"]["status"] == "failed"

    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    built = conn.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'silver'"
    ).fetchone()[0]
    conn.close()
    assert built == 0


def test_lint_file_fix_denies_a_viewer(bind_client, viewer):
    resp = bind_client.post("/api/lint/file", json={
        "path": "transform/silver/orders.sql",
        "fix": True,
        "content": "SELECT 1 AS x\n",
    })
    assert resp.status_code == 403
