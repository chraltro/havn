"""Orchestration jobs engine: discover, resolve, execute YAML-defined jobs."""

from __future__ import annotations

import datetime
import json
import logging
import re
import time
from dataclasses import dataclass, field
from graphlib import TopologicalSorter
from pathlib import Path

import yaml

logger = logging.getLogger("havn.orchestration")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Job:
    name: str
    target: str
    resolve: str = "upstream"            # "upstream" or "none"
    cron: str = ""
    enabled: bool = True
    notify: list[str] = field(default_factory=list)
    retry: int = 0
    retry_delay: int = 10                # seconds
    timeout_minutes: int = 60
    description: str = ""
    file_path: Path = field(default_factory=lambda: Path())


@dataclass
class ExecutionStep:
    step: int
    type: str          # "ingest", "transform", "export"
    target: str        # e.g. "ingest/orders.py" or "bronze.raw_orders"
    estimated_duration_ms: int = 0


@dataclass
class ExecutionPlan:
    steps: list[ExecutionStep] = field(default_factory=list)
    total_estimated_ms: int = 0


@dataclass
class JobResult:
    job_name: str
    status: str = "running"              # running/success/failure/timeout/cancelled
    steps_total: int = 0
    steps_completed: int = 0
    steps_failed: int = 0
    steps_skipped: int = 0
    duration_ms: int = 0
    step_details: list[dict] = field(default_factory=list)
    error: str | None = None
    run_id: str | None = None


# ---------------------------------------------------------------------------
# Table creation & maintenance
# ---------------------------------------------------------------------------


def ensure_job_runs_table(conn) -> None:
    """Create the _havn.job_runs table if it doesn't exist."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS _havn")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.job_runs (
            id              VARCHAR PRIMARY KEY DEFAULT gen_random_uuid()::VARCHAR,
            job_name        VARCHAR NOT NULL,
            job_file        VARCHAR NOT NULL,
            target          VARCHAR NOT NULL,
            status          VARCHAR NOT NULL DEFAULT 'running',
            steps_total     INTEGER NOT NULL DEFAULT 0,
            steps_completed INTEGER NOT NULL DEFAULT 0,
            steps_failed    INTEGER NOT NULL DEFAULT 0,
            steps_skipped   INTEGER NOT NULL DEFAULT 0,
            started_at      TIMESTAMP DEFAULT current_timestamp,
            finished_at     TIMESTAMP,
            duration_ms     BIGINT,
            trigger         VARCHAR DEFAULT 'manual',
            error           VARCHAR,
            step_details    JSON
        )
    """)
    # Migration for databases that predate steps_skipped
    try:
        conn.execute(
            "ALTER TABLE _havn.job_runs ADD COLUMN IF NOT EXISTS steps_skipped INTEGER DEFAULT 0"
        )
    except Exception:
        pass


def mark_stale_runs_failed(conn) -> int:
    """Mark any lingering 'running' rows as failed.

    Called once at server startup to clean up jobs that were mid-execution when
    the process crashed. Only rows with `started_at` older than 1 hour are
    touched — this protects against racing with a CLI invocation that happens
    to run in parallel with the server.

    Returns the number of rows updated.
    """
    ensure_job_runs_table(conn)
    try:
        cursor = conn.execute(
            "UPDATE _havn.job_runs SET status = 'failure', "
            "error = 'Server restarted while job was running', "
            "finished_at = current_timestamp "
            "WHERE status = 'running' "
            "AND started_at < (current_timestamp - INTERVAL '1 hour')"
        )
        # DuckDB returns affected rows via rowcount on the cursor object
        count = getattr(cursor, "rowcount", 0) or 0
        if count > 0:
            logger.info("Marked %d stale job_runs row(s) as failed", count)
        return int(count)
    except Exception as e:
        logger.debug("mark_stale_runs_failed failed: %s", e)
        return 0


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_jobs(project_dir: Path) -> list[Job]:
    """Glob orchestration/*.yml, parse each, return list of Job objects."""
    orch_dir = project_dir / "orchestration"
    if not orch_dir.exists():
        return []
    jobs: list[Job] = []
    for yml_file in sorted(orch_dir.glob("*.yml")):
        try:
            data = yaml.safe_load(yml_file.read_text())
            if not isinstance(data, dict) or "target" not in data:
                logger.warning("Skipping invalid job file: %s", yml_file.name)
                continue
            # Validate cron (5 fields) if present
            cron = data.get("cron", "") or ""
            if cron and len(str(cron).strip().split()) != 5:
                logger.warning("Invalid cron in %s: %s", yml_file.name, cron)
                continue
            jobs.append(Job(
                name=data.get("name", yml_file.stem),
                target=data["target"],
                resolve=data.get("resolve", "upstream"),
                cron=str(cron),
                enabled=data.get("enabled", True),
                notify=data.get("notify", []) or [],
                retry=int(data.get("retry", 0) or 0),
                retry_delay=int(data.get("retry_delay", 10) or 10),
                timeout_minutes=int(data.get("timeout_minutes", 60) or 60),
                description=data.get("description", "") or "",
                file_path=yml_file,
            ))
        except Exception as e:
            logger.warning("Error parsing %s: %s", yml_file.name, e)
    return jobs


def _find_job(project_dir: Path, name: str) -> Job | None:
    """Find a job by name (slug match on filename or name field)."""
    for job in discover_jobs(project_dir):
        if job.file_path.stem == name or job.name == name:
            return job
    return None


# ---------------------------------------------------------------------------
# Plan resolution
# ---------------------------------------------------------------------------


def _collect_upstream(target_name: str, model_map: dict, visited: set) -> None:
    """Recursively collect all upstream model names."""
    if target_name in visited or target_name not in model_map:
        return
    visited.add(target_name)
    for dep in model_map[target_name].depends_on:
        _collect_upstream(dep, model_map, visited)


def _validate_script_target(project_dir: Path, target: str) -> None:
    """Reject script targets that escape the project directory.

    Raises ValueError if the target resolves outside project_dir.
    """
    if ".." in target.split("/") or ".." in target.split("\\"):
        raise ValueError(f"Invalid target (contains '..'): {target}")
    try:
        resolved = (project_dir / target).resolve()
        project_resolved = project_dir.resolve()
        if not str(resolved).startswith(str(project_resolved)):
            raise ValueError(f"Target escapes project directory: {target}")
    except (OSError, RuntimeError) as e:
        raise ValueError(f"Invalid target path {target}: {e}") from e


def resolve_execution_plan(
    target: str,
    dag: list,
    project_dir: Path,
    conn=None,
    resolve: str = "upstream",
) -> ExecutionPlan:
    """Build an ordered execution plan for a target.

    Handles model targets (schema.name), script targets (ingest/*.py, export/*.py),
    and wildcard targets (bronze.*).

    Args:
        target: The target to resolve.
        dag: List of SQLModels from build_dag().
        project_dir: Project root.
        conn: Optional DB connection (used for historical duration estimates).
        resolve: "upstream" to include all transitive deps, "none" to run only
            the literal target (no upstream rebuilds).
    """
    model_map = {m.full_name: m for m in dag}
    steps: list[ExecutionStep] = []
    needed_models: set[str] = set()
    is_export_target = target.startswith("export/")
    is_ingest_target = target.startswith("ingest/")
    is_wildcard = "*" in target

    # Validate script targets for path traversal
    if is_ingest_target or is_export_target:
        _validate_script_target(project_dir, target)

    if is_ingest_target:
        # Ingest script — no upstream, just run it
        steps.append(ExecutionStep(step=1, type="ingest", target=target))
        return ExecutionPlan(steps=steps)

    if is_wildcard:
        # Expand wildcard: e.g. "bronze.*" -> all models with schema=bronze
        schema_prefix = target.replace(".*", "")
        matching = [
            m.full_name
            for m in dag
            if m.schema == schema_prefix or m.full_name.startswith(schema_prefix + ".")
        ]
        if not matching:
            logger.warning(
                "Wildcard target '%s' matched no models in the DAG", target
            )
        if resolve == "none":
            needed_models = set(matching)
        else:
            for mname in matching:
                _collect_upstream(mname, model_map, needed_models)
    elif is_export_target:
        # Export script — try to find deps from its Python source
        script_path = project_dir / target
        if script_path.exists() and resolve != "none":
            try:
                source = script_path.read_text()
            except Exception:
                source = ""
            for m_name in model_map:
                if m_name in source:
                    _collect_upstream(m_name, model_map, needed_models)
        # If no deps detected, just run the export itself — do NOT rebuild the
        # entire warehouse. Users wanting a full rebuild should name a wildcard
        # target explicitly.
    else:
        # Model target: schema.name
        if target not in model_map:
            logger.warning("Target '%s' not found in the DAG", target)
        if resolve == "none":
            if target in model_map:
                needed_models.add(target)
        else:
            _collect_upstream(target, model_map, needed_models)

    # Find ingest scripts needed: models depending on landing.* tables
    ingest_scripts: list[str] = []
    landing_deps: set[str] = set()
    if resolve != "none":
        for name in needed_models:
            if name in model_map:
                for dep in model_map[name].depends_on:
                    if dep.startswith("landing.") and dep not in model_map:
                        landing_deps.add(dep)
        # Try to match landing tables to ingest scripts
        ingest_dir = project_dir / "ingest"
        if ingest_dir.exists() and landing_deps:
            for script in sorted(ingest_dir.glob("*.py")):
                if script.name.startswith("_"):
                    continue
                # Heuristic: read script source, check if it references any landing table
                try:
                    src = script.read_text()
                    for ldep in landing_deps:
                        if ldep in src or ldep.split(".")[-1] in src:
                            rel = f"ingest/{script.name}"
                            if rel not in ingest_scripts:
                                ingest_scripts.append(rel)
                            break
                except Exception:
                    pass

    # Topological sort the needed models
    sorter: TopologicalSorter[str] = TopologicalSorter()
    for name in needed_models:
        if name in model_map:
            known_deps = [d for d in model_map[name].depends_on if d in needed_models]
            sorter.add(name, *known_deps)
    sorted_models = [n for n in sorter.static_order() if n in needed_models and n in model_map]

    # Estimate durations from historical run_log
    estimates: dict[str, int] = {}
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT target, AVG(duration_ms)::INTEGER FROM _havn.run_log "
                "WHERE status = 'success' GROUP BY target"
            ).fetchall()
            estimates = {r[0]: r[1] for r in rows if r[0] is not None and r[1] is not None}
        except Exception:
            pass

    # Build step list: ingest first, then transforms, then export target
    step_num = 1
    for script in ingest_scripts:
        steps.append(ExecutionStep(
            step=step_num, type="ingest", target=script,
            estimated_duration_ms=estimates.get(script, 0),
        ))
        step_num += 1
    for model_name in sorted_models:
        steps.append(ExecutionStep(
            step=step_num, type="transform", target=model_name,
            estimated_duration_ms=estimates.get(model_name, 0),
        ))
        step_num += 1
    if is_export_target:
        steps.append(ExecutionStep(
            step=step_num, type="export", target=target,
            estimated_duration_ms=estimates.get(target, 0),
        ))

    total_est = sum(s.estimated_duration_ms for s in steps)
    return ExecutionPlan(steps=steps, total_estimated_ms=total_est)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


# Module-level cancel flags for running jobs
_cancel_flags: dict[str, bool] = {}


def _interruptible_sleep(seconds: float, run_id: str, start: float, timeout_ms: int) -> str:
    """Sleep in small increments, returning early on cancel or timeout.

    Returns:
        "cancelled" if the cancel flag fires,
        "timeout" if the job timeout is reached,
        "ok" if the sleep completed normally.
    """
    end = time.perf_counter() + seconds
    while True:
        if _cancel_flags.get(run_id, False):
            return "cancelled"
        if int((time.perf_counter() - start) * 1000) > timeout_ms:
            return "timeout"
        remaining = end - time.perf_counter()
        if remaining <= 0:
            return "ok"
        time.sleep(min(0.5, remaining))


def execute_job(
    job: Job,
    plan: ExecutionPlan,
    conn,
    project_dir: Path,
    trigger: str = "manual",
) -> JobResult:
    """Execute a job's plan step by step, logging to _havn.job_runs."""
    from havn.engine.runner import run_script
    from havn.engine.transform.discovery import (
        _compute_upstream_hash,
        _update_state,
        build_dag,
        discover_models,
    )
    from havn.engine.transform.execution import execute_model

    ensure_job_runs_table(conn)

    # Insert initial run row
    run_id = conn.execute("SELECT gen_random_uuid()::VARCHAR").fetchone()[0]
    try:
        rel_path = str(job.file_path.relative_to(project_dir))
    except (ValueError, AttributeError):
        rel_path = str(job.file_path)
    conn.execute(
        "INSERT INTO _havn.job_runs (id, job_name, job_file, target, status, steps_total, trigger) "
        "VALUES (?, ?, ?, ?, 'running', ?, ?)",
        [run_id, job.name, rel_path, job.target, len(plan.steps), trigger],
    )

    result = JobResult(job_name=job.name, steps_total=len(plan.steps), run_id=run_id)
    _cancel_flags[run_id] = False
    start = time.perf_counter()
    timeout_ms = job.timeout_minutes * 60 * 1000
    failed_targets: set[str] = set()

    try:
        # Build model map for transform steps
        models = discover_models(project_dir / "transform")
        dag_sorted = build_dag(models)
        model_map = {m.full_name: m for m in dag_sorted}
        # Compute upstream hashes
        for m in dag_sorted:
            m.upstream_hash = _compute_upstream_hash(m, model_map)

        for step in plan.steps:
            # Check cancellation
            if _cancel_flags.get(run_id, False):
                result.status = "cancelled"
                break

            # Check timeout
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            if elapsed_ms > timeout_ms:
                result.status = "timeout"
                result.error = f"Job exceeded timeout of {job.timeout_minutes} minutes"
                break

            # Skip if upstream failed (for transform steps)
            if step.type == "transform" and step.target in model_map:
                model = model_map[step.target]
                upstream_failed = any(d in failed_targets for d in model.depends_on)
                if upstream_failed:
                    result.step_details.append({
                        "step": step.step,
                        "type": step.type,
                        "target": step.target,
                        "status": "skipped",
                        "duration_ms": 0,
                        "rows_affected": 0,
                        "error": "Upstream dependency failed",
                    })
                    result.steps_skipped += 1
                    failed_targets.add(step.target)  # propagate to further downstream
                    continue

            step_start = time.perf_counter()
            step_result: dict = {"step": step.step, "type": step.type, "target": step.target}

            try:
                if step.type in ("ingest", "export"):
                    script_path = project_dir / step.target
                    r = run_script(conn, script_path, step.type, use_circuit_breaker=False)
                    step_result["status"] = r.get("status", "error")
                    step_result["duration_ms"] = r.get("duration_ms", 0)
                    step_result["rows_affected"] = r.get("rows_affected", 0)
                    if r.get("error"):
                        step_result["error"] = r["error"]
                elif step.type == "transform":
                    model = model_map.get(step.target)
                    if model is None:
                        step_result["status"] = "error"
                        step_result["error"] = f"Model not found: {step.target}"
                        step_result["duration_ms"] = int((time.perf_counter() - step_start) * 1000)
                    else:
                        duration_ms, row_count = execute_model(conn, model)
                        _update_state(conn, model, duration_ms, row_count)
                        step_result["status"] = "success"
                        step_result["duration_ms"] = duration_ms
                        step_result["rows_affected"] = row_count
            except Exception as e:
                step_result["status"] = "error"
                step_result["error"] = str(e)
                step_result["duration_ms"] = int((time.perf_counter() - step_start) * 1000)

            # Handle retries for failed steps
            if step_result.get("status") == "error" and job.retry > 0:
                for _attempt in range(job.retry):
                    sleep_outcome = _interruptible_sleep(
                        job.retry_delay, run_id, start, timeout_ms
                    )
                    if sleep_outcome != "ok":
                        # Cancellation or timeout during retry wait — stop retrying
                        break
                    try:
                        if step.type in ("ingest", "export"):
                            r = run_script(
                                conn,
                                project_dir / step.target,
                                step.type,
                                use_circuit_breaker=False,
                            )
                            if r.get("status") == "success":
                                step_result["status"] = "success"
                                step_result["duration_ms"] = r.get("duration_ms", 0)
                                step_result["rows_affected"] = r.get("rows_affected", 0)
                                step_result.pop("error", None)
                                break
                        elif step.type == "transform" and step.target in model_map:
                            duration_ms, row_count = execute_model(conn, model_map[step.target])
                            _update_state(conn, model_map[step.target], duration_ms, row_count)
                            step_result["status"] = "success"
                            step_result["duration_ms"] = duration_ms
                            step_result["rows_affected"] = row_count
                            step_result.pop("error", None)
                            break
                    except Exception:
                        pass

            result.step_details.append(step_result)
            status = step_result.get("status")
            if status == "success":
                result.steps_completed += 1
            elif status == "error":
                result.steps_failed += 1
                failed_targets.add(step.target)
            elif status == "skipped":
                result.steps_skipped += 1
            else:
                # Unknown status — treat as a failure so totals stay consistent
                result.steps_failed += 1
                failed_targets.add(step.target)

            # Update progress in DB
            try:
                conn.execute(
                    "UPDATE _havn.job_runs SET steps_completed = ?, steps_failed = ?, "
                    "steps_skipped = ?, step_details = ? WHERE id = ?",
                    [
                        result.steps_completed,
                        result.steps_failed,
                        result.steps_skipped,
                        json.dumps(result.step_details),
                        run_id,
                    ],
                )
            except Exception as e:
                logger.debug("Progress update failed: %s", e)

        # Final status
        if result.status == "running":
            result.status = "failure" if result.steps_failed > 0 else "success"
        result.duration_ms = int((time.perf_counter() - start) * 1000)

        # Update final run row.
        # WHERE clause ensures we don't overwrite a row that was cancelled out-of-band
        # by cancel_job_run_endpoint between the cancel flag set and this write.
        try:
            conn.execute(
                "UPDATE _havn.job_runs SET status = ?, finished_at = current_timestamp, "
                "duration_ms = ?, error = ?, step_details = ?, "
                "steps_completed = ?, steps_failed = ?, steps_skipped = ? "
                "WHERE id = ? AND status != 'cancelled'",
                [
                    result.status,
                    result.duration_ms,
                    result.error,
                    json.dumps(result.step_details),
                    result.steps_completed,
                    result.steps_failed,
                    result.steps_skipped,
                    run_id,
                ],
            )
        except Exception as e:
            logger.debug("Final run update failed: %s", e)
    finally:
        _cancel_flags.pop(run_id, None)

    return result


def cancel_job_run(run_id: str) -> bool:
    """Signal a running job to cancel."""
    if run_id in _cancel_flags:
        _cancel_flags[run_id] = True
        return True
    return False


# ---------------------------------------------------------------------------
# Preview & helpers
# ---------------------------------------------------------------------------


def preview_plan(
    target: str,
    dag: list,
    project_dir: Path,
    conn=None,
    resolve: str = "upstream",
) -> dict:
    """Return a JSON-serializable plan preview without executing."""
    plan = resolve_execution_plan(target, dag, project_dir, conn=conn, resolve=resolve)
    return {
        "steps": [
            {
                "step": s.step,
                "type": s.type,
                "target": s.target,
                "estimated_duration_ms": s.estimated_duration_ms,
            }
            for s in plan.steps
        ],
        "total_steps": len(plan.steps),
        "total_estimated_ms": plan.total_estimated_ms,
        "ingest_count": sum(1 for s in plan.steps if s.type == "ingest"),
        "transform_count": sum(1 for s in plan.steps if s.type == "transform"),
        "export_count": sum(1 for s in plan.steps if s.type == "export"),
    }


def get_next_run(cron_expr: str) -> str | None:
    """Calculate the next run time for a cron expression. Returns ISO string."""
    if not cron_expr or not cron_expr.strip():
        return None
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return None
    try:
        now = datetime.datetime.now()
        # Check every minute for the next 48 hours
        candidate = now.replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)
        for _ in range(48 * 60):
            if _matches_cron(parts, candidate):
                return candidate.isoformat()
            candidate += datetime.timedelta(minutes=1)
    except Exception:
        return None
    return None


def _matches_cron(parts: list[str], dt: datetime.datetime) -> bool:
    """Check if a datetime matches a 5-field cron expression.

    Uses POSIX cron convention for weekday: 0=Sunday, 1=Monday, ..., 6=Saturday.
    Python's datetime.weekday() returns 0=Monday..6=Sunday, so we convert.
    """
    # POSIX cron weekday: 0=Sun..6=Sat. Python's weekday(): 0=Mon..6=Sun.
    # (weekday + 1) % 7 -> Mon(0)->1, Tue(1)->2, ..., Sat(5)->6, Sun(6)->0
    posix_weekday = (dt.weekday() + 1) % 7
    checks = [
        (parts[0], dt.minute),
        (parts[1], dt.hour),
        (parts[2], dt.day),
        (parts[3], dt.month),
        (parts[4], posix_weekday),
    ]
    for pattern, current in checks:
        if pattern == "*":
            continue
        try:
            if "/" in pattern:
                _, step_str = pattern.split("/", 1)
                step = int(step_str)
                if step <= 0:
                    return False
                if current % step != 0:
                    return False
            elif "," in pattern:
                if current not in [int(v) for v in pattern.split(",")]:
                    return False
            elif "-" in pattern:
                lo, hi = pattern.split("-", 1)
                if not (int(lo) <= current <= int(hi)):
                    return False
            elif current != int(pattern):
                return False
        except (ValueError, ZeroDivisionError):
            return False
    return True


def save_job(project_dir: Path, job_data: dict) -> Path:
    """Write a job YAML file to orchestration/."""
    orch_dir = project_dir / "orchestration"
    orch_dir.mkdir(exist_ok=True)
    raw_name = str(job_data.get("name", "job")).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw_name).strip("-") or "job"
    # Validate target for path traversal before persisting
    target = str(job_data.get("target", ""))
    if target.startswith(("ingest/", "export/")):
        _validate_script_target(project_dir, target)
    if ".." in target:
        raise ValueError(f"Invalid target (contains '..'): {target}")
    path = orch_dir / f"{slug}.yml"
    path.write_text(yaml.dump(job_data, default_flow_style=False, sort_keys=False))
    return path


def delete_job(project_dir: Path, name: str) -> bool:
    """Delete a job YAML file."""
    job = _find_job(project_dir, name)
    if job and job.file_path.exists():
        job.file_path.unlink()
        return True
    return False
