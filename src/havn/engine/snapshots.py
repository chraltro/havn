"""Pipeline Rewind: snapshot capture, storage, dedup, GC, and restore."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from havn.engine.utils import validate_identifier

logger = logging.getLogger("havn.snapshots")

_MODEL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


def _safe_model_fqn(model_name: str) -> str:
    """Return a safely double-quoted ``"schema"."name"`` for SQL embedding.

    Raises ValueError if the model name fails identifier validation. Callers
    must use this whenever ``model_name`` flows into a SQL string that
    cannot be parameterised (CREATE TABLE, COPY, FROM clauses).
    """
    if not _MODEL_NAME_RE.match(model_name or ""):
        raise ValueError(f"Invalid model name: {model_name!r}")
    parts = model_name.split(".")
    for p in parts:
        validate_identifier(p, "model identifier")
    if len(parts) == 1:
        return f'"{parts[0]}"'
    return f'"{parts[0]}"."{parts[1]}"'

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RewindConfig:
    """Rewind settings from project.yml."""

    enabled: bool = True
    retention: str = "7d"
    max_storage: float | None = None  # GB cap
    dedup: bool = True
    exclude: list[str] = field(default_factory=list)

    @property
    def retention_seconds(self) -> int:
        """Parse retention string (e.g. '7d', '24h') to seconds."""
        s = self.retention.strip().lower()
        if s.endswith("d"):
            return int(s[:-1]) * 86400
        if s.endswith("h"):
            return int(s[:-1]) * 3600
        # Default: treat as days
        try:
            return int(s) * 86400
        except ValueError:
            return 7 * 86400


@dataclass
class SnapshotInfo:
    """Metadata for a single snapshot."""

    run_id: str
    model_name: str
    row_count: int
    col_count: int
    schema_hash: str
    size_bytes: int
    checksum: str
    file_path: str | None
    created_at: str


@dataclass
class RunInfo:
    """Metadata for a pipeline run."""

    run_id: str
    started_at: str
    finished_at: str | None
    status: str
    trigger: str
    models_run: list[str]


# ---------------------------------------------------------------------------
# Metadata database management
# ---------------------------------------------------------------------------

_SNAPSHOTS_DIR = ".havn/snapshots"
_METADATA_DIR = ".havn/metadata"


def _meta_db_path(project_dir: Path) -> Path:
    """Path to the rewind metadata database."""
    return project_dir / _METADATA_DIR / "rewind.duckdb"


import threading

_meta_conns: dict[str, duckdb.DuckDBPyConnection] = {}
_meta_lock = threading.Lock()


def _conn_is_alive(conn: duckdb.DuckDBPyConnection) -> bool:
    """Probe a cached connection; return False if the underlying handle was closed."""
    try:
        conn.execute("SELECT 1").fetchone()
        return True
    except Exception:
        return False


def _meta_conn_for(project_dir: Path) -> duckdb.DuckDBPyConnection:
    """Return a long-lived metadata connection for `project_dir`.

    The metadata DB is a separate DuckDB file; opening it on every helper
    call serialises all callers on the file lock and adds significant
    latency in hot paths like `capture_snapshot`. We cache one connection
    per project_dir and reuse it. A closed cached connection is detected
    and replaced so accidental external closes do not poison subsequent
    calls.
    """
    key = str(project_dir.resolve())
    with _meta_lock:
        conn = _meta_conns.get(key)
        if conn is not None and _conn_is_alive(conn):
            return conn
        if conn is not None:
            _meta_conns.pop(key, None)
        meta_dir = project_dir / _METADATA_DIR
        meta_dir.mkdir(parents=True, exist_ok=True)
        db_path = _meta_db_path(project_dir)
        conn = duckdb.connect(str(db_path))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS runs ("
            "run_id VARCHAR PRIMARY KEY, "
            "started_at TIMESTAMP DEFAULT current_timestamp, "
            "finished_at TIMESTAMP, "
            "status VARCHAR DEFAULT 'running', "
            "trigger VARCHAR DEFAULT 'manual', "
            "models_run VARCHAR[] DEFAULT [])"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS snapshots ("
            "run_id VARCHAR NOT NULL, "
            "model_name VARCHAR NOT NULL, "
            "row_count INTEGER DEFAULT 0, "
            "col_count INTEGER DEFAULT 0, "
            "schema_hash VARCHAR DEFAULT '', "
            "size_bytes BIGINT DEFAULT 0, "
            "checksum VARCHAR DEFAULT '', "
            "file_path VARCHAR, "
            "created_at TIMESTAMP DEFAULT current_timestamp, "
            "PRIMARY KEY (run_id, model_name))"
        )
        _meta_conns[key] = conn
        return conn


def _ensure_meta_db(project_dir: Path) -> duckdb.DuckDBPyConnection:
    """Backwards-compatible wrapper that now reuses a cached connection."""
    return _meta_conn_for(project_dir)


def _close_meta_db(conn: duckdb.DuckDBPyConnection) -> None:
    """No-op: cached connections are kept alive for the process lifetime."""
    return None


def close_meta_db_cache() -> None:
    """Close all cached metadata connections."""
    with _meta_lock:
        for c in list(_meta_conns.values()):
            try:
                c.close()
            except Exception:
                pass
        _meta_conns.clear()


# ---------------------------------------------------------------------------
# Run management
# ---------------------------------------------------------------------------


def start_run(
    project_dir: Path,
    trigger: str = "manual",
) -> str:
    """Start a new pipeline run. Returns the run_id."""
    run_id = str(uuid.uuid4())
    conn = _ensure_meta_db(project_dir)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, trigger) VALUES (?, ?)",
            [run_id, trigger],
        )
    finally:
        _close_meta_db(conn)
    return run_id


def finish_run(
    project_dir: Path,
    run_id: str,
    status: str,
    models_run: list[str],
) -> None:
    """Mark a run as finished."""
    conn = _ensure_meta_db(project_dir)
    try:
        conn.execute(
            """
            UPDATE runs
            SET finished_at = current_timestamp,
                status = ?,
                models_run = ?
            WHERE run_id = ?
            """,
            [status, models_run, run_id],
        )
    finally:
        _close_meta_db(conn)


# ---------------------------------------------------------------------------
# Snapshot capture
# ---------------------------------------------------------------------------


def _compute_checksum(
    warehouse_conn: duckdb.DuckDBPyConnection,
    model_name: str,
) -> str:
    """Compute a content checksum for a model's current output.

    For tables under ~1M rows, hashes the full output.
    For larger tables, uses a proxy (row count + schema + sample rows).
    """
    try:
        fqn = _safe_model_fqn(model_name)
    except ValueError as e:
        logger.warning("Snapshot checksum skipped: %s", e)
        return ""
    try:
        row_count = warehouse_conn.execute(
            f"SELECT COUNT(*) FROM {fqn}"
        ).fetchone()[0]
    except Exception:
        return ""

    if row_count == 0:
        return hashlib.sha256(b"empty").hexdigest()[:16]

    if row_count <= 1_000_000:
        try:
            result = warehouse_conn.execute(
                f"SELECT md5(string_agg(COLUMNS(*)::VARCHAR, '|' ORDER BY rowid)) FROM {fqn}"
            ).fetchone()
            if result and result[0]:
                return hashlib.sha256(result[0].encode()).hexdigest()[:16]
        except Exception:
            pass

    try:
        parts = model_name.split(".")
        if len(parts) == 2:
            schema, name = parts
        else:
            schema, name = "main", model_name
        cols = warehouse_conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? "
            "ORDER BY ordinal_position",
            [schema, name],
        ).fetchall()
        schema_str = "|".join(f"{c[0]}:{c[1]}" for c in cols)

        first_sample = warehouse_conn.execute(
            f"SELECT md5(string_agg(COLUMNS(*)::VARCHAR, '|')) FROM (SELECT * FROM {fqn} LIMIT 1000)"
        ).fetchone()[0] or ""

        last_sample = warehouse_conn.execute(
            f"SELECT md5(string_agg(COLUMNS(*)::VARCHAR, '|')) FROM (SELECT * FROM {fqn} ORDER BY rowid DESC LIMIT 1000)"
        ).fetchone()[0] or ""

        combined = f"{row_count}|{schema_str}|{first_sample}|{last_sample}"
        return hashlib.sha256(combined.encode()).hexdigest()[:16]
    except Exception:
        return hashlib.sha256(str(row_count).encode()).hexdigest()[:16]


def _compute_schema_hash(
    warehouse_conn: duckdb.DuckDBPyConnection,
    model_name: str,
) -> tuple[str, int]:
    """Compute a hash of column names + types. Returns (hash, col_count)."""
    parts = model_name.split(".")
    if len(parts) == 2:
        schema, name = parts
    else:
        schema, name = "main", model_name

    cols = warehouse_conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
        [schema, name],
    ).fetchall()

    if not cols:
        return "", 0

    schema_str = "|".join(f"{c[0]}:{c[1]}" for c in cols)
    return hashlib.sha256(schema_str.encode()).hexdigest()[:16], len(cols)


def capture_snapshot(
    project_dir: Path,
    warehouse_conn: duckdb.DuckDBPyConnection,
    model_name: str,
    run_id: str,
    row_count: int,
    config: RewindConfig | None = None,
) -> bool:
    """Capture a snapshot of a model's output after execution.

    Returns True if a new snapshot file was written, False if deduped or skipped.
    """
    if config and not config.enabled:
        return False
    if config and model_name in config.exclude:
        return False

    meta_conn = _ensure_meta_db(project_dir)
    try:
        # Compute checksums
        checksum = _compute_checksum(warehouse_conn, model_name)
        schema_hash, col_count = _compute_schema_hash(warehouse_conn, model_name)

        # Dedup: check if previous snapshot has same checksum
        dedup = config.dedup if config else True
        prev_file = None
        if dedup and checksum:
            prev = meta_conn.execute(
                """
                SELECT file_path, checksum FROM snapshots
                WHERE model_name = ? AND checksum = ? AND file_path IS NOT NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                [model_name, checksum],
            ).fetchone()
            if prev:
                prev_file = prev[0]

        if prev_file and os.path.exists(project_dir / prev_file):
            # Dedup: point to existing file
            size_bytes = os.path.getsize(project_dir / prev_file)
            meta_conn.execute(
                """
                INSERT INTO snapshots (run_id, model_name, row_count, col_count,
                    schema_hash, size_bytes, checksum, file_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [run_id, model_name, row_count, col_count,
                 schema_hash, size_bytes, checksum, prev_file],
            )
            return False

        try:
            fqn = _safe_model_fqn(model_name)
        except ValueError as e:
            logger.warning("Snapshot rejected: %s", e)
            return False

        snapshot_dir = project_dir / _SNAPSHOTS_DIR / model_name.replace(".", "/")
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        if not snapshot_dir.resolve().is_relative_to(project_dir.resolve()):
            logger.warning("Snapshot dir would escape project_dir for %s", model_name)
            return False
        snapshot_path = snapshot_dir / f"{run_id}.parquet"
        rel_path = str(snapshot_path.relative_to(project_dir))

        try:
            safe_path = str(snapshot_path).replace("'", "''")
            warehouse_conn.execute(
                f"COPY (SELECT * FROM {fqn}) TO '{safe_path}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        except Exception as e:
            logger.warning("Failed to write snapshot for %s: %s", model_name, e)
            return False

        size_bytes = snapshot_path.stat().st_size if snapshot_path.exists() else 0

        meta_conn.execute(
            """
            INSERT INTO snapshots (run_id, model_name, row_count, col_count,
                schema_hash, size_bytes, checksum, file_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [run_id, model_name, row_count, col_count,
             schema_hash, size_bytes, checksum, rel_path],
        )
        return True

    except Exception as e:
        logger.warning("Snapshot capture failed for %s: %s", model_name, e)
        return False
    finally:
        _close_meta_db(meta_conn)


# ---------------------------------------------------------------------------
# Garbage collection
# ---------------------------------------------------------------------------


def run_gc(project_dir: Path, config: RewindConfig | None = None) -> int:
    """Garbage collect old snapshots. Returns number of files deleted."""
    retention_s = config.retention_seconds if config else 7 * 86400
    max_storage_bytes = (config.max_storage * 1_073_741_824) if config and config.max_storage else None

    meta_conn = _ensure_meta_db(project_dir)
    deleted = 0

    try:
        expired = meta_conn.execute(
            "SELECT run_id, model_name, file_path FROM snapshots "
            "WHERE file_path IS NOT NULL "
            "AND created_at < current_timestamp - INTERVAL '1 second' * ?",
            [retention_s],
        ).fetchall()

        if expired:
            # Compute live reference counts in a single grouped query so
            # GC stays O(N) rather than O(N^2) on the metadata DB.
            ref_rows = meta_conn.execute(
                "SELECT file_path, COUNT(*) FROM snapshots "
                "WHERE file_path IS NOT NULL "
                "AND created_at >= current_timestamp - INTERVAL '1 second' * ? "
                "GROUP BY file_path",
                [retention_s],
            ).fetchall()
            live_refs: dict[str, int] = {fp: cnt for fp, cnt in ref_rows}

            for run_id, model_name, file_path in expired:
                if live_refs.get(file_path, 0) == 0:
                    full_path = project_dir / file_path
                    if full_path.exists():
                        try:
                            full_path.unlink()
                            deleted += 1
                        except Exception as e:
                            logger.warning(
                                "Failed to delete snapshot %s: %s", file_path, e
                            )

            meta_conn.execute(
                "UPDATE snapshots SET file_path = NULL "
                "WHERE file_path IS NOT NULL "
                "AND created_at < current_timestamp - INTERVAL '1 second' * ?",
                [retention_s],
            )

        # Storage cap enforcement
        if max_storage_bytes:
            total = meta_conn.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM snapshots WHERE file_path IS NOT NULL"
            ).fetchone()[0]

            if total > max_storage_bytes:
                # Delete oldest snapshot files until under cap
                oldest = meta_conn.execute(
                    """
                    SELECT run_id, model_name, file_path, size_bytes FROM snapshots
                    WHERE file_path IS NOT NULL
                    ORDER BY created_at ASC
                    """
                ).fetchall()

                for run_id, model_name, file_path, size_bytes in oldest:
                    if total <= max_storage_bytes:
                        break
                    full_path = project_dir / file_path
                    if full_path.exists():
                        try:
                            full_path.unlink()
                            deleted += 1
                        except Exception:
                            pass
                    meta_conn.execute(
                        "UPDATE snapshots SET file_path = NULL WHERE run_id = ? AND model_name = ?",
                        [run_id, model_name],
                    )
                    total -= size_bytes

        # Clean up empty snapshot directories
        snapshots_root = project_dir / _SNAPSHOTS_DIR
        if snapshots_root.exists():
            for dirpath, dirnames, filenames in os.walk(str(snapshots_root), topdown=False):
                if not filenames and not dirnames:
                    try:
                        os.rmdir(dirpath)
                    except OSError:
                        pass

    finally:
        _close_meta_db(meta_conn)

    return deleted


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def get_runs(
    project_dir: Path,
    limit: int = 100,
) -> list[RunInfo]:
    """Get recent pipeline runs."""
    meta_conn = _ensure_meta_db(project_dir)
    try:
        rows = meta_conn.execute(
            """
            SELECT run_id, started_at, finished_at, status, trigger, models_run
            FROM runs ORDER BY started_at DESC LIMIT ?
            """,
            [limit],
        ).fetchall()
        return [
            RunInfo(
                run_id=r[0],
                started_at=str(r[1]) if r[1] else "",
                finished_at=str(r[2]) if r[2] else None,
                status=r[3],
                trigger=r[4],
                models_run=r[5] if r[5] else [],
            )
            for r in rows
        ]
    finally:
        _close_meta_db(meta_conn)


def get_snapshots_for_run(
    project_dir: Path,
    run_id: str,
) -> list[SnapshotInfo]:
    """Get all snapshots for a specific run."""
    meta_conn = _ensure_meta_db(project_dir)
    try:
        rows = meta_conn.execute(
            """
            SELECT run_id, model_name, row_count, col_count, schema_hash,
                   size_bytes, checksum, file_path, created_at
            FROM snapshots WHERE run_id = ?
            ORDER BY model_name
            """,
            [run_id],
        ).fetchall()
        return [
            SnapshotInfo(
                run_id=r[0], model_name=r[1], row_count=r[2], col_count=r[3],
                schema_hash=r[4], size_bytes=r[5], checksum=r[6],
                file_path=r[7], created_at=str(r[8]) if r[8] else "",
            )
            for r in rows
        ]
    finally:
        _close_meta_db(meta_conn)


def get_all_snapshots(
    project_dir: Path,
    limit: int = 5000,
) -> list[SnapshotInfo]:
    """Get all snapshot metadata (for slider initialization)."""
    meta_conn = _ensure_meta_db(project_dir)
    try:
        rows = meta_conn.execute(
            """
            SELECT s.run_id, s.model_name, s.row_count, s.col_count, s.schema_hash,
                   s.size_bytes, s.checksum, s.file_path, s.created_at
            FROM snapshots s
            JOIN runs r ON s.run_id = r.run_id
            ORDER BY r.started_at DESC, s.model_name
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        return [
            SnapshotInfo(
                run_id=r[0], model_name=r[1], row_count=r[2], col_count=r[3],
                schema_hash=r[4], size_bytes=r[5], checksum=r[6],
                file_path=r[7], created_at=str(r[8]) if r[8] else "",
            )
            for r in rows
        ]
    finally:
        _close_meta_db(meta_conn)


def get_snapshot_sample(
    project_dir: Path,
    run_id: str,
    model_name: str,
    limit: int = 100,
) -> dict:
    """Load a sample from a snapshot parquet file. Returns {columns, rows}."""
    meta_conn = _ensure_meta_db(project_dir)
    try:
        row = meta_conn.execute(
            "SELECT file_path FROM snapshots WHERE run_id = ? AND model_name = ?",
            [run_id, model_name],
        ).fetchone()

        if not row or not row[0]:
            return {"error": "Snapshot expired or not found", "columns": [], "rows": []}

        file_path = project_dir / row[0]
        if not file_path.exists():
            return {"error": "Snapshot file missing", "columns": [], "rows": []}

        if not file_path.resolve().is_relative_to(project_dir.resolve()):
            return {"error": "Snapshot path escapes project", "columns": [], "rows": []}
        read_conn = duckdb.connect(":memory:")
        try:
            safe_path = str(file_path).replace("'", "''")
            result = read_conn.execute(
                f"SELECT * FROM read_parquet('{safe_path}') LIMIT ?", [limit]
            )
            columns = [desc[0] for desc in result.description]
            rows = [[str(v) if v is not None else None for v in row] for row in result.fetchall()]
            return {"columns": columns, "rows": rows}
        finally:
            read_conn.close()
    finally:
        _close_meta_db(meta_conn)


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def restore_snapshot(
    project_dir: Path,
    warehouse_conn: duckdb.DuckDBPyConnection,
    run_id: str,
    model_name: str,
) -> dict:
    """Restore a model to a snapshot state.

    Returns:
        {"status": "success"|"error", "message": str, "schema_warning": bool}
    """
    meta_conn = _ensure_meta_db(project_dir)
    try:
        snapshot = meta_conn.execute(
            "SELECT file_path, schema_hash FROM snapshots WHERE run_id = ? AND model_name = ?",
            [run_id, model_name],
        ).fetchone()

        if not snapshot:
            return {"status": "error", "message": "Snapshot not found", "schema_warning": False}

        file_path, old_schema_hash = snapshot
        if not file_path:
            return {
                "status": "error",
                "message": "Snapshot expired. Only metadata available.",
                "schema_warning": False,
            }

        full_path = project_dir / file_path
        if not full_path.exists():
            return {"status": "error", "message": "Snapshot file missing", "schema_warning": False}

        # Check for schema changes
        current_hash, _ = _compute_schema_hash(warehouse_conn, model_name)
        schema_warning = bool(old_schema_hash and current_hash and old_schema_hash != current_hash)

        try:
            fqn = _safe_model_fqn(model_name)
        except ValueError as e:
            return {"status": "error", "message": str(e), "schema_warning": False}
        if not full_path.resolve().is_relative_to(project_dir.resolve()):
            return {"status": "error", "message": "Snapshot path escapes project", "schema_warning": False}
        parts = model_name.split(".")
        if len(parts) == 2:
            schema, _name = parts
            validate_identifier(schema, "schema")
            warehouse_conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        safe_path = str(full_path).replace("'", "''")
        warehouse_conn.execute(
            f"CREATE OR REPLACE TABLE {fqn} AS SELECT * FROM read_parquet('{safe_path}')"
        )

        return {
            "status": "success",
            "message": f"Restored {model_name} from run {run_id}",
            "schema_warning": schema_warning,
        }
    finally:
        _close_meta_db(meta_conn)


def get_downstream_models(
    model_name: str,
    transform_dir: Path,
) -> list[str]:
    """Find all downstream models that depend on the given model."""
    from havn.engine.transform import build_dag, discover_models

    models = discover_models(transform_dir)
    if not models:
        return []

    # Build adjacency for downstream traversal
    downstream: dict[str, set[str]] = {}
    for m in models:
        for dep in m.depends_on:
            if dep not in downstream:
                downstream[dep] = set()
            downstream[dep].add(m.full_name)

    # BFS from model_name
    result = []
    visited = set()
    queue = [model_name]
    while queue:
        current = queue.pop(0)
        for child in downstream.get(current, []):
            if child not in visited:
                visited.add(child)
                result.append(child)
                queue.append(child)

    return result


def restore_with_cascade(
    project_dir: Path,
    warehouse_conn: duckdb.DuckDBPyConnection,
    run_id: str,
    model_name: str,
    transform_dir: Path,
    db_path: str | None = None,
    db_config: object | None = None,
) -> dict:
    """Restore a snapshot and re-run downstream models.

    Returns summary of restore + cascade execution.
    """
    from havn.engine.transform import run_transform

    # 1. Restore the snapshot
    restore_result = restore_snapshot(project_dir, warehouse_conn, run_id, model_name)
    if restore_result["status"] == "error":
        return restore_result

    # 2. Find downstream models
    downstream = get_downstream_models(model_name, transform_dir)

    # 3. Start a new run for the cascade
    cascade_run_id = start_run(project_dir, trigger="restore")

    # 4. Capture snapshot of the restored model
    try:
        row_count = warehouse_conn.execute(
            f"SELECT COUNT(*) FROM {model_name}"
        ).fetchone()[0]
    except Exception:
        row_count = 0

    capture_snapshot(project_dir, warehouse_conn, model_name, cascade_run_id, row_count)

    # 5. Re-run downstream models if any
    cascade_results = {}
    if downstream:
        cascade_results = run_transform(
            warehouse_conn,
            transform_dir,
            targets=downstream,
            force=True,
            db_path=db_path,
            db_config=db_config,
        )

        # Capture snapshots for rebuilt downstream models
        for model_full_name, status in cascade_results.items():
            if status == "built":
                try:
                    rc = warehouse_conn.execute(
                        f"SELECT COUNT(*) FROM {model_full_name}"
                    ).fetchone()[0]
                except Exception:
                    rc = 0
                capture_snapshot(
                    project_dir, warehouse_conn, model_full_name,
                    cascade_run_id, rc,
                )

    # 6. Finish the cascade run
    all_models = [model_name] + list(cascade_results.keys())
    has_errors = any(s in ("error", "assertion_failed") for s in cascade_results.values())
    status = "partial" if has_errors else "success"
    finish_run(project_dir, cascade_run_id, status, all_models)

    return {
        "status": "success",
        "message": restore_result["message"],
        "schema_warning": restore_result["schema_warning"],
        "cascade_run_id": cascade_run_id,
        "downstream_models": downstream,
        "cascade_results": cascade_results,
    }
