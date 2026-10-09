"""Live models: watermarks, CDC apply, the live runner, coexistence with batch runs.

Real DuckDB throughout. A streaming source is simulated the way the built-in
connectors feed one: rows are appended to a landing table and
``advance_source`` -- the hook the webhook flush, CDC and API-poll consumers
call after each commit -- stamps them and announces the advance.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import duckdb
import pytest

from havn.config import DatabaseConfig
from havn.engine.backends import create_backend
from havn.engine.live import events
from havn.engine.live.runner import LiveRunner
from havn.engine.live.settings import LiveSettings, parse_duration, parse_live_flag
from havn.engine.live.sources import advance_source
from havn.engine.live.state import consumed_watermarks, load_states, source_watermarks
from havn.engine.transform import discover_models, run_transform
from havn.engine.transform.analysis import validate_models
from havn.engine.write_queue import WriteQueue

FAST = LiveSettings(
    min_interval=0.05, debounce=0.02, max_latency=0.3, poll_interval=0.3,
    log_interval=3600, backoff_base=0.2, backoff_max=1.0, profile_interval=3600,
)


def write_project(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.yml").write_text("name: live_test\n", encoding="utf-8")
    for rel, body in files.items():
        p = root / "transform" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return root


def open_queue(root: Path) -> WriteQueue:
    backend = create_backend(DatabaseConfig(path="warehouse.duckdb"), project_dir=root)
    wq = WriteQueue(backend)
    from havn.engine.database import ensure_meta_table

    ensure_meta_table(wq.conn)
    return wq


def wait_until(predicate, timeout: float = 15.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s (last={last!r})")


def q(conn, sql, params=None):
    return conn.execute(sql, params or []).fetchall()


ORDERS_LANDING = (
    "CREATE TABLE landing.orders (id INTEGER, amount INTEGER, region VARCHAR, "
    "op VARCHAR, lsn BIGINT)"
)

BRONZE_CDC = (
    "@config materialized=incremental, live=true, incremental_strategy=merge, unique_key=id, "
    "cdc_op=op, cdc_seq=lsn, cdc_deletes=soft, incremental_filter=WHERE _havn_seq > {watermark}\n"
    "SELECT id, amount, region, op, lsn, _havn_seq FROM landing.orders\n"
)
SILVER_VIEW = (
    "@config materialized=view, live=true\n"
    "SELECT id, amount, region, _havn_seq, _havn_deleted FROM bronze.orders\n"
)
GOLD_BY_REGION = (
    "@config materialized=incremental, live=true, incremental_strategy=delete+insert, unique_key=region\n"
    "@assert row_count >= 0\n"
    "SELECT region, SUM(CASE WHEN NOT _havn_deleted THEN amount ELSE 0 END) AS total,\n"
    "       COUNT(*) FILTER (WHERE NOT _havn_deleted) AS n\n"
    "FROM silver.orders\n"
    "WHERE region IN (SELECT region FROM silver.orders WHERE _havn_seq > {watermark})\n"
    "GROUP BY region\n"
)


def insert_events(conn, rows):
    conn.executemany(
        "INSERT INTO landing.orders (id, amount, region, op, lsn) VALUES (?, ?, ?, ?, ?)", rows
    )


@pytest.fixture
def chain(tmp_path):
    root = write_project(tmp_path, {
        "bronze/orders.sql": BRONZE_CDC,
        "silver/orders.sql": SILVER_VIEW,
        "gold/by_region.sql": GOLD_BY_REGION,
    })
    wq = open_queue(root)
    cur = wq.cursor()
    cur.execute("CREATE SCHEMA landing")
    cur.execute(ORDERS_LANDING)
    yield root, wq, cur
    cur.close()
    wq.close()


# ---------------------------------------------------------------------------
# Settings and parsing
# ---------------------------------------------------------------------------


def test_parse_duration_units():
    assert parse_duration("500ms") == 0.5
    assert parse_duration("10s") == 10
    assert parse_duration("2m") == 120
    assert parse_duration(3) == 3.0
    with pytest.raises(ValueError):
        parse_duration("soon")
    with pytest.raises(ValueError):
        parse_duration(-1)


def test_live_flag_and_settings():
    assert parse_live_flag("true") and parse_live_flag("YES")
    assert not parse_live_flag("false") and not parse_live_flag(None)
    s = LiveSettings.from_raw({"min_interval": "250ms", "max_latency": 3, "enabled": "false"})
    assert s.min_interval == 0.25 and s.max_latency == 3 and s.enabled is False


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def test_advance_source_stamps_in_commit_order_and_publishes(tmp_path):
    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    conn.execute("CREATE SCHEMA landing")
    conn.execute("CREATE TABLE landing.ev (x INTEGER)")
    seen = []
    unsubscribe = events.subscribe(seen.append)
    try:
        conn.execute("INSERT INTO landing.ev VALUES (1), (2)")
        a1 = advance_source(conn, "landing.ev")
        conn.execute("INSERT INTO landing.ev (x) VALUES (3)")
        a2 = advance_source(conn, "landing.ev")
        assert advance_source(conn, "landing.ev") is None  # nothing new
    finally:
        unsubscribe()
    assert (a1.wm_from, a1.wm_to, a1.rows) == (0, 2, 2)
    assert (a2.wm_from, a2.wm_to) == (2, 3)
    assert q(conn, "SELECT x, _havn_seq FROM landing.ev ORDER BY x") == [(1, 1), (2, 2), (3, 3)]
    assert [(e.source, e.watermark) for e in seen] == [("landing.ev", 2), ("landing.ev", 3)]
    assert source_watermarks(conn) == {"landing.ev": 3}


def test_advance_inside_open_transaction_joins_and_does_not_publish(tmp_path):
    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    conn.execute("CREATE SCHEMA landing")
    conn.execute("CREATE TABLE landing.ev (x INTEGER)")
    seen = []
    unsubscribe = events.subscribe(seen.append)
    try:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO landing.ev VALUES (1)")
        adv = advance_source(conn, "landing.ev")
        conn.execute("COMMIT")
    finally:
        unsubscribe()
    assert adv is not None and not adv.published
    assert seen == []
    assert source_watermarks(conn) == {"landing.ev": 1}


def test_advance_rejects_bad_source(tmp_path):
    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    with pytest.raises(ValueError):
        advance_source(conn, "orders")
    with pytest.raises(ValueError):
        advance_source(conn, "landing.missing")


# ---------------------------------------------------------------------------
# CDC apply (batch path)
# ---------------------------------------------------------------------------


def _cdc_project(tmp_path, deletes="hard"):
    root = write_project(tmp_path, {
        "bronze/orders.sql": (
            "@config materialized=incremental, live=true, incremental_strategy=merge, "
            f"unique_key=id, cdc_op=op, cdc_seq=lsn, cdc_deletes={deletes}, "
            "incremental_filter=WHERE _havn_seq > {watermark}\n"
            "SELECT id, amount, region, op, lsn, _havn_seq FROM landing.orders\n"
        ),
    })
    conn = duckdb.connect(str(root / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA landing")
    conn.execute(ORDERS_LANDING)
    return root, conn


def _land(conn, rows):
    insert_events(conn, rows)
    advance_source(conn, "landing.orders")


def test_cdc_insert_update_delete(tmp_path):
    root, conn = _cdc_project(tmp_path)
    _land(conn, [(1, 10, "eu", "I", 1), (2, 20, "us", "I", 2), (3, 30, "us", "I", 3)])
    run_transform(conn, root / "transform", project_dir=root)
    _land(conn, [(1, 11, "eu", "U", 4), (2, None, None, "D", 5)])
    run_transform(conn, root / "transform", project_dir=root)
    assert q(conn, "SELECT id, amount FROM bronze.orders ORDER BY id") == [(1, 11), (3, 30)]
    # The delete is remembered as a tombstone.
    assert q(conn, "SELECT id, lsn FROM _havn.cdc_tombstones__bronze__orders") == [(2, 5)]


def test_cdc_duplicates_and_out_of_order_are_idempotent(tmp_path):
    root, conn = _cdc_project(tmp_path)
    # Within one batch: two versions of id 1 arrive newest-first, plus an
    # exact duplicate of id 2.
    _land(conn, [(1, 15, "eu", "U", 3), (1, 10, "eu", "I", 1), (2, 20, "us", "I", 2), (2, 20, "us", "I", 2)])
    run_transform(conn, root / "transform", project_dir=root)
    assert q(conn, "SELECT id, amount, lsn FROM bronze.orders ORDER BY id") == [(1, 15, 3), (2, 20, 2)]
    # Across batches: a replay of old events (lower LSN) changes nothing.
    _land(conn, [(1, 10, "eu", "I", 1), (2, 99, "us", "U", 2)])
    run_transform(conn, root / "transform", project_dir=root)
    assert q(conn, "SELECT id, amount, lsn FROM bronze.orders ORDER BY id") == [(1, 15, 3), (2, 20, 2)]
    # A delete, then an older insert replayed after it: the row stays gone.
    _land(conn, [(2, None, None, "delete", 7)])
    run_transform(conn, root / "transform", project_dir=root)
    _land(conn, [(2, 20, "us", "I", 2)])
    run_transform(conn, root / "transform", project_dir=root)
    assert q(conn, "SELECT id FROM bronze.orders ORDER BY id") == [(1,)]
    # A newer insert after the delete brings the key back.
    _land(conn, [(2, 21, "us", "I", 9)])
    run_transform(conn, root / "transform", project_dir=root)
    assert q(conn, "SELECT id, amount FROM bronze.orders ORDER BY id") == [(1, 15), (2, 21)]
    assert q(conn, "SELECT count(*) FROM _havn.cdc_tombstones__bronze__orders") == [(0,)]


def test_cdc_soft_deletes_keep_a_tombstone_row(tmp_path):
    root, conn = _cdc_project(tmp_path, deletes="soft")
    _land(conn, [(1, 10, "eu", "I", 1), (2, 20, "us", "I", 2)])
    run_transform(conn, root / "transform", project_dir=root)
    _land(conn, [(2, None, None, "D", 3)])
    run_transform(conn, root / "transform", project_dir=root)
    assert q(conn, "SELECT id, _havn_deleted, lsn FROM bronze.orders ORDER BY id") == [
        (1, False, 1), (2, True, 3)
    ]
    _land(conn, [(2, 20, "us", "U", 2)])  # late, older than the delete
    run_transform(conn, root / "transform", project_dir=root)
    assert q(conn, "SELECT id, _havn_deleted FROM bronze.orders ORDER BY id") == [(1, False), (2, True)]


def test_batch_run_consumes_watermark_once(tmp_path):
    root = write_project(tmp_path, {
        "bronze/events.sql": (
            "@config materialized=incremental, live=true, incremental_strategy=append, "
            "incremental_filter=WHERE _havn_seq > {watermark}\n"
            "SELECT x, _havn_seq FROM landing.ev\n"
        ),
    })
    conn = duckdb.connect(str(root / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA landing")
    conn.execute("CREATE TABLE landing.ev (x INTEGER)")
    conn.execute("INSERT INTO landing.ev VALUES (1), (2)")
    advance_source(conn, "landing.ev")
    for _ in range(3):  # re-running without new data appends nothing
        run_transform(conn, root / "transform", project_dir=root)
    assert q(conn, "SELECT count(*) FROM bronze.events") == [(2,)]
    conn.execute("INSERT INTO landing.ev (x) VALUES (3)")
    advance_source(conn, "landing.ev")
    run_transform(conn, root / "transform", project_dir=root)
    run_transform(conn, root / "transform", project_dir=root)
    assert q(conn, "SELECT x FROM bronze.events ORDER BY x") == [(1,), (2,), (3,)]
    assert consumed_watermarks(conn, "bronze.events") == {"landing.ev": 3}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _messages(tmp_path, files):
    root = write_project(tmp_path, files)
    models = discover_models(root / "transform")
    return [(e.model, e.severity, e.message) for e in validate_models(None, models, known_tables={"landing.ev", "landing.a", "landing.b"})]


def test_validation_rejects_live_tables_and_microbatch(tmp_path):
    msgs = _messages(tmp_path, {
        "gold/t.sql": "@config materialized=table, live=true\nSELECT 1 AS x FROM landing.ev\n",
        "gold/m.sql": (
            "@config materialized=incremental, live=true, incremental_strategy=microbatch, "
            "event_time=ts, batch_size=day, begin=2024-01-01\n"
            "SELECT * FROM landing.ev WHERE ts >= {start} AND ts < {end}\n"
        ),
    })
    errors = {(m, s) for m, s, _ in msgs if s == "error"}
    assert ("gold.t", "error") in errors and ("gold.m", "error") in errors
    text = " ".join(t for _, _, t in msgs)
    assert "must be materialized=incremental" in text
    assert "microbatch" in text


def test_validation_watermark_needs_one_input(tmp_path):
    msgs = _messages(tmp_path, {
        "silver/j.sql": (
            "@config materialized=incremental, live=true, unique_key=id\n"
            "SELECT a.id FROM landing.a a JOIN landing.b b ON a.id = b.id WHERE a._havn_seq > {watermark}\n"
        ),
        "silver/k.sql": (
            "@config materialized=incremental, live=true, unique_key=id\n"
            "SELECT a.id FROM landing.a a JOIN landing.b b ON a.id = b.id "
            "WHERE a._havn_seq > {watermark:landing.a} OR b._havn_seq > {watermark:landing.b}\n"
        ),
    })
    j = [t for m, s, t in msgs if m == "silver.j" and s == "error"]
    k = [t for m, s, t in msgs if m == "silver.k" and s == "error"]
    assert any("exactly one live input" in t for t in j)
    assert k == []


def test_validation_cdc_rules(tmp_path):
    msgs = _messages(tmp_path, {
        "bronze/a.sql": (
            "@config materialized=incremental, incremental_strategy=append, cdc_op=op\n"
            "SELECT * FROM landing.ev\n"
        ),
    })
    text = [t for _, s, t in msgs if s == "error"]
    assert any("cdc_op and cdc_seq go together" in t for t in text)
    assert any("merge or delete+insert" in t for t in text)
    assert any("unique_key" in t for t in text)


def test_validation_append_live_needs_filter_and_unknown_flag(tmp_path):
    msgs = _messages(tmp_path, {
        "bronze/a.sql": (
            "@config materialized=incremental, live=true, incremental_strategy=append\n"
            "SELECT * FROM landing.ev\n"
        ),
        "bronze/b.sql": "@config materialized=view, live=ture\nSELECT * FROM landing.ev\n",
    })
    assert any(m == "bronze.a" and s == "error" and "needs an incremental_filter" in t for m, s, t in msgs)
    assert any(m == "bronze.b" and "not true or false" in t for m, s, t in msgs)


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


def test_end_to_end_live_chain(chain):
    root, wq, cur = chain
    runner = LiveRunner(root, wq, settings=FAST)
    runner.start()
    try:
        insert_events(cur, [(1, 10, "eu", "I", 1), (2, 20, "us", "I", 2)])
        t0 = time.monotonic()
        advance_source(cur, "landing.orders")
        wait_until(lambda: q(cur, "SELECT region, total, n FROM gold.by_region ORDER BY region")
                   == [("eu", 10, 1), ("us", 20, 1)] if _exists(cur, "gold", "by_region") else False)
        assert time.monotonic() - t0 < 10  # seconds, not a batch interval

        # An update and a delete flow through bronze -> silver (view) -> gold.
        insert_events(cur, [(1, 15, "eu", "U", 3), (2, None, "us", "D", 4), (3, 5, "eu", "I", 5)])
        advance_source(cur, "landing.orders")
        wait_until(lambda: q(cur, "SELECT region, total, n FROM gold.by_region ORDER BY region")
                   == [("eu", 20, 2), ("us", 0, 0)])
        status = runner.status()
        by_name = {m["model"]: m for m in status["models"]}
        assert set(by_name) == {"bronze.orders", "silver.orders", "gold.by_region"}
        assert by_name["gold.by_region"]["lag_seconds"] == 0
        assert by_name["gold.by_region"]["refreshes"] >= 2
        assert by_name["bronze.orders"]["status"] == "live"
        assert by_name["bronze.orders"]["last_lag_ms"] is not None
        assert any(s["source"] == "landing.orders" for s in status["sources"])
    finally:
        runner.stop()


def _exists(cur, schema, name):
    return bool(q(cur, "SELECT 1 FROM information_schema.tables WHERE table_schema=? AND table_name=?",
                  [schema, name]))


def test_coalesces_a_burst_into_few_refreshes(chain):
    root, wq, cur = chain
    settings = LiveSettings(**{**FAST.to_dict(), "min_interval": 0.5, "debounce": 0.3, "max_latency": 2.0})
    runner = LiveRunner(root, wq, settings=settings)
    runner.start()
    try:
        insert_events(cur, [(0, 1, "eu", "I", 1)])
        advance_source(cur, "landing.orders")
        wait_until(lambda: _exists(cur, "gold", "by_region"))
        wait_until(lambda: runner.snapshot()["models"]["gold.by_region"]["refreshes"] >= 1)
        before = runner.snapshot()["models"]["bronze.orders"]["refreshes"]
        for i in range(1, 41):  # 40 commits in quick succession
            insert_events(cur, [(i, 1, "eu", "I", i + 1)])
            advance_source(cur, "landing.orders")
        wait_until(lambda: q(cur, "SELECT n FROM gold.by_region WHERE region='eu'") == [(41,)])
        after = runner.snapshot()["models"]["bronze.orders"]["refreshes"]
        assert 1 <= after - before <= 5, after - before
    finally:
        runner.stop()


def test_failure_pauses_with_backoff_then_recovers(tmp_path, monkeypatch):
    root = write_project(tmp_path, {
        "bronze/ev.sql": (
            "@config materialized=incremental, live=true, incremental_strategy=append, "
            "incremental_filter=WHERE _havn_seq > {watermark}\n"
            "@assert no_nulls(x)\n"
            "SELECT x, _havn_seq FROM landing.ev\n"
        ),
        "silver/ev.sql": (
            "@config materialized=incremental, live=true, incremental_strategy=append, "
            "incremental_filter=WHERE _havn_seq > {watermark}\n"
            "SELECT x, _havn_seq FROM bronze.ev\n"
        ),
    })
    wq = open_queue(root)
    cur = wq.cursor()
    cur.execute("CREATE SCHEMA landing")
    cur.execute("CREATE TABLE landing.ev (x INTEGER)")
    alerts = []
    from havn.engine import alerts as alerts_mod

    original = alerts_mod.send_alert

    def capture(alert, config, conn=None):
        # Records what was sent and still delivers it (to the log channel and
        # _havn.alert_log), so this is a spy, not a stand-in.
        alerts.append(alert.alert_type)
        return original(alert, config, conn)

    monkeypatch.setattr(alerts_mod, "send_alert", capture)
    runner = LiveRunner(root, wq, settings=FAST, alerts=type("A", (), {"channels": ["log"]})())
    runner.start()
    try:
        cur.execute("INSERT INTO landing.ev VALUES (1)")
        advance_source(cur, "landing.ev")
        wait_until(lambda: _exists(cur, "silver", "ev") and q(cur, "SELECT count(*) FROM silver.ev") == [(1,)])

        # A NULL fails bronze's error assertion: the batch is rolled back,
        # bronze goes failing with backoff, silver waits, landing keeps going.
        cur.execute("INSERT INTO landing.ev (x) VALUES (NULL)")
        advance_source(cur, "landing.ev")
        wait_until(lambda: runner.snapshot()["models"]["bronze.ev"]["status"] == "failing")
        assert q(cur, "SELECT count(*) FROM bronze.ev") == [(1,)]  # rolled back
        wait_until(lambda: runner.snapshot()["models"]["bronze.ev"]["consecutive_failures"] >= 2)
        st = runner.snapshot()["models"]["bronze.ev"]
        assert st["next_retry_at"] is not None and "no_nulls" in (st["last_error"] or "")
        status = {m["model"]: m for m in runner.status()["models"]}
        assert status["bronze.ev"]["status"] == "failing"
        assert status["silver.ev"]["status"] == "waiting"
        assert "live_model_failed" in alerts
        assert q(cur, "SELECT count(*) FROM _havn.run_log WHERE run_type='live' AND status='error'")[0][0] >= 2

        # Upstream keeps landing while bronze is paused by its failure.
        cur.execute("INSERT INTO landing.ev (x) VALUES (2)")
        advance_source(cur, "landing.ev")
        assert source_watermarks(cur)["landing.ev"] == 3

        # Fix the data; bronze catches up on everything that queued.
        cur.execute("UPDATE landing.ev SET x = 0 WHERE x IS NULL")
        runner.refresh_now("bronze.ev")
        wait_until(lambda: q(cur, "SELECT count(*) FROM silver.ev") == [(3,)])
        assert runner.snapshot()["models"]["bronze.ev"]["status"] == "active"
        assert "live_model_recovered" in alerts
        assert q(cur, "SELECT count(*) FROM _havn.alert_log WHERE alert_type = 'live_model_failed'")[0][0] >= 1
    finally:
        runner.stop()
        cur.close()
        wq.close()


def test_pause_and_resume(chain):
    root, wq, cur = chain
    runner = LiveRunner(root, wq, settings=FAST)
    runner.start()
    try:
        insert_events(cur, [(1, 10, "eu", "I", 1)])
        advance_source(cur, "landing.orders")
        wait_until(lambda: _exists(cur, "gold", "by_region"))
        runner.pause("bronze.orders")
        insert_events(cur, [(2, 5, "eu", "I", 2)])
        advance_source(cur, "landing.orders")
        time.sleep(1.0)
        assert q(cur, "SELECT n FROM gold.by_region") == [(1,)]
        status = {m["model"]: m for m in runner.status()["models"]}
        assert status["bronze.orders"]["status"] == "paused"
        assert status["gold.by_region"]["status"] == "waiting"
        assert status["bronze.orders"]["lag_seconds"] > 0
        assert load_states(cur)["bronze.orders"].paused  # persisted
        runner.resume("bronze.orders")
        wait_until(lambda: q(cur, "SELECT n FROM gold.by_region") == [(2,)])
    finally:
        runner.stop()


def test_aggregated_run_log(chain):
    root, wq, cur = chain
    settings = LiveSettings(**{**FAST.to_dict(), "log_interval": 0.5})
    runner = LiveRunner(root, wq, settings=settings)
    runner.start()
    try:
        for i in range(5):
            insert_events(cur, [(i, 1, "eu", "I", i + 1)])
            advance_source(cur, "landing.orders")
            time.sleep(0.15)
        wait_until(lambda: q(cur, "SELECT n FROM gold.by_region") == [(5,)] if _exists(cur, "gold", "by_region") else False)
        wait_until(lambda: q(cur, "SELECT count(*) FROM _havn.run_log WHERE run_type='live' AND target='bronze.orders'")[0][0] >= 1)
    finally:
        runner.stop()
    rows = q(cur, "SELECT status, log_output FROM _havn.run_log WHERE run_type='live' AND target='bronze.orders'")
    total = sum(int(r[1].split(" ")[0]) for r in rows if r[1] and "refresh" in r[1])
    refreshes = load_states(cur)["bronze.orders"].refreshes
    assert total == refreshes
    # Far fewer rows than refreshes would be, and none of them transform rows.
    assert q(cur, "SELECT count(*) FROM _havn.run_log WHERE run_type='transform'") == [(0,)]


def test_coexists_with_concurrent_batch_transform(tmp_path):
    """An append live model is the strictest check: a double-apply duplicates rows."""
    root = write_project(tmp_path, {
        "bronze/ev.sql": (
            "@config materialized=incremental, live=true, incremental_strategy=append, "
            "incremental_filter=WHERE _havn_seq > {watermark}\n"
            "SELECT x, _havn_seq FROM landing.ev\n"
        ),
        "gold/total.sql": (
            "@config materialized=incremental, live=true, incremental_strategy=delete+insert, unique_key=k\n"
            "SELECT 1 AS k, COUNT(*) AS n, SUM(x) AS s FROM bronze.ev\n"
        ),
    })
    wq = open_queue(root)
    cur = wq.cursor()
    cur.execute("CREATE SCHEMA landing")
    cur.execute("CREATE TABLE landing.ev (x INTEGER)")
    runner = LiveRunner(root, wq, settings=FAST)
    runner.start()
    stop = threading.Event()
    batch_errors: list[str] = []

    def batch_loop():
        bcur = wq.cursor()
        try:
            while not stop.is_set():
                try:
                    run_transform(bcur, root / "transform", project_dir=root)
                except Exception as e:  # pragma: no cover - reported below
                    batch_errors.append(str(e))
                time.sleep(0.01)
        finally:
            bcur.close()

    t = threading.Thread(target=batch_loop)
    t.start()
    try:
        expected = 0
        for i in range(1, 61):
            cur.execute("INSERT INTO landing.ev (x) VALUES (?)", [i])
            advance_source(cur, "landing.ev")
            expected += i
            if i % 7 == 0:
                time.sleep(0.05)
        wait_until(lambda: _exists(cur, "gold", "total")
                   and q(cur, "SELECT n, s FROM gold.total") == [(60, expected)], timeout=30)
    finally:
        stop.set()
        t.join(30)
        runner.stop()
    assert q(cur, "SELECT count(*), count(DISTINCT x) FROM bronze.ev") == [(60, 60)]
    assert consumed_watermarks(cur, "bronze.ev") == {"landing.ev": 60}
    assert batch_errors == []
    cur.close()
    wq.close()


def test_runner_skips_model_a_batch_run_is_building(chain):
    from havn.engine.transform.locks import model_lock

    root, wq, cur = chain
    runner = LiveRunner(root, wq, settings=FAST)
    runner._load_models(force=True)
    runner._call(runner._load_states)
    insert_events(cur, [(1, 10, "eu", "I", 1)])
    advance_source(cur, "landing.orders")
    held = threading.Event()
    release = threading.Event()

    def hold():
        with model_lock("bronze.orders"):
            held.set()
            release.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    try:
        results = {r.model: r.status for r in runner.run_cycle()}
        assert results["bronze.orders"] == "busy"
    finally:
        release.set()
        t.join()
    results = {r.model: r.status for r in runner.run_cycle()}
    assert results["bronze.orders"] == "built"
    assert results["gold.by_region"] == "built"


def test_live_interval_spaces_refreshes(tmp_path):
    root = write_project(tmp_path, {
        "bronze/ev.sql": (
            "@config materialized=incremental, live=true, live_interval=30s, incremental_strategy=append, "
            "incremental_filter=WHERE _havn_seq > {watermark}\n"
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
        cur.execute("INSERT INTO landing.ev VALUES (1)")
        advance_source(cur, "landing.ev")
        assert [r.status for r in runner.run_cycle()] == ["built"]
        cur.execute("INSERT INTO landing.ev (x) VALUES (2)")
        advance_source(cur, "landing.ev")
        assert runner.run_cycle() == []  # within live_interval
        assert q(cur, "SELECT count(*) FROM bronze.ev") == [(1,)]
    finally:
        cur.close()
        wq.close()


def test_freshness_understands_live_models(chain):
    from havn.engine.transform.analysis import check_freshness

    root, wq, cur = chain
    runner = LiveRunner(root, wq, settings=FAST)
    runner._load_models(force=True)
    runner._call(runner._load_states)
    insert_events(cur, [(1, 10, "eu", "I", 1)])
    advance_source(cur, "landing.orders")
    runner.run_cycle()
    # Pretend the last build was long ago: a caught-up live model is fresh.
    cur.execute("UPDATE _havn.model_state SET last_run_at = TIMESTAMP '2000-01-01'")
    rows = {r["model"]: r for r in check_freshness(cur, max_age_hours=1, transform_dir=root / "transform")}
    assert rows["bronze.orders"]["live"] is True
    assert rows["bronze.orders"]["is_stale"] is False
    # Data waiting longer than live.max_lag makes it stale.
    insert_events(cur, [(2, 10, "eu", "I", 2)])
    advance_source(cur, "landing.orders")
    cur.execute("UPDATE _havn.live_advances SET origin_at = TIMESTAMP '2000-01-01' WHERE wm_to = 2 AND source='landing.orders'")
    rows = {r["model"]: r for r in check_freshness(cur, max_age_hours=1, transform_dir=root / "transform")}
    assert rows["bronze.orders"]["is_stale"] is True
    assert rows["bronze.orders"]["lag_seconds"] > 300


# ---------------------------------------------------------------------------
# Plumbing the feature depends on
# ---------------------------------------------------------------------------


def test_begin_transaction_joins_an_open_one_without_aborting_it(tmp_path):
    """A second BEGIN aborts DuckDB's outer transaction, so joining must not send one."""
    from havn.engine.utils import begin_transaction, in_transaction

    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    conn.execute("CREATE TABLE t (x INTEGER)")
    assert not in_transaction(conn)
    assert begin_transaction(conn) is True
    conn.execute("INSERT INTO t VALUES (1)")
    assert in_transaction(conn)
    assert begin_transaction(conn) is False  # joined
    conn.execute("INSERT INTO t VALUES (2)")  # the outer transaction still works
    conn.execute("COMMIT")
    assert q(conn, "SELECT count(*) FROM t") == [(2,)]


def test_named_watermark_placeholder_survives_sql_round_trips():
    from havn.engine.sql_rewrite import mask_placeholders, rewrite_table_refs

    sql = "SELECT * FROM landing.a WHERE _havn_seq > {watermark:landing.a} AND y > {watermark}"
    masked, restore = mask_placeholders(sql)
    assert "{" not in masked
    assert restore(masked) == sql
    out = rewrite_table_refs(sql, {"landing.a": "other.a"})
    assert "{watermark:landing.a}" in out and "{watermark}" in out and "other.a" in out


def test_bind_pass_sees_watermark_as_a_number(tmp_path):
    from havn.engine.transform.bind import _bindable_query

    root = write_project(tmp_path, {
        "bronze/a.sql": (
            "@config materialized=incremental, live=true, unique_key=id\n"
            "SELECT id FROM landing.a WHERE _havn_seq > {watermark}\n"
        ),
    })
    model = discover_models(root / "transform")[0]
    assert model.live and "{watermark}" not in _bindable_query(model)
    assert "_havn_seq > 0" in _bindable_query(model)


def test_live_flag_is_not_part_of_the_content_hash(tmp_path):
    a = write_project(tmp_path / "a", {"bronze/m.sql": "@config materialized=incremental, unique_key=id\nSELECT 1 AS id\n"})
    b = write_project(tmp_path / "b", {"bronze/m.sql": "@config materialized=incremental, unique_key=id, live=true\nSELECT 1 AS id\n"})
    ma, mb = discover_models(a / "transform")[0], discover_models(b / "transform")[0]
    assert not ma.live and mb.live
    assert ma.content_hash == mb.content_hash


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_live_once_and_status(tmp_path):
    import json as _json

    from typer.testing import CliRunner

    from havn.cli import app

    root = write_project(tmp_path, {
        "bronze/ev.sql": (
            "@config materialized=incremental, live=true, incremental_strategy=append, "
            "incremental_filter=WHERE _havn_seq > {watermark}\n"
            "SELECT x, _havn_seq FROM landing.ev\n"
        ),
    })
    conn = duckdb.connect(str(root / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA landing")
    conn.execute("CREATE TABLE landing.ev (x INTEGER)")
    conn.execute("INSERT INTO landing.ev VALUES (1), (2)")
    conn.close()

    cli = CliRunner()
    r = cli.invoke(app, ["live", "advance", "landing.ev", "-p", str(root)])
    assert r.exit_code == 0, r.output
    assert "advanced by 2" in r.output
    r = cli.invoke(app, ["live", "--once", "-p", str(root)])
    assert r.exit_code == 0, r.output
    assert "built" in r.output and "bronze.ev" in r.output
    r = cli.invoke(app, ["live", "pause", "bronze.ev", "-p", str(root)])
    assert r.exit_code == 0, r.output
    r = cli.invoke(app, ["live", "status", "--json", "-p", str(root)])
    assert r.exit_code == 0, r.output
    status = _json.loads(r.output)
    assert status["models"][0]["model"] == "bronze.ev"
    assert status["models"][0]["status"] == "paused"
    assert status["sources"][0]["watermark"] == 2
    r = cli.invoke(app, ["live", "pause", "bronze.nope", "-p", str(root)])
    assert r.exit_code == 1
