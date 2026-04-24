"""Postgres logical-replication CDC consumer.

Wraps ``pypgoutput`` (vendored under :mod:`havn.vendor.pypgoutput`) to stream
WAL events from a Postgres source into havn landing tables with low latency.

Design:

- One :class:`LogicalCDCConsumer` per source database.
- Buffers events by target table; flushes every ``flush_interval`` seconds
  or when a table buffer reaches ``flush_rows``.
- Low-level DDL replication is out of scope — users own their landing schema.
- If ``pypgoutput`` is not importable at runtime (user hasn't installed the
  optional ``psycopg`` extra), :func:`build_consumer` raises
  :class:`LogicalCDCUnavailable` with an actionable message.

This is a thin orchestration layer: the hard parts (WAL decoding, replication
slots) are delegated to pypgoutput.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import duckdb

from havn.engine.utils import validate_identifier

logger = logging.getLogger("havn.streaming.cdc_logical")


class LogicalCDCUnavailable(RuntimeError):
    """Raised when pypgoutput / psycopg isn't importable at runtime."""


@dataclass
class LogicalCDCConfig:
    """Per-source configuration."""

    dsn: str                              # postgres DSN for the replication connection
    slot_name: str                        # replication slot name
    publication: str                      # publication name
    tables: list[str] = field(default_factory=list)  # qualified source table names
    target_schema: str = "landing"
    flush_interval: float = 10.0
    flush_rows: int = 50


def _require_pypgoutput():
    try:
        from havn.vendor import pypgoutput  # type: ignore[import-not-found]
    except Exception as e:  # pragma: no cover - exercised only when vendor missing
        raise LogicalCDCUnavailable(
            "pypgoutput not vendored. See havn/vendor/pypgoutput/README for the "
            "drop-in steps, and install the 'psycopg[binary]' optional extra."
        ) from e
    return pypgoutput


# ---------------------------------------------------------------------------
# Consumer
# ---------------------------------------------------------------------------


class LogicalCDCConsumer:
    """Drives pypgoutput and flushes decoded events into DuckDB landing tables."""

    def __init__(
        self,
        config: LogicalCDCConfig,
        *,
        connection_factory: Callable[[], duckdb.DuckDBPyConnection],
    ) -> None:
        self.config = config
        self._factory = connection_factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._buffers: dict[str, list[dict[str, Any]]] = {}
        self._last_flush = time.time()
        self.rows_consumed = 0
        self.rows_flushed = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"havn-cdc-{self.config.slot_name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    # --- Main loop ---------------------------------------------------------

    def _run(self) -> None:
        try:
            pypgoutput = _require_pypgoutput()
        except LogicalCDCUnavailable as e:
            logger.error("logical cdc disabled: %s", e)
            return

        extractor = pypgoutput.LogicalReplicationReader(  # type: ignore[attr-defined]
            publication_name=self.config.publication,
            slot_name=self.config.slot_name,
            dsn=self.config.dsn,
        )

        try:
            for message in self._iter(extractor):
                if self._stop.is_set():
                    break
                self._handle(message)
                if self._should_flush():
                    self._flush()
        finally:
            self._flush()
            try:
                extractor.stop()
            except Exception:
                pass

    def _iter(self, extractor) -> Iterable[Any]:
        """Yield messages from the pypgoutput reader with a stop check."""
        while not self._stop.is_set():
            for message in extractor:  # pypgoutput iterator
                if self._stop.is_set():
                    return
                yield message

    def _handle(self, message) -> None:
        # pypgoutput yields ChangeEvent-like objects; we keep only insert/update.
        op = getattr(message, "op", None) or getattr(message, "type", None)
        table = getattr(message, "table_name", None) or getattr(message, "table", None)
        if not table or op not in {"I", "U", "INSERT", "UPDATE"}:
            return
        data = getattr(message, "after", None) or getattr(message, "new", None) or getattr(message, "data", None)
        if data is None:
            return
        if table not in self.config.tables:
            return
        self._buffers.setdefault(table, []).append({"op": op, "row": data, "ts": time.time()})
        self.rows_consumed += 1

    def _should_flush(self) -> bool:
        if not self._buffers:
            return False
        max_buf = max(len(v) for v in self._buffers.values())
        if max_buf >= self.config.flush_rows:
            return True
        return time.time() - self._last_flush >= self.config.flush_interval

    def _flush(self) -> None:
        if not self._buffers:
            self._last_flush = time.time()
            return
        from havn.engine.observability import ROWS_PROCESSED, STREAMING_EVENTS
        from havn.engine.resource_manager import get_resource_manager

        manager = get_resource_manager()
        conn = self._factory()
        try:
            with manager.acquire_sync(
                "streaming",
                f"cdc-flush:{self.config.slot_name}",
                conn=conn,
            ):
                conn.execute(f"CREATE SCHEMA IF NOT EXISTS {self.config.target_schema}")
                for table, rows in self._buffers.items():
                    validate_identifier(self.config.target_schema, "target schema")
                    short = table.split(".")[-1]
                    validate_identifier(short, "table")
                    target = f"{self.config.target_schema}.{short}"
                    conn.execute(
                        f"""
                        CREATE TABLE IF NOT EXISTS {target} (
                            op VARCHAR,
                            received_at TIMESTAMP,
                            payload JSON
                        )
                        """
                    )
                    rows_to_insert = [
                        (r["op"], r["ts"], json.dumps(r["row"], default=str)) for r in rows
                    ]
                    conn.executemany(
                        f"INSERT INTO {target} (op, received_at, payload) "
                        f"VALUES (?, to_timestamp(?), ?::JSON)",
                        rows_to_insert,
                    )
                    STREAMING_EVENTS.labels(source=short, status="cdc").inc(len(rows))
                    ROWS_PROCESSED.labels(category="streaming").inc(len(rows))
                    self.rows_flushed += len(rows)
            self._buffers.clear()
            self._last_flush = time.time()
        except Exception as e:
            logger.warning("cdc flush failed: %s", e)
        finally:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def build_consumer(
    config: LogicalCDCConfig,
    *,
    connection_factory: Callable[[], duckdb.DuckDBPyConnection],
) -> LogicalCDCConsumer:
    """Construct a consumer after confirming pypgoutput is importable."""
    _require_pypgoutput()
    return LogicalCDCConsumer(config, connection_factory=connection_factory)
