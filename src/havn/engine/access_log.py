"""Data-access logging: who read which tables and PII columns, and when.

The audit log (``engine/audit.py``) records *that* a query ran, with the SQL
text. It cannot answer "who read personal-data column X between two dates",
because it never resolves the SQL to the relations and columns it touched, and
its only read surface records raw, truncated SQL.

This module records the resolved access. Every read that goes through the
governed read path (``engine/governed_query.run_governed_query``) can write one
row here carrying the relations read, the PII-classified columns in scope, the
columns a masking policy rewrote, the row-filtered tables, and the identity and
timing. The governance rewriter already computes all of this to enforce policy;
``GovernedQuery`` now carries it out so the read path can persist it.

Scope and honesty:

- The detailed relation/column fields are populated for identities that
  governance actually evaluates (anyone a masking or row policy could apply
  to). An admin with no policies is short-circuited by the rewriter for speed,
  so an admin read is recorded as who/what/when with empty relation lists. The
  existing audit log still records the SQL for those.
- A row is written only when the caller passes a writable connection, so the
  surfaces that opt in (``/api/query``, its CSV export, published links) are
  covered and other callers are unaffected until wired.
"""

from __future__ import annotations

import json
import logging
import weakref

import duckdb

logger = logging.getLogger("havn.access_log")

# Connections whose access_log table is known to exist, so the DDL runs once
# per connection rather than on every governed read. WeakSet so closed
# connections drop out on their own.
try:
    _ensured: "weakref.WeakSet[duckdb.DuckDBPyConnection]" = weakref.WeakSet()
except TypeError:  # pragma: no cover - if connections are not weak-referenceable
    _ensured = None  # type: ignore[assignment]

# Statement text is truncated to keep the log small; the resolved relations are
# the durable record, not the SQL.
_STATEMENT_CAP = 2000


def ensure_access_log_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Create ``_havn.access_log`` if it does not exist.

    A UUID id (not a sequence) so the same DDL works on DuckDB and DuckLake.
    The relation/column sets are JSON arrays of ``schema.table`` /
    ``schema.table.column`` strings. The table is ensured once per connection
    (it is also created at startup by ``ensure_meta_table``).
    """
    if _ensured is not None:
        try:
            if conn in _ensured:
                return
        except TypeError:
            pass
    conn.execute("CREATE SCHEMA IF NOT EXISTS _havn")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _havn.access_log (
            id                  VARCHAR DEFAULT gen_random_uuid()::VARCHAR,
            "user"              VARCHAR NOT NULL,
            role                VARCHAR,
            source              VARCHAR,
            statement           VARCHAR,
            tables              JSON DEFAULT '[]',
            pii_columns         JSON DEFAULT '[]',
            masked_columns      JSON DEFAULT '[]',
            row_filtered_tables JSON DEFAULT '[]',
            policy_count        INTEGER DEFAULT 0,
            row_count           BIGINT DEFAULT 0,
            duration_ms         BIGINT DEFAULT 0,
            status              VARCHAR DEFAULT 'ok',
            ip_address          VARCHAR,
            "timestamp"         TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    if _ensured is not None:
        try:
            _ensured.add(conn)
        except TypeError:
            pass


def log_access(
    conn: duckdb.DuckDBPyConnection,
    *,
    user: str,
    role: str | None = None,
    source: str | None = None,
    statement: str | None = None,
    read_tables: list[str] | None = None,
    pii_columns: list[str] | None = None,
    masked_columns: list[str] | None = None,
    row_filtered_tables: list[str] | None = None,
    policy_count: int = 0,
    row_count: int = 0,
    duration_ms: int = 0,
    status: str = "ok",
    ip_address: str | None = None,
) -> None:
    """Write one data-access entry. Best-effort: never raises to the caller."""
    try:
        ensure_access_log_table(conn)
        conn.execute(
            """
            INSERT INTO _havn.access_log
                ("user", role, source, statement, tables, pii_columns,
                 masked_columns, row_filtered_tables, policy_count, row_count,
                 duration_ms, status, ip_address)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                user,
                role,
                source,
                (statement or "")[:_STATEMENT_CAP] or None,
                json.dumps(sorted(read_tables or [])),
                json.dumps(sorted(pii_columns or [])),
                json.dumps(sorted(masked_columns or [])),
                json.dumps(sorted(row_filtered_tables or [])),
                int(policy_count or 0),
                int(row_count or 0),
                int(duration_ms or 0),
                status,
                ip_address,
            ],
        )
    except Exception:
        # A read must never fail because its access record could not be
        # written. The miss is logged for the operator, not surfaced.
        logger.debug("Failed to write access log entry", exc_info=True)


def query_access_log(
    conn: duckdb.DuckDBPyConnection,
    *,
    limit: int = 100,
    user: str | None = None,
    table: str | None = None,
    pii_column: str | None = None,
    source: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Return access-log entries, newest first, with optional filters.

    ``table`` and ``pii_column`` match a substring of the recorded relation
    and PII-column arrays, so "who read ``gold.customers.email``" is a
    ``pii_column='gold.customers.email'`` query. ``since`` / ``until`` are
    timestamps (any form DuckDB parses, e.g. ``2026-10-01`` or
    ``2026-10-01 09:00:00``) and are the date range the audit log never had.
    """
    ensure_access_log_table(conn)

    conditions: list[str] = []
    params: list = []
    if user is not None:
        conditions.append('"user" = ?')
        params.append(user)
    if source is not None:
        conditions.append("source = ?")
        params.append(source)
    if table is not None:
        conditions.append("CAST(tables AS VARCHAR) ILIKE ?")
        params.append(f"%{table}%")
    if pii_column is not None:
        conditions.append("CAST(pii_columns AS VARCHAR) ILIKE ?")
        params.append(f"%{pii_column}%")
    if since is not None:
        conditions.append('"timestamp" >= ?')
        params.append(since)
    if until is not None:
        conditions.append('"timestamp" <= ?')
        params.append(until)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(max(1, min(limit, 10000)))

    rows = conn.execute(
        f"""
        SELECT id, "user", role, source, statement, tables, pii_columns,
               masked_columns, row_filtered_tables, policy_count, row_count,
               duration_ms, status, ip_address, "timestamp"
        FROM _havn.access_log
        {where}
        ORDER BY "timestamp" DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

    def _arr(v) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            try:
                return list(json.loads(v))
            except (ValueError, TypeError):
                return []
        return list(v)

    return [
        {
            "id": r[0],
            "user": r[1],
            "role": r[2],
            "source": r[3],
            "statement": r[4],
            "tables": _arr(r[5]),
            "pii_columns": _arr(r[6]),
            "masked_columns": _arr(r[7]),
            "row_filtered_tables": _arr(r[8]),
            "policy_count": r[9],
            "row_count": r[10],
            "duration_ms": r[11],
            "status": r[12],
            "ip_address": r[13],
            "timestamp": str(r[14]) if r[14] else None,
        }
        for r in rows
    ]
