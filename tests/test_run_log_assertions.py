"""The run log reflects assertion results, on every build path.

A build whose severity=error assertion fails blocks its descendants, so the
run log must not record it as a success. Warnings stay a success.
"""
from __future__ import annotations

import duckdb
import pytest

from havn.engine.database import ensure_meta_table
from havn.engine.transform import run_transform

MODELS = {
    "bad.sql": "@config materialized=table, schema=bronze\n@assert row_count > 5\n\nSELECT 1 AS id\n",
    "warned.sql": "@config materialized=table, schema=bronze\n@assert id > 1, severity=warn\n\nSELECT 1 AS id\n",
    "good.sql": "@config materialized=table, schema=bronze\n@assert id = 1\n\nSELECT 1 AS id\n",
}


def _project(tmp_path, names):
    d = tmp_path / "transform" / "bronze"
    d.mkdir(parents=True)
    for name in names:
        (d / name).write_text(MODELS[name])
    return tmp_path / "transform"


def _log(conn):
    return {
        r[0]: (r[1], r[2], r[3])
        for r in conn.execute(
            "SELECT target, status, error, rows_affected FROM _havn.run_log"
        ).fetchall()
    }


@pytest.mark.parametrize(
    "parallel, names",
    [
        (False, ["bad.sql", "warned.sql", "good.sql"]),  # sequential
        (True, ["bad.sql", "warned.sql", "good.sql"]),   # parallel workers
        (True, ["bad.sql"]),                             # parallel, one-model tier
    ],
    ids=["sequential", "parallel-workers", "parallel-single"],
)
def test_run_log_status_follows_assertions(tmp_path, parallel, names):
    transform_dir = _project(tmp_path, names)
    db = tmp_path / "w.duckdb"
    conn = duckdb.connect(str(db))
    ensure_meta_table(conn)
    run_transform(conn, transform_dir, force=True, parallel=parallel, db_path=str(db))
    log = _log(conn)
    conn.close()

    status, error, rows = log["bronze.bad"]
    assert status == "error"
    assert error.startswith("assertion failed: row_count > 5")
    assert rows == 1  # the build itself happened and is still recorded

    if "warned.sql" in names:
        assert log["bronze.warned"][0] == "success"
        assert log["bronze.warned"][1] is None
        assert log["bronze.good"][0] == "success"
