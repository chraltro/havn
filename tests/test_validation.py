"""Tests for compile-time SQL validation and impact analysis."""

from __future__ import annotations

import textwrap
from pathlib import Path

import duckdb
import pytest

from havn.engine.database import ensure_meta_table
from havn.engine.transform import (
    SQLModel,
    ValidationError,
    discover_models,
    impact_analysis,
    validate_models,
)


@pytest.fixture
def db(tmp_path):
    """Create a DuckDB connection with metadata tables."""
    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))
    ensure_meta_table(conn)
    conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
    return conn


@pytest.fixture
def transform_dir(tmp_path):
    """Create a basic transform directory."""
    t = tmp_path / "transform"
    t.mkdir()
    for sub in ("bronze", "silver", "gold"):
        (t / sub).mkdir()
    return t


class TestValidation:
    def test_validate_valid_models(self, db, transform_dir):
        db.execute("CREATE TABLE landing.data AS SELECT 1 AS id, 'test' AS name")
        (transform_dir / "bronze" / "data.sql").write_text(textwrap.dedent("""\
            -- config: materialized=table, schema=bronze
            -- depends_on: landing.data

            SELECT id, name FROM landing.data
        """))
        models = discover_models(transform_dir)
        errors = validate_models(db, models)
        assert len(errors) == 0

    def test_validate_bad_sql(self, transform_dir):
        (transform_dir / "bronze" / "bad.sql").write_text(textwrap.dedent("""\
            -- config: materialized=table, schema=bronze

            SELECTT id FRUM landing.data WHEREE
        """))
        models = discover_models(transform_dir)
        errors = validate_models(None, models)
        parse_errors = [e for e in errors if "parse error" in e.message.lower()]
        assert len(parse_errors) >= 1

    def test_validate_missing_table(self, db, transform_dir):
        (transform_dir / "bronze" / "missing.sql").write_text(textwrap.dedent("""\
            -- config: materialized=table, schema=bronze
            -- depends_on: landing.nonexistent

            SELECT id FROM landing.nonexistent
        """))
        models = discover_models(transform_dir)
        errors = validate_models(db, models)
        table_errors = [e for e in errors if "does not exist" in e.message]
        assert len(table_errors) >= 1

    def test_validate_bad_column(self, db, transform_dir):
        """Qualified column reference to a non-existent column should be caught."""
        db.execute("CREATE TABLE landing.users AS SELECT 1 AS id, 'alice' AS name")
        (transform_dir / "bronze" / "users.sql").write_text(textwrap.dedent("""\
            -- config: materialized=table, schema=bronze
            -- depends_on: landing.users

            SELECT u.id, u.nonexistent_column FROM landing.users u
        """))
        models = discover_models(transform_dir)
        errors = validate_models(db, models)
        col_errors = [e for e in errors if "not found" in e.message.lower()]
        assert len(col_errors) >= 1

    def test_validate_no_false_positive_on_cte_columns(self, db, transform_dir):
        """CTE-internal columns must not be looked up against source tables."""
        db.execute("CREATE TABLE landing.txns AS SELECT 1 AS account_id, 100.0 AS value_nok")
        (transform_dir / "silver" / "flows.sql").write_text(textwrap.dedent("""\
            -- config: materialized=table, schema=silver
            -- depends_on: landing.txns

            WITH flows AS (
                SELECT account_id, SUM(value_nok) AS inflow_nok
                FROM landing.txns
                GROUP BY 1
            )
            SELECT f.account_id, f.inflow_nok FROM flows f
        """))
        models = discover_models(transform_dir)
        errors = validate_models(db, models)
        # No "Column 'inflow_nok' not found" errors should fire — it is a CTE column.
        bad = [e for e in errors if "inflow_nok" in e.message and "not found" in e.message.lower()]
        assert bad == [], bad

    def test_validate_no_false_positive_on_star_alias(self, db, transform_dir):
        """`SELECT b.*` must not be flagged as a missing '*' column."""
        db.execute("CREATE TABLE landing.events AS SELECT 1 AS id, 'x' AS kind")
        (transform_dir / "bronze" / "passthrough.sql").write_text(textwrap.dedent("""\
            -- config: materialized=view, schema=bronze
            -- depends_on: landing.events

            SELECT b.* FROM landing.events b
        """))
        models = discover_models(transform_dir)
        errors = validate_models(db, models)
        bad = [e for e in errors if "'*'" in e.message]
        assert bad == [], bad

    def test_validate_cte_column_typo_still_caught(self, db, transform_dir):
        """When a CTE's columns are inferable, referencing a missing one errors."""
        db.execute("CREATE TABLE landing.txns AS SELECT 1 AS account_id, 100.0 AS value_nok")
        (transform_dir / "silver" / "flows_typo.sql").write_text(textwrap.dedent("""\
            -- config: materialized=table, schema=silver
            -- depends_on: landing.txns

            WITH flows AS (
                SELECT account_id, SUM(value_nok) AS inflow_nok
                FROM landing.txns
                GROUP BY 1
            )
            SELECT f.account_id, f.inflowww_nok FROM flows f
        """))
        models = discover_models(transform_dir)
        errors = validate_models(db, models)
        bad = [
            e for e in errors
            if "inflowww_nok" in e.message and "not found" in e.message.lower()
        ]
        assert len(bad) >= 1, errors

    def test_validate_select_star_in_cte_not_falsely_flagged(self, db, transform_dir):
        """A CTE with `SELECT *` exposes columns we can't enumerate; downstream
        column refs against that CTE must not error."""
        db.execute("CREATE TABLE landing.events AS SELECT 1 AS id, 'x' AS kind")
        (transform_dir / "silver" / "passthrough.sql").write_text(textwrap.dedent("""\
            -- config: materialized=table, schema=silver
            -- depends_on: landing.events

            WITH all_events AS (
                SELECT * FROM landing.events
            )
            SELECT a.id, a.kind, a.something_we_cant_verify FROM all_events a
        """))
        models = discover_models(transform_dir)
        errors = validate_models(db, models)
        # We don't know the CTE's column list (SELECT *), so we must not
        # falsely flag any downstream column ref as missing.
        bad = [e for e in errors if "not found" in e.message.lower()]
        assert bad == [], bad

    def test_validate_recursive_cte_does_not_loop(self, db, transform_dir):
        """A WITH RECURSIVE CTE must not crash or infinite-loop the validator."""
        (transform_dir / "silver" / "recursive.sql").write_text(textwrap.dedent("""\
            -- config: materialized=table, schema=silver

            WITH RECURSIVE counter(n) AS (
                SELECT 1
                UNION ALL
                SELECT n + 1 FROM counter WHERE n < 5
            )
            SELECT n FROM counter
        """))
        models = discover_models(transform_dir)
        # Just verify it doesn't blow up; the result may be empty or have
        # warnings but the validator itself must terminate.
        validate_models(db, models)

    def test_validate_model_references_other_model(self, db, transform_dir):
        """Referencing another model (not yet built) should not error."""
        (transform_dir / "bronze" / "a.sql").write_text(textwrap.dedent("""\
            -- config: materialized=table, schema=bronze

            SELECT 1 AS id
        """))
        (transform_dir / "silver" / "b.sql").write_text(textwrap.dedent("""\
            -- config: materialized=table, schema=silver
            -- depends_on: bronze.a

            SELECT id FROM bronze.a
        """))
        models = discover_models(transform_dir)
        errors = validate_models(db, models)
        # bronze.a is a known model name, so it should not error
        table_errors = [e for e in errors if "bronze.a" in e.message and "does not exist" in e.message]
        assert len(table_errors) == 0

    def test_validate_no_connection(self, transform_dir):
        """Validation without a connection should still check SQL parsing."""
        (transform_dir / "bronze" / "ok.sql").write_text("SELECT 1 AS id\n")
        models = discover_models(transform_dir)
        errors = validate_models(None, models)
        assert len(errors) == 0


    def test_validate_depends_on_missing_from_dag_and_catalog(self, db, transform_dir):
        """depends_on references that don't exist in DAG or catalog should error."""
        (transform_dir / "bronze" / "orphan.sql").write_text(textwrap.dedent("""\
            -- config: materialized=table, schema=bronze
            -- depends_on: staging.ghost_table

            SELECT 1 AS id
        """))
        models = discover_models(transform_dir)
        errors = validate_models(db, models)
        dep_errors = [e for e in errors if "ghost_table" in e.message and e.severity == "error"]
        assert len(dep_errors) >= 1

    def test_validate_incremental_no_unique_key_warns(self):
        """Incremental model with delete+insert but no unique_key should warn."""
        model = SQLModel(
            path=Path("inc.sql"), name="inc", schema="silver", full_name="silver.inc",
            sql="", query="SELECT 1 AS id", materialized="incremental",
            depends_on=[], incremental_strategy="delete+insert", unique_key=None,
        )
        errors = validate_models(None, [model])
        warnings = [e for e in errors if e.severity == "warning" and "unique_key" in e.message]
        assert len(warnings) == 1

    def test_validate_incremental_merge_no_unique_key_warns(self):
        """Incremental model with merge strategy but no unique_key should warn."""
        model = SQLModel(
            path=Path("inc.sql"), name="inc", schema="silver", full_name="silver.inc",
            sql="", query="SELECT 1 AS id", materialized="incremental",
            depends_on=[], incremental_strategy="merge", unique_key=None,
        )
        errors = validate_models(None, [model])
        warnings = [e for e in errors if e.severity == "warning" and "unique_key" in e.message]
        assert len(warnings) == 1

    def test_validate_incremental_with_unique_key_ok(self):
        """Incremental model with unique_key set should not warn."""
        model = SQLModel(
            path=Path("inc.sql"), name="inc", schema="silver", full_name="silver.inc",
            sql="", query="SELECT 1 AS id", materialized="incremental",
            depends_on=[], incremental_strategy="delete+insert", unique_key="id",
        )
        errors = validate_models(None, [model])
        warnings = [e for e in errors if "unique_key" in e.message]
        assert len(warnings) == 0

    def test_validate_incremental_append_no_unique_key_ok(self):
        """Incremental model with append strategy needs no unique_key."""
        model = SQLModel(
            path=Path("inc.sql"), name="inc", schema="silver", full_name="silver.inc",
            sql="", query="SELECT 1 AS id", materialized="incremental",
            depends_on=[], incremental_strategy="append", unique_key=None,
        )
        errors = validate_models(None, [model])
        warnings = [e for e in errors if "unique_key" in e.message]
        assert len(warnings) == 0

    def test_validate_model_writes_to_landing_schema_errors(self):
        """Model targeting a landing schema should produce an error."""
        model = SQLModel(
            path=Path("bad.sql"), name="bad", schema="landing", full_name="landing.bad",
            sql="", query="SELECT 1 AS id", materialized="table", depends_on=[],
        )
        errors = validate_models(None, [model])
        landing_errors = [e for e in errors if e.severity == "error" and "landing" in e.message]
        assert len(landing_errors) >= 1

    def test_validate_model_writes_to_custom_landing_schema_errors(self):
        """Model targeting a custom landing schema should error."""
        model = SQLModel(
            path=Path("bad.sql"), name="bad", schema="raw_data", full_name="raw_data.bad",
            sql="", query="SELECT 1 AS id", materialized="table", depends_on=[],
        )
        errors = validate_models(None, [model], landing_schemas={"raw_data"})
        landing_errors = [e for e in errors if e.severity == "error" and "raw_data" in e.message]
        assert len(landing_errors) >= 1

    def test_validate_model_in_bronze_schema_ok(self):
        """Model in bronze schema should not trigger landing-schema error."""
        model = SQLModel(
            path=Path("ok.sql"), name="ok", schema="bronze", full_name="bronze.ok",
            sql="", query="SELECT 1 AS id", materialized="table", depends_on=[],
        )
        errors = validate_models(None, [model])
        landing_errors = [e for e in errors if "landing" in e.message.lower() and "overwrite" in e.message.lower()]
        assert len(landing_errors) == 0


class TestImpactAnalysis:
    def test_basic_impact(self):
        models = [
            SQLModel(
                path=Path("a.sql"), name="a", schema="bronze", full_name="bronze.a",
                sql="", query="SELECT 1", materialized="table", depends_on=[],
            ),
            SQLModel(
                path=Path("b.sql"), name="b", schema="silver", full_name="silver.b",
                sql="", query="SELECT 1", materialized="table",
                depends_on=["bronze.a"],
            ),
            SQLModel(
                path=Path("c.sql"), name="c", schema="gold", full_name="gold.c",
                sql="", query="SELECT 1", materialized="table",
                depends_on=["silver.b"],
            ),
        ]
        result = impact_analysis(models, "bronze.a")
        assert "silver.b" in result["downstream_models"]
        assert "gold.c" in result["downstream_models"]
        assert len(result["downstream_models"]) == 2

    def test_impact_no_downstream(self):
        models = [
            SQLModel(
                path=Path("a.sql"), name="a", schema="gold", full_name="gold.a",
                sql="", query="SELECT 1", materialized="table", depends_on=[],
            ),
        ]
        result = impact_analysis(models, "gold.a")
        assert result["downstream_models"] == []

    def test_impact_chain(self):
        models = [
            SQLModel(path=Path("a.sql"), name="a", schema="bronze", full_name="bronze.a",
                     sql="", query="SELECT 1", materialized="table", depends_on=[]),
            SQLModel(path=Path("b.sql"), name="b", schema="silver", full_name="silver.b",
                     sql="", query="SELECT 1", materialized="table", depends_on=["bronze.a"]),
            SQLModel(path=Path("c.sql"), name="c", schema="gold", full_name="gold.c",
                     sql="", query="SELECT 1", materialized="table", depends_on=["silver.b"]),
        ]
        result = impact_analysis(models, "bronze.a")
        chain = result["impact_chain"]
        assert "bronze.a" in chain
        assert "silver.b" in chain["bronze.a"]
        assert "silver.b" in chain
        assert "gold.c" in chain["silver.b"]

    def test_impact_with_column(self, db):
        db.execute("CREATE SCHEMA IF NOT EXISTS bronze")
        db.execute("CREATE TABLE bronze.src AS SELECT 1 AS id, 'x' AS name")

        models = [
            SQLModel(path=Path("a.sql"), name="src", schema="bronze", full_name="bronze.src",
                     sql="", query="SELECT 1 AS id, 'x' AS name", materialized="table", depends_on=[]),
            SQLModel(path=Path("b.sql"), name="users", schema="silver", full_name="silver.users",
                     sql="", query="SELECT s.id, s.name FROM bronze.src s",
                     materialized="table", depends_on=["bronze.src"]),
        ]
        result = impact_analysis(models, "bronze.src", column="name", conn=db)
        assert result["column"] == "name"
        affected = result["affected_columns"]
        assert any(a["model"] == "silver.users" and a["column"] == "name" for a in affected)

    def test_impact_diamond_dependency(self):
        """Diamond dependency: A -> B, A -> C, B -> D, C -> D."""
        models = [
            SQLModel(path=Path("a.sql"), name="a", schema="bronze", full_name="bronze.a",
                     sql="", query="SELECT 1", materialized="table", depends_on=[]),
            SQLModel(path=Path("b.sql"), name="b", schema="silver", full_name="silver.b",
                     sql="", query="SELECT 1", materialized="table", depends_on=["bronze.a"]),
            SQLModel(path=Path("c.sql"), name="c", schema="silver", full_name="silver.c",
                     sql="", query="SELECT 1", materialized="table", depends_on=["bronze.a"]),
            SQLModel(path=Path("d.sql"), name="d", schema="gold", full_name="gold.d",
                     sql="", query="SELECT 1", materialized="table",
                     depends_on=["silver.b", "silver.c"]),
        ]
        result = impact_analysis(models, "bronze.a")
        assert set(result["downstream_models"]) == {"silver.b", "silver.c", "gold.d"}
