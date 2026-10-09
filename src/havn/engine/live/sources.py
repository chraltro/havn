"""Live sources: stamping landing commits with a sequence and announcing them.

The convention every live landing table follows:

``_havn_seq BIGINT``
    Assigned by havn when a batch is committed, increasing in commit order
    per table. It is the watermark live models consume by: a live model
    reads ``WHERE _havn_seq > {watermark}`` and records the new high mark in
    the same transaction as its write. Rows not yet stamped have a NULL
    sequence and are invisible to that filter, so a writer that inserts
    first and stamps afterwards is still exact.

CDC landing tables (written by :mod:`havn.engine.streaming.cdc_logical`, or
by any ingest that follows the same shape) add two more:

``op VARCHAR``
    The change: ``I`` / ``U`` / ``D`` (``insert`` / ``update`` / ``delete``
    and Debezium's ``c`` / ``r`` are read the same way; anything starting
    with ``d`` is a delete).
``lsn BIGINT``
    The source's own change sequence (Postgres LSN). It orders the versions
    of one key, which is what makes applying duplicates and out-of-order
    replays idempotent; see ``cdc_op`` / ``cdc_seq`` in
    :mod:`havn.engine.transform.cdc_apply`.

Stamping and the watermark move happen in one transaction, and commits to one
table are serialized by a per-table lock in this process. Across processes
(DuckLake with a Postgres catalog) two writers that race are caught by the
database: both update the same ``live_sources`` row and one fails with a
write conflict instead of handing out overlapping sequences.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime

import duckdb

from havn.engine.utils import begin_transaction, validate_identifier

from . import events
from .state import ensure_live_tables, record_advance, source_watermarks, utcnow

logger = logging.getLogger("havn.live")

SEQ_COLUMN = "_havn_seq"
CDC_OP_COLUMN = "op"
CDC_LSN_COLUMN = "lsn"

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _source_lock(source: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(source)
        if lock is None:
            lock = _locks[source] = threading.Lock()
        return lock


@dataclass(frozen=True)
class Advance:
    """What one :func:`advance_source` call committed."""

    source: str
    wm_from: int
    wm_to: int
    rows: int
    advanced_at: datetime
    published: bool


def split_source(source: str) -> tuple[str, str]:
    """``landing.orders`` -> ("landing", "orders"), validated and lowercased."""
    parts = source.strip().lower().split(".")
    if len(parts) != 2:
        raise ValueError(f"live source must be schema.table, not {source!r}")
    schema, table = parts
    validate_identifier(schema, "source schema")
    validate_identifier(table, "source table")
    return schema, table


def ensure_seq_column(conn: duckdb.DuckDBPyConnection, schema: str, table: str) -> None:
    """Add ``_havn_seq`` to a landing table that does not have it yet."""
    row = conn.execute(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_catalog = current_database() AND table_schema = ? "
        "AND table_name = ? AND column_name = ?",
        [schema, table, SEQ_COLUMN],
    ).fetchone()
    if row and row[0]:
        return
    conn.execute(f'ALTER TABLE {schema}.{table} ADD COLUMN IF NOT EXISTS {SEQ_COLUMN} BIGINT')


def _table_exists(conn: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_catalog = current_database() AND table_schema = ? "
        "AND table_name = ? AND table_type = 'BASE TABLE'",
        [schema, table],
    ).fetchone()
    return bool(row and row[0])


def advance_source(
    conn: duckdb.DuckDBPyConnection,
    source: str,
    *,
    origin_at: datetime | None = None,
    publish: bool = True,
) -> Advance | None:
    """Stamp a landing table's new rows and announce that it advanced.

    Call it after committing rows to ``source`` (``schema.table``). Every row
    whose ``_havn_seq`` is still NULL is numbered in insertion order after
    the current watermark, the watermark moves, the advance is logged, and,
    once that has committed, :class:`~havn.engine.live.events.SourceAdvanced`
    goes out to the live runner. Returns None when there was nothing new.

    Ingest scripts call it the same way the built-in streaming connectors do::

        db.execute("INSERT INTO landing.orders SELECT ...")
        from havn.engine.live import advance_source
        advance_source(db, "landing.orders")

    When ``conn`` is already inside a transaction the stamping joins it and
    no event is published (the commit is the caller's, and announcing data
    that might still roll back would be wrong); the runner's watermark poll
    picks the advance up after the caller commits.
    """
    schema, table = split_source(source)
    source = f"{schema}.{table}"
    with _source_lock(source):
        if not _table_exists(conn, schema, table):
            raise ValueError(f"live source {source} does not exist")
        owns_tx = begin_transaction(conn)
        try:
            ensure_live_tables(conn)
            ensure_seq_column(conn, schema, table)
            wm_from = source_watermarks(conn, [source]).get(source, 0)
            result = conn.execute(
                f"""
                UPDATE {schema}.{table} AS t SET {SEQ_COLUMN} = s.seq
                FROM (
                    SELECT rowid AS _rid, ? + row_number() OVER (ORDER BY rowid) AS seq
                    FROM {schema}.{table} WHERE {SEQ_COLUMN} IS NULL
                ) AS s
                WHERE t.rowid = s._rid
                """,
                [wm_from],
            ).fetchone()
            rows = int(result[0]) if result and result[0] else 0
            now = utcnow()
            if rows:
                record_advance(
                    conn, source, "source", wm_from, wm_from + rows, rows, now, origin_at or now
                )
            if owns_tx:
                conn.execute("COMMIT")
        except Exception:
            if owns_tx:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
            raise
    if not rows:
        return None
    published = bool(owns_tx and publish)
    if published:
        events.publish(
            events.SourceAdvanced(
                source=source, watermark=wm_from + rows, rows=rows, advanced_at=now
            )
        )
    return Advance(source, wm_from, wm_from + rows, rows, now, published)


def notify_advanced(source: str, watermark: int, rows: int = 0) -> None:
    """Publish an advance a caller committed itself inside its own transaction."""
    events.publish(
        events.SourceAdvanced(source=source, watermark=watermark, rows=rows, advanced_at=utcnow())
    )
