"""A failed severity=error assertion keeps blocking until the data is fixed."""
from __future__ import annotations

import duckdb
import pytest

from havn.engine.database import ensure_meta_table
from havn.engine.transform import run_transform

UNIQUE_IDS = "@config materialized=table, schema=bronze\n@assert unique(id)\n\nSELECT id FROM landing.src\n"
COUNTED = "@config materialized=table, schema=silver\n\nSELECT COUNT(*) AS n FROM bronze.ids\n"


@pytest.fixture
def setup(tmp_path):
    db_path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    ensure_meta_table(conn)
    conn.execute("CREATE SCHEMA landing")
    conn.execute("CREATE TABLE landing.src AS SELECT * FROM (VALUES (1), (1), (2)) t(id)")
    transform_dir = tmp_path / "transform"
    (transform_dir / "bronze").mkdir(parents=True)
    (transform_dir / "silver").mkdir()
    (transform_dir / "bronze" / "ids.sql").write_text(UNIQUE_IDS)
    (transform_dir / "silver" / "counted.sql").write_text(COUNTED)
    yield conn, transform_dir, db_path
    conn.close()


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel"])
def test_rerun_does_not_get_past_a_failed_assertion(setup, parallel):
    """Re-running used to be enough to get bad data past an error assertion.

    The model's state was recorded as soon as it built, before its
    assertions ran. The next run saw it as unchanged and skipped it, so
    nothing blocked its descendants, and they built on the rows the
    assertion had rejected.
    """
    conn, transform_dir, db_path = setup

    def run():
        return run_transform(conn, transform_dir, parallel=parallel, db_path=str(db_path))

    first = run()
    assert first["bronze.ids"] == "assertion_failed"
    assert first["silver.counted"] != "built"

    second = run()
    assert second["bronze.ids"] == "assertion_failed", second
    assert second["silver.counted"] != "built", second
    built = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = 'silver' AND table_name = 'counted'"
    ).fetchone()[0]
    assert built == 0

    conn.execute("CREATE OR REPLACE TABLE landing.src AS SELECT * FROM (VALUES (1), (2)) t(id)")
    third = run()
    assert third["bronze.ids"] == "built", third
    assert third["silver.counted"] == "built", third


def test_a_warning_assertion_does_not_force_a_rebuild(setup):
    """Only severity=error gates. A warning is reported, not enforced."""
    conn, transform_dir, db_path = setup
    (transform_dir / "bronze" / "ids.sql").write_text(
        "@config materialized=table, schema=bronze\n@assert unique(id) severity=warn\n\nSELECT id FROM landing.src\n"
    )
    run_transform(conn, transform_dir, parallel=False, db_path=str(db_path))
    again = run_transform(conn, transform_dir, parallel=False, db_path=str(db_path))
    assert again["bronze.ids"] == "skipped", again


def test_a_rejected_model_needs_a_build_but_is_not_reported_as_edited(setup):
    """Two questions, two answers.

    "Was the definition edited since it was built" drives the UI and the
    state:modified selector, and the answer is no. "Should the next run
    build it" is yes, because its last build was rejected. Folding the
    second into the first made the Home page call rejected models edited.
    """
    from havn.engine.transform import discover_models
    from havn.engine.transform.discovery import (
        _compute_upstream_hash,
        _has_changed,
        _needs_build,
    )

    conn, transform_dir, db_path = setup
    run_transform(conn, transform_dir, parallel=False, db_path=str(db_path))

    models = {m.full_name: m for m in discover_models(transform_dir)}
    ids = models["bronze.ids"]
    ids.upstream_hash = _compute_upstream_hash(ids, models)
    assert _has_changed(conn, ids) is False
    assert _needs_build(conn, ids) is True


@pytest.mark.parametrize("materialized, expected", [
    ("snapshot", True), ("incremental", True), ("table", False), ("view", False),
])
def test_data_driven_models_always_run(tmp_path, materialized, expected):
    """Snapshot and incremental models run every time; others only when changed.

    Judged by their SQL alone they were skipped forever once built, so a
    scheduled run recorded no new SCD2 history and loaded no new windows.
    """
    from havn.engine.transform.discovery import _needs_build, _update_state
    from havn.engine.transform.models import SQLModel

    conn = duckdb.connect()
    ensure_meta_table(conn)
    model = SQLModel(
        path=tmp_path / "m.sql", name="m", schema="silver", full_name="silver.m",
        sql="SELECT 1", query="SELECT 1", materialized=materialized, depends_on=[],
    )
    model.content_hash, model.upstream_hash = "abc", "def"
    _update_state(conn, model, 1, 1)
    assert _needs_build(conn, model) is expected
    conn.close()


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel"])
def test_a_blocked_parent_outside_the_selection_still_blocks(setup, parallel):
    """Edit only the child and build it alone (state:modified+): still blocked."""
    conn, transform_dir, db_path = setup
    run_transform(conn, transform_dir, parallel=parallel, db_path=str(db_path))

    only_child = run_transform(
        conn, transform_dir, targets=["silver.counted"], parallel=parallel, db_path=str(db_path)
    )
    assert only_child.get("silver.counted") != "built", only_child


def test_building_without_checking_does_not_lift_a_block(setup):
    """The job runner builds and records state without running assertions.

    Clearing the block on every state update let a scheduled job reopen the
    gate; only passing checks may lift it.
    """
    from havn.engine.transform import discover_models
    from havn.engine.transform.discovery import _update_state, blocked_models

    conn, transform_dir, db_path = setup
    run_transform(conn, transform_dir, parallel=False, db_path=str(db_path))
    assert "bronze.ids" in blocked_models(conn)

    ids = next(m for m in discover_models(transform_dir) if m.full_name == "bronze.ids")
    _update_state(conn, ids, 1, 3)  # what a job step does after building
    assert "bronze.ids" in blocked_models(conn)

    again = run_transform(conn, transform_dir, parallel=False, db_path=str(db_path))
    assert again["bronze.ids"] == "assertion_failed"
    assert again["silver.counted"] != "built"


def _project(tmp_path, files):
    db_path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    ensure_meta_table(conn)
    conn.execute("CREATE SCHEMA landing")
    conn.execute("CREATE TABLE landing.ev AS SELECT * FROM (VALUES (1), (2)) t(id)")
    tdir = tmp_path / "transform"
    for rel, sql in files.items():
        (tdir / rel).parent.mkdir(parents=True, exist_ok=True)
        (tdir / rel).write_text(sql)
    return conn, tdir, db_path


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel"])
def test_new_incremental_data_reaches_downstream_tables(tmp_path, parallel):
    """An incremental model picked up new rows, but the table built from it was
    skipped (its SQL had not changed), so gold kept reporting the old count."""
    conn, tdir, db_path = _project(tmp_path, {
        "silver/ev.sql": "@config materialized=incremental, unique_key=id\n\nSELECT id FROM landing.ev\n",
        "gold/cnt.sql": "@config materialized=table\n\nSELECT COUNT(*) AS n FROM silver.ev\n",
    })
    run_transform(conn, tdir, parallel=parallel, db_path=str(db_path))
    conn.execute("INSERT INTO landing.ev VALUES (3)")
    run_transform(conn, tdir, parallel=parallel, db_path=str(db_path))
    assert conn.execute("SELECT n FROM gold.cnt").fetchone()[0] == 3
    conn.close()


def test_a_plain_append_is_not_rerun_into_duplicates(tmp_path):
    """An unfiltered append re-inserts everything it selects, so it is not re-run blindly."""
    conn, tdir, db_path = _project(tmp_path, {
        "silver/ev.sql": "@config materialized=incremental, incremental_strategy=append\n\nSELECT id FROM landing.ev\n",
    })
    for _ in range(3):
        run_transform(conn, tdir, parallel=False, db_path=str(db_path))
    assert conn.execute("SELECT COUNT(*) FROM silver.ev").fetchone()[0] == 2
    conn.close()
