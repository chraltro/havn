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


def _scheduler(project: Path, started_at: dt.datetime) -> SchedulerThread:
    t = SchedulerThread(project)
    t._started_at = started_at
    return t


# ---------------------------------------------------------------------------
# Scheduler: due-time logic
# ---------------------------------------------------------------------------


def test_should_run_reports_a_fire_missed_during_a_long_run(tmp_path):
    t = _scheduler(tmp_path, dt.datetime(2026, 4, 6, 6, 0, 0))
    # 06:00:10 poll: the 06:05 schedule is not due yet.
    assert not t._should_run("s", "5 6 * * *", now=dt.datetime(2026, 4, 6, 6, 0, 10))
    # The loop was busy until 06:21 (a long job): 06:05 is caught up, once.
    assert t._should_run("s", "5 6 * * *", now=dt.datetime(2026, 4, 6, 6, 21, 0))
    assert not t._should_run("s", "5 6 * * *", now=dt.datetime(2026, 4, 6, 6, 21, 30))


def test_should_run_coalesces_several_missed_fires(tmp_path):
    t = _scheduler(tmp_path, dt.datetime(2026, 4, 6, 6, 0, 30))
    assert t._should_run("s", "*/5 * * * *", now=dt.datetime(2026, 4, 6, 6, 0, 30))
    # 06:05, 06:10, 06:15 and 06:20 all passed while busy: one fire.
    assert t._should_run("s", "*/5 * * * *", now=dt.datetime(2026, 4, 6, 6, 22, 0))
    assert not t._should_run("s", "*/5 * * * *", now=dt.datetime(2026, 4, 6, 6, 22, 30))
    assert t._should_run("s", "*/5 * * * *", now=dt.datetime(2026, 4, 6, 6, 25, 1))


def test_should_run_never_fires_for_time_before_start(tmp_path):
    t = _scheduler(tmp_path, dt.datetime(2026, 4, 6, 7, 0, 10))
    for now in (dt.datetime(2026, 4, 6, 7, 0, 10), dt.datetime(2026, 4, 6, 7, 30)):
        assert not t._should_run("daily", "0 6 * * *", now=now)
    # Until the next day's 06:00.
    assert t._should_run("daily", "0 6 * * *", now=dt.datetime(2026, 4, 7, 6, 0, 20))


def test_should_run_counts_the_start_minute(tmp_path):
    # Started a few seconds into 06:00: the 06:00 schedule still runs, as it
    # did when the scheduler matched the current minute.
    t = _scheduler(tmp_path, dt.datetime(2026, 4, 6, 6, 0, 10))
    assert t._should_run("daily", "0 6 * * *", now=dt.datetime(2026, 4, 6, 6, 0, 10))
    assert not t._should_run("daily", "0 6 * * *", now=dt.datetime(2026, 4, 6, 6, 0, 40))


def test_schedule_added_later_does_not_replay_the_past(tmp_path):
    t = _scheduler(tmp_path, dt.datetime(2026, 4, 6, 5, 0))
    t._cron_clock_settled = True  # the first poll has happened
    # First seen at 10:00; its 06:00 fire is before it existed.
    assert not t._should_run("new", "0 6 * * *", now=dt.datetime(2026, 4, 6, 10, 0, 15))


def test_should_run_does_not_refire_when_clock_steps_back(tmp_path):
    t = _scheduler(tmp_path, dt.datetime(2026, 10, 25, 2, 0))
    assert t._should_run("s", "30 2 * * *", now=dt.datetime(2026, 10, 25, 2, 30, 5))
    # DST ends: the wall clock goes back to 02:00 and passes 02:30 again.
    assert not t._should_run("s", "30 2 * * *", now=dt.datetime(2026, 10, 25, 2, 0, 5))
    assert not t._should_run("s", "30 2 * * *", now=dt.datetime(2026, 10, 25, 2, 31))


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
    t._tick(now=dt.datetime(2026, 4, 6, 6, 0, 1))
    assert _ticks(project) == 0
    # The next poll comes 21 minutes later, after a long 06:00 run.
    t._tick(now=dt.datetime(2026, 4, 6, 6, 21, 0))
    assert _ticks(project) == 1
    t._tick(now=dt.datetime(2026, 4, 6, 6, 21, 30))
    t._tick(now=dt.datetime(2026, 4, 6, 6, 22, 0))
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
    t._tick(now=dt.datetime(2026, 4, 6, 9, 0, 1))
    t._tick(now=dt.datetime(2026, 4, 6, 9, 0, 31))
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
    t._tick(now=dt.datetime(2026, 4, 6, 6, 0, 1))
    t._tick(now=dt.datetime(2026, 4, 6, 6, 30, 0))
    assert _ticks(project) == 1
    t._tick(now=dt.datetime(2026, 4, 6, 6, 30, 30))
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
        t._tick(now=dt.datetime(2026, 4, 6, 6, 0, 5))
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
    t._tick(now=dt.datetime(2026, 4, 6, 6, 0, 5))
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
