"""The governed read path: every read-only query a person asks for runs here.

``/api/query``, its CSV export, dashboard widgets (in the editor and on
published links) and scheduled reports all go through this module, so the
rules applied to a query do not depend on which surface sent it:

1. ``validate_read_only_query`` (``engine/sql_safety.py``): one read-only
   statement, checked by the splitter and by DuckDB's own parser.
2. Governance rewrites for the identity the query runs as. Today that is the
   column-masking rewriter (``engine/masking_rewriter.py``). New rewriting
   passes, such as row-level security, belong in :func:`prepare_governed_sql`
   so every caller picks them up without changes of its own.
3. The query governor (``engine/query_governor.py``): a per-role timeout,
   enforced with ``conn.interrupt()``, and a resource-manager ``query`` slot.
4. Post-query masking for the policies the rewriter could not apply in SQL.

The identity is explicit (:class:`QueryIdentity`) rather than read from a
request, because a published dashboard runs as its link's "view as"
identity and a scheduled report runs as its owner, neither of whom is the
HTTP caller.

Callers pass a read cursor (the server's read pool, or any connection the
CLI or scheduler holds). Errors come back as :class:`GovernedQueryError`
with an HTTP-style ``status_code`` so routes can translate them directly.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import duckdb

logger = logging.getLogger("havn.engine.governed_query")

# Cap applied when neither the SQL nor the caller sets a limit, so DuckDB
# never buffers millions of rows for a browser.
SERVER_ROW_CAP = 50_000


@dataclass(frozen=True)
class QueryIdentity:
    """Who a query runs as.

    ``username`` is the havn user (or a synthetic name such as
    ``share:<id>`` for a public link that views as a role); ``role`` decides
    masking exemptions and the timeout. ``source`` says which surface ran
    the query (``query``, ``dashboard``, ``share``, ``report``) and is used
    only for logging and audit detail.
    """

    username: str
    role: str
    source: str = "query"

    @classmethod
    def from_user(cls, user: dict, source: str = "query") -> "QueryIdentity":
        return cls(
            username=str(user.get("username") or "anonymous"),
            role=str(user.get("role") or "viewer"),
            source=source,
        )

    def cache_key(self) -> str:
        """Key for caching results per identity.

        Includes the username, not only the role: once row-level rules depend
        on who is asking, two viewers with the same role can see different
        rows and must not share a cache entry.
        """
        return f"{self.role}:{self.username}"


class GovernedQueryError(Exception):
    """A query was refused or failed. ``status_code`` follows HTTP."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class PreparedQuery:
    """SQL after validation and governance rewrites, plus what post-query masking still owes."""

    original_sql: str
    sql: str
    rewrite_ok: bool
    handled_policy_ids: set[str] = field(default_factory=set)


def prepare_governed_sql(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    identity: QueryIdentity,
) -> PreparedQuery:
    """Validate ``sql`` as read-only and apply the governance rewrites for ``identity``.

    Raises :class:`GovernedQueryError` (400/403) when the SQL is not a
    single read-only statement or reads a masked column in a way masking
    cannot follow.
    """
    from havn.engine.masking_rewriter import (
        MaskedColumnAccessError,
        rewrite_query_with_masking,
    )
    from havn.engine.sql_safety import ReadOnlyQueryError, validate_read_only_query

    try:
        validate_read_only_query(sql)
    except ReadOnlyQueryError as e:
        raise GovernedQueryError(e.status_code, str(e)) from e

    try:
        rewritten, rewrite_ok, handled = rewrite_query_with_masking(sql, identity.role, conn)
    except MaskedColumnAccessError as e:
        raise GovernedQueryError(403, str(e)) from e

    final_sql = rewritten if rewrite_ok else sql
    if final_sql != sql:
        # Validate what actually runs, not only what was sent.
        try:
            validate_read_only_query(final_sql)
        except ReadOnlyQueryError as e:
            raise GovernedQueryError(e.status_code, str(e)) from e
    return PreparedQuery(
        original_sql=sql,
        sql=final_sql,
        rewrite_ok=rewrite_ok,
        handled_policy_ids=set(handled or ()),
    )


def apply_post_query_governance(
    prepared: PreparedQuery,
    columns: list[str],
    rows: list[list],
    identity: QueryIdentity,
    conn: duckdb.DuckDBPyConnection,
) -> list[list]:
    """Apply what the SQL rewrite could not: post-query masking by column name."""
    from havn.engine.masking import apply_masking

    if not prepared.rewrite_ok:
        return apply_masking(columns, rows, identity.role, conn)
    if prepared.handled_policy_ids:
        # Conditional policies and methods without a SQL form still need the
        # post-query pass; skip the ones the rewrite already applied.
        return apply_masking(
            columns, rows, identity.role, conn,
            skip_policy_ids=prepared.handled_policy_ids,
        )
    return rows


def _serialize(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


def _wrap_for_paging(sql: str, limit: int | None, offset: int, row_cap: int | None) -> tuple[str, int | None]:
    """Return (sql to run, effective limit) with paging and the server cap applied."""
    # Trailing newline before the closing paren so a query that ends with a
    # `-- line comment` can't swallow the wrapper.
    sql_clean = sql.strip().rstrip(";") + "\n"
    has_limit = bool(re.search(r"\bLIMIT\b", sql, re.IGNORECASE))
    if offset > 0 and limit is not None:
        return f"SELECT * FROM ({sql_clean}) AS _q OFFSET {int(offset)} LIMIT {int(limit)}", limit
    if limit is not None:
        return f"SELECT * FROM ({sql_clean}) AS _q LIMIT {int(limit)}", limit
    if not has_limit and row_cap:
        # Keep the caller's offset: dropping it here re-served page 1 to
        # clients paginating without an explicit limit.
        offset_clause = f"OFFSET {int(offset)} " if offset > 0 else ""
        return f"SELECT * FROM ({sql_clean}) AS _q {offset_clause}LIMIT {int(row_cap)}", row_cap
    if offset > 0:
        return f"SELECT * FROM ({sql_clean}) AS _q OFFSET {int(offset)}", None
    return sql, None


def run_governed_query(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    identity: QueryIdentity,
    *,
    params: dict | list | None = None,
    limit: int | None = None,
    offset: int = 0,
    timeout_s: float | None = None,
    row_cap: int | None = SERVER_ROW_CAP,
    task_label: str | None = None,
) -> dict:
    """Run a read-only query as ``identity`` and return a JSON-ready result.

    Returns ``{columns, column_types, rows, row_count, truncated, offset,
    limit, duration_ms}``. ``params`` binds ``$name`` (dict) or ``$1``/``?``
    (list) placeholders; values are never spliced into the SQL.

    Raises :class:`GovernedQueryError`: 400 for invalid or failing SQL,
    403 for masked-column access, 408 on timeout.
    """
    from havn.engine.query_governor import get_timeout_for_role
    from havn.engine.resource_manager import current_task, get_resource_manager
    from havn.engine.sql_safety import ReadOnlyQueryError, validate_read_only_query

    prepared = prepare_governed_sql(conn, sql, identity)
    wrapped, effective_limit = _wrap_for_paging(prepared.sql, limit, offset, row_cap)
    if wrapped != prepared.sql:
        try:
            validate_read_only_query(wrapped)
        except ReadOnlyQueryError as e:
            raise GovernedQueryError(e.status_code, str(e)) from e

    query_timeout = timeout_s if timeout_s is not None else get_timeout_for_role(identity.role)
    holder: dict = {}
    errors: list[BaseException] = []

    def _exec() -> None:
        try:
            result = conn.execute(wrapped, params)
            description = result.description or []
            columns = [d[0] for d in description]
            column_types = [str(d[1]) for d in description]
            if effective_limit is not None:
                rows = result.fetchmany(effective_limit)
            else:
                rows = result.fetchall()
            holder["columns"] = columns
            holder["column_types"] = column_types
            holder["rows"] = rows
        except BaseException as e:  # noqa: BLE001 - re-raised on the caller's thread
            errors.append(e)

    label = task_label or f"sql:{sql[:60]}"
    manager = get_resource_manager()
    t_start = time.monotonic()
    try:
        with manager.acquire_sync("query", label, conn=conn):
            task = current_task()
            if task is not None:
                manager.register_cancel(task.task_id, conn.interrupt)
            thread = threading.Thread(target=_exec, daemon=True, name="havn-governed-query")
            thread.start()
            thread.join(timeout=query_timeout)
            if thread.is_alive():
                try:
                    conn.interrupt()
                except Exception:
                    pass
                raise GovernedQueryError(
                    408,
                    f"Query exceeded {query_timeout}s timeout. "
                    f"Try adding filters or a LIMIT clause.",
                )
    except GovernedQueryError:
        raise
    except duckdb.InterruptException as e:
        raise GovernedQueryError(408, f"Query was cancelled: {e}") from e
    duration_ms = int((time.monotonic() - t_start) * 1000)

    if errors:
        err = errors[0]
        if isinstance(err, ReadOnlyQueryError):
            raise GovernedQueryError(err.status_code, str(err)) from err
        if isinstance(err, GovernedQueryError):
            raise err
        raise GovernedQueryError(400, str(err)) from err

    columns = holder["columns"]
    rows = [[_serialize(v) for v in row] for row in holder["rows"]]
    rows = apply_post_query_governance(prepared, columns, rows, identity, conn)
    return {
        "columns": columns,
        "column_types": holder["column_types"],
        "rows": rows,
        "row_count": len(rows),
        "truncated": effective_limit is not None and len(rows) == effective_limit,
        "offset": offset,
        "limit": effective_limit,
        "duration_ms": duration_ms,
    }
