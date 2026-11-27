"""Persisted scheduler history: one row per schedule fire.

``_havn.schedule_history`` records every fire the scheduler saw: when it was
due, when it started and finished, how late it started and how it ended.
``_havn.scheduler_state`` holds the scheduler's heartbeat, which is how a
restart knows from when to look for fires that passed while it was down.

All timestamps are naive UTC. Statuses:

- ``queued``: handed to the worker pool, not started yet
- ``running``: started
- ``success`` / ``failed``: finished
- ``skipped_overlap``: the same stream or job was still running or already
  queued, and the schedule's overlap policy dropped this fire
- ``missed``: the fire passed while no scheduler was watching (process down,
  machine asleep); with ``catchup: true`` the newest one is run instead
- ``interrupted``: was queued or running when the previous scheduler process
  ended
- ``cancelled``: was queued when the scheduler was stopped

Writes go through a :class:`WarehouseLease`: one connection that stays open
while a run is queued or in progress and is closed when the scheduler is
idle, so ``havn schedule`` holds no lock on the warehouse between runs.
"""

from __future__ import annotations

import datetime
import logging
import threading
import uuid

logger = logging.getLogger("havn.scheduler")

UTC = datetime.timezone.utc

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped_overlap"
STATUS_MISSED = "missed"
STATUS_INTERRUPTED = "interrupted"
STATUS_CANCELLED = "cancelled"

# History writes from the loop and the workers take turns; DuckDB allows
# concurrent appends, but schema creation and the retention DELETE are
# simpler to reason about one at a time.
_write_lock = threading.Lock()


def ensure_schedule_tables(conn) -> None:
    """Make sure the scheduler's metadata tables exist.

    ``_havn.schedule_history`` and ``_havn.scheduler_state`` are defined in
    :mod:`havn.engine.migrations` (baseline), with every other ``_havn``
    table.
    """
    from havn.engine.database import ensure_meta_table

    ensure_meta_table(conn)


def utc_naive(instant: datetime.datetime | None) -> datetime.datetime | None:
    """An aware instant as the naive UTC value stored in the tables."""
    if instant is None:
        return None
    if instant.tzinfo is None:
        instant = instant.astimezone(UTC)
    return instant.astimezone(UTC).replace(tzinfo=None)


def from_utc_naive(value: datetime.datetime | None) -> datetime.datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class WarehouseLease:
    """One warehouse connection, open while anybody holds the lease.

    DuckDB shares one database instance between the connections a process
    opens to the same file, and tears it down when the last one closes. A
    thread that opens the file while another is closing the last
    connection gets "Cannot open file ... being used by another process"
    (measured: 22 of 100 open/close cycles from two threads failed). The
    scheduler therefore holds this lease from the moment a run is queued
    until it has finished: the stream/job opens and closes its own
    connections inside that window without ever being the last one, and
    history writes go through cursors of the leased connection.

    With nothing queued or running the lease is released and the
    connection closed, so ``havn schedule`` does not lock the warehouse
    against other processes between runs.
    """

    def __init__(self, connect):
        self._connect = connect
        self._lock = threading.Lock()
        self._conn = None
        self._holders = 0

    def try_acquire(self) -> bool:
        """Take the lease; False (holding nothing) if the warehouse cannot be opened."""
        with self._lock:
            if self._holders == 0:
                try:
                    self._conn = self._connect()
                except Exception as exc:
                    logger.warning("Scheduler: could not open the warehouse: %s", exc)
                    return False
            self._holders += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._holders <= 0:
                return
            self._holders -= 1
            if self._holders == 0 and self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def cursor(self):
        """A cursor on the leased connection (call only while holding the lease)."""
        from havn.engine.write_queue import cursor_for

        with self._lock:
            if self._conn is None:
                raise RuntimeError("warehouse lease not held")
            # cursor_for selects the warehouse catalog on DuckLake, where a
            # bare cursor would start in the in-memory default database.
            return cursor_for(self._conn)

    @property
    def held(self) -> bool:
        return self._holders > 0


class HistoryStore:
    """Writes and reads schedule history through a :class:`WarehouseLease`.

    Each write takes the lease for its duration (opening the connection
    only when nothing else holds it) and uses its own cursor. Write
    failures are logged and swallowed: a history row must never stop a run
    from happening.
    """

    def __init__(self, lease: WarehouseLease, scheduler_id: str):
        self._lease = lease
        self.scheduler_id = scheduler_id
        self._ready = False

    # -- plumbing -------------------------------------------------------

    def _with_conn(self, fn, *, what: str):
        with _write_lock:
            if not self._lease.try_acquire():
                logger.warning("Schedule history: could not %s: the warehouse is not available", what)
                return None
            cur = None
            try:
                cur = self._lease.cursor()
                if not self._ready:
                    ensure_schedule_tables(cur)
                    self._ready = True
                return fn(cur)
            except Exception as exc:
                logger.warning("Schedule history: could not %s: %s", what, exc)
                return None
            finally:
                if cur is not None:
                    try:
                        cur.close()
                    except Exception:
                        pass
                self._lease.release()

    # -- writes ---------------------------------------------------------

    def insert(self, rows: list[dict]) -> None:
        """Insert fire rows (dicts with the column names as keys)."""
        if not rows:
            return
        cols = (
            "id", "schedule_kind", "schedule_name", "cron_expr", "timezone",
            "scheduled_for", "recorded_at", "started_at", "finished_at",
            "lateness_ms", "status", "trigger", "error", "scheduler_id",
        )

        def go(conn):
            conn.executemany(
                f"INSERT INTO _havn.schedule_history ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                [[r.get(c) if c != "scheduler_id" else self.scheduler_id for c in cols] for r in rows],
            )

        self._with_conn(go, what="record fires")

    def mark_started(self, row_id: str, started_at: datetime.datetime, lateness_ms: int) -> None:
        self._with_conn(
            lambda conn: conn.execute(
                "UPDATE _havn.schedule_history SET status = ?, started_at = ?, lateness_ms = ? WHERE id = ?",
                [STATUS_RUNNING, utc_naive(started_at), lateness_ms, row_id],
            ),
            what="record a run start",
        )

    def mark_finished(
        self, row_id: str, status: str, finished_at: datetime.datetime, error: str | None = None,
    ) -> None:
        self._with_conn(
            lambda conn: conn.execute(
                "UPDATE _havn.schedule_history SET status = ?, finished_at = ?, error = ? WHERE id = ?",
                [status, utc_naive(finished_at), (error or None) and str(error)[:4000], row_id],
            ),
            what="record a run result",
        )

    def mark_status(self, row_ids: list[str], status: str, error: str | None = None) -> None:
        if not row_ids:
            return
        self._with_conn(
            lambda conn: conn.executemany(
                "UPDATE _havn.schedule_history SET status = ?, error = ? WHERE id = ?",
                [[status, error, rid] for rid in row_ids],
            ),
            what=f"mark runs {status}",
        )

    def heartbeat(self, now: datetime.datetime) -> None:
        def go(conn):
            conn.execute("DELETE FROM _havn.scheduler_state WHERE name = 'scheduler'")
            conn.execute(
                "INSERT INTO _havn.scheduler_state (name, last_seen, scheduler_id) VALUES ('scheduler', ?, ?)",
                [utc_naive(now), self.scheduler_id],
            )

        self._with_conn(go, what="write the heartbeat")

    def startup(self, retention_days: int, now: datetime.datetime) -> dict | None:
        """Prepare for a new scheduler process; return what it needs to resume.

        Marks rows left ``queued`` or ``running`` by an earlier process as
        ``interrupted``, applies retention, and returns ``{"last_seen":
        instant | None, "last_fires": {(kind, name, expr): instant}}``.
        Returns ``None`` when the warehouse could not be read.
        """
        def go(conn):
            conn.execute(
                "UPDATE _havn.schedule_history SET status = ?, "
                "error = 'The scheduler stopped before this run finished' "
                "WHERE status IN (?, ?) AND scheduler_id <> ?",
                [STATUS_INTERRUPTED, STATUS_QUEUED, STATUS_RUNNING, self.scheduler_id],
            )
            _purge(conn, retention_days, now)
            row = conn.execute(
                "SELECT max(last_seen) FROM _havn.scheduler_state WHERE name = 'scheduler'"
            ).fetchone()
            last_seen = from_utc_naive(row[0]) if row else None
            last_fires = {
                (r[0], r[1], r[2]): from_utc_naive(r[3])
                for r in conn.execute(
                    "SELECT schedule_kind, schedule_name, cron_expr, max(scheduled_for) "
                    "FROM _havn.schedule_history GROUP BY 1, 2, 3"
                ).fetchall()
            }
            return {"last_seen": last_seen, "last_fires": last_fires}

        return self._with_conn(go, what="read history at startup")

    def purge(self, retention_days: int, now: datetime.datetime) -> None:
        self._with_conn(lambda conn: _purge(conn, retention_days, now), what="apply retention")


def _purge(conn, retention_days: int, now: datetime.datetime) -> None:
    if retention_days <= 0:
        return
    cutoff = utc_naive(now) - datetime.timedelta(days=retention_days)
    conn.execute("DELETE FROM _havn.schedule_history WHERE scheduled_for < ?", [cutoff])


def read_history(conn, *, limit: int = 50, name: str | None = None, status: str | None = None) -> list[dict]:
    """Newest-first history rows as dicts; ``[]`` when the table does not exist yet.

    Timestamps are ISO strings in UTC with a ``Z`` suffix.
    """
    import duckdb

    where, params = [], []
    if name:
        where.append("schedule_name = ?")
        params.append(name)
    if status:
        where.append("status = ?")
        params.append(status)
    sql = (
        "SELECT id, schedule_kind, schedule_name, cron_expr, timezone, scheduled_for, "
        "started_at, finished_at, lateness_ms, status, trigger, error "
        "FROM _havn.schedule_history"
        + (f" WHERE {' AND '.join(where)}" if where else "")
        + " ORDER BY scheduled_for DESC, recorded_at DESC LIMIT ?"
    )
    params.append(max(1, min(int(limit), 10_000)))
    try:
        rows = conn.execute(sql, params).fetchall()
    except duckdb.CatalogException:
        return []

    def iso(v):
        return v.isoformat() + "Z" if v is not None else None

    out = []
    for r in rows:
        duration_ms = None
        if r[6] is not None and r[7] is not None:
            duration_ms = int((r[7] - r[6]).total_seconds() * 1000)
        out.append({
            "id": r[0],
            "kind": r[1],
            "name": r[2],
            "cron": r[3],
            "timezone": r[4],
            "scheduled_for": iso(r[5]),
            "started_at": iso(r[6]),
            "finished_at": iso(r[7]),
            "lateness_ms": r[8],
            "duration_ms": duration_ms,
            "status": r[9],
            "trigger": r[10],
            "error": r[11],
        })
    return out
