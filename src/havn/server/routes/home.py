"""Home page: pipeline health in one call.

Aggregates what the Overview, Quality, Sentinel and History panels each show
separately into the four things the Home page needs: health tiles, a ranked
"needs attention" queue, the last 24 hours of runs, and the models per layer
with their status. Read-only throughout.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Request

from havn.server.deps import (
    DbConnReadOnlyOptional,
    _discover_models_cached,
    _get_config,
    _get_db_path,
    _get_project_dir,
    _require_permission,
    ensure_meta_table,
)

logger = logging.getLogger("havn.server")

router = APIRouter()

_META_SCHEMAS = ("_havn", "information_schema", "main", "pg_catalog")
_LAYER_ORDER = {"landing": 0, "bronze": 1, "silver": 2, "gold": 3}
# Ranking for the attention queue: errors first, then by kind, newest first.
_KIND_ORDER = {"build": 0, "assertion": 1, "contract": 2, "freshness": 3, "anomaly": 4}
ATTENTION_LIMIT = 25


def _ts(v: Any) -> str | None:
    return str(v) if v is not None else None


def _rows(conn, sql: str, params: list | None = None) -> list[tuple]:
    """Run a metadata query; a missing table (older warehouse) reads as empty."""
    try:
        return conn.execute(sql, params or []).fetchall()
    except Exception as e:
        logger.debug("home: query skipped: %s", e)
        return []


@router.get("/api/home")
def get_home(request: Request, conn: DbConnReadOnlyOptional = None) -> dict:
    """Health tiles, attention queue, last-24h runs and layer status."""
    _require_permission(request, "read")
    from havn.engine.backup import list_backups
    from havn.engine.selectors import _changed_models
    from havn.engine.transform import failing_rows_sql

    config = _get_config()
    project_dir = _get_project_dir()
    models = _discover_models_cached(project_dir / "transform")
    by_name = {m.full_name: m for m in models}

    def rel(m) -> str | None:
        try:
            return str(m.path.relative_to(project_dir))
        except ValueError:
            return None

    result: dict[str, Any] = {
        "project_name": config.name,
        "is_sample": config.sample,
        "has_data": False,
        "tiles": {
            "models": {"total": len(models), "changed": 0, "never_built": 0, "up_to_date": 0},
            "checks": {"passed": 0, "failed": 0, "warned": 0, "contracts_failed": 0},
            "last_run": None,
            "warehouse": {"size_bytes": None, "last_backup": None},
        },
        "attention": [],
        "runs": [],
        "layers": [],
    }
    tiles = result["tiles"]

    try:
        db_path = _get_db_path()
        if db_path.exists():
            tiles["warehouse"]["size_bytes"] = os.path.getsize(db_path)
    except Exception:
        pass
    try:
        backups = [b for b in list_backups(project_dir) if b.get("exists")]
        if backups:
            last = max(backups, key=lambda b: b.get("timestamp") or "")
            tiles["warehouse"]["last_backup"] = {
                "timestamp": last.get("timestamp"),
                "verified": bool(last.get("verified")),
            }
    except Exception as e:
        logger.debug("home: backups skipped: %s", e)

    if conn is None:
        # No warehouse yet: every model is unbuilt, nothing else to report.
        tiles["models"]["never_built"] = len(models)
        result["layers"] = _layers(models, {}, {}, set(), rel)
        return result

    ensure_meta_table(conn)

    result["has_data"] = bool(_rows(
        conn,
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_catalog = current_database() "
        f"AND table_schema NOT IN {_META_SCHEMAS} AND NOT starts_with(table_schema, '__') "
        "LIMIT 1",
    ))

    # --- Model state -------------------------------------------------------
    state = {
        r[0]: {"last_run_at": _ts(r[1]), "row_count": r[2]}
        for r in _rows(conn, "SELECT model_path, last_run_at, row_count FROM _havn.model_state")
    }
    try:
        changed = _changed_models(conn, list(models))
    except Exception as e:
        logger.debug("home: change detection skipped: %s", e)
        changed = {}
    for m in models:
        if m.full_name not in state:
            tiles["models"]["never_built"] += 1
        elif changed.get(m.full_name):
            tiles["models"]["changed"] += 1
        else:
            tiles["models"]["up_to_date"] += 1

    attention: list[dict] = []

    # --- Assertions: latest result per (model, expression) -----------------
    failing_models: set[str] = set()
    for model_path, expr, passed, detail, severity, checked_at in _rows(
        conn,
        """
        SELECT model_path, expression, passed, detail, COALESCE(severity, 'error'), checked_at
        FROM _havn.assertion_results
        QUALIFY row_number() OVER (
            PARTITION BY model_path, expression ORDER BY checked_at DESC
        ) = 1
        """,
    ):
        if model_path not in by_name:
            continue  # model or check since removed
        declared = {e for e, _ in by_name[model_path].assertion_specs} | set(by_name[model_path].assertions)
        if by_name[model_path].grain:
            declared.add(f"grain({', '.join(by_name[model_path].grain)})")
        if expr not in declared:
            continue  # the check was edited or deleted since it last ran
        if passed:
            tiles["checks"]["passed"] += 1
            continue
        sev = "warn" if severity == "warn" else "error"
        tiles["checks"]["warned" if sev == "warn" else "failed"] += 1
        if sev == "error":
            failing_models.add(model_path)
        model = by_name[model_path]
        attention.append({
            "kind": "assertion",
            "severity": sev,
            "title": f"Check failed in {model_path}",
            "subject": model_path,
            "detail": f"{expr} · {detail}" if detail else expr,
            "at": _ts(checked_at),
            "path": rel(model),
            "sql": failing_rows_sql(model, expr),
        })

    # --- Builds and scripts: latest run per target that errored -------------
    for target, run_type, error, started_at in _rows(
        conn,
        """
        SELECT target, run_type, error, started_at FROM (
            SELECT target, run_type, status, error, started_at,
                   row_number() OVER (PARTITION BY target ORDER BY started_at DESC) AS rn
            FROM _havn.run_log
        ) WHERE rn = 1 AND status IN ('error', 'failed')
        """,
    ):
        # A failed check already has its own, more specific entry.
        if (error or "").startswith("assertion failed:"):
            continue
        model = by_name.get(target)
        if model is not None:
            failing_models.add(target)
        attention.append({
            "kind": "build",
            "severity": "error",
            "title": f"{'Build' if run_type == 'transform' else (run_type or 'Run').capitalize()} failed: {target}",
            "subject": target,
            "detail": (error or "").splitlines()[0][:300] if error else "",
            "at": _ts(started_at),
            "path": rel(model) if model is not None else (target if "/" in (target or "") else None),
            "sql": None,
        })

    # Models whose latest run was skipped because an upstream failed.
    blocked = {
        r[0] for r in _rows(
            conn,
            """
            SELECT target FROM (
                SELECT target, status, error,
                       row_number() OVER (PARTITION BY target ORDER BY started_at DESC) AS rn
                FROM _havn.run_log WHERE run_type = 'transform'
            ) WHERE rn = 1 AND status = 'skipped' AND starts_with(error, 'upstream blocked')
            """,
        )
    }

    # --- Contracts: latest result per contract ------------------------------
    for name, model_name, passed, severity, detail, checked_at in _rows(
        conn,
        """
        SELECT contract_name, model, passed, severity, detail, checked_at
        FROM _havn.contract_results
        QUALIFY row_number() OVER (PARTITION BY contract_name ORDER BY checked_at DESC) = 1
        """,
    ):
        if passed:
            continue
        sev = "warn" if severity == "warn" else "error"
        tiles["checks"]["contracts_failed"] += 1
        model = by_name.get(model_name)
        attention.append({
            "kind": "contract",
            "severity": sev,
            "title": f"Contract broken: {name}",
            "subject": model_name,
            "detail": _contract_detail(detail),
            "at": _ts(checked_at),
            "path": rel(model) if model is not None else None,
            "sql": None,
        })

    # --- Source freshness: latest check per (model, source) that was stale --
    for model_path, table, age, max_age, severity, error, checked_at in _rows(
        conn,
        """
        SELECT model_path, source_table, age_seconds, max_age_seconds, severity, error, checked_at FROM (
            SELECT *, row_number() OVER (
                PARTITION BY model_path, source_table ORDER BY checked_at DESC
            ) AS rn
            FROM _havn.source_freshness
        ) WHERE rn = 1 AND is_stale
        """,
    ):
        model = by_name.get(model_path)
        if error:
            detail = error
        elif age is not None and max_age:
            detail = f"last load {_hours(age)} ago, expected within {_hours(max_age)} · read by {model_path}"
        else:
            detail = f"read by {model_path}"
        attention.append({
            "kind": "freshness",
            "severity": "warn" if severity == "warn" else "error",
            "title": f"Source late: {table}",
            "subject": table,
            "detail": detail,
            "at": _ts(checked_at),
            "path": rel(model) if model is not None else None,
            "sql": None,
        })

    # --- Anomalies: latest per (model, metric) in the last 7 days ----------
    for model_name, metric, message, z, detected_at in _rows(
        conn,
        """
        SELECT model_name, metric, message, z_score, detected_at FROM (
            SELECT *, row_number() OVER (
                PARTITION BY model_name, metric ORDER BY detected_at DESC
            ) AS rn
            FROM _havn.anomaly_log
            WHERE detected_at >= now() - INTERVAL 7 DAY
        ) WHERE rn = 1
        """,
    ):
        model = by_name.get(model_name)
        attention.append({
            "kind": "anomaly",
            "severity": "warn",
            "title": f"Unusual {metric.replace('_', ' ')} in {model_name}",
            "subject": model_name,
            "detail": f"{message} (z={z:.1f})" if z is not None else message,
            "at": _ts(detected_at),
            "path": rel(model) if model is not None else None,
            "sql": None,
        })

    attention.sort(key=lambda a: a["at"] or "", reverse=True)
    attention.sort(key=lambda a: (a["severity"] != "error", _KIND_ORDER.get(a["kind"], 9)))
    result["attention"] = attention[:ATTENTION_LIMIT]
    result["attention_total"] = len(attention)

    # --- Runs: pipeline runs in the last 24 hours, oldest first ------------
    runs = [
        {
            "pipeline_run_id": r[0],
            "target": r[1],
            "started_at": _ts(r[2]),
            "status": r[3],
            "duration_ms": r[4],
            "model_count": r[5],
            "error_count": r[6],
            "rows": r[7],
        }
        for r in _rows(
            conn,
            """
            SELECT
                pipeline_run_id,
                MIN(target),
                MIN(started_at) AS started,
                CASE WHEN SUM(CASE WHEN status IN ('error', 'failed') THEN 1 ELSE 0 END) > 0
                     THEN 'failed' ELSE 'success' END,
                SUM(duration_ms),
                COUNT(*),
                SUM(CASE WHEN status IN ('error', 'failed') THEN 1 ELSE 0 END),
                SUM(rows_affected)
            FROM _havn.run_log
            WHERE pipeline_run_id IS NOT NULL
            GROUP BY pipeline_run_id
            HAVING MIN(started_at) >= now() - INTERVAL 24 HOUR
            ORDER BY started DESC
            LIMIT 48
            """,
        )
    ]
    result["runs"] = list(reversed(runs))
    last = _rows(
        conn,
        """
        SELECT
            pipeline_run_id, MIN(target), MIN(started_at) AS started,
            CASE WHEN SUM(CASE WHEN status IN ('error', 'failed') THEN 1 ELSE 0 END) > 0
                 THEN 'failed' ELSE 'success' END,
            SUM(duration_ms), COUNT(*),
            SUM(CASE WHEN status IN ('error', 'failed') THEN 1 ELSE 0 END),
            SUM(rows_affected)
        FROM _havn.run_log
        WHERE pipeline_run_id IS NOT NULL
        GROUP BY pipeline_run_id
        ORDER BY started DESC
        LIMIT 1
        """,
    )
    if last:
        r = last[0]
        tiles["last_run"] = {
            "pipeline_run_id": r[0], "target": r[1], "started_at": _ts(r[2]),
            "status": r[3], "duration_ms": r[4], "model_count": r[5],
            "error_count": r[6], "rows": r[7],
        }

    # --- Layers ------------------------------------------------------------
    landing = [
        r[0] for r in _rows(
            conn,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_catalog = current_database() AND table_schema = 'landing' "
            "ORDER BY table_name",
        )
    ]
    result["layers"] = _layers(models, state, changed, failing_models, rel, landing, blocked)
    return result


def _layers(models, state, changed, failing, rel, landing: list[str] | None = None,
            blocked: set[str] | None = None) -> list[dict]:
    layers: dict[str, list[dict]] = {}
    if landing:
        layers["landing"] = [
            {"name": t, "full_name": f"landing.{t}", "path": None, "status": "source",
             "materialized": "source", "last_run_at": None, "row_count": None}
            for t in landing
        ]
    for m in models:
        st = state.get(m.full_name)
        if m.full_name in failing:
            status = "failing"
        elif blocked and m.full_name in blocked:
            status = "blocked"
        elif st is None:
            status = "never_built"
        elif changed.get(m.full_name):
            status = "changed"
        else:
            status = "fresh"
        layers.setdefault(m.schema, []).append({
            "name": m.name,
            "full_name": m.full_name,
            "path": rel(m),
            "status": status,
            "materialized": m.materialized,
            "last_run_at": st["last_run_at"] if st else None,
            "row_count": st["row_count"] if st else None,
        })
    rank = {"failing": 0, "blocked": 1, "changed": 2, "never_built": 3, "fresh": 4, "source": 5}
    return [
        {"schema": schema, "models": sorted(items, key=lambda i: (rank[i["status"]], i["name"]))}
        for schema, items in sorted(
            layers.items(), key=lambda kv: (_LAYER_ORDER.get(kv[0], 99), kv[0])
        )
    ]


def _hours(seconds: float) -> str:
    h = seconds / 3600
    if h < 1:
        return f"{max(1, round(seconds / 60))}m"
    if h < 48:
        return f"{h:.0f}h"
    return f"{h / 24:.0f}d"


def _contract_detail(detail: Any) -> str:
    """One line from a contract result's JSON detail."""
    import json

    if detail is None:
        return ""
    try:
        parsed = json.loads(detail) if isinstance(detail, str) else detail
    except Exception:
        return str(detail)[:300]
    if isinstance(parsed, list):
        failed = [
            d for d in parsed
            if isinstance(d, dict) and d.get("passed") is False
        ]
        if failed:
            return "; ".join(
                f"{d.get('assertion') or d.get('expression') or d.get('name', '')} {d.get('detail', '')}".strip()
                for d in failed[:3]
            )[:300]
    if isinstance(parsed, dict):
        return str(parsed.get("detail") or parsed.get("message") or "")[:300]
    return str(parsed)[:300]
