"""Anomaly detection from real transform runs, and the parallel runner's report.

The anomaly tests in ``test_anomaly.py`` seed history by hand and call
``detect_anomalies`` with an unsaved profile. These run ``run_transform`` the
way ``havn transform`` does, where the fresh profile is saved to
``profile_history`` before detection runs, in both runners.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import duckdb
import pytest

from havn.engine.anomaly import detect_anomalies
from havn.engine.database import ensure_meta_table
from havn.engine.transform import SQLModel, run_transform
from havn.engine.transform.models import ModelResult, ProfileResult
from havn.engine.transform.orchestration import _blocked_walker, _report_worker_result
from havn.engine.transform.quality import _save_profile


@pytest.fixture
def project(tmp_path):
    t = tmp_path / "transform"
    for sub in ("bronze", "silver"):
        (t / sub).mkdir(parents=True)
    db_path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    ensure_meta_table(conn)
    conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
    yield tmp_path, conn, str(db_path)
    conn.close()


def _write_models(t: Path) -> None:
    # bronze.a and bronze.b share tier 1, so the parallel runner sends them to
    # workers; silver.c is alone in tier 2 and runs on the main connection.
    (t / "bronze" / "a.sql").write_text(
        "@config materialized=table, schema=bronze\n\nSELECT id FROM landing.src\n"
    )
    (t / "bronze" / "b.sql").write_text(
        "@config materialized=table, schema=bronze\n\nSELECT id * 2 AS id FROM landing.src\n"
    )
    (t / "silver" / "c.sql").write_text(
        "@config materialized=table, schema=silver\n\nSELECT id FROM bronze.a\n"
    )


def _load(conn, n: int) -> None:
    conn.execute(f"CREATE OR REPLACE TABLE landing.src AS SELECT range AS id FROM range({n})")


def _anomalies(conn) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for model, metric in conn.execute(
        "SELECT model_name, metric FROM _havn.anomaly_log"
    ).fetchall():
        out.setdefault(model, set()).add(metric)
    return out


@pytest.mark.parametrize("parallel", [True, False])
def test_spike_after_four_runs_is_logged(project, parallel):
    """Four ordinary runs, then a 10x spike: every built table logs an anomaly.

    With the current run inside its own baseline, five values can give at
    most |z| = 2.0, which never exceeds the 2.0 threshold, so this logged
    nothing in either runner, and the parallel runner did not detect at all.
    """
    root, conn, db_path = project
    t = root / "transform"
    _write_models(t)
    for n in (100, 102, 98, 101):
        _load(conn, n)
        results = run_transform(conn, t, force=True, parallel=parallel, db_path=db_path)
        assert set(results.values()) == {"built"}
    assert _anomalies(conn) == {}

    _load(conn, 1000)
    run_transform(conn, t, force=True, parallel=parallel, db_path=db_path)

    found = _anomalies(conn)
    for model in ("bronze.a", "bronze.b", "silver.c"):
        assert "row_count" in found.get(model, set()), (model, found)
    mean = conn.execute(
        "SELECT mean_value FROM _havn.anomaly_log "
        "WHERE model_name = 'bronze.a' AND metric = 'row_count'"
    ).fetchone()[0]
    assert mean == pytest.approx((100 + 102 + 98 + 101) / 4)


def test_ordinary_run_logs_nothing(project):
    root, conn, db_path = project
    t = root / "transform"
    _write_models(t)
    for n in (100, 102, 98, 101, 99, 100):
        _load(conn, n)
        run_transform(conn, t, force=True, parallel=True, db_path=db_path)
    assert _anomalies(conn) == {}


# ---------------------------------------------------------------------------
# Baseline: the saved current profile is left out, by row id
# ---------------------------------------------------------------------------


def _model(name: str = "silver.m", depends_on: list[str] | None = None) -> SQLModel:
    schema, short = name.split(".")
    return SQLModel(
        path=Path(f"{short}.sql"), name=short, schema=schema, full_name=name,
        sql="", query="SELECT 1", materialized="table", depends_on=depends_on or [],
    )


def _seed(conn, model: SQLModel, counts: list[int]) -> None:
    for n in counts:
        _save_profile(conn, model, ProfileResult(row_count=n, column_count=1))


@pytest.mark.parametrize("history", [[100, 102, 98], [100, 102, 98, 101], [100, 102, 98, 101, 99]])
def test_saved_profile_is_not_its_own_baseline(project, history):
    _, conn, _ = project
    model = _model()
    _seed(conn, model, history)
    current = ProfileResult(row_count=1000, column_count=1)
    _save_profile(conn, model, current)
    assert current.history_id is not None

    (rc,) = [r for r in detect_anomalies(conn, model.full_name, current) if r.metric == "row_count"]
    assert rc.mean == pytest.approx(sum(history) / len(history))
    assert rc.is_anomaly


def test_baseline_does_not_depend_on_distinct_timestamps(project):
    """Every row saved in one transaction shares current_timestamp."""
    _, conn, _ = project
    model = _model()
    conn.execute("BEGIN")
    _seed(conn, model, [100, 102, 98])
    current = ProfileResult(row_count=1000, column_count=1)
    _save_profile(conn, model, current)
    conn.execute("COMMIT")
    assert len(set(conn.execute(
        "SELECT profiled_at FROM _havn.profile_history"
    ).fetchall())) == 1

    (rc,) = [r for r in detect_anomalies(conn, model.full_name, current) if r.metric == "row_count"]
    assert rc.mean == pytest.approx(100.0)
    assert rc.is_anomaly


def test_unsaved_profile_uses_all_history(project):
    """The way test_anomaly.py calls it: history stored, current not saved."""
    _, conn, _ = project
    model = _model()
    _seed(conn, model, [100, 102, 98])
    current = ProfileResult(row_count=1000, column_count=1)
    (rc,) = [r for r in detect_anomalies(conn, model.full_name, current) if r.metric == "row_count"]
    assert rc.mean == pytest.approx(100.0)
    assert rc.is_anomaly


def test_saved_profile_with_too_little_history_is_skipped(project):
    """Two earlier runs plus the current one is not enough history."""
    _, conn, _ = project
    model = _model()
    _seed(conn, model, [100, 102])
    current = ProfileResult(row_count=1000, column_count=1)
    _save_profile(conn, model, current)
    assert detect_anomalies(conn, model.full_name, current) == []


def test_history_id_does_not_affect_equality():
    a = ProfileResult(row_count=1)
    b = ProfileResult(row_count=1)
    b.history_id = "x"
    assert a == b


# ---------------------------------------------------------------------------
# Blocked walk
# ---------------------------------------------------------------------------


def _layered(layers: int, width: int) -> dict[str, SQLModel]:
    models: dict[str, SQLModel] = {}
    for layer in range(layers):
        for i in range(width):
            deps = [f"s.l{layer - 1}_{j}" for j in range(width)] if layer else []
            m = _model(f"s.l{layer}_{i}", deps)
            models[m.full_name] = m
    return models


def test_blocked_walk_is_linear_on_diamonds():
    """25 layers of 3 fully connected models: 3**24 paths from the top."""
    model_map = _layered(25, 3)
    failed: set[str] = set()
    is_blocked = _blocked_walker(model_map, failed)
    start = time.perf_counter()
    for name in model_map:  # tier order, nothing failed
        assert is_blocked(name) is None
    assert time.perf_counter() - start < 2.0


def test_blocked_walk_sees_failures_added_between_tiers():
    model_map = _layered(25, 3)
    failed: set[str] = set()
    is_blocked = _blocked_walker(model_map, failed)
    # Tier 0 checked and built; one model of tier 5 fails after being checked.
    for layer in range(6):
        for i in range(3):
            assert is_blocked(f"s.l{layer}_{i}") is None
    failed.add("s.l5_1")
    for i in range(3):
        assert is_blocked(f"s.l6_{i}") == "s.l5_1"
        failed.add(f"s.l6_{i}")  # the runner marks skipped models failed too
    for layer in range(7, 25):
        for i in range(3):
            assert is_blocked(f"s.l{layer}_{i}") is not None
            failed.add(f"s.l{layer}_{i}")


def test_blocked_walk_only_blocks_descendants():
    model_map = {
        m.full_name: m for m in (
            _model("s.a"), _model("s.b"),
            _model("s.c", ["s.a"]), _model("s.d", ["s.b"]),
            _model("s.e", ["s.c", "s.d"]),
            # Through an unselected-looking intermediate not in `failed`.
            _model("s.f", ["s.c"]), _model("s.g", ["s.f"]),
        )
    }
    failed = {"s.a"}
    is_blocked = _blocked_walker(model_map, failed)
    assert is_blocked("s.b") is None
    assert is_blocked("s.d") is None
    assert is_blocked("s.c") == "s.a"
    assert is_blocked("s.e") == "s.a"
    assert is_blocked("s.g") == "s.a"


def test_parallel_run_on_a_diamond_dag_finishes_and_blocks_descendants(project):
    """18 layers of 3 fully connected models through run_transform.

    The walk only recursed past ancestors that had not failed, so it was
    exponential exactly when nothing upstream failed: 3**17 paths for the
    last layer, which did not finish. A failing model beside the diamond must
    block what reads it and nothing else.
    """
    root, conn, db_path = project
    t = root / "transform"
    layers, width = 18, 3
    for layer in range(layers):
        for i in range(width):
            if layer == 0:
                body = f"SELECT {i} AS v"
            else:
                # Tables of one row each: a view over views would make
                # DuckDB itself expand every path.
                body = "SELECT max(v) AS v FROM (" + " UNION ALL ".join(
                    f"SELECT v FROM silver.l{layer - 1}_{j}" for j in range(width)
                ) + ")"
            (t / "silver" / f"l{layer}_{i}.sql").write_text(
                f"@config materialized=table, schema=silver\n\n{body}\n"
            )
    (t / "silver" / "bad.sql").write_text(
        "@config materialized=table, schema=silver\n\nSELECT error('nope') AS v\n"
    )
    (t / "silver" / "after_bad.sql").write_text(
        "@config materialized=view, schema=silver\n\n"
        f"SELECT v FROM silver.bad UNION ALL SELECT v FROM silver.l{layers - 1}_0\n"
    )
    start = time.perf_counter()
    results = run_transform(conn, t, force=True, parallel=True, db_path=db_path)
    assert time.perf_counter() - start < 60
    assert results["silver.bad"] == "error"
    assert results["silver.after_bad"] == "skipped_upstream_blocked"
    for layer in range(layers):
        for i in range(width):
            assert results[f"silver.l{layer}_{i}"] == "built"


def test_blocked_walk_ignores_sources_and_tolerates_cycles():
    model_map = {
        m.full_name: m for m in (
            _model("s.x", ["landing.raw", "s.y"]), _model("s.y", ["s.x"]),
        )
    }
    is_blocked = _blocked_walker(model_map, {"landing.other"})
    assert is_blocked("s.x") is None
    assert is_blocked("s.y") is None


# ---------------------------------------------------------------------------
# Parallel report
# ---------------------------------------------------------------------------


def test_parallel_error_with_markup_does_not_raise(project, capsys):
    root, conn, db_path = project
    t = root / "transform"
    (t / "bronze" / "ok.sql").write_text(
        "@config materialized=table, schema=bronze\n\nSELECT 1 AS id\n"
    )
    (t / "bronze" / "bad.sql").write_text(
        "@config materialized=table, schema=bronze\n\nSELECT error('boom [/x] here') AS id\n"
    )
    results = run_transform(conn, t, force=True, parallel=True, db_path=db_path)
    assert results == {"bronze.ok": "built", "bronze.bad": "error"}
    assert "boom [/x] here" in capsys.readouterr().out.replace("\n", "")


def test_parallel_report_matches_sequential(project, capsys):
    root, conn, db_path = project
    t = root / "transform"
    (t / "bronze" / "checked.sql").write_text(
        "@config materialized=table, schema=bronze\n"
        "@assert row_count > 0\n"
        "@assert row_count > 5, severity=warn\n\n"
        "SELECT 1 AS id, CAST(NULL AS VARCHAR) AS empty_col\n"
    )
    (t / "bronze" / "rejected.sql").write_text(
        "@config materialized=table, schema=bronze\n"
        "@assert row_count > 10\n\n"
        "SELECT 1 AS id\n"
    )
    (t / "bronze" / "chatty.py").write_text(
        "def model(db):\n    print('hello from the worker')\n    return db.sql('SELECT 1 AS a')\n"
    )

    def run(parallel: bool) -> tuple[dict, str]:
        res = run_transform(conn, t, force=True, parallel=parallel, db_path=db_path)
        out = capsys.readouterr().out
        return res, " ".join(out.split())

    seq_results, seq_out = run(False)
    par_results, par_out = run(True)
    assert seq_results == par_results
    assert par_results["bronze.rejected"] == "assertion_failed"
    for out in (seq_out, par_out):
        assert "pass assert: row_count > 0" in out
        assert "WARN assert: row_count > 5" in out
        assert "FAIL assert: row_count > 10" in out
        assert "high nulls: empty_col(100.0%)" in out
        assert "| hello from the worker" in out
        assert "bronze.checked (table)" in out


def test_report_worker_result_escapes_everything(capsys):
    from havn.engine.transform.models import AssertionResult

    result = ModelResult(
        status="built", row_count=1, duration_ms=1,
        assertions=[AssertionResult(expression="x = '[/a]'", passed=False, detail="[/b]", severity="warn")],
        profile=ProfileResult(null_percentages={"[/c]": 90.0}),
        schema_changes=["added column [/d] VARCHAR"],
        output="[/e]\n",
    )
    _report_worker_result(None, "bronze.[/f]", result)
    out = capsys.readouterr().out
    for tag in ("[/a]", "[/b]", "[/c]", "[/d]", "[/e]", "[/f]"):
        assert tag in out
