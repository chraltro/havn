"""Git operations endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from havn.server.deps import _get_project_dir, _require_permission

router = APIRouter()


# --- Pydantic models ---


class StageRequest(BaseModel):
    files: list[str] = Field(..., min_length=1)


class UnstageRequest(BaseModel):
    files: list[str] = Field(..., min_length=1)


class CommitRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=5000)


class PullRequest(BaseModel):
    remote: str = "origin"
    branch: str | None = None


class PushRequest(BaseModel):
    remote: str = "origin"
    branch: str | None = None


class CreateBranchRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=250)
    checkout: bool = True


class CheckoutRequest(BaseModel):
    branch: str = Field(..., min_length=1, max_length=250)


class StashRequest(BaseModel):
    message: str | None = None


class DiscardRequest(BaseModel):
    files: list[str] = Field(..., min_length=1)


# --- Read endpoints ---


@router.get("/api/git/status")
def get_git_status(request: Request) -> dict:
    """Get git status for the project (branch, dirty, changed files)."""
    _require_permission(request, "read")
    try:
        from havn.engine.git import (
            changed_files,
            current_branch,
            git_status_detailed,
            is_dirty,
            is_git_repo,
            last_commit_hash,
            last_commit_message,
        )

        project_dir = _get_project_dir()
        if not is_git_repo(project_dir):
            return {"is_git_repo": False}

        return {
            "is_git_repo": True,
            "branch": current_branch(project_dir),
            "dirty": is_dirty(project_dir),
            "changed_files": changed_files(project_dir),
            "files": git_status_detailed(project_dir),
            "last_commit": last_commit_hash(project_dir),
            "last_message": last_commit_message(project_dir),
        }
    except Exception:
        return {"is_git_repo": False}


@router.get("/api/git/log")
def get_git_log(request: Request, limit: int = 20) -> list[dict]:
    """Get commit history."""
    _require_permission(request, "read")
    from havn.engine.git import git_log, is_git_repo

    project_dir = _get_project_dir()
    if not is_git_repo(project_dir):
        return []
    return git_log(project_dir, limit=limit)


@router.get("/api/git/diff")
def get_git_diff(request: Request, file: str | None = None, staged: bool = False) -> dict:
    """Get diff text."""
    _require_permission(request, "read")
    from havn.engine.git import git_diff, is_git_repo

    project_dir = _get_project_dir()
    if not is_git_repo(project_dir):
        return {"diff": ""}
    return {"diff": git_diff(project_dir, file=file, staged=staged)}


@router.get("/api/git/branches")
def get_git_branches(request: Request) -> list[dict]:
    """List branches."""
    _require_permission(request, "read")
    from havn.engine.git import git_branches, is_git_repo

    project_dir = _get_project_dir()
    if not is_git_repo(project_dir):
        return []
    return git_branches(project_dir)


@router.get("/api/git/stash")
def get_git_stash_list(request: Request) -> list[dict]:
    """List stashes."""
    _require_permission(request, "read")
    from havn.engine.git import git_stash_list, is_git_repo

    project_dir = _get_project_dir()
    if not is_git_repo(project_dir):
        return []
    return git_stash_list(project_dir)


@router.get("/api/git/remote")
def get_git_remote(request: Request) -> dict:
    """Get remote URL."""
    _require_permission(request, "read")
    from havn.engine.git import git_remote_url, is_git_repo

    project_dir = _get_project_dir()
    if not is_git_repo(project_dir):
        return {"url": None}
    return {"url": git_remote_url(project_dir)}


# --- Write endpoints ---


@router.post("/api/git/stage")
def post_git_stage(request: Request, req: StageRequest) -> dict:
    """Stage files."""
    _require_permission(request, "write")
    from havn.engine.git import git_stage, is_git_repo

    project_dir = _get_project_dir()
    if not is_git_repo(project_dir):
        raise HTTPException(400, "Not a git repository")
    success = git_stage(project_dir, req.files)
    if not success:
        raise HTTPException(500, "Failed to stage files")
    return {"status": "staged", "files": req.files}


@router.post("/api/git/unstage")
def post_git_unstage(request: Request, req: UnstageRequest) -> dict:
    """Unstage files."""
    _require_permission(request, "write")
    from havn.engine.git import git_unstage, is_git_repo

    project_dir = _get_project_dir()
    if not is_git_repo(project_dir):
        raise HTTPException(400, "Not a git repository")
    success = git_unstage(project_dir, req.files)
    if not success:
        raise HTTPException(500, "Failed to unstage files")
    return {"status": "unstaged", "files": req.files}


@router.post("/api/git/commit")
def post_git_commit(request: Request, req: CommitRequest) -> dict:
    """Create a commit."""
    _require_permission(request, "write")
    from havn.engine.git import git_commit, is_git_repo

    project_dir = _get_project_dir()
    if not is_git_repo(project_dir):
        raise HTTPException(400, "Not a git repository")
    result = git_commit(project_dir, req.message)
    if result is None:
        raise HTTPException(500, "Failed to create commit. Are there staged changes?")
    return result


@router.post("/api/git/pull")
def post_git_pull(request: Request, req: PullRequest) -> dict:
    """Pull from remote."""
    _require_permission(request, "write")
    from havn.engine.git import git_pull, is_git_repo

    project_dir = _get_project_dir()
    if not is_git_repo(project_dir):
        raise HTTPException(400, "Not a git repository")
    return git_pull(project_dir, remote=req.remote, branch=req.branch)


@router.post("/api/git/push")
def post_git_push(request: Request, req: PushRequest) -> dict:
    """Push to remote."""
    _require_permission(request, "write")
    from havn.engine.git import git_push, is_git_repo

    project_dir = _get_project_dir()
    if not is_git_repo(project_dir):
        raise HTTPException(400, "Not a git repository")
    return git_push(project_dir, remote=req.remote, branch=req.branch)


@router.post("/api/git/branch")
def post_git_create_branch(request: Request, req: CreateBranchRequest) -> dict:
    """Create a new branch."""
    _require_permission(request, "write")
    from havn.engine.git import git_create_branch, is_git_repo

    project_dir = _get_project_dir()
    if not is_git_repo(project_dir):
        raise HTTPException(400, "Not a git repository")
    success = git_create_branch(project_dir, req.name, checkout=req.checkout)
    if not success:
        raise HTTPException(400, f"Failed to create branch: {req.name}")
    return {"status": "created", "name": req.name, "checked_out": req.checkout}


@router.post("/api/git/checkout")
def post_git_checkout(request: Request, req: CheckoutRequest) -> dict:
    """Checkout a branch."""
    _require_permission(request, "write")
    from havn.engine.git import git_checkout_branch, is_git_repo

    project_dir = _get_project_dir()
    if not is_git_repo(project_dir):
        raise HTTPException(400, "Not a git repository")
    result = git_checkout_branch(project_dir, req.branch)
    if not result["success"]:
        raise HTTPException(400, result.get("error", "Failed to checkout branch"))
    return {"status": "checked_out", "branch": req.branch}


@router.delete("/api/git/branch")
def delete_git_branch(request: Request, name: str) -> dict:
    """Delete a branch."""
    _require_permission(request, "write")
    from havn.engine.git import git_delete_branch, is_git_repo

    project_dir = _get_project_dir()
    if not is_git_repo(project_dir):
        raise HTTPException(400, "Not a git repository")
    result = git_delete_branch(project_dir, name)
    if not result["success"]:
        raise HTTPException(400, result.get("error", "Failed to delete branch"))
    return {"status": "deleted", "name": name}


@router.post("/api/git/stash")
def post_git_stash(request: Request, req: StashRequest) -> dict:
    """Stash changes."""
    _require_permission(request, "write")
    from havn.engine.git import git_stash, is_git_repo

    project_dir = _get_project_dir()
    if not is_git_repo(project_dir):
        raise HTTPException(400, "Not a git repository")
    result = git_stash(project_dir, message=req.message)
    if not result["success"]:
        raise HTTPException(500, "Failed to stash changes")
    return result


@router.post("/api/git/stash/pop")
def post_git_stash_pop(request: Request) -> dict:
    """Pop the latest stash."""
    _require_permission(request, "write")
    from havn.engine.git import git_stash_pop, is_git_repo

    project_dir = _get_project_dir()
    if not is_git_repo(project_dir):
        raise HTTPException(400, "Not a git repository")
    result = git_stash_pop(project_dir)
    if not result["success"]:
        raise HTTPException(400, result.get("error", "Failed to pop stash"))
    return result


@router.post("/api/git/discard")
def post_git_discard(request: Request, req: DiscardRequest) -> dict:
    """Discard working directory changes for specific files."""
    _require_permission(request, "write")
    from havn.engine.git import git_discard_changes, is_git_repo

    project_dir = _get_project_dir()
    if not is_git_repo(project_dir):
        raise HTTPException(400, "Not a git repository")
    success = git_discard_changes(project_dir, req.files)
    if not success:
        raise HTTPException(500, "Failed to discard changes")
    return {"status": "discarded", "files": req.files}
