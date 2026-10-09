"""Verified agent changes: submit, verify, apply and discard change sets.

A change set is a proposed edit to model files that has to pass verification
(validate, bind, unit tests, contracts, a scratch build and a data diff)
before it is "ready to apply". Every endpoint needs ``write``: a change set
carries proposed source and data diff samples of the models it touches.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from havn.server.deps import (
    DbConnReadOnlyOptional,
    _get_config,
    _get_project_dir,
    _require_permission,
)

logger = logging.getLogger("havn.server")

router = APIRouter()


class ProposedFile(BaseModel):
    path: str = Field(..., min_length=1, max_length=500)
    content: str | None = Field(default=None, max_length=1_000_000)
    delete: bool = False


class SubmitRequest(BaseModel):
    files: list[ProposedFile] = Field(..., min_length=1, max_length=100)
    title: str = Field(default="", max_length=200)
    source: str = Field(default="api", max_length=80)
    change_set_id: str | None = Field(default=None, max_length=32)
    verify: bool = True


class ApplyRequest(BaseModel):
    force: bool = False


def _store_errors(fn, *args, **kwargs):
    from havn.engine.changesets import ChangeSetConflict, ChangeSetError

    try:
        return fn(*args, **kwargs)
    except ChangeSetConflict as e:
        raise HTTPException(409, {"message": str(e), "paths": e.paths})
    except ChangeSetError as e:
        status = 404 if "not found" in str(e) else 400
        raise HTTPException(status, str(e))


def _audit(request: Request, user: dict, action: str, resource: str, detail: str | None = None) -> None:
    try:
        from havn.engine.audit import log_audit
        from havn.server.deps import _get_shared_conn

        log_audit(
            _get_shared_conn(),
            user=user.get("username", "anonymous"),
            action=action,
            resource=resource,
            detail=detail,
            ip_address=request.client.host if request.client else None,
        )
    except Exception:
        logger.debug("Failed to write audit log for %s", action, exc_info=True)


def verify_now(cs_id: str, conn, role: str):
    from havn.engine.changesets.service import verify_and_store

    return verify_and_store(_get_project_dir(), cs_id, conn=conn, config=_get_config(), role=role)


@router.get("/api/changesets")
def list_changesets(request: Request, open: bool = False, limit: int = 50) -> dict:
    _require_permission(request, "write")
    from havn.engine.changesets import list_change_sets

    project_dir = _get_project_dir()
    items = list_change_sets(project_dir, include_closed=not open, limit=max(1, min(limit, 200)))
    return {"changesets": [cs.to_dict(project_dir, include_content=False) for cs in items]}


@router.get("/api/changesets/{cs_id}")
def get_changeset(request: Request, cs_id: str) -> dict:
    _require_permission(request, "write")
    from havn.engine.changesets import get_change_set

    project_dir = _get_project_dir()
    return _store_errors(get_change_set, project_dir, cs_id).to_dict(project_dir)


@router.post("/api/changesets")
def submit_changeset(request: Request, req: SubmitRequest, conn: DbConnReadOnlyOptional) -> dict:
    """Create (or revise) a change set and verify it. Returns it with its report."""
    user = _require_permission(request, "write")
    from havn.engine.changesets import create_change_set, revise_change_set

    project_dir = _get_project_dir()
    proposals = [
        {"path": f.path, "content": None if f.delete else f.content} for f in req.files
    ]
    if req.change_set_id:
        cs = _store_errors(revise_change_set, project_dir, req.change_set_id, proposals,
                           title=req.title or None)
    else:
        cs = _store_errors(create_change_set, project_dir, proposals,
                           source=req.source or "api", title=req.title)
    _audit(request, user, "changeset_submit", cs.id, ", ".join(f.path for f in cs.files)[:500])
    if req.verify:
        cs = verify_now(cs.id, conn, user.get("role", "viewer"))
    return cs.to_dict(project_dir)


@router.post("/api/changesets/{cs_id}/verify")
def reverify_changeset(request: Request, cs_id: str, conn: DbConnReadOnlyOptional) -> dict:
    user = _require_permission(request, "write")
    from havn.engine.changesets import get_change_set

    project_dir = _get_project_dir()
    _store_errors(get_change_set, project_dir, cs_id)
    return verify_now(cs_id, conn, user.get("role", "viewer")).to_dict(project_dir)


@router.post("/api/changesets/{cs_id}/apply")
def apply_changeset(request: Request, cs_id: str, req: ApplyRequest | None = None) -> dict:
    """Write the change set's files into the project (all or nothing)."""
    user = _require_permission(request, "write")
    from havn.engine.changesets import apply_change_set

    project_dir = _get_project_dir()
    force = bool(req and req.force)
    cs = _store_errors(apply_change_set, project_dir, cs_id, force=force,
                       user=user.get("username"))
    _audit(request, user, "changeset_apply", cs_id,
           ("forced; " if force else "") + ", ".join(f.path for f in cs.files)[:480])
    return cs.to_dict(project_dir)


@router.post("/api/changesets/{cs_id}/discard")
def discard_changeset(request: Request, cs_id: str) -> dict:
    user = _require_permission(request, "write")
    from havn.engine.changesets import discard_change_set

    project_dir = _get_project_dir()
    cs = _store_errors(discard_change_set, project_dir, cs_id)
    _audit(request, user, "changeset_discard", cs_id)
    return cs.to_dict(project_dir)
