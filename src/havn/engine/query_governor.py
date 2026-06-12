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
    params: dict | None = None,
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
    params : optional named parameters bound to ``$name`` placeholders

    Returns
    -------
    (result, duration_ms) where result is the DuckDB result object.

    Raises
    ------
    QueryTimeoutError
        If the query exceeds the timeout.
    """
    result_holder: list = []
    error_holder: list = []

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
        raise QueryTimeoutError(
            f"Query exceeded {timeout_s}s timeout and was cancelled. "
            f"Try adding filters or a LIMIT clause."
        )

    if error_holder:
        raise error_holder[0]

    return result_holder[0], duration_ms


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
