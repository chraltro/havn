"""/api/assertions reports the current state of the checks, not the raw history."""
from __future__ import annotations

import duckdb
import pytest
from fastapi.testclient import TestClient

ORDERS_V1 = "@config materialized=table, schema=bronze\n@assert row_count > 100\n@assert no_nulls(id)\n\nSELECT * FROM landing.orders\n"
ORDERS_V2 = "@config materialized=table, schema=bronze\n@assert row_count > 0\n@assert no_nulls(id)\n\nSELECT * FROM landing.orders\n"


@pytest.fixture
def project(tmp_path):
    (tmp_path / "project.yml").write_text("name: quay\ndatabase:\n  path: warehouse.duckdb\n")
    (tmp_path / "transform" / "bronze").mkdir(parents=True)
    (tmp_path / "transform" / "bronze" / "orders.sql").write_text(ORDERS_V1)
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA landing")
    conn.execute("CREATE TABLE landing.orders AS SELECT * FROM (VALUES (1), (2)) t(id)")
    conn.close()
    return tmp_path


@pytest.fixture
def client(project):
    import havn.server.app as server_app
    from havn.server.deps import reset_shared_conn

    reset_shared_conn()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = False
    yield TestClient(server_app.app)
    reset_shared_conn()


def test_counts_each_current_check_once_and_drops_removed_ones(client, project):
    """Two builds of the same checks, then a check is edited away.

    The raw history now holds the no_nulls(id) pass twice and a failure of
    "row_count > 100", a check the model no longer declares. The page's pass
    rate used to be computed over that history, so it showed a failure that
    could not happen any more and counted passing checks twice.
    """
    client.post("/api/transform", json={"force": True})
    client.post("/api/transform", json={"force": True})
    (project / "transform" / "bronze" / "orders.sql").write_text(ORDERS_V2)
    client.post("/api/transform", json={"force": True})

    results = client.get("/api/assertions").json()
    by_expr = {r["expression"]: r for r in results}

    assert sorted(by_expr) == ["no_nulls(id)", "row_count > 0"]
    assert all(r["passed"] for r in results)
    assert all(r["model"] == "bronze.orders" for r in results)
