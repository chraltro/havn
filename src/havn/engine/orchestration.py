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
    target: str                          # legacy single-target (= targets[0])
    resolve: str = "upstream"            # "upstream" or "none"
    cron: str = ""                       # legacy single-schedule (= schedules[0])
    enabled: bool = True
    notify: list[str] = field(default_factory=list)
    retry: int = 0
    retry_delay: int = 10                # seconds
    timeout_minutes: int = 60
    description: str = ""
    file_path: Path = field(default_factory=lambda: Path())
    targets: list[str] = field(default_factory=list)      # preferred multi-target
    schedules: list[str] = field(default_factory=list)    # preferred multi-schedule
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Mirror target <-> targets for backward compatibility
        if not self.targets and self.target:
            self.targets = [self.target]
        if self.targets and not self.target:
            self.target = self.targets[0]
        # Mirror cron <-> schedules
        if not self.schedules and self.cron:
            self.schedules = [self.cron]
        if self.schedules and not self.cron:
            self.cron = self.schedules[0]


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
    """Glob orchestration/*.yml, parse each, return list of Job objects.

    Accepts either ``target: <str>`` (legacy single-target) or ``targets:
    [<str>, ...]`` (preferred multi-target). If both are present, ``targets``
    wins and ``target`` is set to the first element.
    """
    orch_dir = project_dir / "orchestration"
    if not orch_dir.exists():
        return []
    jobs: list[Job] = []
    for yml_file in sorted(orch_dir.glob("*.yml")):
        try:
            data = yaml.safe_load(yml_file.read_text())
            if not isinstance(data, dict):
                logger.warning("Skipping invalid job file: %s", yml_file.name)
                continue
            # Accept either `targets` (list) or `target` (single)
            raw_targets = data.get("targets")
            targets: list[str] = []
            if isinstance(raw_targets, list) and raw_targets:
                targets = [str(t) for t in raw_targets if t]
            elif "target" in data and data["target"]:
                targets = [str(data["target"])]
            if not targets:
                logger.warning(
                    "Skipping %s: no target/targets defined", yml_file.name
                )
                continue
            # Accept either `schedules` (list) or `cron` (single, legacy)
            raw_schedules = data.get("schedules")
            schedules: list[str] = []
            if isinstance(raw_schedules, list):
                schedules = [str(s).strip() for s in raw_schedules if s]
            elif data.get("cron"):
                schedules = [str(data["cron"]).strip()]
            # Validate each schedule — accept cron OR interval syntax.
            # Drop invalid entries individually so the rest of the job still
            # loads and the user can fix them in place.
            valid_schedules: list[str] = []
            for sched in schedules:
                if not sched:
                    continue
                if not is_valid_schedule(sched):
                    logger.warning(
                        "Invalid schedule in %s: %s (skipping)",
                        yml_file.name,
                        sched,
                    )
                    continue
                valid_schedules.append(sched)
            tags = data.get("tags", []) or []
            if not isinstance(tags, list):
                tags = [str(tags)]
            jobs.append(Job(
                name=data.get("name", yml_file.stem),
                target=targets[0],
                targets=targets,
                resolve=data.get("resolve", "upstream"),
                cron=valid_schedules[0] if valid_schedules else "",
                schedules=valid_schedules,
                tags=[str(t) for t in tags if t],
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


def _collect_downstream(
    target_name: str, model_map: dict, visited: set
) -> None:
    """Recursively collect all downstream model names for a given target."""
    if target_name in visited:
        return
    visited.add(target_name)
    for name, model in model_map.items():
        if target_name in (model.depends_on or []):
            _collect_downstream(name, model_map, visited)


def _parse_selector(target: str) -> tuple[bool, bool, str]:
    """Parse a dbt-style selector into (include_upstream, include_downstream, bare_target).

    ``+foo`` -> (True, False, "foo")
    ``foo+`` -> (False, True, "foo")
    ``+foo+`` -> (True, True, "foo")
    ``foo`` -> (False, False, "foo")

    The legacy ``+downstream:`` prefix is still accepted for back-compat.
    """
    if target.startswith("+downstream:"):  # legacy
        return (False, True, target[len("+downstream:") :])
    up = False
    down = False
    inner = target
    if inner.startswith("+"):
        up = True
        inner = inner[1:]
    if inner.endswith("+"):
        down = True
        inner = inner[:-1]
    return (up, down, inner)


def resolve_execution_plan(
    targets: str | list[str],
    dag: list,
    project_dir: Path,
    conn=None,
    resolve: str = "upstream",
) -> ExecutionPlan:
    """Build an ordered execution plan for one or more targets.

    Accepts either a single target string (legacy) or a list of targets. All
    targets are unioned into a single plan with steps in dependency order.

    Each target string is a **dbt-style selector**:

    - ``schema.name`` — just this model, no upstream, no downstream
    - ``+schema.name`` — this model plus every upstream dependency
    - ``schema.name+`` — this model plus every downstream consumer
    - ``+schema.name+`` — upstream + this + downstream
    - ``schema.*`` — wildcard over every model in ``schema``
    - ``+schema.*`` / ``schema.*+`` / ``+schema.*+`` — upstream/downstream of all matched
    - ``ingest/script.py`` — run that ingest script
    - ``export/script.py`` — run that export script (and its referenced models
      when the selector includes ``+`` prefix)
    - ``ingest/script.py+`` — run ingest, then all models that eventually
      depend on tables it creates (handy for a fresh-data refresh)

    Args:
        targets: One or more targets.
        dag: List of SQLModels from build_dag().
        project_dir: Project root.
        conn: Optional DB connection (used for duration estimates).
        resolve: Legacy knob. When "upstream" (the old default), bare targets
            without ``+`` markers are treated as ``+target`` for backward
            compatibility with jobs saved before selectors existed. When set
            to "none", bare targets run literally with no expansion. New jobs
            should rely on the selector syntax and leave this at the default.
    """
    if isinstance(targets, str):
        targets = [targets]
    # Deduplicate while preserving order
    seen: set[str] = set()
    targets = [t for t in targets if not (t in seen or seen.add(t))]

    model_map = {m.full_name: m for m in dag}
    needed_models: set[str] = set()
    explicit_ingest_scripts: list[str] = []
    explicit_export_scripts: list[str] = []

    for target in targets:
        up, down, inner = _parse_selector(target)
        # If no explicit selector markers, honour the legacy resolve mode
        # so existing jobs keep working
        if not up and not down and resolve == "upstream":
            up = True

        is_ingest_target = inner.startswith("ingest/")
        is_export_target = inner.startswith("export/")
        is_wildcard = "*" in inner

        if is_ingest_target or is_export_target:
            _validate_script_target(project_dir, inner)

        if is_ingest_target:
            if inner not in explicit_ingest_scripts:
                explicit_ingest_scripts.append(inner)
            # +ingest/x.py doesn't make sense (no upstream); we silently
            # ignore the + prefix. ingest/x.py+ pulls in every model that
            # depends on tables this script creates.
            if down:
                # Scan the script source to discover landing tables it creates
                script_path = project_dir / inner
                try:
                    src = script_path.read_text() if script_path.exists() else ""
                except Exception:
                    src = ""
                # For each model whose depends_on matches something in the
                # ingest script's source, treat it as downstream origin
                for m_name, m in model_map.items():
                    for dep in m.depends_on or []:
                        if dep not in model_map and (dep in src or dep.split(".")[-1] in src):
                            needed_models.add(m_name)
                            downstream: set[str] = set()
                            _collect_downstream(m_name, model_map, downstream)
                            needed_models.update(downstream)
                            break
            continue

        if is_export_target:
            if inner not in explicit_export_scripts:
                explicit_export_scripts.append(inner)
            # +export/x.py pulls in the models it references + their upstream
            if up:
                script_path = project_dir / inner
                try:
                    src = script_path.read_text() if script_path.exists() else ""
                except Exception:
                    src = ""
                for m_name in model_map:
                    if m_name in src:
                        _collect_upstream(m_name, model_map, needed_models)
            # export+ doesn't make sense (exports are terminal); ignore down
            continue

        if is_wildcard:
            schema_prefix = inner.replace(".*", "")
            matching = [
                m.full_name
                for m in dag
                if m.schema == schema_prefix
                or m.full_name.startswith(schema_prefix + ".")
            ]
            if not matching:
                logger.warning("Wildcard target '%s' matched no models", target)
            for mname in matching:
                if up:
                    _collect_upstream(mname, model_map, needed_models)
                else:
                    needed_models.add(mname)
                if down:
                    downstream = set()
                    _collect_downstream(mname, model_map, downstream)
                    if resolve == "upstream":
                        for m_name in downstream:
                            _collect_upstream(m_name, model_map, needed_models)
                    else:
                        needed_models.update(downstream)
            continue

        # Plain model target
        if inner not in model_map:
            logger.warning("Target '%s' not found in the DAG", target)
            continue
        if up:
            _collect_upstream(inner, model_map, needed_models)
        else:
            needed_models.add(inner)
        if down:
            downstream = set()
            _collect_downstream(inner, model_map, downstream)
            if resolve == "upstream":
                for m_name in downstream:
                    _collect_upstream(m_name, model_map, needed_models)
            else:
                needed_models.update(downstream)

    # Find ingest scripts that feed the selected models. Previously this only
    # considered ``landing.*`` deps, which silently dropped ingest steps for
    # projects using any other source schema (``raw.``, ``source.``, etc.).
    # Now we treat ANY dep not in the model_map as an external/source table
    # candidate and match it against ingest script sources.
    ingest_scripts: list[str] = list(explicit_ingest_scripts)
    external_deps: set[str] = set()
    if resolve != "none":
        for name in needed_models:
            if name in model_map:
                for dep in model_map[name].depends_on or []:
                    if dep not in model_map:
                        external_deps.add(dep)
        ingest_dir = project_dir / "ingest"
        if ingest_dir.exists() and external_deps:
            ingest_files = list(ingest_dir.glob("*.py")) + list(ingest_dir.glob("*.dpnb"))
            for script in sorted(ingest_files, key=lambda p: p.name):
                if script.name.startswith("_"):
                    continue
                try:
                    src = script.read_text()
                    for dep in external_deps:
                        # Match on the fully-qualified name first, then fall
                        # back to the bare table name so scripts that build
                        # the table via ``CREATE TABLE raw.orders AS`` or
                        # via a pandas ``to_sql('orders', schema='raw')``
                        # both get picked up.
                        if dep in src or dep.split(".")[-1] in src:
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
            known_deps = [
                d for d in (model_map[name].depends_on or []) if d in needed_models
            ]
            sorter.add(name, *known_deps)
    sorted_models = [
        n for n in sorter.static_order() if n in needed_models and n in model_map
    ]

    # Duration estimates from run_log
    estimates: dict[str, int] = {}
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT target, AVG(duration_ms)::INTEGER FROM _havn.run_log "
                "WHERE status = 'success' GROUP BY target"
            ).fetchall()
            estimates = {
                r[0]: r[1]
                for r in rows
                if r[0] is not None and r[1] is not None
            }
        except Exception:
            pass

    # Build ordered step list
    steps: list[ExecutionStep] = []
    step_num = 1
    for script in ingest_scripts:
        steps.append(
            ExecutionStep(
                step=step_num,
                type="ingest",
                target=script,
                estimated_duration_ms=estimates.get(script, 0),
            )
        )
        step_num += 1
    for model_name in sorted_models:
        steps.append(
            ExecutionStep(
                step=step_num,
                type="transform",
                target=model_name,
                estimated_duration_ms=estimates.get(model_name, 0),
            )
        )
        step_num += 1
    for export_script in explicit_export_scripts:
        steps.append(
            ExecutionStep(
                step=step_num,
                type="export",
                target=export_script,
                estimated_duration_ms=estimates.get(export_script, 0),
            )
        )
        step_num += 1

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
    targets: str | list[str],
    dag: list,
    project_dir: Path,
    conn=None,
    resolve: str = "upstream",
) -> dict:
    """Return a JSON-serializable plan preview without executing."""
    plan = resolve_execution_plan(targets, dag, project_dir, conn=conn, resolve=resolve)
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


# ---------------------------------------------------------------------------
# Interval schedules (alongside standard cron)
# ---------------------------------------------------------------------------
#
# 5-field cron cannot express "every 2 weeks" or "every 3 days from now" —
# these require state (when was the last run). We accept a second schedule
# format: human strings like ``every 2 weeks`` or ``every 3 days`` that the
# scheduler evaluates as "elapsed since last fire >= interval". The two
# formats coexist in the same ``schedules`` list.
#
# Supported units: minute(s), hour(s), day(s), week(s), month(s), year(s).
# Month = 30 days, year = 365 days (approximations; exact calendar
# arithmetic isn't needed for scheduling pipelines).

_INTERVAL_RE = re.compile(
    r"^\s*every\s+(\d+)\s+(minute|hour|day|week|month|year)s?\s*$",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 86400 * 7,
    "month": 86400 * 30,
    "year": 86400 * 365,
}


def is_interval_schedule(expr: str) -> bool:
    return bool(_INTERVAL_RE.match(expr or ""))


def parse_interval(expr: str) -> datetime.timedelta | None:
    """Parse an ``every N unit`` interval string into a ``timedelta``."""
    m = _INTERVAL_RE.match(expr or "")
    if not m:
        return None
    n = int(m.group(1))
    if n <= 0:
        return None
    unit = m.group(2).lower()
    return datetime.timedelta(seconds=n * _UNIT_SECONDS[unit])


def is_valid_schedule(expr: str) -> bool:
    """Accepts either 5-field cron or an ``every N unit`` interval string."""
    if not expr or not expr.strip():
        return False
    if is_interval_schedule(expr):
        return parse_interval(expr) is not None
    return len(expr.strip().split()) == 5


def describe_interval(expr: str) -> str | None:
    m = _INTERVAL_RE.match(expr or "")
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    return f"Every {n} {unit}{'s' if n != 1 else ''}"


def get_next_run(cron_expr: str, last_fire_iso: str | None = None) -> str | None:
    """Calculate the next run time for a cron or interval expression.

    For cron: iterates forward minute-by-minute until a match is found.
    For interval: returns ``last_fire + interval``, or ``now`` if there is
    no prior fire.

    Returns an ISO 8601 string or ``None`` for invalid expressions.
    """
    if not cron_expr or not cron_expr.strip():
        return None
    # Interval path
    if is_interval_schedule(cron_expr):
        delta = parse_interval(cron_expr)
        if delta is None:
            return None
        if last_fire_iso:
            try:
                base = datetime.datetime.fromisoformat(last_fire_iso.replace("Z", "+00:00"))
                if base.tzinfo is not None:
                    base = base.replace(tzinfo=None)
            except Exception:
                base = datetime.datetime.now()
        else:
            base = datetime.datetime.now()
        return (base + delta).isoformat()
    # Cron path
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return None
    try:
        now = datetime.datetime.now()
        candidate = now.replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)
        for _ in range(48 * 60):
            if _matches_cron(parts, candidate):
                return candidate.isoformat()
            candidate += datetime.timedelta(minutes=1)
    except Exception:
        return None
    return None


def get_earliest_next_run(
    schedules: list[str],
    last_fire_iso: str | None = None,
) -> str | None:
    """Return the earliest upcoming run across a list of schedules.

    For interval schedules, ``last_fire_iso`` is used as the base timestamp
    (the last successful fire of the job). If omitted, intervals are
    computed from "now" (i.e. the job would fire one interval from now).
    """
    if not schedules:
        return None
    candidates: list[str] = []
    for sched in schedules:
        result = get_next_run(sched, last_fire_iso=last_fire_iso)
        if result:
            candidates.append(result)
    if not candidates:
        return None
    return min(candidates)


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
    """Write a job YAML file to ``orchestration/``.

    Accepts either legacy single-value fields (``target``, ``cron``) or the
    preferred list fields (``targets``, ``schedules``). Both are validated.
    When both are present, the list fields win and the single-value fields
    are normalized to the first element for backward compatibility.
    """
    orch_dir = project_dir / "orchestration"
    orch_dir.mkdir(exist_ok=True)
    raw_name = str(job_data.get("name", "job")).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw_name).strip("-") or "job"

    # Normalize targets
    raw_targets = job_data.get("targets")
    targets: list[str] = []
    if isinstance(raw_targets, list) and raw_targets:
        targets = [str(t) for t in raw_targets if t]
    elif job_data.get("target"):
        targets = [str(job_data["target"])]
    if not targets:
        raise ValueError("Job must have at least one target")

    for t in targets:
        if ".." in t:
            raise ValueError(f"Invalid target (contains '..'): {t}")
        if t.startswith(("ingest/", "export/")):
            _validate_script_target(project_dir, t)
        if t.startswith("+downstream:"):
            inner = t[len("+downstream:") :]
            if ".." in inner:
                raise ValueError(f"Invalid target (contains '..'): {t}")

    # Normalize schedules: list form is canonical; fall back to legacy cron
    raw_schedules = job_data.get("schedules")
    schedules: list[str] = []
    if isinstance(raw_schedules, list):
        schedules = [str(s).strip() for s in raw_schedules if s]
    elif job_data.get("cron"):
        schedules = [str(job_data["cron"]).strip()]
    for sched in schedules:
        if not is_valid_schedule(sched):
            raise ValueError(
                f"Invalid schedule (expected cron 5-field or 'every N unit'): {sched!r}"
            )

    # Normalize tags
    tags = job_data.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    tags = [str(t).strip() for t in tags if t]

    out_data = dict(job_data)
    out_data["targets"] = targets
    out_data["target"] = targets[0]
    if schedules:
        out_data["schedules"] = schedules
        out_data["cron"] = schedules[0]
    else:
        out_data.pop("schedules", None)
        out_data["cron"] = ""
    out_data["tags"] = tags

    path = orch_dir / f"{slug}.yml"
    path.write_text(yaml.dump(out_data, default_flow_style=False, sort_keys=False))
    return path


def delete_job(project_dir: Path, name: str) -> bool:
    """Delete a job YAML file."""
    job = _find_job(project_dir, name)
    if job and job.file_path.exists():
        job.file_path.unlink()
        return True
    return False
