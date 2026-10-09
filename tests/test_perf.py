"""Performance advisor: plan capture, regressions, advice rules, critical path, CLI, API."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from havn.engine.database import ensure_meta_table, log_run
from havn.engine.instrumentation import clear_settings_cache
from havn.engine.perf import (
    BuildRecord,
    compute_advice,
    critical_path,
    detect_run_regressions,
    diff_plans,
    get_build,
    list_regressions,
    model_history,
    prune,
    record_build,
    robust_z,
    set_advice_state,
)
from havn.engine.perf.capture import BuildCapture
from havn.engine.perf.regression import check_build
from havn.engine.transform import run_transform


@pytest.fixture(autouse=True)
def _fresh_settings():
    clear_settings_cache()
    yield
    clear_settings_cache()


def _project(tmp_path: Path, extra_yml: str = "") -> Path:
    (tmp_path / "project.yml").write_text(
        "name: perfproj\ndatabase:\n  path: warehouse.duckdb\n" + extra_yml, encoding="utf-8"
    )
    for sub in ("bronze", "silver", "gold"):
        (tmp_path / "transform" / sub).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _model(project: Path, name: str, sql: str) -> None:
    schema, table = name.split(".")
    path = project / "transform" / schema / f"{table}.sql"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sql, encoding="utf-8")


def _connect(project: Path) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    ensure_meta_table(conn)
    return conn


def _run(conn, project: Path, **kw):
    return run_transform(conn, project / "transform", project_dir=project, **kw)


def _fake_build(conn, model: str, duration_ms: int, *, rows=1_000_000, when=None, plan=None,
                status="success", run_id=None, materialized="table", rows_scanned=None) -> str:
    when = when or datetime.now()
    cap = None
    if plan is not None or rows_scanned is not None:
        cap = BuildCapture(model)
        cap.statements = 1
        cap.plan = plan
        cap.rows_scanned = rows_scanned if rows_scanned is not None else rows
    return record_build(conn, BuildRecord(
        model=model, pipeline_run_id=run_id, status=status, materialized=materialized,
        strategy=None, started_at=when - timedelta(milliseconds=duration_ms), finished_at=when,
        duration_ms=duration_ms, rows_out=rows, rows_in=rows, capture=cap,
    ))


# ---------------------------------------------------------------------------
# Plan capture
# ---------------------------------------------------------------------------


def test_a_table_build_records_its_plan_from_the_real_run(tmp_path):
    project = _project(tmp_path)
    _model(project, "bronze.orders", "@config materialized=table\nSELECT range AS id, range % 7 AS k FROM range(50000)\n")
    _model(project, "silver.by_k", "@config materialized=table\nSELECT k, count(*) AS n FROM bronze.orders GROUP BY k\n")
    conn = _connect(project)
    try:
        assert _run(conn, project) == {"bronze.orders": "built", "silver.by_k": "built"}
        rows = model_history(conn, "silver.by_k")
        assert len(rows) == 1
        b = get_build(conn, rows[0]["id"])
        assert b["plan_captured"] is True
        assert b["rows_out"] == 7
        assert b["rows_in"] == 50000          # upstream size, from the catalog
        assert b["rows_scanned"] == 50000     # from the profile
        assert b["full_refresh"] is True
        ops = [n["operator"] for n in _walk(b["plan"])]
        assert any("GROUP_BY" in op for op in ops)
        scan = next(n for n in _walk(b["plan"]) if n.get("table") == "bronze.orders")
        assert scan["actual_rows"] == 50000 and scan["actual_time_ms"] >= 0
        assert b["top_operators"] and b["top_operators"][0]["time_ms"] > 0
        # Profiling is switched back off afterwards.
        assert conn.execute("SELECT current_setting('enable_profiling')").fetchone()[0] is None
    finally:
        conn.close()


def _walk(plan):
    yield plan
    for c in plan.get("children") or []:
        yield from _walk(c)


def test_profiling_left_on_by_someone_else_stays_on(tmp_path):
    project = _project(tmp_path)
    _model(project, "bronze.t", "@config materialized=table\nSELECT 1 AS x\n")
    conn = _connect(project)
    try:
        conn.execute("PRAGMA enable_profiling='no_output'")
        _run(conn, project)
        assert conn.execute("SELECT current_setting('enable_profiling')").fetchone()[0] == "no_output"
    finally:
        conn.close()


def test_capture_plans_false_keeps_timings_but_no_plan(tmp_path):
    project = _project(tmp_path, "performance:\n  capture_plans: false\n")
    _model(project, "bronze.t", "@config materialized=table\nSELECT range AS x FROM range(10)\n")
    conn = _connect(project)
    try:
        _run(conn, project)
        (row,) = model_history(conn, "bronze.t")
        assert row["plan_captured"] is False
        assert row["rows_out"] == 10 and row["duration_ms"] is not None
    finally:
        conn.close()


def test_sampled_capture_still_profiles_a_model_with_few_plans(tmp_path):
    project = _project(tmp_path, "performance:\n  capture_plans: sampled\n  sample_rate: 0\n")
    _model(project, "bronze.t", "@config materialized=table\nSELECT range AS x FROM range(10)\n")
    conn = _connect(project)
    try:
        for _ in range(5):
            _run(conn, project, force=True)
        flags = [r["plan_captured"] for r in reversed(model_history(conn, "bronze.t"))]
        assert flags == [True, True, True, False, False]
    finally:
        conn.close()


def test_performance_disabled_records_nothing(tmp_path):
    project = _project(tmp_path, "performance:\n  enabled: false\n")
    _model(project, "bronze.t", "@config materialized=table\nSELECT 1 AS x\n")
    conn = _connect(project)
    try:
        _run(conn, project)
        assert model_history(conn, "bronze.t") == []
    finally:
        conn.close()


def test_invalid_capture_mode_is_a_config_error(tmp_path):
    from havn.config import load_project

    project = _project(tmp_path, "performance:\n  capture_plans: sometimes\n")
    with pytest.raises(Exception, match="capture_plans"):
        load_project(project)


def test_incremental_records_full_load_then_delta(tmp_path):
    project = _project(tmp_path)
    _model(project, "bronze.src", "@config materialized=table\nSELECT range AS id FROM range(100)\n")
    _model(project, "silver.inc", "@config materialized=incremental, unique_key=id\nSELECT id FROM bronze.src\n")
    conn = _connect(project)
    try:
        _run(conn, project)
        _run(conn, project, force=True)
        first, second = reversed(model_history(conn, "silver.inc"))
        assert first["full_refresh"] is True and second["full_refresh"] is False
        assert second["rows_before"] == 100 and second["plan_captured"] is True
    finally:
        conn.close()


def test_a_failed_build_is_recorded_as_an_error(tmp_path):
    project = _project(tmp_path)
    _model(project, "bronze.bad", "@config materialized=table\nSELECT * FROM landing.missing\n")
    conn = _connect(project)
    try:
        assert _run(conn, project)["bronze.bad"] == "error"
        (row,) = model_history(conn, "bronze.bad", status=None)
        assert row["status"] == "error" and "missing" in (row["error"] or "")
    finally:
        conn.close()


def test_parallel_workers_record_their_builds_too(tmp_path):
    project = _project(tmp_path)
    _model(project, "bronze.a", "@config materialized=table\nSELECT 1 AS x\n")
    _model(project, "silver.b", "@config materialized=table\nSELECT x FROM bronze.a\n")
    _model(project, "silver.c", "@config materialized=table\nSELECT x + 1 AS y FROM bronze.a\n")
    conn = _connect(project)
    try:
        _run(conn, project, parallel=True, db_path=str(project / "warehouse.duckdb"))
        runs = {r["model_path"]: r for r in conn.execute(
            "SELECT model_path, pipeline_run_id, plan_captured FROM _havn.model_perf"
        ).fetchdf().to_dict("records")}
        assert set(runs) == {"bronze.a", "silver.b", "silver.c"}
        assert len({r["pipeline_run_id"] for r in runs.values()}) == 1
        assert all(r["plan_captured"] for r in runs.values())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Regressions
# ---------------------------------------------------------------------------


def test_robust_z_ignores_one_outlier_in_history():
    history = [1000, 1010, 990, 1005, 995, 1002, 30_000]  # one pathological run
    z, med, mad = robust_z(4000, history)
    assert med == 1002
    assert z > 3.5  # still clearly slow: the outlier did not inflate the scale


def _history(conn, model, durations, *, rows=1_000_000, plan=None, start=None):
    start = start or datetime.now() - timedelta(days=2)
    ids = []
    for i, d in enumerate(durations):
        ids.append(_fake_build(conn, model, d, rows=rows, when=start + timedelta(minutes=i), plan=plan))
    return ids


_FAST_PLAN = {
    "operator": "HASH_GROUP_BY", "actual_rows": 10, "actual_time_ms": 50.0,
    "children": [{
        "operator": "HASH_JOIN", "actual_rows": 1_000_000, "actual_time_ms": 200.0,
        "extra_info": {"Join Type": "INNER", "Conditions": "a.id = b.id"},
        "children": [
            {"operator": "SEQ_SCAN", "table": "silver.a", "actual_rows": 1_000_000, "actual_time_ms": 300.0},
            {"operator": "SEQ_SCAN", "table": "silver.b", "actual_rows": 1_000, "actual_time_ms": 5.0},
        ],
    }],
}
_SLOW_PLAN = {
    "operator": "HASH_GROUP_BY", "actual_rows": 10, "actual_time_ms": 60.0,
    "children": [{
        "operator": "NESTED_LOOP_JOIN", "actual_rows": 1_000_000, "actual_time_ms": 4200.0,
        "extra_info": {"Join Type": "INNER", "Conditions": "a.id >= b.id"},
        "children": [
            {"operator": "SEQ_SCAN", "table": "silver.a", "actual_rows": 1_000_000, "actual_time_ms": 310.0},
            {"operator": "SEQ_SCAN", "table": "silver.b", "actual_rows": 1_000, "actual_time_ms": 5.0},
        ],
    }],
}


def _perf_cfg(**kw):
    from havn.config import PerformanceConfig

    return PerformanceConfig(**kw)


def test_a_slow_build_is_a_regression_with_a_plan_diff(tmp_path):
    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    try:
        _history(conn, "gold.m", [1000, 1040, 980, 1010, 990, 1020, 1005], plan=_FAST_PLAN)
        slow = _fake_build(conn, "gold.m", 5000, plan=_SLOW_PLAN)
        reg = check_build(conn, slow, _perf_cfg())
        assert reg is not None
        assert reg.ratio == pytest.approx(5000 / 1005, rel=0.01)
        assert reg.normalized is True   # same rows, so per-row time regressed too
        assert reg.baseline_perf_id is not None
        diff = reg.plan_diff
        assert diff["join_changes"][0]["before"]["operator"] == "HASH_JOIN"
        assert diff["join_changes"][0]["after"]["operator"] == "NESTED_LOOP_JOIN"
        assert any("NESTED_LOOP_JOIN" in s for s in diff["summary"])
        assert "HASH_JOIN" in reg.message and "5.0 s" in reg.message
    finally:
        conn.close()


def test_slower_in_proportion_to_more_data_is_not_a_regression(tmp_path):
    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    try:
        _history(conn, "gold.m", [1000, 1040, 980, 1010, 990, 1020])
        grown = _fake_build(conn, "gold.m", 5000, rows=5_000_000)
        assert check_build(conn, grown, _perf_cfg()) is None
    finally:
        conn.close()


def test_small_absolute_slowdowns_and_short_history_are_ignored(tmp_path):
    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    try:
        _history(conn, "gold.tiny", [40, 41, 39, 40, 42, 40])
        assert check_build(conn, _fake_build(conn, "gold.tiny", 200), _perf_cfg()) is None  # +160 ms
        _history(conn, "gold.young", [1000, 1000])
        assert check_build(conn, _fake_build(conn, "gold.young", 9000), _perf_cfg()) is None
    finally:
        conn.close()


def test_detected_regressions_are_saved_and_alerted(tmp_path):
    from havn.config import AlertsConfig
    from havn.engine.perf import alert_regressions

    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    try:
        ensure_meta_table(conn)
        _history(conn, "gold.m", [1000, 1040, 980, 1010, 990, 1020])
        slow = _fake_build(conn, "gold.m", 6000, run_id="run-1")
        regs = detect_run_regressions(conn, [slow], _perf_cfg())
        assert len(regs) == 1
        saved = list_regressions(conn, days=1)
        assert saved[0]["model_path"] == "gold.m" and saved[0]["pipeline_run_id"] == "run-1"
        assert alert_regressions(regs, AlertsConfig(channels=["log"]), conn) == 1
        row = conn.execute(
            "SELECT alert_type, target, status FROM _havn.alert_log"
        ).fetchone()
        assert row == ("perf_regression", "gold.m", "sent")
        assert conn.execute("SELECT alerted FROM _havn.perf_regressions").fetchone()[0] is True
    finally:
        conn.close()


def test_a_real_run_that_regresses_is_flagged_at_the_end_of_the_run(tmp_path, capsys):
    project = _project(
        tmp_path,
        "performance:\n  regression_min_delta_ms: 0\n  regression_min_history: 3\n",
    )
    _model(project, "bronze.big", "@config materialized=table\n"
           "SELECT range AS id, md5(range::VARCHAR) AS h FROM range(300000)\n")
    conn = _connect(project)
    try:
        ensure_meta_table(conn)
        # A history of implausibly fast builds of the same size.
        _history(conn, "bronze.big", [1, 1, 1, 1], rows=300_000)
        conn.execute("UPDATE _havn.model_perf SET rows_scanned = NULL, rows_in = NULL")
        _run(conn, project)
        regs = list_regressions(conn, days=1, model="bronze.big")
        assert len(regs) == 1
        assert "slower" in capsys.readouterr().out
    finally:
        conn.close()


def test_diff_plans_ranks_operators_by_slowdown():
    diff = diff_plans(_FAST_PLAN, _FAST_PLAN | {"actual_time_ms": 900.0})
    assert diff["operators"][0]["operator"] == "HASH_GROUP_BY"
    assert diff["operators"][0]["delta_ms"] == pytest.approx(850.0)
    assert diff["join_changes"] == []


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_prune_drops_old_rows_and_old_plans(tmp_path):
    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    try:
        old = datetime.now() - timedelta(days=200)
        _fake_build(conn, "gold.m", 10, when=old, plan=_FAST_PLAN)
        for i in range(4):
            _fake_build(conn, "gold.m", 10, when=datetime.now() - timedelta(minutes=10 - i), plan=_FAST_PLAN)
        prune(conn, retention_days=90, plan_retention=2)
        rows = model_history(conn, "gold.m")
        assert len(rows) == 4
        assert [r["plan_captured"] for r in rows] == [True, True, False, False]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Critical path
# ---------------------------------------------------------------------------


def _b(model, start_ms, dur_ms, base=datetime(2026, 1, 1, 12, 0, 0)):
    s = base + timedelta(milliseconds=start_ms)
    return {"model_path": model, "started_at": s.isoformat(),
            "finished_at": (s + timedelta(milliseconds=dur_ms)).isoformat(),
            "duration_ms": dur_ms, "status": "success"}


def test_critical_path_follows_what_the_run_waited_for():
    # a -> b (fast), a -> c (slow) -> d ; e is independent and short.
    deps = {"a": [], "b": ["a"], "c": ["a"], "d": ["c", "b"], "e": []}
    builds = [_b("a", 0, 100), _b("e", 0, 50), _b("b", 110, 20), _b("c", 110, 500), _b("d", 700, 100)]
    cp = critical_path(builds, deps)
    assert [p["model"] for p in cp["path"]] == ["a", "c", "d"]
    assert cp["path"][-1]["wait_ms"] == 90       # d started 90 ms after c finished
    assert cp["wall_ms"] == 800
    assert [c["model"] for c in cp["longest_chain"]] == ["a", "c", "d"]
    assert cp["longest_chain_ms"] == 700
    assert cp["tiers"] == 3
    assert {m["model"]: m["on_path"] for m in cp["models"]}["e"] is False


def test_critical_path_looks_through_models_that_did_not_build():
    deps = {"a": [], "skipped": ["a"], "z": ["skipped"]}
    cp = critical_path([_b("a", 0, 100), _b("z", 100, 100)], deps)
    assert [p["model"] for p in cp["path"]] == ["a", "z"]
    assert cp["longest_chain_ms"] == 200


# ---------------------------------------------------------------------------
# Advice rules
# ---------------------------------------------------------------------------


def _advice_cfg(**kw):
    from havn.config import PerfAdviceConfig

    base = dict(big_table_rows=1000, min_duration_ms=10, fanout_min_rows=1000, udf_rows=100)
    base.update(kw)
    return PerfAdviceConfig(**base)


def _rules(items, model=None):
    return {(i.rule, i.model) for i in items if model is None or i.model == model}


def test_incremental_candidate_for_an_append_only_full_refresh_table(tmp_path):
    project = _project(tmp_path)
    _model(project, "silver.events", "@config materialized=table\nSELECT * FROM bronze.raw\n")
    conn = _connect(project)
    try:
        conn.execute("CREATE SCHEMA silver")
        conn.execute("CREATE TABLE silver.events AS SELECT range AS event_id, "
                     "TIMESTAMP '2024-01-01' + to_seconds(range) AS updated_at, 'x' AS payload FROM range(5000)")
        conn.execute("INSERT INTO _havn.model_profiles (model_path, row_count, column_count, distinct_counts) "
                     "VALUES ('silver.events', 5000, 3, '{\"event_id\": 5000, \"updated_at\": 5000, \"payload\": 1}')")
        base = datetime.now() - timedelta(days=1)
        for i, rows in enumerate([4700, 4800, 4900, 5000]):
            _fake_build(conn, "silver.events", 200, rows=rows, when=base + timedelta(hours=i))
        items = compute_advice(conn, project, _advice_cfg())
        (item,) = [i for i in items if i.rule == "incremental_candidate"]
        assert item.model == "silver.events"
        assert "unique_key=event_id" in item.suggestion
        assert "@watermark updated_at" in item.suggestion
        assert item.evidence["row_history"] == [4700, 4800, 4900, 5000]
        # Shrinking at some point means it is not append-only.
        _fake_build(conn, "silver.events", 200, rows=100, when=base + timedelta(hours=5))
        assert "incremental_candidate" not in {i.rule for i in compute_advice(conn, project, _advice_cfg())}
    finally:
        conn.close()


def test_join_fanout_from_the_plan(tmp_path):
    project = _project(tmp_path)
    _model(project, "gold.j", "@config materialized=table\nSELECT * FROM silver.a JOIN silver.b USING (k)\n")
    conn = _connect(project)
    try:
        plan = {"operator": "CREATE_TABLE_AS", "children": [{
            "operator": "HASH_JOIN", "actual_rows": 500_000, "actual_time_ms": 900.0,
            "extra_info": {"Join Type": "INNER", "Conditions": "k = k"},
            "children": [
                {"operator": "SEQ_SCAN", "table": "silver.a", "actual_rows": 5_000},
                {"operator": "SEQ_SCAN", "table": "silver.b", "actual_rows": 2_000},
            ],
        }]}
        _fake_build(conn, "gold.j", 1000, plan=plan)
        (item,) = [i for i in compute_advice(conn, project, _advice_cfg()) if i.rule == "join_fanout"]
        assert item.evidence["ratio"] == 100.0
        assert item.evidence["conditions"] == "k = k"
        assert "100x" in item.explanation
    finally:
        conn.close()


def test_materialize_a_heavy_view_read_by_many(tmp_path):
    project = _project(tmp_path)
    _model(project, "silver.v", "@config materialized=view\nSELECT a.k, count(*) n FROM bronze.a a JOIN bronze.b b USING (k) GROUP BY 1\n")
    for i in range(3):
        _model(project, f"gold.r{i}", f"@config materialized=table\nSELECT * FROM silver.v WHERE n > {i}\n")
    _model(project, "silver.thin", "@config materialized=view\nSELECT k FROM bronze.a\n")
    for i in range(3):
        _model(project, f"gold.t{i}", f"@config materialized=table\nSELECT * FROM silver.thin WHERE k > {i}\n")
    conn = _connect(project)
    try:
        items = compute_advice(conn, project, _advice_cfg())
        assert ("materialize_view", "silver.v") in _rules(items)
        assert ("materialize_view", "silver.thin") not in _rules(items)  # a plain projection is fine as a view
    finally:
        conn.close()


def test_unused_table_needs_age_and_no_reads(tmp_path):
    project = _project(tmp_path)
    _model(project, "gold.orphan", "@config materialized=table\nSELECT 1 AS x\n")
    _model(project, "gold.young", "@config materialized=table\nSELECT 1 AS x\n")
    _model(project, "gold.read", "@config materialized=table\nSELECT 1 AS x\n")
    conn = _connect(project)
    try:
        for name, days in (("gold.orphan", 40), ("gold.young", 2), ("gold.read", 40)):
            log_run(conn, "transform", name, "success", 10, 1)
            conn.execute(
                "UPDATE _havn.run_log SET started_at = current_timestamp - to_days(CAST(? AS INTEGER)) WHERE target = ?",
                [days, name],
            )
        from havn.engine.audit import log_audit

        log_audit(conn, user="ana", action="query", resource="SELECT * FROM gold.read LIMIT 5")
        rules = _rules(compute_advice(conn, project, _advice_cfg()))
        assert ("unused_table", "gold.orphan") in rules
        assert ("unused_table", "gold.young") not in rules
        assert ("unused_table", "gold.read") not in rules
    finally:
        conn.close()


def test_unused_table_respects_exposures(tmp_path):
    from havn.config import ExposureConfig

    project = _project(tmp_path)
    _model(project, "gold.orphan", "@config materialized=table\nSELECT 1 AS x\n")
    conn = _connect(project)
    try:
        log_run(conn, "transform", "gold.orphan", "success", 10, 1)
        conn.execute("UPDATE _havn.run_log SET started_at = current_timestamp - INTERVAL 40 DAY")
        exp = [ExposureConfig(name="bi", depends_on=["gold.orphan"])]
        assert ("unused_table", "gold.orphan") not in _rules(
            compute_advice(conn, project, _advice_cfg(), exposures=exp)
        )
    finally:
        conn.close()


def test_scan_small_slice_with_and_without_a_filter_above(tmp_path):
    project = _project(tmp_path)
    _model(project, "gold.pushed", "@config materialized=table\nSELECT * FROM silver.big WHERE d = 1\n")
    _model(project, "gold.wrapped", "@config materialized=view\nSELECT * FROM silver.big WHERE lower(s) = 'a'\n")
    conn = _connect(project)
    try:
        _fake_build(conn, "gold.pushed", 500, plan={"operator": "SEQ_SCAN", "table": "silver.big",
            "rows_scanned": 2_000_000, "actual_rows": 1_000, "extra_info": {"Filters": "d=1"}})
        _fake_build(conn, "gold.wrapped", 500, plan={"operator": "FILTER", "actual_rows": 500,
            "extra_info": {"Expression": "(lower(s) = 'a')"},
            "children": [{"operator": "SEQ_SCAN", "table": "silver.big", "rows_scanned": 2_000_000,
                          "actual_rows": 2_000_000}]})
        items = {i.model: i for i in compute_advice(conn, project, _advice_cfg()) if i.rule == "scan_small_slice"}
        assert items["gold.pushed"].evidence["filter_above_scan"] is False
        assert "incremental" in items["gold.pushed"].suggestion       # it is a table
        assert items["gold.wrapped"].evidence["filter_above_scan"] is True
        assert "not pushed" in items["gold.wrapped"].explanation
        # Under the size threshold nothing fires.
        quiet = compute_advice(conn, project, _advice_cfg(big_table_rows=10_000_000))
        assert not [i for i in quiet if i.rule == "scan_small_slice"]
    finally:
        conn.close()


def test_order_by_only_flagged_when_someone_reads_the_model(tmp_path):
    project = _project(tmp_path)
    _model(project, "silver.sorted", "@config materialized=table\nSELECT 1 AS x ORDER BY x\n")
    _model(project, "gold.final", "@config materialized=table\nSELECT x FROM silver.sorted ORDER BY x\n")
    _model(project, "silver.topn", "@config materialized=table\nSELECT 1 AS x ORDER BY x LIMIT 5\n")
    _model(project, "gold.reader", "@config materialized=table\nSELECT x FROM silver.topn\n")
    _model(project, "silver.window", "@config materialized=table\nSELECT row_number() OVER (ORDER BY 1) AS r\n")
    _model(project, "gold.wreader", "@config materialized=table\nSELECT r FROM silver.window\n")
    conn = _connect(project)
    try:
        rules = _rules(compute_advice(conn, project, _advice_cfg()))
        assert ("order_by_non_final", "silver.sorted") in rules
        assert ("order_by_non_final", "gold.final") not in rules   # final model
        assert ("order_by_non_final", "silver.topn") not in rules  # ORDER BY + LIMIT is a top-N
        assert ("order_by_non_final", "silver.window") not in rules
    finally:
        conn.close()


def test_distinct_on_a_large_input(tmp_path):
    project = _project(tmp_path)
    _model(project, "silver.d", "@config materialized=table\nSELECT DISTINCT a, b FROM bronze.x\n")
    _model(project, "silver.small", "@config materialized=table\nSELECT DISTINCT a FROM bronze.x\n")
    conn = _connect(project)
    try:
        plan = {"operator": "HASH_GROUP_BY", "actual_rows": 900_000,
                "children": [{"operator": "SEQ_SCAN", "table": "bronze.x", "actual_rows": 1_000_000}]}
        _fake_build(conn, "silver.d", 800, rows=900_000, plan=plan)
        _fake_build(conn, "silver.small", 800, rows=10, plan={"operator": "HASH_GROUP_BY", "actual_rows": 10,
                    "children": [{"operator": "SEQ_SCAN", "actual_rows": 50}]})
        conn.execute("UPDATE _havn.model_perf SET rows_in = NULL WHERE model_path = 'silver.small'")
        items = [i for i in compute_advice(conn, project, _advice_cfg(big_table_rows=100_000)) if i.rule == "distinct_large"]
        assert [i.model for i in items] == ["silver.d"]
        assert items[0].evidence["rows_in"] == 1_000_000
    finally:
        conn.close()


def test_python_udf_on_many_rows(tmp_path):
    project = _project(tmp_path)
    (project / "macros").mkdir()
    (project / "macros" / "util.py").write_text(
        "from havn import macro\n\n@macro\ndef shout(s: str) -> str:\n    return s.upper()\n", encoding="utf-8"
    )
    _model(project, "silver.loud", "@config materialized=table\nSELECT shout(name) AS n FROM bronze.people\n")
    _model(project, "silver.quiet", "@config materialized=table\nSELECT upper(name) AS n FROM bronze.people\n")
    conn = _connect(project)
    try:
        _fake_build(conn, "silver.loud", 900, rows=50_000)
        _fake_build(conn, "silver.quiet", 900, rows=50_000)
        items = [i for i in compute_advice(conn, project, _advice_cfg()) if i.rule == "python_udf_hot_path"]
        assert [i.model for i in items] == ["silver.loud"]
        assert items[0].evidence["functions"] == ["shout"]
    finally:
        conn.close()


def test_dismiss_snooze_and_reopen(tmp_path):
    project = _project(tmp_path)
    _model(project, "silver.sorted", "@config materialized=table\nSELECT 1 AS x ORDER BY x\n")
    _model(project, "gold.r", "@config materialized=table\nSELECT x FROM silver.sorted\n")
    conn = _connect(project)
    try:
        key = ("order_by_non_final", "silver.sorted")
        assert key in _rules(compute_advice(conn, project, _advice_cfg()))
        set_advice_state(conn, "silver.sorted", "order_by_non_final", "dismissed")
        assert key not in _rules(compute_advice(conn, project, _advice_cfg()))
        shown = [i for i in compute_advice(conn, project, _advice_cfg(), include_dismissed=True)
                 if (i.rule, i.model) == key]
        assert shown[0].status == "dismissed"
        set_advice_state(conn, "silver.sorted", "order_by_non_final", "snoozed", days=3)
        snoozed = [i for i in compute_advice(conn, project, _advice_cfg(), include_dismissed=True)
                   if (i.rule, i.model) == key][0]
        assert snoozed.status == "snoozed" and snoozed.snoozed_until
        # An expired snooze is open again.
        conn.execute("UPDATE _havn.perf_advice_state SET until = current_timestamp - INTERVAL 1 DAY")
        assert key in _rules(compute_advice(conn, project, _advice_cfg()))
        set_advice_state(conn, "silver.sorted", "order_by_non_final", "open")
        assert conn.execute("SELECT count(*) FROM _havn.perf_advice_state").fetchone()[0] == 0
        with pytest.raises(ValueError):
            set_advice_state(conn, "silver.sorted", "no_such_rule", "dismissed")
        with pytest.raises(ValueError):
            set_advice_state(conn, "silver.sorted", "order_by_non_final", "snoozed")
    finally:
        conn.close()


def test_default_thresholds_keep_a_small_project_quiet(tmp_path):
    from havn.config import PerfAdviceConfig

    project = _project(tmp_path)
    _model(project, "bronze.a", "@config materialized=table\nSELECT range AS id FROM range(1000)\n")
    _model(project, "silver.b", "@config materialized=table\nSELECT DISTINCT id FROM bronze.a\n")
    conn = _connect(project)
    try:
        for _ in range(4):
            _run(conn, project, force=True)
        assert compute_advice(conn, project, PerfAdviceConfig()) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI and API
# ---------------------------------------------------------------------------


def _built_project(tmp_path):
    project = _project(tmp_path)
    _model(project, "bronze.orders", "@config materialized=table\nSELECT range AS id, range % 3 AS k FROM range(1000)\n")
    _model(project, "silver.sorted", "@config materialized=table\nSELECT k, count(*) AS n FROM bronze.orders GROUP BY k ORDER BY k\n")
    _model(project, "gold.final", "@config materialized=table\nSELECT * FROM silver.sorted\n")
    conn = _connect(project)
    try:
        _run(conn, project)
        _run(conn, project, force=True)
    finally:
        conn.close()
    return project


def test_cli_summary_detail_and_critical_path(tmp_path):
    from typer.testing import CliRunner

    from havn.cli import app

    project = _built_project(tmp_path)
    runner = CliRunner()
    res = runner.invoke(app, ["perf", "-p", str(project)])
    assert res.exit_code == 0, res.output
    assert "Slowest models" in res.output and "silver.sorted" in res.output
    assert "order_by_non_final" in res.output

    res = runner.invoke(app, ["perf", "silver.sorted", "-p", str(project)])
    assert res.exit_code == 0, res.output
    assert "Recent builds" in res.output and "SEQ_SCAN" in res.output

    res = runner.invoke(app, ["perf", "--critical-path", "-p", str(project)])
    assert res.exit_code == 0, res.output
    assert "Critical path" in res.output and "gold.final" in res.output

    res = runner.invoke(app, ["perf", "silver.sorted", "--dismiss", "order_by_non_final", "-p", str(project)])
    assert res.exit_code == 0, res.output
    res = runner.invoke(app, ["perf", "--advice", "--json", "-p", str(project)])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["advice"] == []


@pytest.fixture
def api_client(tmp_path):
    from fastapi.testclient import TestClient

    import havn.server.app as server_app
    from havn.server.deps import reset_shared_conn

    project = _built_project(tmp_path)
    reset_shared_conn()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = False
    with TestClient(server_app.app) as client:
        yield client
    reset_shared_conn()


def test_api_summary_model_detail_and_advice_state(api_client):
    summary = api_client.get("/api/perf/summary").json()
    assert {s["model_path"] for s in summary["slowest"]} == {"bronze.orders", "silver.sorted", "gold.final"}
    assert summary["runs"] and summary["settings"]["capture_plans"] == "true"
    assert any(a["rule"] == "order_by_non_final" for a in summary["advice"])

    detail = api_client.get("/api/perf/models/silver.sorted").json()
    assert len(detail["history"]) == 2
    assert detail["plan"]["_total_time_ms"] >= 0      # enriched for ExplainPanel
    assert detail["plan_build"]["model_path"] == "silver.sorted"

    build_ids = [h["id"] for h in detail["history"]]
    diff = api_client.get(f"/api/perf/diff?fast={build_ids[1]}&slow={build_ids[0]}").json()
    assert "operators" in diff["diff"]

    r = api_client.post("/api/perf/advice/state", json={
        "model": "silver.sorted", "rule": "order_by_non_final", "status": "snoozed", "days": 2,
    })
    assert r.status_code == 200 and r.json()["until"]
    assert not [a for a in api_client.get("/api/perf/advice").json() if a["model"] == "silver.sorted"]
    bad = api_client.post("/api/perf/advice/state", json={"model": "x.y", "rule": "nope", "status": "dismissed"})
    assert bad.status_code == 400

    run_id = summary["runs"][0]["pipeline_run_id"]
    cp = api_client.get(f"/api/perf/runs/{run_id}/critical-path").json()
    assert [p["model"] for p in cp["path"]] == ["bronze.orders", "silver.sorted", "gold.final"]
    assert api_client.get("/api/perf/runs/nope/critical-path").status_code == 404


def test_api_on_a_warehouse_without_perf_tables(tmp_path):
    from fastapi.testclient import TestClient

    import havn.server.app as server_app
    from havn.server.deps import reset_shared_conn

    project = _project(tmp_path)
    duckdb.connect(str(project / "warehouse.duckdb")).close()
    reset_shared_conn()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = False
    with TestClient(server_app.app) as client:
        body = client.get("/api/perf/summary").json()
        assert body["slowest"] == [] and body["regressions"] == [] and body["runs"] == []
        assert client.get("/api/perf/models/gold.x").json()["history"] == []
    reset_shared_conn()
