"""Verified backup and restore for the warehouse database.

Creates file-level backups with integrity verification, SHA-256
checksums, and a metadata registry for tracking backup history.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from havn.engine.database import connect

logger = logging.getLogger("havn.engine.backup")

BACKUPS_DIR = "_backups"
BACKUP_MANIFEST = "_backups/manifest.json"


# ---------------------------------------------------------------------------
# Manifest (lightweight JSON registry, no DB dependency)
# ---------------------------------------------------------------------------


def _load_manifest(project_dir: Path) -> list[dict]:
    manifest_path = project_dir / BACKUP_MANIFEST
    if manifest_path.exists():
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_manifest(project_dir: Path, entries: list[dict]) -> None:
    manifest_path = project_dir / BACKUP_MANIFEST
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(entries, indent=2, default=str),
        encoding="utf-8",
    )


def _compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# (resolved path, size, mtime_ns) -> sha256. list_backups re-checks every
# file on each call (the dashboard calls it per page load); an unchanged file
# is not re-hashed.
_SHA_CACHE: dict[tuple[str, int, int], str] = {}


def _cached_sha256(path: Path) -> str:
    st = path.stat()
    key = (str(path.resolve()), st.st_size, st.st_mtime_ns)
    sha = _SHA_CACHE.get(key)
    if sha is None:
        sha = _compute_sha256(path)
        _SHA_CACHE[key] = sha
    return sha


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _manifest_entry_for(backup_path: Path, project_dir: Path | None = None) -> dict | None:
    """The manifest entry recorded for ``backup_path``, if any.

    Looks in ``project_dir``'s manifest, then in a manifest beside the backup
    (the default ``_backups/`` layout keeps ``manifest.json`` there).
    """
    sources: list[tuple[Path, list[dict]]] = []
    if project_dir is not None:
        sources.append((project_dir, _load_manifest(project_dir)))
    beside = backup_path.parent / Path(BACKUP_MANIFEST).name
    if beside.exists():
        try:
            sources.append((backup_path.parent.parent, json.loads(beside.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, OSError):
            pass
    for base, entries in sources:
        for entry in reversed(entries):
            recorded = Path(entry.get("path", ""))
            if not recorded.is_absolute():
                recorded = base / recorded
            if _same_file(recorded, backup_path):
                return entry
    return None


_LOCKED_HINT = (
    "the warehouse file is in use by another process "
    "(is `havn serve` or another havn command running?). Stop it and retry."
)


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


class BackupError(Exception):
    """Raised when a backup or restore operation fails."""
    pass


def create_backup(
    project_dir: Path,
    db_path: Path,
    output: Path | None = None,
    verify: bool = True,
    note: str = "",
) -> dict[str, Any]:
    """Create a verified backup of the warehouse database.

    1. CHECKPOINT to flush the WAL
    2. Copy the .duckdb file
    3. Verify the copy with PRAGMA integrity_check (optional)
    4. Compute SHA-256 checksum
    5. Record in manifest

    Returns backup metadata dict.
    """
    if not db_path.exists():
        raise BackupError(f"Database not found: {db_path}")

    # Default: _backups/warehouse-YYYYMMDD_HHMMSS_ffffff.duckdb
    if output is None:
        backups_dir = project_dir / BACKUPS_DIR
        backups_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        stem = db_path.stem
        output = backups_dir / f"{stem}-{ts}.duckdb"

    # Ensure parent directory exists
    output.parent.mkdir(parents=True, exist_ok=True)

    # 1. Flush WAL via CHECKPOINT
    _conn = None
    try:
        _conn = connect(db_path)
        _conn.execute("CHECKPOINT")
    except Exception as e:
        logger.warning("CHECKPOINT skipped (proceeding with copy): %s", e)
    finally:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass

    # 2. Copy
    try:
        shutil.copy2(str(db_path), str(output))
    except PermissionError as e:
        output.unlink(missing_ok=True)
        raise BackupError(f"Cannot read {db_path}: {_LOCKED_HINT} ({e})")
    except OSError as e:
        output.unlink(missing_ok=True)
        raise BackupError(f"Cannot copy {db_path} to {output}: {e}")

    # 3. Verify integrity (open read-only, query metadata)
    verified = False
    if verify:
        try:
            verify_conn = duckdb.connect(str(output), read_only=True)
            # Verify we can read the catalog and count tables
            verify_conn.execute(
                "SELECT COUNT(*) FROM information_schema.tables"
            ).fetchone()
            verify_conn.close()
            verified = True
        except Exception as e:
            output.unlink(missing_ok=True)
            raise BackupError(f"Failed to verify backup: {e}")

    # 4. Checksum
    sha256 = _compute_sha256(output)

    # 5. Record
    entry = {
        "path": str(output),
        "filename": output.name,
        "size_bytes": output.stat().st_size,
        "sha256": sha256,
        "timestamp": datetime.now().isoformat(),
        "verified": verified,
        "note": note,
    }

    manifest = _load_manifest(project_dir)
    manifest.append(entry)
    _save_manifest(project_dir, manifest)

    return entry


def restore_backup(
    project_dir: Path,
    db_path: Path,
    backup_path: Path,
    verify: bool = True,
) -> dict[str, Any]:
    """Restore the warehouse from a backup file.

    1. Optionally verify the backup (it opens as DuckDB, and its SHA-256
       matches the manifest when the backup is tracked)
    2. Copy the backup to a temp file beside the warehouse
    3. Move the old WAL aside, then atomically replace the warehouse
    4. Drop the old WAL: it belongs to the old database, and replaying it
       onto the restored file would corrupt it

    Any failure leaves the original warehouse and its WAL as they were, so a
    crash or a locked file mid-restore never leaves a half-copied warehouse.
    Returns restore metadata dict.
    """
    if not backup_path.exists():
        raise BackupError(f"Backup file not found: {backup_path}")

    if verify:
        result = verify_backup(backup_path, project_dir=project_dir)
        if not result.get("valid"):
            raise BackupError(
                f"Backup failed verification: {result.get('error', 'unknown error')}"
            )

    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Same directory as the target, so os.replace is a rename and not a copy.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{db_path.name}.restore-", suffix=".tmp", dir=str(db_path.parent)
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    wal_path = Path(str(db_path) + ".wal")
    wal_aside = Path(tmp_name + ".wal")
    try:
        try:
            shutil.copyfile(str(backup_path), str(tmp_path))
            with open(tmp_path, "rb+") as f:
                os.fsync(f.fileno())
            # mkstemp creates the file 0600; without this the restored
            # warehouse would silently lose group/other access on POSIX.
            if db_path.exists():
                shutil.copymode(str(db_path), str(tmp_path))
            else:
                # Not reading the umask: os.umask() is process-wide and
                # racy under the server's threads.
                os.chmod(tmp_path, 0o644)
        except OSError as e:
            raise BackupError(f"Cannot copy {backup_path}: {e}")

        moved_wal = False
        try:
            if wal_path.exists():
                os.replace(wal_path, wal_aside)
                moved_wal = True
            os.replace(tmp_path, db_path)
        except OSError as e:
            if moved_wal:
                os.replace(wal_aside, wal_path)
            hint = _LOCKED_HINT if isinstance(e, PermissionError) else str(e)
            raise BackupError(f"Cannot replace {db_path}: {hint}")
    finally:
        tmp_path.unlink(missing_ok=True)
    try:
        wal_aside.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("Could not remove the pre-restore WAL %s: %s", wal_aside, e)

    return {
        "restored_from": str(backup_path),
        "restored_to": str(db_path),
        "size_bytes": db_path.stat().st_size,
        "timestamp": datetime.now().isoformat(),
        "verified_before_restore": verify,
    }


def verify_backup(backup_path: Path, project_dir: Path | None = None) -> dict[str, Any]:
    """Verify a backup file's integrity and compute its checksum.

    A tracked backup whose SHA-256 no longer matches the manifest is invalid
    even when DuckDB can still open it (bit rot, a partial overwrite, an
    edit). The file is then opened read-only and its schemas and tables are
    counted. ``project_dir`` locates the manifest; without it the manifest
    beside the backup (the default ``_backups/`` layout) is used.
    """
    if not backup_path.exists():
        raise BackupError(f"Backup file not found: {backup_path}")

    sha256 = _compute_sha256(backup_path)
    entry = _manifest_entry_for(backup_path, project_dir)
    expected = entry.get("sha256") if entry else None
    if expected and expected != sha256:
        return {
            "path": str(backup_path),
            "valid": False,
            "sha256": sha256,
            "expected_sha256": expected,
            "checksum_match": False,
            "error": (
                f"SHA-256 mismatch: the manifest records {expected[:16]}..., "
                f"the file is {sha256[:16]}... (it changed after the backup was taken)"
            ),
        }

    try:
        conn = duckdb.connect(str(backup_path), read_only=True)
    except duckdb.Error as e:
        return {
            "path": str(backup_path),
            "valid": False,
            "error": f"Cannot open: {e}",
        }

    try:
        # Verify by reading catalog metadata
        integrity_ok = True

        # Count objects
        schemas = conn.execute(
            "SELECT DISTINCT table_schema FROM information_schema.tables "
            "WHERE table_catalog = current_database() "
            "AND table_schema NOT IN ('information_schema', 'pg_catalog')"
        ).fetchall()
        table_count = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_catalog = current_database() "
            "AND table_schema NOT IN ('information_schema', 'pg_catalog')"
        ).fetchone()[0]

        conn.close()

        return {
            "path": str(backup_path),
            "valid": integrity_ok,
            "sha256": sha256,
            "expected_sha256": expected,
            # None: untracked backup, nothing to compare against.
            "checksum_match": True if expected else None,
            "size_bytes": backup_path.stat().st_size,
            "schemas": [s[0] for s in schemas],
            "table_count": table_count,
        }
    except Exception as e:
        conn.close()
        return {
            "path": str(backup_path),
            "valid": False,
            "error": str(e),
        }


def list_backups(project_dir: Path) -> list[dict]:
    """List all tracked backups from the manifest.

    Also checks which backup files still exist on disk.
    """
    entries = _load_manifest(project_dir)
    for entry in entries:
        path = Path(entry["path"])
        if not path.is_absolute():
            path = project_dir / path
        entry["exists"] = path.exists()
        # "verified" was recorded when the backup was taken; a file that has
        # changed on disk since is no longer verified.
        entry["checksum_match"] = None
        if entry["exists"] and entry.get("sha256"):
            try:
                entry["checksum_match"] = _cached_sha256(path) == entry["sha256"]
            except OSError:
                entry["checksum_match"] = False
            if not entry["checksum_match"]:
                entry["verified"] = False
    return entries


def cleanup_backups(
    project_dir: Path,
    keep: int = 10,
) -> list[dict]:
    """Remove old backups, keeping the N most recent.

    Returns list of removed entries.
    """
    # keep=0 used to remove nothing (manifest[:-0] is empty) and a negative
    # keep removed from the oldest end by count; neither is a retention policy.
    if keep < 1:
        raise ValueError(f"keep must be at least 1, got {keep}")
    manifest = _load_manifest(project_dir)
    if len(manifest) <= keep:
        return []

    # Sort by timestamp (oldest first)
    manifest.sort(key=lambda e: e.get("timestamp", ""))
    to_remove = manifest[:-keep]
    to_keep = manifest[-keep:]

    removed = []
    for entry in to_remove:
        path = Path(entry["path"])
        if path.exists():
            try:
                path.unlink()
                removed.append(entry)
            except OSError as e:
                logger.warning("Failed to remove old backup %s: %s", path, e)
                to_keep.insert(0, entry)  # keep in manifest if can't delete
        else:
            removed.append(entry)

    _save_manifest(project_dir, to_keep)
    return removed
