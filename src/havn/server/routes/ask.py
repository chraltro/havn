"""Ask the warehouse: questions answered through the semantic layer."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from havn.server.deps import (
    DbConnReadOnlyOptional,
    _discover_models_cached,
    _get_config,
    _get_project_dir,
    _require_permission,
)

logger = logging.getLogger("havn.server")

router = APIRouter()


class AskHistoryTurn(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    spec: dict | None = None


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    history: list[AskHistoryTurn] = Field(default_factory=list, max_length=10)
    exploratory: bool = False
    summarize: bool = False


class AcceptMetricRequest(BaseModel):
    definition: dict
    path: str | None = None


class AskEvalRequest(BaseModel):
    paths: list[str] = Field(default_factory=lambda: ["tests/ask/*.yml"], max_length=50)


def _ai_config():
    from havn.engine.ai.config import AIConfigError, parse_ai_config

    config = _get_config()
    try:
        return parse_ai_config((getattr(config, "_raw", None) or {}).get("ai"))
    except AIConfigError as e:
        raise HTTPException(400, str(e))


def _context(request: Request, conn, user: dict):
    from havn.engine.ai import providers
    from havn.engine.ai.ask import AskContext
    from havn.engine.ai.config import AIConfigError

    ai = _ai_config()
    try:
        provider = providers.provider_from_config(ai)
    except AIConfigError as e:
        raise HTTPException(400, str(e))
    project_dir = _get_project_dir()
    try:
        models = _discover_models_cached(project_dir / "transform")
    except Exception as e:
        logger.debug("Model discovery failed for ask: %s", e)
        models = []
    return AskContext(
        project_dir=project_dir,
        conn=conn,
        user=user,
        provider=provider,
        ai=ai,
        project_config=_get_config(),
        models=list(models),
    )


@router.get("/api/ask/status")
def ask_status(request: Request) -> dict:
    """Whether Ask is configured, and with which provider (never the key)."""
    _require_permission(request, "read")
    from havn.engine.ai.config import AIConfigError, parse_ai_config
    from havn.engine.ai.providers import provider_from_config
    from havn.engine.semantic import load_metrics

    metrics, errors = load_metrics(_get_project_dir())
    out: dict = {"metrics": len(metrics), "metric_errors": errors}
    try:
        ai = parse_ai_config((getattr(_get_config(), "_raw", None) or {}).get("ai"))
    except AIConfigError as e:
        return {**out, "configured": False, "error": str(e)}
    out.update(ai.public_dict())
    try:
        provider_from_config(ai)
        out["configured"] = True
        out["error"] = None
    except AIConfigError as e:
        out["configured"] = False
        out["error"] = str(e)
    return out


@router.post("/api/ask")
def ask_endpoint(request: Request, req: AskRequest, conn: DbConnReadOnlyOptional) -> dict:
    """Answer a question from the metric catalog, as the requesting user."""
    user = _require_permission(request, "read")
    from havn.engine.ai.ask import ask

    ctx = _context(request, conn, user)
    try:
        from havn.engine.audit import log_audit
        from havn.server.deps import _get_shared_conn

        log_audit(
            _get_shared_conn(),
            user=user.get("username", "anonymous"),
            action="ask",
            resource=req.question[:500],
            ip_address=request.client.host if request.client else None,
        )
    except Exception:
        logger.debug("Failed to write audit log for ask", exc_info=True)

    return ask(
        req.question,
        ctx,
        history=[t.model_dump() for t in req.history],
        exploratory=req.exploratory,
        summarize=req.summarize,
    )


@router.post("/api/ask/accept-metric")
def accept_metric(request: Request, req: AcceptMetricRequest) -> dict:
    """Save a suggested metric definition into metrics/."""
    _require_permission(request, "write")
    from havn.engine.ai.service import accept_suggested_metric

    try:
        path = accept_suggested_metric(_get_project_dir(), req.definition, req.path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"path": path}


@router.post("/api/ask/eval")
def ask_eval(request: Request, req: AskEvalRequest, conn: DbConnReadOnlyOptional) -> dict:
    """Run question -> spec eval cases from the project (planning only)."""
    user = _require_permission(request, "read")
    from havn.engine.ai.evaluate import expand_paths, load_cases, run_eval

    project_dir = _get_project_dir().resolve()
    files = []
    for f in expand_paths(req.paths, project_dir):
        try:
            f.resolve().relative_to(project_dir)
        except ValueError:
            raise HTTPException(400, f"eval files must be inside the project: {f}")
        files.append(f)
    cases, errors = load_cases(files)
    if not cases:
        raise HTTPException(400, "No eval cases found" + (f": {'; '.join(errors)}" if errors else ""))
    report = run_eval(cases, _context(request, conn, user))
    report["load_errors"] = errors
    report["files"] = [f.resolve().relative_to(project_dir).as_posix() for f in files]
    return report
