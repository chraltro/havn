"""A table model rebuilds when its inputs hold newer data, not only when its SQL changes."""
from __future__ import annotations

import duckdb
import pytest

from havn.engine.database import ensure_meta_table, log_run
from havn.engine.transform import run_transform

BRONZE = "@config materialized=table, schema=bronze\n\nSELECT id FROM landing.src\n"
GOLD = "@config materialized=table, schema=gold\n\nSELECT COUNT(*) AS n FROM bronze.ids\n"


@pytest.fixture
def project(tmp_path):
    db_path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    ensure_meta_table(conn)
    conn.execute("CREATE SCHEMA landing")
    conn.execute("CREATE TABLE landing.src AS SELECT * FROM (VALUES (1), (2)) t(id)")
    tdir = tmp_path / "transform"
    for rel, sql in {"bronze/ids.sql": BRONZE, "gold/cnt.sql": GOLD}.items():
        (tdir / rel).parent.mkdir(parents=True, exist_ok=True)
        (tdir / rel).write_text(sql)
    yield conn, tdir, str(db_path)
    conn.close()


def _count(conn):
    return conn.execute("SELECT n FROM gold.cnt").fetchone()[0]


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel"])
@pytest.mark.parametrize("run_type", ["ingest", "import", "connector_sync", "seed"])
def test_a_load_after_the_build_reaches_gold(project, parallel, run_type):
    """Re-ingested landing data used to stop at the first table until --force."""
    conn, tdir, db = project
    run_transform(conn, tdir, parallel=parallel, db_path=db)
    assert _count(conn) == 2

    conn.execute("INSERT INTO landing.src VALUES (3)")
    log_run(conn, run_type, "load_src.py", "success")
    results = run_transform(conn, tdir, parallel=parallel, db_path=db)

    assert results["bronze.ids"] == "built"
    assert results["gold.cnt"] == "built"
    assert _count(conn) == 3


def test_nothing_new_means_nothing_rebuilt(project):
    conn, tdir, db = project
    run_transform(conn, tdir, db_path=db)
    log_run(conn, "ingest", "load_src.py", "success")
    run_transform(conn, tdir, db_path=db)

    again = run_transform(conn, tdir, db_path=db)
    assert again == {"bronze.ids": "skipped", "gold.cnt": "skipped"}


def test_a_failed_or_unrelated_run_does_not_count(project):
    conn, tdir, db = project
    run_transform(conn, tdir, db_path=db)
    log_run(conn, "ingest", "load_src.py", "error", error="boom")
    log_run(conn, "export", "report.py", "success")

    assert run_transform(conn, tdir, db_path=db)["bronze.ids"] == "skipped"


def test_a_parent_built_in_a_narrower_run_rebuilds_its_children(project):
    """`havn transform bronze.ids` refreshed bronze but left gold on the old
    count, and every later full run skipped gold as unchanged."""
    conn, tdir, db = project
    run_transform(conn, tdir, db_path=db)
    conn.execute("INSERT INTO landing.src VALUES (3)")
    run_transform(conn, tdir, targets=["bronze.ids"], force=True, db_path=db)
    assert _count(conn) == 2  # gold was outside that run

    results = run_transform(conn, tdir, db_path=db)
    assert results["gold.cnt"] == "built"
    assert _count(conn) == 3


def test_views_and_plain_appends_are_left_alone(tmp_path):
    db_path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    ensure_meta_table(conn)
    conn.execute("CREATE SCHEMA landing")
    conn.execute("CREATE TABLE landing.src AS SELECT * FROM (VALUES (1), (2)) t(id)")
    tdir = tmp_path / "transform"
    (tdir / "silver").mkdir(parents=True)
    (tdir / "silver" / "v.sql").write_text("@config materialized=view\n\nSELECT id FROM landing.src\n")
    (tdir / "silver" / "app.sql").write_text(
        "@config materialized=incremental, incremental_strategy=append\n\nSELECT id FROM landing.src\n"
    )
    try:
        run_transform(conn, tdir, db_path=str(db_path))
        log_run(conn, "ingest", "load_src.py", "success")
        results = run_transform(conn, tdir, db_path=str(db_path))
        assert results == {"silver.app": "skipped", "silver.v": "skipped"}
        assert conn.execute("SELECT COUNT(*) FROM silver.app").fetchone()[0] == 2
    finally:
        conn.close()


def test_an_ephemeral_input_counts_as_its_raw_source(tmp_path):
    """Ephemerals are recorded on every run, so their timestamp is no signal;
    new raw data under one still reaches the table that inlines it."""
    db_path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    ensure_meta_table(conn)
    conn.execute("CREATE SCHEMA landing")
    conn.execute("CREATE TABLE landing.src AS SELECT * FROM (VALUES (1), (2)) t(id)")
    tdir = tmp_path / "transform"
    (tdir / "silver").mkdir(parents=True)
    (tdir / "gold").mkdir()
    (tdir / "silver" / "base.sql").write_text(
        "@config materialized=ephemeral\n@depends_on landing.src\n\nSELECT id FROM landing.src\n"
    )
    (tdir / "gold" / "cnt.sql").write_text("@config materialized=table\n\nSELECT COUNT(*) AS n FROM silver.base\n")
    try:
        run_transform(conn, tdir, db_path=str(db_path))
        assert run_transform(conn, tdir, db_path=str(db_path))["gold.cnt"] == "skipped"
        conn.execute("INSERT INTO landing.src VALUES (3)")
        log_run(conn, "ingest", "load_src.py", "success")
        assert run_transform(conn, tdir, db_path=str(db_path))["gold.cnt"] == "built"
        assert conn.execute("SELECT n FROM gold.cnt").fetchone()[0] == 3
    finally:
        conn.close()


def test_new_raw_data_reaches_a_table_through_a_view(tmp_path):
    """bronze defaults to views: landing -> bronze (view) -> silver (table)."""
    db_path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    ensure_meta_table(conn)
    conn.execute("CREATE SCHEMA landing")
    conn.execute("CREATE TABLE landing.src AS SELECT * FROM (VALUES (1), (2)) t(id)")
    tdir = tmp_path / "transform"
    (tdir / "bronze").mkdir(parents=True)
    (tdir / "silver").mkdir()
    (tdir / "bronze" / "ids.sql").write_text("SELECT id FROM landing.src\n")
    (tdir / "silver" / "cnt.sql").write_text("@config materialized=table\n\nSELECT COUNT(*) AS n FROM bronze.ids\n")
    try:
        run_transform(conn, tdir, db_path=str(db_path))
        assert run_transform(conn, tdir, db_path=str(db_path))["silver.cnt"] == "skipped"
        conn.execute("INSERT INTO landing.src VALUES (3)")
        log_run(conn, "ingest", "load_src.py", "success")
        assert run_transform(conn, tdir, db_path=str(db_path))["silver.cnt"] == "built"
        assert conn.execute("SELECT n FROM silver.cnt").fetchone()[0] == 3
        assert run_transform(conn, tdir, db_path=str(db_path))["silver.cnt"] == "skipped"
    finally:
        conn.close()
