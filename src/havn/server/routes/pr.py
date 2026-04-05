"""Pull request API routes."""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from havn.server.deps import (
    DbConn,
    DbConnReadOnly,
    _get_project_dir,
    _require_permission,
    ensure_meta_table,
)

logger = logging.getLogger("havn.server")
router = APIRouter(tags=["pr"])


# --- Pydantic models ---


class CreatePrRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=250)
    description: str = Field(default="", max_length=10000)
    base_ref: str = Field(..., min_length=1, max_length=250)
    head_ref: str = Field(..., min_length=1, max_length=250)
    author: str = Field(default="local", max_length=100)
    require_approval: bool = True


class UpdatePrRequest(BaseModel):
    title: str | None = Field(default=None, max_length=250)
    description: str | None = Field(default=None, max_length=10000)
    require_approval: bool | None = None


class CommentRequest(BaseModel):
    body: str = Field(..., min_length=1, max_length=10000)
    author: str = Field(default="local", max_length=100)
    comment_type: str = Field(default="human", pattern="^(human|ai_review)$")
    file: str | None = Field(default=None, max_length=500)
    line: int | None = None


class ReviewerActionRequest(BaseModel):
    reviewer: str = Field(default="local", max_length=100)
    reason: str = Field(default="", max_length=2000)


class MergeRequest(BaseModel):
    user: str = Field(default="local", max_length=100)


class CloseRequest(BaseModel):
    user: str = Field(default="local", max_length=100)


# --- Helpers ---


def _pr_to_dict(pr) -> dict:
    return pr.to_dict()


# --- PR lifecycle ---


# --- State status (registered before /api/prs/{pr_id} so it isn't shadowed) ---


@router.get("/api/prs/state-status")
def pr_state_status_endpoint(request: Request):
    _require_permission(request, "read")
    from havn.engine.pr import pr_state_status

    project_dir = _get_project_dir()
    return pr_state_status(project_dir)


@router.get("/api/prs")
def list_prs_endpoint(
    request: Request,
    status: str | None = None,
):
    _require_permission(request, "read")
    from havn.engine.pr import list_prs

    project_dir = _get_project_dir()
    prs = list_prs(project_dir, status=status)
    return [_pr_to_dict(p) for p in prs]


@router.post("/api/prs")
def create_pr_endpoint(req: CreatePrRequest, request: Request):
    _require_permission(request, "write")
    from havn.engine.pr import create_pr

    project_dir = _get_project_dir()
    try:
        pr = create_pr(
            project_dir,
            title=req.title,
            description=req.description,
            base_ref=req.base_ref,
            head_ref=req.head_ref,
            author=req.author,
            require_approval=req.require_approval,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _pr_to_dict(pr)


@router.get("/api/prs/{pr_id}")
def get_pr_endpoint(pr_id: str, request: Request):
    _require_permission(request, "read")
    from havn.engine.pr import get_pr

    project_dir = _get_project_dir()
    pr = get_pr(project_dir, pr_id)
    if pr is None:
        raise HTTPException(404, f"PR '{pr_id}' not found")
    return _pr_to_dict(pr)


@router.patch("/api/prs/{pr_id}")
def update_pr_endpoint(pr_id: str, req: UpdatePrRequest, request: Request):
    _require_permission(request, "write")
    from havn.engine.pr import update_pr

    project_dir = _get_project_dir()
    try:
        pr = update_pr(
            project_dir,
            pr_id,
            title=req.title,
            description=req.description,
            require_approval=req.require_approval,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _pr_to_dict(pr)


@router.post("/api/prs/{pr_id}/close")
def close_pr_endpoint(pr_id: str, req: CloseRequest, request: Request):
    _require_permission(request, "write")
    from havn.engine.pr import close_pr

    project_dir = _get_project_dir()
    try:
        pr = close_pr(project_dir, pr_id, req.user)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _pr_to_dict(pr)


# --- Comments / review ---


@router.get("/api/prs/{pr_id}/comments")
def list_comments_endpoint(pr_id: str, request: Request):
    _require_permission(request, "read")
    from havn.engine.pr import get_pr

    project_dir = _get_project_dir()
    pr = get_pr(project_dir, pr_id)
    if pr is None:
        raise HTTPException(404, f"PR '{pr_id}' not found")
    return [c.to_dict() for c in pr.comments]


@router.post("/api/prs/{pr_id}/comments")
def add_comment_endpoint(pr_id: str, req: CommentRequest, request: Request):
    _require_permission(request, "write")
    from havn.engine.pr import add_comment

    project_dir = _get_project_dir()
    try:
        comment = add_comment(
            project_dir,
            pr_id,
            author=req.author,
            body=req.body,
            comment_type=req.comment_type,
            file=req.file,
            line=req.line,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return comment.to_dict()


@router.post("/api/prs/{pr_id}/approve")
def approve_pr_endpoint(pr_id: str, req: ReviewerActionRequest, request: Request):
    _require_permission(request, "write")
    from havn.engine.pr import approve_pr

    project_dir = _get_project_dir()
    try:
        pr = approve_pr(project_dir, pr_id, req.reviewer)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _pr_to_dict(pr)


@router.post("/api/prs/{pr_id}/request-changes")
def request_changes_endpoint(pr_id: str, req: ReviewerActionRequest, request: Request):
    _require_permission(request, "write")
    from havn.engine.pr import request_changes

    project_dir = _get_project_dir()
    try:
        pr = request_changes(project_dir, pr_id, req.reviewer, reason=req.reason)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _pr_to_dict(pr)


# --- Build ---


@router.post("/api/prs/{pr_id}/build")
def build_pr_endpoint(pr_id: str, request: Request, conn: DbConn):
    _require_permission(request, "execute")
    from havn.engine.pr import build_pr, get_pr

    project_dir = _get_project_dir()
    if get_pr(project_dir, pr_id) is None:
        raise HTTPException(404, f"PR '{pr_id}' not found")

    # Run build on a dedicated cursor in a background thread so the HTTP
    # request returns immediately and long transforms don't block API reads
    def _run():
        from havn.server.deps import _get_shared_conn

        cursor = None
        try:
            cursor = _get_shared_conn().cursor()
            build_pr(project_dir, pr_id, cursor)
        except Exception as e:
            logger.error("PR build '%s' failed: %s", pr_id, e)
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return {"status": "started", "pr_id": pr_id}


@router.get("/api/prs/{pr_id}/build")
def get_latest_build_endpoint(pr_id: str, request: Request, conn: DbConnReadOnly):
    _require_permission(request, "read")
    from havn.engine.pr import ensure_pr_builds_table, get_latest_build

    ensure_pr_builds_table(conn)
    build = get_latest_build(conn, pr_id)
    if build is None:
        return {"pr_id": pr_id, "status": "none"}
    return build


# --- Merge ---


@router.post("/api/prs/{pr_id}/merge")
def merge_pr_endpoint(pr_id: str, req: MergeRequest, request: Request, conn: DbConn):
    _require_permission(request, "execute")
    from havn.engine.pr import merge_pr

    project_dir = _get_project_dir()
    result = merge_pr(project_dir, pr_id, req.user, conn)
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "merge failed"))
    return result


# --- Review prompt ---


@router.get("/api/prs/{pr_id}/review-prompt")
def review_prompt_endpoint(pr_id: str, request: Request, conn: DbConnReadOnly):
    _require_permission(request, "read")
    from fastapi.responses import PlainTextResponse

    from havn.engine.pr import (
        build_review_prompt,
        ensure_pr_builds_table,
        get_latest_build,
        get_pr,
    )

    project_dir = _get_project_dir()
    pr = get_pr(project_dir, pr_id)
    if pr is None:
        raise HTTPException(404, f"PR '{pr_id}' not found")
    ensure_pr_builds_table(conn)
    build = get_latest_build(conn, pr_id)
    prompt = build_review_prompt(project_dir, pr, build=build)
    return PlainTextResponse(prompt)


# --- Diff / lineage impact ---


@router.get("/api/prs/{pr_id}/diff")
def pr_diff_endpoint(pr_id: str, request: Request):
    _require_permission(request, "read")
    from havn.engine.git import diff_files_between
    from havn.engine.pr import get_pr

    project_dir = _get_project_dir()
    pr = get_pr(project_dir, pr_id)
    if pr is None:
        raise HTTPException(404, f"PR '{pr_id}' not found")
    files = diff_files_between(project_dir, pr.base_ref, pr.head_ref)
    return {"files": files, "base_ref": pr.base_ref, "head_ref": pr.head_ref}


@router.get("/api/prs/{pr_id}/lineage-impact")
def pr_lineage_impact_endpoint(pr_id: str, request: Request):
    _require_permission(request, "read")
    from havn.engine.git import diff_files_between
    from havn.engine.pr import _compute_lineage_impact, get_pr
    from havn.engine.transform.discovery import build_dag, discover_models

    project_dir = _get_project_dir()
    pr = get_pr(project_dir, pr_id)
    if pr is None:
        raise HTTPException(404, f"PR '{pr_id}' not found")
    files = diff_files_between(project_dir, pr.base_ref, pr.head_ref)
    dag = build_dag(discover_models(project_dir / "transform"))
    return _compute_lineage_impact(files, dag, project_dir)
