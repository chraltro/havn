"""Metrics and observability endpoints: system health, model stats, and pipeline overview."""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, Request

from havn.server.deps import (
    DbConnReadOnly,
    DbConnReadOnlyOptional,
    _get_db_path,
    _require_permission,
    ensure_meta_table,
)

router = APIRouter()

_SERVER_START_TIME = time.time()


# --- Health ---


@router.get("/api/health")
def health_check(conn: DbConnReadOnlyOptional) -> dict:
    """Basic health check. Returns database connectivity status."""
    db_ok = False
    if conn is not None:
        try:
            conn.execute("SELECT 1").fetchone()
            db_ok = True
        except Exception:
            pass
    return {"status": "ok", "database": db_ok}


# --- Aggregate metrics ---


@router.get("/api/metrics")
def get_metrics(request: Request, conn: DbConnReadOnly) -> dict:
    """Return aggregate metrics for the platform."""
    _require_permission(request, "read")
    ensure_meta_table(conn)

    # --- Model counts by status from last run ---
    total_models = 0
    status_counts: dict[str, int] = {"built": 0, "skipped": 0, "error": 0}
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS cnt "
            "FROM _dp_internal.run_log "
            "WHERE run_type = 'transform' "
            "AND started_at = ("
            "  SELECT MAX(started_at) FROM _dp_internal.run_log WHERE run_type = 'transform'"
            ") "
            "GROUP BY status"
        ).fetchall()
        for r in rows:
            status_counts[r[0]] = r[1]
            total_models += r[1]
    except Exception:
        pass

    # --- Average build time from model_state ---
    avg_build_time_ms: float | None = None
    try:
        row = conn.execute(
            "SELECT AVG(run_duration_ms) FROM _dp_internal.model_state "
            "WHERE run_duration_ms > 0"
        ).fetchone()
        if row and row[0] is not None:
            avg_build_time_ms = round(float(row[0]), 2)
    except Exception:
        pass

    # --- Total rows across all tables ---
    total_rows = 0
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(row_count), 0) FROM _dp_internal.model_state"
        ).fetchone()
        if row:
            total_rows = int(row[0])
    except Exception:
        pass

    # --- Database file size ---
    db_size_bytes: int | None = None
    try:
        db_path = _get_db_path()
        if db_path.exists():
            db_size_bytes = os.path.getsize(db_path)
    except Exception:
        pass

    # --- Uptime ---
    uptime_seconds = round(time.time() - _SERVER_START_TIME, 2)

    # --- Last pipeline run ---
    last_run: dict | None = None
    try:
        row = conn.execute(
            "SELECT run_type, target, status, started_at, finished_at, duration_ms, error "
            "FROM _dp_internal.run_log ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if row:
            last_run = {
                "run_type": row[0],
                "target": row[1],
                "status": row[2],
                "started_at": str(row[3]) if row[3] else None,
                "finished_at": str(row[4]) if row[4] else None,
                "duration_ms": row[5],
                "error": row[6],
            }
    except Exception:
        pass

    # --- Top 10 slowest models ---
    slowest_models: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT model_path, run_duration_ms, last_run_at "
            "FROM _dp_internal.model_state "
            "WHERE run_duration_ms > 0 "
            "ORDER BY run_duration_ms DESC LIMIT 10"
        ).fetchall()
        slowest_models = [
            {
                "model": r[0],
                "run_duration_ms": r[1],
                "last_run_at": str(r[2]) if r[2] else None,
            }
            for r in rows
        ]
    except Exception:
        pass

    return {
        "total_models": total_models,
        "status_counts": status_counts,
        "avg_build_time_ms": avg_build_time_ms,
        "total_rows": total_rows,
        "db_size_bytes": db_size_bytes,
        "uptime_seconds": uptime_seconds,
        "last_run": last_run,
        "slowest_models": slowest_models,
    }


# --- Per-model stats ---


@router.get("/api/metrics/models")
def get_model_metrics(request: Request, conn: DbConnReadOnly) -> list[dict]:
    """Return per-model stats from _dp_internal.model_state."""
    _require_permission(request, "read")
    ensure_meta_table(conn)

    rows = conn.execute(
        "SELECT model_path, materialized_as, last_run_at, run_duration_ms, "
        "row_count, content_hash "
        "FROM _dp_internal.model_state ORDER BY model_path"
    ).fetchall()

    return [
        {
            "name": r[0],
            "materialized": r[1],
            "last_run_at": str(r[2]) if r[2] else None,
            "run_duration_ms": r[3],
            "row_count": r[4],
            "content_hash": r[5],
        }
        for r in rows
    ]
