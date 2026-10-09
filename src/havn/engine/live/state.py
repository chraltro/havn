"""Durable state of live sources and live models, in the ``_havn`` schema.

Four tables, all created lazily by :func:`ensure_live_tables`:

``live_sources``
    One row per thing a live model can read incrementally: a landing table
    written through :func:`havn.engine.live.sources.advance_source` (kind
    ``source``) or a live model (kind ``model``). ``watermark`` is the
    highest ``_havn_seq`` committed so far.

``live_advances``
    One row per commit that moved a source's watermark: the range it covered,
    how many rows, when it was committed and when its data *originally*
    arrived. For a landing batch the two times are the same; for a live model
    ``origin_at`` is carried over from the source data it consumed, which is
    what makes lag end-to-end rather than per hop.

``live_consumed``
    Per live model and input source, the watermark the model has applied.
    Written in the same transaction as the model's data, so the two cannot
    disagree, and two builders racing on one model hit a write conflict
    instead of both applying the same batch.

``live_state``
    Per live model, what the runner knows about it: paused by a user,
    failing with backoff, counters, the last error and the last refresh.

Every timestamp is UTC wall clock supplied by Python, never DuckDB's
``current_timestamp``: inside a transaction that is the transaction's start
time, and lag measured from it would be wrong by however long the refresh
took.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import duckdb

logger = logging.getLogger("havn.live")

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS _havn.live_sources (
        source      VARCHAR PRIMARY KEY,
        kind        VARCHAR NOT NULL,
        watermark   BIGINT NOT NULL,
        rows_total  BIGINT DEFAULT 0,
        advanced_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS _havn.live_advances (
        source      VARCHAR NOT NULL,
        wm_from     BIGINT NOT NULL,
        wm_to       BIGINT NOT NULL,
        "rows"      BIGINT DEFAULT 0,
        advanced_at TIMESTAMP NOT NULL,
        origin_at   TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS _havn.live_consumed (
        model       VARCHAR NOT NULL,
        source      VARCHAR NOT NULL,
        watermark   BIGINT NOT NULL,
        consumed_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS _havn.live_state (
        model                VARCHAR PRIMARY KEY,
        status               VARCHAR NOT NULL,
        paused               BOOLEAN DEFAULT false,
        consecutive_failures INTEGER DEFAULT 0,
        next_retry_at        TIMESTAMP,
        last_error           VARCHAR,
        last_refresh_at      TIMESTAMP,
        last_duration_ms     BIGINT DEFAULT 0,
        last_lag_ms          BIGINT,
        refreshes            BIGINT DEFAULT 0,
        rows_total           BIGINT DEFAULT 0,
        last_assertions_at   TIMESTAMP,
        last_profile_at      TIMESTAMP,
        updated_at           TIMESTAMP
    )
    """,
)


def utcnow() -> datetime:
    """Naive UTC now, the form every live timestamp is stored in."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ensure_live_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the live tables if they are missing. Cheap when they exist."""
    try:
        conn.execute("SELECT 1 FROM _havn.live_state LIMIT 0")
        conn.execute("SELECT 1 FROM _havn.live_advances LIMIT 0")
        return
    except duckdb.CatalogException:
        pass
    from havn.engine.database import _is_ducklake_connection, _strip_pk

    is_lake = _is_ducklake_connection(conn)
    conn.execute("CREATE SCHEMA IF NOT EXISTS _havn")
    for ddl in _DDL:
        conn.execute(_strip_pk(ddl, is_lake))


def _tables_exist(conn: duckdb.DuckDBPyConnection) -> bool:
    try:
        conn.execute("SELECT 1 FROM _havn.live_sources LIMIT 0")
        return True
    except duckdb.CatalogException:
        return False


# ---------------------------------------------------------------------------
# Sources and advances
# ---------------------------------------------------------------------------


def source_watermarks(
    conn: duckdb.DuckDBPyConnection, sources: Iterable[str] | None = None
) -> dict[str, int]:
    """Current committed watermark per source (missing sources are absent)."""
    if not _tables_exist(conn):
        return {}
    names = sorted(set(sources)) if sources is not None else None
    if names is not None and not names:
        return {}
    if names is None:
        rows = conn.execute("SELECT source, watermark FROM _havn.live_sources").fetchall()
    else:
        marks = ", ".join("?" for _ in names)
        rows = conn.execute(
            f"SELECT source, watermark FROM _havn.live_sources WHERE source IN ({marks})",
            names,
        ).fetchall()
    return {r[0]: int(r[1] or 0) for r in rows}


def record_advance(
    conn: duckdb.DuckDBPyConnection,
    source: str,
    kind: str,
    wm_from: int,
    wm_to: int,
    rows: int,
    advanced_at: datetime,
    origin_at: datetime,
) -> None:
    """Move ``source`` to ``wm_to`` and log the advance. Caller owns the tx."""
    updated = conn.execute(
        "UPDATE _havn.live_sources SET watermark = ?, kind = ?, "
        "rows_total = COALESCE(rows_total, 0) + ?, advanced_at = ? WHERE source = ?",
        [wm_to, kind, rows, advanced_at, source],
    ).fetchone()
    if not updated or not updated[0]:
        conn.execute(
            "INSERT INTO _havn.live_sources (source, kind, watermark, rows_total, advanced_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [source, kind, wm_to, rows, advanced_at],
        )
    conn.execute(
        'INSERT INTO _havn.live_advances (source, wm_from, wm_to, "rows", advanced_at, origin_at) '
        "VALUES (?, ?, ?, ?, ?, ?)",
        [source, wm_from, wm_to, rows, advanced_at, origin_at],
    )


def oldest_origin(
    conn: duckdb.DuckDBPyConnection, consumed: dict[str, int], upto: dict[str, int] | None = None
) -> datetime | None:
    """When the oldest data newer than ``consumed`` (and at most ``upto``) arrived.

    ``consumed`` maps source -> watermark already applied. Returns None when
    nothing is pending, which is what "lag zero" means.
    """
    if not consumed or not _tables_exist(conn):
        return None
    clauses = []
    params: list = []
    for source, wm in consumed.items():
        if upto is not None and source in upto:
            clauses.append("(source = ? AND wm_to > ? AND wm_from < ?)")
            params.extend([source, wm, upto[source]])
        else:
            clauses.append("(source = ? AND wm_to > ?)")
            params.extend([source, wm])
    row = conn.execute(
        f"SELECT MIN(origin_at) FROM _havn.live_advances WHERE {' OR '.join(clauses)}",
        params,
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def events_per_second(conn: duckdb.DuckDBPyConnection, window_s: float = 60.0) -> dict[str, float]:
    """Rows advanced per second over the last ``window_s`` seconds, per source."""
    if not _tables_exist(conn):
        return {}
    since = utcnow().timestamp() - window_s
    since_dt = datetime.fromtimestamp(since, timezone.utc).replace(tzinfo=None)
    rows = conn.execute(
        'SELECT source, SUM("rows") FROM _havn.live_advances WHERE advanced_at >= ? GROUP BY source',
        [since_dt],
    ).fetchall()
    return {r[0]: round(float(r[1] or 0) / window_s, 3) for r in rows}


def prune_advances(conn: duckdb.DuckDBPyConnection, keep_s: float = 3600.0) -> None:
    """Drop advance rows every consumer has applied and that are older than ``keep_s``.

    Anything older than a week goes regardless, so an abandoned consumer
    cannot make the log grow without bound.
    """
    if not _tables_exist(conn):
        return
    now = utcnow().timestamp()
    cutoff = datetime.fromtimestamp(now - keep_s, timezone.utc).replace(tzinfo=None)
    hard_cutoff = datetime.fromtimestamp(now - 7 * 86400, timezone.utc).replace(tzinfo=None)
    conn.execute(
        """
        DELETE FROM _havn.live_advances AS a
        WHERE a.advanced_at < ?
          AND a.wm_to <= COALESCE(
              (SELECT MIN(c.watermark) FROM _havn.live_consumed c WHERE c.source = a.source),
              a.wm_to)
        """,
        [cutoff],
    )
    conn.execute("DELETE FROM _havn.live_advances WHERE advanced_at < ?", [hard_cutoff])


# ---------------------------------------------------------------------------
# Consumed watermarks
# ---------------------------------------------------------------------------


def consumed_watermarks(conn: duckdb.DuckDBPyConnection, model: str) -> dict[str, int]:
    if not _tables_exist(conn):
        return {}
    rows = conn.execute(
        "SELECT source, watermark FROM _havn.live_consumed WHERE model = ?", [model]
    ).fetchall()
    return {r[0]: int(r[1] or 0) for r in rows}


def all_consumed(conn: duckdb.DuckDBPyConnection) -> dict[str, dict[str, int]]:
    if not _tables_exist(conn):
        return {}
    out: dict[str, dict[str, int]] = {}
    for model, source, wm in conn.execute(
        "SELECT model, source, watermark FROM _havn.live_consumed"
    ).fetchall():
        out.setdefault(model, {})[source] = int(wm or 0)
    return out


def write_consumed(
    conn: duckdb.DuckDBPyConnection, model: str, marks: dict[str, int], at: datetime
) -> None:
    """Record what ``model`` has applied. Caller owns the transaction.

    Delete-then-insert per source rather than one upsert: it works the same
    on DuckLake (no primary keys) and the DELETE is what makes a concurrent
    second builder of the same model conflict instead of double-applying.
    """
    for source, wm in sorted(marks.items()):
        conn.execute(
            "DELETE FROM _havn.live_consumed WHERE model = ? AND source = ?", [model, source]
        )
        conn.execute(
            "INSERT INTO _havn.live_consumed (model, source, watermark, consumed_at) VALUES (?, ?, ?, ?)",
            [model, source, int(wm), at],
        )


def reset_consumed(conn: duckdb.DuckDBPyConnection, model: str) -> None:
    ensure_live_tables(conn)
    conn.execute("DELETE FROM _havn.live_consumed WHERE model = ?", [model])


# ---------------------------------------------------------------------------
# Per-model runner state
# ---------------------------------------------------------------------------


@dataclass
class ModelLiveState:
    model: str
    status: str = "active"          # active | failing | paused
    paused: bool = False
    consecutive_failures: int = 0
    next_retry_at: datetime | None = None
    last_error: str | None = None
    last_refresh_at: datetime | None = None
    last_duration_ms: int = 0
    last_lag_ms: int | None = None
    refreshes: int = 0
    rows_total: int = 0
    last_assertions_at: datetime | None = None
    last_profile_at: datetime | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict:
        def ts(v: datetime | None) -> str | None:
            return v.isoformat() + "Z" if v is not None else None

        return {
            "model": self.model,
            "status": self.status,
            "paused": self.paused,
            "consecutive_failures": self.consecutive_failures,
            "next_retry_at": ts(self.next_retry_at),
            "last_error": self.last_error,
            "last_refresh_at": ts(self.last_refresh_at),
            "last_duration_ms": self.last_duration_ms,
            "last_lag_ms": self.last_lag_ms,
            "refreshes": self.refreshes,
            "rows_total": self.rows_total,
        }


_STATE_COLUMNS = (
    "model", "status", "paused", "consecutive_failures", "next_retry_at", "last_error",
    "last_refresh_at", "last_duration_ms", "last_lag_ms", "refreshes", "rows_total",
    "last_assertions_at", "last_profile_at", "updated_at",
)


def load_states(conn: duckdb.DuckDBPyConnection) -> dict[str, ModelLiveState]:
    if not _tables_exist(conn):
        return {}
    rows = conn.execute(f"SELECT {', '.join(_STATE_COLUMNS)} FROM _havn.live_state").fetchall()
    out = {}
    for row in rows:
        values = dict(zip(_STATE_COLUMNS, row))
        values["paused"] = bool(values["paused"])
        values["consecutive_failures"] = int(values["consecutive_failures"] or 0)
        values["last_duration_ms"] = int(values["last_duration_ms"] or 0)
        values["refreshes"] = int(values["refreshes"] or 0)
        values["rows_total"] = int(values["rows_total"] or 0)
        out[values["model"]] = ModelLiveState(**values)
    return out


def save_state(conn: duckdb.DuckDBPyConnection, state: ModelLiveState) -> None:
    ensure_live_tables(conn)
    state.updated_at = utcnow()
    values = [getattr(state, c) for c in _STATE_COLUMNS]
    conn.execute("DELETE FROM _havn.live_state WHERE model = ?", [state.model])
    conn.execute(
        f"INSERT INTO _havn.live_state ({', '.join(_STATE_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in _STATE_COLUMNS)})",
        values,
    )


def set_paused(conn: duckdb.DuckDBPyConnection, model: str, paused: bool) -> ModelLiveState:
    """Pause or resume one live model in the warehouse (works with no runner)."""
    ensure_live_tables(conn)
    state = load_states(conn).get(model) or ModelLiveState(model=model)
    state.paused = paused
    if paused:
        state.status = "paused"
    else:
        state.status = "active"
        state.consecutive_failures = 0
        state.next_retry_at = None
    save_state(conn, state)
    return state
