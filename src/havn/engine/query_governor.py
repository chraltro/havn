"""Query governor: timeout enforcement via DuckDB interrupt().

Wraps query execution with a watchdog timer that calls
``conn.interrupt()`` if the query exceeds its time limit.
"""

from __future__ import annotations

import logging
import threading
import time
import duckdb

logger = logging.getLogger("havn.engine.query_governor")


class QueryTimeoutError(Exception):
    """Raised when a query exceeds its time limit."""
    pass


def execute_governed(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    timeout_s: float = 30,
    params: dict | list | None = None,
) -> tuple[duckdb.DuckDBPyConnection, float]:
    """Execute SQL with a watchdog timer.

    Spawns the query in a thread and waits up to ``timeout_s`` seconds.
    If the query exceeds the timeout, calls ``conn.interrupt()`` from
    the watchdog thread, which raises an InterruptException in the
    executing thread.

    Parameters
    ----------
    conn : DuckDB connection or cursor
    sql : SQL to execute
    timeout_s : maximum seconds before interrupt
    params : optional parameters -- a dict bound to ``$name`` placeholders, or
        a list bound to positional ``$1``/``?`` placeholders

    Returns
    -------
    (result, duration_ms) where result is the DuckDB result object.

    Raises
    ------
    QueryTimeoutError
        If the query exceeds the timeout.
    """
    from havn.engine.governed_query import read_only_execution

    result_holder: list = []
    error_holder: list = []
    # Every governed read runs locked down (READ ONLY transaction, no Python
    # replacement scans), here rather than in each route, so no caller can
    # skip it. The guard stays open while the caller reads the result and
    # closes when the result is exhausted or closed.
    guard = read_only_execution(conn)
    guard.__enter__()

    def _run():
        try:
            result_holder.append(conn.execute(sql, params))
        except Exception as e:
            error_holder.append(e)

    t_start = time.monotonic()
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout_s)

    duration_ms = (time.monotonic() - t_start) * 1000

    if thread.is_alive():
        try:
            conn.interrupt()
        except Exception:
            pass
        thread.join(timeout=5)
        guard.__exit__(None, None, None)
        raise QueryTimeoutError(
            f"Query exceeded {timeout_s}s timeout and was cancelled. "
            f"Try adding filters or a LIMIT clause."
        )

    if error_holder:
        guard.__exit__(None, None, None)
        raise error_holder[0]

    return GovernedResult(result_holder[0], lambda: guard.__exit__(None, None, None)), duration_ms


class GovernedResult:
    """A query result whose read-only guard is released once it has been read.

    Proxies ``description``, ``fetchall``, ``fetchmany`` and ``fetchone``. The
    guard (the READ ONLY transaction) ends when a fetch comes back short or
    empty, on ``close()``, or when the object is collected.
    """

    def __init__(self, result, release) -> None:
        self._result = result
        self._release = release
        self._closed = False

    @property
    def description(self):
        return self._result.description

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._release()
            except Exception:
                logger.debug("Could not release a governed read", exc_info=True)

    def fetchall(self):
        try:
            return self._result.fetchall()
        finally:
            self.close()

    def fetchmany(self, size: int = 1):
        try:
            rows = self._result.fetchmany(size)
        except BaseException:
            self.close()
            raise
        if len(rows) < size:
            self.close()
        return rows

    def fetchone(self):
        try:
            row = self._result.fetchone()
        except BaseException:
            self.close()
            raise
        if row is None:
            self.close()
        return row

    def __getattr__(self, name):
        return getattr(self._result, name)

    def __del__(self):  # pragma: no cover - best effort
        try:
            self.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Role-based timeout resolution
# ---------------------------------------------------------------------------

# Default per-role timeouts (seconds).  Can be overridden in project.yml.
DEFAULT_ROLE_TIMEOUTS: dict[str, int] = {
    "admin": 300,   # 5 minutes
    "editor": 120,  # 2 minutes
    "viewer": 60,   # 1 minute
}


def get_timeout_for_role(
    role: str,
    config_timeouts: dict[str, int] | None = None,
) -> int:
    """Resolve the query timeout for a user role.

    Uses config overrides if provided, otherwise falls back to defaults.
    """
    timeouts = config_timeouts or DEFAULT_ROLE_TIMEOUTS
    return timeouts.get(role, timeouts.get("viewer", 60))
