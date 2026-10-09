"""Performance advisor endpoints: slow models, regressions, advice, plans, critical path."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from havn.server.deps import (
    DbConn,
    DbConnReadOnlyOptional,
    _discover_models_cached,
    _get_config,
    _get_project_dir,
    _require_permission,
)

logger = logging.getLogger("havn.server")

router = APIRouter()


class AdviceStateRequest(BaseModel):
    model: str = Field(..., min_length=1, max_length=300)
    rule: str = Field(..., min_length=1, max_length=64)
    status: str = Field(..., pattern=r"^(dismissed|snoozed|open)$")
    days: float | None = Field(None, gt=0, le=3650)
    note: str | None = Field(None, max_length=1000)


def _perf_config():
    try:
        return _get_config().performance
    except Exception:
        from havn.config import PerformanceConfig

        return PerformanceConfig()


def _models():
    try:
        return list(_discover_models_cached(_get_project_dir() / "transform"))
    except Exception as e:
        logger.debug("Model discovery for perf failed: %s", e)
        return []


def _all_models():
    """Every model in the project, packages included (advice needs consumers)."""
    try:
        from havn.engine.transform.discovery import discover_all_models

        return discover_all_models(_get_project_dir())
    except Exception as e:
        logger.debug("Model discovery for perf failed: %s", e)
        return _models()


def _exposures():
    try:
        return list(_get_config().exposures)
    except Exception:
        return []


def _settings_summary(cfg) -> dict:
    return {
        "enabled": cfg.enabled,
        "capture_plans": cfg.capture_plans,
        "sample_rate": cfg.sample_rate,
        "retention_days": cfg.retention_days,
        "plan_retention": cfg.plan_retention,
        "regression_threshold": cfg.regression_threshold,
        "regression_min_ratio": cfg.regression_min_ratio,
    }


def _enriched(plan: dict | None) -> dict | None:
    if not plan:
        return None
    import copy

    from havn.engine.explain import enrich_plan_dict

    return enrich_plan_dict(copy.deepcopy(plan))


@router.get("/api/perf/summary")
def perf_summary(request: Request, conn: DbConnReadOnlyOptional, days: int = 7) -> dict:
    """Everything the Performance page opens with."""
    _require_permission(request, "read")
    cfg = _perf_config()
    empty = {
        "slowest": [], "regressions": [], "advice": [], "runs": [], "trend": {},
        "settings": _settings_summary(cfg),
    }
    if conn is None:
        return empty
    from havn.engine.perf import compute_advice, list_regressions, recent_runs, slowest_models, trend

    days = max(1, min(int(days), 365))
    slowest = slowest_models(conn, days=days, limit=15)
    top = [s["model_path"] for s in slowest[:6]]
    advice = []
    try:
        advice = [a.to_dict() for a in compute_advice(
            conn, _get_project_dir(), cfg.advice, models=_all_models(), exposures=_exposures(),
        )]
    except Exception as e:
        logger.debug("Advice failed: %s", e)
    return {
        "slowest": slowest,
        "regressions": list_regressions(conn, days=days, limit=30),
        "advice": advice,
        "runs": recent_runs(conn, limit=15),
        "trend": trend(conn, top, days=max(days, 30)),
        "settings": _settings_summary(cfg),
    }


@router.get("/api/perf/slowest")
def perf_slowest(request: Request, conn: DbConnReadOnlyOptional, days: int = 7, limit: int = 20) -> list[dict]:
    _require_permission(request, "read")
    if conn is None:
        return []
    from havn.engine.perf import slowest_models

    return slowest_models(conn, days=max(1, min(days, 365)), limit=max(1, min(limit, 500)))


@router.get("/api/perf/trend")
def perf_trend(request: Request, conn: DbConnReadOnlyOptional, models: str = "", days: int = 30) -> dict:
    _require_permission(request, "read")
    if conn is None:
        return {}
    from havn.engine.perf import trend

    names = [m.strip() for m in models.split(",") if m.strip()][:20]
    return trend(conn, names, days=max(1, min(days, 365)))


@router.get("/api/perf/regressions")
def perf_regressions(
    request: Request, conn: DbConnReadOnlyOptional, days: int = 7, model: str | None = None,
) -> list[dict]:
    _require_permission(request, "read")
    if conn is None:
        return []
    from havn.engine.perf import list_regressions

    return list_regressions(conn, days=max(1, min(days, 365)), model=model)


@router.get("/api/perf/advice")
def perf_advice(
    request: Request,
    conn: DbConnReadOnlyOptional,
    model: str | None = None,
    include_dismissed: bool = False,
) -> list[dict]:
    """Advice items, most severe first. Dismissed / snoozed ones only on request."""
    _require_permission(request, "read")
    if conn is None:
        return []
    from havn.engine.perf import compute_advice

    items = compute_advice(
        conn, _get_project_dir(), _perf_config().advice,
        model=model, include_dismissed=include_dismissed,
        models=_all_models(), exposures=_exposures(),
    )
    return [a.to_dict() for a in items]


@router.post("/api/perf/advice/state")
def perf_advice_state(request: Request, req: AdviceStateRequest, conn: DbConn) -> dict:
    """Dismiss, snooze (``days``) or reopen one advice item."""
    user = _require_permission(request, "write")
    from havn.engine.perf import set_advice_state

    try:
        return set_advice_state(
            conn, req.model, req.rule, req.status,
            days=req.days, note=req.note, user=user.get("username"),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/perf/models/{model}")
def perf_model(request: Request, model: str, conn: DbConnReadOnlyOptional, limit: int = 60) -> dict:
    """One model: build history, its latest captured plan, regressions and advice."""
    _require_permission(request, "read")
    model = model.lower()
    out: dict = {"model": model, "history": [], "plan": None, "plan_build": None,
                 "regressions": [], "advice": []}
    if conn is None:
        return out
    from havn.engine.perf import compute_advice, list_regressions, model_history

    history = model_history(conn, model, limit=max(1, min(limit, 500)), status=None)
    out["history"] = history
    with_plan = next((h for h in history if h.get("plan_captured") and h.get("status") == "success"), None)
    if with_plan is not None:
        from havn.engine.perf import get_build

        full = get_build(conn, with_plan["id"]) or {}
        out["plan"] = _enriched(full.get("plan"))
        out["plan_build"] = {k: v for k, v in full.items() if k != "plan"}
    out["regressions"] = list_regressions(conn, days=365, model=model, limit=20)
    try:
        out["advice"] = [a.to_dict() for a in compute_advice(
            conn, _get_project_dir(), _perf_config().advice, model=model, include_dismissed=True,
            models=_all_models(), exposures=_exposures(),
        )]
    except Exception as e:
        logger.debug("Advice for %s failed: %s", model, e)
    return out


@router.get("/api/perf/builds/{build_id}")
def perf_build(request: Request, build_id: str, conn: DbConnReadOnlyOptional) -> dict:
    """One recorded build with its plan (enriched with per-operator time shares)."""
    _require_permission(request, "read")
    if conn is None:
        raise HTTPException(404, "No warehouse")
    from havn.engine.perf import get_build

    build = get_build(conn, build_id)
    if build is None:
        raise HTTPException(404, f"No build {build_id}")
    build["plan"] = _enriched(build.get("plan"))
    return build


@router.get("/api/perf/diff")
def perf_diff(request: Request, fast: str, slow: str, conn: DbConnReadOnlyOptional) -> dict:
    """Plan diff between two recorded builds of the same model."""
    _require_permission(request, "read")
    if conn is None:
        raise HTTPException(404, "No warehouse")
    from havn.engine.perf import diff_plans, get_build

    a, b = get_build(conn, fast), get_build(conn, slow)
    if a is None or b is None:
        raise HTTPException(404, "Build not found")
    if not a.get("plan") or not b.get("plan"):
        raise HTTPException(409, "Both builds need a captured plan to diff")
    return {
        "fast": {k: v for k, v in a.items() if k != "plan"},
        "slow": {k: v for k, v in b.items() if k != "plan"},
        "diff": diff_plans(a["plan"], b["plan"]),
    }


@router.get("/api/perf/runs")
def perf_runs(request: Request, conn: DbConnReadOnlyOptional, limit: int = 20) -> list[dict]:
    _require_permission(request, "read")
    if conn is None:
        return []
    from havn.engine.perf import recent_runs

    return recent_runs(conn, limit=max(1, min(limit, 200)))


@router.get("/api/perf/runs/{run_id}/critical-path")
def perf_critical_path(request: Request, run_id: str, conn: DbConnReadOnlyOptional) -> dict:
    """Which chain of models set this run's length, and the floor more workers cannot beat."""
    _require_permission(request, "read")
    if conn is None:
        raise HTTPException(404, "No warehouse")
    from havn.engine.perf import critical_path, run_builds

    builds = run_builds(conn, run_id)
    if not builds:
        raise HTTPException(404, f"No builds recorded for run {run_id}")
    deps = {m.full_name: list(m.depends_on) for m in _all_models()}
    result = critical_path(builds, deps)
    result["pipeline_run_id"] = run_id
    return result
