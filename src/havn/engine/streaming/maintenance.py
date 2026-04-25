"""DuckLake maintenance scheduler.

Periodic housekeeping for DuckLake-backed warehouses:

- **Flush** inlined rows to Parquet (every ~5-10 minutes).
- **Merge adjacent files** (every ~30 minutes) to keep file counts bounded.
- **Checkpoint + snapshot expiration** (daily) to reclaim storage.

On a DuckDB-backend warehouse every operation is a no-op — we short-circuit
without opening a connection.

The scheduler is a single daemon thread driven by ``time.monotonic()``.
Integrates with the existing Huey scheduler only loosely: it's simpler to run
an in-process loop than register three Huey tasks that also need DuckDB
access. Huey stays the job runner; this thread owns DuckLake housekeeping.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

import duckdb

logger = logging.getLogger("havn.streaming.maintenance")


@dataclass
class MaintenanceConfig:
    flush_interval_s: int = 600        # 10 minutes
    merge_interval_s: int = 1800       # 30 minutes
    checkpoint_interval_s: int = 86400  # 24 hours
    snapshot_retention_days: int = 7


class MaintenanceScheduler:
    def __init__(
        self,
        *,
        connection_factory: Callable[[], duckdb.DuckDBPyConnection],
        backend_name: str,
        config: MaintenanceConfig | None = None,
    ) -> None:
        self._factory = connection_factory
        self._backend_name = backend_name
        self._config = config or MaintenanceConfig()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last: dict[str, float] = {"flush": 0.0, "merge": 0.0, "checkpoint": 0.0}

    def start(self) -> None:
        if self._backend_name != "ducklake":
            logger.debug("maintenance disabled: backend=%s", self._backend_name)
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="havn-maintenance", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    # --- Loop --------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                if now - self._last["flush"] >= self._config.flush_interval_s:
                    self.flush()
                    self._last["flush"] = now
                if now - self._last["merge"] >= self._config.merge_interval_s:
                    self.merge()
                    self._last["merge"] = now
                if now - self._last["checkpoint"] >= self._config.checkpoint_interval_s:
                    self.checkpoint_and_expire()
                    self._last["checkpoint"] = now
            except Exception as e:
                logger.warning("maintenance tick failed: %s", e)
            self._stop.wait(30.0)

    # --- Individual operations --------------------------------------------

    def flush(self) -> None:
        """Call DuckLake's flush to move inlined rows to Parquet."""
        self._exec("CALL ducklake_flush('warehouse')", op="flush")

    def merge(self) -> None:
        """Compact adjacent small files into larger ones."""
        self._exec("CALL ducklake_merge_adjacent_files('warehouse')", op="merge")

    def checkpoint_and_expire(self) -> None:
        """Checkpoint the catalog and drop snapshots older than the retention window."""
        retention = self._config.snapshot_retention_days
        try:
            self._exec("CHECKPOINT", op="checkpoint")
        except Exception as e:
            logger.debug("CHECKPOINT failed: %s", e)
        # DuckLake exposes a function to expire snapshots older than N days.
        try:
            self._exec(
                f"CALL ducklake_expire_snapshots('warehouse', older_than => INTERVAL '{retention} days')",
                op="expire",
            )
        except Exception as e:
            logger.debug("snapshot expiration failed: %s", e)

    def _exec(self, sql: str, *, op: str) -> None:
        from havn.engine.resource_manager import get_resource_manager

        manager = get_resource_manager()
        conn = self._factory()
        try:
            with manager.acquire_sync("system", f"ducklake-{op}", conn=conn):
                conn.execute(sql)
        except duckdb.Error as e:
            logger.debug("ducklake %s returned %s", op, e)
        finally:
            # The factory may return a cursor on the shared write conn —
            # closing those is fine and required to release the cursor; the
            # underlying parent conn is owned by the WriteQueue and stays open.
            try:
                conn.close()
            except Exception:
                pass
