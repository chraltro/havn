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

logger = logging.getLogger("havn.server")

router = APIRouter()


# --- Pydantic models ---


class RunScriptRequest(BaseModel):
    script_path: str = Field(..., min_length=1, max_length=500)


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
    script_path = _get_project_dir() / req.script_path
    if not script_path.exists():
        raise HTTPException(404, f"Script not found: {req.script_path}")
    script_type = "ingest" if "ingest" in req.script_path else "export"
    result = run_script(conn, script_path, script_type)
    from havn.engine.secrets import mask_output

    if result.get("log_output"):
        result["log_output"] = mask_output(result["log_output"], _get_project_dir())
    return result


# --- Stream cancellation ---

import threading

_cancel_flag = threading.Event()


@router.post("/api/stream/cancel")
def cancel_stream(request: Request) -> dict:
    """Cancel the currently running stream."""
    _require_permission(request, "execute")
    _cancel_flag.set()
    logger.info("Stream cancellation requested")
    return {"status": "cancelling"}


# --- Stream execution (SSE) ---


@router.get("/api/stream/{stream_name}/events")
async def run_stream_sse(
    request: Request, stream_name: str, force: bool = False
):
    """Run a stream with Server-Sent Events for real-time progress."""
    from fastapi.responses import StreamingResponse
    from havn.engine.database import connect as _connect
    from havn.server.deps import _get_db_path

    user = _require_permission(request, "execute")
    _cancel_flag.clear()
    logger.info("Stream SSE requested: %s (force=%s)", stream_name, force)

    # Audit pipeline start
    try:
        from havn.engine.audit import log_audit

        audit_conn = _connect(_get_db_path())
        try:
            client_ip = request.client.host if request.client else None
            log_audit(
                audit_conn,
                user=user.get("username", "anonymous"),
                action="transform",
                resource=stream_name,
                detail=f"stream started (force={force})",
                ip_address=client_ip,
            )
        finally:
            audit_conn.close()
    except Exception:
        logger.debug("Failed to write audit log for stream start", exc_info=True)

    config = _get_config()
    if stream_name not in config.streams:
        raise HTTPException(404, f"Stream '{stream_name}' not found")
    stream_config = config.streams[stream_name]

    # Resolve db_path before entering the generator
    db_path_str = str(_get_db_path())

    def _generate():
        import json as _json
        import time as _time
        import queue as _queue
        from concurrent.futures import ThreadPoolExecutor
        from graphlib import TopologicalSorter

        from havn.engine.database import connect as _connect, ensure_meta_table as _emt, log_run as _lr
        from havn.engine.runner import run_script as _run_script
        from havn.engine.transform import discover_models as _dm, build_dag as _bd, validate_models as _vm
        from havn.engine.transform.discovery import _compute_upstream_hash as _cuh, _has_changed as _hc, _update_state as _us
        from havn.engine.transform.execution import execute_model as _em
        from havn.engine.transform.quality import run_assertions as _ra, _save_assertions as _sa, profile_model as _pm, _save_profile as _sp
        from havn.engine.transform.models import ModelResult as _MR
        from havn.server.deps import _get_db_resource_limits

        _mem_limit, _threads = _get_db_resource_limits()

        def emit(event_type: str, data: dict):
            return f"event: {_json.dumps(event_type)}\ndata: {_json.dumps(data)}\n\n".replace(
                f"event: {_json.dumps(event_type)}", f"event: {event_type}"
            )

        def emit(event_type: str, data: dict):
            payload = _json.dumps(data)
            return f"event: {event_type}\ndata: {payload}\n\n"

        start = _time.perf_counter()
        has_error = False
        cancelled = False
        project_dir = _get_project_dir()

        def _is_cancelled():
            return _cancel_flag.is_set()

        # ── Build unified DAG ──
        # Nodes: "ingest:customers.py", "transform:bronze.customers", "export:report.py"
        # Edges: transform models depend on their declared deps; all transforms
        #        implicitly depend on ingest completing (since ingest populates landing.*).
        #        Exports depend on all transforms.

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
        # Node info: {node_id: {type, action, path/model, ...}}
        nodes = {}
        dag_deps = {}  # node_id -> set of dependency node_ids

        # Ingest nodes have no dependencies (they're roots)
        ingest_node_ids = set()
        for script in ingest_scripts:
            nid = f"ingest:{script.name}"
            nodes[nid] = {"type": "ingest", "path": script, "name": script.name}
            dag_deps[nid] = set()
            ingest_node_ids.add(nid)

        # Transform nodes depend on:
        #   - Other transform nodes (if depends_on references a model in the DAG)
        #   - ALL ingest nodes (since landing.* tables come from ingest scripts)
        #     But only if the model references landing.* in depends_on
        for model in ordered:
            nid = f"transform:{model.full_name}"
            nodes[nid] = {"type": "transform", "model": model, "name": model.full_name}
            deps = set()
            for dep in model.depends_on:
                dep_nid = f"transform:{dep}"
                if dep_nid in nodes:
                    deps.add(dep_nid)
                elif dep.startswith("landing."):
                    # This model depends on landing data — depends on ALL ingests
                    deps.update(ingest_node_ids)
            dag_deps[nid] = deps

        # Export nodes depend on all transforms
        transform_node_ids = {f"transform:{m.full_name}" for m in ordered}
        for script in export_scripts:
            nid = f"export:{script.name}"
            nodes[nid] = {"type": "export", "path": script, "name": script.name}
            dag_deps[nid] = transform_node_ids.copy()

        total_items = len(nodes)
        yield emit("start", {"stream": stream_name, "steps": 1, "total": total_items})

        if not nodes:
            yield emit("complete", {"stream": stream_name, "status": "success", "duration_seconds": 0})
            return

        # 5. Pre-build validation for transform models
        if models:
            conn_val = _connect(db_path_str, memory_limit=_mem_limit, threads=_threads)
            _emt(conn_val)
            try:
                _val_errors = _vm(conn_val, models)
            finally:
                conn_val.close()
            for _ve in _val_errors:
                yield emit("validation", {"model": _ve.model, "severity": _ve.severity, "message": _ve.message})
                if _ve.severity == "error":
                    has_error = True
            if has_error:
                yield emit("complete", {"stream": stream_name, "status": "failed", "duration_seconds": 0})
                return

        # 6. Pre-create schemas
        conn_setup = _connect(db_path_str, memory_limit=_mem_limit, threads=_threads)
        _emt(conn_setup)
        for schema in ("landing", "bronze", "silver", "gold"):
            conn_setup.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        conn_setup.close()

        # 7. Streaming DAG execution
        # Open a single shared connection for all operations
        conn = _connect(db_path_str, memory_limit=_mem_limit, threads=_threads)
        _emt(conn)

        result_q = _queue.Queue()
        sorter = TopologicalSorter(dag_deps)
        sorter.prepare()
        max_workers = _threads or 4
        executor = ThreadPoolExecutor(max_workers=max_workers)
        active = 0  # number of in-flight tasks

        def _exec_node(node_id):
            """Execute a single node and put result on queue."""
            info = nodes[node_id]
            try:
                if info["type"] == "ingest":
                    result = _run_script(conn, info["path"], "ingest")
                    result_q.put((node_id, {
                        "status": result["status"],
                        "duration_ms": result.get("duration_ms", 0),
                        "rows_affected": result.get("rows_affected", 0),
                        "error": result.get("error"),
                    }))
                elif info["type"] == "transform":
                    m = info["model"]
                    if not force and not _hc(conn, m):
                        result_q.put((node_id, {"status": "skipped"}))
                        return
                    duration_ms, row_count = _em(conn, m)
                    _us(conn, m, duration_ms, row_count)
                    _lr(conn, "transform", m.full_name, "success", duration_ms, row_count)
                    status = "built"
                    if m.assertions:
                        ar = _ra(conn, m)
                        _sa(conn, m, ar)
                        if any(not a.passed for a in ar):
                            status = "assertion_failed"
                    if m.materialized in ("table", "incremental"):
                        prof = _pm(conn, m)
                        _sp(conn, m, prof)
                    result_q.put((node_id, {
                        "status": status, "duration_ms": duration_ms,
                        "row_count": row_count,
                    }))
                elif info["type"] == "export":
                    result = _run_script(conn, info["path"], "export")
                    result_q.put((node_id, {
                        "status": result["status"],
                        "duration_ms": result.get("duration_ms", 0),
                        "error": result.get("error"),
                    }))
            except Exception as e:
                logger.error("Node %s failed: %s", node_id, e, exc_info=True)
                result_q.put((node_id, {"status": "error", "error": str(e)}))

        def _submit_ready():
            """Submit all ready nodes to the executor."""
            nonlocal active
            ready = list(sorter.get_ready())
            for nid in ready:
                if _is_cancelled():
                    break
                info = nodes[nid]
                action = info["type"]
                name = info["name"]
                yield_data = {"name": name, "action": action}
                if action == "transform":
                    yield_data["materialized"] = info["model"].materialized
                result_q.put(("__start__", yield_data))
                executor.submit(_exec_node, nid)
                active += 1
            return ready

        # Kick off initial ready nodes (roots with no deps)
        initial = list(sorter.get_ready())
        for nid in initial:
            info = nodes[nid]
            result_q.put(("__start__", {"name": info["name"], "action": info["type"]}))
            executor.submit(_exec_node, nid)
            active += 1

        # Process results as they arrive, submit newly ready nodes
        while sorter.is_active() or active > 0:
            if _is_cancelled():
                cancelled = True
                executor.shutdown(wait=False, cancel_futures=True)
                break

            try:
                node_id, result = result_q.get(timeout=2)
            except _queue.Empty:
                yield ": keepalive\n\n"
                continue

            # Handle start events (emitted before execution)
            if node_id == "__start__":
                yield emit("model_start", result)
                continue

            active -= 1
            info = nodes[node_id]
            status = result.get("status", "error")

            # Emit result
            yield emit("model_end", {
                "name": info["name"],
                "action": info["type"],
                "status": status,
                "duration_ms": result.get("duration_ms", 0),
                "row_count": result.get("row_count", 0),
                "rows_affected": result.get("rows_affected", 0),
                "error": result.get("error"),
                "materialized": info["model"].materialized if info["type"] == "transform" else None,
            })

            if status in ("error", "assertion_failed"):
                has_error = True

            # Mark node as done — this may release dependent nodes
            sorter.done(node_id)

            # Submit any newly ready nodes
            try:
                newly_ready = list(sorter.get_ready())
                for nid in newly_ready:
                    if _is_cancelled():
                        break
                    ni = nodes[nid]
                    start_data = {"name": ni["name"], "action": ni["type"]}
                    if ni["type"] == "transform":
                        start_data["materialized"] = ni["model"].materialized
                    yield emit("model_start", start_data)
                    executor.submit(_exec_node, nid)
                    active += 1
            except Exception:
                pass  # sorter exhausted

        executor.shutdown(wait=False)
        conn.close()

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
            audit_conn = _connect(db_path_str)
            try:
                log_audit(audit_conn, user=user.get("username", "anonymous"),
                          action="transform", resource=stream_name,
                          detail=f"stream completed: {status} in {duration_s}s")
            finally:
                audit_conn.close()
        except Exception:
            pass

        yield emit("complete", {
            "stream": stream_name,
            "status": status,
            "duration_seconds": duration_s,
        })

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
        FROM _dp_internal.run_log
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


# --- Scheduler status ---


@router.get("/api/scheduler")
def get_scheduler_status(request: Request) -> dict:
    """Get scheduler status and scheduled streams."""
    _require_permission(request, "read")
    from havn.engine.scheduler import get_scheduled_streams

    streams = get_scheduled_streams(_get_project_dir())
    return {"scheduled_streams": streams}
