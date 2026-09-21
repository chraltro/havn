"""Column rename endpoints: find references, plan the edits, apply them.

Three steps, deliberately separate, because the interesting part of a rename
is what it refuses to do. ``/api/rename/references`` answers "where is this
column written" and is safe to call while someone is only reading. ``/plan``
adds the splices and the resulting file contents without touching disk.
``/apply`` is the only one that writes, and it writes every file or none,
rejecting the whole batch when a file changed since the plan was made.

The first two need read permission; the last needs write.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from havn.server.deps import (
    _discover_models_cached,
    _get_project_dir,
    _require_permission,
)

logger = logging.getLogger("havn.server")

router = APIRouter()


# --- Pydantic models ---


class RenamePlanRequest(BaseModel):
    model: str = Field(..., max_length=500)
    column: str = Field(..., max_length=500)
    new_name: str = Field(..., max_length=500)
    force: bool = False


class RenameApplyRequest(RenamePlanRequest):
    # Per-file hashes as the caller last read them. A path that is missing
    # here is written without a check, which is what a caller that never read
    # the file wants.
    hashes: dict[str, str] = Field(default_factory=dict)


# --- Helpers ---


def _models():
    return _discover_models_cached(_get_project_dir() / "transform")


def _schemas(models) -> dict[str, list[tuple[str, str]]]:
    """Column names per model, best source first.

    The bind pass is not run here: it costs a shadow catalog per call and the
    rename walk only needs names, so the columns recorded at the last build
    are used, and the models' own projections fill the gaps.
    """
    from havn.engine.rename import schemas_from_models
    from havn.engine.transform.columns import load_model_columns
    from havn.server.deps import _get_read_pool

    found = schemas_from_models(models)
    try:
        with _get_read_pool().connection() as conn:
            for model in models:
                persisted = load_model_columns(conn, model.full_name)
                if persisted:
                    found[model.full_name] = [
                        (c["name"], c.get("type", "")) for c in persisted
                    ]
    except Exception:
        logger.debug("Could not read persisted columns for the rename index")
    return found


def _site_payload(site) -> dict:
    return {
        "model": site.model,
        "path": site.path,
        "line": site.line,
        "col": site.col,
        "start": site.start,
        "end": site.end,
        "clause": site.clause,
        "kind": site.kind,
        "resolved": site.resolved,
        "text": site.text,
        "needs_alias": site.needs_alias,
    }


def _blocker_payload(blocker) -> dict:
    return {
        "reason": blocker.reason,
        "model": blocker.model,
        "path": blocker.path,
        "message": blocker.message,
        "line": blocker.line,
    }


def _report(model: str, column: str):
    from havn.engine.rename import RenameError, find_column_references

    models = _models()
    schemas = _schemas(models)
    try:
        report = find_column_references(
            models,
            model,
            column,
            schemas=schemas,
            project_dir=_get_project_dir(),
        )
    except RenameError as e:
        raise HTTPException(404, str(e))
    return report, schemas


# --- Endpoints ---


@router.get("/api/rename/references")
def rename_references(
    request: Request,
    model: str = Query(..., max_length=500),
    column: str = Query(..., max_length=500),
) -> dict:
    """Every place a model's column is written, plus what could not be seen."""
    _require_permission(request, "read")
    report, _schema_map = _report(model, column)
    return {
        "model": report.target,
        "column": report.column,
        "sites": [_site_payload(s) for s in report.sites],
        "blocked": [_blocker_payload(b) for b in report.blocked],
        "models": report.models,
    }


@router.post("/api/rename/plan")
def rename_plan(request: Request, req: RenamePlanRequest) -> dict:
    """The splices a rename would make, and the file contents they produce.

    Nothing is written. ``files`` carries each touched file's new content and
    the hash of its content *as it is now*, which ``/apply`` takes back as the
    conflict check.
    """
    _require_permission(request, "read")
    from havn.engine.rename import RenameError, apply_rename, plan_rename
    from havn.server.routes.files import _file_hash

    report, schemas = _report(req.model, req.column)
    payload = {
        "model": report.target,
        "column": report.column,
        "new_name": req.new_name,
        "sites": [_site_payload(s) for s in report.sites],
        "blocked": [_blocker_payload(b) for b in report.blocked],
        "edits": [],
        "files": [],
    }
    try:
        edits = plan_rename(
            report, req.column, req.new_name, force=req.force, schemas=schemas
        )
    except RenameError as e:
        payload["error"] = str(e)
        return payload

    project_dir = _get_project_dir()
    try:
        contents = apply_rename(project_dir, edits, dry_run=True)
    except RenameError as e:
        payload["error"] = str(e)
        return payload

    payload["edits"] = [
        {
            "path": e.path,
            "start": e.start,
            "end": e.end,
            "old_text": e.old_text,
            "new_text": e.new_text,
            "kind": e.kind,
            "model": e.model,
            "line": e.line,
        }
        for e in edits
    ]
    payload["files"] = [
        {
            "path": path,
            "content": content,
            # The hash of what is on disk now, so /apply can tell whether the
            # file moved under the plan.
            "file_hash": _file_hash((project_dir / path).read_text(encoding="utf-8")),
        }
        for path, content in sorted(contents.items())
    ]
    return payload


@router.post("/api/rename/apply")
def rename_apply(request: Request, req: RenameApplyRequest) -> dict:
    """Apply a rename to every file it touches, or to none of them."""
    user = _require_permission(request, "write")
    from havn.engine.rename import RenameError, apply_rename, plan_rename
    from havn.server.routes.files import BatchFileWrite, check_write_conflicts

    report, schemas = _report(req.model, req.column)
    try:
        edits = plan_rename(
            report, req.column, req.new_name, force=req.force, schemas=schemas
        )
    except RenameError as e:
        raise HTTPException(400, str(e))
    if not edits:
        raise HTTPException(400, f"Nothing references {req.model}.{req.column}")

    project_dir = _get_project_dir()
    try:
        contents = apply_rename(project_dir, edits, dry_run=True)
    except RenameError as e:
        raise HTTPException(409, str(e))

    # Same conflict check the file editor uses, one entry per file.
    checks = [
        BatchFileWrite(
            path=path, content=body, expected_hash=req.hashes.get(path)
        )
        for path, body in sorted(contents.items())
    ]
    _resolved, conflicts = check_write_conflicts(project_dir, checks)
    if conflicts:
        return JSONResponse(
            status_code=409,
            content={
                "conflict": True,
                "message": "Files were modified since the rename was planned",
                "stale": sorted(conflicts),
                "current_hashes": conflicts,
            },
        )

    try:
        apply_rename(project_dir, edits)
    except RenameError as e:
        raise HTTPException(409, str(e))

    from havn.server.routes.files import _audit_file_action, _file_hash

    for path in sorted(contents):
        _audit_file_action(
            request,
            user,
            "file_edit",
            path,
            detail=f"rename {req.model}.{req.column} to {req.new_name}",
        )
    return {
        "status": "applied",
        "model": report.target,
        "column": report.column,
        "new_name": req.new_name,
        "blocked": [_blocker_payload(b) for b in report.blocked],
        "files": [
            {"path": path, "file_hash": _file_hash(body)}
            for path, body in sorted(contents.items())
        ],
    }
