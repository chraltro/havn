"""One scheduler per warehouse (``engine/scheduler_lock.py``).

A second scheduler on the same warehouse, in this process or another, is
refused with an error naming the process that holds the lock; the lock goes
away with its process, also when that process is killed.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from havn.engine.scheduler import SchedulerConfigError, SchedulerThread
from havn.engine.scheduler_lock import SchedulerLock, SchedulerLockedError, warehouse_key

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 4, 6, 5, 0, 30, tzinfo=UTC)

PROJECT = """\
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
streams:
  hourly:
    schedule: "0 * * * *"
    steps:
      - ingest: [all]
"""


def _project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    (p / "ingest").mkdir(parents=True)
    (p / "project.yml").write_text(PROJECT, encoding="utf-8")
    return p


def _scheduler(project: Path, env: str | None = None) -> SchedulerThread:
    from havn.config import load_project

    return SchedulerThread(project, clock=lambda: NOW,
                           config_loader=lambda: load_project(project, env=env))


def test_second_scheduler_in_the_same_process_is_refused(tmp_path):
    project = _project(tmp_path)
    first = _scheduler(project)
    first._startup(NOW)
    second = _scheduler(project)
    try:
        with pytest.raises(SchedulerLockedError) as err:
            second._startup(NOW)
        assert f"process {os.getpid()}" in str(err.value)
        assert "dev.duckdb" in str(err.value)
        # start() reports it as a scheduler error, before any thread starts.
        with pytest.raises(SchedulerConfigError, match="Another scheduler is already running"):
            second.start()
        assert not second.is_alive()
    finally:
        first.stop()
    # Released on stop: now the second one may start.
    second._startup(NOW)
    second.stop()


def test_another_environment_has_its_own_lock(tmp_path):
    project = _project(tmp_path)
    dev = _scheduler(project, "dev")
    prod = _scheduler(project, "prod")
    dev._startup(NOW)
    try:
        prod._startup(NOW)
        prod.stop()
    finally:
        dev.stop()


def test_refused_scheduler_does_not_touch_the_others_history(tmp_path):
    """The refusal comes before startup marks live rows ``interrupted``."""
    import duckdb

    from havn.engine.schedule_history import HistoryStore, WarehouseLease

    project = _project(tmp_path)
    first = _scheduler(project)
    first._startup(NOW)
    try:
        store = HistoryStore(WarehouseLease(lambda: duckdb.connect(str(project / "dev.duckdb"))),
                             first.scheduler_id)
        store.insert([{
            "id": "r1", "schedule_kind": "stream", "schedule_name": "hourly", "cron_expr": "0 * * * *",
            "scheduled_for": dt.datetime(2026, 4, 6, 5, 0), "recorded_at": dt.datetime(2026, 4, 6, 5, 0),
            "status": "running", "trigger": "schedule",
        }])
        with pytest.raises(SchedulerLockedError):
            _scheduler(project)._startup(NOW)
        conn = duckdb.connect(str(project / "dev.duckdb"))
        try:
            assert conn.execute("SELECT status FROM _havn.schedule_history").fetchall() == [("running",)]
        finally:
            conn.close()
    finally:
        first.stop()


def test_lock_held_by_another_process_names_it_and_dies_with_it(tmp_path):
    project = _project(tmp_path)
    key = warehouse_key(type("C", (), {"database": type("D", (), {"path": "dev.duckdb"})()})(), project)
    holder = textwrap.dedent(f"""
        import os, sys, time
        from pathlib import Path
        from havn.engine.scheduler_lock import SchedulerLock
        lock = SchedulerLock(Path(r"{project}"), "{key}", warehouse="dev.duckdb")
        lock.acquire()
        print("locked", os.getpid(), flush=True)
        sys.stdin.readline()
        os._exit(1)  # a crash: no release, no cleanup
    """)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(Path(__file__).resolve().parents[1] / "src"),
                                         env.get("PYTHONPATH", "")])
    proc = subprocess.Popen([sys.executable, "-c", holder], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, env=env)
    try:
        word, holder_pid = proc.stdout.readline().split()
        assert word == "locked"
        with pytest.raises(SchedulerLockedError) as err:
            _scheduler(project)._startup(NOW)
        # The holder's own pid (a venv's python.exe on Windows is a launcher
        # whose child is the real interpreter).
        assert f"process {holder_pid}" in str(err.value)
    finally:
        proc.stdin.write("die\n")
        proc.stdin.close()
        proc.wait(30)
        proc.stdout.close()
    # The OS dropped the lock with the process; the stale files do not matter.
    lock = SchedulerLock(project, key)
    lock.acquire()
    lock.release()
    t = _scheduler(project)
    t._startup(NOW)
    t.stop()
