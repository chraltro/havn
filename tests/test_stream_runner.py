"""Shared stream runner, scheduler catch-up and pipeline alerts.

Covers engine/stream_runner.py (used by `havn stream` and scheduled
streams), the SchedulerThread due-time logic, and the pipeline alerts sent
by streams and jobs. Real DuckDB files throughout; time is injected.
"""

from __future__ import annotations

import datetime as dt
import textwrap
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from havn.engine.scheduler import SchedulerThread, _run_stream_task

TICK_SCRIPT = (
    "db.execute('CREATE SCHEMA IF NOT EXISTS landing')\n"
    "db.execute('CREATE TABLE IF NOT EXISTS landing.ticks (n INTEGER)')\n"
    "db.execute('INSERT INTO landing.ticks VALUES (1)')\n"
)


def _project(tmp_path: Path, project_yml: str) -> Path:
    p = tmp_path / "proj"
    for d in ("ingest", "export", "seeds", "transform/bronze", "orchestration"):
        (p / d).mkdir(parents=True, exist_ok=True)
    (p / "project.yml").write_text(textwrap.dedent(project_yml), encoding="utf-8")
    (p / "ingest" / "tick.py").write_text(TICK_SCRIPT, encoding="utf-8")
    return p


def _query(project: Path, sql: str):
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _ticks(project: Path) -> int:
    if not (project / "warehouse.duckdb").exists():
        return 0
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        exists = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'landing' AND table_name = 'ticks'"
        ).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM landing.ticks").fetchone()[0] if exists else 0
    finally:
        conn.close()


_SCHEDULERS: list[SchedulerThread] = []


@pytest.fixture(autouse=True)
def _stop_schedulers():
    yield
    while _SCHEDULERS:
        _SCHEDULERS.pop().stop()


def _scheduler(project: Path, started_at: dt.datetime) -> SchedulerThread:
    t = SchedulerThread(project)
    t._started_at = started_at
    _SCHEDULERS.append(t)
    return t


def _tick(t: SchedulerThread, now: dt.datetime) -> None:
    """One poll, then wait for the runs it handed to the worker pool."""
    t._tick(now=now)
    assert t.wait_idle(timeout=120)


# ---------------------------------------------------------------------------
# Scheduler: due-time logic
# ---------------------------------------------------------------------------


class _Due:
    """Drives ``SchedulerThread._evaluate`` (the loop's due-time logic) for one
    cron schedule and records what it dispatches and what it records missed.

    The dispatch and history writes are replaced by recorders, so only the
    due-time decision is under test; the tick tests below run real streams.
    """

    def __init__(self, t: SchedulerThread, expr: str, name: str = "s"):
        from havn.engine.scheduler import ScheduleSpec

        self.t = t
        self.spec = ScheduleSpec("stream", name, (expr,), "skip", False, None)
        self.dispatched: list[dt.datetime] = []
        self.missed: list[dt.datetime] = []
        t._dispatch = lambda spec, expr, inst, now, trigger="schedule": self.dispatched.append(inst)
        t._history.insert = lambda rows: self.missed.extend(r["scheduled_for"] for r in rows)

    def __call__(self, now: dt.datetime) -> bool:
        """One evaluation at ``now`` (naive = local); True when it dispatched."""
        from havn.engine.schedule_time import as_instant

        before = len(self.dispatched)
        self.t._evaluate(self.spec, as_instant(now))
        return len(self.dispatched) > before


def _local(*args) -> dt.datetime:
    from havn.engine.schedule_time import as_instant

    return as_instant(dt.datetime(*args))


def test_evaluate_runs_a_fire_missed_during_a_long_run_once(tmp_path):
    t = _scheduler(tmp_path, dt.datetime(2026, 4, 6, 6, 0, 0))
    due = _Due(t, "5 6 * * *")
    # 06:00:10 poll: the 06:05 schedule is not due yet.
    assert not due(dt.datetime(2026, 4, 6, 6, 0, 10))
    # The loop was busy until 06:21 (a long job): 06:05 is caught up, once.
    assert due(dt.datetime(2026, 4, 6, 6, 21, 0))
    assert not due(dt.datetime(2026, 4, 6, 6, 21, 30))
    assert due.dispatched == [_local(2026, 4, 6, 6, 5)]
    assert due.missed == []


def test_evaluate_runs_the_newest_of_several_missed_fires_and_records_the_rest(tmp_path):
    from havn.engine.schedule_history import utc_naive

    t = _scheduler(tmp_path, dt.datetime(2026, 4, 6, 6, 0, 30))
    due = _Due(t, "*/5 * * * *")
    assert due(dt.datetime(2026, 4, 6, 6, 0, 30))
    # 06:05, 06:10, 06:15 and 06:20 all passed while busy: one run (06:20),
    # the older three recorded as missed.
    assert due(dt.datetime(2026, 4, 6, 6, 22, 0))
    assert not due(dt.datetime(2026, 4, 6, 6, 22, 30))
    assert due(dt.datetime(2026, 4, 6, 6, 25, 1))
    assert due.dispatched == [_local(2026, 4, 6, 6, m) for m in (0, 20, 25)]
    assert due.missed == [utc_naive(_local(2026, 4, 6, 6, m)) for m in (5, 10, 15)]


def test_evaluate_never_fires_for_time_before_start(tmp_path):
    t = _scheduler(tmp_path, dt.datetime(2026, 4, 6, 7, 0, 10))
    due = _Due(t, "0 6 * * *", "daily")
    for now in (dt.datetime(2026, 4, 6, 7, 0, 10), dt.datetime(2026, 4, 6, 7, 30)):
        assert not due(now)
    # Until the next day's 06:00.
    assert due(dt.datetime(2026, 4, 7, 6, 0, 20))
    assert due.missed == []


def test_evaluate_counts_the_start_minute(tmp_path):
    # Started a few seconds into 06:00: the 06:00 schedule still runs, as it
    # did when the scheduler matched the current minute.
    t = _scheduler(tmp_path, dt.datetime(2026, 4, 6, 6, 0, 10))
    due = _Due(t, "0 6 * * *", "daily")
    assert due(dt.datetime(2026, 4, 6, 6, 0, 10))
    assert not due(dt.datetime(2026, 4, 6, 6, 0, 40))


def test_schedule_added_later_does_not_replay_the_past(tmp_path):
    t = _scheduler(tmp_path, dt.datetime(2026, 4, 6, 5, 0))
    t._cron_clock_settled = True  # the first poll has happened
    due = _Due(t, "0 6 * * *", "new")
    # First seen at 10:00; its 06:00 fire is before it existed.
    assert not due(dt.datetime(2026, 4, 6, 10, 0, 15))
    assert due.missed == []


def test_evaluate_does_not_refire_when_clock_steps_back(tmp_path):
    t = _scheduler(tmp_path, dt.datetime(2026, 10, 25, 2, 0))
    due = _Due(t, "30 2 * * *")
    assert due(dt.datetime(2026, 10, 25, 2, 30, 5))
    # The clock goes back to 02:00 and passes 02:30 again.
    assert not due(dt.datetime(2026, 10, 25, 2, 0, 5))
    assert not due(dt.datetime(2026, 10, 25, 2, 31))


def test_tick_runs_a_stream_missed_during_a_long_run_exactly_once(tmp_path):
    project = _project(tmp_path, """\
        name: t
        database:
          path: warehouse.duckdb
        rewind:
          enabled: false
        streams:
          five-past:
            schedule: "5 6 * * *"
            steps:
              - ingest: [tick]
    """)
    t = _scheduler(project, dt.datetime(2026, 4, 6, 6, 0, 0))
    _tick(t, dt.datetime(2026, 4, 6, 6, 0, 1))
    assert _ticks(project) == 0
    # The next poll comes 21 minutes later, after a long 06:00 run.
    _tick(t, dt.datetime(2026, 4, 6, 6, 21, 0))
    assert _ticks(project) == 1
    _tick(t, dt.datetime(2026, 4, 6, 6, 21, 30))
    _tick(t, dt.datetime(2026, 4, 6, 6, 22, 0))
    assert _ticks(project) == 1


def test_tick_does_not_catch_up_before_thread_start(tmp_path):
    project = _project(tmp_path, """\
        name: t
        database:
          path: warehouse.duckdb
        rewind:
          enabled: false
        streams:
          early:
            schedule: "0 6 * * *"
            steps:
              - ingest: [tick]
    """)
    t = _scheduler(project, dt.datetime(2026, 4, 6, 9, 0, 0))
    _tick(t, dt.datetime(2026, 4, 6, 9, 0, 1))
    _tick(t, dt.datetime(2026, 4, 6, 9, 0, 31))
    assert _ticks(project) == 0


def test_job_with_two_schedules_due_in_one_window_fires_once(tmp_path):
    project = _project(tmp_path, """\
        name: t
        database:
          path: warehouse.duckdb
    """)
    (project / "orchestration" / "j.yml").write_text(textwrap.dedent("""\
        name: j
        targets: [ingest/tick.py]
        resolve: none
        schedules: ["5 6 * * *", "10 6 * * *"]
        retry: 0
    """), encoding="utf-8")
    t = _scheduler(project, dt.datetime(2026, 4, 6, 6, 0, 0))
    _tick(t, dt.datetime(2026, 4, 6, 6, 0, 1))
    _tick(t, dt.datetime(2026, 4, 6, 6, 30, 0))
    assert _ticks(project) == 1
    _tick(t, dt.datetime(2026, 4, 6, 6, 30, 30))
    assert _ticks(project) == 1


# ---------------------------------------------------------------------------
# Stream runner: failure semantics, seeds
# ---------------------------------------------------------------------------


ASSERT_PROJECT = """\
    name: t
    database:
      path: warehouse.duckdb
    rewind:
      enabled: false
    streams:
      nightly:
        schedule: "0 6 * * *"
        steps:
          - transform: [all]
          - export: [all]
"""


def _with_failing_assert(project: Path) -> Path:
    (project / "transform" / "bronze" / "bad.sql").write_text(
        "@config materialized=table, schema=bronze\n"
        "@assert row_count > 5\n\n"
        "SELECT 1 AS id\n",
        encoding="utf-8",
    )
    (project / "export" / "marker.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(project / 'exported.txt')!r}).write_text('x')\n",
        encoding="utf-8",
    )
    return project


def test_cli_stream_stops_before_export_when_an_assert_fails(tmp_path):
    from havn.cli import app

    project = _with_failing_assert(_project(tmp_path, ASSERT_PROJECT))
    result = CliRunner().invoke(app, ["stream", "nightly", "--project", str(project)])
    assert result.exit_code == 1, result.output
    assert "Stream failed" in result.output
    assert "assertion failures" in result.output
    assert not (project / "exported.txt").exists()


def test_scheduled_stream_reports_assert_failure_and_skips_export(tmp_path):
    project = _with_failing_assert(_project(tmp_path, ASSERT_PROJECT))
    out = _run_stream_task(str(project), "nightly")
    assert out["status"] == "error"
    assert [s["action"] for s in out["steps"]] == ["transform"]
    assert not (project / "exported.txt").exists()


def test_scheduler_loop_prints_failed_not_completed(tmp_path, capsys):
    import havn.engine.scheduler as sched

    project = _with_failing_assert(_project(tmp_path, ASSERT_PROJECT))
    t = _scheduler(project, dt.datetime(2026, 4, 6, 6, 0, 0))
    printed: list[str] = []
    original = sched.console.print
    sched.console.print = lambda *a, **k: printed.append(" ".join(str(x) for x in a))
    try:
        _tick(t, dt.datetime(2026, 4, 6, 6, 0, 5))
    finally:
        sched.console.print = original
    assert any("Stream 'nightly' failed" in line for line in printed), printed
    assert not any("Stream 'nightly' completed" in line for line in printed)


def test_assert_failure_is_not_retried(tmp_path):
    from havn.config import load_project
    from havn.engine.stream_runner import run_stream

    project = _with_failing_assert(_project(tmp_path, ASSERT_PROJECT.replace(
        'schedule: "0 6 * * *"', 'schedule: "0 6 * * *"\n        retries: 3\n        retry_delay: 0'
    )))
    sleeps: list[float] = []
    result = run_stream(load_project(project), project, "nightly", sleep=sleeps.append)
    assert not result.ok
    assert sleeps == []
    assert result.steps[0].attempts == 1


def test_failed_ingest_is_retried(tmp_path):
    from havn.config import load_project
    from havn.engine.stream_runner import run_stream

    project = _project(tmp_path, """\
        name: t
        database:
          path: warehouse.duckdb
        rewind:
          enabled: false
        streams:
          s:
            retries: 2
            retry_delay: 0
            steps:
              - ingest: [flaky]
    """)
    # Fails on the first run, succeeds on the second.
    (project / "ingest" / "flaky.py").write_text(
        "from pathlib import Path\n"
        f"flag = Path({str(project / 'flag')!r})\n"
        "if not flag.exists():\n"
        "    flag.write_text('1')\n"
        "    raise RuntimeError('first attempt fails')\n",
        encoding="utf-8",
    )
    events: list[str] = []
    result = run_stream(
        load_project(project), project, "s",
        sleep=lambda s: None, on_event=lambda e, d: events.append(e),
    )
    assert result.ok, result.error
    assert result.steps[0].attempts == 2
    assert events.count("retry") == 1


def test_scheduled_stream_runs_seed_step(tmp_path):
    project = _project(tmp_path, """\
        name: t
        database:
          path: warehouse.duckdb
        streams:
          seeds-only:
            schedule: "0 6 * * *"
            steps:
              - seed: [all]
    """)
    (project / "seeds" / "colours.csv").write_text("id,name\n1,red\n2,blue\n", encoding="utf-8")
    t = _scheduler(project, dt.datetime(2026, 4, 6, 6, 0, 0))
    _tick(t, dt.datetime(2026, 4, 6, 6, 0, 5))
    assert _query(project, "SELECT COUNT(*) FROM seeds.colours") == [(2,)]


def test_stream_rewind_run_records_models(tmp_path):
    from havn.config import load_project
    from havn.engine.snapshots import get_runs
    from havn.engine.stream_runner import run_stream

    project = _project(tmp_path, """\
        name: t
        database:
          path: warehouse.duckdb
        streams:
          s:
            steps:
              - transform: [all]
    """)
    (project / "transform" / "bronze" / "ok.sql").write_text(
        "@config materialized=table, schema=bronze\n\nSELECT 1 AS id\n", encoding="utf-8",
    )
    result = run_stream(load_project(project), project, "s", trigger="scheduled")
    assert result.ok
    runs = get_runs(project, limit=5)
    assert runs and runs[0].status == "success"
    assert runs[0].trigger == "scheduled:s"
    assert "bronze.ok" in runs[0].models_run


def test_unknown_stream_raises(tmp_path):
    from havn.config import load_project
    from havn.engine.stream_runner import StreamNotFound, run_stream

    project = _project(tmp_path, "name: t\n")
    with pytest.raises(StreamNotFound):
        run_stream(load_project(project), project, "nope")
    assert _run_stream_task(str(project), "nope")["status"] == "error"


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def _alert_rows(project: Path) -> list[tuple]:
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        exists = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = '_havn' AND table_name = 'alert_log'"
        ).fetchone()[0]
        if not exists:
            return []
        return conn.execute(
            "SELECT alert_type, channel, target, status FROM _havn.alert_log ORDER BY 1"
        ).fetchall()
    finally:
        conn.close()


def test_failing_stream_logs_a_failure_alert(tmp_path):
    project = _with_failing_assert(_project(tmp_path, ASSERT_PROJECT + """\
    alerts:
      channels: [log]
"""))
    assert _run_stream_task(str(project), "nightly")["status"] == "error"
    assert _alert_rows(project) == [("pipeline_failure", "log", "nightly", "sent")]


def test_failing_stream_sends_nothing_when_on_failure_is_false(tmp_path):
    project = _with_failing_assert(_project(tmp_path, ASSERT_PROJECT + """\
    alerts:
      channels: [log]
      on_failure: false
"""))
    assert _run_stream_task(str(project), "nightly")["status"] == "error"
    assert _alert_rows(project) == []


def test_cli_stream_success_alert_honours_on_success(tmp_path):
    from havn.cli import app

    yml = """\
        name: t
        database:
          path: warehouse.duckdb
        rewind:
          enabled: false
        alerts:
          channels: [log]
          on_success: {flag}
        streams:
          s:
            steps:
              - ingest: [tick]
    """
    on = _project(tmp_path / "on", yml.format(flag="true"))
    off = _project(tmp_path / "off", yml.format(flag="false"))
    for project in (on, off):
        r = CliRunner().invoke(app, ["stream", "s", "--project", str(project)])
        assert r.exit_code == 0, r.output
    assert _alert_rows(on) == [("pipeline_success", "log", "s", "sent")]
    assert _alert_rows(off) == []


def test_alert_delivery_failure_does_not_fail_the_stream(tmp_path):
    project = _project(tmp_path, """\
        name: t
        database:
          path: warehouse.duckdb
        rewind:
          enabled: false
        alerts:
          webhook_url: "http://127.0.0.1:9/unreachable"
        streams:
          s:
            steps:
              - ingest: [tick]
    """)
    out = _run_stream_task(str(project), "s")
    assert out["status"] == "success"
    assert _alert_rows(project) == [("pipeline_success", "webhook", "s", "error")]


def test_failing_job_logs_a_failure_alert(tmp_path):
    from havn.engine.database import ensure_meta_table, open_warehouse
    from havn.config import load_project
    from havn.engine.orchestration import discover_jobs, execute_job, resolve_execution_plan
    from havn.engine.transform.discovery import build_dag, discover_all_models

    project = _with_failing_assert(_project(tmp_path, """\
        name: t
        database:
          path: warehouse.duckdb
        alerts:
          channels: [log]
    """))
    (project / "orchestration" / "j.yml").write_text(
        "name: j\ntargets: [bronze.bad]\nresolve: none\nretry: 0\n", encoding="utf-8",
    )
    job = discover_jobs(project)[0]
    conn = open_warehouse(load_project(project), project)
    try:
        ensure_meta_table(conn)
        plan = resolve_execution_plan(
            job.targets, build_dag(discover_all_models(project)), project, conn=conn, resolve="none",
        )
        result = execute_job(job, plan, conn, project, trigger="scheduled")
    finally:
        conn.close()
    assert result.status == "failure"
    assert _alert_rows(project) == [("pipeline_failure", "log", "job:j", "sent")]
