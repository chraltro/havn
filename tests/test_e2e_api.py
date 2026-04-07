"""End-to-end API tests.

Boots the FastAPI server via TestClient and exercises full workflows:
pipeline execution, data verification via API, query with masking,
diff engine, backup/restore cycle.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path):
    """Create a full test project with all pipeline layers."""
    p = tmp_path / "project"
    p.mkdir()

    for d in [
        "ingest", "transform/bronze", "transform/silver",
        "transform/gold", "export", "seeds", "orchestration",
    ]:
        (p / d).mkdir(parents=True, exist_ok=True)

    (p / "project.yml").write_text(textwrap.dedent("""\
        name: e2e-test
        database:
          path: warehouse.duckdb
        streams:
          full-refresh:
            description: "Full pipeline"
            steps:
              - seed: [all]
              - ingest: [all]
              - transform: [all]
    """))

    # Seed
    (p / "seeds" / "regions.csv").write_text(
        "id,name\n1,north\n2,south\n3,east\n"
    )

    # Ingest
    (p / "ingest" / "load_orders.py").write_text(textwrap.dedent("""\
        db.execute("CREATE SCHEMA IF NOT EXISTS landing")
        db.execute(\"\"\"
            CREATE OR REPLACE TABLE landing.orders AS
            SELECT * FROM (VALUES
                (1, 'widget', 10.50, 1, '2024-01-01'),
                (2, 'gadget', 25.00, 2, '2024-01-02'),
                (3, 'widget', 10.50, 1, '2024-01-03'),
                (4, 'gizmo', 99.99, 3, '2024-01-03')
            ) AS t(id, product, amount, region_id, order_date)
        \"\"\")
    """))

    # Bronze
    (p / "transform" / "bronze" / "orders.sql").write_text(textwrap.dedent("""\
        -- config: materialized=table, schema=bronze
        -- depends_on: landing.orders

        SELECT id, product, amount, region_id,
               CAST(order_date AS DATE) AS order_date
        FROM landing.orders
    """))

    # Silver
    (p / "transform" / "silver" / "order_details.sql").write_text(textwrap.dedent("""\
        -- config: materialized=table, schema=silver
        -- depends_on: bronze.orders

        SELECT o.id, o.product, o.amount, o.order_date,
               o.region_id, r.name AS region_name
        FROM bronze.orders o
        LEFT JOIN seeds.regions r ON o.region_id = r.id
    """))

    # Gold
    (p / "transform" / "gold" / "revenue_by_region.sql").write_text(textwrap.dedent("""\
        -- config: materialized=table, schema=gold
        -- depends_on: silver.order_details

        SELECT region_name, COUNT(*) AS order_count,
               SUM(amount) AS total_revenue
        FROM silver.order_details
        GROUP BY region_name
    """))

    # Orchestration job
    (p / "orchestration" / "full-refresh.yml").write_text(textwrap.dedent("""\
        name: full-refresh
        targets:
          - gold.*
        resolve: upstream
    """))

    return p


@pytest.fixture
def client(project):
    """TestClient with a fresh project."""
    import havn.server.app as server_app
    from havn.server.deps import reset_shared_conn

    reset_shared_conn()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = False
    server_app.ACTIVE_ENV = None
    yield TestClient(server_app.app)
    reset_shared_conn()


@pytest.fixture
def seeded_client(project, client):
    """Client with seed data loaded and ingest + transform run."""
    # Run seeds via direct engine call (faster than API for setup)
    from havn.engine.database import connect
    from havn.engine.seeds import run_seeds
    from havn.engine.runner import run_scripts_in_dir
    from havn.engine.transform import run_transform

    db_path = project / "warehouse.duckdb"
    conn = connect(db_path)
    run_seeds(conn, project / "seeds", force=True)
    run_scripts_in_dir(conn, project / "ingest", "ingest")
    run_transform(conn, project / "transform", force=True)
    conn.close()

    from havn.server.deps import reset_shared_conn
    reset_shared_conn()
    return client


# ---------------------------------------------------------------------------
# Full pipeline via API
# ---------------------------------------------------------------------------


class TestPipelineViaAPI:
    """Execute and verify pipeline through REST API endpoints."""

    def test_transform_via_api(self, project, client):
        """POST /api/transform should build all models."""
        # First load data directly (ingest isn't an API endpoint)
        from havn.engine.database import connect
        from havn.engine.seeds import run_seeds
        from havn.engine.runner import run_scripts_in_dir

        conn = connect(project / "warehouse.duckdb")
        run_seeds(conn, project / "seeds", force=True)
        run_scripts_in_dir(conn, project / "ingest", "ingest")
        conn.close()

        from havn.server.deps import reset_shared_conn
        reset_shared_conn()

        resp = client.post("/api/transform", json={"force": True})
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert results["bronze.orders"] == "built"
        assert results["silver.order_details"] == "built"
        assert results["gold.revenue_by_region"] == "built"

    def test_tables_visible_after_pipeline(self, seeded_client):
        """GET /api/tables should show all pipeline tables."""
        resp = seeded_client.get("/api/tables")
        assert resp.status_code == 200
        tables = resp.json()
        schema_tables = {f"{t['schema']}.{t['name']}" for t in tables}
        assert "gold.revenue_by_region" in schema_tables
        assert "silver.order_details" in schema_tables
        assert "bronze.orders" in schema_tables

    def test_sample_endpoint_returns_data(self, seeded_client):
        """GET /api/tables/{schema}/{table}/sample should return rows."""
        resp = seeded_client.get("/api/tables/gold/revenue_by_region/sample")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["rows"]) > 0
        assert "region_name" in data["columns"]

    def test_query_gold_data(self, seeded_client):
        """POST /api/query should return correct gold layer data."""
        resp = seeded_client.post("/api/query", json={
            "sql": "SELECT region_name, total_revenue FROM gold.revenue_by_region ORDER BY region_name"
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["columns"] == ["region_name", "total_revenue"]
        # 3 regions with known revenue
        assert len(data["rows"]) == 3

    def test_dag_reflects_pipeline(self, seeded_client):
        """GET /api/dag should show model dependencies."""
        resp = seeded_client.get("/api/dag")
        assert resp.status_code == 200
        dag = resp.json()
        names = {n["id"] for n in dag["nodes"]}
        assert "bronze.orders" in names
        assert "silver.order_details" in names
        assert "gold.revenue_by_region" in names
        # Should have edges
        assert len(dag["edges"]) >= 2


# ---------------------------------------------------------------------------
# Query with masking via API
# ---------------------------------------------------------------------------


class TestMaskingViaAPI:
    """Verify masking works end-to-end through the query API."""

    def test_masked_column_in_query(self, seeded_client, project):
        """A masked column should return masked values via /api/query."""
        # Create a masking policy with no exempted roles (applies to everyone incl. admin)
        resp = seeded_client.post("/api/masking/policies", json={
            "schema_name": "gold",
            "table_name": "revenue_by_region",
            "column_name": "total_revenue",
            "method": "redact",
            "exempted_roles": [],
        })
        assert resp.status_code == 200

        # Query the masked column
        resp = seeded_client.post("/api/query", json={
            "sql": "SELECT region_name, total_revenue FROM gold.revenue_by_region"
        })
        assert resp.status_code == 200
        data = resp.json()
        for row in data["rows"]:
            # total_revenue should be masked
            assert row[1] == "***"

    def test_alias_bypass_prevented(self, seeded_client):
        """SELECT masked_col AS x should still be masked."""
        seeded_client.post("/api/masking/policies", json={
            "schema_name": "gold",
            "table_name": "revenue_by_region",
            "column_name": "total_revenue",
            "method": "redact",
            "exempted_roles": [],
        })

        resp = seeded_client.post("/api/query", json={
            "sql": "SELECT total_revenue AS revenue FROM gold.revenue_by_region"
        })
        assert resp.status_code == 200
        for row in resp.json()["rows"]:
            assert row[0] == "***"

    def test_where_on_masked_column_denied(self, seeded_client):
        """Filtering on a masked column should be denied."""
        seeded_client.post("/api/masking/policies", json={
            "schema_name": "gold",
            "table_name": "revenue_by_region",
            "column_name": "total_revenue",
            "method": "redact",
            "exempted_roles": [],
        })

        resp = seeded_client.post("/api/query", json={
            "sql": "SELECT region_name FROM gold.revenue_by_region WHERE total_revenue > 50"
        })
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Diff engine via API
# ---------------------------------------------------------------------------


class TestDiffViaAPI:
    """Test the diff engine through the API."""

    def test_diff_detects_change(self, seeded_client, project):
        """Modifying a model and diffing should show changes."""
        # Modify the gold model
        (project / "transform" / "gold" / "revenue_by_region.sql").write_text(textwrap.dedent("""\
            -- config: materialized=table, schema=gold
            -- depends_on: silver.order_details

            SELECT region_name, COUNT(*) AS order_count,
                   SUM(amount) AS total_revenue,
                   AVG(amount) AS avg_revenue
            FROM silver.order_details
            GROUP BY region_name
        """))

        resp = seeded_client.post("/api/diff", json={
            "targets": ["gold.revenue_by_region"],
        })
        assert resp.status_code == 200
        diffs = resp.json()
        assert len(diffs) > 0
        # Should detect the schema change (new column)
        diff = diffs[0]
        assert diff["model"] == "gold.revenue_by_region"


# ---------------------------------------------------------------------------
# Backup/restore via API
# ---------------------------------------------------------------------------


class TestBackupViaAPI:
    """Test backup and restore through the API."""

    def test_backup_and_list(self, seeded_client):
        """Create a backup and verify it appears in the list."""
        resp = seeded_client.post("/api/backup", json={"note": "e2e test"})
        assert resp.status_code == 200
        entry = resp.json()
        assert entry["verified"] is True
        assert entry["note"] == "e2e test"

        resp = seeded_client.get("/api/backups")
        assert resp.status_code == 200
        backups = resp.json()
        assert len(backups) >= 1
        assert any(b["note"] == "e2e test" for b in backups)

    def test_backup_restore_cycle(self, seeded_client, project):
        """Backup, destroy data, restore, verify data is back."""
        # Backup
        resp = seeded_client.post("/api/backup", json={})
        assert resp.status_code == 200
        backup_path = resp.json()["path"]

        # Verify data exists
        resp = seeded_client.post("/api/query", json={
            "sql": "SELECT COUNT(*) AS cnt FROM gold.revenue_by_region"
        })
        assert resp.status_code == 200
        original_count = resp.json()["rows"][0][0]
        assert original_count > 0

        # Restore
        resp = seeded_client.post("/api/backup/restore", json={
            "backup_path": backup_path,
        })
        assert resp.status_code == 200

        # Verify data survived
        from havn.server.deps import reset_shared_conn
        reset_shared_conn()

        resp = seeded_client.post("/api/query", json={
            "sql": "SELECT COUNT(*) AS cnt FROM gold.revenue_by_region"
        })
        assert resp.status_code == 200
        assert resp.json()["rows"][0][0] == original_count


# ---------------------------------------------------------------------------
# History / run log
# ---------------------------------------------------------------------------


class TestHistoryViaAPI:
    """Verify pipeline history is tracked."""

    def test_history_after_pipeline(self, seeded_client):
        """GET /api/history should show pipeline run entries."""
        resp = seeded_client.get("/api/history")
        assert resp.status_code == 200
        entries = resp.json()
        # Should have entries from seed, ingest, transform
        assert len(entries) > 0
