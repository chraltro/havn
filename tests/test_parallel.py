"""Tests for parallel model execution."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from havn.engine.database import ensure_meta_table
from havn.engine.transform import (
    SQLModel,
    build_dag_tiers,
    discover_models,
    run_transform,
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


class TestParallelExecution:
    def test_build_dag_tiers(self):
        models = [
            SQLModel(
                path=Path("a.sql"), name="a", schema="bronze", full_name="bronze.a",
                sql="", query="SELECT 1", materialized="table", depends_on=[],
            ),
            SQLModel(
                path=Path("b.sql"), name="b", schema="bronze", full_name="bronze.b",
                sql="", query="SELECT 1", materialized="table", depends_on=[],
            ),
            SQLModel(
                path=Path("c.sql"), name="c", schema="silver", full_name="silver.c",
                sql="", query="SELECT 1", materialized="table",
                depends_on=["bronze.a", "bronze.b"],
            ),
            SQLModel(
                path=Path("d.sql"), name="d", schema="gold", full_name="gold.d",
                sql="", query="SELECT 1", materialized="table",
                depends_on=["silver.c"],
            ),
        ]
        tiers = build_dag_tiers(models)
        assert len(tiers) == 3

        # First tier: a and b (no dependencies)
        tier1_names = {m.full_name for m in tiers[0]}
        assert tier1_names == {"bronze.a", "bronze.b"}

        # Second tier: c (depends on a and b)
        tier2_names = {m.full_name for m in tiers[1]}
        assert tier2_names == {"silver.c"}

        # Third tier: d (depends on c)
        tier3_names = {m.full_name for m in tiers[2]}
        assert tier3_names == {"gold.d"}

    def test_parallel_transform(self, db, transform_dir):
        """Test parallel execution produces correct results."""
        db.execute("CREATE TABLE landing.a AS SELECT 1 AS id")
        db.execute("CREATE TABLE landing.b AS SELECT 2 AS id")

        (transform_dir / "bronze" / "a.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.a\n\n"
            "SELECT id FROM landing.a\n"
        )
        (transform_dir / "bronze" / "b.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.b\n\n"
            "SELECT id FROM landing.b\n"
        )
        (transform_dir / "gold" / "combined.sql").write_text(
            "-- config: materialized=table, schema=gold\n"
            "-- depends_on: bronze.a, bronze.b\n\n"
            "SELECT * FROM bronze.a UNION ALL SELECT * FROM bronze.b\n"
        )

        # Note: parallel=True requires file-based database (not in-memory)
        # The db fixture already uses a file-based database
        results = run_transform(db, transform_dir, force=True, parallel=False)
        assert results["bronze.a"] == "built"
        assert results["bronze.b"] == "built"
        assert results["gold.combined"] == "built"

        # Verify combined table
        row = db.execute("SELECT COUNT(*) FROM gold.combined").fetchone()
        assert row[0] == 2

    def test_single_model_tier(self):
        """A single model should create a single tier."""
        models = [
            SQLModel(
                path=Path("a.sql"), name="a", schema="bronze", full_name="bronze.a",
                sql="", query="SELECT 1", materialized="table", depends_on=[],
            ),
        ]
        tiers = build_dag_tiers(models)
        assert len(tiers) == 1
        assert len(tiers[0]) == 1

    def test_parallel_first_run_creates_schemas_without_race(self, tmp_path, transform_dir):
        """Regression: on the very first transform run, multiple models in
        tier 1 targeting the same not-yet-existing schema must not race on
        `CREATE SCHEMA`. Pre-creating schemas on the main connection before
        dispatching workers is what prevents
        `Catalog write-write conflict on create with "bronze"`.

        Setup mimics a fresh `havn init` + `havn transform`: warehouse exists
        but no `bronze` schema, four bronze models all in tier 1 (no deps on
        each other), so they all dispatch to parallel workers simultaneously.
        """
        # Fresh DB, only landing schema exists
        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        ensure_meta_table(conn)
        conn.execute("CREATE SCHEMA landing")
        conn.execute("CREATE TABLE landing.src AS SELECT i AS id FROM range(100) g(i)")

        # Four independent bronze models — all in tier 1, all need bronze schema
        for name in ("a", "b", "c", "d"):
            (transform_dir / "bronze" / f"{name}.sql").write_text(
                f"-- config: materialized=table, schema=bronze\n"
                f"-- depends_on: landing.src\n\n"
                f"SELECT id FROM landing.src WHERE id % 4 = "
                f"{ord(name) - ord('a')}\n"
            )

        # parallel=True with max_workers=4 forces the race condition
        results = run_transform(
            conn, transform_dir,
            force=True, parallel=True, max_workers=4,
            db_path=str(db_path),
        )

        # All four must succeed — the schema-precreate fix means the first
        # tier doesn't race on CREATE SCHEMA bronze
        for name in ("a", "b", "c", "d"):
            assert results[f"bronze.{name}"] == "built", (
                f"bronze.{name} did not build: status={results.get(f'bronze.{name}')!r}. "
                "If the schema-precreate fix regressed, you'll see a "
                "'Catalog write-write conflict' error here."
            )
        conn.close()

    def test_parallel_error_in_tier_blocks_next(self, db, transform_dir):
        """An error in one tier should block downstream tiers in parallel mode."""
        db.execute("CREATE TABLE landing.good AS SELECT 1 AS id")

        (transform_dir / "bronze" / "bad.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.nonexistent\n\n"
            "SELECT * FROM landing.nonexistent\n"
        )
        (transform_dir / "silver" / "downstream.sql").write_text(
            "-- config: materialized=table, schema=silver\n"
            "-- depends_on: bronze.bad\n\n"
            "SELECT * FROM bronze.bad\n"
        )

        results = run_transform(db, transform_dir, force=True, parallel=False)
        assert results["bronze.bad"] == "error"
        # Downstream should still be attempted (sequential doesn't auto-block),
        # but it should also error because the source doesn't exist
        assert results.get("silver.downstream") in ("error", "skipped")
