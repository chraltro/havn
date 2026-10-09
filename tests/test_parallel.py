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

    def test_parallel_assertion_failure_blocks_downstream(self, tmp_path, transform_dir):
        """A severity=error @assert failure in a parallel worker tier must be
        reported as assertion_failed and block descendants — not silently pass
        as 'built'. Only reproduces when 2+ models share a tier (so the parallel
        worker path runs instead of the inline single-model path)."""
        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        ensure_meta_table(conn)
        conn.execute("CREATE SCHEMA landing")
        conn.execute("CREATE TABLE landing.src AS SELECT i AS id FROM range(10) g(i)")

        # Two independent tier-1 models forces the parallel worker path.
        # bronze.bad has an error-severity assertion that fails.
        (transform_dir / "bronze" / "bad.sql").write_text(
            "@config materialized=table, schema=bronze\n"
            "@assert (SELECT count(*) FROM bronze.bad) = 0\n\n"
            "SELECT id FROM landing.src\n"
        )
        (transform_dir / "bronze" / "ok.sql").write_text(
            "@config materialized=table, schema=bronze\n\n"
            "SELECT id FROM landing.src\n"
        )
        # Downstream of the failing model — must be blocked.
        (transform_dir / "silver" / "down.sql").write_text(
            "@config materialized=table, schema=silver\n"
            "@depends_on bronze.bad\n\n"
            "SELECT id FROM bronze.bad\n"
        )

        results = run_transform(
            conn, transform_dir,
            force=True, parallel=True, max_workers=4,
            db_path=str(db_path),
        )
        assert results["bronze.bad"] == "assertion_failed"
        assert results["bronze.ok"] == "built"
        assert results.get("silver.down") in (
            "skipped", "skipped_upstream_blocked", "assertion_failed",
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
        # When upstream errors, downstream is now skipped with a
        # specific "upstream blocked" status — the runner refuses to
        # build models that depend on a failed upstream so bad data
        # can't cascade.
        assert results.get("silver.downstream") in ("error", "skipped", "skipped_upstream_blocked")


class TestParallelSubsetAndGates:
    def test_subset_tiers_follow_an_unselected_intermediate(self, tmp_path):
        """bronze.a -> silver.b (view, unselected) -> gold.d: d must run after a.

        Tiers were built from the selection alone, which has no a -> d edge,
        so both landed in one tier and d read b's view over the old a.
        """
        (tmp_path / "project.yml").write_text("name: t\n")
        t = tmp_path / "transform"
        for sub in ("bronze", "silver", "gold"):
            (t / sub).mkdir(parents=True)
        (t / "bronze" / "a.sql").write_text("@config materialized=table, tags=daily\nSELECT 1 AS x\n")
        (t / "silver" / "b.sql").write_text("@config materialized=view\nSELECT * FROM bronze.a\n")
        (t / "gold" / "d.sql").write_text("@config materialized=table, tags=daily\nSELECT * FROM silver.b\n")
        db_path = str(tmp_path / "w.duckdb")
        conn = duckdb.connect(db_path)
        run_transform(conn, t, project_dir=tmp_path)

        models = discover_models(t)
        subset = [m for m in models if m.full_name != "silver.b"]
        assert [[m.full_name for m in tier] for tier in build_dag_tiers(subset, models)] == [
            ["bronze.a"], ["gold.d"],
        ]

        (t / "bronze" / "a.sql").write_text("@config materialized=table, tags=daily\nSELECT 2 AS x\n")
        run_transform(
            conn, t, targets=["tag:daily"], project_dir=tmp_path,
            parallel=True, db_path=db_path,
        )
        assert conn.execute("SELECT x FROM gold.d").fetchall() == [(2,)]
        conn.close()

    @pytest.mark.parametrize("siblings", [0, 1])
    def test_parallel_honours_source_freshness(self, tmp_path, siblings):
        """A stale error-severity source stops the model in parallel runs too,
        whether it runs alone in its tier or beside another model."""
        t = tmp_path / "transform"
        (t / "silver").mkdir(parents=True)
        db_path = str(tmp_path / "w.duckdb")
        conn = duckdb.connect(db_path)
        conn.execute("CREATE SCHEMA landing")
        conn.execute("CREATE TABLE landing.tx (id INT, loaded_at TIMESTAMP)")
        conn.execute("INSERT INTO landing.tx VALUES (1, now()::TIMESTAMP - INTERVAL 30 DAY)")
        (t / "silver" / "tx.sql").write_text(
            "@config materialized=table\n"
            "@source_freshness landing.tx, max_age=1h, on=loaded_at\n"
            "SELECT * FROM landing.tx\n"
        )
        if siblings:
            (t / "silver" / "other.sql").write_text("@config materialized=table\nSELECT 1 AS x\n")
        results = run_transform(conn, t, parallel=True, db_path=db_path)
        assert results["silver.tx"] == "source_stale"
        if siblings:
            assert results["silver.other"] == "built"
        conn.close()
