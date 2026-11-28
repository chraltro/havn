"""The build path inside a caller's transaction.

The live runner builds a refresh inside one transaction (data, consumed
watermarks and state commit together). On DuckDB 2.0 any failed statement
aborts that transaction, so "try a statement, fall back on the error" -- an
aggregate assertion tried as a row-level filter first, an INSERT into a
metadata table an old warehouse may lack -- made every later statement fail.
Each of these runs inside BEGIN here and must leave the transaction usable.
"""

from __future__ import annotations

import types

import duckdb
import pytest

from havn.engine.live.runner import LiveRunner
from havn.engine.live.sources import advance_source
from havn.engine.transform.models import AssertionResult, ProfileResult
from havn.engine.transform.quality import run_assertions

from tests.test_live import FAST, open_queue, q, write_project


def _model(**kw):
    base = dict(full_name="gold.o", schema="gold", name="o", assertions=[], grain=None,
                owner=None, assertion_specs=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture
def conn():
    c = duckdb.connect()
    c.execute("CREATE SCHEMA gold")
    c.execute("CREATE TABLE gold.o AS SELECT 5 AS amount, 1 AS id UNION ALL SELECT 7, 2")
    c.execute("CREATE TABLE gold.empty (amount INT)")
    yield c
    c.close()


@pytest.mark.parametrize("expr, expected", [
    ("sum(amount) > 0", True),
    ("count(*) = count(DISTINCT id)", True),
    ("max(amount) - min(amount) < 100", True),
    ("arg_max(id, amount) = 2", True),  # DuckDB aggregate sqlglot may not know
    ("amount >= 0", False),
    ("amount > (SELECT min(amount) FROM gold.o) - 1", False),  # aggregate in a subquery
    ("sum(amount) OVER () > 0", False),  # window, evaluated per row
    ("abs(amount) >= 0 AND id IS NOT NULL", False),
    ("row_count > 0 AND amount >= 0", False),
])
def test_aggregate_or_row_level_is_decided_up_front(conn, expr, expected):
    from havn.engine.transform.quality import _is_aggregate_expression

    assert _is_aggregate_expression(conn, expr) is expected


def test_assertions_inside_a_transaction_do_not_abort_it(conn):
    model = _model(assertions=[
        "sum(amount) > 0", "count(*) > 0", "amount >= 0", "arg_max(id, amount) = 2",
        "sum(amount) > 1000",
    ])
    conn.execute("BEGIN")
    results = run_assertions(conn, model)
    assert [r.passed for r in results] == [True, True, True, True, False]
    assert all("Assertion error" not in r.detail for r in results), [r.detail for r in results]
    assert conn.execute("SELECT count(*) FROM gold.o").fetchone() == (2,)  # still usable
    conn.execute("COMMIT")


def test_row_count_compound_on_an_empty_table_inside_a_transaction(conn):
    model = _model(full_name="gold.empty", name="empty",
                   assertions=["row_count > 0 AND amount >= 0", "row_count >= 0 AND sum(amount) IS NULL"])
    conn.execute("BEGIN")
    results = run_assertions(conn, model)
    assert results[0].passed is False
    assert results[1].passed is True
    assert conn.execute("SELECT 1").fetchone() == (1,)
    conn.execute("COMMIT")


def test_metadata_writes_on_an_old_warehouse_inside_a_transaction(conn):
    """Tables or columns an older warehouse lacks are looked up, not tried."""
    from havn.engine.transform.discovery import _clear_block, _is_blocked, blocked_models
    from havn.engine.transform.quality import _save_assertions, _save_profile, _save_source_freshness

    conn.execute("CREATE SCHEMA _havn")
    # Legacy shapes: no model_blocked, profile_history or source_freshness;
    # assertion_results without severity / owner.
    conn.execute(
        "CREATE TABLE _havn.assertion_results (model_path VARCHAR, expression VARCHAR, "
        "passed BOOLEAN, detail VARCHAR, checked_at TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE _havn.model_profiles (model_path VARCHAR, row_count BIGINT, column_count INT, "
        "null_percentages JSON, distinct_counts JSON, profiled_at TIMESTAMP)"
    )
    model = _model()
    conn.execute("BEGIN")
    _clear_block(conn, model)
    assert _is_blocked(conn, model) is False
    assert blocked_models(conn) == set()
    _save_assertions(conn, model, [AssertionResult(expression="x", passed=True, detail="ok")])
    profile = ProfileResult(row_count=2, column_count=2, null_percentages={}, distinct_counts={})
    _save_profile(conn, model, profile)
    assert profile.history_id is None
    _save_source_freshness(conn, model, [{"table": "landing.x", "is_stale": False}])
    conn.execute("COMMIT")
    assert conn.execute("SELECT count(*) FROM _havn.assertion_results").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM _havn.model_profiles").fetchone() == (1,)


def test_live_model_with_aggregate_assertions_refreshes(tmp_path):
    """The review's reproduction: every refresh of such a model failed on DuckDB 2.0."""
    root = write_project(tmp_path, {
        "bronze/ev.sql": (
            "@config materialized=incremental, live=true, incremental_strategy=append, "
            "incremental_filter=WHERE _havn_seq > {watermark}\n"
            "@assert sum(x) >= 0\n"
            "@assert count(*) > 0\n"
            "@assert x >= 0\n"
            "SELECT x, _havn_seq FROM landing.ev\n"
        ),
    })
    wq = open_queue(root)
    cur = wq.cursor()
    cur.execute("CREATE SCHEMA landing")
    cur.execute("CREATE TABLE landing.ev (x INTEGER)")
    runner = LiveRunner(root, wq, settings=FAST)
    runner._load_models(force=True)
    runner._call(runner._load_states)
    try:
        for value, total in ((1, 1), (2, 2)):
            cur.execute("INSERT INTO landing.ev (x) VALUES (?)", [value])
            advance_source(cur, "landing.ev")
            results = runner.run_cycle()
            assert [(r.status, r.error) for r in results] == [("built", None)]
            assert q(cur, "SELECT count(*) FROM bronze.ev") == [(total,)]
        passed = q(cur, "SELECT DISTINCT passed FROM _havn.assertion_results WHERE model_path = 'bronze.ev'")
        assert passed == [(True,)]
        assert q(cur, "SELECT count(*) FROM _havn.profile_history WHERE model_path = 'bronze.ev'")[0][0] >= 1
    finally:
        cur.close()
        wq.close()
