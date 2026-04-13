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

# Statements that modify data or schema — only SELECT is allowed through
# the query endpoint.
_FORBIDDEN_STATEMENT_RE = re.compile(
    r'^\s*('
    r'INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|MERGE'
    r'|COPY|ATTACH|DETACH|INSTALL|LOAD|EXPORT|IMPORT'
    r'|GRANT|REVOKE|SET|RESET|VACUUM|CHECKPOINT|PRAGMA'
    r'|CALL|EXECUTE'
    r')\b',
    re.IGNORECASE,
)

# DuckDB functions that can read/write files on the filesystem
_DANGEROUS_FUNCTIONS_RE = re.compile(
    r'\b('
    r'read_csv_auto|read_csv|read_parquet|read_json_auto|read_json'
    r'|read_json_objects|read_ndjson|read_ndjson_auto'
    r'|read_blob|read_text'
    r'|write_csv|write_parquet'
    r'|iceberg_scan|delta_scan|parquet_scan|csv_scan'
    r'|httpfs_.*|http_get|http_post'
    r')\s*\(',
    re.IGNORECASE,
)

# Block access to the internal metadata schema
_INTERNAL_SCHEMA_RE = re.compile(r'\b_havn\b', re.IGNORECASE)


def _validate_query_sql(sql: str) -> None:
    """Reject SQL that is not a safe read-only query.

    Raises HTTPException(403) if the SQL contains forbidden statements,
    dangerous file-access functions, or references to _havn.
    """
    stripped = sql.strip().rstrip(";").strip()

    if _FORBIDDEN_STATEMENT_RE.match(stripped):
        raise HTTPException(
            403,
            "Only SELECT queries are allowed through the query interface.",
        )

    if _DANGEROUS_FUNCTIONS_RE.search(stripped):
        raise HTTPException(
            403,
            "File-access functions (read_csv, read_parquet, etc.) are not allowed through the query interface.",
        )

    if _INTERNAL_SCHEMA_RE.search(stripped):
        raise HTTPException(
            403,
            "Access to _havn schema is not allowed through the query interface.",
        )


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
        from havn.server.deps import _get_backend
        from havn.engine.masking import ensure_masking_table
        conn_rw = _get_backend().connect(read_only=False)
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

    # CREATE MASKING POLICY ON schema.table.column METHOD method [EXEMPT role1,role2]
    create_match = re.match(
        r'^\s*CREATE\s+MASKING\s+POLICY\s+ON\s+(\w+)\.(\w+)\.(\w+)\s+METHOD\s+(\w+)(?:\s+EXEMPT\s+([\w,\s]+))?\s*$',
        sql_stripped, re.IGNORECASE | re.DOTALL
    )
    if create_match:
        schema, table, column, method, exempt_str = create_match.groups()
        method = method.lower()
        if method not in ('hash', 'redact', 'null', 'partial'):
            return {"columns": ["error"], "rows": [["Invalid method. Use: hash, redact, null, partial"]], "row_count": 1, "truncated": False}
        exempted = [r.strip() for r in exempt_str.split(',')] if exempt_str else ['admin']
        from havn.server.deps import _get_backend
        from havn.engine.masking import ensure_masking_table
        conn_rw = _get_backend().connect(read_only=False)
        try:
            ensure_masking_table(conn_rw)
            policy = create_policy(conn_rw, schema_name=schema, table_name=table, column_name=column, method=method, exempted_roles=exempted)
            return {"columns": ["result", "id"], "rows": [["Masking policy created", policy["id"]]], "row_count": 1, "truncated": False}
        finally:
            conn_rw.close()

    # DROP MASKING POLICY <id>
    drop_match = re.match(r'^\s*DROP\s+MASKING\s+POLICY\s+([\w-]+)\s*$', sql_stripped, re.IGNORECASE | re.DOTALL)
    if drop_match:
        policy_id = drop_match.group(1)
        from havn.server.deps import _get_backend
        from havn.engine.masking import ensure_masking_table
        conn_rw = _get_backend().connect(read_only=False)
        try:
            ensure_masking_table(conn_rw)
            deleted = delete_policy(conn_rw, policy_id)
            if deleted:
                return {"columns": ["result"], "rows": [["Masking policy deleted"]], "row_count": 1, "truncated": False}
            else:
                return {"columns": ["error"], "rows": [[f"Policy {policy_id} not found"]], "row_count": 1, "truncated": False}
        finally:
            conn_rw.close()
    # --- End masking SQL command interception ---

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
                if effective_limit is not None:
                    rows = result.fetchmany(effective_limit)
                else:
                    rows = result.fetchall()
                query_result["data"] = {
                    "columns": columns,
                    "rows": [[_serialize(v) for v in row] for row in rows],
                    "truncated": effective_limit is not None and len(rows) == effective_limit,
                    "offset": req.offset,
                    "limit": effective_limit,
                }
            except Exception as e:
                query_error.append(e)

        query_timeout = get_timeout_for_role(user.get("role", "viewer"))
        t_start = time.monotonic()
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
                from havn.server.deps import _get_backend
                from havn.engine.database import ensure_meta_table
                conn_rw = _get_backend().connect(read_only=False)
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
    """List warehouse tables and views."""
    _require_permission(request, "read")
    if schema:
        _validate_identifier(schema, "schema")
        rows = conn.execute(
            """
            SELECT table_schema, table_name, table_type
            FROM information_schema.tables
            WHERE table_schema NOT IN ('information_schema', '_havn')
              AND table_schema = ?
            ORDER BY table_schema, table_name
            """,
            [schema],
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT table_schema, table_name, table_type
            FROM information_schema.tables
            WHERE table_schema NOT IN ('information_schema', '_havn')
            ORDER BY table_schema, table_name
            """
        ).fetchall()
    return [{"schema": r[0], "name": r[1], "type": r[2]} for r in rows]


@router.get("/api/tables/{schema}/{table}")
def describe_table(
    request: Request, schema: str, table: str, conn: DbConnReadOnly
) -> dict:
    """Get column info for a table."""
    _require_permission(request, "read")
    _validate_identifier(schema, "schema")
    _validate_identifier(table, "table")
    cols = conn.execute(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = ? AND table_name = ?
        ORDER BY ordinal_position
        """,
        [schema, table],
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
    user = _require_permission(request, "read")
    _validate_identifier(schema, "schema")
    _validate_identifier(table, "table")
    limit = max(1, min(limit, 100_000))
    offset = max(0, offset)
    try:
        quoted = f'"{schema}"."{table}"'
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
    user = _require_permission(request, "read")
    _validate_identifier(schema, "schema")
    _validate_identifier(table, "table")
    try:
        quoted = f'"{schema}"."{table}"'
        row_count = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
        cols = conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
            [schema, table],
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
        WHERE table_schema NOT IN ('information_schema', '_havn')
        ORDER BY table_schema, table_name
        """
    ).fetchall()

    columns = conn.execute(
        """
        SELECT table_schema, table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema NOT IN ('information_schema', '_havn')
        ORDER BY table_schema, table_name, ordinal_position
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
