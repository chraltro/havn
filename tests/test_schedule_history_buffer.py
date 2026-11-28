"""History writes that fail because the warehouse cannot be opened are kept
and retried in order, so a fire and its outcome still end up recorded."""

from __future__ import annotations

import datetime as dt
import textwrap
from pathlib import Path

import duckdb

from havn.engine.schedule_history import HistoryStore, WarehouseLease

UTC = dt.timezone.utc


class Flaky:
    """Opens the real warehouse, unless ``down`` (another process holds it)."""

    def __init__(self, path: Path):
        self.path = path
        self.down = False

    def __call__(self):
        if self.down:
            raise duckdb.IOException("Cannot open file: being used by another process")
        return duckdb.connect(str(self.path))


def _rows(path: Path) -> list[tuple]:
    conn = duckdb.connect(str(path))
    try:
        return conn.execute(
            "SELECT id, status, started_at IS NOT NULL, finished_at IS NOT NULL, error "
            "FROM _havn.schedule_history ORDER BY recorded_at"
        ).fetchall()
    finally:
        conn.close()


def _row(rid: str) -> dict:
    t = dt.datetime(2026, 4, 6, 5, 0)
    return {"id": rid, "schedule_kind": "stream", "schedule_name": "s", "cron_expr": "0 * * * *",
            "scheduled_for": t, "recorded_at": t, "status": "queued", "trigger": "schedule"}


def test_writes_while_the_warehouse_is_unavailable_are_replayed_in_order(tmp_path):
    db = tmp_path / "w.duckdb"
    connect = Flaky(db)
    store = HistoryStore(WarehouseLease(connect), "sched")
    store.insert([_row("ok")])                       # creates the table

    connect.down = True
    t = dt.datetime(2026, 4, 6, 5, 0, 1, tzinfo=UTC)
    store.insert([_row("r1")])
    store.mark_started("r1", t, 1000)
    store.mark_finished("r1", "success", t + dt.timedelta(seconds=5))
    assert store.pending_writes == 3
    store.heartbeat(t)                               # still down: nothing lost
    assert store.pending_writes == 3

    connect.down = False
    store.heartbeat(t + dt.timedelta(seconds=30))    # the next tick's write replays them
    assert store.pending_writes == 0
    assert _rows(db) == [("ok", "queued", False, False, None), ("r1", "success", True, True, None)]


def test_buffer_is_bounded(tmp_path, monkeypatch):
    connect = Flaky(tmp_path / "w.duckdb")
    store = HistoryStore(WarehouseLease(connect), "sched")
    monkeypatch.setattr(store, "BUFFER_LIMIT", 3)
    connect.down = True
    for i in range(5):
        store.insert([_row(f"r{i}")])
    assert store.pending_writes == 3
    connect.down = False
    store.flush()
    assert [r[0] for r in _rows(tmp_path / "w.duckdb")] == ["r2", "r3", "r4"]


def test_scheduled_run_outcome_is_recorded_when_the_warehouse_was_held(tmp_path):
    """The fire, its start and its result are written once the warehouse is free."""
    from havn.engine.scheduler import SchedulerThread

    project = tmp_path / "proj"
    (project / "ingest").mkdir(parents=True)
    (project / "project.yml").write_text(textwrap.dedent("""\
        name: t
        database:
          path: warehouse.duckdb
        scheduler:
          timezone: UTC
        streams:
          hourly:
            schedule: "0 * * * *"
            steps:
              - ingest: [all]
    """), encoding="utf-8")
    db = project / "warehouse.duckdb"
    duckdb.connect(str(db)).close()

    t = SchedulerThread(project, clock=lambda: dt.datetime(2026, 4, 6, 5, 0, 5, tzinfo=UTC),
                        runner=lambda task: ("success", None))
    flaky = Flaky(db)
    t._lease._connect = flaky
    try:
        t._started_at = dt.datetime(2026, 4, 6, 4, 59, 0, tzinfo=UTC)
        flaky.down = True
        t._tick(now=dt.datetime(2026, 4, 6, 5, 0, 5, tzinfo=UTC))
        assert t.wait_idle(60)
        assert t._history.pending_writes >= 3       # queued, started, finished
        flaky.down = False
        t._tick(now=dt.datetime(2026, 4, 6, 5, 0, 35, tzinfo=UTC))
        assert t.wait_idle(60)
        assert t._history.pending_writes == 0
        rows = _rows(db)
        assert [(r[1], r[2], r[3]) for r in rows] == [("success", True, True)]
    finally:
        t.stop()
