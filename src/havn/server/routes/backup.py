"""Backup and restore API endpoints."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from havn.server.deps import _get_db_path, _get_project_dir, _require_permission

logger = logging.getLogger("havn.server")

router = APIRouter()


def _validate_backup_path(path_str: str, project_dir: Path) -> Path:
    """Validate that a backup path resolves within the project directory."""
    resolved = Path(path_str).resolve()
    project_resolved = project_dir.resolve()
    if not str(resolved).startswith(str(project_resolved)):
        raise HTTPException(403, "Backup path must be within the project directory")
    return resolved


class BackupRequest(BaseModel):
    note: str = ""
    verify: bool = True


class RestoreRequest(BaseModel):
    backup_path: str
    verify: bool = True


@router.post("/api/backup")
def create_backup_endpoint(request: Request, req: BackupRequest) -> dict:
    """Create a verified backup of the warehouse database."""
    _require_permission(request, "write")

    from havn.engine.backup import BackupError, create_backup

    project_dir = _get_project_dir()
    db_path = _get_db_path()

    try:
        entry = create_backup(
            project_dir, db_path,
            verify=req.verify,
            note=req.note,
        )
        return entry
    except BackupError as e:
        raise HTTPException(400, str(e))


@router.get("/api/backups")
def list_backups_endpoint(request: Request) -> list[dict]:
    """List all tracked backups."""
    _require_permission(request, "read")

    from havn.engine.backup import list_backups

    project_dir = _get_project_dir()
    return list_backups(project_dir)


@router.post("/api/backup/verify")
def verify_backup_endpoint(request: Request, path: str) -> dict:
    """Verify a backup file's integrity."""
    _require_permission(request, "read")

    from havn.engine.backup import verify_backup

    project_dir = _get_project_dir()
    safe_path = _validate_backup_path(path, project_dir)
    result = verify_backup(safe_path)
    return result


@router.post("/api/backup/restore")
def restore_backup_endpoint(request: Request, req: RestoreRequest) -> dict:
    """Restore the warehouse from a backup."""
    _require_permission(request, "write")

    from havn.engine.backup import BackupError, restore_backup
    from havn.server.deps import reset_shared_conn

    project_dir = _get_project_dir()
    db_path = _get_db_path()
    safe_path = _validate_backup_path(req.backup_path, project_dir)

    # Reset connections before restore
    reset_shared_conn()

    try:
        result = restore_backup(
            project_dir, db_path, safe_path,
            verify=req.verify,
        )
        return result
    except BackupError as e:
        raise HTTPException(400, str(e))


@router.post("/api/backup/cleanup")
def cleanup_backups_endpoint(request: Request, keep: int = 10) -> dict:
    """Remove old backups, keeping the N most recent."""
    _require_permission(request, "write")
    if keep < 1:
        raise HTTPException(400, "keep must be at least 1")

    from havn.engine.backup import cleanup_backups

    project_dir = _get_project_dir()
    removed = cleanup_backups(project_dir, keep=keep)
    return {"removed": len(removed), "entries": removed}
