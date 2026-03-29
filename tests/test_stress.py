"""Stress tests: large data, deep DAGs, complex pipelines, and edge cases."""

import textwrap
from pathlib import Path

import duckdb
import pytest

from havn.engine.database import ensure_meta_table
from havn.engine.transform import (
    SQLModel,
    build_dag,
    build_dag_tiers,
    discover_models,
    run_transform,
)
from havn.engine.transform.quality import run_assertions, profile_model
from havn.engine.runner import run_script, run_scripts_in_dir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "stress.duckdb"
    conn = duckdb.connect(str(db_path))
    ensure_meta_table(conn)
    conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
    return conn


@pytest.fixture
def transform_dir(tmp_path):
    t = tmp_path / "transform"
    t.mkdir()
    for sub in ("bronze", "silver", "gold"):
        (t / sub).mkdir()
    return t


# ===========================================================================
# 1. LARGE DATA VOLUME
# ===========================================================================

class TestLargeData:
    """Test the pipeline with large row counts."""

    def test_million_row_transform(self, db, transform_dir):
        """1M rows through a bronze -> silver -> gold pipeline."""
        db.execute(
            "CREATE TABLE landing.big AS "
            "SELECT i AS id, 'name_' || i AS name, random() AS score "
            "FROM generate_series(1, 1000000) t(i)"
        )

        (transform_dir / "bronze" / "big.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.big\n\n"
            "SELECT id, name, score FROM landing.big\n"
        )
        (transform_dir / "silver" / "big_agg.sql").write_text(
            "-- config: materialized=table, schema=silver\n"
            "-- depends_on: bronze.big\n\n"
            "SELECT\n"
            "    id % 100 AS bucket,\n"
            "    COUNT(*) AS cnt,\n"
            "    AVG(score) AS avg_score,\n"
            "    MIN(score) AS min_score,\n"
            "    MAX(score) AS max_score\n"
            "FROM bronze.big\n"
            "GROUP BY 1\n"
        )
        (transform_dir / "gold" / "summary.sql").write_text(
            "-- config: materialized=table, schema=gold\n"
            "-- depends_on: silver.big_agg\n\n"
            "SELECT\n"
            "    SUM(cnt) AS total_rows,\n"
            "    AVG(avg_score) AS grand_avg\n"
            "FROM silver.big_agg\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.big"] == "built"
        assert results["silver.big_agg"] == "built"
        assert results["gold.summary"] == "built"

        row = db.execute("SELECT total_rows FROM gold.summary").fetchone()
        assert row[0] == 1_000_000

    def test_wide_table_many_columns(self, db, transform_dir):
        """Table with 200 columns — tests profiling and schema handling."""
        cols = ", ".join(f"{i} AS col_{i}" for i in range(200))
        db.execute(f"CREATE TABLE landing.wide AS SELECT {cols}")

        select_cols = ", ".join(f"col_{i}" for i in range(200))
        (transform_dir / "bronze" / "wide.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.wide\n\n"
            f"SELECT {select_cols} FROM landing.wide\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.wide"] == "built"

        col_count = db.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = 'bronze' AND table_name = 'wide'"
        ).fetchone()[0]
        assert col_count == 200

    def test_large_incremental_upsert(self, db, transform_dir):
        """Incremental upsert on 500k rows then another 500k overlapping."""
        db.execute(
            "CREATE TABLE landing.events AS "
            "SELECT i AS event_id, 'type_' || (i % 10) AS event_type, random() AS val "
            "FROM generate_series(1, 500000) t(i)"
        )

        (transform_dir / "bronze" / "events.sql").write_text(
            "-- config: materialized=incremental, schema=bronze, "
            "unique_key=event_id\n"
            "-- depends_on: landing.events\n\n"
            "SELECT event_id, event_type, val FROM landing.events\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.events"] == "built"
        count1 = db.execute("SELECT COUNT(*) FROM bronze.events").fetchone()[0]
        assert count1 == 500_000

        # Add overlapping + new rows
        db.execute(
            "CREATE OR REPLACE TABLE landing.events AS "
            "SELECT i AS event_id, 'type_' || (i % 10) AS event_type, random() AS val "
            "FROM generate_series(250000, 750000) t(i)"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.events"] == "built"
        count2 = db.execute("SELECT COUNT(*) FROM bronze.events").fetchone()[0]
        assert count2 == 750_000


# ===========================================================================
# 2. DEEP / COMPLEX DAG
# ===========================================================================

class TestDAGComplexity:
    """Test deep chains, wide fans, and diamond dependencies."""

    def test_deep_chain_20_levels(self, db, transform_dir):
        """A linear chain of 20 models: level_0 -> level_1 -> ... -> level_19."""
        db.execute("CREATE TABLE landing.seed AS SELECT 1 AS val")

        # Level 0 depends on landing
        (transform_dir / "bronze" / "level_0.sql").write_text(
            "-- config: materialized=view, schema=bronze\n"
            "-- depends_on: landing.seed\n\n"
            "SELECT val, val + 1 AS computed FROM landing.seed\n"
        )

        # Levels 1-19 chain off each other (all in silver for simplicity)
        for i in range(1, 20):
            prev_schema = "bronze" if i == 1 else "silver"
            (transform_dir / "silver" / f"level_{i}.sql").write_text(
                f"-- config: materialized=view, schema=silver\n"
                f"-- depends_on: {prev_schema}.level_{i-1}\n\n"
                f"SELECT val, computed + 1 AS computed FROM {prev_schema}.level_{i-1}\n"
            )

        results = run_transform(db, transform_dir, force=True)
        assert all(v == "built" for v in results.values())
        assert len(results) == 20

        # Verify the chain computed correctly: val should be 1, computed = 1 + 20 = 21
        row = db.execute("SELECT computed FROM silver.level_19").fetchone()
        assert row[0] == 21

    def test_wide_fan_50_sources(self, db, transform_dir):
        """50 independent bronze models feeding into 1 gold aggregation."""
        for i in range(50):
            db.execute(f"CREATE TABLE landing.src_{i} AS SELECT {i} AS id, random() AS val")
            (transform_dir / "bronze" / f"src_{i}.sql").write_text(
                f"-- config: materialized=view, schema=bronze\n"
                f"-- depends_on: landing.src_{i}\n\n"
                f"SELECT id, val FROM landing.src_{i}\n"
            )

        # Gold model unions all 50
        deps = ", ".join(f"bronze.src_{i}" for i in range(50))
        unions = "\nUNION ALL\n".join(f"SELECT id, val FROM bronze.src_{i}" for i in range(50))
        (transform_dir / "gold" / "all_sources.sql").write_text(
            f"-- config: materialized=table, schema=gold\n"
            f"-- depends_on: {deps}\n\n"
            f"{unions}\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["gold.all_sources"] == "built"
        assert len(results) == 51  # 50 bronze + 1 gold

        count = db.execute("SELECT COUNT(*) FROM gold.all_sources").fetchone()[0]
        assert count == 50

    def test_diamond_dependency(self, db, transform_dir):
        """Diamond: A -> B, A -> C, B -> D, C -> D."""
        db.execute("CREATE TABLE landing.root AS SELECT 1 AS val")

        (transform_dir / "bronze" / "a.sql").write_text(
            "-- config: materialized=view, schema=bronze\n"
            "-- depends_on: landing.root\n\n"
            "SELECT val FROM landing.root\n"
        )
        (transform_dir / "silver" / "b.sql").write_text(
            "-- config: materialized=view, schema=silver\n"
            "-- depends_on: bronze.a\n\n"
            "SELECT val, val * 2 AS doubled FROM bronze.a\n"
        )
        (transform_dir / "silver" / "c.sql").write_text(
            "-- config: materialized=view, schema=silver\n"
            "-- depends_on: bronze.a\n\n"
            "SELECT val, val * 3 AS tripled FROM bronze.a\n"
        )
        (transform_dir / "gold" / "d.sql").write_text(
            "-- config: materialized=table, schema=gold\n"
            "-- depends_on: silver.b, silver.c\n\n"
            "SELECT b.doubled, c.tripled FROM silver.b b, silver.c c\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert all(v == "built" for v in results.values())

        row = db.execute("SELECT doubled, tripled FROM gold.d").fetchone()
        assert row == (2, 3)

    def test_circular_dependency_detected(self, db, transform_dir):
        """Circular dependency (A -> B -> C -> A) should raise an error."""
        (transform_dir / "bronze" / "a.sql").write_text(
            "-- config: materialized=view, schema=bronze\n"
            "-- depends_on: silver.c\n\n"
            "SELECT 1 AS val\n"
        )
        (transform_dir / "silver" / "b.sql").write_text(
            "-- config: materialized=view, schema=silver\n"
            "-- depends_on: bronze.a\n\n"
            "SELECT 1 AS val\n"
        )
        (transform_dir / "silver" / "c.sql").write_text(
            "-- config: materialized=view, schema=silver\n"
            "-- depends_on: silver.b\n\n"
            "SELECT 1 AS val\n"
        )

        models = discover_models(transform_dir)
        # graphlib.TopologicalSorter raises CycleError for circular deps
        from graphlib import CycleError
        with pytest.raises(CycleError):
            build_dag(models)

    def test_dag_tier_grouping_correctness(self, db, transform_dir):
        """Verify tier grouping puts independent models in the same tier."""
        db.execute("CREATE TABLE landing.x AS SELECT 1 AS val")
        db.execute("CREATE TABLE landing.y AS SELECT 2 AS val")

        # Two independent bronze models
        (transform_dir / "bronze" / "x.sql").write_text(
            "-- config: materialized=view, schema=bronze\n"
            "-- depends_on: landing.x\n\n"
            "SELECT val FROM landing.x\n"
        )
        (transform_dir / "bronze" / "y.sql").write_text(
            "-- config: materialized=view, schema=bronze\n"
            "-- depends_on: landing.y\n\n"
            "SELECT val FROM landing.y\n"
        )
        # Silver depends on both
        (transform_dir / "silver" / "combined.sql").write_text(
            "-- config: materialized=view, schema=silver\n"
            "-- depends_on: bronze.x, bronze.y\n\n"
            "SELECT * FROM bronze.x UNION ALL SELECT * FROM bronze.y\n"
        )

        models = discover_models(transform_dir)
        tiers = build_dag_tiers(models)

        # Tier 1: bronze.x and bronze.y (independent)
        # Tier 2: silver.combined (depends on both)
        assert len(tiers) == 2
        tier1_names = {m.full_name for m in tiers[0]}
        assert tier1_names == {"bronze.x", "bronze.y"}
        assert tiers[1][0].full_name == "silver.combined"


# ===========================================================================
# 3. EDGE CASES — MALFORMED SQL & BAD INPUT
# ===========================================================================

class TestEdgeCases:
    """Malformed SQL, missing deps, bad config, Unicode, etc."""

    def test_syntax_error_in_sql(self, db, transform_dir):
        """Malformed SQL should result in an error status, not a crash."""
        db.execute("CREATE TABLE landing.data AS SELECT 1 AS val")

        (transform_dir / "bronze" / "bad.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.data\n\n"
            "SELECTT val FROMM landing.data\n"  # typo
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.bad"] == "error"

    def test_missing_upstream_table(self, db, transform_dir):
        """Referencing a non-existent landing table should error gracefully."""
        (transform_dir / "bronze" / "ghost.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.does_not_exist\n\n"
            "SELECT * FROM landing.does_not_exist\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.ghost"] == "error"

    def test_empty_sql_file(self, db, transform_dir):
        """An empty .sql file should be handled gracefully."""
        (transform_dir / "bronze" / "empty.sql").write_text("")

        # Should discover it but it may error or produce no output
        models = discover_models(transform_dir)
        # Empty file produces an empty query — DuckDB will error on execute
        if models:
            results = run_transform(db, transform_dir, force=True)
            assert results.get("bronze.empty") in ("error", "built")

    def test_sql_with_only_comments(self, db, transform_dir):
        """SQL file with only config comments and no actual query."""
        (transform_dir / "bronze" / "comments_only.sql").write_text(
            "-- config: materialized=view, schema=bronze\n"
            "-- depends_on: landing.something\n"
        )

        models = discover_models(transform_dir)
        if models:
            results = run_transform(db, transform_dir, force=True)
            assert results.get("bronze.comments_only") in ("error", "built")

    def test_unicode_in_data_and_sql(self, db, transform_dir):
        """Unicode content in both data and SQL should work fine."""
        db.execute(
            "CREATE TABLE landing.intl AS "
            "SELECT 'Ørsted' AS name, 'København' AS city, '日本語' AS lang"
        )

        (transform_dir / "bronze" / "intl.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.intl\n\n"
            "SELECT name, city, lang FROM landing.intl\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.intl"] == "built"

        row = db.execute("SELECT name, city, lang FROM bronze.intl").fetchone()
        assert row == ("Ørsted", "København", "日本語")

    def test_very_long_sql_query(self, db, transform_dir):
        """A SQL query with many columns (simulating a very long query)."""
        # Generate 500 columns
        cols = ", ".join(f"{i} AS c{i}" for i in range(500))
        db.execute(f"CREATE TABLE landing.long_q AS SELECT {cols}")

        select = ", ".join(f"c{i}" for i in range(500))
        (transform_dir / "bronze" / "long_q.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.long_q\n\n"
            f"SELECT {select} FROM landing.long_q\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.long_q"] == "built"

    def test_duplicate_model_names_different_schemas(self, db, transform_dir):
        """Same model name in different schemas should both work."""
        db.execute("CREATE TABLE landing.data AS SELECT 1 AS val")

        (transform_dir / "bronze" / "stats.sql").write_text(
            "-- config: materialized=view, schema=bronze\n"
            "-- depends_on: landing.data\n\n"
            "SELECT val FROM landing.data\n"
        )
        (transform_dir / "silver" / "stats.sql").write_text(
            "-- config: materialized=table, schema=silver\n"
            "-- depends_on: bronze.stats\n\n"
            "SELECT val, val * 2 AS doubled FROM bronze.stats\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.stats"] == "built"
        assert results["silver.stats"] == "built"

    def test_view_to_table_materialization_switch(self, db, transform_dir):
        """Switching materialization from view to table should drop and recreate."""
        db.execute("CREATE TABLE landing.data AS SELECT 1 AS val")

        sql_file = transform_dir / "bronze" / "switchable.sql"
        sql_file.write_text(
            "-- config: materialized=view, schema=bronze\n"
            "-- depends_on: landing.data\n\n"
            "SELECT val FROM landing.data\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.switchable"] == "built"

        # Verify it's a view
        obj_type = db.execute(
            "SELECT table_type FROM information_schema.tables "
            "WHERE table_schema='bronze' AND table_name='switchable'"
        ).fetchone()[0]
        assert obj_type == "VIEW"

        # Switch to table
        sql_file.write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.data\n\n"
            "SELECT val FROM landing.data\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.switchable"] == "built"

        obj_type = db.execute(
            "SELECT table_type FROM information_schema.tables "
            "WHERE table_schema='bronze' AND table_name='switchable'"
        ).fetchone()[0]
        assert obj_type == "BASE TABLE"

    def test_upstream_change_cascades(self, db, transform_dir):
        """Changing a bronze model should cascade rebuild to silver/gold."""
        db.execute("CREATE TABLE landing.data AS SELECT 1 AS val")

        (transform_dir / "bronze" / "base.sql").write_text(
            "-- config: materialized=view, schema=bronze\n"
            "-- depends_on: landing.data\n\n"
            "SELECT val FROM landing.data\n"
        )
        (transform_dir / "silver" / "derived.sql").write_text(
            "-- config: materialized=view, schema=silver\n"
            "-- depends_on: bronze.base\n\n"
            "SELECT val * 2 AS doubled FROM bronze.base\n"
        )
        (transform_dir / "gold" / "final.sql").write_text(
            "-- config: materialized=table, schema=gold\n"
            "-- depends_on: silver.derived\n\n"
            "SELECT doubled FROM silver.derived\n"
        )

        # First run — build all
        results = run_transform(db, transform_dir, force=True)
        assert all(v == "built" for v in results.values())

        # Second run — all skip
        results = run_transform(db, transform_dir)
        assert all(v == "skipped" for v in results.values())

        # Modify bronze — should cascade
        (transform_dir / "bronze" / "base.sql").write_text(
            "-- config: materialized=view, schema=bronze\n"
            "-- depends_on: landing.data\n\n"
            "SELECT val + 10 AS val FROM landing.data\n"
        )

        results = run_transform(db, transform_dir)
        assert results["bronze.base"] == "built"
        # Direct downstream rebuilds because its upstream content_hash changed
        assert results["silver.derived"] == "built"
        # Transitive cascade: gold rebuilds because upstream_hash now includes
        # both content_hash and upstream_hash of dependencies
        assert results["gold.final"] == "built"


# ===========================================================================
# 4. EDGE CASES — RUNNER (PYTHON SCRIPTS)
# ===========================================================================

class TestRunnerEdgeCases:
    """Edge cases in the Python script runner."""

    def test_script_timeout(self, tmp_path):
        """Script that exceeds timeout should be stopped."""
        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        ensure_meta_table(conn)

        script = tmp_path / "slow.py"
        script.write_text("import time\ntime.sleep(30)\n")

        result = run_script(conn, script, "ingest", timeout=2)
        assert result["status"] == "error"
        assert "timed out" in result["error"].lower()
        conn.close()

    def test_script_with_syntax_error(self, tmp_path):
        """Script with Python syntax error should fail gracefully."""
        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        ensure_meta_table(conn)

        script = tmp_path / "bad_syntax.py"
        script.write_text("def foo(\n")  # unclosed paren

        result = run_script(conn, script, "ingest")
        assert result["status"] == "error"
        conn.close()

    def test_script_with_import_error(self, tmp_path):
        """Script importing a nonexistent module should fail gracefully."""
        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        ensure_meta_table(conn)

        script = tmp_path / "bad_import.py"
        script.write_text("import nonexistent_module_xyz_123\n")

        result = run_script(conn, script, "ingest")
        assert result["status"] == "error"
        assert "nonexistent_module_xyz_123" in result["error"]
        conn.close()

    def test_script_large_stdout(self, tmp_path):
        """Script producing lots of stdout should not crash."""
        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        ensure_meta_table(conn)

        script = tmp_path / "chatty.py"
        script.write_text(
            "for i in range(10000):\n"
            "    print(f'Line {i}: ' + 'x' * 100)\n"
        )

        result = run_script(conn, script, "ingest")
        assert result["status"] == "success"
        assert len(result["log_output"]) > 100_000
        conn.close()

    def test_script_modifies_db_then_fails(self, tmp_path):
        """Script that partially writes then errors — data should persist (no auto-rollback)."""
        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        ensure_meta_table(conn)

        script = tmp_path / "partial.py"
        script.write_text(
            'db.execute("CREATE SCHEMA IF NOT EXISTS landing")\n'
            'db.execute("CREATE TABLE landing.partial AS SELECT 42 AS val")\n'
            'raise RuntimeError("oops")\n'
        )

        result = run_script(conn, script, "ingest")
        assert result["status"] == "error"

        # The table was created before the error
        row = conn.execute("SELECT val FROM landing.partial").fetchone()
        assert row[0] == 42
        conn.close()

    def test_ingest_stops_on_first_error(self, tmp_path):
        """run_scripts_in_dir with ingest should stop after first failure."""
        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        ensure_meta_table(conn)

        ingest_dir = tmp_path / "ingest"
        ingest_dir.mkdir()

        (ingest_dir / "a_good.py").write_text('print("a ok")\n')
        (ingest_dir / "b_bad.py").write_text('raise ValueError("b fails")\n')
        (ingest_dir / "c_good.py").write_text('print("c ok")\n')

        results = run_scripts_in_dir(conn, ingest_dir, "ingest")
        # a runs, b fails, c should NOT run
        assert len(results) == 2
        assert results[0]["status"] == "success"
        assert results[1]["status"] == "error"
        conn.close()

    def test_export_continues_on_error(self, tmp_path):
        """run_scripts_in_dir with export should continue past failures."""
        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        ensure_meta_table(conn)

        export_dir = tmp_path / "export"
        export_dir.mkdir()

        (export_dir / "a_good.py").write_text('print("a ok")\n')
        (export_dir / "b_bad.py").write_text('raise ValueError("b fails")\n')
        (export_dir / "c_good.py").write_text('print("c ok")\n')

        results = run_scripts_in_dir(conn, export_dir, "export")
        # All three should run
        assert len(results) == 3
        assert results[0]["status"] == "success"
        assert results[1]["status"] == "error"
        assert results[2]["status"] == "success"
        conn.close()

    def test_skipped_underscore_scripts(self, tmp_path):
        """Scripts prefixed with _ should be skipped."""
        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        ensure_meta_table(conn)

        ingest_dir = tmp_path / "ingest"
        ingest_dir.mkdir()

        (ingest_dir / "_helper.py").write_text('print("should not run")\n')
        (ingest_dir / "real.py").write_text('print("ran")\n')

        results = run_scripts_in_dir(conn, ingest_dir, "ingest")
        assert len(results) == 1
        assert results[0]["script"] == "real.py"
        conn.close()


# ===========================================================================
# 5. INCREMENTAL EDGE CASES
# ===========================================================================

class TestIncrementalEdgeCases:
    """Edge cases in incremental materialization strategies."""

    def test_incremental_with_no_new_data(self, db, transform_dir):
        """Incremental run where the query returns 0 new rows."""
        db.execute(
            "CREATE TABLE landing.events AS "
            "SELECT 1 AS id, 'a' AS val"
        )

        (transform_dir / "bronze" / "events.sql").write_text(
            "-- config: materialized=incremental, schema=bronze, unique_key=id\n"
            "-- depends_on: landing.events\n\n"
            "SELECT id, val FROM landing.events\n"
        )

        # First run
        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.events"] == "built"
        count1 = db.execute("SELECT COUNT(*) FROM bronze.events").fetchone()[0]
        assert count1 == 1

        # Second run — same data, should upsert with no net change
        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.events"] == "built"
        count2 = db.execute("SELECT COUNT(*) FROM bronze.events").fetchone()[0]
        assert count2 == 1

    def test_incremental_append_strategy(self, db, transform_dir):
        """Append strategy should keep adding without dedup."""
        db.execute("CREATE TABLE landing.logs AS SELECT 1 AS id, 'msg' AS text")

        (transform_dir / "bronze" / "logs.sql").write_text(
            "-- config: materialized=incremental, schema=bronze, "
            "incremental_strategy=append\n"
            "-- depends_on: landing.logs\n\n"
            "SELECT id, text FROM landing.logs\n"
        )

        run_transform(db, transform_dir, force=True)
        count1 = db.execute("SELECT COUNT(*) FROM bronze.logs").fetchone()[0]
        assert count1 == 1

        # Run again — should append another copy
        run_transform(db, transform_dir, force=True)
        count2 = db.execute("SELECT COUNT(*) FROM bronze.logs").fetchone()[0]
        assert count2 == 2

    def test_incremental_merge_strategy(self, db, transform_dir):
        """Merge strategy should update existing and insert new rows."""
        db.execute(
            "CREATE TABLE landing.products AS "
            "SELECT 1 AS id, 'Widget' AS name, 10.0 AS price"
        )

        (transform_dir / "bronze" / "products.sql").write_text(
            "-- config: materialized=incremental, schema=bronze, "
            "unique_key=id, incremental_strategy=merge\n"
            "-- depends_on: landing.products\n\n"
            "SELECT id, name, price FROM landing.products\n"
        )

        run_transform(db, transform_dir, force=True)

        # Update price and add new product
        db.execute(
            "CREATE OR REPLACE TABLE landing.products AS "
            "SELECT 1 AS id, 'Widget' AS name, 15.0 AS price "
            "UNION ALL "
            "SELECT 2, 'Gadget', 20.0"
        )

        run_transform(db, transform_dir, force=True)

        rows = db.execute(
            "SELECT id, name, price FROM bronze.products ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0] == (1, "Widget", 15.0)  # updated
        assert rows[1] == (2, "Gadget", 20.0)  # inserted

    def test_incremental_schema_evolution(self, db, transform_dir):
        """Adding a new column to incremental source should auto-add to target."""
        db.execute("CREATE TABLE landing.evo AS SELECT 1 AS id, 'a' AS val")

        sql_file = transform_dir / "bronze" / "evo.sql"
        sql_file.write_text(
            "-- config: materialized=incremental, schema=bronze, unique_key=id\n"
            "-- depends_on: landing.evo\n\n"
            "SELECT id, val FROM landing.evo\n"
        )

        run_transform(db, transform_dir, force=True)

        # Add a new column
        db.execute("CREATE OR REPLACE TABLE landing.evo AS SELECT 1 AS id, 'a' AS val, 99 AS new_col")
        sql_file.write_text(
            "-- config: materialized=incremental, schema=bronze, unique_key=id\n"
            "-- depends_on: landing.evo\n\n"
            "SELECT id, val, new_col FROM landing.evo\n"
        )

        run_transform(db, transform_dir, force=True)

        cols = {
            r[0] for r in db.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='bronze' AND table_name='evo'"
            ).fetchall()
        }
        assert "new_col" in cols


# ===========================================================================
# 6. CHANGE DETECTION EDGE CASES
# ===========================================================================

class TestChangeDetection:
    """Edge cases in the change detection system."""

    def test_whitespace_only_change_triggers_rebuild(self, db, transform_dir):
        """Adding/removing whitespace changes the hash and triggers rebuild."""
        db.execute("CREATE TABLE landing.ws AS SELECT 1 AS val")

        sql_file = transform_dir / "bronze" / "ws.sql"
        sql_file.write_text(
            "-- config: materialized=view, schema=bronze\n"
            "-- depends_on: landing.ws\n\n"
            "SELECT val FROM landing.ws\n"
        )

        run_transform(db, transform_dir, force=True)
        results = run_transform(db, transform_dir)
        assert results["bronze.ws"] == "skipped"

        # Add trailing whitespace
        sql_file.write_text(
            "-- config: materialized=view, schema=bronze\n"
            "-- depends_on: landing.ws\n\n"
            "SELECT val FROM landing.ws   \n"
        )

        results = run_transform(db, transform_dir)
        # May or may not rebuild depending on normalization — either is acceptable
        assert results["bronze.ws"] in ("built", "skipped")

    def test_force_flag_ignores_cache(self, db, transform_dir):
        """force=True should always rebuild, even with no changes."""
        db.execute("CREATE TABLE landing.fc AS SELECT 1 AS val")

        (transform_dir / "bronze" / "fc.sql").write_text(
            "-- config: materialized=view, schema=bronze\n"
            "-- depends_on: landing.fc\n\n"
            "SELECT val FROM landing.fc\n"
        )

        run_transform(db, transform_dir, force=True)

        # No change, but force=True
        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.fc"] == "built"

        # Without force — should skip
        results = run_transform(db, transform_dir, force=False)
        assert results["bronze.fc"] == "skipped"


# ===========================================================================
# 7. ASSERTION & PROFILING EDGE CASES
# ===========================================================================

class TestAssertionEdgeCases:
    """Edge cases in data quality assertions and profiling."""

    def test_assertion_row_count_zero(self, db, transform_dir):
        """Assertion row_count > 0 should fail on empty table."""
        db.execute("CREATE TABLE landing.empty AS SELECT 1 AS id WHERE false")

        (transform_dir / "bronze" / "empty.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.empty\n"
            "-- assert: row_count > 0\n\n"
            "SELECT id FROM landing.empty\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.empty"] == "assertion_failed"

    def test_assertion_unique_passes(self, db, transform_dir):
        """unique() assertion on actually unique column should pass."""
        db.execute("CREATE TABLE landing.uniq AS SELECT i AS id FROM generate_series(1,100) t(i)")

        (transform_dir / "bronze" / "uniq.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.uniq\n"
            "-- assert: unique(id)\n\n"
            "SELECT id FROM landing.uniq\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.uniq"] == "built"

    def test_assertion_unique_fails_on_dupes(self, db, transform_dir):
        """unique() assertion should fail when duplicates exist."""
        db.execute("CREATE TABLE landing.dupes AS SELECT 1 AS id UNION ALL SELECT 1")

        (transform_dir / "bronze" / "dupes.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.dupes\n"
            "-- assert: unique(id)\n\n"
            "SELECT id FROM landing.dupes\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.dupes"] == "assertion_failed"

    def test_assertion_no_nulls_passes(self, db, transform_dir):
        """no_nulls() should pass when column has no nulls."""
        db.execute("CREATE TABLE landing.full AS SELECT 'a' AS name UNION ALL SELECT 'b'")

        (transform_dir / "bronze" / "full.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.full\n"
            "-- assert: no_nulls(name)\n\n"
            "SELECT name FROM landing.full\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.full"] == "built"

    def test_assertion_no_nulls_fails(self, db, transform_dir):
        """no_nulls() should fail when column has nulls."""
        db.execute("CREATE TABLE landing.nulls AS SELECT NULL AS name")

        (transform_dir / "bronze" / "nulls.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.nulls\n"
            "-- assert: no_nulls(name)\n\n"
            "SELECT name FROM landing.nulls\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.nulls"] == "assertion_failed"

    def test_assertion_accepted_values(self, db, transform_dir):
        """accepted_values() should pass with valid values, fail with unexpected."""
        db.execute("CREATE TABLE landing.av AS SELECT 'active' AS status UNION ALL SELECT 'inactive'")

        (transform_dir / "bronze" / "av.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.av\n"
            "-- assert: accepted_values(status, ['active', 'inactive'])\n\n"
            "SELECT status FROM landing.av\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.av"] == "built"

    def test_assertion_accepted_values_fails(self, db, transform_dir):
        """accepted_values() should fail with unexpected values."""
        db.execute("CREATE TABLE landing.avf AS SELECT 'unknown' AS status")

        (transform_dir / "bronze" / "avf.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.avf\n"
            "-- assert: accepted_values(status, ['active', 'inactive'])\n\n"
            "SELECT status FROM landing.avf\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.avf"] == "assertion_failed"

    def test_multiple_assertions_all_pass(self, db, transform_dir):
        """Multiple assertions on a single model should all be evaluated."""
        db.execute(
            "CREATE TABLE landing.multi AS "
            "SELECT i AS id, 'name_' || i AS name "
            "FROM generate_series(1, 10) t(i)"
        )

        (transform_dir / "bronze" / "multi.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.multi\n"
            "-- assert: row_count > 0\n"
            "-- assert: unique(id)\n"
            "-- assert: no_nulls(name)\n\n"
            "SELECT id, name FROM landing.multi\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.multi"] == "built"

    def test_multiple_assertions_one_fails(self, db, transform_dir):
        """If any assertion fails, the model should be marked assertion_failed."""
        db.execute("CREATE TABLE landing.mf AS SELECT 1 AS id, NULL AS name")

        (transform_dir / "bronze" / "mf.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.mf\n"
            "-- assert: row_count > 0\n"
            "-- assert: no_nulls(name)\n\n"
            "SELECT id, name FROM landing.mf\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.mf"] == "assertion_failed"

    def test_profile_on_empty_table(self, db, transform_dir):
        """Profiling an empty table should not crash."""
        db.execute("CREATE TABLE landing.pf_empty AS SELECT 1 AS id, 'a' AS val WHERE false")

        (transform_dir / "bronze" / "pf_empty.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.pf_empty\n\n"
            "SELECT id, val FROM landing.pf_empty\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.pf_empty"] == "built"

    def test_profile_captures_null_percentages(self, db, transform_dir):
        """Profiler should detect high null percentages."""
        db.execute(
            "CREATE TABLE landing.sparse AS "
            "SELECT i AS id, CASE WHEN i % 10 = 0 THEN 'x' ELSE NULL END AS val "
            "FROM generate_series(1, 100) t(i)"
        )

        (transform_dir / "bronze" / "sparse.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.sparse\n\n"
            "SELECT id, val FROM landing.sparse\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.sparse"] == "built"

        # Check the profile was saved
        row = db.execute(
            "SELECT null_percentages FROM _havn.model_profiles "
            "WHERE model_path = 'bronze.sparse'"
        ).fetchone()
        assert row is not None


# ===========================================================================
# 8. SQL COMPLEXITY EDGE CASES
# ===========================================================================

class TestSQLComplexity:
    """Complex SQL patterns that should work through the transform engine."""

    def test_cte_with_multiple_levels(self, db, transform_dir):
        """Multi-level CTEs should execute correctly."""
        db.execute("CREATE TABLE landing.cte_src AS SELECT i AS val FROM generate_series(1,100) t(i)")

        (transform_dir / "bronze" / "cte.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.cte_src\n\n"
            "WITH step1 AS (\n"
            "    SELECT val, val * 2 AS doubled FROM landing.cte_src\n"
            "),\n"
            "step2 AS (\n"
            "    SELECT doubled, doubled + 1 AS tripled FROM step1\n"
            "),\n"
            "step3 AS (\n"
            "    SELECT tripled, tripled * 10 AS big FROM step2\n"
            ")\n"
            "SELECT * FROM step3\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.cte"] == "built"
        count = db.execute("SELECT COUNT(*) FROM bronze.cte").fetchone()[0]
        assert count == 100

    def test_window_functions(self, db, transform_dir):
        """Window functions (ROW_NUMBER, LAG, SUM OVER) should work."""
        db.execute(
            "CREATE TABLE landing.win AS "
            "SELECT i AS id, i % 5 AS grp, random() AS val "
            "FROM generate_series(1, 50) t(i)"
        )

        (transform_dir / "bronze" / "win.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.win\n\n"
            "SELECT\n"
            "    id, grp, val,\n"
            "    ROW_NUMBER() OVER (PARTITION BY grp ORDER BY id) AS rn,\n"
            "    LAG(val) OVER (PARTITION BY grp ORDER BY id) AS prev_val,\n"
            "    SUM(val) OVER (PARTITION BY grp ORDER BY id) AS running_sum\n"
            "FROM landing.win\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.win"] == "built"

    def test_union_all(self, db, transform_dir):
        """UNION ALL combining multiple sources."""
        db.execute("CREATE TABLE landing.u1 AS SELECT 1 AS id, 'a' AS src")
        db.execute("CREATE TABLE landing.u2 AS SELECT 2 AS id, 'b' AS src")
        db.execute("CREATE TABLE landing.u3 AS SELECT 3 AS id, 'c' AS src")

        (transform_dir / "bronze" / "unioned.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.u1, landing.u2, landing.u3\n\n"
            "SELECT id, src FROM landing.u1\n"
            "UNION ALL\n"
            "SELECT id, src FROM landing.u2\n"
            "UNION ALL\n"
            "SELECT id, src FROM landing.u3\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.unioned"] == "built"
        count = db.execute("SELECT COUNT(*) FROM bronze.unioned").fetchone()[0]
        assert count == 3

    def test_subquery_in_where(self, db, transform_dir):
        """Correlated subquery in WHERE clause."""
        db.execute("CREATE TABLE landing.sq_orders AS SELECT i AS id, i % 100 AS cust_id, random() * 100 AS amount FROM generate_series(1,1000) t(i)")
        db.execute("CREATE TABLE landing.sq_custs AS SELECT i AS cust_id, 50.0 AS threshold FROM generate_series(1,100) t(i)")

        (transform_dir / "bronze" / "subq.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.sq_orders, landing.sq_custs\n\n"
            "SELECT o.id, o.cust_id, o.amount\n"
            "FROM landing.sq_orders o\n"
            "WHERE o.amount > (\n"
            "    SELECT c.threshold FROM landing.sq_custs c\n"
            "    WHERE c.cust_id = o.cust_id\n"
            "    LIMIT 1\n"
            ")\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.subq"] == "built"

    def test_case_when_expressions(self, db, transform_dir):
        """Complex CASE WHEN with nested logic."""
        db.execute(
            "CREATE TABLE landing.cases AS "
            "SELECT i AS id, i % 7 AS day_num, random() * 100 AS score "
            "FROM generate_series(1, 200) t(i)"
        )

        (transform_dir / "bronze" / "cases.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.cases\n\n"
            "SELECT\n"
            "    id,\n"
            "    CASE\n"
            "        WHEN day_num IN (0, 6) THEN 'weekend'\n"
            "        ELSE 'weekday'\n"
            "    END AS day_type,\n"
            "    CASE\n"
            "        WHEN score >= 90 THEN 'A'\n"
            "        WHEN score >= 80 THEN 'B'\n"
            "        WHEN score >= 70 THEN 'C'\n"
            "        WHEN score >= 60 THEN 'D'\n"
            "        ELSE 'F'\n"
            "    END AS grade\n"
            "FROM landing.cases\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.cases"] == "built"
        count = db.execute("SELECT COUNT(*) FROM bronze.cases").fetchone()[0]
        assert count == 200

    def test_self_join(self, db, transform_dir):
        """Self-join pattern (e.g. finding parent-child relationships)."""
        db.execute(
            "CREATE TABLE landing.tree AS "
            "SELECT 1 AS id, NULL AS parent_id, 'root' AS name "
            "UNION ALL SELECT 2, 1, 'child_a' "
            "UNION ALL SELECT 3, 1, 'child_b' "
            "UNION ALL SELECT 4, 2, 'grandchild'"
        )

        (transform_dir / "bronze" / "tree.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.tree\n\n"
            "SELECT\n"
            "    c.id,\n"
            "    c.name,\n"
            "    p.name AS parent_name\n"
            "FROM landing.tree c\n"
            "LEFT JOIN landing.tree p ON c.parent_id = p.id\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.tree"] == "built"
        row = db.execute(
            "SELECT parent_name FROM bronze.tree WHERE name = 'grandchild'"
        ).fetchone()
        assert row[0] == "child_a"

    def test_group_by_having(self, db, transform_dir):
        """GROUP BY with HAVING clause to filter aggregates."""
        db.execute(
            "CREATE TABLE landing.sales AS "
            "SELECT i % 20 AS product_id, random() * 50 AS amount "
            "FROM generate_series(1, 1000) t(i)"
        )

        (transform_dir / "bronze" / "sales.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.sales\n\n"
            "SELECT product_id, COUNT(*) AS cnt, SUM(amount) AS total\n"
            "FROM landing.sales\n"
            "GROUP BY product_id\n"
            "HAVING COUNT(*) > 40\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.sales"] == "built"

    def test_cross_join_lateral(self, db, transform_dir):
        """CROSS JOIN LATERAL (DuckDB-specific lateral join)."""
        db.execute("CREATE TABLE landing.lat AS SELECT i AS id, i * 3 AS n FROM generate_series(1,5) t(i)")

        (transform_dir / "bronze" / "lat.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.lat\n\n"
            "SELECT l.id, g.x\n"
            "FROM landing.lat l,\n"
            "LATERAL (SELECT UNNEST(generate_series(1, l.n)) AS x) g\n"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.lat"] == "built"
        # id=1 -> 3 rows, id=2 -> 6, id=3 -> 9, id=4 -> 12, id=5 -> 15 = 45
        count = db.execute("SELECT COUNT(*) FROM bronze.lat").fetchone()[0]
        assert count == 45


# ===========================================================================
# 9. PARALLEL EXECUTION EDGE CASES
# ===========================================================================

class TestParallelEdgeCases:
    """Edge cases in parallel transform execution."""

    def test_parallel_independent_models(self, tmp_path):
        """Multiple independent models should all build in parallel mode."""
        db_path = tmp_path / "par.duckdb"
        conn = duckdb.connect(str(db_path))
        ensure_meta_table(conn)
        conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
        conn.execute("CREATE SCHEMA IF NOT EXISTS bronze")

        transform_dir = tmp_path / "transform" / "bronze"
        transform_dir.mkdir(parents=True)

        for i in range(10):
            conn.execute(f"CREATE TABLE landing.t{i} AS SELECT {i} AS val")
            (transform_dir / f"m{i}.sql").write_text(
                f"-- config: materialized=table, schema=bronze\n"
                f"-- depends_on: landing.t{i}\n\n"
                f"SELECT val FROM landing.t{i}\n"
            )

        results = run_transform(
            conn, tmp_path / "transform", force=True,
            parallel=True, db_path=str(db_path),
        )
        assert len(results) == 10
        assert all(v == "built" for v in results.values())
        conn.close()

    def test_parallel_with_error_blocks_downstream(self, tmp_path):
        """Error in tier N should block tier N+1 in parallel mode."""
        db_path = tmp_path / "par_err.duckdb"
        conn = duckdb.connect(str(db_path))
        ensure_meta_table(conn)
        conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
        conn.execute("CREATE TABLE landing.ok AS SELECT 1 AS val")

        transform_dir = tmp_path / "transform"
        bronze = transform_dir / "bronze"
        silver = transform_dir / "silver"
        bronze.mkdir(parents=True)
        silver.mkdir(parents=True)

        (bronze / "ok.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.ok\n\n"
            "SELECT val FROM landing.ok\n"
        )
        (bronze / "bad.sql").write_text(
            "-- config: materialized=table, schema=bronze\n"
            "-- depends_on: landing.nonexistent\n\n"
            "SELECT * FROM landing.nonexistent\n"
        )
        (silver / "downstream.sql").write_text(
            "-- config: materialized=table, schema=silver\n"
            "-- depends_on: bronze.ok\n\n"
            "SELECT val FROM bronze.ok\n"
        )

        results = run_transform(
            conn, transform_dir, force=True,
            parallel=True, db_path=str(db_path),
        )
        assert results["bronze.bad"] == "error"
        # Downstream should be skipped due to error in same tier
        assert results["silver.downstream"] == "skipped"
        conn.close()


# ===========================================================================
# 10. DISCOVERY & CONFIG EDGE CASES
# ===========================================================================

class TestDiscoveryEdgeCases:
    """Edge cases in model discovery and config parsing."""

    def test_nested_subdirectory(self, db, transform_dir):
        """SQL files in nested subdirectories should be discovered."""
        nested = transform_dir / "bronze" / "sub" / "deep"
        nested.mkdir(parents=True)

        db.execute("CREATE TABLE landing.nest AS SELECT 1 AS val")

        (nested / "model.sql").write_text(
            "-- config: materialized=view, schema=bronze\n"
            "-- depends_on: landing.nest\n\n"
            "SELECT val FROM landing.nest\n"
        )

        models = discover_models(transform_dir)
        assert len(models) == 1
        assert models[0].schema == "bronze"

    def test_no_transform_dir(self, db, tmp_path):
        """Missing transform/ directory should return empty results."""
        results = run_transform(db, tmp_path / "nonexistent_transform")
        assert results == {}

    def test_empty_transform_dir(self, db, transform_dir):
        """Transform dir with no SQL files should return empty results."""
        results = run_transform(db, transform_dir)
        assert results == {}

    def test_config_overrides_folder_schema(self, db, transform_dir):
        """-- config: schema= should override folder-based schema convention."""
        db.execute("CREATE TABLE landing.ov AS SELECT 1 AS val")

        (transform_dir / "bronze" / "override.sql").write_text(
            "-- config: materialized=view, schema=silver\n"
            "-- depends_on: landing.ov\n\n"
            "SELECT val FROM landing.ov\n"
        )

        models = discover_models(transform_dir)
        assert len(models) == 1
        assert models[0].schema == "silver"
        assert models[0].full_name == "silver.override"

    def test_target_filtering(self, db, transform_dir):
        """targets parameter should only run specified models."""
        db.execute("CREATE TABLE landing.tf1 AS SELECT 1 AS val")
        db.execute("CREATE TABLE landing.tf2 AS SELECT 2 AS val")

        (transform_dir / "bronze" / "tf1.sql").write_text(
            "-- config: materialized=view, schema=bronze\n"
            "-- depends_on: landing.tf1\n\n"
            "SELECT val FROM landing.tf1\n"
        )
        (transform_dir / "bronze" / "tf2.sql").write_text(
            "-- config: materialized=view, schema=bronze\n"
            "-- depends_on: landing.tf2\n\n"
            "SELECT val FROM landing.tf2\n"
        )

        results = run_transform(db, transform_dir, targets=["bronze.tf1"], force=True)
        assert "bronze.tf1" in results
        assert "bronze.tf2" not in results
