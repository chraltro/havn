"""Tests for ephemeral models: inlined into consumers, never materialized."""

from __future__ import annotations

import textwrap

import duckdb
import pytest

from havn.engine.database import ensure_meta_table
from havn.engine.transform import (
    build_dag,
    discover_models,
    extract_column_lineage,
    inline_ephemeral,
    run_transform,
    validate_models,
)
from havn.engine.transform.inline import EphemeralInlineError


@pytest.fixture
def db(tmp_path):
    conn = duckdb.connect(str(tmp_path / "test.duckdb"))
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


def _write(transform_dir, schema, name, body):
    (transform_dir / schema / f"{name}.sql").write_text(textwrap.dedent(body))


def _map(transform_dir):
    models = build_dag(discover_models(transform_dir))
    return models, {m.full_name: m for m in models}


def _objects(db, schema):
    return {
        r[0]
        for r in db.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = ?",
            [schema],
        ).fetchall()
    }


class TestEphemeralConsumers:
    def test_consumed_by_a_view(self, db, transform_dir):
        db.execute("CREATE TABLE landing.raw AS SELECT 1 AS id, 10 AS amount")
        _write(transform_dir, "silver", "base", """\
            @config materialized=ephemeral, schema=silver
            @depends_on landing.raw

            SELECT id, amount * 2 AS doubled FROM landing.raw
        """)
        _write(transform_dir, "gold", "report", """\
            @config materialized=view, schema=gold

            SELECT id, doubled FROM silver.base
        """)
        results = run_transform(db, transform_dir, force=True)
        assert results["silver.base"] == "inlined"
        assert results["gold.report"] == "built"
        # Nothing was materialized for the ephemeral model.
        assert "base" not in _objects(db, "silver")
        assert db.execute("SELECT id, doubled FROM gold.report").fetchall() == [(1, 20)]

    def test_consumed_by_a_table(self, db, transform_dir):
        db.execute("CREATE TABLE landing.raw AS SELECT 1 AS id, 10 AS amount")
        _write(transform_dir, "silver", "base", """\
            @config materialized=ephemeral, schema=silver
            @depends_on landing.raw

            SELECT id, amount FROM landing.raw WHERE amount > 5
        """)
        _write(transform_dir, "gold", "report", """\
            @config materialized=table, schema=gold

            SELECT id, amount FROM silver.base
        """)
        results = run_transform(db, transform_dir, force=True)
        assert results["gold.report"] == "built"
        assert "base" not in _objects(db, "silver")
        assert db.execute("SELECT * FROM gold.report").fetchall() == [(1, 10)]

    def test_consumed_by_an_incremental(self, db, transform_dir):
        db.execute("CREATE TABLE landing.raw AS SELECT 1 AS id, 10 AS amount")
        _write(transform_dir, "silver", "base", """\
            @config materialized=ephemeral, schema=silver
            @depends_on landing.raw

            SELECT id, amount FROM landing.raw
        """)
        _write(transform_dir, "gold", "facts", """\
            @config materialized=incremental, schema=gold, unique_key=id

            SELECT id, amount FROM silver.base
        """)
        assert run_transform(db, transform_dir, force=True)["gold.facts"] == "built"
        assert db.execute("SELECT * FROM gold.facts").fetchall() == [(1, 10)]

        # A second run has to inline the ephemeral into the staging query too.
        db.execute("DROP TABLE landing.raw")
        db.execute(
            "CREATE TABLE landing.raw AS "
            "SELECT 1 AS id, 99 AS amount UNION ALL SELECT 2, 20"
        )
        assert run_transform(db, transform_dir, force=True)["gold.facts"] == "built"
        assert db.execute(
            "SELECT id, amount FROM gold.facts ORDER BY id"
        ).fetchall() == [(1, 99), (2, 20)]

    def test_two_level_chain(self, db, transform_dir):
        db.execute("CREATE TABLE landing.raw AS SELECT 1 AS id, 10 AS amount")
        _write(transform_dir, "bronze", "clean", """\
            @config materialized=ephemeral, schema=bronze
            @depends_on landing.raw

            SELECT id, amount FROM landing.raw
        """)
        _write(transform_dir, "silver", "scaled", """\
            @config materialized=ephemeral, schema=silver

            SELECT id, amount * 3 AS amount FROM bronze.clean
        """)
        _write(transform_dir, "gold", "report", """\
            @config materialized=table, schema=gold

            SELECT id, amount FROM silver.scaled
        """)
        results = run_transform(db, transform_dir, force=True)
        assert results["bronze.clean"] == "inlined"
        assert results["silver.scaled"] == "inlined"
        assert results["gold.report"] == "built"
        assert db.execute("SELECT * FROM gold.report").fetchall() == [(1, 30)]

    def test_ephemeral_with_its_own_ctes(self, db, transform_dir):
        db.execute("CREATE TABLE landing.raw AS SELECT 1 AS id, 10 AS amount")
        _write(transform_dir, "silver", "base", """\
            @config materialized=ephemeral, schema=silver
            @depends_on landing.raw

            WITH src AS (
                SELECT id, amount FROM landing.raw
            ), scaled AS (
                SELECT id, amount * 2 AS amount FROM src
            )
            SELECT id, amount FROM scaled
        """)
        # The consumer defines a CTE with the SAME name, which must not clash.
        _write(transform_dir, "gold", "report", """\
            @config materialized=table, schema=gold

            WITH src AS (
                SELECT id, amount FROM silver.base
            )
            SELECT id, amount + 1 AS amount FROM src
        """)
        results = run_transform(db, transform_dir, force=True)
        assert results["gold.report"] == "built"
        assert db.execute("SELECT * FROM gold.report").fetchall() == [(1, 21)]

    def test_alias_is_preserved(self, db, transform_dir):
        db.execute("CREATE TABLE landing.raw AS SELECT 1 AS id, 10 AS amount")
        db.execute("CREATE TABLE landing.names AS SELECT 1 AS id, 'a' AS label")
        _write(transform_dir, "silver", "base", """\
            @config materialized=ephemeral, schema=silver
            @depends_on landing.raw

            SELECT id, amount FROM landing.raw
        """)
        _write(transform_dir, "gold", "report", """\
            @config materialized=table, schema=gold
            @depends_on landing.names

            SELECT b.id, b.amount, n.label
            FROM silver.base b
            JOIN landing.names n ON n.id = b.id
        """)
        results = run_transform(db, transform_dir, force=True)
        assert results["gold.report"] == "built"
        assert db.execute("SELECT * FROM gold.report").fetchall() == [(1, 10, "a")]

    def test_unaliased_reference_keeps_its_implicit_name(self, db, transform_dir):
        """`silver.base` unaliased is still addressable as `base.col`."""
        db.execute("CREATE TABLE landing.raw AS SELECT 1 AS id, 10 AS amount")
        _write(transform_dir, "silver", "base", """\
            @config materialized=ephemeral, schema=silver
            @depends_on landing.raw

            SELECT id, amount FROM landing.raw
        """)
        _write(transform_dir, "gold", "report", """\
            @config materialized=table, schema=gold

            SELECT base.id, base.amount FROM silver.base
        """)
        results = run_transform(db, transform_dir, force=True)
        assert results["gold.report"] == "built"
        assert db.execute("SELECT * FROM gold.report").fetchall() == [(1, 10)]

    def test_parallel_run_inlines_too(self, db, transform_dir, tmp_path):
        db.execute("CREATE TABLE landing.raw AS SELECT 1 AS id, 10 AS amount")
        _write(transform_dir, "silver", "base", """\
            @config materialized=ephemeral, schema=silver
            @depends_on landing.raw

            SELECT id, amount FROM landing.raw
        """)
        for name in ("one", "two"):
            _write(transform_dir, "gold", name, """\
                @config materialized=table, schema=gold

                SELECT id, amount FROM silver.base
            """)
        results = run_transform(
            db, transform_dir, force=True, parallel=True,
            db_path=str(tmp_path / "test.duckdb"),
        )
        assert results["silver.base"] == "inlined"
        assert results["gold.one"] == "built"
        assert results["gold.two"] == "built"


class TestEphemeralOrphans:
    def test_switching_to_ephemeral_drops_the_orphan_table(self, db, transform_dir):
        db.execute("CREATE TABLE landing.raw AS SELECT 1 AS id, 10 AS amount")
        _write(transform_dir, "silver", "base", """\
            @config materialized=table, schema=silver
            @depends_on landing.raw

            SELECT id, amount FROM landing.raw
        """)
        run_transform(db, transform_dir, force=True)
        assert "base" in _objects(db, "silver")

        _write(transform_dir, "silver", "base", """\
            @config materialized=ephemeral, schema=silver
            @depends_on landing.raw

            SELECT id, amount FROM landing.raw
        """)
        results = run_transform(db, transform_dir, force=True)
        assert results["silver.base"] == "inlined"
        assert "base" not in _objects(db, "silver")

    def test_switching_to_ephemeral_drops_the_orphan_view(self, db, transform_dir):
        db.execute("CREATE TABLE landing.raw AS SELECT 1 AS id, 10 AS amount")
        _write(transform_dir, "silver", "base", """\
            @config materialized=view, schema=silver
            @depends_on landing.raw

            SELECT id, amount FROM landing.raw
        """)
        run_transform(db, transform_dir, force=True)
        assert "base" in _objects(db, "silver")

        _write(transform_dir, "silver", "base", """\
            @config materialized=ephemeral, schema=silver
            @depends_on landing.raw

            SELECT id, amount FROM landing.raw
        """)
        run_transform(db, transform_dir, force=True)
        assert "base" not in _objects(db, "silver")


class TestEphemeralState:
    def test_model_state_row_is_honest(self, db, transform_dir):
        db.execute("CREATE TABLE landing.raw AS SELECT 1 AS id")
        _write(transform_dir, "silver", "base", """\
            @config materialized=ephemeral, schema=silver
            @depends_on landing.raw

            SELECT id FROM landing.raw
        """)
        run_transform(db, transform_dir, force=True)
        row = db.execute(
            "SELECT materialized_as, row_count FROM _havn.model_state "
            "WHERE model_path = 'silver.base'"
        ).fetchone()
        assert row == ("ephemeral", 0)

    def test_editing_an_ephemeral_rebuilds_its_consumer(self, db, transform_dir):
        db.execute("CREATE TABLE landing.raw AS SELECT 1 AS id, 10 AS amount")
        _write(transform_dir, "silver", "base", """\
            @config materialized=ephemeral, schema=silver
            @depends_on landing.raw

            SELECT id, amount FROM landing.raw
        """)
        _write(transform_dir, "gold", "report", """\
            @config materialized=table, schema=gold

            SELECT id, amount FROM silver.base
        """)
        assert run_transform(db, transform_dir)["gold.report"] == "built"
        # Nothing changed: the consumer is skipped.
        assert run_transform(db, transform_dir)["gold.report"] == "skipped"

        # Editing the ephemeral must invalidate the consumer through
        # _compute_upstream_hash, and the new value must land.
        _write(transform_dir, "silver", "base", """\
            @config materialized=ephemeral, schema=silver
            @depends_on landing.raw

            SELECT id, amount * 5 AS amount FROM landing.raw
        """)
        assert run_transform(db, transform_dir)["gold.report"] == "built"
        assert db.execute("SELECT amount FROM gold.report").fetchall() == [(50,)]


class TestEphemeralValidation:
    def test_assertions_are_rejected(self, transform_dir):
        _write(transform_dir, "silver", "base", """\
            @config materialized=ephemeral, schema=silver
            @assert row_count > 0

            SELECT 1 AS id
        """)
        models = discover_models(transform_dir)
        errors = validate_models(None, models)
        bad = [e for e in errors if "assertions cannot run on ephemeral" in e.message]
        assert len(bad) == 1
        assert bad[0].severity == "error"
        assert "move them to a consumer" in bad[0].message

    def test_grain_is_rejected_too(self, transform_dir):
        _write(transform_dir, "silver", "base", """\
            @config materialized=ephemeral, schema=silver
            @grain id

            SELECT 1 AS id
        """)
        models = discover_models(transform_dir)
        errors = validate_models(None, models)
        assert [e for e in errors if "assertions cannot run on ephemeral" in e.message]


class TestInlineEphemeral:
    def test_no_ephemeral_upstream_returns_the_query_untouched(self, transform_dir):
        _write(transform_dir, "silver", "base", """\
            @config materialized=table, schema=silver

            SELECT 1 AS id
        """)
        _write(transform_dir, "gold", "report", """\
            @config materialized=table, schema=gold

            SELECT   id   FROM silver.base
        """)
        models, model_map = _map(transform_dir)
        report = model_map["gold.report"]
        assert inline_ephemeral(report, model_map) == report.query

    def test_cte_name_cannot_collide_with_a_user_cte(self, transform_dir):
        _write(transform_dir, "silver", "base", """\
            @config materialized=ephemeral, schema=silver

            SELECT 1 AS id
        """)
        _write(transform_dir, "gold", "report", """\
            @config materialized=table, schema=gold

            SELECT id FROM silver.base
        """)
        _models, model_map = _map(transform_dir)
        sql = inline_ephemeral(model_map["gold.report"], model_map)
        assert "__havn_silver_base" in sql
        assert "silver.base" not in sql

    def test_this_reference_is_refused(self, transform_dir):
        _write(transform_dir, "silver", "base", """\
            @config materialized=ephemeral, schema=silver

            SELECT id FROM landing.raw WHERE id > (SELECT MAX(id) FROM {this})
        """)
        _write(transform_dir, "gold", "report", """\
            @config materialized=table, schema=gold

            SELECT id FROM silver.base
        """)
        _models, model_map = _map(transform_dir)
        with pytest.raises(EphemeralInlineError, match=r"\{this\}"):
            inline_ephemeral(model_map["gold.report"], model_map)

    def test_watermark_is_refused(self, transform_dir):
        _write(transform_dir, "silver", "base", """\
            @config materialized=ephemeral, schema=silver
            @watermark updated_at

            SELECT id, updated_at FROM landing.raw
        """)
        _write(transform_dir, "gold", "report", """\
            @config materialized=table, schema=gold

            SELECT id FROM silver.base
        """)
        _models, model_map = _map(transform_dir)
        with pytest.raises(EphemeralInlineError, match="watermark"):
            inline_ephemeral(model_map["gold.report"], model_map)

    def test_refusal_surfaces_as_a_model_error(self, db, transform_dir):
        db.execute("CREATE TABLE landing.raw AS SELECT 1 AS id")
        _write(transform_dir, "silver", "base", """\
            @config materialized=ephemeral, schema=silver
            @watermark id

            SELECT id FROM landing.raw
        """)
        _write(transform_dir, "gold", "report", """\
            @config materialized=table, schema=gold

            SELECT id FROM silver.base
        """)
        results = run_transform(db, transform_dir, force=True)
        assert results["gold.report"] == "error"


class TestEphemeralLineage:
    def test_columns_are_attributed_to_the_ephemeral_model(self, transform_dir):
        _write(transform_dir, "silver", "base", """\
            @config materialized=ephemeral, schema=silver

            SELECT id, amount FROM landing.raw
        """)
        _write(transform_dir, "gold", "report", """\
            @config materialized=table, schema=gold

            SELECT id, amount FROM silver.base
        """)
        _models, model_map = _map(transform_dir)
        lineage = extract_column_lineage(model_map["gold.report"])
        # Lineage reads the model's own query, not the inlined one, so the
        # ephemeral is still named as the source.
        sources = {
            src["source_table"]
            for entries in lineage.values()
            for src in entries
        }
        assert "silver.base" in sources
