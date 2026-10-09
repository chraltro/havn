"""Branch warehouse routes: the current branch's data, its build and its diff."""

from __future__ import annotations

import logging
import threading

import duckdb
from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, Field

from havn.server.deps import (
    _get_backend,
    _get_config,
    _get_write_queue,
    _require_permission,
    branch_sync_state,
    sync_branch_warehouse,
)

logger = logging.getLogger("havn.server")

router = APIRouter()

# One branch build at a time per server: two would race on the same file.
_build_lock = threading.Lock()


class BranchBuildRequest(BaseModel):
    force: bool = False
    prune: bool = True
    plan: bool = False


class BranchDiffRequest(BaseModel):
    models: list[str] | None = Field(default=None, max_length=500)
    full: bool = False


def branch_payload(config) -> dict:
    """What every branch response carries: the resolution and the server's sync state."""
    from havn.engine.branches import branch_summary

    return {**branch_summary(config), "server": branch_sync_state()}


def _cursor_or_memory():
    """A cursor on the branch warehouse, or an empty connection before it exists."""
    if _get_backend().exists():
        return _get_write_queue().cursor()
    return duckdb.connect()


@router.get("/api/branch")
def get_branch(request: Request) -> dict:
    """The checked-out branch and its warehouse. Cheap: the UI polls it."""
    _require_permission(request, "read")
    sync_branch_warehouse(force=True)
    return branch_payload(_get_config())


@router.get("/api/branch/status")
def get_branch_status(request: Request) -> dict:
    """What is built on the branch, what is read from the base, what is stale."""
    _require_permission(request, "read")
    from havn.engine.branches import branch_status

    config = _get_config()
    if not config.branch.active:
        return branch_payload(config)
    conn = _cursor_or_memory()
    try:
        info = branch_status(conn, config)
    finally:
        conn.close()
    return {**info, "server": branch_sync_state()}


@router.post("/api/branch/build")
def post_branch_build(
    request: Request,
    req: BranchBuildRequest = Body(default_factory=BranchBuildRequest),
) -> dict:
    """Build what the branch changed, deferring everything else to the base."""
    _require_permission(request, "execute")
    from havn.engine.branches import BranchError, build_branch
    from havn.engine.defer import DeferError

    config = _get_config()
    if not config.branch.active:
        raise HTTPException(400, f"Not on a branch warehouse: {config.branch.reason}")
    if not _build_lock.acquire(blocking=False):
        raise HTTPException(409, "A branch build is already running")
    try:
        cursor = _get_write_queue().cursor()
        try:
            result = build_branch(
                cursor, config, force=req.force, prune=req.prune, dry_run=req.plan,
            )
        finally:
            cursor.close()
    except (BranchError, DeferError) as e:
        raise HTTPException(400, str(e)) from e
    finally:
        _build_lock.release()
    return result


@router.post("/api/branch/diff")
def post_branch_diff(
    request: Request,
    req: BranchDiffRequest = Body(default_factory=BranchDiffRequest),
) -> dict:
    """Row-level and schema diff of the branch's models against the base."""
    user = _require_permission(request, "read")
    from havn.engine.branches import BranchError, diff_branch, format_markdown
    from havn.engine.defer import DeferError
    from havn.server.deps import _governed_relation
    from havn.server.routes.models import _samples_withheld

    config = _get_config()
    if not config.branch.active:
        raise HTTPException(400, f"Not on a branch warehouse: {config.branch.reason}")
    conn = _cursor_or_memory()
    try:
        report = diff_branch(conn, config, models=req.models, full=req.full)
        # Sample rows are raw model output (the diff runs ungoverned), so,
        # like /api/diff, they are withheld from a user that masking or row
        # policies apply to on that model -- in the JSON and the markdown.
        for entry in report.get("models", []):
            withheld = _samples_withheld(user, conn, entry.get("model"), _governed_relation)
            entry["samples_withheld"] = withheld
            if withheld:
                entry["sample_added"] = []
                entry["sample_removed"] = []
                entry["sample_modified"] = []
    except (BranchError, DeferError) as e:
        raise HTTPException(400, str(e)) from e
    finally:
        conn.close()
    return {**report, "markdown": format_markdown(report)}


@router.get("/api/branch/list")
def get_branch_list(request: Request) -> list[dict]:
    """Branch warehouses on disk and the git state of each branch."""
    _require_permission(request, "read")
    from havn.engine.branches import list_branch_warehouses

    return list_branch_warehouses(_get_config())
