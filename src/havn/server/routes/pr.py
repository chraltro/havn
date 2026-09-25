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
from havn.engine.write_queue import cursor_for

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
            cursor = cursor_for(_get_shared_conn())
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
    from havn.engine.transform.discovery import build_dag, discover_all_models

    project_dir = _get_project_dir()
    pr = get_pr(project_dir, pr_id)
    if pr is None:
        raise HTTPException(404, f"PR '{pr_id}' not found")
    files = diff_files_between(project_dir, pr.base_ref, pr.head_ref)
    # Packages are part of the DAG, so a package model that reads a changed
    # project model is part of the impact.
    dag = build_dag(discover_all_models(project_dir))
    return _compute_lineage_impact(files, dag, project_dir)


# --- Ship: one review of a change, with its merge gate ---


@router.get("/api/prs/{pr_id}/review")
def pr_review_endpoint(pr_id: str, request: Request, conn: DbConnReadOnly):
    """Everything the Ship page shows for one change, in one call.

    Combines the PR, its changed files, the lineage impact (with the edges
    between the affected models so the page can draw them), the latest build
    and its data diff, and a gate: the checks ``merge_pr`` enforces, marked
    ``required``, plus whether a build of the branch's current head passed.
    """
    _require_permission(request, "read")
    from havn.engine.git import diff_files_between
    from havn.engine.pr import (
        _compute_lineage_impact,
        _run_git,
        can_merge,
        ensure_pr_builds_table,
        get_latest_build,
        get_pr,
        is_dirty,
        PR_STATE_PREFIXES,
    )
    from havn.engine.transform.discovery import discover_all_models

    project_dir = _get_project_dir()
    pr = get_pr(project_dir, pr_id)
    if pr is None:
        raise HTTPException(404, f"PR '{pr_id}' not found")

    files = diff_files_between(project_dir, pr.base_ref, pr.head_ref)
    models = discover_all_models(project_dir)
    by_name = {m.full_name: m for m in models}

    ensure_pr_builds_table(conn)
    build = get_latest_build(conn, pr_id)

    # A finished build knows the PR branch's own DAG (new models included);
    # without one, compute the impact from the base checkout.
    impact = (build or {}).get("lineage_impact") or _compute_lineage_impact(
        files, models, project_dir
    )
    changed = list(impact.get("changed") or [])
    impacted = list(impact.get("impacted") or [])
    upstream = sorted({
        dep
        for name in changed
        for dep in (by_name[name].depends_on if name in by_name else [])
        if dep not in changed and dep not in impacted
    })
    in_graph = set(changed) | set(impacted) | set(upstream)
    edges = sorted({
        (dep, m.full_name)
        for m in models
        if m.full_name in in_graph
        for dep in m.depends_on
        if dep in in_graph and m.full_name not in upstream
    })

    def rel(name: str) -> str | None:
        m = by_name.get(name)
        if m is None:
            return None
        try:
            return str(m.path.relative_to(project_dir))
        except ValueError:
            return None

    nodes = (
        [{"name": n, "role": "upstream", "path": rel(n)} for n in upstream]
        + [{"name": n, "role": "changed", "path": rel(n)} for n in changed]
        + [{"name": n, "role": "impacted", "path": rel(n)} for n in impacted]
    )

    head_sha = None
    res = _run_git(project_dir, "rev-parse", pr.head_ref)
    if res.returncode == 0:
        head_sha = res.stdout.strip() or None

    gate: list[dict] = []

    def check(key: str, label: str, state: str, detail: str, required: bool) -> None:
        gate.append({"key": key, "label": label, "state": state, "detail": detail, "required": required})

    # Build: advisory (merge does not require it) but shown first.
    if build is None:
        check("build", "Built and checked", "pending",
              "Not built yet. A build runs every model on the branch and its checks.", False)
    elif build.get("status") == "running":
        check("build", "Built and checked", "pending", "Build running…", False)
    elif build.get("status") == "error":
        check("build", "Built and checked", "fail", build.get("error") or "Build failed", False)
    elif head_sha and build.get("branch_head") and build["branch_head"] != head_sha:
        check("build", "Built and checked", "warn",
              f"Built {build['branch_head'][:7]}; the branch is now at {head_sha[:7]}. Rebuild to check the latest commit.",
              False)
    else:
        check("build", "Built and checked", "pass",
              f"Every model built and every error-level check passed ({build.get('duration_ms') or 0} ms).",
              False)

    if pr.status != "open":
        check("open", "Open", "fail", f"This change is {pr.status}.", True)

    if pr.change_requesters:
        check("changes", "No changes requested", "fail",
              f"Changes requested by {', '.join(pr.change_requesters)}.", True)
    else:
        check("changes", "No changes requested", "pass", "No reviewer has requested changes.", True)

    if not pr.require_approval:
        check("approval", "Approved", "pass", "This change does not require approval.", True)
    elif pr.approvers:
        others = [a for a in pr.approvers if a != pr.author]
        note = "" if others else " (by its author)"
        check("approval", "Approved", "pass", f"Approved by {', '.join(pr.approvers)}{note}.", True)
    else:
        check("approval", "Approved", "pending", "Needs at least one approval.", True)

    mc = can_merge(project_dir, pr) if pr.status == "open" else {"can_merge": False, "reason": None}
    if pr.status == "open":
        if mc.get("can_merge"):
            check("conflicts", "Merges cleanly", "pass", f"No conflicts with {pr.base_ref}.", True)
        else:
            check("conflicts", "Merges cleanly", "fail", mc.get("reason") or "Cannot merge.", True)

    dirty = is_dirty(project_dir, ignore=PR_STATE_PREFIXES)
    check("clean", "Working tree clean", "fail" if dirty else "pass",
          "Commit or stash local changes first; merging checks out the base branch."
          if dirty else "No uncommitted changes.", True)

    ready = all(g["state"] == "pass" for g in gate if g["required"])

    return {
        "pr": pr.to_dict(),
        "files": files,
        "head_sha": head_sha,
        "impact": {"nodes": nodes, "edges": [list(e) for e in edges]},
        "build": build,
        "gate": gate,
        "ready": ready,
        "build_current": gate[0]["state"] == "pass",
        # What POST /api/prs/{id}/merge does, in order, and what it leaves to you.
        "plan": [
            "Snapshot the warehouse (undo with `havn version restore`)",
            f"Check out {pr.base_ref} and merge {pr.head_ref} with --no-ff",
            "Mark the change merged and switch back to your branch",
        ],
        "after_merge": "Merging changes the code, not the data: run the pipeline "
                       f"on {pr.base_ref} to rebuild the changed models.",
    }
