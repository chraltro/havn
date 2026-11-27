"""The scheduler as an orchestrator: dispatch loop, worker pool, overlap,
persisted history, restart catch-up, time zones and shutdown.

Real DuckDB warehouses and real stream runs throughout. Time is injected
(``clock=``) and the loop is driven by calling ``_tick`` / ``_startup``;
nothing sleeps for schedule-sized intervals. Where a test needs a run to
stay in progress, the runner waits on an Event before doing the real run.
"""

from __future__ import annotations

import datetime as dt
import textwrap
import threading
import time
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from havn.engine.scheduler import (
    SchedulerConfigError,
    SchedulerThread,
    check_schedules,
    collect_schedules,
)
from havn.engine.schedule_time import ScheduleTimezoneError, load_zone, next_fire, to_instant

UTC = dt.timezone.utc


def utc(*args) -> dt.datetime:
    return dt.datetime(*args, tzinfo=UTC)


class Clock:
    def __init__(self, t: dt.datetime):
        self.t = t

    def __call__(self) -> dt.datetime:
        return self.t


def _script(table: str) -> str:
    return (
        "db.execute('CREATE SCHEMA IF NOT EXISTS landing')\n"
        f"db.execute('CREATE TABLE IF NOT EXISTS landing.{table} (n INTEGER)')\n"
        f"db.execute('INSERT INTO landing.{table} VALUES (1)')\n"
    )


def _project(tmp_path: Path, yml: str, scripts=("a", "b")) -> Path:
    p = tmp_path / "proj"
    for d in ("ingest", "export", "transform/bronze", "orchestration"):
        (p / d).mkdir(parents=True, exist_ok=True)
    (p / "project.yml").write_text(textwrap.dedent(yml), encoding="utf-8")
    for s in scripts:
        (p / "ingest" / f"{s}.py").write_text(_script(f"ticks_{s}"), encoding="utf-8")
    return p


def _count(project: Path, table: str) -> int:
    db = project / "warehouse.duckdb"
    if not db.exists():
        return 0
    conn = duckdb.connect(str(db))
    try:
        exists = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'landing' AND table_name = ?",
            [table],
        ).fetchone()[0]
        return conn.execute(f"SELECT count(*) FROM landing.{table}").fetchone()[0] if exists else 0
    finally:
        conn.close()


def _history(project: Path, name: str | None = None) -> list[tuple]:
    """(name, scheduled_for UTC, status, trigger, lateness_ms, error) oldest first."""
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        sql = ("SELECT schedule_name, scheduled_for, status, trigger, lateness_ms, error "
               "FROM _havn.schedule_history")
        params = []
        if name:
            sql += " WHERE schedule_name = ?"
            params.append(name)
        return conn.execute(sql + " ORDER BY scheduled_for, recorded_at, status", params).fetchall()
    finally:
        conn.close()


def _statuses(project: Path, name: str) -> list[str]:
    return [r[2] for r in _history(project, name)]


_SCHEDULERS: list[SchedulerThread] = []


@pytest.fixture(autouse=True)
def _stop_schedulers():
    yield
    while _SCHEDULERS:
        t = _SCHEDULERS.pop()
        t.shutdown_grace = 10
        t.stop()


def _scheduler(project: Path, clock: Clock, **kw) -> SchedulerThread:
    t = SchedulerThread(project, clock=clock, **kw)
    _SCHEDULERS.append(t)
    return t


class Gate:
    """A runner wrapper: named streams wait for ``release()`` before running for real."""

    def __init__(self, scheduler_ref, hold: set[str]):
        self.hold = hold
        self.open = threading.Event()
        self.started: dict[str, threading.Event] = {n: threading.Event() for n in hold}
        self.log: list[tuple[str, str, float]] = []
        self._ref = scheduler_ref
        self._lock = threading.Lock()

    def __call__(self, task):
        name = task.spec.name
        with self._lock:
            self.log.append((name, "start", time.monotonic()))
        if name in self.hold:
            self.started[name].set()
            assert self.open.wait(60), "gate never opened"
        try:
            return self._ref()._default_runner(task)
        finally:
            with self._lock:
                self.log.append((name, "end", time.monotonic()))

    def release(self):
        self.open.set()


TWO_STREAMS = """\
    name: t
    database:
      path: warehouse.duckdb
    rewind:
      enabled: false
    scheduler:
      timezone: UTC
    streams:
      slow:
        schedule: "*/5 * * * *"
        steps:
          - ingest: [a]
      fast:
        schedule: "5 6 * * *"
        steps:
          - ingest: [b]
"""


# ---------------------------------------------------------------------------
# Dispatch loop and worker pool
# ---------------------------------------------------------------------------


def test_loop_keeps_dispatching_while_a_run_is_in_progress(tmp_path):
    project = _project(tmp_path, TWO_STREAMS)
    clock = Clock(utc(2026, 4, 6, 6, 0, 0))
    holder: list[SchedulerThread] = []
    gate = Gate(lambda: holder[0], {"slow"})
    t = _scheduler(project, clock, runner=gate)
    holder.append(t)
    t._started_at = clock.t

    t._tick(now=utc(2026, 4, 6, 6, 0, 5))          # slow fires at 06:00
    assert gate.started["slow"].wait(30)
    # 06:05: fast is due, slow is due again while its 06:00 run is held.
    # The tick returns although the run is still in progress.
    t0 = time.monotonic()
    t._tick(now=utc(2026, 4, 6, 6, 5, 3))
    assert time.monotonic() - t0 < 30
    assert not gate.open.is_set()
    assert _statuses(project, "fast") == ["queued"]
    assert _statuses(project, "slow") == ["running", "skipped_overlap"]

    clock.t = utc(2026, 4, 6, 6, 7, 0)              # fast starts two minutes late
    gate.release()
    assert t.wait_idle(120)
    assert _count(project, "ticks_a") == 1
    assert _count(project, "ticks_b") == 1
    fast = _history(project, "fast")
    assert [r[2] for r in fast] == ["success"]
    assert fast[0][4] == 120_000                     # lateness_ms
    # One worker (the default): the two runs never overlapped.
    order = [(n, e) for n, e, _ in gate.log]
    assert order.index(("fast", "start")) > order.index(("slow", "end"))


def test_max_concurrent_defaults_to_one(tmp_path):
    project = _project(tmp_path, TWO_STREAMS)
    assert SchedulerThread(project).max_concurrent == 1


def test_max_concurrent_two_runs_disjoint_streams_side_by_side(tmp_path):
    project = _project(tmp_path, TWO_STREAMS.replace("timezone: UTC", "timezone: UTC\n      max_concurrent: 2"))
    # Create the metadata tables and the landing schema first: two runs
    # creating _havn or landing at the same moment fail with "Catalog
    # write-write conflict", which is why 1 is the default.
    from havn.engine.database import ensure_meta_table

    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    ensure_meta_table(conn)
    conn.execute("CREATE SCHEMA landing")
    conn.close()

    both = threading.Barrier(2, timeout=60)
    holder: list[SchedulerThread] = []

    def runner(task):
        both.wait()          # only passes when both runs are in progress at once
        return holder[0]._default_runner(task)

    clock = Clock(utc(2026, 4, 6, 6, 5, 0))
    t = _scheduler(project, clock, runner=runner)
    holder.append(t)
    assert t.max_concurrent == 2
    t._started_at = clock.t
    t._tick(now=utc(2026, 4, 6, 6, 5, 2))            # both are due at 06:05
    assert t.wait_idle(120)
    assert _statuses(project, "slow") == ["success"]
    assert _statuses(project, "fast") == ["success"]


# ---------------------------------------------------------------------------
# Overlap policy
# ---------------------------------------------------------------------------


QUEUE_PROJECT = """\
    name: t
    database:
      path: warehouse.duckdb
    rewind:
      enabled: false
    scheduler:
      timezone: UTC
    streams:
      slow:
        schedule: "*/5 * * * *"
        overlap: queue
        steps:
          - ingest: [a]
"""


def test_overlap_queue_keeps_one_pending_run(tmp_path):
    project = _project(tmp_path, QUEUE_PROJECT)
    clock = Clock(utc(2026, 4, 6, 6, 0, 0))
    holder: list[SchedulerThread] = []
    gate = Gate(lambda: holder[0], {"slow"})
    t = _scheduler(project, clock, runner=gate)
    holder.append(t)
    t._started_at = clock.t

    t._tick(now=utc(2026, 4, 6, 6, 0, 5))
    assert gate.started["slow"].wait(30)
    t._tick(now=utc(2026, 4, 6, 6, 5, 5))            # queued behind the running one
    t._tick(now=utc(2026, 4, 6, 6, 10, 5))           # one is already waiting: skipped
    assert _statuses(project, "slow") == ["running", "queued", "skipped_overlap"]

    gate.release()
    assert t.wait_idle(120)
    assert _count(project, "ticks_a") == 2
    assert _statuses(project, "slow") == ["success", "success", "skipped_overlap"]


def test_overlap_skip_is_the_default(tmp_path):
    project = _project(tmp_path, QUEUE_PROJECT.replace("        overlap: queue\n", ""))
    clock = Clock(utc(2026, 4, 6, 6, 0, 0))
    holder: list[SchedulerThread] = []
    gate = Gate(lambda: holder[0], {"slow"})
    t = _scheduler(project, clock, runner=gate)
    holder.append(t)
    t._started_at = clock.t

    t._tick(now=utc(2026, 4, 6, 6, 0, 5))
    assert gate.started["slow"].wait(30)
    t._tick(now=utc(2026, 4, 6, 6, 5, 5))
    t._tick(now=utc(2026, 4, 6, 6, 10, 5))
    gate.release()
    assert t.wait_idle(120)
    assert _count(project, "ticks_a") == 1
    rows = _history(project, "slow")
    assert [r[2] for r in rows] == ["success", "skipped_overlap", "skipped_overlap"]
    assert "still running" in rows[1][5]


def test_project_level_overlap_default_and_job_override(tmp_path):
    project = _project(tmp_path, """\
        name: t
        database:
          path: warehouse.duckdb
        scheduler:
          overlap: queue
          catchup: true
          timezone: Europe/Oslo
        streams:
          s:
            schedule: "0 6 * * *"
            steps:
              - ingest: [a]
    """)
    (project / "orchestration" / "j.yml").write_text(
        'name: j\ntargets: [ingest/a.py]\nschedules: ["0 7 * * *"]\n'
        "overlap: skip\ncatchup: false\ntimezone: America/New_York\n",
        encoding="utf-8",
    )
    specs, errors, _ = collect_schedules(project)
    assert errors == []
    by = {s.name: s for s in specs}
    assert (by["s"].overlap, by["s"].catchup, by["s"].timezone) == ("queue", True, "Europe/Oslo")
    assert (by["j"].overlap, by["j"].catchup, by["j"].timezone) == ("skip", False, "America/New_York")


# ---------------------------------------------------------------------------
# History rows: success, failure, skip, missed; retention
# ---------------------------------------------------------------------------


def test_history_records_a_failed_run_with_its_error(tmp_path):
    project = _project(tmp_path, TWO_STREAMS)
    (project / "ingest" / "a.py").write_text("raise RuntimeError('source is down')\n", encoding="utf-8")
    clock = Clock(utc(2026, 4, 6, 6, 0, 0))
    t = _scheduler(project, clock)
    t._started_at = clock.t
    t._tick(now=utc(2026, 4, 6, 6, 0, 5))
    assert t.wait_idle(120)
    rows = _history(project, "slow")
    assert [r[2] for r in rows] == ["failed"]
    assert "ingest failed" in rows[0][5]


def test_fires_passed_during_a_suspended_loop_are_recorded_missed(tmp_path):
    project = _project(tmp_path, TWO_STREAMS)
    clock = Clock(utc(2026, 4, 6, 6, 0, 0))
    t = _scheduler(project, clock)
    t._started_at = clock.t
    t._tick(now=utc(2026, 4, 6, 6, 0, 5))
    assert t.wait_idle(120)
    # The process was suspended from 06:01 to 06:16: 06:05, 06:10 and 06:15
    # passed unseen. The newest runs; the two before it are missed.
    t._tick(now=utc(2026, 4, 6, 6, 16, 0))
    assert t.wait_idle(120)
    rows = _history(project, "slow")
    assert [(r[1].strftime("%H:%M"), r[2]) for r in rows] == [
        ("06:00", "success"), ("06:05", "missed"), ("06:10", "missed"), ("06:15", "success"),
    ]


def test_retention_drops_old_rows_at_startup(tmp_path):
    from havn.engine.schedule_history import HistoryStore, WarehouseLease, ensure_schedule_tables

    project = _project(tmp_path, TWO_STREAMS.replace("timezone: UTC", "timezone: UTC\n      history_days: 7"))
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    ensure_schedule_tables(conn)
    conn.close()
    store = HistoryStore(WarehouseLease(lambda: duckdb.connect(str(project / "warehouse.duckdb"))), "old-process")
    base = {"schedule_kind": "stream", "schedule_name": "slow", "cron_expr": "*/5 * * * *",
            "status": "success", "trigger": "schedule", "recorded_at": dt.datetime(2026, 1, 1)}
    store.insert([
        {**base, "id": "old", "scheduled_for": dt.datetime(2026, 1, 1, 6, 0)},
        {**base, "id": "new", "scheduled_for": dt.datetime(2026, 4, 5, 6, 0)},
    ])
    t = _scheduler(project, Clock(utc(2026, 4, 6, 6, 0, 0)))
    t._startup(utc(2026, 4, 6, 6, 0, 0))
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM _havn.schedule_history WHERE id IN ('old', 'new')"
        ).fetchall()]
    finally:
        conn.close()
    assert ids == ["new"]


# ---------------------------------------------------------------------------
# Restart: missed fires and catch-up
# ---------------------------------------------------------------------------


RESTART_PROJECT = """\
    name: t
    database:
      path: warehouse.duckdb
    rewind:
      enabled: false
    scheduler:
      timezone: UTC
      catchup: {catchup}
    streams:
      hourly:
        schedule: "0 * * * *"
        steps:
          - ingest: [a]
"""


def _run_first_process(project: Path) -> None:
    """A scheduler that was up from 04:59 to 05:00:30 on 2026-04-06, then died."""
    clock = Clock(utc(2026, 4, 6, 4, 59, 0))
    first = _scheduler(project, clock)
    first._startup(clock.t)
    first._tick(now=utc(2026, 4, 6, 4, 59, 5))
    first._tick(now=utc(2026, 4, 6, 5, 0, 5))       # the 05:00 fire runs
    assert first.wait_idle(120)
    first._tick(now=utc(2026, 4, 6, 5, 0, 30))      # last heartbeat
    first.stop()


def test_restart_records_missed_fires_without_catchup(tmp_path):
    project = _project(tmp_path, RESTART_PROJECT.format(catchup="false"))
    _run_first_process(project)
    assert _count(project, "ticks_a") == 1

    second = _scheduler(project, Clock(utc(2026, 4, 6, 9, 0, 20)))
    second._startup(utc(2026, 4, 6, 9, 0, 20))
    assert second.wait_idle(120)
    rows = _history(project, "hourly")
    assert [(r[1].strftime("%H:%M"), r[2]) for r in rows] == [
        ("05:00", "success"), ("06:00", "missed"), ("07:00", "missed"), ("08:00", "missed"),
    ]
    assert _count(project, "ticks_a") == 1          # record-only
    # The 09:00 fire (the minute it started in) is the loop's, not missed.
    second._tick(now=utc(2026, 4, 6, 9, 0, 25))
    assert second.wait_idle(120)
    assert _statuses(project, "hourly")[-1] == "success"
    assert _count(project, "ticks_a") == 2


def test_restart_with_catchup_runs_the_newest_missed_fire_once(tmp_path):
    project = _project(tmp_path, RESTART_PROJECT.format(catchup="true"))
    _run_first_process(project)

    clock = Clock(utc(2026, 4, 6, 8, 30, 0))
    second = _scheduler(project, clock)
    second._startup(clock.t)
    assert second.wait_idle(120)
    rows = _history(project, "hourly")
    assert [(r[1].strftime("%H:%M"), r[2], r[3]) for r in rows] == [
        ("05:00", "success", "schedule"),
        ("06:00", "missed", "schedule"),
        ("07:00", "missed", "schedule"),
        ("08:00", "success", "catchup"),
    ]
    assert rows[-1][4] == 30 * 60 * 1000             # started 30 minutes late
    assert _count(project, "ticks_a") == 2


@pytest.mark.parametrize("catchup", ["false", "true"])
def test_restart_in_the_minute_of_a_fire_does_not_run_it_again(tmp_path, catchup):
    """The previous process ran the 05:00 fire; this one starts at 05:00:40."""
    project = _project(tmp_path, RESTART_PROJECT.format(catchup=catchup))
    _run_first_process(project)
    assert _count(project, "ticks_a") == 1

    clock = Clock(utc(2026, 4, 6, 5, 0, 40))
    second = _scheduler(project, clock)
    second._startup(clock.t)
    second._tick(now=utc(2026, 4, 6, 5, 0, 45))
    assert second.wait_idle(120)
    assert _count(project, "ticks_a") == 1
    assert [(r[1].strftime("%H:%M"), r[2]) for r in _history(project, "hourly")] == [
        ("05:00", "success"),
    ]
    # The next fire still runs.
    second._tick(now=utc(2026, 4, 6, 6, 0, 5))
    assert second.wait_idle(120)
    assert _count(project, "ticks_a") == 2


def test_restart_does_not_rerun_a_fire_recorded_under_another_expression(tmp_path):
    """Two expressions of one job fire at 05:00; history holds it under one of them."""
    project = _project(tmp_path, RESTART_PROJECT.format(catchup="false"))
    (project / "orchestration" / "twice.yml").write_text(textwrap.dedent("""\
        name: twice
        target: ingest/a.py
        schedules:
          - "0 * * * *"
          - "0 5 * * *"
    """), encoding="utf-8")
    clock = Clock(utc(2026, 4, 6, 4, 59, 0))
    first = _scheduler(project, clock)
    first._startup(clock.t)
    first._tick(now=utc(2026, 4, 6, 4, 59, 5))
    first._tick(now=utc(2026, 4, 6, 5, 0, 5))
    assert first.wait_idle(120)
    first.stop()
    assert len(_history(project, "twice")) == 1

    second = _scheduler(project, Clock(utc(2026, 4, 6, 5, 0, 40)))
    second._startup(utc(2026, 4, 6, 5, 0, 40))
    second._tick(now=utc(2026, 4, 6, 5, 0, 45))
    assert second.wait_idle(120)
    assert len(_history(project, "twice")) == 1


def test_first_start_has_nothing_to_catch_up(tmp_path):
    project = _project(tmp_path, RESTART_PROJECT.format(catchup="true"))
    t = _scheduler(project, Clock(utc(2026, 4, 6, 9, 30, 0)))
    t._startup(utc(2026, 4, 6, 9, 30, 0))
    assert t.wait_idle(60)
    assert _count(project, "ticks_a") == 0
    assert _history(project) == []


def test_runs_left_running_by_a_dead_process_become_interrupted(tmp_path):
    from havn.engine.schedule_history import HistoryStore, WarehouseLease

    project = _project(tmp_path, RESTART_PROJECT.format(catchup="false"))
    store = HistoryStore(WarehouseLease(lambda: duckdb.connect(str(project / "warehouse.duckdb"))), "dead-process")
    store.insert([{
        "id": "r1", "schedule_kind": "stream", "schedule_name": "hourly", "cron_expr": "0 * * * *",
        "scheduled_for": dt.datetime(2026, 4, 6, 5, 0), "recorded_at": dt.datetime(2026, 4, 6, 5, 0),
        "started_at": dt.datetime(2026, 4, 6, 5, 0), "status": "running", "trigger": "schedule",
    }])
    t = _scheduler(project, Clock(utc(2026, 4, 6, 5, 10)))
    t._startup(utc(2026, 4, 6, 5, 10))
    assert _statuses(project, "hourly") == ["interrupted"]


# ---------------------------------------------------------------------------
# Time zones and DST
# ---------------------------------------------------------------------------


OSLO = "Europe/Oslo"


def test_cron_is_evaluated_in_the_configured_zone():
    tz = load_zone(OSLO)
    # 06:00 in Oslo is 05:00 UTC in winter and 04:00 UTC in summer.
    assert next_fire("0 6 * * *", utc(2026, 1, 10, 0, 0), tz) == utc(2026, 1, 10, 5, 0)
    assert next_fire("0 6 * * *", utc(2026, 7, 10, 0, 0), tz) == utc(2026, 7, 10, 4, 0)


def test_spring_forward_gap_runs_at_the_end_of_the_gap():
    tz = load_zone(OSLO)
    # 2026-03-29: Oslo jumps from 02:00 CET to 03:00 CEST (01:00 UTC).
    assert next_fire("30 2 * * *", utc(2026, 3, 28, 23, 0), tz) == utc(2026, 3, 29, 1, 0)
    assert next_fire("30 2 * * *", utc(2026, 3, 29, 1, 0), tz) == utc(2026, 3, 30, 0, 30)
    # Every fire inside the gap collapses into the one at 03:00 CEST.
    after = utc(2026, 3, 29, 0, 50)                  # 01:50 CET
    fires = []
    for _ in range(3):
        after = next_fire("*/15 * * * *", after, tz)
        fires.append(after.astimezone(tz).strftime("%H:%M %Z"))
    assert fires == ["03:00 CEST", "03:15 CEST", "03:30 CEST"]


def test_fall_back_repeated_hour_fires_once():
    tz = load_zone(OSLO)
    # 2026-10-25: Oslo goes from 03:00 CEST back to 02:00 CET (01:00 UTC).
    first = next_fire("30 2 * * *", utc(2026, 10, 24, 23, 0), tz)
    assert first == utc(2026, 10, 25, 0, 30)          # 02:30 CEST, first occurrence
    assert next_fire("30 2 * * *", first, tz) == utc(2026, 10, 26, 1, 30)   # next day 02:30 CET
    # Every 30 minutes: the repeated 02:00-02:59 CET is not fired again.
    after = utc(2026, 10, 24, 23, 45)                 # 01:45 CEST
    seen = []
    for _ in range(4):
        after = next_fire("*/30 * * * *", after, tz)
        seen.append(after)
    assert seen == [utc(2026, 10, 25, 0, 0), utc(2026, 10, 25, 0, 30),
                    utc(2026, 10, 25, 2, 0), utc(2026, 10, 25, 2, 30)]


def test_ambiguous_and_missing_wall_times_resolve_as_documented():
    tz = load_zone(OSLO)
    assert to_instant(dt.datetime(2026, 10, 25, 2, 30), tz) == utc(2026, 10, 25, 0, 30)
    assert to_instant(dt.datetime(2026, 3, 29, 2, 30), tz) == utc(2026, 3, 29, 1, 0)


DST_PROJECT = """\
    name: t
    database:
      path: warehouse.duckdb
    rewind:
      enabled: false
    scheduler:
      timezone: Europe/Oslo
    streams:
      nightly:
        schedule: "30 2 * * *"
        steps:
          - ingest: [a]
"""


@pytest.mark.parametrize("night,start,expected", [
    # Fall back: 02:30 happens twice; one run, at the first occurrence.
    ("fall", utc(2026, 10, 24, 23, 0), [utc(2026, 10, 25, 0, 30)]),
    # Spring forward: 02:30 does not exist; one run, at 03:00 CEST.
    ("spring", utc(2026, 3, 28, 23, 0), [utc(2026, 3, 29, 1, 0)]),
])
def test_scheduler_runs_once_across_a_dst_night(tmp_path, night, start, expected):
    project = _project(tmp_path, DST_PROJECT)
    clock = Clock(start)
    t = _scheduler(project, clock)
    t._started_at = start
    now = start
    for _ in range(4 * 6):                            # every 15 minutes for 6 hours
        now += dt.timedelta(minutes=15)
        clock.t = now
        t._tick(now=now)
        assert t.wait_idle(120)
    rows = _history(project, "nightly")
    assert [(r[1].replace(tzinfo=UTC), r[2]) for r in rows] == [(e, "success") for e in expected]
    assert _count(project, "ticks_a") == 1


def test_stream_timezone_overrides_the_project_default(tmp_path):
    project = _project(tmp_path, DST_PROJECT.replace(
        '        schedule: "30 2 * * *"\n', '        schedule: "0 6 * * *"\n        timezone: UTC\n'
    ))
    specs, errors, _ = collect_schedules(project)
    assert errors == [] and specs[0].timezone == "UTC"
    clock = Clock(utc(2026, 1, 10, 5, 59, 0))
    t = _scheduler(project, clock)
    t._started_at = clock.t
    t._tick(now=utc(2026, 1, 10, 6, 0, 5))           # 06:00 UTC, 07:00 in Oslo
    assert t.wait_idle(120)
    assert _statuses(project, "nightly") == ["success"]


# ---------------------------------------------------------------------------
# Invalid settings
# ---------------------------------------------------------------------------


def test_unknown_timezone_name_is_an_error_not_a_fallback(tmp_path):
    with pytest.raises(ScheduleTimezoneError, match="Mars/Olympus"):
        load_zone("Mars/Olympus")
    project = _project(tmp_path, DST_PROJECT.replace("Europe/Oslo", "Mars/Olympus"))
    errors, _ = check_schedules(project)
    assert any("scheduler.timezone" in e and "Mars/Olympus" in e for e in errors)
    with pytest.raises(SchedulerConfigError, match="Mars/Olympus"):
        SchedulerThread(project).start()


def test_bad_values_in_stream_and_job_are_reported(tmp_path):
    project = _project(tmp_path, """\
        name: t
        database:
          path: warehouse.duckdb
        scheduler:
          max_concurrent: 0
        streams:
          s:
            schedule: "0 6 * * *"
            overlap: sometimes
            steps:
              - ingest: [a]
    """)
    (project / "orchestration" / "j.yml").write_text(
        'name: j\ntargets: [ingest/a.py]\nschedules: ["0 7 * * *"]\ntimezone: Not/AZone\n',
        encoding="utf-8",
    )
    specs, errors, _ = collect_schedules(project)
    assert specs == []
    text = "\n".join(errors)
    assert "scheduler.max_concurrent" in text
    assert "streams.s: overlap must be skip or queue" in text
    assert "orchestration/j.yml: timezone" in text and "Not/AZone" in text


def test_validate_and_schedule_commands_fail_on_an_unknown_timezone(tmp_path):
    from havn.cli import app

    project = _project(tmp_path, DST_PROJECT.replace("Europe/Oslo", "Europe/Atlantis"))
    runner = CliRunner()
    result = runner.invoke(app, ["validate", "--project", str(project), "--no-bind"])
    assert result.exit_code == 1, result.output
    assert "Europe/Atlantis" in result.output
    result = runner.invoke(app, ["schedule", "--project", str(project)])
    assert result.exit_code == 1, result.output
    assert "Europe/Atlantis" in result.output


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def test_shutdown_cancels_queued_runs_and_does_not_wait_forever(tmp_path):
    project = _project(tmp_path, TWO_STREAMS.replace('schedule: "5 6 * * *"', 'schedule: "0 6 * * *"'))
    clock = Clock(utc(2026, 4, 6, 6, 0, 10))
    holder: list[SchedulerThread] = []
    gate = Gate(lambda: holder[0], {"slow"})
    t = _scheduler(project, clock, runner=gate, shutdown_grace=0.5, poll_seconds=3600)
    holder.append(t)
    t.start()                                        # the real loop thread
    assert gate.started["slow"].wait(60)             # slow runs, fast waits behind it

    t.stop()
    t.join(timeout=30)
    assert not t.is_alive()                          # returned after the grace period
    spec = collect_schedules(project)[0][0]
    assert t._dispatch(spec, spec.expressions[0], clock.t, clock.t) == "stopped"
    assert _statuses(project, "fast") == ["cancelled"]
    assert _statuses(project, "slow") == ["running"]
    assert all(w.daemon for w in t.worker_threads)   # never keeps the process alive

    # The run that was in progress finishes and is recorded; the workers exit.
    gate.release()
    for w in t.worker_threads:
        w.join(60)
        assert not w.is_alive()
    assert _statuses(project, "slow") == ["success"]
    assert _count(project, "ticks_b") == 0


# ---------------------------------------------------------------------------
# CLI and API
# ---------------------------------------------------------------------------


def test_schedule_listing_shows_next_run_with_timezone(tmp_path, monkeypatch):
    import havn.engine.scheduler as scheduler_mod
    from havn.cli import app

    class _Fake:
        shutdown_grace = 0

        def __init__(self, project_dir, **kwargs):
            pass

        def start(self):
            pass

        def join(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(scheduler_mod, "SchedulerThread", _Fake)
    project = _project(tmp_path, DST_PROJECT)
    result = CliRunner().invoke(app, ["schedule", "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert "Europe/Oslo" in result.output
    assert "02:30" in result.output
    assert "skip" in result.output


def test_schedule_history_cli_and_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from havn.cli import app

    project = _project(tmp_path, TWO_STREAMS)
    clock = Clock(utc(2026, 4, 6, 6, 0, 0))
    t = _scheduler(project, clock)
    t._started_at = clock.t
    t._tick(now=utc(2026, 4, 6, 6, 0, 5))
    assert t.wait_idle(120)
    t.stop()

    result = CliRunner().invoke(app, ["schedule", "--history", "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert "slow" in result.output and "success" in result.output

    import havn.server.app as server_app

    from havn.server import deps

    monkeypatch.setattr(server_app, "PROJECT_DIR", project)
    monkeypatch.setattr(server_app, "AUTH_ENABLED", False)
    deps.reset_shared_conn()
    client = TestClient(server_app.app)
    resp = client.get("/api/scheduler/history?name=slow")
    assert resp.status_code == 200, resp.text
    hist = resp.json()["history"]
    assert [h["status"] for h in hist] == ["success"]
    assert hist[0]["scheduled_for"] == "2026-04-06T06:00:00Z"
    assert hist[0]["cron"] == "*/5 * * * *"


# ---------------------------------------------------------------------------
# The scheduler builds the warehouse its host builds
# ---------------------------------------------------------------------------


ENV_PROJECT = """\
    name: t
    database:
      path: warehouse.duckdb
    environments:
      dev:
        database:
          path: dev.duckdb
      prod:
        database:
          path: prod.duckdb
    rewind:
      enabled: false
    scheduler:
      timezone: UTC
    streams:
      hourly:
        schedule: "0 * * * *"
        steps:
          - ingest: [a]
"""


def _ticks_in(db: Path) -> int:
    if not db.exists():
        return 0
    conn = duckdb.connect(str(db))
    try:
        exists = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'ticks_a'"
        ).fetchone()[0]
        return conn.execute("SELECT count(*) FROM landing.ticks_a").fetchone()[0] if exists else 0
    finally:
        conn.close()


class _Captured:
    """Stands in for SchedulerThread in the CLI; keeps its config loader."""

    instances: list = []
    shutdown_grace = 0

    def __init__(self, project_dir, **kwargs):
        self.project_dir = project_dir
        self.kwargs = kwargs
        _Captured.instances.append(self)

    def start(self):
        pass

    def join(self, timeout=None):
        pass

    def stop(self):
        pass


def test_scheduler_under_serve_builds_the_servers_environment(tmp_path, monkeypatch):
    """`havn serve --env prod --schedule`: scheduled runs build what the UI builds."""
    import uvicorn

    import havn.engine.scheduler as scheduler_mod
    import havn.server.app as server_app
    from havn.cli import app
    from havn.server import deps

    project = _project(tmp_path, ENV_PROJECT)
    _Captured.instances = []
    monkeypatch.setattr(scheduler_mod, "SchedulerThread", _Captured)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(server_app, "PROJECT_DIR", server_app.PROJECT_DIR)
    monkeypatch.setattr(server_app, "ACTIVE_ENV", server_app.ACTIVE_ENV)
    monkeypatch.setattr(server_app, "AUTH_ENABLED", server_app.AUTH_ENABLED)
    deps.invalidate_config_cache()
    try:
        result = CliRunner().invoke(app, ["serve", "--schedule", "--env", "prod", "--port", "39871",
                                          "--project", str(project)])
        assert result.exit_code == 0, result.output
        (captured,) = _Captured.instances
        loader = captured.kwargs["config_loader"]
        assert loader().database.path == "prod.duckdb"
        assert project / loader().database.path == deps._get_db_path()

        # And a real scheduler with that loader runs into prod, not dev.
        clock = Clock(utc(2026, 4, 6, 5, 0, 30))
        t = _scheduler(project, clock, config_loader=loader)
        t._started_at = clock.t
        t._tick(now=utc(2026, 4, 6, 6, 0, 5))
        assert t.wait_idle(120)
        t.stop()
        assert _ticks_in(project / "prod.duckdb") == 1
        assert _ticks_in(project / "dev.duckdb") == 0
    finally:
        deps.invalidate_config_cache()


def test_schedule_env_option_resolves_like_havn_stream(tmp_path, monkeypatch):
    import havn.engine.scheduler as scheduler_mod
    from havn.cli import app

    project = _project(tmp_path, ENV_PROJECT)
    _Captured.instances = []
    monkeypatch.setattr(scheduler_mod, "SchedulerThread", _Captured)
    result = CliRunner().invoke(app, ["schedule", "--env", "prod", "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert "env=prod" in result.output
    (captured,) = _Captured.instances
    assert captured.kwargs["config_loader"]().database.path == "prod.duckdb"

    # Without --env: the active environment, as `havn stream` picks it.
    _Captured.instances = []
    result = CliRunner().invoke(app, ["schedule", "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert _Captured.instances[0].kwargs["config_loader"]().database.path == "dev.duckdb"

    result = CliRunner().invoke(app, ["schedule", "--env", "nope", "--project", str(project)])
    assert result.exit_code == 1
