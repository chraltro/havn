"""File browsing and editing endpoints."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from havn.engine.packages import PACKAGES_DIRNAME
from havn.server.deps import _detect_language, _get_project_dir, _require_permission

import logging

logger = logging.getLogger("havn.server")


def _audit_file_action(request: Request, user: dict, action: str, resource: str, detail: str | None = None) -> None:
    """Helper to log a file audit entry without failing the main operation."""
    try:
        from havn.engine.audit import log_audit
        from havn.server.deps import _get_shared_conn

        conn = _get_shared_conn()
        client_ip = request.client.host if request.client else None
        log_audit(
            conn,
            user=user.get("username", "anonymous"),
            action=action,
            resource=resource,
            detail=detail,
            ip_address=client_ip,
        )
    except Exception:
        logger.debug("Failed to write audit log for %s", action, exc_info=True)

router = APIRouter()


# --- Pydantic models ---


class FileInfo(BaseModel):
    name: str
    path: str
    type: str  # "file" or "dir"
    children: list[FileInfo] | None = None
    # Name of the havn package this path belongs to, or None for the
    # project's own files. Set for everything under havn_packages/, which
    # the UI marks so nobody edits a file the next install will overwrite.
    package: str | None = None


class SaveFileRequest(BaseModel):
    content: str = Field(..., max_length=5_000_000)
    expected_hash: str | None = None


class MoveFileRequest(BaseModel):
    destination: str


class BatchFileWrite(BaseModel):
    path: str = Field(..., max_length=1000)
    content: str = Field(..., max_length=5_000_000)
    expected_hash: str | None = None


class BatchSaveRequest(BaseModel):
    files: list[BatchFileWrite] = Field(..., max_length=500)


# --- Helpers ---


_SKIP_DIRS = {
    "__pycache__", "node_modules", ".venv", ".pytest_cache",
    ".havn", "dist", "build",
}


def _file_hash(content: str) -> str:
    """Return a short SHA256 hash (16 hex chars) of file content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _safe_project_path(project_dir: Path, rel_path: str) -> Path:
    """Resolve rel_path under project_dir and reject any traversal.

    Uses Path.is_relative_to (Python 3.9+) which compares path parts, avoiding
    Windows string-prefix bypass where C:\\proj-evil starts with C:\\proj.
    """
    if not rel_path or rel_path.strip() in ("", ".", ".."):
        raise HTTPException(400, "Invalid file path")
    project_root = project_dir.resolve()
    full = (project_dir / rel_path).resolve()
    try:
        full.relative_to(project_root)
    except ValueError:
        raise HTTPException(400, "Invalid file path")
    rel_parts = Path(rel_path).parts
    if any(part.startswith(".") for part in rel_parts):
        raise HTTPException(403, "Access to dotfiles is not allowed")
    return full


def _package_of(rel_path: str) -> str | None:
    """The package a project-relative path belongs to, or None.

    ``havn_packages/crm/transform/x.sql`` -> ``crm``; ``havn_packages`` itself
    -> ``""``, which is falsy but still marks the directory as not the
    project's own.
    """
    parts = Path(rel_path).parts
    if not parts or parts[0] != PACKAGES_DIRNAME:
        return None
    return parts[1] if len(parts) > 1 else ""


def _scan_dir(base: Path, rel: Path | None = None) -> list[FileInfo]:
    """Scan a directory and return file tree."""
    target = base / rel if rel else base
    if not target.exists():
        return []
    items = []
    for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
        if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
            continue
        # Skip DuckDB temp/WAL dirs and binary artifacts
        if ".duckdb" in entry.name:
            continue
        rel_path = str(entry.relative_to(base))
        package = _package_of(rel_path)
        if entry.is_dir():
            items.append(
                FileInfo(
                    name=entry.name,
                    path=rel_path,
                    type="dir",
                    children=_scan_dir(base, entry.relative_to(base)),
                    package=package,
                )
            )
        elif entry.suffix in (".sql", ".py", ".yml", ".yaml", ".dpnb", ".csv"):
            items.append(
                FileInfo(name=entry.name, path=rel_path, type="file", package=package)
            )
    return items


# --- File endpoints ---


@router.get("/api/files")
def list_files(request: Request) -> list[FileInfo]:
    """List project files."""
    _require_permission(request, "read")
    project_dir = _get_project_dir()
    return _scan_dir(project_dir)


WRITABLE_SUFFIXES = (
    ".sql", ".py", ".yml", ".yaml", ".dpnb", ".sqlfluff", ".csv", ".md",
)


def check_write_conflicts(
    project_dir: Path, items: list[BatchFileWrite]
) -> tuple[list[Path], dict[str, str]]:
    """Resolve and vet a batch of writes before any of them happens.

    Returns the resolved paths and, when a file on disk no longer hashes to
    what the caller last saw, the current hashes keyed by path. A caller that
    gets a non-empty conflict map must not write.
    """
    resolved: list[Path] = []
    conflicts: dict[str, str] = {}
    for item in items:
        full = _safe_project_path(project_dir, item.path)
        if full.suffix not in WRITABLE_SUFFIXES:
            raise HTTPException(400, f"Unsupported file type: {full.suffix}")
        resolved.append(full)
        if not item.expected_hash or not full.exists():
            continue
        try:
            current = full.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            current = full.read_text(encoding="latin-1")
        current_hash = _file_hash(current)
        if current_hash != item.expected_hash:
            conflicts[item.path] = current_hash
    return resolved, conflicts


@router.put("/api/files")
def save_files(request: Request, req: BatchSaveRequest) -> dict:
    """Save several files as one unit: all of them land, or none do.

    A refactor that spans files (a column rename, say) leaves a project that
    does not build if half its edits reach disk. Every file is hash-checked
    first, then written through a temporary neighbour, and anything already
    written is put back if a later write fails.
    """
    user = _require_permission(request, "write")
    project_dir = _get_project_dir()
    if not req.files:
        return {"status": "saved", "files": []}

    # Two spellings of one file are still one file: `a/b.sql` and `a/./b.sql`
    # both passed a raw-string check, and the batch then wrote the same path
    # twice, so whichever landed last silently won and its hash check was made
    # against content the other write had already replaced.
    seen: dict[Path, str] = {}
    for item in req.files:
        resolved = _safe_project_path(project_dir, item.path)
        first = seen.get(resolved)
        if first is not None:
            detail = f"Duplicate path in batch: {item.path}"
            if first != item.path:
                detail += f" (same file as {first})"
            raise HTTPException(400, detail)
        seen[resolved] = item.path

    _resolved, conflicts = check_write_conflicts(project_dir, req.files)
    if conflicts:
        return JSONResponse(
            status_code=409,
            content={
                "conflict": True,
                "message": "Files were modified by another user or process",
                "stale": sorted(conflicts),
                "current_hashes": conflicts,
            },
        )

    from havn.engine.rename import RenameError, write_files_atomically

    contents = {item.path: item.content for item in req.files}
    try:
        write_files_atomically(project_dir, contents)
    except RenameError as e:
        raise HTTPException(500, str(e))

    written = []
    for item in req.files:
        _audit_file_action(request, user, "file_edit", item.path)
        written.append({"path": item.path, "file_hash": _file_hash(item.content)})
    return {"status": "saved", "files": written}


@router.get("/api/files/{file_path:path}")
def read_file(request: Request, file_path: str) -> dict:
    """Read a file's content."""
    _require_permission(request, "read")
    project_dir = _get_project_dir()
    full_path = _safe_project_path(project_dir, file_path)
    if not full_path.exists():
        raise HTTPException(404, f"File not found: {file_path}")
    if not full_path.is_file():
        raise HTTPException(400, "Not a file")
    try:
        content = full_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            content = full_path.read_text(encoding="latin-1")
        except Exception:
            raise HTTPException(422, "Cannot read file: unsupported encoding")
    fh = _file_hash(content)
    return JSONResponse(
        content={
            "path": file_path,
            "content": content,
            "language": _detect_language(full_path),
            "file_hash": fh,
        },
        headers={"ETag": f'"{fh}"'},
    )


@router.put("/api/files/{file_path:path}")
def save_file(request: Request, file_path: str, req: SaveFileRequest) -> dict:
    """Save a file (creates it if it doesn't exist)."""
    user = _require_permission(request, "write")
    project_dir = _get_project_dir()
    full_path = _safe_project_path(project_dir, file_path)
    if full_path.suffix not in (".sql", ".py", ".yml", ".yaml", ".dpnb", ".sqlfluff", ".csv", ".md"):
        raise HTTPException(400, f"Unsupported file type: {full_path.suffix}")
    # Conflict detection: if expected_hash is provided and file exists, compare
    if req.expected_hash and full_path.exists():
        try:
            current_content = full_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            current_content = full_path.read_text(encoding="latin-1")
        current_hash = _file_hash(current_content)
        if current_hash != req.expected_hash:
            return JSONResponse(
                status_code=409,
                content={
                    "conflict": True,
                    "message": "File was modified by another user or process",
                    "current_hash": current_hash,
                },
            )
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(req.content, encoding="utf-8")
    _audit_file_action(request, user, "file_edit", file_path)
    new_hash = _file_hash(req.content)
    return JSONResponse(
        content={"path": file_path, "status": "saved", "file_hash": new_hash},
        headers={"ETag": f'"{new_hash}"'},
    )


@router.post("/api/files/{file_path:path}/move")
def move_file(request: Request, file_path: str, req: MoveFileRequest) -> dict:
    """Move/rename a file within the project."""
    _require_permission(request, "write")
    project_dir = _get_project_dir()
    src = _safe_project_path(project_dir, file_path)
    dst = _safe_project_path(project_dir, req.destination)
    if not src.exists():
        raise HTTPException(404, f"File not found: {file_path}")
    if not src.is_file():
        raise HTTPException(400, "Not a file")
    if dst.exists():
        raise HTTPException(409, f"Destination already exists: {req.destination}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    # Remove empty parent directories up to project root
    parent = src.parent
    while parent != project_dir.resolve() and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent
    return {"source": file_path, "destination": req.destination, "status": "moved"}


@router.delete("/api/files/{file_path:path}")
def delete_file(
    request: Request,
    file_path: str,
    drop_object: bool = Query(False),
) -> dict:
    """Delete a file, optionally dropping the corresponding database object."""
    user = _require_permission(request, "write")
    project_dir = _get_project_dir()
    full_path = _safe_project_path(project_dir, file_path)
    if not full_path.exists():
        raise HTTPException(404, f"File not found: {file_path}")
    if not full_path.is_file():
        raise HTTPException(400, "Not a file")
    # Prevent deleting critical files
    if full_path.name in ("project.yml", ".env", ".gitignore"):
        raise HTTPException(400, f"Cannot delete {full_path.name}")

    dropped = None
    if drop_object:
        dropped = _drop_db_object(full_path, file_path)

    full_path.unlink()
    # Remove empty parent directories up to project root
    parent = full_path.parent
    while (
        parent != project_dir.resolve()
        and parent.is_dir()
        and not any(parent.iterdir())
    ):
        parent.rmdir()
        parent = parent.parent
    _audit_file_action(request, user, "file_delete", file_path)
    result: dict = {"path": file_path, "status": "deleted"}
    if dropped:
        result["dropped"] = dropped
    return result


def _drop_db_object(full_path: Path, file_path: str) -> str | None:
    """Drop the DuckDB object corresponding to a transform SQL or seed CSV file."""
    from havn.engine.database import connect
    from havn.engine.utils import validate_identifier
    from havn.server.deps import _get_db_path

    normalized = file_path.replace("\\", "/")

    if full_path.suffix == ".sql" and normalized.startswith("transform/"):
        # Derive schema and name from file path / content
        name = full_path.stem
        # Default schema from folder: transform/<schema>/<name>.sql
        parts = normalized.split("/")
        schema = parts[1] if len(parts) >= 3 else "bronze"
        # Check for -- config: schema=<override> in file content
        try:
            content = full_path.read_text(encoding="utf-8")
            m = re.search(r"--\s*config:.*schema\s*=\s*(\w+)", content)
            if m:
                schema = m.group(1)
        except Exception:
            pass
    elif full_path.suffix == ".csv" and normalized.startswith("seeds/"):
        schema = "seeds"
        name = full_path.stem
    else:
        return None

    try:
        validate_identifier(schema, "schema")
        validate_identifier(name, "table name")
    except ValueError:
        return None

    db_path = _get_db_path()
    if not db_path.exists():
        return None

    conn = connect(db_path)
    try:
        # Look up the object type in information_schema
        rows = conn.execute(
            "SELECT table_type FROM information_schema.tables "
            "WHERE table_catalog = current_database() "
            "AND table_schema = ? AND table_name = ?",
            [schema, name],
        ).fetchall()

        if not rows:
            return None

        table_type = rows[0][0]
        obj_kind = "VIEW" if "VIEW" in table_type.upper() else "TABLE"
        conn.execute(f'DROP {obj_kind} IF EXISTS "{schema}"."{name}"')

        # Clean up model_state metadata. The PK column is model_path, keyed by
        # the model's full_name ("schema.name") — same value discovery writes.
        try:
            conn.execute(
                "DELETE FROM _havn.model_state WHERE model_path = ?",
                [f"{schema}.{name}"],
            )
        except Exception:
            pass  # table may not exist yet (fresh project, never transformed)

        return f"{schema}.{name}"
    finally:
        conn.close()
