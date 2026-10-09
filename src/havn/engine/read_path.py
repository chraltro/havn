"""The governed read path: one function that runs a read-only query for a user.

``POST /api/query`` was the only place that put a query through every guard a
read needs: the read-only validator, the pre-query masking rewrite, a row cap,
a per-role timeout, and the post-query masking backstop. Surfaces that run SQL
havn generated on someone's behalf (the semantic layer, ``havn ask``) need the
exact same treatment, and a copy per surface drifts the moment one of them
gains a new guard. So the sequence lives here and every caller goes through
:func:`run_read_query`.

Anything that narrows what a user may read (masking today, row-level policies
tomorrow) belongs inside this function, not in a route. Callers only map the
exceptions to their own error shape.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import duckdb

from havn.engine.masking import apply_masking
from havn.engine.masking_rewriter import (
    MaskedColumnAccessError,
    rewrite_query_with_masking,
)
from havn.engine.query_governor import QueryTimeoutError, get_timeout_for_role
from havn.engine.sql_safety import ReadOnlyQueryError, validate_read_only_query

logger = logging.getLogger("havn.engine.read_path")

#: Rows returned when neither the SQL nor the caller asks for a limit.
SERVER_ROW_CAP = 50_000

__all__ = [
    "MaskedColumnAccessError",
    "QueryTimeoutError",
    "ReadOnlyQueryError",
    "ReadResult",
    "SERVER_ROW_CAP",
    "run_read_query",
]


def _serialize(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


@dataclass
class ReadResult:
    """What a governed read returns; ``to_dict`` is the /api/query body."""

    columns: list[str]
    column_types: list[str]
    rows: list[list[Any]]
    truncated: bool
    offset: int = 0
    limit: int | None = None
    duration_ms: int = 0
    masked: bool = False
    executed_sql: str = field(default="", repr=False)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def to_dict(self) -> dict:
        return {
            "columns": self.columns,
            "column_types": self.column_types,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "offset": self.offset,
            "limit": self.limit,
        }


def _wrap_with_limit(sql: str, limit: int | None, offset: int) -> tuple[str, int | None]:
    """The statement that actually runs, plus the limit it enforces."""
    # Trailing newline before the closing paren so a query that ends with a
    # `-- line comment` can't swallow the wrapper.
    sql_clean = sql.strip().rstrip(";") + "\n"
    has_limit = bool(re.search(r"\bLIMIT\b", sql, re.IGNORECASE))
    if offset > 0 and limit is not None:
        return f"SELECT * FROM ({sql_clean}) AS _q OFFSET {offset} LIMIT {limit}", limit
    if limit is not None:
        return f"SELECT * FROM ({sql_clean}) AS _q LIMIT {limit}", limit
    if not has_limit:
        # No limit anywhere: inject the server cap into the SQL itself so
        # DuckDB can stop scanning early. Keep the caller's offset: dropping
        # it here silently re-served page 1 to clients paginating without an
        # explicit limit.
        offset_clause = f"OFFSET {offset} " if offset > 0 else ""
        return (
            f"SELECT * FROM ({sql_clean}) AS _q {offset_clause}LIMIT {SERVER_ROW_CAP}",
            SERVER_ROW_CAP,
        )
    if offset > 0:
        return f"SELECT * FROM ({sql_clean}) AS _q OFFSET {offset}", None
    return sql, None


def run_read_query(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    *,
    user: dict,
    params: dict | None = None,
    limit: int | None = None,
    offset: int = 0,
    timeout: float | None = None,
    task_label: str | None = None,
) -> ReadResult:
    """Run ``sql`` as ``user`` through every read guard havn has.

    Args:
        conn: A read cursor on the warehouse.
        sql: The query. Must be a single read-only statement.
        user: ``{"username": ..., "role": ...}``; the role drives masking and
            the default timeout.
        params: Named values bound to ``$name`` placeholders.
        limit, offset: Paging. Without a limit (here or in the SQL) the
            result is capped at :data:`SERVER_ROW_CAP`.
        timeout: Seconds before the query is interrupted. Defaults to the
            per-role timeout.
        task_label: Name shown in the resource manager's active-task list.

    Raises:
        ReadOnlyQueryError: the SQL is not a safe read (``status_code`` set).
        MaskedColumnAccessError: the query touches a column the role may not
            read even masked.
        QueryTimeoutError: the query ran past its timeout and was interrupted.
        Exception: whatever DuckDB raised while running it.
    """
    role = str(user.get("role") or "viewer")
    validate_read_only_query(sql)

    # Pre-query masking: rewrite the SQL so masked columns are masked at their
    # source, before any expression can reshape them.
    rewritten_sql, rewrite_ok, handled_ids = rewrite_query_with_masking(sql, role, conn)
    sql_to_execute = rewritten_sql if rewrite_ok else sql

    wrapped, effective_limit = _wrap_with_limit(sql_to_execute, limit, offset)
    # Validate what actually runs, not only what was sent: the wrapper and the
    # masking rewrite both change the text.
    if wrapped != sql:
        validate_read_only_query(wrapped)

    holder: dict = {}
    errors: list[Exception] = []

    def _execute() -> None:
        try:
            cur = conn.execute(wrapped, params)
            description = cur.description or []
            columns = [d[0] for d in description]
            column_types = [str(d[1]) for d in description]
            if not description:
                rows: list = []
            elif effective_limit is not None:
                rows = cur.fetchmany(effective_limit)
            else:
                rows = cur.fetchall()
            holder["data"] = (columns, column_types, rows)
        except Exception as e:  # surfaced to the caller below
            errors.append(e)

    query_timeout = timeout if timeout is not None else get_timeout_for_role(role)
    started = time.monotonic()

    # Acquire a resource-manager slot so the query shows up in the UI's
    # active-task list and counts toward the `query` concurrency budget.
    from havn.engine.resource_manager import current_task, get_resource_manager

    manager = get_resource_manager()
    with manager.acquire_sync("query", task_label or f"sql:{sql[:60]}", conn=conn):
        task = current_task()
        if task is not None:
            manager.register_cancel(task.task_id, conn.interrupt)
        thread = threading.Thread(target=_execute, daemon=True)
        thread.start()
        thread.join(timeout=query_timeout)
        if thread.is_alive():
            try:
                conn.interrupt()
            except Exception:
                pass
            raise QueryTimeoutError(
                f"Query exceeded {query_timeout}s timeout. "
                "Try adding filters or a LIMIT clause."
            )
    duration_ms = int((time.monotonic() - started) * 1000)

    if errors:
        raise errors[0]
    columns, column_types, raw_rows = holder["data"]
    rows = [[_serialize(v) for v in row] for row in raw_rows]

    # Post-query masking backstop: if the rewrite could not be applied (parse
    # failure, unsupported method, a reference it could not resolve), mask the
    # result set. When it was applied, still run the policies it did not
    # handle (conditional ones, unsupported methods).
    masked = False
    if not rewrite_ok:
        rows = apply_masking(columns, rows, role, conn)
        masked = True
    elif handled_ids:
        rows = apply_masking(columns, rows, role, conn, skip_policy_ids=handled_ids)
        masked = True

    return ReadResult(
        columns=columns,
        column_types=column_types,
        rows=[list(r) for r in rows],
        truncated=effective_limit is not None and len(rows) == effective_limit,
        offset=offset,
        limit=effective_limit,
        duration_ms=duration_ms,
        masked=masked or bool(handled_ids),
        executed_sql=wrapped,
    )
