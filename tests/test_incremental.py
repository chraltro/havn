"""Tests for incremental models."""

from __future__ import annotations

import textwrap
from pathlib import Path

import duckdb
import pytest

from havn.engine.database import ensure_meta_table
from havn.engine.sql_analysis import parse_config as _parse_config
from havn.engine.transform import (
    SQLModel,
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


class TestIncrementalModels:
    def test_parse_incremental_config(self):
        sql = "-- config: materialized=incremental, schema=silver, unique_key=id\nSELECT 1"
        config = _parse_config(sql)
        assert config["materialized"] == "incremental"
        assert config["unique_key"] == "id"

    def test_parse_incremental_strategy(self):
        sql = "-- config: materialized=incremental, schema=silver, unique_key=id, incremental_strategy=append\nSELECT 1"
        config = _parse_config(sql)
        assert config["incremental_strategy"] == "append"

    def test_incremental_first_run_creates_table(self, db, transform_dir):
        db.execute("CREATE TABLE landing.orders AS SELECT 1 AS id, 100 AS amount")
        (transform_dir / "silver" / "orders.sql").write_text(textwrap.dedent("""\
            -- config: materialized=incremental, schema=silver, unique_key=id
            -- depends_on: landing.orders

            SELECT id, amount FROM landing.orders
        """))
        results = run_transform(db, transform_dir, force=True)
        assert results["silver.orders"] == "built"
        row = db.execute("SELECT COUNT(*) FROM silver.orders").fetchone()
        assert row[0] == 1

    def test_incremental_upsert(self, db, transform_dir):
        db.execute("CREATE TABLE landing.orders AS SELECT 1 AS id, 100 AS amount")
        (transform_dir / "silver" / "orders.sql").write_text(textwrap.dedent("""\
            -- config: materialized=incremental, schema=silver, unique_key=id
            -- depends_on: landing.orders

            SELECT id, amount FROM landing.orders
        """))
        # First run
        run_transform(db, transform_dir, force=True)
        assert db.execute("SELECT COUNT(*) FROM silver.orders").fetchone()[0] == 1

        # Add new data and update existing
        db.execute("DELETE FROM landing.orders")
        db.execute("INSERT INTO landing.orders VALUES (1, 200)")  # updated amount
        db.execute("INSERT INTO landing.orders VALUES (2, 300)")  # new row

        # Second run — should upsert
        run_transform(db, transform_dir, force=True)
        rows = db.execute("SELECT * FROM silver.orders ORDER BY id").fetchall()
        assert len(rows) == 2
        assert rows[0] == (1, 200)  # updated
        assert rows[1] == (2, 300)  # new

    def test_incremental_append_only(self, db, transform_dir):
        """Without unique_key, incremental should append."""
        db.execute("CREATE TABLE landing.events AS SELECT 1 AS event_id, 'click' AS event_type")
        (transform_dir / "silver" / "events.sql").write_text(textwrap.dedent("""\
            -- config: materialized=incremental, schema=silver
            -- depends_on: landing.events

            SELECT event_id, event_type FROM landing.events
        """))
        # First run
        run_transform(db, transform_dir, force=True)
        assert db.execute("SELECT COUNT(*) FROM silver.events").fetchone()[0] == 1

        # Second run — should append (no unique key)
        run_transform(db, transform_dir, force=True)
        assert db.execute("SELECT COUNT(*) FROM silver.events").fetchone()[0] == 2

    def test_incremental_explicit_append_strategy(self, db, transform_dir):
        """incremental_strategy=append should always append."""
        db.execute("CREATE TABLE landing.logs AS SELECT 1 AS id, 'info' AS msg")
        (transform_dir / "silver" / "logs.sql").write_text(textwrap.dedent("""\
            -- config: materialized=incremental, schema=silver, unique_key=id, incremental_strategy=append
            -- depends_on: landing.logs

            SELECT id, msg FROM landing.logs
        """))
        run_transform(db, transform_dir, force=True)
        assert db.execute("SELECT COUNT(*) FROM silver.logs").fetchone()[0] == 1

        # Even with unique_key, append strategy should not dedup
        run_transform(db, transform_dir, force=True)
        assert db.execute("SELECT COUNT(*) FROM silver.logs").fetchone()[0] == 2

    def test_incremental_schema_evolution(self, db, transform_dir):
        """New columns in source should be auto-added to target."""
        db.execute("CREATE TABLE landing.evolve AS SELECT 1 AS id, 'alice' AS name")
        (transform_dir / "silver" / "evolve.sql").write_text(textwrap.dedent("""\
            -- config: materialized=incremental, schema=silver, unique_key=id
            -- depends_on: landing.evolve

            SELECT id, name FROM landing.evolve
        """))
        # First run
        run_transform(db, transform_dir, force=True)
        assert db.execute("SELECT COUNT(*) FROM silver.evolve").fetchone()[0] == 1

        # Add a new column to source
        db.execute("DROP TABLE landing.evolve")
        db.execute("CREATE TABLE landing.evolve AS SELECT 2 AS id, 'bob' AS name, 'bob@test.com' AS email")

        # Update the SQL to include the new column
        (transform_dir / "silver" / "evolve.sql").write_text(textwrap.dedent("""\
            -- config: materialized=incremental, schema=silver, unique_key=id
            -- depends_on: landing.evolve

            SELECT id, name, email FROM landing.evolve
        """))

        # Second run — should handle new column
        run_transform(db, transform_dir, force=True)
        rows = db.execute("SELECT * FROM silver.evolve ORDER BY id").fetchall()
        assert len(rows) == 2
        # Row 1 should have NULL for email (old row)
        assert rows[0][0] == 1
        assert rows[0][2] is None  # email
        # Row 2 should have email
        assert rows[1][0] == 2
        assert rows[1][2] == "bob@test.com"

    def test_incremental_with_filter(self, db, transform_dir):
        """incremental_filter should be applied on non-first runs."""
        db.execute("CREATE TABLE landing.ts_data AS SELECT 1 AS id, TIMESTAMP '2024-01-01' AS updated_at")
        (transform_dir / "silver" / "ts_data.sql").write_text(textwrap.dedent("""\
            -- config: materialized=incremental, schema=silver, unique_key=id, incremental_filter=WHERE updated_at > (SELECT MAX(updated_at) FROM {this})
            -- depends_on: landing.ts_data

            SELECT id, updated_at FROM landing.ts_data
        """))
        # First run — full load (filter not applied)
        run_transform(db, transform_dir, force=True)
        assert db.execute("SELECT COUNT(*) FROM silver.ts_data").fetchone()[0] == 1

    def test_incremental_duplicate_keys_in_staging(self, db, transform_dir):
        """Duplicate keys in source data should work (last write wins)."""
        db.execute(
            "CREATE TABLE landing.dupes AS "
            "SELECT 1 AS id, 100 AS amount "
            "UNION ALL SELECT 1, 200"
        )
        (transform_dir / "silver" / "dupes.sql").write_text(textwrap.dedent("""\
            -- config: materialized=incremental, schema=silver, unique_key=id
            -- depends_on: landing.dupes

            SELECT id, amount FROM landing.dupes
        """))
        # First run — creates table with both rows (dupes in first load)
        run_transform(db, transform_dir, force=True)
        count = db.execute("SELECT COUNT(*) FROM silver.dupes").fetchone()[0]
        assert count == 2  # Both rows are inserted on first load


class TestIncrementalTransaction:
    """The staging branch must be all-or-nothing.

    ALTER / DELETE / UPDATE / INSERT used to run as separate auto-commit
    statements, so a failing INSERT landed after an already-committed DELETE
    and the target lost the rows the run was meant to replace.
    """

    def test_failed_insert_does_not_lose_rows(self, db, transform_dir):
        db.execute(
            "CREATE TABLE landing.orders AS "
            "SELECT 1 AS id, 100 AS amount UNION ALL SELECT 2, 200"
        )
        (transform_dir / "silver" / "orders.sql").write_text(textwrap.dedent("""\
            @config materialized=incremental, schema=silver, unique_key=id
            @depends_on landing.orders

            SELECT id, amount FROM landing.orders
        """))
        run_transform(db, transform_dir, force=True)
        assert db.execute("SELECT COUNT(*) FROM silver.orders").fetchone()[0] == 2

        # Retype the source column to VARCHAR with a value that cannot be cast
        # back to the target's INTEGER column. The DELETE matches id=1, the
        # INSERT that should put it back then fails.
        db.execute("DROP TABLE landing.orders")
        db.execute(
            "CREATE TABLE landing.orders AS SELECT 1 AS id, 'not-a-number' AS amount"
        )

        results = run_transform(db, transform_dir, force=True)
        assert results["silver.orders"] == "error"

        rows = db.execute("SELECT id, amount FROM silver.orders ORDER BY id").fetchall()
        assert rows == [(1, 100), (2, 200)]

    def test_failed_insert_rolls_back_added_column(self, db, transform_dir):
        """Schema-evolution ALTERs belong to the same unit of work."""
        db.execute("CREATE TABLE landing.events AS SELECT 1 AS id, 10 AS qty")
        (transform_dir / "silver" / "events.sql").write_text(textwrap.dedent("""\
            @config materialized=incremental, schema=silver, unique_key=id
            @depends_on landing.events

            SELECT id, qty FROM landing.events
        """))
        run_transform(db, transform_dir, force=True)
        assert db.execute("SELECT COUNT(*) FROM silver.events").fetchone()[0] == 1

        # The new `note` column triggers an ALTER; `qty` turns into an
        # uncastable string so the INSERT after the ALTER fails.
        db.execute("DROP TABLE landing.events")
        db.execute(
            "CREATE TABLE landing.events AS "
            "SELECT 1 AS id, 'many' AS qty, 'hello' AS note"
        )
        results = run_transform(db, transform_dir, force=True)
        assert results["silver.events"] == "error"

        cols = {
            r[0]
            for r in db.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'silver' AND table_name = 'events'"
            ).fetchall()
        }
        assert "note" not in cols
        assert db.execute("SELECT id, qty FROM silver.events").fetchall() == [(1, 10)]

    def test_successful_incremental_still_commits(self, db, transform_dir):
        """The happy path must still land its writes."""
        db.execute("CREATE TABLE landing.sales AS SELECT 1 AS id, 100 AS amount")
        (transform_dir / "silver" / "sales.sql").write_text(textwrap.dedent("""\
            @config materialized=incremental, schema=silver, unique_key=id
            @depends_on landing.sales

            SELECT id, amount FROM landing.sales
        """))
        run_transform(db, transform_dir, force=True)

        db.execute("DROP TABLE landing.sales")
        db.execute(
            "CREATE TABLE landing.sales AS "
            "SELECT 1 AS id, 150 AS amount UNION ALL SELECT 2, 300"
        )
        results = run_transform(db, transform_dir, force=True)
        assert results["silver.sales"] == "built"
        rows = db.execute("SELECT id, amount FROM silver.sales ORDER BY id").fetchall()
        assert rows == [(1, 150), (2, 300)]


# --------------------------------------------------------------------------
# on_schema_change
# --------------------------------------------------------------------------

STRATEGIES = ["delete+insert", "merge"]

# The starting shape of every on_schema_change scenario: one row, three
# columns, `qty` an INTEGER so a DOUBLE arriving later is a lossy retype.
RUN1 = "SELECT 1 AS id, 'a' AS name, 10 AS qty"

# The four kinds of change, each expressed as the second run's source query.
ADDED = "SELECT 2 AS id, 'b' AS name, 20 AS qty, 'x' AS extra"
REMOVED = "SELECT 2 AS id, 'b' AS name"
RETYPED_LOSSLESS = "SELECT 2 AS id, 'b' AS name, 20::SMALLINT AS qty"
RETYPED_LOSSY = "SELECT 2 AS id, 'b' AS name, 20.5::DOUBLE AS qty"


def _write_incremental(transform_dir, name, policy, strategy):
    """Write a `SELECT *` incremental model so the source shape drives staging."""
    config = (
        f"@config materialized=incremental, schema=silver, unique_key=id, "
        f"incremental_strategy={strategy}, on_schema_change={policy}"
    )
    (transform_dir / "silver" / f"{name}.sql").write_text(
        f"{config}\n@depends_on landing.{name}\n\nSELECT * FROM landing.{name}\n"
    )


def _first_run(db, transform_dir, name, policy, strategy):
    db.execute(f"CREATE OR REPLACE TABLE landing.{name} AS {RUN1}")
    _write_incremental(transform_dir, name, policy, strategy)
    results = run_transform(db, transform_dir, force=True)
    assert results[f"silver.{name}"] == "built"


def _second_run(db, transform_dir, name, source_sql):
    db.execute(f"CREATE OR REPLACE TABLE landing.{name} AS {source_sql}")
    return run_transform(db, transform_dir, force=True)


def _columns(db, name):
    return [
        (r[0], r[1])
        for r in db.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'silver' AND table_name = ? "
            "ORDER BY ordinal_position",
            [name],
        ).fetchall()
    ]


def _assert_untouched(db, name):
    """The target still holds exactly what the first run put there."""
    assert _columns(db, name) == [
        ("id", "INTEGER"), ("name", "VARCHAR"), ("qty", "INTEGER")
    ]
    assert db.execute(f"SELECT id, name, qty FROM silver.{name}").fetchall() == [
        (1, "a", 10)
    ]


def _last_error(db, name):
    return db.execute(
        "SELECT error FROM _havn.run_log WHERE target = ? AND status = 'error' "
        "ORDER BY started_at DESC LIMIT 1",
        [f"silver.{name}"],
    ).fetchone()[0]


class TestOnSchemaChangeAppendNewColumns:
    """The default policy: add new columns, refuse the two silent corruptions."""

    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_added_column_is_appended(self, db, transform_dir, strategy):
        _first_run(db, transform_dir, "m", "append_new_columns", strategy)
        results = _second_run(db, transform_dir, "m", ADDED)
        assert results["silver.m"] == "built"
        assert [c for c, _ in _columns(db, "m")] == ["id", "name", "qty", "extra"]
        rows = db.execute("SELECT id, extra FROM silver.m ORDER BY id").fetchall()
        # No option backfills the old row, exactly like dbt.
        assert rows == [(1, None), (2, "x")]

    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_removed_column_errors_and_leaves_target_alone(
        self, db, transform_dir, strategy
    ):
        _first_run(db, transform_dir, "m", "append_new_columns", strategy)
        results = _second_run(db, transform_dir, "m", REMOVED)
        assert results["silver.m"] == "error"
        _assert_untouched(db, "m")
        err = _last_error(db, "m")
        assert "qty" in err
        assert "NULL" in err

    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_retyped_lossless_still_errors(self, db, transform_dir, strategy):
        """append_new_columns is about columns, not types: any retype stops."""
        _first_run(db, transform_dir, "m", "append_new_columns", strategy)
        results = _second_run(db, transform_dir, "m", RETYPED_LOSSLESS)
        assert results["silver.m"] == "error"
        _assert_untouched(db, "m")

    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_retyped_lossy_errors_naming_both_types(
        self, db, transform_dir, strategy
    ):
        _first_run(db, transform_dir, "m", "append_new_columns", strategy)
        results = _second_run(db, transform_dir, "m", RETYPED_LOSSY)
        assert results["silver.m"] == "error"
        _assert_untouched(db, "m")
        err = _last_error(db, "m")
        assert "'qty'" in err
        assert "INTEGER" in err and "DOUBLE" in err
        assert "20.5" in err  # the silent-rounding hint
        assert "sync_all_columns" in err


class TestOnSchemaChangeIgnore:
    """No ALTER at all: write the intersection, keep the target's types."""

    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_added_column_is_dropped_on_the_floor(
        self, db, transform_dir, strategy
    ):
        _first_run(db, transform_dir, "m", "ignore", strategy)
        results = _second_run(db, transform_dir, "m", ADDED)
        assert results["silver.m"] == "built"
        assert [c for c, _ in _columns(db, "m")] == ["id", "name", "qty"]
        assert db.execute(
            "SELECT id, name, qty FROM silver.m ORDER BY id"
        ).fetchall() == [(1, "a", 10), (2, "b", 20)]

    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_removed_column_keeps_the_target_column(
        self, db, transform_dir, strategy
    ):
        _first_run(db, transform_dir, "m", "ignore", strategy)
        results = _second_run(db, transform_dir, "m", REMOVED)
        assert results["silver.m"] == "built"
        assert [c for c, _ in _columns(db, "m")] == ["id", "name", "qty"]
        # The new row gets NULL for the column the query stopped producing.
        assert db.execute(
            "SELECT id, name, qty FROM silver.m ORDER BY id"
        ).fetchall() == [(1, "a", 10), (2, "b", None)]

    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_retyped_lossless_is_accepted(self, db, transform_dir, strategy):
        _first_run(db, transform_dir, "m", "ignore", strategy)
        results = _second_run(db, transform_dir, "m", RETYPED_LOSSLESS)
        assert results["silver.m"] == "built"
        assert _columns(db, "m")[2] == ("qty", "INTEGER")
        assert db.execute(
            "SELECT id, qty FROM silver.m ORDER BY id"
        ).fetchall() == [(1, 10), (2, 20)]

    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_retyped_lossy_errors(self, db, transform_dir, strategy):
        _first_run(db, transform_dir, "m", "ignore", strategy)
        results = _second_run(db, transform_dir, "m", RETYPED_LOSSY)
        assert results["silver.m"] == "error"
        _assert_untouched(db, "m")


class TestOnSchemaChangeFail:
    """Any difference stops the run before the target is touched."""

    @pytest.mark.parametrize("strategy", STRATEGIES)
    @pytest.mark.parametrize(
        "source_sql", [ADDED, REMOVED, RETYPED_LOSSLESS, RETYPED_LOSSY]
    )
    def test_every_change_kind_errors(
        self, db, transform_dir, strategy, source_sql
    ):
        _first_run(db, transform_dir, "m", "fail", strategy)
        results = _second_run(db, transform_dir, "m", source_sql)
        assert results["silver.m"] == "error"
        _assert_untouched(db, "m")

    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_no_change_still_runs(self, db, transform_dir, strategy):
        _first_run(db, transform_dir, "m", "fail", strategy)
        results = _second_run(
            db, transform_dir, "m", "SELECT 2 AS id, 'b' AS name, 20 AS qty"
        )
        assert results["silver.m"] == "built"
        assert db.execute("SELECT COUNT(*) FROM silver.m").fetchone()[0] == 2


class TestAppendHonoursOnSchemaChange:
    """append used to write positionally and skip the policy entirely.

    ``INSERT INTO target <query>`` matches columns by position, so reordering
    a projection quietly wrote each value into its neighbour's column, and a
    column added, dropped or retyped never reached _plan_schema_change.
    """

    def test_reordered_projection_lands_in_the_right_columns(
        self, db, transform_dir
    ):
        _first_run(db, transform_dir, "m", "append_new_columns", "append")

        results = _second_run(
            db, transform_dir, "m", "SELECT 20 AS qty, 'b' AS name, 2 AS id"
        )

        assert results["silver.m"] == "built"
        assert [c for c, _ in _columns(db, "m")] == ["id", "name", "qty"]
        assert db.execute(
            "SELECT id, name, qty FROM silver.m ORDER BY id"
        ).fetchall() == [(1, "a", 10), (2, "b", 20)]

    def test_fail_policy_refuses_a_dropped_column_before_any_write(
        self, db, transform_dir
    ):
        _first_run(db, transform_dir, "m", "fail", "append")

        results = _second_run(db, transform_dir, "m", REMOVED)

        assert results["silver.m"] == "error"
        _assert_untouched(db, "m")
        assert "on_schema_change=fail" in _last_error(db, "m")

    def test_added_column_is_appended(self, db, transform_dir):
        _first_run(db, transform_dir, "m", "append_new_columns", "append")

        results = _second_run(db, transform_dir, "m", ADDED)

        assert results["silver.m"] == "built"
        assert [c for c, _ in _columns(db, "m")] == ["id", "name", "qty", "extra"]
        assert db.execute(
            "SELECT id, extra FROM silver.m ORDER BY id"
        ).fetchall() == [(1, None), (2, "x")]

    def test_default_policy_refuses_a_removed_column(self, db, transform_dir):
        _first_run(db, transform_dir, "m", "append_new_columns", "append")

        results = _second_run(db, transform_dir, "m", REMOVED)

        assert results["silver.m"] == "error"
        _assert_untouched(db, "m")

    def test_ignore_writes_only_the_shared_columns(self, db, transform_dir):
        _first_run(db, transform_dir, "m", "ignore", "append")

        results = _second_run(db, transform_dir, "m", ADDED)

        assert results["silver.m"] == "built"
        assert [c for c, _ in _columns(db, "m")] == ["id", "name", "qty"]
        assert db.execute(
            "SELECT id, name, qty FROM silver.m ORDER BY id"
        ).fetchall() == [(1, "a", 10), (2, "b", 20)]

    def test_a_model_without_a_unique_key_takes_the_same_path(
        self, db, transform_dir
    ):
        """No unique_key shares the append branch, so it shares the fix."""
        db.execute(f"CREATE OR REPLACE TABLE landing.n AS {RUN1}")
        (transform_dir / "silver" / "n.sql").write_text(
            "@config materialized=incremental, schema=silver\n"
            "@depends_on landing.n\n\nSELECT * FROM landing.n\n"
        )
        assert run_transform(db, transform_dir, force=True)["silver.n"] == "built"

        db.execute(
            "CREATE OR REPLACE TABLE landing.n AS "
            "SELECT 20 AS qty, 'b' AS name, 2 AS id"
        )
        results = run_transform(db, transform_dir, force=True)

        assert results["silver.n"] == "built"
        assert db.execute(
            "SELECT id, name, qty FROM silver.n ORDER BY id"
        ).fetchall() == [(1, "a", 10), (2, "b", 20)]


class TestOnSchemaChangeSyncAllColumns:
    """Add, drop and retype the target so it matches the query."""

    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_added_column_is_appended(self, db, transform_dir, strategy):
        _first_run(db, transform_dir, "m", "sync_all_columns", strategy)
        results = _second_run(db, transform_dir, "m", ADDED)
        assert results["silver.m"] == "built"
        assert [c for c, _ in _columns(db, "m")] == ["id", "name", "qty", "extra"]

    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_removed_column_is_dropped(self, db, transform_dir, strategy):
        _first_run(db, transform_dir, "m", "sync_all_columns", strategy)
        results = _second_run(db, transform_dir, "m", REMOVED)
        assert results["silver.m"] == "built"
        assert [c for c, _ in _columns(db, "m")] == ["id", "name"]
        assert db.execute(
            "SELECT id, name FROM silver.m ORDER BY id"
        ).fetchall() == [(1, "a"), (2, "b")]

    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_retyped_lossless_alters_the_column(
        self, db, transform_dir, strategy
    ):
        _first_run(db, transform_dir, "m", "sync_all_columns", strategy)
        results = _second_run(db, transform_dir, "m", RETYPED_LOSSLESS)
        assert results["silver.m"] == "built"
        assert _columns(db, "m")[2] == ("qty", "SMALLINT")

    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_retyped_lossy_alters_instead_of_rounding(
        self, db, transform_dir, strategy
    ):
        _first_run(db, transform_dir, "m", "sync_all_columns", strategy)
        results = _second_run(db, transform_dir, "m", RETYPED_LOSSY)
        assert results["silver.m"] == "built"
        assert _columns(db, "m")[2] == ("qty", "DOUBLE")
        # 20.5 survives instead of being rounded to 21.
        assert db.execute(
            "SELECT id, qty FROM silver.m ORDER BY id"
        ).fetchall() == [(1, 10.0), (2, 20.5)]

    def test_index_on_a_dropped_column_gives_a_clear_error(
        self, db, transform_dir
    ):
        _first_run(db, transform_dir, "m", "sync_all_columns", "delete+insert")
        db.execute("CREATE INDEX m_qty_idx ON silver.m(qty)")
        results = _second_run(db, transform_dir, "m", REMOVED)
        assert results["silver.m"] == "error"
        err = _last_error(db, "m")
        assert "index" in err.lower()
        assert "'qty'" in err

    def test_actions_are_recorded_in_the_run_log(self, db, transform_dir):
        _first_run(db, transform_dir, "m", "sync_all_columns", "delete+insert")
        _second_run(
            db, transform_dir, "m", "SELECT 2 AS id, 'b' AS name, 'x' AS extra"
        )
        log_output = db.execute(
            "SELECT log_output FROM _havn.run_log WHERE target = 'silver.m' "
            "AND status = 'success' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()[0]
        assert "added column extra" in log_output
        assert "dropped column qty" in log_output


class TestOnSchemaChangeHashing:
    def test_content_hash_changes_with_the_policy(self):
        def _model(policy):
            return SQLModel(
                path=Path("silver/m.sql"),
                name="m",
                schema="silver",
                full_name="silver.m",
                sql="SELECT 1 AS id",
                query="SELECT 1 AS id",
                materialized="incremental",
                unique_key="id",
                on_schema_change=policy,
            )

        default = _model("append_new_columns")
        assert default.content_hash == _model("append_new_columns").content_hash
        for policy in ("ignore", "fail", "sync_all_columns"):
            assert _model(policy).content_hash != default.content_hash
