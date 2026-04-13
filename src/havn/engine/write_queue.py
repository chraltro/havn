"""Write queue and read pool for DuckDB connection management.

DuckDB allows one write connection at a time and unlimited concurrent
readers.  This module provides:

- **WriteQueue**: Serial write executor.  All mutations go through a
  single dedicated connection on a background thread, preventing
  concurrent write contention.
- **ReadPool**: Pool of read-only connections for concurrent reads
  (Linux/Mac only; Windows falls back to cursors from the write
  connection because DuckDB on Windows allows only one connection
  per file).
"""

from __future__ import annotations

import concurrent.futures
import logging
import queue
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

import duckdb

from havn.engine.backends.base import WarehouseBackend

logger = logging.getLogger("havn.server")


class WriteQueueFullError(Exception):
    """Raised when the write queue is at capacity."""
    pass


class WriteQueue:
    """Serial write executor.  All mutations go through here.

    The queue has a bounded depth (``maxsize``).  When full, ``submit``
    waits briefly before raising :class:`WriteQueueFullError` so the
    caller gets a clear "server busy" signal instead of hanging.
    """

    def __init__(
        self,
        backend: WarehouseBackend,
        maxsize: int = 50,
    ):
        self._backend = backend
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._conn = backend.connect(read_only=False)
        self._thread = threading.Thread(target=self._run, daemon=True, name="havn-write-queue")
        self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            func, args, kwargs, future = item
            try:
                result = func(self._conn, *args, **kwargs)
                future.set_result(result)
            except Exception as e:
                future.set_exception(e)
            finally:
                self._queue.task_done()

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        """The underlying write connection (for init/setup only)."""
        return self._conn

    def submit(
        self,
        func: Any,
        *args: Any,
        _queue_timeout: float = 5.0,
        **kwargs: Any,
    ) -> concurrent.futures.Future:
        """Submit a write operation to the queue.

        Parameters
        ----------
        func : callable(conn, *args, **kwargs)
            The function to execute.  Receives the write connection as
            the first argument.
        _queue_timeout : seconds to wait for queue space before raising
            WriteQueueFullError.

        Returns a Future whose ``.result(timeout=...)`` delivers the
        return value.
        """
        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            self._queue.put((func, args, kwargs, future), timeout=_queue_timeout)
        except queue.Full:
            raise WriteQueueFullError(
                "Write queue is full. A long-running operation is in progress. "
                "Try again in a few seconds."
            )
        return future

    def execute(self, sql: str, params: list | None = None, timeout: float = 30) -> Any:
        """Convenience: submit a single SQL statement and wait for the result."""
        def _exec(conn: duckdb.DuckDBPyConnection, sql: str, params: list | None) -> Any:
            return conn.execute(sql, params or [])
        return self.submit(_exec, sql, params).result(timeout=timeout)

    def cursor(self) -> duckdb.DuckDBPyConnection:
        """Return a cursor from the write connection.

        This is the **compatibility bridge**: existing route handlers that
        receive a ``DbConn`` cursor continue to work without changes.
        The cursor serializes through DuckDB's internal locking; the
        WriteQueue adds an explicit serialization layer on top for
        operations that go through ``submit()``.
        """
        return self._conn.cursor()

    def close(self) -> None:
        """Shut down the queue and close the connection."""
        self._queue.put(None)
        self._thread.join(timeout=5)
        try:
            self._conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Read pool
# ---------------------------------------------------------------------------


class ReadPool:
    """Pool of read-only DuckDB connections (Linux/Mac).

    DuckDB supports unlimited concurrent readers.  This pool keeps a
    fixed number of read-only connections (from the backend) and lends
    them out via a context manager.
    """

    def __init__(self, backend: WarehouseBackend, pool_size: int = 4):
        self._backend = backend
        self._pool: queue.Queue = queue.Queue()
        for _ in range(pool_size):
            self._pool.put(backend.connect(read_only=True))

    @contextmanager
    def connection(self) -> Generator[duckdb.DuckDBPyConnection, None, None]:
        conn = self._pool.get()
        try:
            cursor = conn.cursor()
            try:
                yield cursor
            finally:
                cursor.close()
        finally:
            self._pool.put(conn)

    def close(self) -> None:
        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
                conn.close()
            except queue.Empty:
                break


class SharedConnPool:
    """Fallback pool for Windows: cursors from the shared write connection."""

    def __init__(self, shared_conn: duckdb.DuckDBPyConnection):
        self._conn = shared_conn

    @contextmanager
    def connection(self) -> Generator[duckdb.DuckDBPyConnection, None, None]:
        cursor = self._conn.cursor()
        try:
            yield cursor
        finally:
            cursor.close()

    def close(self) -> None:
        pass  # shared conn is owned by the WriteQueue


def create_read_pool(
    backend: WarehouseBackend,
    shared_conn: duckdb.DuckDBPyConnection,
    pool_size: int = 4,
) -> ReadPool | SharedConnPool:
    """Create the appropriate read pool for the current platform and backend.

    The Windows single-writer-per-file constraint only applies to the DuckDB
    file backend. DuckLake opens a fresh in-memory DuckDB per connection and
    ATTACHes the catalog, so multi-connection reads work on every platform.
    """
    if sys.platform == "win32" and backend.name == "duckdb":
        logger.info("Windows + duckdb backend: using shared connection for reads")
        return SharedConnPool(shared_conn)
    try:
        pool = ReadPool(backend, pool_size)
        logger.info("Read pool: %d read-only connections", pool_size)
        return pool
    except Exception as e:
        logger.warning("Failed to create read pool, falling back to shared connection: %s", e)
        return SharedConnPool(shared_conn)
