"""The governed read path: one function that runs a read-only query for a user.

``POST /api/query`` was the only place that put a query through every guard a
read needs: the read-only validator, the pre-query masking rewrite, a row cap,
a per-role timeout, and the post-query masking backstop. Surfaces that run SQL
havn generated on someone's behalf (the semantic layer, ``havn ask``) need the
exact same treatment, and a copy per surface drifts the moment one of them
gains a new guard. So the sequence lives here and every caller goes through
:func:`run_read_query`.

The guards themselves live in :mod:`havn.engine.governed_query` (shared with
dashboards, published links and reports); this module is a thin wrapper. Callers only map the
exceptions to their own error shape.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import duckdb

from havn.engine.masking_rewriter import MaskedColumnAccessError
from havn.engine.query_governor import QueryTimeoutError
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
    # One governed path for every read surface: run_governed_query applies
    # read-only validation, masking and row policies (explicit and inherited
    # through lineage), the row cap, the per-role timeout and the post-query
    # masking pass. This wrapper keeps the ReadResult shape and the exception
    # types the semantic layer and `havn ask` expect.
    from havn.engine.governed_query import (
        GovernedQueryError,
        QueryIdentity,
        run_governed_query,
    )

    try:
        data = run_governed_query(
            conn,
            sql,
            QueryIdentity.from_user(user, source=task_label or "read"),
            params=params,
            limit=limit,
            offset=offset,
            timeout_s=timeout,
            row_cap=SERVER_ROW_CAP,
            task_label=task_label,
        )
    except GovernedQueryError as e:
        cause = e.__cause__
        if isinstance(cause, (ReadOnlyQueryError, MaskedColumnAccessError)):
            raise cause from None
        if e.status_code == 408:
            raise QueryTimeoutError(str(e)) from None
        if e.status_code == 403:
            raise MaskedColumnAccessError(str(e)) from None
        raise RuntimeError(str(e)) from None

    return ReadResult(
        columns=data["columns"],
        column_types=data["column_types"],
        rows=[list(r) for r in data["rows"]],
        truncated=data["truncated"],
        offset=data["offset"],
        limit=data["limit"],
        duration_ms=data.get("duration_ms", 0),
    )
