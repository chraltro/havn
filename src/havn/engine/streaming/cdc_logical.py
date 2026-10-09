"""Postgres logical-replication CDC consumer.

Wraps ``pypgoutput`` (vendored under :mod:`havn.vendor.pypgoutput`) to stream
WAL events from a Postgres source into havn landing tables with low latency.

Design:

- One :class:`LogicalCDCConsumer` per source database.
- Buffers events by target table; flushes every ``flush_interval`` seconds
  or when a table buffer reaches ``flush_rows``.
- Low-level DDL replication is out of scope — users own their landing schema.
- Every change is kept, deletes included, in the CDC landing convention of
  :mod:`havn.engine.live.sources`: ``op`` (I/U/D), ``lsn`` (the WAL position,
  which orders the versions of a key), ``received_at``, ``payload`` (the new
  row, or for a delete the old key) and ``_havn_seq``. Each flush is a live
  source commit, so a bronze model with ``cdc_op=op, cdc_seq=lsn`` applies
  the changes within seconds.
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
        """Buffer one decoded change (insert, update or delete)."""
        op = _normalize_op(getattr(message, "op", None) or getattr(message, "type", None))
        table = _message_table(message)
        if not table or op is None:
            return
        if op == "D":
            # A delete carries the old row (or at least its replica identity).
            data = (
                getattr(message, "before", None) or getattr(message, "old", None)
                or getattr(message, "identity", None) or getattr(message, "data", None)
            )
        else:
            data = (
                getattr(message, "after", None) or getattr(message, "new", None)
                or getattr(message, "data", None)
            )
        if data is None:
            return
        table = self._match_table(table)
        if table is None:
            return
        self._buffers.setdefault(table, []).append({
            "op": op, "row": _plain(data), "ts": time.time(), "lsn": _message_lsn(message),
        })
        self.rows_consumed += 1

    def _match_table(self, table: str) -> str | None:
        """The configured name ``table`` matches (qualified or bare), or None."""
        if table in self.config.tables:
            return table
        short = table.split(".")[-1]
        for configured in self.config.tables:
            if configured.split(".")[-1] == short:
                return configured
        return None

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
                            lsn BIGINT,
                            received_at TIMESTAMP,
                            payload JSON
                        )
                        """
                    )
                    # Tables landed before the LSN was kept.
                    conn.execute(f"ALTER TABLE {target} ADD COLUMN IF NOT EXISTS lsn BIGINT")
                    rows_to_insert = [
                        (r["op"], r.get("lsn"), r["ts"], json.dumps(r["row"], default=str))
                        for r in rows
                    ]
                    conn.executemany(
                        f"INSERT INTO {target} (op, lsn, received_at, payload) "
                        f"VALUES (?, ?, to_timestamp(?), ?::JSON)",
                        rows_to_insert,
                    )
                    self._advance(conn, target)
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


    def _advance(self, conn: duckdb.DuckDBPyConnection, target: str) -> None:
        """Stamp the batch just written and tell the live runner."""
        try:
            from havn.engine.live.sources import advance_source

            advance_source(conn, target)
        except Exception as e:
            logger.warning("live advance for %s failed: %s", target, e)


_OPS = {"I": "I", "INSERT": "I", "C": "I", "R": "I", "U": "U", "UPDATE": "U", "D": "D", "DELETE": "D"}


def _normalize_op(op: object) -> str | None:
    """I / U / D for a decoded change, None for anything else (truncate, begin...)."""
    if op is None:
        return None
    return _OPS.get(str(getattr(op, "value", op)).strip().upper())


def _message_table(message) -> str | None:
    """``schema.table`` (or a bare name) from the shapes pypgoutput versions use."""
    for attr in ("table_name", "table"):
        value = getattr(message, attr, None)
        if isinstance(value, str) and value:
            return value
    schema = getattr(message, "table_schema", None)
    if schema is not None:
        name = getattr(schema, "table", None)
        ns = getattr(schema, "schema_name", None) or getattr(schema, "schema", None)
        if name:
            return f"{ns}.{name}" if ns else name
    return None


def _message_lsn(message) -> int | None:
    """The change's WAL position as an integer, which orders versions of a key."""
    for value in (
        getattr(message, "lsn", None),
        getattr(getattr(message, "transaction", None), "commit_lsn", None),
        getattr(getattr(message, "transaction", None), "begin_lsn", None),
    ):
        if value is None:
            continue
        if isinstance(value, int):
            return value
        text = str(value)
        if "/" in text:  # Postgres text form, e.g. 16/B374D848
            hi, lo = text.split("/", 1)
            try:
                return (int(hi, 16) << 32) + int(lo, 16)
            except ValueError:
                continue
        try:
            return int(text)
        except ValueError:
            continue
    return None


def _plain(data):
    """A JSON-ready dict from the pydantic models / mappings pypgoutput hands back."""
    if hasattr(data, "model_dump"):
        return data.model_dump()
    return data


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
