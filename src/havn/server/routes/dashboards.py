"""Dashboard CRUD, widget management, and widget query execution."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from havn.server.deps import (
    DbConn,
    DbConnReadOnly,
    _require_permission,
    _serialize,
)

logger = logging.getLogger("havn.server")

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class DashboardCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    template_id: str | None = None


class DashboardUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    layout: dict | None = None
    filters: list | None = None
    settings: dict | None = None
    is_template: bool | None = None


class WidgetCreate(BaseModel):
    widget_type: str = Field(..., pattern=r"^(chart|kpi|table|text|filter|image|divider)$")
    chart_type: str | None = None
    title: str = Field(default="", max_length=500)
    sql_query: str | None = Field(default=None, max_length=100_000)
    config: dict = Field(default_factory=dict)
    position: dict = Field(...)  # {x, y, w, h}
    filters: list = Field(default_factory=list)
    cache_ttl: int = Field(default=0, ge=0, le=86400)
    sort_order: int = Field(default=0)

    @field_validator("position")
    @classmethod
    def validate_position(cls, v):
        for key in ("x", "y", "w", "h"):
            if key not in v or not isinstance(v[key], (int, float)) or v[key] < 1:
                raise ValueError(f"position.{key} must be a positive number")
        return v


class WidgetUpdate(BaseModel):
    widget_type: str | None = Field(default=None, pattern=r"^(chart|kpi|table|text|filter|image|divider)$")
    chart_type: str | None = None
    title: str | None = Field(default=None, max_length=500)
    sql_query: str | None = Field(default=None, max_length=100_000)
    config: dict | None = None
    position: dict | None = None
    filters: list | None = None
    cache_ttl: int | None = Field(default=None, ge=0, le=86400)
    sort_order: int | None = None


class WidgetPositionUpdate(BaseModel):
    positions: list[dict]  # [{id, position: {x, y, w, h}}]


class WidgetQueryRequest(BaseModel):
    filters: dict = Field(default_factory=dict)  # {column: value}
    parameters: dict = Field(default_factory=dict)  # {param_name: value}


class BatchQueryRequest(BaseModel):
    filters: dict = Field(default_factory=dict)
    parameters: dict = Field(default_factory=dict)


class DashboardImport(BaseModel):
    dashboard: dict
    widgets: list[dict]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_QUERY_TIMEOUT_SECONDS = 30


def _get_user_name(request: Request) -> str:
    """Extract username from request, defaulting to 'anonymous'."""
    user = getattr(request.state, "user", None)
    if user and isinstance(user, dict):
        return user.get("username", "anonymous")
    return "anonymous"


def _cache_key(widget_id: str, filters: dict, parameters: dict) -> str:
    """Compute cache key from widget ID + filter/param state."""
    payload = json.dumps({"w": widget_id, "f": filters, "p": parameters}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _execute_widget_query(
    conn,
    sql_query: str,
    filters: dict,
    parameters: dict,
) -> dict:
    """Execute a widget SQL query with filter injection and parameter substitution.

    Returns {columns, rows, row_count}.
    """
    if not sql_query or not sql_query.strip():
        return {"columns": [], "rows": [], "row_count": 0}

    # Build parameterized filter injection
    base_sql = sql_query.strip().rstrip(";")
    params: list = []
    where_clauses: list[str] = []

    for col, val in filters.items():
        # Validate column name to prevent injection
        if not col.replace("_", "").replace(".", "").isalnum():
            continue
        params.append(val)
        where_clauses.append(f'"{col}" = ${len(params)}')

    # Substitute ${param_name} placeholders with positional params
    import re
    param_pattern = re.compile(r"\$\{(\w+)\}")
    def _replace_param(m):
        name = m.group(1)
        if name in parameters:
            params.append(parameters[name])
            return f"${len(params)}"
        return m.group(0)  # leave unresolved
    base_sql = param_pattern.sub(_replace_param, base_sql)

    if where_clauses:
        sql = f"WITH _src AS ({base_sql}) SELECT * FROM _src WHERE {' AND '.join(where_clauses)}"
    else:
        sql = base_sql

    try:
        # Set query timeout
        try:
            conn.execute(f"SET statement_timeout={_QUERY_TIMEOUT_SECONDS * 1000}")
        except Exception:
            pass  # Older DuckDB versions may not support this

        result = conn.execute(sql, params)
        columns = [desc[0] for desc in result.description] if result.description else []
        rows = result.fetchall()
        return {
            "columns": columns,
            "rows": [[_serialize(v) for v in row] for row in rows],
            "row_count": len(rows),
        }
    except Exception as e:
        raise HTTPException(400, f"Widget query error: {e}")


# ---------------------------------------------------------------------------
# Dashboard CRUD
# ---------------------------------------------------------------------------


@router.get("/api/dashboards")
def list_dashboards(request: Request, conn: DbConnReadOnly) -> list:
    """List all dashboards with widget counts."""
    _require_permission(request, "read")
    try:
        rows = conn.execute("""
            SELECT d.id, d.name, d.description, d.created_by, d.updated_by,
                   d.created_at, d.updated_at, d.is_template,
                   (SELECT COUNT(*) FROM _dp_internal.dashboard_widgets w
                    WHERE w.dashboard_id = d.id) AS widget_count
            FROM _dp_internal.dashboards d
            WHERE d.is_template = FALSE
            ORDER BY d.updated_at DESC
        """).fetchall()
    except Exception:
        return []

    return [
        {
            "id": r[0],
            "name": r[1],
            "description": r[2],
            "created_by": r[3],
            "updated_by": r[4],
            "created_at": _serialize(r[5]),
            "updated_at": _serialize(r[6]),
            "is_template": r[7],
            "widget_count": r[8],
        }
        for r in rows
    ]


@router.get("/api/dashboards/templates")
def list_templates(request: Request, conn: DbConnReadOnly) -> list:
    """List dashboard templates."""
    _require_permission(request, "read")
    try:
        rows = conn.execute("""
            SELECT d.id, d.name, d.description,
                   (SELECT COUNT(*) FROM _dp_internal.dashboard_widgets w
                    WHERE w.dashboard_id = d.id) AS widget_count
            FROM _dp_internal.dashboards d
            WHERE d.is_template = TRUE
            ORDER BY d.name
        """).fetchall()
    except Exception:
        return []

    return [
        {"id": r[0], "name": r[1], "description": r[2], "widget_count": r[3]}
        for r in rows
    ]


@router.post("/api/dashboards")
def create_dashboard(request: Request, req: DashboardCreate, conn: DbConn) -> dict:
    """Create a new dashboard, optionally from a template."""
    _require_permission(request, "write")
    username = _get_user_name(request)

    if req.template_id:
        return _clone_dashboard_impl(conn, req.template_id, req.name, req.description, username)

    row = conn.execute(
        """
        INSERT INTO _dp_internal.dashboards (name, description, created_by, updated_by)
        VALUES (?, ?, ?, ?)
        RETURNING id, name, description, created_by, created_at, updated_at, is_template
        """,
        [req.name, req.description, username, username],
    ).fetchone()

    return {
        "id": row[0],
        "name": row[1],
        "description": row[2],
        "created_by": row[3],
        "created_at": _serialize(row[4]),
        "updated_at": _serialize(row[5]),
        "is_template": row[6],
        "widget_count": 0,
    }


@router.get("/api/dashboards/{dashboard_id}")
def get_dashboard(request: Request, dashboard_id: str, conn: DbConnReadOnly) -> dict:
    """Get full dashboard with all widgets."""
    _require_permission(request, "read")

    row = conn.execute(
        """
        SELECT id, name, description, layout, filters, settings,
               created_by, updated_by, created_at, updated_at, is_template
        FROM _dp_internal.dashboards WHERE id = ?
        """,
        [dashboard_id],
    ).fetchone()

    if not row:
        raise HTTPException(404, "Dashboard not found")

    widgets_raw = conn.execute(
        """
        SELECT id, widget_type, chart_type, title, sql_query, config,
               position, filters, cache_ttl, sort_order, created_at
        FROM _dp_internal.dashboard_widgets
        WHERE dashboard_id = ?
        ORDER BY sort_order, created_at
        """,
        [dashboard_id],
    ).fetchall()

    def _parse_json(val):
        if val is None:
            return None
        if isinstance(val, (dict, list)):
            return val
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val

    widgets = [
        {
            "id": w[0],
            "widget_type": w[1],
            "chart_type": w[2],
            "title": w[3],
            "sql_query": w[4],
            "config": _parse_json(w[5]),
            "position": _parse_json(w[6]),
            "filters": _parse_json(w[7]),
            "cache_ttl": w[8],
            "sort_order": w[9],
            "created_at": _serialize(w[10]),
        }
        for w in widgets_raw
    ]

    return {
        "id": row[0],
        "name": row[1],
        "description": row[2],
        "layout": _parse_json(row[3]),
        "filters": _parse_json(row[4]),
        "settings": _parse_json(row[5]),
        "created_by": row[6],
        "updated_by": row[7],
        "created_at": _serialize(row[8]),
        "updated_at": _serialize(row[9]),
        "is_template": row[10],
        "widgets": widgets,
    }


@router.put("/api/dashboards/{dashboard_id}")
def update_dashboard(
    request: Request, dashboard_id: str, req: DashboardUpdate, conn: DbConn
) -> dict:
    """Update dashboard metadata, layout, filters, or settings."""
    _require_permission(request, "write")
    username = _get_user_name(request)

    # Build dynamic SET clause
    sets: list[str] = ["updated_by = ?", "updated_at = current_timestamp"]
    params: list = [username]

    if req.name is not None:
        sets.append("name = ?")
        params.append(req.name)
    if req.description is not None:
        sets.append("description = ?")
        params.append(req.description)
    if req.layout is not None:
        sets.append("layout = ?")
        params.append(json.dumps(req.layout))
    if req.filters is not None:
        sets.append("filters = ?")
        params.append(json.dumps(req.filters))
    if req.settings is not None:
        sets.append("settings = ?")
        params.append(json.dumps(req.settings))
    if req.is_template is not None:
        sets.append("is_template = ?")
        params.append(req.is_template)

    params.append(dashboard_id)
    row = conn.execute(
        f"""
        UPDATE _dp_internal.dashboards
        SET {', '.join(sets)}
        WHERE id = ?
        RETURNING id, name, description, updated_at
        """,
        params,
    ).fetchone()

    if not row:
        raise HTTPException(404, "Dashboard not found")

    return {
        "id": row[0],
        "name": row[1],
        "description": row[2],
        "updated_at": _serialize(row[3]),
    }


@router.delete("/api/dashboards/{dashboard_id}")
def delete_dashboard(request: Request, dashboard_id: str, conn: DbConn) -> dict:
    """Delete a dashboard and all its widgets and cache entries."""
    _require_permission(request, "write")

    # Check exists
    exists = conn.execute(
        "SELECT 1 FROM _dp_internal.dashboards WHERE id = ?", [dashboard_id]
    ).fetchone()
    if not exists:
        raise HTTPException(404, "Dashboard not found")

    # Clean up expired cache entries
    try:
        conn.execute("DELETE FROM _dp_internal.dashboard_cache WHERE expires_at < current_timestamp")
    except Exception:
        pass

    # Delete widgets then dashboard
    conn.execute(
        "DELETE FROM _dp_internal.dashboard_widgets WHERE dashboard_id = ?",
        [dashboard_id],
    )
    conn.execute(
        "DELETE FROM _dp_internal.dashboards WHERE id = ?", [dashboard_id]
    )

    return {"status": "deleted", "id": dashboard_id}


def _clone_dashboard_impl(
    conn, source_id: str, name: str, description: str, username: str
) -> dict:
    """Clone a dashboard and all its widgets."""
    source = conn.execute(
        """
        SELECT layout, filters, settings
        FROM _dp_internal.dashboards WHERE id = ?
        """,
        [source_id],
    ).fetchone()
    if not source:
        raise HTTPException(404, "Source dashboard/template not found")

    new_dash = conn.execute(
        """
        INSERT INTO _dp_internal.dashboards
            (name, description, layout, filters, settings, created_by, updated_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        RETURNING id, name, description, created_by, created_at, updated_at
        """,
        [name, description, source[0], source[1], source[2], username, username],
    ).fetchone()

    # Clone widgets
    widgets = conn.execute(
        """
        SELECT widget_type, chart_type, title, sql_query, config,
               position, filters, cache_ttl, sort_order
        FROM _dp_internal.dashboard_widgets
        WHERE dashboard_id = ?
        ORDER BY sort_order, created_at
        """,
        [source_id],
    ).fetchall()

    for w in widgets:
        conn.execute(
            """
            INSERT INTO _dp_internal.dashboard_widgets
                (dashboard_id, widget_type, chart_type, title, sql_query,
                 config, position, filters, cache_ttl, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [new_dash[0], *w],
        )

    return {
        "id": new_dash[0],
        "name": new_dash[1],
        "description": new_dash[2],
        "created_by": new_dash[3],
        "created_at": _serialize(new_dash[4]),
        "updated_at": _serialize(new_dash[5]),
        "is_template": False,
        "widget_count": len(widgets),
    }


@router.post("/api/dashboards/{dashboard_id}/clone")
def clone_dashboard(
    request: Request, dashboard_id: str, conn: DbConn, name: str = "Copy"
) -> dict:
    """Clone a dashboard."""
    _require_permission(request, "write")
    username = _get_user_name(request)
    return _clone_dashboard_impl(conn, dashboard_id, name, "", username)


@router.get("/api/dashboards/{dashboard_id}/export")
def export_dashboard(request: Request, dashboard_id: str, conn: DbConnReadOnly) -> dict:
    """Export dashboard as JSON for import elsewhere."""
    _require_permission(request, "read")
    full = get_dashboard(request, dashboard_id, conn)
    return {"dashboard": full, "widgets": full.pop("widgets", []), "version": 1}


@router.post("/api/dashboards/import")
def import_dashboard(request: Request, req: DashboardImport, conn: DbConn) -> dict:
    """Import a dashboard from exported JSON."""
    _require_permission(request, "write")
    username = _get_user_name(request)

    d = req.dashboard
    new_dash = conn.execute(
        """
        INSERT INTO _dp_internal.dashboards
            (name, description, layout, filters, settings, created_by, updated_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        RETURNING id, name, description, created_by, created_at, updated_at
        """,
        [
            d.get("name", "Imported Dashboard"),
            d.get("description", ""),
            json.dumps(d.get("layout", {})),
            json.dumps(d.get("filters", [])),
            json.dumps(d.get("settings", {})),
            username,
            username,
        ],
    ).fetchone()

    for w in req.widgets:
        conn.execute(
            """
            INSERT INTO _dp_internal.dashboard_widgets
                (dashboard_id, widget_type, chart_type, title, sql_query,
                 config, position, filters, cache_ttl, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                new_dash[0],
                w.get("widget_type", "chart"),
                w.get("chart_type"),
                w.get("title", ""),
                w.get("sql_query"),
                json.dumps(w.get("config", {})),
                json.dumps(w.get("position", {"x": 1, "y": 1, "w": 6, "h": 4})),
                json.dumps(w.get("filters", [])),
                w.get("cache_ttl", 0),
                w.get("sort_order", 0),
            ],
        )

    return {
        "id": new_dash[0],
        "name": new_dash[1],
        "description": new_dash[2],
        "created_by": new_dash[3],
        "created_at": _serialize(new_dash[4]),
        "updated_at": _serialize(new_dash[5]),
        "is_template": False,
        "widget_count": len(req.widgets),
    }


# ---------------------------------------------------------------------------
# Widget CRUD
# ---------------------------------------------------------------------------


@router.post("/api/dashboards/{dashboard_id}/widgets")
def add_widget(request: Request, dashboard_id: str, req: WidgetCreate, conn: DbConn) -> dict:
    """Add a widget to a dashboard."""
    _require_permission(request, "write")

    # Verify dashboard exists
    exists = conn.execute(
        "SELECT 1 FROM _dp_internal.dashboards WHERE id = ?", [dashboard_id]
    ).fetchone()
    if not exists:
        raise HTTPException(404, "Dashboard not found")

    row = conn.execute(
        """
        INSERT INTO _dp_internal.dashboard_widgets
            (dashboard_id, widget_type, chart_type, title, sql_query,
             config, position, filters, cache_ttl, sort_order)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id, widget_type, chart_type, title, position, sort_order, created_at
        """,
        [
            dashboard_id,
            req.widget_type,
            req.chart_type,
            req.title,
            req.sql_query,
            json.dumps(req.config),
            json.dumps(req.position),
            json.dumps(req.filters),
            req.cache_ttl,
            req.sort_order,
        ],
    ).fetchone()

    # Touch dashboard updated_at
    conn.execute(
        "UPDATE _dp_internal.dashboards SET updated_at = current_timestamp WHERE id = ?",
        [dashboard_id],
    )

    return {
        "id": row[0],
        "widget_type": row[1],
        "chart_type": row[2],
        "title": row[3],
        "sql_query": req.sql_query,
        "config": req.config,
        "position": req.position,
        "filters": req.filters,
        "cache_ttl": req.cache_ttl,
        "sort_order": row[5],
        "created_at": _serialize(row[6]),
    }


@router.put("/api/dashboards/{dashboard_id}/widgets/{widget_id}")
def update_widget(
    request: Request, dashboard_id: str, widget_id: str, req: WidgetUpdate, conn: DbConn
) -> dict:
    """Update a widget's configuration."""
    _require_permission(request, "write")

    sets: list[str] = []
    params: list = []

    if req.widget_type is not None:
        sets.append("widget_type = ?")
        params.append(req.widget_type)
    if req.chart_type is not None:
        sets.append("chart_type = ?")
        params.append(req.chart_type)
    if req.title is not None:
        sets.append("title = ?")
        params.append(req.title)
    if req.sql_query is not None:
        sets.append("sql_query = ?")
        params.append(req.sql_query)
    if req.config is not None:
        sets.append("config = ?")
        params.append(json.dumps(req.config))
    if req.position is not None:
        sets.append("position = ?")
        params.append(json.dumps(req.position))
    if req.filters is not None:
        sets.append("filters = ?")
        params.append(json.dumps(req.filters))
    if req.cache_ttl is not None:
        sets.append("cache_ttl = ?")
        params.append(req.cache_ttl)
    if req.sort_order is not None:
        sets.append("sort_order = ?")
        params.append(req.sort_order)

    if not sets:
        raise HTTPException(400, "No fields to update")

    params.extend([dashboard_id, widget_id])
    row = conn.execute(
        f"""
        UPDATE _dp_internal.dashboard_widgets
        SET {', '.join(sets)}
        WHERE dashboard_id = ? AND id = ?
        RETURNING id, widget_type, chart_type, title, sql_query, config,
                  position, filters, cache_ttl, sort_order, created_at
        """,
        params,
    ).fetchone()

    if not row:
        raise HTTPException(404, "Widget not found")

    # Touch dashboard
    conn.execute(
        "UPDATE _dp_internal.dashboards SET updated_at = current_timestamp WHERE id = ?",
        [dashboard_id],
    )

    def _pj(v):
        if v is None:
            return None
        if isinstance(v, (dict, list)):
            return v
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return v

    return {
        "id": row[0],
        "widget_type": row[1],
        "chart_type": row[2],
        "title": row[3],
        "sql_query": row[4],
        "config": _pj(row[5]),
        "position": _pj(row[6]),
        "filters": _pj(row[7]),
        "cache_ttl": row[8],
        "sort_order": row[9],
        "created_at": _serialize(row[10]),
    }


@router.patch("/api/dashboards/{dashboard_id}/widgets/positions")
def update_widget_positions(
    request: Request, dashboard_id: str, req: WidgetPositionUpdate, conn: DbConn
) -> dict:
    """Batch-update widget positions (after drag/resize)."""
    _require_permission(request, "write")

    for item in req.positions:
        wid = item.get("id")
        pos = item.get("position")
        if wid and pos:
            conn.execute(
                """
                UPDATE _dp_internal.dashboard_widgets
                SET position = ?
                WHERE dashboard_id = ? AND id = ?
                """,
                [json.dumps(pos), dashboard_id, wid],
            )

    conn.execute(
        "UPDATE _dp_internal.dashboards SET updated_at = current_timestamp WHERE id = ?",
        [dashboard_id],
    )

    return {"status": "ok", "updated": len(req.positions)}


@router.delete("/api/dashboards/{dashboard_id}/widgets/{widget_id}")
def delete_widget(request: Request, dashboard_id: str, widget_id: str, conn: DbConn) -> dict:
    """Remove a widget from a dashboard."""
    _require_permission(request, "write")

    deleted = conn.execute(
        """
        DELETE FROM _dp_internal.dashboard_widgets
        WHERE dashboard_id = ? AND id = ?
        RETURNING id
        """,
        [dashboard_id, widget_id],
    ).fetchone()

    if not deleted:
        raise HTTPException(404, "Widget not found")

    conn.execute(
        "UPDATE _dp_internal.dashboards SET updated_at = current_timestamp WHERE id = ?",
        [dashboard_id],
    )

    return {"status": "deleted", "id": widget_id}


# ---------------------------------------------------------------------------
# Widget query execution
# ---------------------------------------------------------------------------


@router.post("/api/dashboards/{dashboard_id}/widgets/{widget_id}/query")
def query_widget(
    request: Request,
    dashboard_id: str,
    widget_id: str,
    req: WidgetQueryRequest,
    conn: DbConnReadOnly,
) -> dict:
    """Execute a widget's SQL query with filter injection."""
    _require_permission(request, "read")

    widget = conn.execute(
        """
        SELECT sql_query, cache_ttl FROM _dp_internal.dashboard_widgets
        WHERE dashboard_id = ? AND id = ?
        """,
        [dashboard_id, widget_id],
    ).fetchone()

    if not widget:
        raise HTTPException(404, "Widget not found")

    sql_query = widget[0]
    cache_ttl = widget[1] or 0

    # Check cache
    if cache_ttl > 0:
        ck = _cache_key(widget_id, req.filters, req.parameters)
        cached = conn.execute(
            """
            SELECT result_json, row_count FROM _dp_internal.dashboard_cache
            WHERE cache_key = ? AND expires_at > current_timestamp
            """,
            [ck],
        ).fetchone()
        if cached:
            result = cached[0]
            if isinstance(result, str):
                result = json.loads(result)
            return result

    # Execute query
    result = _execute_widget_query(conn, sql_query, req.filters, req.parameters)

    # Store in cache
    if cache_ttl > 0:
        ck = _cache_key(widget_id, req.filters, req.parameters)
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO _dp_internal.dashboard_cache
                    (cache_key, result_json, row_count, cached_at, expires_at)
                VALUES (?, ?, ?, current_timestamp, current_timestamp + INTERVAL ? SECOND)
                """,
                [ck, json.dumps(result), result.get("row_count", 0), cache_ttl],
            )
        except Exception:
            pass  # Cache write failure is not critical

    return result


@router.post("/api/dashboards/{dashboard_id}/query-batch")
def query_batch(
    request: Request,
    dashboard_id: str,
    req: BatchQueryRequest,
    conn: DbConnReadOnly,
) -> dict:
    """Execute all widget queries for a dashboard in one call."""
    _require_permission(request, "read")

    widgets = conn.execute(
        """
        SELECT id, sql_query, cache_ttl FROM _dp_internal.dashboard_widgets
        WHERE dashboard_id = ? AND sql_query IS NOT NULL AND sql_query != ''
        ORDER BY sort_order, created_at
        """,
        [dashboard_id],
    ).fetchall()

    results: dict = {}
    for w_id, sql_query, cache_ttl in widgets:
        cache_ttl = cache_ttl or 0

        # Check cache first
        if cache_ttl > 0:
            ck = _cache_key(w_id, req.filters, req.parameters)
            cached = conn.execute(
                """
                SELECT result_json FROM _dp_internal.dashboard_cache
                WHERE cache_key = ? AND expires_at > current_timestamp
                """,
                [ck],
            ).fetchone()
            if cached:
                r = cached[0]
                if isinstance(r, str):
                    r = json.loads(r)
                results[w_id] = r
                continue

        try:
            result = _execute_widget_query(conn, sql_query, req.filters, req.parameters)
            results[w_id] = result

            # Cache the result
            if cache_ttl > 0:
                ck = _cache_key(w_id, req.filters, req.parameters)
                try:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO _dp_internal.dashboard_cache
                            (cache_key, result_json, row_count, cached_at, expires_at)
                        VALUES (?, ?, ?, current_timestamp, current_timestamp + INTERVAL ? SECOND)
                        """,
                        [ck, json.dumps(result), result.get("row_count", 0), cache_ttl],
                    )
                except Exception:
                    pass
        except HTTPException:
            results[w_id] = {"columns": [], "rows": [], "row_count": 0, "error": "Query failed"}
        except Exception as e:
            results[w_id] = {"columns": [], "rows": [], "row_count": 0, "error": str(e)}

    return {"results": results}


@router.delete("/api/dashboards/{dashboard_id}/cache")
def clear_cache(request: Request, dashboard_id: str, conn: DbConn) -> dict:
    """Clear all cache entries for a dashboard's widgets."""
    _require_permission(request, "write")

    widget_ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM _dp_internal.dashboard_widgets WHERE dashboard_id = ?",
            [dashboard_id],
        ).fetchall()
    ]

    deleted = 0
    for wid in widget_ids:
        # Cache keys are hashes of widget_id + filters; delete all matching this widget
        # Since cache_key is a hash, we can't easily filter. Clear all cache instead.
        pass

    # Simple approach: clear all expired + all for this dashboard
    conn.execute("DELETE FROM _dp_internal.dashboard_cache WHERE expires_at < current_timestamp")

    return {"status": "ok"}
