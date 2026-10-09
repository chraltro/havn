"""Running a dashboard's saved widget queries with filters and parameters.

Shared by the dashboard editor routes, published dashboards and scheduled
reports. Every query goes through :mod:`havn.engine.governed_query`, so
masking (and any later governance pass) applies to the final SQL, filter
predicates included.

Filter and parameter values are always bound as named parameters
(``$f0``, ``$p_region``). Only filter *column names* reach the SQL text, and
those must be plain ASCII identifiers (``col`` or ``table.col``), quoted on
the way in.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from typing import Any

import duckdb

from havn.engine.governed_query import QueryIdentity, run_governed_query

logger = logging.getLogger("havn.engine.dashboard_queries")

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
_PARAM_RE = re.compile(r"\$\{(\w+)\}")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?$")

# Filter types the dashboard filter bar offers (DashboardFilterBar.jsx).
FILTER_TYPES = {"dropdown", "multi_select", "date_range", "text", "number_range", "toggle"}

MAX_FILTER_LIST = 1000
MAX_FILTER_TEXT = 500

DEFAULT_WIDGET_TIMEOUT_S = 30
WIDGET_ROW_CAP = 10_000


class FilterValueError(ValueError):
    """A filter or parameter value does not fit its declared type."""


def parse_json(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return val


def load_dashboard(conn: duckdb.DuckDBPyConnection, dashboard_id: str) -> dict | None:
    """Load a dashboard row and its widgets (saved SQL included)."""
    row = conn.execute(
        """
        SELECT id, name, description, layout, filters, settings,
               created_by, updated_by, created_at, updated_at, is_template
        FROM _havn.dashboards WHERE id = ?
        """,
        [dashboard_id],
    ).fetchone()
    if not row:
        return None
    widgets_raw = conn.execute(
        """
        SELECT id, widget_type, chart_type, title, sql_query, config,
               position, filters, cache_ttl, sort_order, created_at
        FROM _havn.dashboard_widgets
        WHERE dashboard_id = ?
        ORDER BY sort_order, created_at
        """,
        [dashboard_id],
    ).fetchall()
    widgets = [
        {
            "id": w[0],
            "widget_type": w[1],
            "chart_type": w[2],
            "title": w[3],
            "sql_query": w[4],
            "config": parse_json(w[5]) or {},
            "position": parse_json(w[6]) or {},
            "filters": parse_json(w[7]) or [],
            "cache_ttl": w[8] or 0,
            "sort_order": w[9],
            "created_at": w[10],
        }
        for w in widgets_raw
    ]
    return {
        "id": row[0],
        "name": row[1],
        "description": row[2] or "",
        "layout": parse_json(row[3]) or {},
        "filters": parse_json(row[4]) or [],
        "settings": parse_json(row[5]) or {},
        "created_by": row[6],
        "updated_by": row[7],
        "created_at": row[8],
        "updated_at": row[9],
        "is_template": row[10],
        "widgets": widgets,
    }


def runs_query(widget: dict) -> bool:
    """True for widgets whose saved ``sql_query`` is SQL to run.

    Text widgets keep their markdown in ``sql_query`` as a fallback for
    ``config.content``; it is never executed.
    """
    return bool((widget.get("sql_query") or "").strip()) and widget.get("widget_type") not in (
        "text", "image", "divider",
    )


def declared_filter_types(dashboard_filters: list | None) -> dict[str, str]:
    """Map each declared filter column to its filter type."""
    out: dict[str, str] = {}
    for f in dashboard_filters or []:
        if not isinstance(f, dict):
            continue
        col = f.get("column")
        if isinstance(col, str) and _IDENT_RE.match(col):
            ftype = f.get("type") if f.get("type") in FILTER_TYPES else "dropdown"
            out[col] = ftype
    return out


def declared_parameters(settings: dict | None) -> dict[str, dict]:
    """Map each declared dashboard parameter name to its definition."""
    out: dict[str, dict] = {}
    for p in (settings or {}).get("parameters") or []:
        if isinstance(p, dict) and isinstance(p.get("name"), str) and re.match(r"^\w+$", p["name"]):
            out[p["name"]] = p
    return out


def _scalar(value: Any, what: str) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > MAX_FILTER_TEXT:
            raise FilterValueError(f"{what} is too long")
        return value
    raise FilterValueError(f"{what} must be a single value")


def _number(value: Any, what: str) -> float | int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise FilterValueError(f"{what} must be a number")
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value) if any(c in value for c in ".eE") else int(value)
        except ValueError:
            pass
    raise FilterValueError(f"{what} must be a number")


def _date(value: Any, what: str) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str) and _DATE_RE.match(value.strip()):
        return value.strip()
    raise FilterValueError(f"{what} must be a date (YYYY-MM-DD)")


def normalize_filter_value(ftype: str, value: Any, column: str = "filter") -> Any:
    """Validate a filter value against its declared type and return it normalised.

    Returns ``None`` when the filter is effectively unset.
    """
    what = f"Filter '{column}'"
    if value is None or value == "":
        return None
    if ftype == "multi_select":
        if not isinstance(value, list):
            value = [value]
        if len(value) > MAX_FILTER_LIST:
            raise FilterValueError(f"{what} has too many values")
        vals = [_scalar(v, what) for v in value]
        return vals or None
    if ftype == "date_range":
        if not isinstance(value, dict):
            raise FilterValueError(f"{what} must be a date range")
        frm, to = _date(value.get("from"), what), _date(value.get("to"), what)
        if frm is None and to is None:
            return None
        return {"from": frm, "to": to}
    if ftype == "number_range":
        if not isinstance(value, dict):
            raise FilterValueError(f"{what} must be a number range")
        lo, hi = _number(value.get("min"), what), _number(value.get("max"), what)
        if lo is None and hi is None:
            return None
        return {"min": lo, "max": hi}
    if ftype == "toggle":
        if value is True or value == "true":
            return True
        if value is False or value == "false":
            return None
        raise FilterValueError(f"{what} must be on or off")
    if ftype == "text":
        if not isinstance(value, str):
            value = str(_scalar(value, what))
        return _scalar(value, what)
    # dropdown and unknown types: one value
    return _scalar(value, what)


def _infer_type(value: Any) -> str:
    if isinstance(value, list):
        return "multi_select"
    if isinstance(value, dict):
        if "from" in value or "to" in value:
            return "date_range"
        if "min" in value or "max" in value:
            return "number_range"
    if value is True:
        return "toggle"
    return "dropdown"


def _quote_column(col: str) -> str:
    # Callers have already checked _IDENT_RE, so no quote can appear inside.
    return ".".join(f'"{part}"' for part in col.split("."))


def _filter_condition(col: str, ftype: str, value: Any, idx: int, params: dict) -> str | None:
    qcol = _quote_column(col)
    key = f"f{idx}"
    if ftype == "multi_select":
        names = []
        for j, v in enumerate(value):
            params[f"{key}_{j}"] = v
            names.append(f"${key}_{j}")
        return f"{qcol} IN ({', '.join(names)})" if names else None
    if ftype in ("date_range", "number_range"):
        lo_key, hi_key = ("from", "to") if ftype == "date_range" else ("min", "max")
        parts = []
        if value.get(lo_key) is not None:
            params[f"{key}_lo"] = value[lo_key]
            parts.append(f"{qcol} >= ${key}_lo")
        if value.get(hi_key) is not None:
            params[f"{key}_hi"] = value[hi_key]
            if ftype == "date_range" and _DATE_RE.match(str(value[hi_key])) and len(str(value[hi_key])) == 10:
                # A date "to" includes the whole day for timestamp columns.
                parts.append(f"{qcol} < CAST(${key}_hi AS DATE) + INTERVAL 1 DAY")
            else:
                parts.append(f"{qcol} <= ${key}_hi")
        return " AND ".join(parts) or None
    if ftype == "text":
        params[key] = value
        return f"CAST({qcol} AS VARCHAR) ILIKE '%' || ${key} || '%'"
    params[key] = value
    return f"{qcol} = ${key}"


def build_widget_sql(
    sql: str,
    filters: dict | None,
    parameters: dict | None,
    filter_types: dict[str, str] | None = None,
) -> tuple[str, dict]:
    """Return ``(sql, params)`` for a widget's saved SQL with filters applied.

    * ``${name}`` placeholders become ``$p_name`` bound to ``parameters[name]``
      (``NULL`` when the parameter is not supplied).
    * Each filter becomes a predicate on the outermost SELECT (so it filters
      before any GROUP BY there), or on a wrapping ``SELECT *`` when the
      query is not a plain SELECT. Columns must be identifiers; values are
      bound, never spliced.

    Filters whose column is not an identifier are skipped. Values are
    validated against ``filter_types`` (declared on the dashboard) or, when
    a column is not declared, against the shape of the value.
    """
    filters = filters or {}
    parameters = parameters or {}
    filter_types = filter_types or {}
    params: dict[str, Any] = {}
    base = sql.strip().rstrip(";").strip()

    unresolved: list[str] = []

    def _param(m: re.Match) -> str:
        name = m.group(1)
        if name in parameters:
            params[f"p_{name}"] = _scalar(parameters[name], f"Parameter '{name}'")
            return f"$p_{name}"
        unresolved.append(name)
        return "NULL"

    base = _PARAM_RE.sub(_param, base)
    if unresolved:
        logger.debug("Dashboard query had unresolved parameters: %s", unresolved)

    conditions: list[str] = []
    for idx, (col, raw_value) in enumerate(filters.items()):
        if not isinstance(col, str) or not _IDENT_RE.match(col):
            continue
        ftype = filter_types.get(col) or _infer_type(raw_value)
        value = normalize_filter_value(ftype, raw_value, col)
        if value is None:
            continue
        cond = _filter_condition(col, ftype, value, idx, params)
        if cond:
            conditions.append(cond)

    if not conditions:
        return base, params

    predicate = " AND ".join(f"({c})" for c in conditions)
    try:
        import sqlglot
        from sqlglot import exp

        parsed = sqlglot.parse_one(base, read="duckdb")
        if isinstance(parsed, exp.Select):
            return parsed.where(predicate, dialect="duckdb", copy=True).sql(dialect="duckdb"), params
    except Exception:
        logger.debug("Could not parse widget SQL for filter injection; wrapping", exc_info=True)
    # Trailing newline so a final `-- comment` cannot swallow the wrapper.
    return f"SELECT * FROM ({base}\n) AS _w WHERE {predicate}", params


def run_widget_query(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    identity: QueryIdentity,
    *,
    filters: dict | None = None,
    parameters: dict | None = None,
    filter_types: dict[str, str] | None = None,
    timeout_s: float | None = None,
    row_cap: int = WIDGET_ROW_CAP,
) -> dict:
    """Run one widget's saved SQL through the governed read path.

    Returns ``{columns, column_types, rows, row_count, truncated}``.
    Raises :class:`~havn.engine.governed_query.GovernedQueryError` or
    :class:`FilterValueError`.
    """
    if not sql or not sql.strip():
        return {"columns": [], "column_types": [], "rows": [], "row_count": 0, "truncated": False}
    final_sql, params = build_widget_sql(sql, filters, parameters, filter_types)
    result = run_governed_query(
        conn,
        final_sql,
        identity,
        params=params or None,
        timeout_s=timeout_s if timeout_s is not None else DEFAULT_WIDGET_TIMEOUT_S,
        row_cap=row_cap,
        task_label=f"widget:{identity.source}",
    )
    return {
        "columns": result["columns"],
        "column_types": result["column_types"],
        "rows": result["rows"],
        "row_count": result["row_count"],
        "truncated": result["truncated"],
    }


# ---------------------------------------------------------------------------
# Freshness and KPI helpers
# ---------------------------------------------------------------------------


def widget_table_refs(sql: str) -> list[str]:
    """Schema-qualified tables a widget's SQL reads (lowercased)."""
    if not sql:
        return []
    try:
        from havn.engine.sql_analysis import extract_table_refs

        return [r.lower() for r in extract_table_refs(_PARAM_RE.sub("NULL", sql))]
    except Exception:
        return []


def dashboard_freshness(conn: duckdb.DuckDBPyConnection, widgets: list[dict]) -> dict:
    """Last build time of the models the dashboard's widgets read.

    Returns ``{"as_of": <oldest last build>, "newest": ..., "models": [{name,
    last_built_at}], "unknown": [tables with no build record]}``. ``as_of`` is
    the oldest build, because the dashboard is only as fresh as its stalest
    input.
    """
    refs: set[str] = set()
    for w in widgets:
        if runs_query(w):
            refs.update(widget_table_refs(w.get("sql_query") or ""))
    if not refs:
        return {"as_of": None, "newest": None, "models": [], "unknown": []}
    built: dict[str, Any] = {}
    try:
        placeholders = ", ".join("?" for _ in refs)
        for name, last_run in conn.execute(
            f"SELECT model_path, last_run_at FROM _havn.model_state WHERE model_path IN ({placeholders})",
            sorted(refs),
        ).fetchall():
            built[name] = last_run
    except Exception:
        logger.debug("model_state lookup failed", exc_info=True)
    models = [
        {"name": name, "last_built_at": _iso(built[name])}
        for name in sorted(built)
    ]
    times = [built[n] for n in built if built[n] is not None]
    return {
        "as_of": _iso(min(times)) if times else None,
        "newest": _iso(max(times)) if times else None,
        "models": models,
        "unknown": sorted(refs - set(built)),
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    return str(value)


def kpi_value(result: dict, column: str | None = None) -> tuple[str | None, Any]:
    """The headline value of a result: ``column`` (or the first numeric one) of row one."""
    columns = result.get("columns") or []
    rows = result.get("rows") or []
    if not columns or not rows:
        return None, None
    first = rows[0]
    if column and column in columns:
        return column, first[columns.index(column)]
    for i, v in enumerate(first):
        if _as_number(v) is not None:
            return columns[i], v
    return columns[0], first[0]


def _as_number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def validate_viewer_inputs(
    dashboard: dict, filters: dict | None, parameters: dict | None
) -> tuple[dict, dict, dict[str, str]]:
    """Keep only the dashboard's declared filters and parameters, validated.

    Used where the caller is not trusted to pick columns: published links and
    scheduled reports. Returns ``(filters, parameters, filter_types)``.
    Parameters not supplied take their declared default. Raises
    :class:`FilterValueError` for an undeclared filter or parameter, or a
    value that does not fit its declared type.
    """
    types = declared_filter_types(dashboard.get("filters"))
    clean_filters: dict = {}
    for col, value in (filters or {}).items():
        if col not in types:
            raise FilterValueError(f"'{col}' is not a filter on this dashboard")
        v = normalize_filter_value(types[col], value, col)
        if v is not None:
            clean_filters[col] = v
    declared = declared_parameters(dashboard.get("settings"))
    for name in parameters or {}:
        if name not in declared:
            raise FilterValueError(f"'{name}' is not a parameter on this dashboard")
    clean_params: dict = {}
    for name, p in declared.items():
        value = (parameters or {}).get(name, p.get("default"))
        clean_params[name] = _scalar(value, f"Parameter '{name}'")
    return clean_filters, clean_params, types
