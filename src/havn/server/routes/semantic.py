"""Semantic layer endpoints: list, compile, and query declared metrics."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from havn.engine.masking_rewriter import MaskedColumnAccessError
from havn.engine.semantic import (
    SemanticError,
    compile_metric,
    get_metric,
    load_metrics,
)
from havn.engine.sql_safety import ReadOnlyQueryError
from havn.server.deps import (
    DbConnReadOnly,
    _get_project_dir,
    _require_permission,
)

logger = logging.getLogger("havn.server")

router = APIRouter()

_DEFAULT_ROW_CAP = 10_000


class MetricQueryRequest(BaseModel):
    metric: str = Field(..., min_length=1)
    dimensions: list[str] | None = None
    grain: str | None = None
    start: str | None = None
    end: str | None = None
    limit: int | None = Field(default=None, gt=0, le=50_000)


@router.get("/api/semantic/metrics")
def list_metrics_endpoint(request: Request) -> dict:
    """List all declared metrics plus any definition-load errors."""
    _require_permission(request, "read")
    metrics, errors = load_metrics(_get_project_dir())
    return {
        "metrics": [m.to_dict() for m in metrics.values()],
        "errors": errors,
    }


@router.post("/api/semantic/compile")
def compile_metric_endpoint(request: Request, req: MetricQueryRequest) -> dict:
    """Compile a metric query to SQL without executing it."""
    _require_permission(request, "read")
    sql = _compile_or_400(req)
    return {"metric": req.metric, "sql": sql}


@router.post("/api/semantic/query")
def query_metric_endpoint(request: Request, req: MetricQueryRequest, conn: DbConnReadOnly) -> dict:
    """Compile and execute a metric query, honoring masking policies."""
    user = _require_permission(request, "read")
    sql = _compile_or_400(req)

    # The compiler only emits SELECTs, but the measure and filter expressions
    # come from project YAML: run it through the same governed read path as
    # /api/query (read-only validation, masking before and after, timeout).
    from havn.engine.read_path import QueryTimeoutError, run_read_query

    cap = req.limit or _DEFAULT_ROW_CAP
    try:
        result = run_read_query(conn, sql, user=user, limit=cap + 1)
    except ReadOnlyQueryError as e:
        raise HTTPException(e.status_code, str(e))
    except MaskedColumnAccessError as e:
        raise HTTPException(403, str(e))
    except QueryTimeoutError as e:
        raise HTTPException(408, str(e))
    except Exception as e:
        logger.warning("Metric query failed (%s): %s", req.metric, e)
        raise HTTPException(400, str(e))

    rows = result.rows
    truncated = len(rows) > cap
    rows = rows[:cap]
    return {
        "metric": req.metric,
        "sql": sql,
        "columns": result.columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }


def _compile_or_400(req: MetricQueryRequest) -> str:
    try:
        metric = get_metric(_get_project_dir(), req.metric)
        return compile_metric(
            metric,
            dimensions=req.dimensions,
            grain=req.grain,
            start=req.start,
            end=req.end,
            limit=req.limit,
        )
    except SemanticError as e:
        raise HTTPException(400, str(e))
