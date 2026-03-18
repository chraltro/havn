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
    request: Request, stream_name: str, conn: DbConn, force: bool = False
):
    """Run a stream with Server-Sent Events for real-time progress."""
    from fastapi.responses import StreamingResponse

    _require_permission(request, "execute")
    _cancel_flag.clear()
    logger.info("Stream SSE requested: %s (force=%s)", stream_name, force)
    config = _get_config()
    if stream_name not in config.streams:
        raise HTTPException(404, f"Stream '{stream_name}' not found")
    stream_config = config.streams[stream_name]

    def _generate():
        import json as _json
        import time as _time

        def emit(event_type: str, data: dict):
            payload = _json.dumps(data)
            return f"event: {event_type}\ndata: {payload}\n\n"

        start = _time.perf_counter()
        has_error = False
        cancelled = False
        ingest_ran = False

        def _is_cancelled():
            return _cancel_flag.is_set()

        # Pre-compute total item count for progress numbering
        total_items = 0
        for _step in stream_config.steps:
            if _step.action in ("ingest", "export"):
                _dir = _get_project_dir() / _step.action
                if _dir.exists():
                    _files = [f for f in sorted(list(_dir.glob("*.py")) + list(_dir.glob("*.dpnb"))) if not f.name.startswith("_")]
                    total_items += len(_files)
            elif _step.action == "transform":
                from havn.engine.transform import discover_models as _dm_count
                total_items += len(_dm_count(_get_project_dir() / "transform"))

        yield emit("start", {"stream": stream_name, "steps": len(stream_config.steps), "total": total_items})

        for step_idx, step in enumerate(stream_config.steps):
            if _is_cancelled():
                cancelled = True
                break

            yield emit("step_start", {"action": step.action, "index": step_idx})

            if step.action == "ingest":
                from concurrent.futures import ThreadPoolExecutor, as_completed
                from havn.engine.runner import run_script as _run_script
                from havn.engine.database import connect as _connect

                ingest_dir = _get_project_dir() / "ingest"
                if not ingest_dir.exists():
                    yield emit("step_end", {"action": "ingest", "results": [], "error": False})
                    continue

                py_scripts = list(ingest_dir.glob("*.py"))
                nb_scripts = list(ingest_dir.glob("*.dpnb"))
                scripts = sorted(py_scripts + nb_scripts, key=lambda p: p.name)
                if step.targets and step.targets != ["all"]:
                    target_set = {t.removesuffix(".py").removesuffix(".dpnb") for t in step.targets}
                    scripts = [s for s in scripts if s.stem in target_set]

                scripts = [s for s in scripts if not s.name.startswith("_")]

                # Get db path for parallel connections
                from havn.server.deps import _get_db_path
                db_path_str = str(_get_db_path())

                # Pre-create common schemas on the main connection so parallel
                # scripts don't race on CREATE SCHEMA IF NOT EXISTS
                for schema in ("landing", "bronze", "silver", "gold"):
                    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

                def _run_ingest_script(script_path):
                    """Run a single ingest script with its own connection."""
                    script_conn = _connect(db_path_str)
                    try:
                        result = _run_script(script_conn, script_path, "ingest")
                        return script_path.name, result
                    finally:
                        script_conn.close()

                # Emit start for all scripts
                for script in scripts:
                    yield emit("model_start", {"name": script.name, "action": "ingest"})

                # Run all ingest scripts in parallel, emitting results as they complete
                import queue
                result_queue = queue.Queue()
                step_error = False

                def _run_and_enqueue(script_path):
                    name, result = _run_ingest_script(script_path)
                    result_queue.put((name, result))

                with ThreadPoolExecutor(max_workers=min(len(scripts), 4)) as executor:
                    for script in scripts:
                        executor.submit(_run_and_enqueue, script)

                    # Yield results as they arrive, with short poll to keep SSE alive
                    remaining = len(scripts)
                    while remaining > 0:
                        if _is_cancelled():
                            cancelled = True
                            executor.shutdown(wait=False, cancel_futures=True)
                            break
                        try:
                            script_name, result = result_queue.get(timeout=2)
                        except queue.Empty:
                            # Send SSE comment as keepalive
                            yield ": keepalive\n\n"
                            continue
                        remaining -= 1
                        if result.get("log_output"):
                            from havn.engine.secrets import mask_output
                            result["log_output"] = mask_output(result["log_output"], _get_project_dir())
                        yield emit("model_end", {
                            "name": script_name,
                            "action": "ingest",
                            "status": result["status"],
                            "duration_ms": result.get("duration_ms", 0),
                            "rows_affected": result.get("rows_affected", 0),
                            "error": result.get("error"),
                        })
                        if result["status"] == "success":
                            ingest_ran = True
                        if result["status"] == "error":
                            step_error = True

                if cancelled:
                    break
                yield emit("step_end", {"action": "ingest", "error": step_error})
                if step_error:
                    has_error = True
                    break

            elif step.action == "transform":
                from concurrent.futures import ThreadPoolExecutor, as_completed
                from havn.engine.database import ensure_meta_table as _emt, connect as _connect
                from havn.engine.transform import discover_models as _dm, build_dag as _bd
                from havn.engine.transform.discovery import (
                    _compute_upstream_hash as _cuh,
                    _has_changed as _hc,
                    _update_state as _us,
                    build_dag_tiers as _bdt,
                )
                from havn.engine.transform.execution import execute_model as _em, _execute_single_model as _esm
                from havn.engine.transform.quality import (
                    run_assertions as _ra,
                    _save_assertions as _sa,
                    profile_model as _pm,
                    _save_profile as _sp,
                )
                from havn.server.deps import _get_db_path

                _emt(conn)
                transform_dir = _get_project_dir() / "transform"
                force_transform = force or ingest_ran
                models = _dm(transform_dir)

                if step.targets and step.targets != ["all"]:
                    target_set = set(step.targets)
                    models = [m for m in models if m.full_name in target_set or m.name in target_set]

                ordered = _bd(models)
                model_map = {m.full_name: m for m in ordered}
                for model in ordered:
                    model.upstream_hash = _cuh(model, model_map)

                tiers = _bdt(models)
                db_path_str = str(_get_db_path())

                step_error = False
                model_results = {}

                for tier_idx, tier in enumerate(tiers):
                    if _is_cancelled():
                        cancelled = True
                        break

                    # Check if previous tier had blocking failures
                    if any(s in ("error", "assertion_failed") for s in model_results.values()):
                        for model in tier:
                            yield emit("model_end", {
                                "name": model.full_name,
                                "action": "transform",
                                "status": "skipped",
                                "materialized": model.materialized,
                            })
                            model_results[model.full_name] = "skipped"
                        continue

                    # Partition tier into skipped (unchanged) and needs-build
                    to_build = []
                    for model in tier:
                        changed = force_transform or _hc(conn, model)
                        if not changed:
                            yield emit("model_end", {
                                "name": model.full_name,
                                "action": "transform",
                                "status": "skipped",
                                "materialized": model.materialized,
                            })
                            model_results[model.full_name] = "skipped"
                        else:
                            to_build.append(model)

                    if not to_build:
                        continue

                    # Emit start for all models in this tier
                    for model in to_build:
                        yield emit("model_start", {
                            "name": model.full_name,
                            "action": "transform",
                            "materialized": model.materialized,
                        })

                    if len(to_build) == 1:
                        # Single model — run on main connection
                        model = to_build[0]
                        try:
                            duration_ms, row_count = _em(conn, model)
                            _us(conn, model, duration_ms, row_count)
                            from havn.engine.database import log_run as _lr
                            _lr(conn, "transform", model.full_name, "success", duration_ms, row_count)

                            assertion_status = "built"
                            if model.assertions:
                                ar_results = _ra(conn, model)
                                _sa(conn, model, ar_results)
                                if any(not ar.passed for ar in ar_results):
                                    assertion_status = "assertion_failed"

                            if model.materialized in ("table", "incremental"):
                                profile = _pm(conn, model)
                                _sp(conn, model, profile)

                            yield emit("model_end", {
                                "name": model.full_name,
                                "action": "transform",
                                "status": assertion_status,
                                "duration_ms": duration_ms,
                                "row_count": row_count,
                                "materialized": model.materialized,
                            })
                            model_results[model.full_name] = assertion_status
                            if assertion_status == "assertion_failed":
                                step_error = True
                        except Exception as e:
                            from havn.engine.database import log_run as _lr
                            _lr(conn, "transform", model.full_name, "error", error=str(e))
                            yield emit("model_end", {
                                "name": model.full_name,
                                "action": "transform",
                                "status": "error",
                                "error": str(e),
                                "materialized": model.materialized,
                            })
                            model_results[model.full_name] = "error"
                            step_error = True
                    else:
                        # Multiple models in tier — run in parallel
                        tier_completed = {}
                        with ThreadPoolExecutor(max_workers=min(len(to_build), 4)) as executor:
                            futures = {
                                executor.submit(_esm, db_path_str, model, force_transform, model_map): model
                                for model in to_build
                            }
                            for future in as_completed(futures):
                                if _is_cancelled():
                                    cancelled = True
                                    executor.shutdown(wait=False, cancel_futures=True)
                                    break
                                model_name, model_result = future.result()
                                tier_completed[model_name] = model_result

                        # Emit results in original tier order
                        for model in to_build:
                            mr = tier_completed.get(model.full_name)
                            if not mr:
                                continue
                            yield emit("model_end", {
                                "name": model.full_name,
                                "action": "transform",
                                "status": mr.status,
                                "duration_ms": mr.duration_ms,
                                "row_count": mr.row_count,
                                "error": mr.error,
                                "materialized": model.materialized,
                            })
                            model_results[model.full_name] = mr.status
                            if mr.status in ("error", "assertion_failed"):
                                step_error = True

                if cancelled:
                    break
                yield emit("step_end", {"action": "transform", "results": model_results, "error": step_error})
                if step_error:
                    has_error = True
                    break

            elif step.action == "export":
                from havn.engine.runner import run_script as _run_script

                export_dir = _get_project_dir() / "export"
                if not export_dir.exists():
                    yield emit("step_end", {"action": "export", "results": [], "error": False})
                    continue

                py_scripts = list(export_dir.glob("*.py"))
                nb_scripts = list(export_dir.glob("*.dpnb"))
                scripts = sorted(py_scripts + nb_scripts, key=lambda p: p.name)

                script_results = []
                step_error = False
                for script in scripts:
                    if script.name.startswith("_"):
                        continue
                    if _is_cancelled():
                        cancelled = True
                        break
                    yield emit("model_start", {"name": script.name, "action": "export"})
                    result = _run_script(conn, script, "export")
                    script_results.append(result)
                    yield emit("model_end", {
                        "name": script.name,
                        "action": "export",
                        "status": result["status"],
                        "duration_ms": result.get("duration_ms", 0),
                        "error": result.get("error"),
                    })
                    if result["status"] == "error":
                        step_error = True

                if cancelled:
                    break
                yield emit("step_end", {"action": "export", "error": step_error})
                if step_error:
                    has_error = True
                    break

        duration_s = round(_time.perf_counter() - start, 1)
        if cancelled:
            status = "cancelled"
        elif has_error:
            status = "failed"
        else:
            status = "success"
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
    _require_permission(request, "execute")
    logger.info("Stream run requested: %s (force=%s)", stream_name, force)
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
