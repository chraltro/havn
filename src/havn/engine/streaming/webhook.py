"""Webhook staging + flush worker.

The existing webhook route at ``/api/webhook/{name}`` inserts each POST into
``landing.<name>_inbox`` directly. That works for light loads but:

- every POST is its own DuckDB write (no batching),
- DuckLake users lose the benefit of data-inlining tuning per table,
- the write queue serializes every insert across the whole warehouse.

This module adds a staging layer: webhook rows land in ``_havn.webhook_staging``
(a single hot table), a background worker drains it every ``flush_interval``
seconds, and batches into the target landing table. On DuckLake the worker
respects per-table ``inlining_row_limit`` (up to ~500 rows per insert) so
streaming writes stay in the Postgres catalog until a CHECKPOINT.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

import duckdb

from havn.engine.utils import validate_identifier

logger = logging.getLogger("havn.streaming.webhook")

STAGING_TABLE = "_havn.webhook_staging"


# ---------------------------------------------------------------------------
# Staging table DDL
# ---------------------------------------------------------------------------


class WebhookStaging:
    """Helpers for the single staging table shared by all webhooks."""

    @staticmethod
    def ensure(conn: duckdb.DuckDBPyConnection) -> None:
        """Create the staging table if it doesn't exist. Idempotent."""
        conn.execute("CREATE SCHEMA IF NOT EXISTS _havn")
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {STAGING_TABLE} (
                id VARCHAR DEFAULT gen_random_uuid()::VARCHAR,
                source VARCHAR NOT NULL,
                received_at TIMESTAMP DEFAULT current_timestamp,
                payload JSON,
                flushed BOOLEAN DEFAULT false
            )
            """
        )

    @staticmethod
    def backlog(conn: duckdb.DuckDBPyConnection) -> int:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {STAGING_TABLE} WHERE flushed = false"
        ).fetchone()
        return int(row[0]) if row else 0


def append_event(
    conn: duckdb.DuckDBPyConnection,
    source: str,
    payload: dict | list | str,
) -> None:
    """Append a single webhook event to the staging table.

    Called from the FastAPI webhook handler under the write-queue lock.
    """
    validate_identifier(source, "webhook source")
    WebhookStaging.ensure(conn)
    if isinstance(payload, (dict, list)):
        payload_str = json.dumps(payload)
    else:
        payload_str = str(payload)
    conn.execute(
        f"INSERT INTO {STAGING_TABLE} (source, payload) VALUES (?, ?::JSON)",
        [source, payload_str],
    )


# ---------------------------------------------------------------------------
# FlushWorker
# ---------------------------------------------------------------------------


@dataclass
class FlushStats:
    flushes: int = 0
    rows_flushed: int = 0
    errors: int = 0
    last_error: str | None = None


class FlushWorker:
    """Background thread that drains ``webhook_staging`` into landing tables.

    Usage::

        worker = FlushWorker(connection_factory=lambda: backend.connect())
        worker.start()
        ...
        worker.stop()

    ``connection_factory`` must return a fresh write-capable DuckDB connection
    each call. The worker closes its connection on stop.
    """

    def __init__(
        self,
        *,
        connection_factory: Callable[[], duckdb.DuckDBPyConnection],
        flush_interval: float = 15.0,
        batch_size: int = 500,
    ) -> None:
        self._factory = connection_factory
        self._interval = flush_interval
        self._batch_size = batch_size
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stats = FlushStats()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="havn-webhook-flush", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.flush_once()
            except Exception as e:
                self.stats.errors += 1
                self.stats.last_error = str(e)[:500]
                logger.warning("flush loop error: %s", e)
            self._stop.wait(self._interval)

    def flush_once(self) -> int:
        """Drain one batch. Returns rows flushed this call."""
        from havn.engine.observability import ROWS_PROCESSED, STREAMING_EVENTS
        from havn.engine.resource_manager import get_resource_manager

        manager = get_resource_manager()
        conn = self._factory()
        try:
            WebhookStaging.ensure(conn)

            with manager.acquire_sync("streaming", "webhook-flush", conn=conn):
                sources = conn.execute(
                    f"SELECT source, COUNT(*) AS n FROM {STAGING_TABLE} "
                    f"WHERE flushed = false GROUP BY source"
                ).fetchall()
                if not sources:
                    return 0

                total = 0
                for source, _ in sources:
                    flushed = self._flush_source(conn, source)
                    total += flushed
                    if flushed:
                        STREAMING_EVENTS.labels(source=source, status="flushed").inc(flushed)
                        ROWS_PROCESSED.labels(category="streaming").inc(flushed)

                self.stats.flushes += 1
                self.stats.rows_flushed += total
                return total
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _flush_source(self, conn: duckdb.DuckDBPyConnection, source: str) -> int:
        """Move one batch for a single source into ``landing.<source>``."""
        validate_identifier(source, "webhook source")
        target = f"landing.{source}"

        conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {target} (
                id VARCHAR,
                received_at TIMESTAMP,
                payload JSON
            )
            """
        )

        # Pull up to batch_size IDs we're about to flush.
        ids = [
            r[0]
            for r in conn.execute(
                f"SELECT id FROM {STAGING_TABLE} "
                f"WHERE flushed = false AND source = ? "
                f"ORDER BY received_at LIMIT ?",
                [source, self._batch_size],
            ).fetchall()
        ]
        if not ids:
            return 0

        placeholders = ",".join(["?"] * len(ids))
        conn.execute(
            f"""
            INSERT INTO {target} (id, received_at, payload)
            SELECT id, received_at, payload FROM {STAGING_TABLE}
            WHERE id IN ({placeholders})
            """,
            ids,
        )
        conn.execute(
            f"UPDATE {STAGING_TABLE} SET flushed = true WHERE id IN ({placeholders})",
            ids,
        )
        return len(ids)

    def purge_flushed(self, older_than_seconds: int = 3600) -> int:
        """Delete flushed rows older than ``older_than_seconds`` (housekeeping)."""
        conn = self._factory()
        try:
            WebhookStaging.ensure(conn)
            res = conn.execute(
                f"""
                DELETE FROM {STAGING_TABLE}
                WHERE flushed = true
                  AND received_at < current_timestamp - INTERVAL '1 second' * ?
                """,
                [older_than_seconds],
            )
            try:
                return res.rowcount  # type: ignore[attr-defined]
            except Exception:
                return 0
        finally:
            try:
                conn.close()
            except Exception:
                pass
