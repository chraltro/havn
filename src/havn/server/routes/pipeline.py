"""Pipeline execution, streams, history, and scheduler endpoints."""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from havn.server.deps import (
    DbConn,
    _get_config,
    _get_project_dir,
    _require_permission,
    ensure_meta_table,
    run_transform,
)
from havn.engine.write_queue import cursor_for

logger = logging.getLogger("havn.server")

router = APIRouter()


# --- Pydantic models ---


class RunScriptRequest(BaseModel):
    script_path: str = Field(..., min_length=1, max_length=500)
    force: bool = False


class PipelineStartRequest(BaseModel):
    steps: list[str] = Field(default=["ingest", "transform", "export"])
    force: bool = False


# --- Helpers ---


def _send_webhook_notification(
    url: str, stream_name: str, status: str, duration_s: float
) -> None:
    """Send a POST webhook notification for stream completion."""
    from datetime import datetime
    from urllib.request import Request, urlopen

    payload = json.dumps(
        {
            "stream": stream_name,
            "status": status,
            "duration_seconds": duration_s,
            "timestamp": datetime.now().isoformat(),
        }
    ).encode()

    try:
        req = Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        urlopen(req, timeout=10)
        logger.info("Webhook sent to %s for stream %s", url, stream_name)
    except Exception as e:
        logger.warning("Webhook failed for stream %s: %s", stream_name, e)


# --- Script execution ---


@router.post("/api/run")
def run_script_endpoint(
    request: Request, req: RunScriptRequest, conn: DbConn
) -> dict:
    """Run an ingest or export script."""
    _require_permission(request, "execute")
    from havn.engine.runner import run_script

    logger.info("Script run requested: %s", req.script_path)
    project_dir = _get_project_dir()
    script_path = (project_dir / req.script_path).resolve()
    # Path traversal protection
    if not script_path.is_relative_to(project_dir.resolve()):
        raise HTTPException(400, "Invalid script path")
    if not script_path.exists():
        raise HTTPException(404, f"Script not found: {req.script_path}")
    script_type = "ingest" if "ingest" in req.script_path else "export"
    result = run_script(conn, script_path, script_type, force=req.force)
    from havn.engine.secrets import mask_output

    if result.get("log_output"):
        result["log_output"] = mask_output(result["log_output"], _get_project_dir())
    return result


# --- Pipeline state (module-level, independent of SSE connections) ---

import threading

_cancel_flag = threading.Event()

_pipeline_state = {
    "running": False,
    "operation": None,        # "stream", "transform", "lint", "script", "contracts"
    "operation_label": None,  # Human-readable label
    "stream_name": None,      # backward compat
    "started_at": None,
    "events": [],       # list of {"event": str, "data": dict}
    "finished": False,   # True when pipeline completes (events still available)
}
_pipeline_lock = threading.Lock()
_pipeline_cond = threading.Condition(_pipeline_lock)

_STALE_TIMEOUT = 600  # 10 minutes


def _start_operation(operation: str, label: str, target_fn, args: tuple) -> dict:
    """Start a background operation. Returns status dict.

    Atomic check-and-set under a single lock acquisition: returns
    'already_running' if a pipeline is in progress, otherwise reserves
    the slot and starts the worker thread.
    """
    with _pipeline_cond:
        if _pipeline_state["running"]:
            return {
                "status": "already_running",
                "operation": _pipeline_state["operation"],
                "operation_label": _pipeline_state["operation_label"],
            }
        _cancel_flag.clear()
        _pipeline_state["running"] = True
        _pipeline_state["operation"] = operation
        _pipeline_state["operation_label"] = label
        _pipeline_state["stream_name"] = label
        _pipeline_state["started_at"] = time.time()
        _pipeline_state["events"] = []
        _pipeline_state["finished"] = False
        _pipeline_cond.notify_all()

    t = threading.Thread(target=target_fn, args=args, daemon=True)
    t.start()
    return {"status": "started", "operation": operation, "operation_label": label}


def _emit(event_type: str, data: dict):
    """Append an event to the pipeline event buffer and wake SSE listeners."""
    data["ts"] = time.time()
    with _pipeline_cond:
        _pipeline_state["events"].append({"event": event_type, "data": data})
        _pipeline_cond.notify_all()


def _finish_operation():
    """Mark the current operation as finished."""
    try:
        from havn.server.deps import _get_shared_conn
        _get_shared_conn().execute("FORCE CHECKPOINT")
    except Exception:
        pass
    with _pipeline_cond:
        _pipeline_state["running"] = False
        _pipeline_state["finished"] = True
        _pipeline_cond.notify_all()


# --- Background thread: Lint ---


def _run_lint_thread(fix, project_dir, config):
    """Run SQLFluff lint in background thread."""
    import time as _time

    start = _time.perf_counter()
    _emit("start", {"operation": "lint", "label": f"Lint{' --fix' if fix else ''}"})
    try:
        from havn.lint.linter import lint

        count, violations, fixed = lint(
            project_dir / "transform",
            fix=fix,
            dialect=config.lint.dialect,
            rules=config.lint.rules or None,
        )

        for v in violations:
            _emit("lint_violation", {
                "file": v["file"], "line": v["line"], "col": v["col"],
                "code": v["code"], "description": v["description"],
                "fixable": v.get("fixable", False),
            })

        duration_s = round(_time.perf_counter() - start, 1)
        _emit("complete", {
            "operation": "lint", "status": "success",
            "duration_seconds": duration_s,
            "count": count, "fixed": fixed, "fix": fix,
        })
    except Exception as e:
        duration_s = round(_time.perf_counter() - start, 1)
        _emit("complete", {
            "operation": "lint", "status": "failed",
            "duration_seconds": duration_s, "error": str(e),
        })
    finally:
        _finish_operation()


# --- Background thread: Contracts ---


def _run_contracts_thread(project_dir):
    """Run data contracts in background thread."""
    import time as _time
    from havn.server.deps import _get_shared_conn

    start = _time.perf_counter()
    _emit("start", {"operation": "contracts", "label": "Contracts"})
    try:
        from havn.engine.contracts import run_contracts

        conn = _get_shared_conn()
        contracts_dir = project_dir / "contracts"
        results = run_contracts(conn, contracts_dir)

        for r in results:
            _emit("contract_result", {
                "contract_name": r.contract_name,
                "model": r.model,
                "passed": r.passed,
                "severity": r.severity,
                "duration_ms": r.duration_ms,
                "error": r.error,
                "assertions": r.results,
                "consecutive_failures": r.consecutive_failures,
            })

        duration_s = round(_time.perf_counter() - start, 1)
        _emit("complete", {
            "operation": "contracts", "status": "success",
            "duration_seconds": duration_s,
            "total": len(results),
            "passed": sum(1 for r in results if r.passed),
            "failed": sum(1 for r in results if not r.passed),
        })
    except Exception as e:
        duration_s = round(_time.perf_counter() - start, 1)
        _emit("complete", {
            "operation": "contracts", "status": "failed",
            "duration_seconds": duration_s, "error": str(e),
        })
    finally:
        _finish_operation()


# --- Background thread: Script ---


def _run_script_thread(script_path_str, project_dir, force=False):
    """Run a single script in background thread."""
    import time as _time
    from havn.server.deps import _get_shared_conn

    start = _time.perf_counter()
    _emit("start", {"operation": "script", "label": script_path_str})
    try:
        from havn.engine.runner import run_script

        conn = _get_shared_conn()
        script_path = project_dir / script_path_str
        script_type = "ingest" if "ingest" in script_path_str else "export"
        result = run_script(conn, script_path, script_type, force=force)

        from havn.engine.secrets import mask_output
        log_output = result.get("log_output", "")
        if log_output:
            log_output = mask_output(log_output, project_dir)
            for line in log_output.split("\n"):
                if line.strip():
                    _emit("script_output", {"line": line})

        duration_s = round(_time.perf_counter() - start, 1)
        _emit("complete", {
            "operation": "script", "status": result.get("status", "success"),
            "duration_seconds": duration_s,
            "duration_ms": result.get("duration_ms", 0),
            "rows_affected": result.get("rows_affected", 0),
            "error": result.get("error"),
            "script_path": script_path_str,
        })
    except Exception as e:
        duration_s = round(_time.perf_counter() - start, 1)
        _emit("complete", {
            "operation": "script", "status": "error",
            "duration_seconds": duration_s, "error": str(e),
            "script_path": script_path_str,
        })
    finally:
        _finish_operation()


# --- Background thread: Transform ---


def _run_transform_thread(targets, force, project_dir):
    """Run SQL transforms in background thread."""
    import time as _time
    import uuid as _uuid
    from havn.server.deps import _get_shared_conn

    pipeline_run_id = str(_uuid.uuid4())

    label = "Transform"
    if targets:
        label += f" ({', '.join(targets)})"
    if force:
        label += " --force"

    start = _time.perf_counter()
    _emit("start", {"operation": "transform", "label": label, "pipeline_run_id": pipeline_run_id})
    try:
        conn = _get_shared_conn()
        results = run_transform(
            conn,
            project_dir / "transform",
            targets=targets,
            force=force,
            pipeline_run_id=pipeline_run_id,
        )

        for model, status in results.items():
            _emit("model_end", {
                "name": model,
                "action": "transform",
                "status": status,
                "duration_ms": 0,
                "row_count": 0,
                "num": 0,
            })

        duration_s = round(_time.perf_counter() - start, 1)
        has_error = any(s == "error" for s in results.values())
        _emit("complete", {
            "operation": "transform",
            "status": "failed" if has_error else "success",
            "duration_seconds": duration_s,
            "pipeline_run_id": pipeline_run_id,
        })
    except Exception as e:
        duration_s = round(_time.perf_counter() - start, 1)
        _emit("complete", {
            "operation": "transform", "status": "failed",
            "duration_seconds": duration_s, "error": str(e),
            "pipeline_run_id": pipeline_run_id,
        })
    finally:
        _finish_operation()


# --- Pipeline background thread ---


def _run_pipeline_thread(stream_name, stream_config, project_dir, db_path_str, force, user):
    """Run pipeline in background. Appends events to _pipeline_state["events"]."""
    global _pipeline_state

    import json as _json
    import time as _time
    import uuid as _uuid
    import queue as _queue
    from concurrent.futures import ThreadPoolExecutor
    from graphlib import TopologicalSorter

    from havn.engine.database import log_run as _lr
    from havn.engine.runner import run_script as _run_script
    from havn.server.deps import _get_db_resource_limits, _get_shared_conn
    from havn.engine.transform import discover_models as _dm, build_dag as _bd, validate_models as _vm
    from havn.engine.transform.discovery import _compute_upstream_hash as _cuh, _has_changed as _hc, _update_state as _us
    from havn.engine.transform.execution import execute_model as _em
    from havn.engine.transform.quality import run_assertions as _ra, _save_assertions as _sa, profile_model as _pm, _save_profile as _sp

    # Generate a pipeline_run_id that groups all model executions in this run
    pipeline_run_id = str(_uuid.uuid4())

    def emit(event_type: str, data: dict):
        _emit(event_type, data)

    start = _time.perf_counter()
    has_error = False
    cancelled = False
    completed_count = 0
    failed_count = 0
    skipped_count = 0

    def _is_cancelled():
        return _cancel_flag.is_set()

    try:
        # -- Build unified DAG --
        # 1. Discover ingest scripts
        ingest_dir = project_dir / "ingest"
        ingest_scripts = []
        if ingest_dir.exists():
            py = list(ingest_dir.glob("*.py"))
            nb = list(ingest_dir.glob("*.dpnb"))
            ingest_scripts = sorted([s for s in py + nb if not s.name.startswith("_")], key=lambda p: p.name)

        # 2. Discover transform models
        transform_dir = project_dir / "transform"
        models = _dm(transform_dir) if transform_dir.exists() else []
        ordered = _bd(models) if models else []
        model_map = {m.full_name: m for m in ordered}
        for model in ordered:
            model.upstream_hash = _cuh(model, model_map)

        # 3. Discover export scripts
        export_dir = project_dir / "export"
        export_scripts = []
        if export_dir.exists():
            py = list(export_dir.glob("*.py"))
            nb = list(export_dir.glob("*.dpnb"))
            export_scripts = sorted([s for s in py + nb if not s.name.startswith("_")], key=lambda p: p.name)

        # 4. Build node registry and DAG
        nodes = {}
        dag_deps = {}

        # Ingest nodes have no dependencies (they're roots)
        ingest_node_ids = set()
        for script in ingest_scripts:
            nid = f"ingest:{script.name}"
            nodes[nid] = {"type": "ingest", "path": script, "name": script.name}
            dag_deps[nid] = set()
            ingest_node_ids.add(nid)

        # Build a mapping from landing table names to their likely ingest script.
        _landing_to_ingest = {}
        for ingest_nid in ingest_node_ids:
            stem = nodes[ingest_nid]["path"].stem
            _landing_to_ingest[f"landing.{stem}"] = ingest_nid

        # Transform nodes
        for model in ordered:
            nid = f"transform:{model.full_name}"
            nodes[nid] = {"type": "transform", "model": model, "name": model.full_name}
            deps = set()
            for dep in model.depends_on:
                dep_nid = f"transform:{dep}"
                if dep_nid in nodes:
                    deps.add(dep_nid)
                elif dep.startswith("landing."):
                    mapped = _landing_to_ingest.get(dep)
                    if mapped:
                        deps.add(mapped)
                    elif ingest_node_ids:
                        deps.update(ingest_node_ids)
            dag_deps[nid] = deps

        # Export nodes depend on all transforms
        transform_node_ids = {f"transform:{m.full_name}" for m in ordered}
        for script in export_scripts:
            nid = f"export:{script.name}"
            nodes[nid] = {"type": "export", "path": script, "name": script.name}
            dag_deps[nid] = transform_node_ids.copy()

        total_items = len(nodes)

        # Record job_run so this stream appears in Job Results
        try:
            from havn.engine.orchestration import ensure_job_runs_table
            _jr_conn = _get_shared_conn()
            _jr_cur = cursor_for(_jr_conn)
            try:
                ensure_job_runs_table(_jr_cur)
                _jr_cur.execute(
                    "INSERT INTO _havn.job_runs (id, job_name, job_file, target, status, steps_total, trigger) "
                    "VALUES (?, ?, ?, ?, 'running', ?, ?)",
                    [pipeline_run_id, f"stream ({stream_name})", "stream", stream_name, total_items, "pipeline"],
                )
            finally:
                _jr_cur.close()
        except Exception:
            logger.debug("Failed to insert job_run for stream pipeline", exc_info=True)

        # Numbering assigned at runtime as nodes START
        _node_number = {}
        _next_num = [1]

        emit("start", {"operation": "stream", "label": f"Pipeline {stream_name}", "stream": stream_name, "steps": 1, "total": total_items, "pipeline_run_id": pipeline_run_id})

        if not nodes:
            emit("complete", {"stream": stream_name, "status": "success", "duration_seconds": 0, "pipeline_run_id": pipeline_run_id})
            return

        # Use the server's shared connection
        conn = _get_shared_conn()

        # Ensure metadata tables exist
        from havn.engine.database import ensure_meta_table as _emt
        _emt(conn)

        # 5. Pre-build validation for transform models
        # Skip validation when stream includes ingest steps — landing tables
        # won't exist yet on a fresh database and will be created by ingest.
        has_ingest = bool(ingest_node_ids)
        if models and not has_ingest:
            val_cur = cursor_for(conn)
            try:
                _val_errors = _vm(val_cur, models)
            finally:
                val_cur.close()
            for _ve in _val_errors:
                emit("validation", {"model": _ve.model, "severity": _ve.severity, "message": _ve.message})
                if _ve.severity == "error":
                    has_error = True
            if has_error:
                emit("complete", {"stream": stream_name, "status": "failed", "duration_seconds": 0, "pipeline_run_id": pipeline_run_id})
                return

        # 6. Pre-create schemas
        schema_cur = cursor_for(conn)
        for schema in ("landing", "bronze", "silver", "gold"):
            schema_cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        schema_cur.close()

        result_q = _queue.Queue()
        sorter = TopologicalSorter(dag_deps)
        sorter.prepare()
        _, _threads_cfg = _get_db_resource_limits()
        # DuckLake's catalog cannot tolerate concurrent in-process writes —
        # multiple threads each calling `conn.cursor().execute("CREATE TABLE ...")`
        # against the same attached catalog corrupt its metadata. Force
        # sequential execution on DuckLake; transforms still get full memory
        # and threads via per-query SET memory_limit / SET threads.
        from havn.server.deps import _get_backend
        if _get_backend().name == "ducklake":
            max_workers = 1
        else:
            max_workers = _threads_cfg or 4
        executor = ThreadPoolExecutor(max_workers=max_workers)
        active = 0

        def _exec_node(node_id):
            """Execute a single node on a thread-local cursor."""
            local = cursor_for(conn)
            info = nodes[node_id]
            start_data = {"name": info["name"], "action": info["type"], "num": _node_number.get(node_id, 0)}
            if info["type"] == "transform":
                start_data["materialized"] = info["model"].materialized
            result_q.put(("__start__", start_data))
            try:
                if info["type"] == "ingest":
                    result = _run_script(local, info["path"], "ingest", pipeline_run_id=pipeline_run_id, force=force)
                    result_q.put((node_id, {
                        "status": result["status"],
                        "duration_ms": result.get("duration_ms", 0),
                        "rows_affected": result.get("rows_affected", 0),
                        "error": result.get("error"),
                    }))
                elif info["type"] == "transform":
                    m = info["model"]
                    if not force and not _hc(local, m):
                        # Log the skip too, so `havn history` and external
                        # readers see "12 steps, 12 skipped" instead of an
                        # empty run_log for a no-op pipeline.
                        try:
                            _lr(local, "transform", m.full_name, "skipped", 0, 0, pipeline_run_id=pipeline_run_id)
                        except Exception:
                            pass
                        result_q.put((node_id, {"status": "skipped"}))
                        return
                    duration_ms, row_count = _em(local, m)
                    _us(local, m, duration_ms, row_count)
                    _lr(local, "transform", m.full_name, "success", duration_ms, row_count, pipeline_run_id=pipeline_run_id)
                    status = "built"
                    if m.assertions:
                        ar = _ra(local, m)
                        _sa(local, m, ar)
                        if any(not a.passed for a in ar):
                            status = "assertion_failed"
                    if m.materialized in ("table", "incremental"):
                        prof = _pm(local, m)
                        _sp(local, m, prof)
                    result_q.put((node_id, {
                        "status": status, "duration_ms": duration_ms,
                        "row_count": row_count,
                    }))
                elif info["type"] == "export":
                    result = _run_script(local, info["path"], "export", pipeline_run_id=pipeline_run_id, force=force)
                    result_q.put((node_id, {
                        "status": result["status"],
                        "duration_ms": result.get("duration_ms", 0),
                        "error": result.get("error"),
                    }))
            except Exception as e:
                logger.error("Node %s failed: %s", node_id, e, exc_info=True)
                # Persist transform failures to _havn.run_log so they survive
                # the OUTPUT panel scrolling away. Without this row, a query
                # like `WHERE pipeline_run_id = ?` returns only the success
                # rows and the failure looks like it never happened.
                try:
                    if info["type"] == "transform":
                        _lr(local, "transform", info["model"].full_name, "error",
                            error=str(e), pipeline_run_id=pipeline_run_id)
                except Exception:
                    pass
                result_q.put((node_id, {"status": "error", "error": str(e)}))
            finally:
                local.close()

        # Pending queue: nodes that are ready but not yet submitted
        pending = []

        def _submit_batch():
            """Submit pending nodes up to max_workers active limit."""
            nonlocal active
            while pending and active < max_workers:
                nid = pending.pop(0)
                if nid not in _node_number:
                    _node_number[nid] = _next_num[0]
                    _next_num[0] += 1
                try:
                    executor.submit(_exec_node, nid)
                except RuntimeError:
                    # Interpreter shutting down — stop submitting
                    return
                active += 1

        # Kick off initial ready nodes (roots with no deps)
        pending.extend(sorter.get_ready())
        _submit_batch()

        # Process results as they arrive, submit newly ready nodes
        while sorter.is_active() or active > 0:
            if _is_cancelled():
                cancelled = True
                executor.shutdown(wait=False, cancel_futures=True)
                break

            try:
                node_id, result = result_q.get(timeout=2)
            except _queue.Empty:
                continue

            # Handle start events
            if node_id == "__start__":
                emit("model_start", result)
                continue

            active -= 1
            info = nodes[node_id]
            status = result.get("status", "error")

            # Assign number if not already assigned (skipped nodes skip __start__)
            if node_id not in _node_number:
                _node_number[node_id] = _next_num[0]
                _next_num[0] += 1

            # Emit result
            emit("model_end", {
                "name": info["name"],
                "action": info["type"],
                "status": status,
                "duration_ms": result.get("duration_ms", 0),
                "row_count": result.get("row_count", 0),
                "rows_affected": result.get("rows_affected", 0),
                "error": result.get("error"),
                "materialized": info["model"].materialized if info["type"] == "transform" else None,
                "num": _node_number[node_id],
            })

            if status in ("error", "assertion_failed"):
                has_error = True
                failed_count += 1
            elif status == "skipped":
                skipped_count += 1
            else:
                completed_count += 1

            # Mark node as done -- this may release dependent nodes
            sorter.done(node_id)

            # Add newly ready nodes to pending and submit up to max_workers
            try:
                pending.extend(sorter.get_ready())
            except Exception:
                pass  # sorter exhausted
            _submit_batch()

        # Shut down executor (don't close conn -- it's the shared singleton)
        try:
            executor.shutdown(wait=False)
        except Exception:
            pass

        duration_s = round(_time.perf_counter() - start, 1)
        if cancelled:
            status = "cancelled"
        elif has_error:
            status = "failed"
        else:
            status = "success"

        # Audit
        try:
            from havn.engine.audit import log_audit
            audit_cur = cursor_for(conn)
            log_audit(audit_cur, user=user.get("username", "anonymous"),
                      action="transform", resource=stream_name,
                      detail=f"stream completed: {status} in {duration_s}s")
            audit_cur.close()
        except Exception:
            pass

        emit("complete", {
            "stream": stream_name,
            "status": status,
            "duration_seconds": duration_s,
            "pipeline_run_id": pipeline_run_id,
        })

    except Exception as e:
        logger.error("Pipeline thread error: %s", e, exc_info=True)
        has_error = True
        duration_s = round(_time.perf_counter() - start, 1)
        emit("complete", {
            "stream": stream_name,
            "status": "failed",
            "duration_seconds": duration_s,
            "pipeline_run_id": pipeline_run_id,
        })
    finally:
        # Update job_run with final status
        try:
            from havn.engine.orchestration import ensure_job_runs_table
            _fin_conn = _get_shared_conn()
            _fin_cur = cursor_for(_fin_conn)
            try:
                ensure_job_runs_table(_fin_cur)
                status_val = "failure" if has_error else ("cancelled" if cancelled else "success")
                duration_ms = int((_time.perf_counter() - start) * 1000)
                _fin_cur.execute(
                    "UPDATE _havn.job_runs SET status = ?, finished_at = current_timestamp, "
                    "duration_ms = ?, steps_completed = ?, steps_failed = ?, steps_skipped = ? "
                    "WHERE id = ?",
                    [status_val, duration_ms, completed_count, failed_count, skipped_count, pipeline_run_id],
                )
            finally:
                _fin_cur.close()
        except Exception:
            logger.debug("Failed to update job_run for stream pipeline", exc_info=True)
        _finish_operation()


# --- Background thread: Selective pipeline ---


def _run_selective_pipeline_thread(steps, force, project_dir, user):
    """Run selective pipeline steps in background. Appends events to _pipeline_state["events"]."""
    global _pipeline_state

    import time as _time
    import uuid as _uuid
    import queue as _queue
    from concurrent.futures import ThreadPoolExecutor
    from graphlib import TopologicalSorter

    from havn.engine.database import log_run as _lr
    from havn.engine.runner import run_script as _run_script
    from havn.server.deps import _get_db_resource_limits, _get_shared_conn
    from havn.engine.transform import discover_models as _dm, build_dag as _bd, validate_models as _vm
    from havn.engine.transform.discovery import _compute_upstream_hash as _cuh, _has_changed as _hc, _update_state as _us
    from havn.engine.transform.execution import execute_model as _em
    from havn.engine.transform.quality import run_assertions as _ra, _save_assertions as _sa, profile_model as _pm, _save_profile as _sp

    pipeline_run_id = str(_uuid.uuid4())

    def emit(event_type: str, data: dict):
        _emit(event_type, data)

    step_label = "+".join(steps)
    if force:
        step_label += " --force"

    start = _time.perf_counter()
    has_error = False
    cancelled = False
    completed_count = 0
    failed_count = 0
    skipped_count = 0

    def _is_cancelled():
        return _cancel_flag.is_set()

    try:
        # -- Discover scripts and models based on requested steps --
        ingest_scripts = []
        if "ingest" in steps:
            ingest_dir = project_dir / "ingest"
            if ingest_dir.exists():
                py = list(ingest_dir.glob("*.py"))
                nb = list(ingest_dir.glob("*.dpnb"))
                ingest_scripts = sorted([s for s in py + nb if not s.name.startswith("_")], key=lambda p: p.name)

        models = []
        ordered = []
        model_map = {}
        if "transform" in steps:
            transform_dir = project_dir / "transform"
            models = _dm(transform_dir) if transform_dir.exists() else []
            ordered = _bd(models) if models else []
            model_map = {m.full_name: m for m in ordered}
            for model in ordered:
                model.upstream_hash = _cuh(model, model_map)

        export_scripts = []
        if "export" in steps:
            export_dir = project_dir / "export"
            if export_dir.exists():
                py = list(export_dir.glob("*.py"))
                nb = list(export_dir.glob("*.dpnb"))
                export_scripts = sorted([s for s in py + nb if not s.name.startswith("_")], key=lambda p: p.name)

        # -- Build node registry and DAG --
        nodes = {}
        dag_deps = {}

        ingest_node_ids = set()
        for script in ingest_scripts:
            nid = f"ingest:{script.name}"
            nodes[nid] = {"type": "ingest", "path": script, "name": script.name}
            dag_deps[nid] = set()
            ingest_node_ids.add(nid)

        _landing_to_ingest = {}
        for ingest_nid in ingest_node_ids:
            stem = nodes[ingest_nid]["path"].stem
            _landing_to_ingest[f"landing.{stem}"] = ingest_nid

        for model in ordered:
            nid = f"transform:{model.full_name}"
            nodes[nid] = {"type": "transform", "model": model, "name": model.full_name}
            deps = set()
            for dep in model.depends_on:
                dep_nid = f"transform:{dep}"
                if dep_nid in nodes:
                    deps.add(dep_nid)
                elif dep.startswith("landing."):
                    mapped = _landing_to_ingest.get(dep)
                    if mapped:
                        deps.add(mapped)
                    elif ingest_node_ids:
                        deps.update(ingest_node_ids)
            dag_deps[nid] = deps

        transform_node_ids = {f"transform:{m.full_name}" for m in ordered}
        for script in export_scripts:
            nid = f"export:{script.name}"
            nodes[nid] = {"type": "export", "path": script, "name": script.name}
            dag_deps[nid] = transform_node_ids.copy()

        total_items = len(nodes)

        # Record job_run so this pipeline appears in Job Results
        try:
            from havn.engine.orchestration import ensure_job_runs_table
            _jr_conn = _get_shared_conn()
            _jr_cur = cursor_for(_jr_conn)
            try:
                ensure_job_runs_table(_jr_cur)
                _jr_cur.execute(
                    "INSERT INTO _havn.job_runs (id, job_name, job_file, target, status, steps_total, trigger) "
                    "VALUES (?, ?, ?, ?, 'running', ?, ?)",
                    [pipeline_run_id, f"pipeline ({step_label})", "pipeline", step_label, total_items, "pipeline"],
                )
            finally:
                _jr_cur.close()
        except Exception:
            logger.debug("Failed to insert job_run for selective pipeline", exc_info=True)

        _node_number = {}
        _next_num = [1]

        emit("start", {
            "operation": "pipeline",
            "label": f"Pipeline ({step_label})",
            "stream": "pipeline",
            "steps": len(steps),
            "total": total_items,
            "pipeline_run_id": pipeline_run_id,
        })

        if not nodes:
            emit("complete", {"stream": "pipeline", "status": "success", "duration_seconds": 0, "pipeline_run_id": pipeline_run_id})
            return

        conn = _get_shared_conn()

        from havn.engine.database import ensure_meta_table as _emt
        _emt(conn)

        # Pre-build validation for transform models (skip if ingest is included)
        has_ingest = bool(ingest_node_ids)
        if models and not has_ingest:
            val_cur = cursor_for(conn)
            try:
                _val_errors = _vm(val_cur, models)
            finally:
                val_cur.close()
            for _ve in _val_errors:
                emit("validation", {"model": _ve.model, "severity": _ve.severity, "message": _ve.message})
                if _ve.severity == "error":
                    has_error = True
            if has_error:
                emit("complete", {"stream": "pipeline", "status": "failed", "duration_seconds": 0, "pipeline_run_id": pipeline_run_id})
                return

        # Pre-create schemas
        schema_cur = cursor_for(conn)
        for schema in ("landing", "bronze", "silver", "gold"):
            schema_cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        schema_cur.close()

        result_q = _queue.Queue()
        sorter = TopologicalSorter(dag_deps)
        sorter.prepare()
        _, _threads_cfg = _get_db_resource_limits()
        # DuckLake's catalog cannot tolerate concurrent in-process writes —
        # multiple threads each calling `conn.cursor().execute("CREATE TABLE ...")`
        # against the same attached catalog corrupt its metadata. Force
        # sequential execution on DuckLake; transforms still get full memory
        # and threads via per-query SET memory_limit / SET threads.
        from havn.server.deps import _get_backend
        if _get_backend().name == "ducklake":
            max_workers = 1
        else:
            max_workers = _threads_cfg or 4
        executor = ThreadPoolExecutor(max_workers=max_workers)
        active = 0

        def _exec_node(node_id):
            """Execute a single node on a thread-local cursor."""
            local = cursor_for(conn)
            info = nodes[node_id]
            start_data = {"name": info["name"], "action": info["type"], "num": _node_number.get(node_id, 0)}
            if info["type"] == "transform":
                start_data["materialized"] = info["model"].materialized
            result_q.put(("__start__", start_data))
            try:
                if info["type"] == "ingest":
                    result = _run_script(local, info["path"], "ingest", pipeline_run_id=pipeline_run_id, force=force)
                    result_q.put((node_id, {
                        "status": result["status"],
                        "duration_ms": result.get("duration_ms", 0),
                        "rows_affected": result.get("rows_affected", 0),
                        "error": result.get("error"),
                    }))
                elif info["type"] == "transform":
                    m = info["model"]
                    if not force and not _hc(local, m):
                        # Log the skip too, so `havn history` and external
                        # readers see "12 steps, 12 skipped" instead of an
                        # empty run_log for a no-op pipeline.
                        try:
                            _lr(local, "transform", m.full_name, "skipped", 0, 0, pipeline_run_id=pipeline_run_id)
                        except Exception:
                            pass
                        result_q.put((node_id, {"status": "skipped"}))
                        return
                    duration_ms, row_count = _em(local, m)
                    _us(local, m, duration_ms, row_count)
                    _lr(local, "transform", m.full_name, "success", duration_ms, row_count, pipeline_run_id=pipeline_run_id)
                    status = "built"
                    if m.assertions:
                        ar = _ra(local, m)
                        _sa(local, m, ar)
                        if any(not a.passed for a in ar):
                            status = "assertion_failed"
                    if m.materialized in ("table", "incremental"):
                        prof = _pm(local, m)
                        _sp(local, m, prof)
                    result_q.put((node_id, {
                        "status": status, "duration_ms": duration_ms,
                        "row_count": row_count,
                    }))
                elif info["type"] == "export":
                    result = _run_script(local, info["path"], "export", pipeline_run_id=pipeline_run_id, force=force)
                    result_q.put((node_id, {
                        "status": result["status"],
                        "duration_ms": result.get("duration_ms", 0),
                        "error": result.get("error"),
                    }))
            except Exception as e:
                logger.error("Node %s failed: %s", node_id, e, exc_info=True)
                try:
                    if info["type"] == "transform":
                        _lr(local, "transform", info["model"].full_name, "error",
                            error=str(e), pipeline_run_id=pipeline_run_id)
                except Exception:
                    pass
                result_q.put((node_id, {"status": "error", "error": str(e)}))
            finally:
                local.close()

        pending = []

        def _submit_batch():
            """Submit pending nodes up to max_workers active limit."""
            nonlocal active
            while pending and active < max_workers:
                nid = pending.pop(0)
                if nid not in _node_number:
                    _node_number[nid] = _next_num[0]
                    _next_num[0] += 1
                try:
                    executor.submit(_exec_node, nid)
                except RuntimeError:
                    return
                active += 1

        pending.extend(sorter.get_ready())
        _submit_batch()

        while sorter.is_active() or active > 0:
            if _is_cancelled():
                cancelled = True
                executor.shutdown(wait=False, cancel_futures=True)
                break

            try:
                node_id, result = result_q.get(timeout=2)
            except _queue.Empty:
                continue

            if node_id == "__start__":
                emit("model_start", result)
                continue

            active -= 1
            info = nodes[node_id]
            status = result.get("status", "error")

            if node_id not in _node_number:
                _node_number[node_id] = _next_num[0]
                _next_num[0] += 1

            emit("model_end", {
                "name": info["name"],
                "action": info["type"],
                "status": status,
                "duration_ms": result.get("duration_ms", 0),
                "row_count": result.get("row_count", 0),
                "rows_affected": result.get("rows_affected", 0),
                "error": result.get("error"),
                "materialized": info["model"].materialized if info["type"] == "transform" else None,
                "num": _node_number[node_id],
            })

            if status in ("error", "assertion_failed"):
                has_error = True
                failed_count += 1
            elif status == "skipped":
                skipped_count += 1
            else:
                completed_count += 1

            sorter.done(node_id)

            try:
                pending.extend(sorter.get_ready())
            except Exception:
                pass
            _submit_batch()

        try:
            executor.shutdown(wait=False)
        except Exception:
            pass

        duration_s = round(_time.perf_counter() - start, 1)
        if cancelled:
            status = "cancelled"
        elif has_error:
            status = "failed"
        else:
            status = "success"

        # Audit
        try:
            from havn.engine.audit import log_audit
            audit_cur = cursor_for(conn)
            log_audit(audit_cur, user=user.get("username", "anonymous"),
                      action="transform", resource=f"pipeline({step_label})",
                      detail=f"selective pipeline completed: {status} in {duration_s}s")
            audit_cur.close()
        except Exception:
            pass

        emit("complete", {
            "stream": "pipeline",
            "status": status,
            "duration_seconds": duration_s,
            "pipeline_run_id": pipeline_run_id,
        })

    except Exception as e:
        logger.error("Selective pipeline thread error: %s", e, exc_info=True)
        has_error = True
        duration_s = round(_time.perf_counter() - start, 1)
        emit("complete", {
            "stream": "pipeline",
            "status": "failed",
            "duration_seconds": duration_s,
            "pipeline_run_id": pipeline_run_id,
        })
    finally:
        # Update job_run with final status
        try:
            from havn.engine.orchestration import ensure_job_runs_table
            _fin_conn = _get_shared_conn()
            _fin_cur = cursor_for(_fin_conn)
            try:
                ensure_job_runs_table(_fin_cur)
                status_val = "failure" if has_error else ("cancelled" if cancelled else "success")
                duration_ms = int((_time.perf_counter() - start) * 1000)
                _fin_cur.execute(
                    "UPDATE _havn.job_runs SET status = ?, finished_at = current_timestamp, "
                    "duration_ms = ?, steps_completed = ?, steps_failed = ?, steps_skipped = ? "
                    "WHERE id = ?",
                    [status_val, duration_ms, completed_count, failed_count, skipped_count, pipeline_run_id],
                )
            finally:
                _fin_cur.close()
        except Exception:
            logger.debug("Failed to update job_run for selective pipeline", exc_info=True)
        _finish_operation()


# --- Pipeline start (selective steps) ---


_VALID_PIPELINE_STEPS = {"ingest", "transform", "export"}


@router.post("/api/pipeline/start")
def start_pipeline(request: Request, req: PipelineStartRequest) -> dict:
    """Start a pipeline with selective steps. Returns immediately."""
    user = _require_permission(request, "execute")

    # Validate steps
    invalid = set(req.steps) - _VALID_PIPELINE_STEPS
    if invalid:
        raise HTTPException(400, f"Invalid pipeline steps: {', '.join(sorted(invalid))}. Valid steps: ingest, transform, export")
    if not req.steps:
        raise HTTPException(400, "At least one step must be specified")

    step_label = "+".join(req.steps)
    if req.force:
        step_label += " --force"

    logger.info("Selective pipeline start requested: steps=%s force=%s", req.steps, req.force)

    # Audit pipeline start
    try:
        from havn.engine.audit import log_audit
        from havn.server.deps import _get_shared_conn

        shared = _get_shared_conn()
        audit_cur = cursor_for(shared)
        client_ip = request.client.host if request.client else None
        log_audit(
            audit_cur,
            user=user.get("username", "anonymous"),
            action="transform",
            resource=f"pipeline({step_label})",
            detail=f"selective pipeline started (steps={req.steps}, force={req.force})",
            ip_address=client_ip,
        )
        audit_cur.close()
    except Exception:
        logger.debug("Failed to write audit log for pipeline start", exc_info=True)

    project_dir = _get_project_dir()

    return _start_operation(
        "pipeline",
        f"Pipeline ({step_label})",
        _run_selective_pipeline_thread,
        (req.steps, req.force, project_dir, user),
    )


# --- Stream start ---


@router.post("/api/stream/{stream_name}/start")
def start_stream(request: Request, stream_name: str, force: bool = False) -> dict:
    """Start a pipeline in a background thread. Returns immediately."""
    user = _require_permission(request, "execute")

    logger.info("Stream start requested: %s (force=%s)", stream_name, force)

    config = _get_config()
    if stream_name not in config.streams:
        raise HTTPException(404, f"Stream '{stream_name}' not found")
    stream_config = config.streams[stream_name]

    from havn.server.deps import _get_db_path
    db_path_str = str(_get_db_path())
    project_dir = _get_project_dir()

    try:
        from havn.engine.audit import log_audit
        from havn.server.deps import _get_shared_conn

        shared = _get_shared_conn()
        audit_cur = cursor_for(shared)
        client_ip = request.client.host if request.client else None
        try:
            log_audit(
                audit_cur,
                user=user.get("username", "anonymous"),
                action="transform",
                resource=stream_name,
                detail=f"stream started (force={force})",
                ip_address=client_ip,
            )
        finally:
            audit_cur.close()
    except Exception:
        logger.debug("Failed to write audit log for stream start", exc_info=True)

    result = _start_operation(
        "stream",
        stream_name,
        _run_pipeline_thread,
        (stream_name, stream_config, project_dir, db_path_str, force, user),
    )
    if result.get("status") == "already_running":
        return {"status": "already_running", "stream_name": _pipeline_state["stream_name"]}
    return {"status": "started", "operation": "stream", "stream_name": stream_name}


# --- Start endpoints for non-stream operations ---


class TransformStartRequest(BaseModel):
    targets: list[str] | None = None
    force: bool = False


@router.post("/api/lint/start")
def start_lint(request: Request, fix: bool = False) -> dict:
    """Start lint in background thread. Returns immediately."""
    _require_permission(request, "execute")
    config = _get_config()
    project_dir = _get_project_dir()
    return _start_operation("lint", f"Lint{' --fix' if fix else ''}", _run_lint_thread, (fix, project_dir, config))


@router.post("/api/contracts/run/start")
def start_contracts(request: Request) -> dict:
    """Start contracts in background thread. Returns immediately."""
    _require_permission(request, "execute")
    project_dir = _get_project_dir()
    return _start_operation("contracts", "Contracts", _run_contracts_thread, (project_dir,))


@router.post("/api/run/start")
def start_script(request: Request, req: RunScriptRequest) -> dict:
    """Start a script in background thread. Returns immediately."""
    _require_permission(request, "execute")
    project_dir = _get_project_dir()
    script_path = (project_dir / req.script_path).resolve()
    # Path traversal protection
    if not script_path.is_relative_to(project_dir.resolve()):
        raise HTTPException(400, "Invalid script path")
    if not script_path.exists():
        raise HTTPException(404, f"Script not found: {req.script_path}")
    return _start_operation("script", req.script_path, _run_script_thread, (req.script_path, project_dir, req.force))


@router.post("/api/transform/start")
def start_transform(request: Request, req: TransformStartRequest) -> dict:
    """Start transform in background thread. Returns immediately."""
    _require_permission(request, "execute")
    project_dir = _get_project_dir()
    label = "Transform"
    if req.targets:
        label += f" ({', '.join(req.targets)})"
    return _start_operation("transform", label, _run_transform_thread, (req.targets, req.force, project_dir))


# --- Stream events (SSE) ---


@router.get("/api/stream/events")
async def stream_events_sse(request: Request, from_event: int = 0):
    """SSE endpoint that reads from the pipeline event buffer.

    Replays all events from index `from_event`, then follows live.
    Stops when pipeline is finished and all events have been sent.
    """
    from fastapi.responses import StreamingResponse

    _require_permission(request, "read")

    def _generate():
        import json as _json

        last_sent = from_event

        while True:
            with _pipeline_cond:
                # Block until there are new events or the pipeline finishes.
                while (
                    last_sent >= len(_pipeline_state["events"])
                    and _pipeline_state["running"]
                    and not _pipeline_state["finished"]
                ):
                    notified = _pipeline_cond.wait(timeout=15.0)
                    if not notified:
                        break
                events = list(_pipeline_state["events"])
                finished = _pipeline_state["finished"]
                running = _pipeline_state["running"]

            if last_sent < len(events):
                batch = events[last_sent:]
                last_sent = len(events)
                for evt in batch:
                    payload = _json.dumps(evt["data"])
                    yield f"event: {evt['event']}\ndata: {payload}\n\n"

            if finished and last_sent >= len(events):
                break

            if not running and not finished and len(events) == 0:
                break

            yield ": keepalive\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- Stream cancellation ---


@router.post("/api/stream/cancel")
def cancel_stream(request: Request) -> dict:
    """Cancel the currently running stream."""
    _require_permission(request, "execute")
    _cancel_flag.set()
    logger.info("Stream cancellation requested")
    return {"status": "cancelling"}


# --- Active stream status ---


@router.get("/api/stream/active")
def get_active_stream(request: Request) -> dict:
    """Check if a pipeline is currently running or has finished events."""
    _require_permission(request, "read")
    with _pipeline_lock:
        running = _pipeline_state["running"]
        operation = _pipeline_state["operation"]
        operation_label = _pipeline_state["operation_label"]
        stream_name = _pipeline_state["stream_name"]
        started_at = _pipeline_state["started_at"]
        total_events = len(_pipeline_state["events"])
        finished = _pipeline_state["finished"]
        # Extract status and duration from the last complete event if available
        last_status = None
        last_duration = None
        for evt in reversed(_pipeline_state["events"]):
            if evt["event"] == "complete":
                last_status = evt["data"].get("status")
                last_duration = evt["data"].get("duration_seconds")
                break

    # Check for stale pipeline
    if running and started_at:
        elapsed = time.time() - started_at
        if elapsed > _STALE_TIMEOUT:
            logger.warning("Clearing stale active stream (%.0fs old)", elapsed)
            with _pipeline_cond:
                _pipeline_state["running"] = False
                _pipeline_state["finished"] = True
                _pipeline_cond.notify_all()
            return {"running": False, "operation": operation, "operation_label": operation_label,
                    "stream_name": stream_name, "started_at": started_at,
                    "total_events": total_events, "finished": True,
                    "status": last_status, "duration_seconds": last_duration}

    return {
        "running": running,
        "operation": operation,
        "operation_label": operation_label,
        "stream_name": stream_name,
        "started_at": started_at,
        "total_events": total_events,
        "finished": finished,
        "status": last_status,
        "duration_seconds": last_duration,
    }


# --- Stream execution (SSE) --- LEGACY: kept for backward compat, now redirects to start+events ---


@router.get("/api/stream/{stream_name}/events")
async def run_stream_sse(
    request: Request, stream_name: str, force: bool = False
):
    """Run a stream with Server-Sent Events for real-time progress.

    Legacy endpoint: starts the pipeline via the new background thread mechanism,
    then streams events via the unified SSE endpoint logic.
    """
    from fastapi.responses import StreamingResponse
    from havn.server.deps import _get_db_path

    user = _require_permission(request, "execute")
    _cancel_flag.clear()
    logger.info("Stream SSE requested (legacy): %s (force=%s)", stream_name, force)

    # Audit pipeline start
    try:
        from havn.engine.audit import log_audit
        from havn.server.deps import _get_shared_conn

        shared = _get_shared_conn()
        audit_cur = cursor_for(shared)
        client_ip = request.client.host if request.client else None
        log_audit(
            audit_cur,
            user=user.get("username", "anonymous"),
            action="transform",
            resource=stream_name,
            detail=f"stream started (force={force})",
            ip_address=client_ip,
        )
        audit_cur.close()
    except Exception:
        logger.debug("Failed to write audit log for stream start", exc_info=True)

    config = _get_config()
    if stream_name not in config.streams:
        raise HTTPException(404, f"Stream '{stream_name}' not found")
    stream_config = config.streams[stream_name]

    db_path_str = str(_get_db_path())
    project_dir = _get_project_dir()

    _start_operation(
        "stream",
        stream_name,
        _run_pipeline_thread,
        (stream_name, stream_config, project_dir, db_path_str, force, user),
    )

    def _generate():
        import json as _json

        last_sent = 0

        while True:
            with _pipeline_cond:
                while (
                    last_sent >= len(_pipeline_state["events"])
                    and _pipeline_state["running"]
                    and not _pipeline_state["finished"]
                ):
                    notified = _pipeline_cond.wait(timeout=15.0)
                    if not notified:
                        break
                events = list(_pipeline_state["events"])
                finished = _pipeline_state["finished"]
                running = _pipeline_state["running"]

            if last_sent < len(events):
                batch = events[last_sent:]
                last_sent = len(events)
                for evt in batch:
                    payload = _json.dumps(evt["data"])
                    yield f"event: {evt['event']}\ndata: {payload}\n\n"

            if finished and last_sent >= len(events):
                break

            if not running and not finished and len(events) == 0:
                break

            yield ": keepalive\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- Stream execution (legacy) ---


@router.post("/api/stream/{stream_name}")
def run_stream_endpoint(
    request: Request, stream_name: str, conn: DbConn, force: bool = False
) -> dict:
    """Run a full stream with retry support."""
    user = _require_permission(request, "execute")
    logger.info("Stream run requested: %s (force=%s)", stream_name, force)

    # Audit pipeline start
    try:
        from havn.engine.audit import log_audit

        client_ip = request.client.host if request.client else None
        log_audit(
            conn,
            user=user.get("username", "anonymous"),
            action="transform",
            resource=stream_name,
            detail=f"stream started (force={force})",
            ip_address=client_ip,
        )
    except Exception:
        logger.debug("Failed to write audit log for stream start", exc_info=True)
    config = _get_config()
    if stream_name not in config.streams:
        raise HTTPException(404, f"Stream '{stream_name}' not found")
    stream_config = config.streams[stream_name]

    step_results = []
    has_error = False
    ingest_ran = False
    start = time.perf_counter()

    def _run_step(step):
        nonlocal ingest_ran
        from havn.engine.runner import run_scripts_in_dir

        if step.action == "ingest":
            results = run_scripts_in_dir(
                conn, _get_project_dir() / "ingest", "ingest", step.targets
            )
            if any(r["status"] == "success" for r in results):
                ingest_ran = True
            return {
                "action": "ingest",
                "results": results,
                "error": any(r["status"] == "error" for r in results),
            }
        elif step.action == "transform":
            # Force rebuild if ingest ran (upstream data changed)
            force_transform = force or ingest_ran
            results = run_transform(
                conn,
                _get_project_dir() / "transform",
                targets=step.targets if step.targets != ["all"] else None,
                force=force_transform,
            )
            return {
                "action": "transform",
                "results": results,
                "error": any(s == "error" for s in results.values()),
            }
        elif step.action == "export":
            results = run_scripts_in_dir(
                conn, _get_project_dir() / "export", "export", step.targets
            )
            return {
                "action": "export",
                "results": results,
                "error": any(r["status"] == "error" for r in results),
            }
        elif step.action == "seed":
            from havn.engine.seeds import run_seeds

            results = run_seeds(
                conn, _get_project_dir() / "seeds", force=force
            )
            return {
                "action": "seed",
                "results": results,
                "error": any(s == "error" for s in results.values()),
            }
        return {"action": step.action, "results": {}, "error": False}

    import time as _time

    for step in stream_config.steps:
        result = _run_step(step)
        if result["error"] and stream_config.retries > 0:
            for attempt in range(1, stream_config.retries + 1):
                logger.info(
                    "Retrying %s step (attempt %d/%d)",
                    step.action,
                    attempt,
                    stream_config.retries,
                )
                _time.sleep(stream_config.retry_delay)
                result = _run_step(step)
                if not result["error"]:
                    break
        step_results.append(
            {"action": result["action"], "results": result["results"]}
        )
        if result["error"]:
            has_error = True
            break

    duration_s = round(time.perf_counter() - start, 1)
    status = "failed" if has_error else "success"

    if stream_config.webhook_url:
        _send_webhook_notification(
            stream_config.webhook_url, stream_name, status, duration_s
        )

    return {
        "stream": stream_name,
        "steps": step_results,
        "status": status,
        "duration_seconds": duration_s,
    }


# --- Streams config ---


@router.get("/api/streams")
def list_streams(request: Request) -> dict:
    """List configured streams."""
    _require_permission(request, "read")
    config = _get_config()
    return {
        name: {
            "description": s.description,
            "schedule": s.schedule,
            "steps": [
                {"action": step.action, "targets": step.targets}
                for step in s.steps
            ],
        }
        for name, s in config.streams.items()
    }


# --- Run history ---


@router.get("/api/history")
def get_history(request: Request, conn: DbConn, limit: int = 50) -> list[dict]:
    """Get run history."""
    _require_permission(request, "read")
    ensure_meta_table(conn)
    rows = conn.execute(
        """
        SELECT run_id, run_type, target, status, started_at, duration_ms, rows_affected, error
        FROM _havn.run_log
        ORDER BY started_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    return [
        {
            "run_id": r[0],
            "run_type": r[1],
            "target": r[2],
            "status": r[3],
            "started_at": str(r[4]) if r[4] else None,
            "duration_ms": r[5],
            "rows_affected": r[6],
            "error": r[7],
        }
        for r in rows
    ]


# --- Pipeline run grouping ---


@router.get("/api/history/runs")
def get_pipeline_runs(request: Request, conn: DbConn, limit: int = 50) -> list[dict]:
    """Get pipeline runs grouped by pipeline_run_id."""
    _require_permission(request, "read")
    ensure_meta_table(conn)
    rows = conn.execute(
        """
        SELECT
            pipeline_run_id,
            MIN(run_type) AS run_type,
            MIN(target) AS first_target,
            MIN(started_at) AS started_at,
            MAX(CASE WHEN status IN ('error', 'failed') THEN status ELSE 'success' END) AS status,
            SUM(duration_ms) AS total_duration_ms,
            COUNT(*) AS model_count,
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
            SUM(CASE WHEN status IN ('error', 'failed') THEN 1 ELSE 0 END) AS error_count,
            SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped_count,
            SUM(rows_affected) AS total_rows
        FROM _havn.run_log
        WHERE pipeline_run_id IS NOT NULL
        GROUP BY pipeline_run_id
        ORDER BY MIN(started_at) DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()

    return [
        {
            "pipeline_run_id": r[0],
            "run_type": r[1],
            "target": r[2],
            "started_at": str(r[3]) if r[3] else None,
            "status": r[4],
            "total_duration_ms": r[5],
            "model_count": r[6],
            "success_count": r[7],
            "error_count": r[8],
            "skipped_count": r[9],
            "total_rows": r[10],
        }
        for r in rows
    ]


@router.get("/api/history/runs/{pipeline_run_id}")
def get_pipeline_run_detail(request: Request, pipeline_run_id: str, conn: DbConn) -> list[dict]:
    """Get individual model entries for a pipeline run."""
    _require_permission(request, "read")
    ensure_meta_table(conn)
    rows = conn.execute(
        """
        SELECT run_id, run_type, target, status, started_at, duration_ms, rows_affected, error
        FROM _havn.run_log
        WHERE pipeline_run_id = ?
        ORDER BY started_at ASC
        """,
        [pipeline_run_id],
    ).fetchall()

    return [
        {
            "run_id": r[0],
            "run_type": r[1],
            "target": r[2],
            "status": r[3],
            "started_at": str(r[4]) if r[4] else None,
            "duration_ms": r[5],
            "rows_affected": r[6],
            "error": r[7],
        }
        for r in rows
    ]


@router.get("/api/history/runs/{pipeline_run_id}/comparison")
def get_run_comparison(request: Request, pipeline_run_id: str, conn: DbConn) -> dict:
    """Compare a pipeline run with the previous run of the same type."""
    _require_permission(request, "read")
    ensure_meta_table(conn)

    # Get current run info
    current = conn.execute(
        """
        SELECT run_type, target, MIN(started_at) AS started_at
        FROM _havn.run_log
        WHERE pipeline_run_id = ?
        GROUP BY run_type, target
        """,
        [pipeline_run_id],
    ).fetchone()

    if not current:
        return {"models": [], "previous_run_id": None}

    # Find previous pipeline run of the same type
    prev = conn.execute(
        """
        SELECT pipeline_run_id
        FROM _havn.run_log
        WHERE pipeline_run_id IS NOT NULL
          AND pipeline_run_id != ?
          AND run_type = ?
          AND started_at < ?
        GROUP BY pipeline_run_id
        ORDER BY MIN(started_at) DESC
        LIMIT 1
        """,
        [pipeline_run_id, current[0], current[2]],
    ).fetchone()

    if not prev:
        return {"models": [], "previous_run_id": None}

    prev_run_id = prev[0]

    # Get current run models
    current_models = conn.execute(
        """
        SELECT target, duration_ms, rows_affected, status
        FROM _havn.run_log
        WHERE pipeline_run_id = ?
        """,
        [pipeline_run_id],
    ).fetchall()

    # Get previous run models
    prev_models = conn.execute(
        """
        SELECT target, duration_ms, rows_affected, status
        FROM _havn.run_log
        WHERE pipeline_run_id = ?
        """,
        [prev_run_id],
    ).fetchall()

    prev_map = {
        r[0]: {"duration_ms": r[1], "rows_affected": r[2], "status": r[3]}
        for r in prev_models
    }

    comparisons = []
    for m in current_models:
        target = m[0]
        prev_data = prev_map.get(target)
        comparisons.append({
            "target": target,
            "duration_ms": m[1],
            "rows_affected": m[2],
            "status": m[3],
            "prev_duration_ms": prev_data["duration_ms"] if prev_data else None,
            "prev_rows_affected": prev_data["rows_affected"] if prev_data else None,
            "prev_status": prev_data["status"] if prev_data else None,
            "is_new": prev_data is None,
        })

    # Check for removed models (in prev but not in current)
    current_targets = {m[0] for m in current_models}
    for target, data in prev_map.items():
        if target not in current_targets:
            comparisons.append({
                "target": target,
                "duration_ms": None,
                "rows_affected": None,
                "status": "removed",
                "prev_duration_ms": data["duration_ms"],
                "prev_rows_affected": data["rows_affected"],
                "prev_status": data["status"],
                "is_new": False,
            })

    return {"models": comparisons, "previous_run_id": prev_run_id}


# --- Scheduler status ---


@router.get("/api/scheduler")
def get_scheduler_status(request: Request) -> dict:
    """Get scheduler status and scheduled streams."""
    _require_permission(request, "read")
    from havn.engine.scheduler import get_scheduled_streams

    streams = get_scheduled_streams(_get_project_dir())
    return {"scheduled_streams": streams}
