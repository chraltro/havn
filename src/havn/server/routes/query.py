"""SQL query execution, table browsing, and autocomplete endpoints."""

from __future__ import annotations

import logging
import re
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from havn.server.deps import (
    DbConn,
    DbConnReadOnly,
    _require_permission,
    _serialize,
    _validate_identifier,
)
from havn.engine.masking import apply_masking, list_policies, create_policy, delete_policy
from havn.engine.masking_rewriter import rewrite_query_with_masking, MaskedColumnAccessError

logger = logging.getLogger("havn.server")

router = APIRouter()

# --- Constants ---

_SLOW_QUERY_THRESHOLD_MS = 5000


# --- SQL safety validation ---

_FORBIDDEN_STATEMENT_KEYWORDS = frozenset({
    "insert", "update", "delete", "drop", "create", "alter", "truncate", "merge",
    "copy", "attach", "detach", "install", "load", "export", "import",
    "grant", "revoke", "set", "reset", "vacuum", "checkpoint", "pragma",
    "call", "execute",
})

_DANGEROUS_FUNCTION_NAMES = frozenset({
    "read_csv_auto", "read_csv", "read_parquet", "read_json_auto", "read_json",
    "read_json_objects", "read_ndjson", "read_ndjson_auto",
    "read_blob", "read_text",
    "write_csv", "write_parquet",
    "iceberg_scan", "delta_scan", "parquet_scan", "csv_scan",
    "http_get", "http_post",
})


def _strip_sql_comments_and_strings(sql: str) -> str:
    """Remove string literals and comments so keyword/function scans cannot
    be fooled by content inside quotes or comments."""
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        nx = sql[i + 1] if i + 1 < n else ""
        if c == "-" and nx == "-":
            j = sql.find("\n", i)
            if j < 0:
                break
            i = j + 1
            continue
        if c == "/" and nx == "*":
            j = sql.find("*/", i + 2)
            if j < 0:
                break
            i = j + 2
            continue
        if c == "'":
            i += 1
            while i < n:
                if sql[i] == "'" and (i + 1 < n and sql[i + 1] == "'"):
                    i += 2
                    continue
                if sql[i] == "'":
                    i += 1
                    break
                i += 1
            out.append("''")
            continue
        if c == '"':
            j = sql.find('"', i + 1)
            if j < 0:
                break
            out.append(sql[i : j + 1])
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


_IDENT_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
_FUNCTION_CALL_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*\(')
_QUOTED_FUNCTION_CALL_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*\(')


def _split_statements(sql: str) -> list[str]:
    """Split top-level SQL statements on ;, respecting strings/comments
    (the input here is already stripped of those)."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for c in sql:
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        if c == ";" and depth == 0:
            text = "".join(buf).strip()
            if text:
                parts.append(text)
            buf = []
            continue
        buf.append(c)
    text = "".join(buf).strip()
    if text:
        parts.append(text)
    return parts


def _leading_statement_keyword(stmt: str) -> str:
    """Return the lowercased leading keyword of a statement.

    For statements starting with WITH, walks the CTE definitions
    ``name [AS] (...)``, possibly comma-separated and possibly leading
    with ``RECURSIVE``, then returns the first keyword after the last
    CTE body (the real statement verb: SELECT / DELETE / INSERT / ...).
    """
    s = stmt.lstrip()
    m = _IDENT_RE.match(s)
    if not m:
        return ""
    head = m.group(0).lower()
    if head != "with":
        return head
    i = m.end()
    n = len(s)

    def _skip_ws(j: int) -> int:
        while j < n and s[j].isspace():
            j += 1
        return j

    def _skip_parens(j: int) -> int:
        if j >= n or s[j] != "(":
            return j
        depth = 1
        j += 1
        while j < n and depth > 0:
            if s[j] == "(":
                depth += 1
            elif s[j] == ")":
                depth -= 1
            j += 1
        return j

    i = _skip_ws(i)
    if i < n:
        rec = _IDENT_RE.match(s, i)
        if rec and rec.group(0).lower() == "recursive":
            i = rec.end()
            i = _skip_ws(i)

    while True:
        m2 = _IDENT_RE.match(s, i)
        if not m2:
            return "with"
        i = m2.end()
        i = _skip_ws(i)
        if i < n:
            opt_as = _IDENT_RE.match(s, i)
            if opt_as and opt_as.group(0).lower() == "as":
                i = opt_as.end()
                i = _skip_ws(i)
        if i >= n or s[i] != "(":
            return m2.group(0).lower()
        i = _skip_parens(i)
        i = _skip_ws(i)
        if i < n and s[i] == ",":
            i += 1
            i = _skip_ws(i)
            continue
        next_m = _IDENT_RE.match(s, i)
        if next_m:
            return next_m.group(0).lower()
        return "with"


def _validate_query_sql(sql: str) -> None:
    """Reject SQL that is not a safe read-only query.

    Strips strings and comments, splits top-level statements, then rejects:
      - multi-statement queries
      - any statement whose leading keyword (after a CTE) is a mutation verb
      - calls to file-access functions (read_csv, read_parquet, http_*)

    Unknown leading keywords are passed through to DuckDB, which will return
    a parse error so the caller gets a 400 (not a misleading 403).
    """
    cleaned = _strip_sql_comments_and_strings(sql)
    statements = _split_statements(cleaned)
    if not statements:
        raise HTTPException(400, "Empty query.")
    if len(statements) > 1:
        raise HTTPException(403, "Multi-statement queries are not allowed.")
    stmt = statements[0]
    head = _leading_statement_keyword(stmt)
    if head in _FORBIDDEN_STATEMENT_KEYWORDS:
        raise HTTPException(
            403, "Only SELECT queries are allowed through the query interface."
        )
    # Note: at this point string literals are already stripped to '', so we
    # can scan the cleaned statement directly. We also have to catch quoted
    # identifiers: DuckDB happily accepts `"read_csv"(...)` and treats the
    # quoted identifier as the same builtin.
    for fname in _FUNCTION_CALL_RE.findall(stmt):
        if fname.lower() in _DANGEROUS_FUNCTION_NAMES:
            raise HTTPException(
                403,
                "File-access functions (read_csv, read_parquet, etc.) are not allowed.",
            )
    for fname in _QUOTED_FUNCTION_CALL_RE.findall(stmt):
        if fname.lower() in _DANGEROUS_FUNCTION_NAMES:
            raise HTTPException(
                403,
                "File-access functions (read_csv, read_parquet, etc.) are not allowed.",
            )
    if re.search(r'\bhttpfs_', stmt, re.IGNORECASE):
        raise HTTPException(403, "HTTPFS access is not allowed through the query interface.")


# --- Pydantic models ---


class QueryRequest(BaseModel):
    sql: str = Field(..., min_length=1, max_length=100_000)
    limit: int | None = Field(default=None, gt=0, le=50_000)
    offset: int = Field(default=0, ge=0)


class ExplainRequest(BaseModel):
    sql: str = Field(..., min_length=1, max_length=100_000)


from havn.engine.query_governor import QueryTimeoutError, get_timeout_for_role

_QUERY_TIMEOUT_SECONDS = 30  # fallback; overridden per-role below


# --- Explain / Profile endpoints ---


@router.post("/api/query/explain")
def explain_endpoint(request: Request, req: ExplainRequest, conn: DbConnReadOnly) -> dict:
    """Run EXPLAIN on a SQL query and return structured plan + raw text."""
    _require_permission(request, "read")
    _validate_query_sql(req.sql)
    try:
        from havn.engine.explain import explain_query as _explain_query, plan_to_dict

        plan_node, raw = _explain_query(conn, req.sql)
        return {"plan": plan_to_dict(plan_node), "raw": raw}
    except Exception as e:
        logger.warning("EXPLAIN failed: %s", e)
        raise HTTPException(400, str(e))


@router.post("/api/query/explain-analyze")
def explain_analyze_endpoint(request: Request, req: ExplainRequest, conn: DbConnReadOnly) -> dict:
    """Run EXPLAIN ANALYZE on a SQL query and return structured plan + raw text."""
    _require_permission(request, "read")
    _validate_query_sql(req.sql)
    try:
        from havn.engine.explain import explain_analyze_query as _explain_analyze, plan_to_dict

        plan_node, raw = _explain_analyze(conn, req.sql)
        return {"plan": plan_to_dict(plan_node), "raw": raw}
    except Exception as e:
        logger.warning("EXPLAIN ANALYZE failed: %s", e)
        raise HTTPException(400, str(e))


@router.post("/api/query/profile")
def profile_query(request: Request, req: ExplainRequest, conn: DbConnReadOnly) -> dict:
    """Run EXPLAIN ANALYZE on a SQL query and return the profiled plan."""
    _require_permission(request, "read")
    _validate_query_sql(req.sql)
    try:
        from havn.engine.explain import explain_analyze_query as _explain_analyze, plan_to_dict

        plan_node, raw = _explain_analyze(conn, req.sql)
        return {"plan": plan_to_dict(plan_node), "raw": raw}
    except Exception as e:
        logger.warning("EXPLAIN ANALYZE failed: %s", e)
        raise HTTPException(400, str(e))


# --- Slow query metrics ---


@router.get("/api/metrics/slow-queries")
def get_slow_queries(request: Request, conn: DbConnReadOnly, limit: int = 50) -> list[dict]:
    """Return recent slow queries from _havn.slow_queries."""
    _require_permission(request, "read")
    try:
        from havn.engine.database import ensure_meta_table
        ensure_meta_table(conn)
        rows = conn.execute(
            "SELECT query_text, duration_ms, row_count, executed_at "
            "FROM _havn.slow_queries ORDER BY executed_at DESC LIMIT ?",
            [min(limit, 500)],
        ).fetchall()
        return [
            {
                "query_text": r[0],
                "duration_ms": r[1],
                "row_count": r[2],
                "executed_at": _serialize(r[3]),
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("Failed to fetch slow queries: %s", e)
        raise HTTPException(400, str(e))


# --- Query endpoint ---


@router.post("/api/query")
def run_query(request: Request, req: QueryRequest, conn: DbConnReadOnly) -> dict:
    """Run an ad-hoc SQL query with a timeout."""
    user = _require_permission(request, "read")
    sql = req.sql

    # Audit log the query execution
    try:
        from havn.engine.audit import log_audit
        from havn.server.deps import _get_shared_conn

        _audit_conn = _get_shared_conn()
        client_ip = request.client.host if request.client else None
        log_audit(
            _audit_conn,
            user=user.get("username", "anonymous"),
            action="query",
            resource=sql[:500],
            ip_address=client_ip,
        )
    except Exception:
        logger.debug("Failed to write audit log for query", exc_info=True)

    # --- Masking SQL command interception ---
    sql_stripped = sql.strip()

    # SHOW MASKING POLICIES
    if re.match(r'^\s*SHOW\s+MASKING\s+POLIC', sql_stripped, re.IGNORECASE | re.DOTALL):
        from havn.engine.masking import ensure_masking_table
        from havn.engine.write_queue import cursor_for
        from havn.server.deps import _get_shared_conn
        conn_rw = cursor_for(_get_shared_conn())
        try:
            ensure_masking_table(conn_rw)
            policies = list_policies(conn_rw)
            columns = ["id", "schema_name", "table_name", "column_name", "method", "method_config", "condition_column", "condition_value", "exempted_roles", "created_at"]
            rows = []
            for p in policies:
                rows.append([p.get(c, "") for c in columns])
            return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": False}
        finally:
            conn_rw.close()

    create_match = re.match(
        r'^\s*CREATE\s+MASKING\s+POLICY\s+ON\s+(\w+)\.(\w+)\.(\w+)\s+METHOD\s+(\w+)(?:\s+EXEMPT\s+([\w,\s]+))?\s*$',
        sql_stripped, re.IGNORECASE | re.DOTALL
    )
    if create_match:
        _require_permission(request, "write")
        schema, table, column, method, exempt_str = create_match.groups()
        method = method.lower()
        if method not in ('hash', 'redact', 'null', 'partial'):
            raise HTTPException(400, "Invalid masking method. Use: hash, redact, null, partial")
        exempted = [r.strip() for r in exempt_str.split(',')] if exempt_str else ['admin']
        from havn.engine.masking import ensure_masking_table
        from havn.engine.write_queue import cursor_for
        from havn.server.deps import _get_shared_conn
        conn_rw = cursor_for(_get_shared_conn())
        try:
            ensure_masking_table(conn_rw)
            policy = create_policy(conn_rw, schema_name=schema, table_name=table, column_name=column, method=method, exempted_roles=exempted)
            return {"columns": ["result", "id"], "rows": [["Masking policy created", policy["id"]]], "row_count": 1, "truncated": False}
        finally:
            conn_rw.close()

    drop_match = re.match(r'^\s*DROP\s+MASKING\s+POLICY\s+([\w-]+)\s*$', sql_stripped, re.IGNORECASE | re.DOTALL)
    if drop_match:
        _require_permission(request, "write")
        policy_id = drop_match.group(1)
        from havn.engine.masking import ensure_masking_table
        from havn.engine.write_queue import cursor_for
        from havn.server.deps import _get_shared_conn
        conn_rw = cursor_for(_get_shared_conn())
        try:
            ensure_masking_table(conn_rw)
            deleted = delete_policy(conn_rw, policy_id)
            if deleted:
                return {"columns": ["result"], "rows": [["Masking policy deleted"]], "row_count": 1, "truncated": False}
            raise HTTPException(404, f"Policy {policy_id} not found")
        finally:
            conn_rw.close()

    # Validate the SQL is a safe read-only query
    _validate_query_sql(sql)

    # Pre-query masking: rewrite SQL to inject masking at column source level
    try:
        rewritten_sql, rewrite_ok, handled_ids = rewrite_query_with_masking(
            sql, user["role"], conn,
        )
    except MaskedColumnAccessError as e:
        raise HTTPException(403, str(e))
    sql_to_execute = rewritten_sql if rewrite_ok else sql

    try:
        import threading

        query_result: dict = {}
        query_error: list[Exception] = []

        def _exec_query():
            try:
                # Safety: if no LIMIT in the SQL and no limit param, inject a
                # server-side cap so DuckDB never buffers millions of rows.
                # The response includes total_rows so the user knows it was capped.
                SERVER_ROW_CAP = 50_000
                sql_clean = sql_to_execute.strip().rstrip(";")
                has_limit = bool(re.search(r'\bLIMIT\b', sql_to_execute, re.IGNORECASE))
                effective_limit = req.limit

                if req.offset > 0 and effective_limit is not None:
                    wrapped = f"SELECT * FROM ({sql_clean}) AS _q OFFSET {req.offset} LIMIT {effective_limit}"
                elif effective_limit is not None:
                    wrapped = f"SELECT * FROM ({sql_clean}) AS _q LIMIT {effective_limit}"
                elif not has_limit:
                    # No limit anywhere — inject server cap into the SQL itself
                    # so DuckDB can optimize and stop scanning early
                    wrapped = f"SELECT * FROM ({sql_clean}) AS _q LIMIT {SERVER_ROW_CAP}"
                    effective_limit = SERVER_ROW_CAP
                else:
                    wrapped = sql_to_execute

                if req.offset > 0 and effective_limit is None:
                    wrapped = f"SELECT * FROM ({sql_clean}) AS _q OFFSET {req.offset}"

                result = conn.execute(wrapped)
                columns = [desc[0] for desc in result.description]
                column_types = [str(desc[1]) for desc in result.description]
                if effective_limit is not None:
                    rows = result.fetchmany(effective_limit)
                else:
                    rows = result.fetchall()
                query_result["data"] = {
                    "columns": columns,
                    "column_types": column_types,
                    "rows": [[_serialize(v) for v in row] for row in rows],
                    "truncated": effective_limit is not None and len(rows) == effective_limit,
                    "offset": req.offset,
                    "limit": effective_limit,
                }
            except Exception as e:
                query_error.append(e)

        query_timeout = get_timeout_for_role(user.get("role", "viewer"))
        t_start = time.monotonic()

        # Acquire a resource-manager slot so the query shows up in the UI's
        # active-task list and counts toward the `query` concurrency budget.
        from havn.engine.resource_manager import current_task as _current_task
        from havn.engine.resource_manager import get_resource_manager as _get_rm

        _manager = _get_rm()
        with _manager.acquire_sync("query", f"sql:{sql[:60]}", conn=conn):
            _task = _current_task()
            if _task is not None:
                _manager.register_cancel(_task.task_id, conn.interrupt)

            thread = threading.Thread(target=_exec_query, daemon=True)
            thread.start()
            thread.join(timeout=query_timeout)

            if thread.is_alive():
                conn.interrupt()
                raise HTTPException(
                    408,
                    f"Query exceeded {query_timeout}s timeout. "
                    f"Try adding filters or a LIMIT clause.",
                )
        duration_ms = int((time.monotonic() - t_start) * 1000)

        if query_error:
            raise query_error[0]
        data = query_result["data"]
        # Post-query masking: skip policies already handled by pre-query rewriting
        if not rewrite_ok:
            data["rows"] = apply_masking(
                data["columns"], data["rows"], user["role"], conn,
            )
        elif handled_ids:
            # Rewrite succeeded but some policies may still need post-query
            # (conditional policies, unsupported methods). Run post-query
            # skipping what was already handled.
            data["rows"] = apply_masking(
                data["columns"], data["rows"], user["role"], conn,
                skip_policy_ids=handled_ids,
            )

        # Log slow queries
        if duration_ms >= _SLOW_QUERY_THRESHOLD_MS:
            try:
                from havn.engine.database import ensure_meta_table
                from havn.engine.write_queue import cursor_for
                from havn.server.deps import _get_shared_conn
                conn_rw = cursor_for(_get_shared_conn())
                try:
                    ensure_meta_table(conn_rw)
                    conn_rw.execute(
                        "INSERT INTO _havn.slow_queries (query_text, duration_ms, row_count) VALUES (?, ?, ?)",
                        [req.sql[:10_000], duration_ms, len(data["rows"])],
                    )
                finally:
                    conn_rw.close()
            except Exception:
                logger.debug("Failed to log slow query", exc_info=True)

        return data
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Query failed: %s", e)
        raise HTTPException(400, str(e))


# --- Tables ---


@router.get("/api/tables")
def list_tables(
    request: Request, conn: DbConnReadOnly, schema: str | None = None
) -> list[dict]:
    """List warehouse tables and views.

    Excludes ``information_schema`` and the ``_havn`` metadata schema so
    users only see their own data and the backend's queryable system
    tables (e.g. DuckLake's ``main.ducklake_*`` bookkeeping). Tables in
    a non-default catalog (DuckLake's ``__ducklake_metadata_warehouse``)
    are returned with the catalog prefixed onto the schema field, so
    a SELECT generated from the click handler is correctly qualified
    (``SELECT * FROM __ducklake_metadata_warehouse.main.ducklake_column``).
    """
    _require_permission(request, "read")
    # information_schema views don't list themselves in
    # information_schema.tables (chicken-and-egg with the SQL spec), so we
    # UNION in their rows from duckdb_views() to make them browseable.
    base_sql = """
        SELECT table_schema AS schema_label, table_name, table_type
        FROM information_schema.tables
        WHERE table_catalog = current_database()
        UNION ALL
        SELECT DISTINCT schema_name, view_name, 'VIEW'
        FROM duckdb_views()
        WHERE schema_name = 'information_schema'
    """
    if schema:
        _validate_identifier(schema, "schema")
        rows = conn.execute(
            f"SELECT * FROM ({base_sql}) WHERE schema_label = ? ORDER BY 1, 2",
            [schema],
        ).fetchall()
    else:
        rows = conn.execute(f"SELECT * FROM ({base_sql}) ORDER BY 1, 2").fetchall()
    return [{"schema": r[0], "name": r[1], "type": r[2]} for r in rows]


@router.get("/api/tables/{schema}/{table}")
def describe_table(
    request: Request, schema: str, table: str, conn: DbConnReadOnly
) -> dict:
    """Get column info for a table."""
    from havn.server.deps import _validate_schema_label

    _require_permission(request, "read")
    catalog, schema_name = _validate_schema_label(schema)
    _validate_identifier(table, "table")
    if catalog is None:
        cols = conn.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_catalog = current_database()
              AND table_schema = ? AND table_name = ?
            ORDER BY ordinal_position
            """,
            [schema_name, table],
        ).fetchall()
    else:
        cols = conn.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_catalog = ? AND table_schema = ? AND table_name = ?
            ORDER BY ordinal_position
            """,
            [catalog, schema_name, table],
        ).fetchall()
    if not cols and schema_name == "information_schema":
        # information_schema.columns doesn't describe its own views; fall
        # back to duckdb_columns(). Don't filter by database_name — DuckDB
        # reports info_schema under 'system' even when current_database()
        # is something else (e.g. 'warehouse' on DuckLake).
        cols = conn.execute(
            """
            SELECT column_name, data_type, CASE WHEN is_nullable THEN 'YES' ELSE 'NO' END
            FROM duckdb_columns()
            WHERE schema_name = ? AND table_name = ?
            ORDER BY column_index
            """,
            [schema_name, table],
        ).fetchall()
    return {
        "schema": schema,
        "name": table,
        "columns": [
            {"name": c[0], "type": c[1], "nullable": c[2] == "YES"} for c in cols
        ],
    }


@router.get("/api/tables/{schema}/{table}/sample")
def sample_table(
    request: Request,
    schema: str,
    table: str,
    conn: DbConnReadOnly,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Get sample rows from a table with pagination."""
    from havn.server.deps import _validate_schema_label

    user = _require_permission(request, "read")
    catalog, schema_name = _validate_schema_label(schema)
    _validate_identifier(table, "table")
    limit = max(1, min(limit, 100_000))
    offset = max(0, offset)
    try:
        quoted = (
            f'"{catalog}"."{schema_name}"."{table}"' if catalog
            else f'"{schema_name}"."{table}"'
        )
        result = conn.execute(f"SELECT * FROM {quoted} LIMIT {limit} OFFSET {offset}")
        columns = [desc[0] for desc in result.description]
        rows = [[_serialize(v) for v in row] for row in result.fetchall()]
        rows = apply_masking(columns, rows, user["role"], conn, schema=schema, table=table)
        return {
            "schema": schema,
            "table": table,
            "columns": columns,
            "rows": rows,
            "limit": limit,
            "offset": offset,
        }
    except Exception as e:
        logger.warning("Sample query failed for %s.%s: %s", schema, table, e)
        raise HTTPException(400, str(e))


@router.get("/api/tables/{schema}/{table}/profile")
def profile_table(
    request: Request, schema: str, table: str, conn: DbConnReadOnly
) -> dict:
    """Get column-level statistics for a table."""
    from havn.server.deps import _validate_schema_label

    user = _require_permission(request, "read")
    catalog, schema_name = _validate_schema_label(schema)
    _validate_identifier(table, "table")
    try:
        quoted = (
            f'"{catalog}"."{schema_name}"."{table}"' if catalog
            else f'"{schema_name}"."{table}"'
        )
        row_count = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
        if catalog is None:
            cols = conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_catalog = current_database() "
                "  AND table_schema = ? AND table_name = ? ORDER BY ordinal_position",
                [schema_name, table],
            ).fetchall()
        else:
            cols = conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_catalog = ? AND table_schema = ? AND table_name = ? "
                "ORDER BY ordinal_position",
                [catalog, schema_name, table],
            ).fetchall()
        if not cols and schema_name == "information_schema":
            cols = conn.execute(
                "SELECT column_name, data_type FROM duckdb_columns() "
                "WHERE schema_name = ? AND table_name = ? ORDER BY column_index",
                [schema_name, table],
            ).fetchall()

        profiles = []
        for col_name, col_type in cols:
            qcol = f'"{col_name}"'
            stats: dict = {"name": col_name, "type": col_type}

            basic = conn.execute(
                f"SELECT COUNT(*) - COUNT({qcol}), COUNT(DISTINCT {qcol}) FROM {quoted}"
            ).fetchone()
            stats["null_count"] = basic[0]
            stats["distinct_count"] = basic[1]

            is_numeric = any(
                t in col_type.upper()
                for t in (
                    "INT",
                    "FLOAT",
                    "DOUBLE",
                    "DECIMAL",
                    "NUMERIC",
                    "BIGINT",
                    "SMALLINT",
                    "TINYINT",
                    "HUGEINT",
                )
            )
            if is_numeric:
                num = conn.execute(
                    f"SELECT MIN({qcol}), MAX({qcol}), AVG({qcol}::DOUBLE) FROM {quoted}"
                ).fetchone()
                stats["min"] = _serialize(num[0])
                stats["max"] = _serialize(num[1])
                stats["avg"] = round(num[2], 4) if num[2] is not None else None
            else:
                minmax = conn.execute(
                    f"SELECT MIN({qcol}::VARCHAR), MAX({qcol}::VARCHAR) FROM {quoted}"
                ).fetchone()
                stats["min"] = minmax[0]
                stats["max"] = minmax[1]

            samples = conn.execute(
                f"SELECT DISTINCT {qcol}::VARCHAR FROM {quoted} WHERE {qcol} IS NOT NULL LIMIT 5"
            ).fetchall()
            stats["sample_values"] = [s[0] for s in samples]

            profiles.append(stats)

        # Mask sample_values in profile output
        from havn.engine.masking import load_policies, apply_mask

        policies = load_policies(conn)
        for col_profile in profiles:
            for p in policies:
                if user["role"] in p["exempted_roles"]:
                    continue
                if (p["schema_name"].lower() == schema.lower()
                        and p["table_name"].lower() == table.lower()
                        and p["column_name"].lower() == col_profile["name"].lower()):
                    col_profile["sample_values"] = [
                        apply_mask(v, p["method"], p["method_config"])
                        for v in col_profile["sample_values"]
                    ]
                    break

        return {
            "schema": schema,
            "table": table,
            "row_count": row_count,
            "columns": profiles,
        }
    except Exception as e:
        logger.warning("Profile failed for %s.%s: %s", schema, table, e)
        raise HTTPException(400, str(e))


# --- Autocomplete ---


@router.get("/api/autocomplete")
def get_autocomplete(request: Request, conn: DbConnReadOnly) -> dict:
    """Get table and column names for query autocomplete."""
    _require_permission(request, "read")
    tables = conn.execute(
        """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_catalog = current_database()
        UNION ALL
        SELECT DISTINCT schema_name, view_name
        FROM duckdb_views()
        WHERE schema_name = 'information_schema'
        ORDER BY 1, 2
        """
    ).fetchall()

    columns = conn.execute(
        """
        SELECT table_schema, table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_catalog = current_database()
        UNION ALL
        SELECT DISTINCT schema_name, table_name, column_name, data_type
        FROM duckdb_columns()
        WHERE schema_name = 'information_schema'
        ORDER BY 1, 2
        """
    ).fetchall()

    return {
        "tables": [
            {"schema": t[0], "name": t[1], "full_name": f"{t[0]}.{t[1]}"}
            for t in tables
        ],
        "columns": [
            {
                "schema": c[0],
                "table": c[1],
                "name": c[2],
                "type": c[3],
                "full_name": f"{c[0]}.{c[1]}.{c[2]}",
            }
            for c in columns
        ],
    }


# --- Export to CSV ---


class ExportRequest(BaseModel):
    sql: str = Field(..., min_length=1, max_length=100_000)


@router.post("/api/query/export-csv")
def export_csv(request: Request, req: ExportRequest, conn: DbConnReadOnly):
    """Stream query results as CSV download. No row limit."""
    user = _require_permission(request, "read")
    import csv
    import io

    # Validate the SQL is a safe read-only query
    _validate_query_sql(req.sql)

    # Pre-query masking rewrite
    try:
        csv_rewritten, csv_rewrite_ok, csv_handled = rewrite_query_with_masking(
            req.sql, user["role"], conn,
        )
    except MaskedColumnAccessError as e:
        raise HTTPException(403, str(e))
    csv_sql = csv_rewritten if csv_rewrite_ok else req.sql

    csv_timeout = get_timeout_for_role(user.get("role", "viewer"))
    try:
        from havn.engine.query_governor import execute_governed
        result, _ = execute_governed(conn, csv_sql, timeout_s=csv_timeout)
        columns = [desc[0] for desc in result.description]
    except QueryTimeoutError as e:
        raise HTTPException(408, str(e))
    except Exception as e:
        raise HTTPException(400, f"SQL error: {e}")

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        # Header
        writer.writerow(columns)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        # Stream rows in batches, applying masking to each batch
        while True:
            batch = result.fetchmany(5000)
            if not batch:
                break
            serialized = [[_serialize(v) for v in row] for row in batch]
            if not csv_rewrite_ok:
                serialized = apply_masking(columns, serialized, user["role"], conn)
            elif csv_handled:
                serialized = apply_masking(
                    columns, serialized, user["role"], conn,
                    skip_policy_ids=csv_handled,
                )
            for row in serialized:
                writer.writerow(row)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=export.csv"},
    )
