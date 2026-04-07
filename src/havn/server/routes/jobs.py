"""Orchestration jobs API routes."""

from __future__ import annotations

import json
import logging
import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from havn.server.deps import (
    DbConn,
    DbConnReadOnly,
    _get_config,
    _get_project_dir,
    _require_permission,
    ensure_meta_table,
)

logger = logging.getLogger("havn.server")
router = APIRouter(tags=["jobs"])


# --- Pydantic models ---


def _reject_traversal(value: str) -> str:
    """Reject target strings that contain path traversal sequences."""
    if ".." in value:
        raise ValueError("target must not contain '..'")
    if value.startswith("/") or value.startswith("\\"):
        raise ValueError("target must not be absolute")
    return value


def _validate_target_list(v: list[str] | None) -> list[str] | None:
    if v is None:
        return None
    if not isinstance(v, list):
        raise ValueError("targets must be a list of strings")
    for t in v:
        if not isinstance(t, str) or not t.strip():
            raise ValueError("each target must be a non-empty string")
        # Allow +downstream: prefix — strip for validation
        raw = t[len("+downstream:") :] if t.startswith("+downstream:") else t
        _reject_traversal(raw)
    return v


class CreateJobRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    # Either `target` (legacy single) or `targets` (preferred list). At least
    # one must be provided; both are supported for backward compatibility.
    target: str | None = Field(default=None, min_length=1, max_length=500)
    targets: list[str] | None = Field(default=None, max_length=200)
    resolve: str = Field(default="upstream", pattern="^(upstream|none)$")
    # Either `cron` (legacy single) or `schedules` (preferred list). Either
    # can be omitted for on-demand-only jobs.
    cron: str = Field(default="")
    schedules: list[str] | None = Field(default=None, max_length=50)
    tags: list[str] = Field(default_factory=list, max_length=20)
    enabled: bool = True
    notify: list[str] = Field(default_factory=list)
    retry: int = Field(default=0, ge=0, le=10)
    retry_delay: int = Field(default=10, ge=0, le=3600)
    timeout_minutes: int = Field(default=60, ge=1, le=1440)
    description: str = ""

    @field_validator("target")
    @classmethod
    def _validate_target(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _reject_traversal(v)

    @field_validator("targets")
    @classmethod
    def _validate_targets(cls, v: list[str] | None) -> list[str] | None:
        return _validate_target_list(v)


class UpdateJobRequest(BaseModel):
    name: str | None = None
    target: str | None = None
    targets: list[str] | None = None
    resolve: str | None = None
    cron: str | None = None
    schedules: list[str] | None = None
    tags: list[str] | None = None
    enabled: bool | None = None
    notify: list[str] | None = None
    retry: int | None = None
    retry_delay: int | None = None
    timeout_minutes: int | None = None
    description: str | None = None

    @field_validator("target")
    @classmethod
    def _validate_target(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _reject_traversal(v)

    @field_validator("targets")
    @classmethod
    def _validate_targets(cls, v: list[str] | None) -> list[str] | None:
        return _validate_target_list(v)


# --- Helpers ---


def _get_dag(project_dir):
    from havn.engine.transform.discovery import build_dag, discover_models

    models = discover_models(project_dir / "transform")
    return build_dag(models)


def _run_row_to_dict(row) -> dict:
    step_details = row[12]
    if isinstance(step_details, str):
        try:
            step_details = json.loads(step_details)
        except Exception:
            step_details = []
    return {
        "id": row[0],
        "job_name": row[1],
        "status": row[2],
        "steps_total": row[3],
        "steps_completed": row[4],
        "steps_failed": row[5],
        "steps_skipped": row[6] or 0,
        "duration_ms": row[7],
        "trigger": row[8],
        "started_at": str(row[9]) if row[9] else None,
        "finished_at": str(row[10]) if row[10] else None,
        "error": row[11],
        "step_details": step_details or [],
    }


# --- Jobs endpoints ---


@router.get("/api/jobs")
def list_jobs(request: Request, conn: DbConnReadOnly):
    _require_permission(request, "read")
    from havn.engine.orchestration import (
        discover_jobs,
        ensure_job_runs_table,
        get_earliest_next_run,
    )

    project_dir = _get_project_dir()
    ensure_job_runs_table(conn)
    jobs = discover_jobs(project_dir)
    # Get last run per job + last 10 runs per job for sparkline
    last_runs: dict[str, dict] = {}
    sparklines: dict[str, list[dict]] = {}
    try:
        rows = conn.execute(
            "SELECT job_name, status, started_at, duration_ms, steps_completed, "
            "steps_total, steps_skipped, steps_failed "
            "FROM _havn.job_runs WHERE (job_name, started_at) IN "
            "(SELECT job_name, MAX(started_at) FROM _havn.job_runs GROUP BY job_name)"
        ).fetchall()
        for r in rows:
            last_runs[r[0]] = {
                "status": r[1],
                "started_at": str(r[2]) if r[2] else None,
                "duration_ms": r[3],
                "steps_completed": r[4],
                "steps_total": r[5],
                "steps_skipped": r[6] or 0,
                "steps_failed": r[7] or 0,
            }
        # Last 10 runs per job (ordered oldest to newest for sparkline rendering)
        spark_rows = conn.execute(
            "SELECT job_name, status, duration_ms, started_at FROM ("
            "  SELECT job_name, status, duration_ms, started_at, "
            "    ROW_NUMBER() OVER (PARTITION BY job_name ORDER BY started_at DESC) AS rn "
            "  FROM _havn.job_runs"
            ") WHERE rn <= 10 ORDER BY job_name, started_at"
        ).fetchall()
        for row in spark_rows:
            sparklines.setdefault(row[0], []).append({
                "status": row[1],
                "duration_ms": row[2],
                "started_at": str(row[3]) if row[3] else None,
            })
    except Exception:
        pass
    result = []
    for job in jobs:
        schedules = job.schedules or ([job.cron] if job.cron else [])
        result.append({
            "name": job.name,
            "target": job.target,
            "targets": job.targets or [job.target],
            "resolve": job.resolve,
            "cron": job.cron,
            "schedules": schedules,
            "tags": job.tags,
            "enabled": job.enabled,
            "notify": job.notify,
            "retry": job.retry,
            "retry_delay": job.retry_delay,
            "timeout_minutes": job.timeout_minutes,
            "description": job.description,
            "file": job.file_path.name,
            "sparkline": sparklines.get(job.name, []),
            "last_run": last_runs.get(job.name),
            "next_run": get_earliest_next_run(
                schedules,
                last_fire_iso=(last_runs.get(job.name) or {}).get("started_at"),
            ) if job.enabled and schedules else None,
        })
    return result


@router.get("/api/jobs/{name}")
def get_job(name: str, request: Request, conn: DbConnReadOnly):
    _require_permission(request, "read")
    from havn.engine.orchestration import (
        _find_job,
        ensure_job_runs_table,
        get_earliest_next_run,
        preview_plan,
    )

    project_dir = _get_project_dir()
    ensure_job_runs_table(conn)
    job = _find_job(project_dir, name)
    if not job:
        raise HTTPException(404, f"Job '{name}' not found")
    dag = _get_dag(project_dir)
    plan = preview_plan(
        job.targets or [job.target], dag, project_dir, conn=conn, resolve=job.resolve
    )
    schedules = job.schedules or ([job.cron] if job.cron else [])
    # Fetch last successful fire for interval-schedule next-run computation
    last_fire_iso = None
    try:
        row = conn.execute(
            "SELECT started_at FROM _havn.job_runs WHERE job_name = ? "
            "ORDER BY started_at DESC LIMIT 1",
            [job.name],
        ).fetchone()
        if row and row[0]:
            last_fire_iso = str(row[0])
    except Exception:
        pass
    return {
        "name": job.name,
        "target": job.target,
        "targets": job.targets,
        "resolve": job.resolve,
        "cron": job.cron,
        "schedules": schedules,
        "tags": job.tags,
        "enabled": job.enabled,
        "description": job.description,
        "retry": job.retry,
        "retry_delay": job.retry_delay,
        "timeout_minutes": job.timeout_minutes,
        "notify": job.notify,
        "file": job.file_path.name,
        "next_run": get_earliest_next_run(schedules, last_fire_iso=last_fire_iso) if job.enabled and schedules else None,
        "plan": plan,
    }


@router.get("/api/jobs/{name}/plan")
def get_job_plan(name: str, request: Request, conn: DbConnReadOnly):
    _require_permission(request, "read")
    from havn.engine.orchestration import _find_job, preview_plan

    project_dir = _get_project_dir()
    job = _find_job(project_dir, name)
    if not job:
        raise HTTPException(404, f"Job '{name}' not found")
    dag = _get_dag(project_dir)
    return preview_plan(
        job.targets or [job.target], dag, project_dir, conn=conn, resolve=job.resolve
    )


@router.post("/api/jobs/{name}/run")
def run_job(name: str, request: Request, conn: DbConn):
    _require_permission(request, "execute")
    from havn.engine.orchestration import (
        _find_job,
        ensure_job_runs_table,
        execute_job,
        resolve_execution_plan,
    )

    project_dir = _get_project_dir()
    ensure_job_runs_table(conn)
    job = _find_job(project_dir, name)
    if not job:
        raise HTTPException(404, f"Job '{name}' not found")
    dag = _get_dag(project_dir)
    plan = resolve_execution_plan(
        job.targets or [job.target], dag, project_dir, conn=conn, resolve=job.resolve
    )

    # Try to use the pipeline SSE infrastructure so the UI sees live output.
    # Falls back to a plain background thread if the pipeline is already busy.
    try:
        from havn.server.routes.pipeline import _start_operation, _emit, _finish_operation

        def _run_with_sse():
            from havn.server.deps import _get_shared_conn

            cursor = None
            try:
                cursor = _get_shared_conn().cursor()
                execute_job(job, plan, cursor, project_dir, trigger="manual", emit=_emit)
            except Exception as e:
                logger.error("Job '%s' failed: %s", name, e)
            finally:
                if cursor is not None:
                    try:
                        cursor.close()
                    except Exception:
                        pass
                _finish_operation()

        result = _start_operation("job", f"Job: {name}", _run_with_sse, ())
        if result.get("status") == "already_running":
            # Pipeline is busy — fall back to background thread without SSE
            _run_job_background(job, plan, project_dir, name)
        return {"status": "started", "job": name, "steps": len(plan.steps)}
    except ImportError:
        # Pipeline routes not available — fall back
        _run_job_background(job, plan, project_dir, name)
        return {"status": "started", "job": name, "steps": len(plan.steps)}


def _run_job_background(job, plan, project_dir, name):
    """Run a job on a background thread without SSE (fallback)."""
    from havn.engine.orchestration import execute_job

    def _run():
        from havn.server.deps import _get_shared_conn

        cursor = None
        try:
            cursor = _get_shared_conn().cursor()
            execute_job(job, plan, cursor, project_dir, trigger="manual")
        except Exception as e:
            logger.error("Job '%s' failed: %s", name, e)
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()


@router.post("/api/jobs")
def create_job(req: CreateJobRequest, request: Request):
    _require_permission(request, "write")
    from havn.engine.orchestration import save_job

    project_dir = _get_project_dir()
    data = req.model_dump(exclude_none=True)
    # Must have at least one of target/targets
    if not data.get("targets") and not data.get("target"):
        raise HTTPException(400, "Must provide 'targets' (list) or 'target' (string)")
    try:
        path = save_job(project_dir, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "created", "file": path.name}


@router.patch("/api/jobs/{name}")
def update_job(name: str, req: UpdateJobRequest, request: Request):
    _require_permission(request, "write")
    import yaml

    from havn.engine.orchestration import _find_job

    project_dir = _get_project_dir()
    job = _find_job(project_dir, name)
    if not job:
        raise HTTPException(404, f"Job '{name}' not found")
    data = yaml.safe_load(job.file_path.read_text()) or {}
    updates = req.model_dump(exclude_none=True)
    # If `targets` is sent, replace both targets and the mirrored target field
    if "targets" in updates:
        data["targets"] = updates["targets"]
        data["target"] = updates["targets"][0] if updates["targets"] else data.get("target", "")
        updates.pop("targets", None)
        updates.pop("target", None)
    # If `schedules` is sent, replace both schedules and the mirrored cron field
    if "schedules" in updates:
        new_schedules = updates["schedules"] or []
        # Validate each schedule
        for sched in new_schedules:
            if len(sched.split()) != 5:
                raise HTTPException(400, f"Invalid cron (need 5 fields): {sched}")
        data["schedules"] = new_schedules
        data["cron"] = new_schedules[0] if new_schedules else ""
        updates.pop("schedules", None)
        updates.pop("cron", None)
    data.update(updates)
    job.file_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    return {"status": "updated", "file": job.file_path.name}


@router.delete("/api/jobs/{name}")
def delete_job_endpoint(name: str, request: Request):
    _require_permission(request, "write")
    from havn.engine.orchestration import delete_job

    project_dir = _get_project_dir()
    if not delete_job(project_dir, name):
        raise HTTPException(404, f"Job '{name}' not found")
    return {"status": "deleted"}


@router.get("/api/jobs/{name}/history")
def get_job_history(name: str, request: Request, conn: DbConnReadOnly, limit: int = 50):
    _require_permission(request, "read")
    from havn.engine.orchestration import ensure_job_runs_table

    ensure_job_runs_table(conn)
    rows = conn.execute(
        "SELECT id, job_name, status, steps_total, steps_completed, steps_failed, "
        "steps_skipped, duration_ms, trigger, started_at, finished_at, error, step_details "
        "FROM _havn.job_runs WHERE job_name = ? ORDER BY started_at DESC LIMIT ?",
        [name, limit],
    ).fetchall()
    return [_run_row_to_dict(r) for r in rows]


# --- Job Runs endpoints ---


@router.get("/api/job-runs")
def list_job_runs(
    request: Request,
    conn: DbConnReadOnly,
    limit: int = 50,
    job: str | None = None,
):
    _require_permission(request, "read")
    from havn.engine.orchestration import ensure_job_runs_table

    ensure_job_runs_table(conn)
    query = (
        "SELECT id, job_name, status, steps_total, steps_completed, steps_failed, "
        "steps_skipped, duration_ms, trigger, started_at, finished_at, error, step_details "
        "FROM _havn.job_runs"
    )
    params: list = []
    if job:
        query += " WHERE job_name = ?"
        params.append(job)
    query += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [_run_row_to_dict(r) for r in rows]


@router.get("/api/job-runs/{run_id}")
def get_job_run(run_id: str, request: Request, conn: DbConnReadOnly):
    _require_permission(request, "read")
    from havn.engine.orchestration import ensure_job_runs_table

    ensure_job_runs_table(conn)
    rows = conn.execute(
        "SELECT id, job_name, status, steps_total, steps_completed, steps_failed, "
        "steps_skipped, duration_ms, trigger, started_at, finished_at, error, step_details "
        "FROM _havn.job_runs WHERE id = ?",
        [run_id],
    ).fetchall()
    if not rows:
        raise HTTPException(404, "Run not found")
    return _run_row_to_dict(rows[0])


@router.get("/api/step-preview/{schema_name}/{table_name}")
def get_step_preview(
    schema_name: str, table_name: str, request: Request, conn: DbConnReadOnly, limit: int = 10
):
    """Return a small sample of rows from a model table for inline preview.

    Masking policies are applied so non-exempt users never see raw PII.
    """
    user = _require_permission(request, "read")
    import re

    from havn.engine.masking import apply_masking
    from havn.engine.masking_rewriter import rewrite_query_with_masking

    # Validate identifiers to prevent injection
    ident_re = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    if not ident_re.match(schema_name) or not ident_re.match(table_name):
        raise HTTPException(400, "Invalid schema or table name")
    limit = min(max(limit, 1), 50)
    fqn = f"{schema_name}.{table_name}"
    try:
        preview_sql = f"SELECT * FROM {fqn} LIMIT {limit}"
        role = user.get("role", "viewer")
        rewritten, rw_ok, rw_handled = rewrite_query_with_masking(
            preview_sql, role, conn,
        )
        result = conn.execute(rewritten if rw_ok else preview_sql)
        columns = [desc[0] for desc in result.description]
        rows = [[_serialize_cell(v) for v in row] for row in result.fetchall()]
        # Post-query masking for unhandled policies
        if not rw_ok:
            rows = apply_masking(
                columns, rows, role, conn,
                schema=schema_name, table=table_name,
            )
        elif rw_handled:
            rows = apply_masking(
                columns, rows, role, conn,
                schema=schema_name, table=table_name,
                skip_policy_ids=rw_handled,
            )
        return {"columns": columns, "rows": rows, "table": fqn}
    except Exception as e:
        raise HTTPException(404, f"Table not found: {fqn} ({e})")


def _serialize_cell(value):
    """Make a cell value JSON-safe."""
    if value is None:
        return None
    if isinstance(value, (int, float, bool, str)):
        return value
    return str(value)


@router.post("/api/job-runs/{run_id}/cancel")
def cancel_job_run_endpoint(run_id: str, request: Request, conn: DbConn):
    _require_permission(request, "execute")
    from havn.engine.orchestration import cancel_job_run

    if not cancel_job_run(run_id):
        raise HTTPException(404, "Run not found or not running")
    conn.execute(
        "UPDATE _havn.job_runs SET status = 'cancelled' WHERE id = ? AND status = 'running'",
        [run_id],
    )
    return {"status": "cancelled"}
