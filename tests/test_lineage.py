"""Tests for column-level lineage (sqlglot AST-based)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import duckdb
import pytest

from havn.engine.database import ensure_meta_table
from havn.engine.sql_analysis import (
    clear_lineage_cache,
    extract_column_lineage as extract_column_lineage_sql,
    fetch_column_catalog,
)
from havn.engine.transform import (
    SQLModel,
    extract_column_lineage,
    impact_analysis,
)


@pytest.fixture
def db(tmp_path):
    """Create a DuckDB connection with metadata tables."""
    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))
    ensure_meta_table(conn)
    conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
    return conn


class TestColumnLineage:
    def test_simple_lineage(self):
        model = SQLModel(
            path=Path("test.sql"), name="customers", schema="gold",
            full_name="gold.customers", sql="",
            query="SELECT c.customer_id, c.name, COUNT(o.order_id) AS order_count FROM bronze.customers c LEFT JOIN bronze.orders o ON c.customer_id = o.customer_id GROUP BY 1, 2",
            materialized="table",
            depends_on=["bronze.customers", "bronze.orders"],
        )
        lineage = extract_column_lineage(model)
        assert "customer_id" in lineage
        assert any(s["source_table"] == "bronze.customers" for s in lineage["customer_id"])
        assert "order_count" in lineage
        assert any(s["source_table"] == "bronze.orders" for s in lineage["order_count"])

    def test_lineage_with_aliases(self):
        model = SQLModel(
            path=Path("test.sql"), name="summary", schema="gold",
            full_name="gold.summary", sql="",
            query="SELECT e.event_id, e.magnitude AS mag FROM silver.earthquake_events AS e",
            materialized="table",
            depends_on=["silver.earthquake_events"],
        )
        lineage = extract_column_lineage(model)
        assert "event_id" in lineage
        assert "mag" in lineage
        # mag should trace to silver.earthquake_events.magnitude
        assert any(
            s["source_table"] == "silver.earthquake_events" and s["source_column"] == "magnitude"
            for s in lineage["mag"]
        )

    def test_lineage_star_select(self):
        model = SQLModel(
            path=Path("test.sql"), name="test", schema="bronze",
            full_name="bronze.test", sql="",
            query="SELECT * FROM landing.raw_data",
            materialized="view",
            depends_on=["landing.raw_data"],
        )
        lineage = extract_column_lineage(model)
        # * doesn't give us column names without a db connection
        assert isinstance(lineage, dict)

    def test_lineage_computed_column(self):
        model = SQLModel(
            path=Path("test.sql"), name="test", schema="silver",
            full_name="silver.test", sql="",
            query="SELECT d.id, d.amount * 1.1 AS amount_with_tax FROM bronze.data d",
            materialized="table",
            depends_on=["bronze.data"],
        )
        lineage = extract_column_lineage(model)
        assert "amount_with_tax" in lineage
        assert any(s["source_column"] == "amount" for s in lineage["amount_with_tax"])

    def test_lineage_with_cte(self):
        """CTEs should be traced through to the source tables."""
        model = SQLModel(
            path=Path("test.sql"), name="test", schema="gold",
            full_name="gold.test", sql="",
            query=textwrap.dedent("""\
                WITH filtered AS (
                    SELECT id, name FROM bronze.customers WHERE active = true
                )
                SELECT f.id, f.name FROM filtered f
            """),
            materialized="table",
            depends_on=["bronze.customers"],
        )
        lineage = extract_column_lineage(model)
        assert "id" in lineage
        assert "name" in lineage

    def test_lineage_with_case(self):
        """CASE expressions should trace all column references."""
        model = SQLModel(
            path=Path("test.sql"), name="test", schema="silver",
            full_name="silver.test", sql="",
            query="SELECT e.id, CASE WHEN e.magnitude >= 5.0 THEN 'strong' ELSE 'weak' END AS strength FROM bronze.events e",
            materialized="table",
            depends_on=["bronze.events"],
        )
        lineage = extract_column_lineage(model)
        assert "strength" in lineage
        assert any(s["source_column"] == "magnitude" for s in lineage["strength"])

    def test_lineage_with_window_function(self):
        """Window functions should trace column references."""
        model = SQLModel(
            path=Path("test.sql"), name="test", schema="silver",
            full_name="silver.test", sql="",
            query="SELECT e.id, ROW_NUMBER() OVER (PARTITION BY e.region ORDER BY e.magnitude DESC) AS rn FROM bronze.events e",
            materialized="table",
            depends_on=["bronze.events"],
        )
        lineage = extract_column_lineage(model)
        assert "rn" in lineage
        # rn references region and magnitude
        source_cols = {s["source_column"] for s in lineage["rn"]}
        assert "region" in source_cols
        assert "magnitude" in source_cols

    def test_lineage_with_subquery(self):
        """Subqueries in SELECT should trace sources."""
        model = SQLModel(
            path=Path("test.sql"), name="test", schema="gold",
            full_name="gold.test", sql="",
            query="SELECT c.id, (SELECT COUNT(*) FROM bronze.orders o WHERE o.customer_id = c.id) AS order_count FROM bronze.customers c",
            materialized="table",
            depends_on=["bronze.customers", "bronze.orders"],
        )
        lineage = extract_column_lineage(model)
        assert "id" in lineage
        assert "order_count" in lineage

    def test_lineage_union_all(self):
        """UNION ALL should trace from the first SELECT."""
        model = SQLModel(
            path=Path("test.sql"), name="test", schema="silver",
            full_name="silver.test", sql="",
            query="SELECT a.id, a.name FROM bronze.customers_a a UNION ALL SELECT b.id, b.name FROM bronze.customers_b b",
            materialized="table",
            depends_on=["bronze.customers_a", "bronze.customers_b"],
        )
        lineage = extract_column_lineage(model)
        assert "id" in lineage
        assert "name" in lineage

    def test_lineage_star_with_connection(self, db):
        """SELECT * with a connection should resolve columns from information_schema."""
        db.execute("CREATE SCHEMA IF NOT EXISTS bronze")
        db.execute("CREATE TABLE bronze.src AS SELECT 1 AS id, 'x' AS name, 3.14 AS val")
        model = SQLModel(
            path=Path("test.sql"), name="test", schema="silver",
            full_name="silver.test", sql="",
            query="SELECT * FROM bronze.src",
            materialized="view",
            depends_on=["bronze.src"],
        )
        lineage = extract_column_lineage(model, conn=db)
        assert "id" in lineage
        assert "name" in lineage
        assert "val" in lineage
        assert lineage["id"][0]["source_table"] == "bronze.src"

    def test_lineage_unparseable_sql(self):
        """Unparseable SQL should return empty lineage, not crash."""
        model = SQLModel(
            path=Path("test.sql"), name="test", schema="bronze",
            full_name="bronze.test", sql="",
            query="THIS IS NOT VALID SQL AT ALL",
            materialized="view",
        )
        lineage = extract_column_lineage(model)
        assert lineage == {}

    def test_lineage_multi_source_with_cte(self):
        """Multi-source models with CTEs must split lineage across the right
        upstreams. Reproduces the route_b bug where every column was attributed
        to depends_on[0]."""
        model = SQLModel(
            path=Path("test.sql"), name="cfo", schema="gold",
            full_name="gold.cfo", sql="",
            query=textwrap.dedent("""\
                WITH top_branches AS (
                    SELECT branch_id, branch_name FROM bronze.branches LIMIT 3
                )
                SELECT
                    f.amount,
                    b.branch_name
                FROM silver.fact_transactions f
                JOIN top_branches b ON f.branch_id = b.branch_id
            """),
            materialized="table",
            depends_on=["bronze.branches", "silver.fact_transactions"],
        )
        lineage = extract_column_lineage(model)
        amount_sources = {s["source_table"] for s in lineage["amount"]}
        branch_sources = {s["source_table"] for s in lineage["branch_name"]}
        assert "silver.fact_transactions" in amount_sources, amount_sources
        assert "bronze.branches" in branch_sources, branch_sources
        # Most importantly, amount must NOT be attributed to bronze.branches.
        assert "bronze.branches" not in amount_sources, amount_sources

    def test_lineage_nested_cte(self):
        """Lineage should propagate through CTEs that reference other CTEs."""
        model = SQLModel(
            path=Path("test.sql"), name="nested", schema="gold",
            full_name="gold.nested", sql="",
            query=textwrap.dedent("""\
                WITH base AS (
                    SELECT id, amount FROM bronze.txns
                ),
                doubled AS (
                    SELECT id, amount * 2 AS amount FROM base
                )
                SELECT d.id, d.amount FROM doubled d
            """),
            materialized="table",
            depends_on=["bronze.txns"],
        )
        lineage = extract_column_lineage(model)
        assert "id" in lineage
        assert "amount" in lineage
        for col in ["id", "amount"]:
            assert any(s["source_table"] == "bronze.txns" for s in lineage[col]), (
                col, lineage[col]
            )

    def test_lineage_select_b_star(self, db):
        """`SELECT b.*` with a real connection should expand from the aliased table."""
        db.execute("CREATE SCHEMA IF NOT EXISTS silver")
        db.execute("CREATE TABLE silver.fact_transactions AS SELECT 1 AS id, 'x' AS kind, 100.0 AS amount")
        model = SQLModel(
            path=Path("test.sql"), name="passthrough", schema="gold",
            full_name="gold.passthrough", sql="",
            query="SELECT b.* FROM silver.fact_transactions b",
            materialized="view",
            depends_on=["silver.fact_transactions"],
        )
        lineage = extract_column_lineage(model, conn=db)
        for col in ("id", "kind", "amount"):
            assert col in lineage, lineage
            assert lineage[col][0]["source_table"] == "silver.fact_transactions"


class _CountingConn:
    """Wraps a real DuckDB connection and counts ``execute`` calls."""

    def __init__(self, conn):
        self._conn = conn
        self.executes = 0

    def execute(self, *args, **kwargs):
        self.executes += 1
        return self._conn.execute(*args, **kwargs)


class TestLineageCatalogFetch:
    def test_catalog_read_once_per_model(self, db):
        """Resolution must not scan information_schema once per dependency."""
        db.execute("CREATE SCHEMA IF NOT EXISTS bronze")
        for name in ("a", "b", "c", "d"):
            db.execute(f"CREATE TABLE bronze.{name} AS SELECT 1 AS id, 2 AS val_{name}")
        model = SQLModel(
            path=Path("test.sql"), name="joined", schema="silver",
            full_name="silver.joined", sql="",
            query=(
                "SELECT a.id, a.val_a, b.val_b, c.val_c, d.val_d "
                "FROM bronze.a a "
                "JOIN bronze.b b ON a.id = b.id "
                "JOIN bronze.c c ON a.id = c.id "
                "JOIN bronze.d d ON a.id = d.id"
            ),
            materialized="view",
            depends_on=["bronze.a", "bronze.b", "bronze.c", "bronze.d"],
        )
        counting = _CountingConn(db)
        lineage = extract_column_lineage(model, conn=counting)
        assert counting.executes == 1
        assert lineage["val_c"][0]["source_table"] == "bronze.c"

    def test_shared_catalog_avoids_all_queries(self, db):
        """A caller-supplied catalog means no catalog query at all."""
        db.execute("CREATE SCHEMA IF NOT EXISTS bronze")
        db.execute("CREATE TABLE bronze.src AS SELECT 1 AS id, 'x' AS name")
        catalog = fetch_column_catalog(db)

        models = [
            SQLModel(
                path=Path(f"m{i}.sql"), name=f"m{i}", schema="silver",
                full_name=f"silver.m{i}", sql="",
                query="SELECT * FROM bronze.src",
                materialized="view",
                depends_on=["bronze.src"],
            )
            for i in range(5)
        ]
        counting = _CountingConn(db)
        for model in models:
            lineage = extract_column_lineage(model, conn=counting, column_catalog=catalog)
            assert set(lineage) == {"id", "name"}
        assert counting.executes == 0

    def test_fetch_column_catalog_preserves_ordinal_position(self, db):
        db.execute("CREATE SCHEMA IF NOT EXISTS bronze")
        db.execute("CREATE TABLE bronze.ordered AS SELECT 1 AS zeta, 2 AS alpha, 3 AS mid")
        catalog = fetch_column_catalog(db)
        assert catalog["bronze.ordered"] == ["zeta", "alpha", "mid"]

    def test_impact_analysis_reads_catalog_once(self, db):
        """Column-level impact walks many downstream models on one catalog."""
        db.execute("CREATE SCHEMA IF NOT EXISTS bronze")
        db.execute("CREATE TABLE bronze.src AS SELECT 1 AS id, 'x' AS name")
        models = [
            SQLModel(
                path=Path("src.sql"), name="src", schema="bronze",
                full_name="bronze.src", sql="",
                query="SELECT id, name FROM landing.src",
                materialized="table", depends_on=["landing.src"],
            )
        ]
        models += [
            SQLModel(
                path=Path(f"d{i}.sql"), name=f"d{i}", schema="silver",
                full_name=f"silver.d{i}", sql="",
                query="SELECT id, name FROM bronze.src",
                materialized="view", depends_on=["bronze.src"],
            )
            for i in range(6)
        ]
        counting = _CountingConn(db)
        result = impact_analysis(models, "bronze.src", column="name", conn=counting)
        assert len(result["affected_columns"]) == 6
        assert counting.executes == 1


class TestInferredSchema:
    """The `schema=` argument: columns a bind pass knows, the catalog does not."""

    def test_star_expands_from_an_inferred_schema(self):
        lineage = extract_column_lineage_sql(
            "SELECT * FROM silver.unbuilt",
            ["silver.unbuilt"],
            schema={"silver.unbuilt": [("id", "INTEGER"), ("label", "VARCHAR")]},
        )
        assert set(lineage) == {"id", "label"}
        assert lineage["label"] == [
            {"source_table": "silver.unbuilt", "source_column": "label"}
        ]

    def test_inferred_schema_wins_over_the_catalog(self, db):
        """The catalog describes the last build; the inferred schema, the next one."""
        db.execute("CREATE SCHEMA IF NOT EXISTS bronze")
        db.execute("CREATE TABLE bronze.src AS SELECT 1 AS id, 'x' AS name")
        lineage = extract_column_lineage_sql(
            "SELECT * FROM bronze.src",
            ["bronze.src"],
            db,
            schema={"bronze.src": [("id", "INTEGER"), ("name", "VARCHAR"), ("added", "DOUBLE")]},
        )
        assert set(lineage) == {"id", "name", "added"}

    def test_unparseable_inferred_type_does_not_break_the_trace(self):
        lineage = extract_column_lineage_sql(
            "SELECT id FROM silver.unbuilt",
            ["silver.unbuilt"],
            schema={"silver.unbuilt": [("id", "NOT A REAL TYPE")]},
        )
        assert lineage["id"] == [
            {"source_table": "silver.unbuilt", "source_column": "id"}
        ]

    def test_star_without_any_schema_is_marked_unresolved(self):
        """No catalog, no inferred schema: the upstream is known, the columns are not."""
        lineage = extract_column_lineage_sql(
            "SELECT * FROM bronze.mystery", ["bronze.mystery"]
        )
        assert lineage == {
            "*": [
                {
                    "source_table": "bronze.mystery",
                    "source_column": "*",
                    "resolved": False,
                }
            ]
        }


class TestLineageMemo:
    """Tracing is memoized on the SQL plus the upstream column lists."""

    def test_repeat_call_returns_an_independent_copy(self):
        clear_lineage_cache()
        sql = "SELECT c.customer_id, c.name FROM bronze.customers c"
        first = extract_column_lineage_sql(sql, ["bronze.customers"])
        first["customer_id"].append({"source_table": "junk", "source_column": "junk"})
        first["injected"] = []

        second = extract_column_lineage_sql(sql, ["bronze.customers"])
        assert "injected" not in second
        assert second["customer_id"] == [
            {"source_table": "bronze.customers", "source_column": "customer_id"}
        ]

    def test_new_upstream_column_is_not_served_from_the_memo(self, db):
        """A rebuilt upstream changes the key, so `SELECT *` re-expands."""
        clear_lineage_cache()
        db.execute("CREATE SCHEMA IF NOT EXISTS bronze")
        db.execute("CREATE TABLE bronze.src AS SELECT 1 AS id, 'x' AS name")
        sql = "SELECT * FROM bronze.src"
        assert set(extract_column_lineage_sql(sql, ["bronze.src"], db)) == {"id", "name"}

        db.execute("ALTER TABLE bronze.src ADD COLUMN extra INTEGER")
        assert set(extract_column_lineage_sql(sql, ["bronze.src"], db)) == {
            "id",
            "name",
            "extra",
        }

    def test_clear_lineage_cache_does_not_change_results(self):
        sql = "SELECT o.order_id, o.amount FROM bronze.orders o"
        before = extract_column_lineage_sql(sql, ["bronze.orders"])
        clear_lineage_cache()
        assert extract_column_lineage_sql(sql, ["bronze.orders"]) == before
