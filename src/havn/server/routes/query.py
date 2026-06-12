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
# The implementation lives in havn.engine.sql_safety so non-HTTP surfaces
# (MCP server, semantic layer, CLI) share the exact same rules. The names
# below are kept as thin wrappers/aliases for backward compatibility —
# other route modules and tests import them from here.

from havn.engine.sql_safety import (  # noqa: E402
    ReadOnlyQueryError,
    leading_statement_keyword as _leading_statement_keyword,  # noqa: F401
    split_statements as _split_statements,  # noqa: F401
    strip_sql_comments_and_strings as _strip_sql_comments_and_strings,  # noqa: F401
    validate_read_only_query,
)


def _validate_query_sql(sql: str) -> None:
    """Reject SQL that is not a safe read-only query (HTTP flavour).

    See :func:`havn.engine.sql_safety.validate_read_only_query` for the rules.
    """
    try:
        validate_read_only_query(sql)
    except ReadOnlyQueryError as e:
        raise HTTPException(e.status_code, str(e))


# --- Pydantic models ---


QueryParams = dict[str, str | int | float | bool | None]


class QueryRequest(BaseModel):
    sql: str = Field(..., min_length=1, max_length=100_000)
    limit: int | None = Field(default=None, gt=0, le=50_000)
    offset: int = Field(default=0, ge=0)
    params: QueryParams | None = Field(
        default=None, description="Named values bound to $name placeholders"
    )


class ExplainRequest(BaseModel):
    sql: str = Field(..., min_length=1, max_length=100_000)
    params: QueryParams | None = None


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

        plan_node, raw = _explain_query(conn, req.sql, params=req.params)
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

        plan_node, raw = _explain_analyze(conn, req.sql, params=req.params)
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

        plan_node, raw = _explain_analyze(conn, req.sql, params=req.params)
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
                # Trailing newline before the closing paren so a query that
                # ends with a `-- line comment` can't swallow the wrapper.
                sql_clean = sql_to_execute.strip().rstrip(";") + "\n"
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

                result = conn.execute(wrapped, req.params)
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
    params: QueryParams | None = None


@router.post("/api/query/export-csv")
def export_csv(request: Request, req: ExportRequest):
    """Stream query results as CSV download. No row limit."""
    user = _require_permission(request, "read")
    import csv
    import io

    # Validate the SQL is a safe read-only query
    _validate_query_sql(req.sql)

    # FastAPI (0.106+) closes yield-dependency cursors before a StreamingResponse body runs, so manage the read cursor manually and release it when the stream ends
    from havn.server.deps import _get_backend, _get_read_pool, _require_db

    _require_db(_get_backend())
    pool_ctx = _get_read_pool().connection()
    conn = pool_ctx.__enter__()

    def _release():
        try:
            pool_ctx.__exit__(None, None, None)
        except Exception:
            logger.debug("Failed to release export-csv cursor", exc_info=True)

    try:
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
            result, _ = execute_governed(conn, csv_sql, timeout_s=csv_timeout, params=req.params)
            columns = [desc[0] for desc in result.description]
        except QueryTimeoutError as e:
            raise HTTPException(408, str(e))
        except Exception as e:
            raise HTTPException(400, f"SQL error: {e}")
    except BaseException:
        _release()
        raise

    def generate():
        try:
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
        finally:
            _release()

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=export.csv"},
    )
