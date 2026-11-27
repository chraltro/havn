"""Pipeline execution, streams, history, and scheduler endpoints."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from havn.server.deps import (
    DbConn,
    _get_config,
    _get_project_dir,
    _require_permission,
    ensure_meta_table,
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


# --- The run gate ---
#
# Every operation that writes to the warehouse holds the project's run gate
# (engine/run_gate.py) while it runs, exclusively: the scheduler's worker
# pool takes the same gate (shared) for its runs under `havn serve
# --schedule`, so a UI run and a scheduled run never overlap.


@contextmanager
def exclusive_run(label: str):
    """Hold the run gate for a blocking endpoint's run; HTTP 409 when busy."""
    from havn.engine.run_gate import GateBusy, gate_for

    taken = gate_for(_get_project_dir()).try_exclusive(label)
    if isinstance(taken, GateBusy):
        raise HTTPException(409, taken.message)
    try:
        yield
    finally:
        taken.release()


def _busy_response(busy) -> dict:
    """The "already running" answer, saying what holds the gate."""
    from havn.engine.run_gate import SHARED

    scheduled = busy.kind == SHARED
    return {
        "status": "already_running",
        # Held here by a blocking endpoint (a background UI operation is
        # caught earlier, by the "running" flag): name what holds it.
        "operation": "scheduled" if scheduled else "run",
        "operation_label": ", ".join(busy.labels),
        "scheduled": scheduled,
        "message": busy.message,
    }


# --- Script execution ---


@router.post("/api/run")
def run_script_endpoint(
    request: Request, req: RunScriptRequest, conn: DbConn
) -> dict:
    """Run an ingest or export script.

    Runs for the requesting user: if masking or row policies apply to them,
    the script runs in a governed child process (see engine/governance).
    """
    user = _require_permission(request, "execute")
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
    with exclusive_run(f"script '{req.script_path}'"):
        result = run_script(conn, script_path, script_type, force=req.force, run_as=user)
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
    "gate_ticket": None,  # the run gate, held while a warehouse operation runs
}
_pipeline_lock = threading.Lock()
_pipeline_cond = threading.Condition(_pipeline_lock)

_STALE_TIMEOUT = 600  # 10 minutes


def _start_operation(operation: str, label: str, target_fn, args: tuple, *, gated: bool = True) -> dict:
    """Start a background operation. Returns status dict.

    Atomic check-and-set under a single lock acquisition: returns
    'already_running' if a UI operation is in progress, or if the project's
    run gate is held by scheduled runs (``"scheduled": true`` and a
    ``message`` saying which); otherwise reserves the slot, takes the gate
    (``gated``: every operation that writes to the warehouse) and starts
    the worker thread. ``_finish_operation`` gives the gate back.
    """
    from havn.engine.run_gate import GateBusy, gate_for

    with _pipeline_cond:
        if _pipeline_state["running"]:
            return {
                "status": "already_running",
                "operation": _pipeline_state["operation"],
                "operation_label": _pipeline_state["operation_label"],
                "scheduled": False,
                "message": f"An operation is already running ({_pipeline_state['operation_label']}).",
            }
        ticket = None
        if gated:
            taken = gate_for(_get_project_dir()).try_exclusive(f"{operation} '{label}'")
            if isinstance(taken, GateBusy):
                return _busy_response(taken)
            ticket = taken
        _pipeline_state["gate_ticket"] = ticket
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
    try:
        t.start()
    except BaseException:
        _finish_operation()
        raise
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
        from havn.engine.write_queue import cursor_for
        from havn.server.deps import _get_shared_conn

        # A cursor, never the shared connection itself: on DuckDB 2.0 a
        # connection another thread is using refuses the call instead of
        # waiting for it.
        cur = cursor_for(_get_shared_conn())
        try:
            cur.execute("FORCE CHECKPOINT")
        finally:
            cur.close()
    except Exception:
        pass
    with _pipeline_cond:
        ticket = _pipeline_state.get("gate_ticket")
        _pipeline_state["gate_ticket"] = None
        _pipeline_state["running"] = False
        _pipeline_state["finished"] = True
        _pipeline_cond.notify_all()
    if ticket is not None:
        ticket.release()


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

        from havn.engine.write_queue import cursor_for

        conn = cursor_for(_get_shared_conn())
        contracts_dir = project_dir / "contracts"
        try:
            results = run_contracts(conn, contracts_dir)
        finally:
            conn.close()

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


def _run_script_thread(script_path_str, project_dir, force=False, user=None):
    """Run a single script in background thread."""
    import time as _time
    from havn.server.deps import _get_shared_conn

    start = _time.perf_counter()
    _emit("start", {"operation": "script", "label": script_path_str})
    try:
        from havn.engine.runner import run_script

        from havn.engine.write_queue import cursor_for

        conn = cursor_for(_get_shared_conn())
        script_path = project_dir / script_path_str
        script_type = "ingest" if "ingest" in script_path_str else "export"
        result = run_script(conn, script_path, script_type, force=force, run_as=user)
        if not result.get("orphaned"):
            # A script that timed out and is still running owns the cursor;
            # closing it under a running query is not safe, so it is left
            # for the garbage collector.
            conn.close()

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


# --- Background threads: transform, stream, selective pipeline ---
#
# All three describe their run as a RunSpec and hand it to the pipeline
# engine (havn.engine.pipeline_engine) through havn.server.pipeline_runner,
# which turns the engine's events into the browser's. `havn stream` and
# scheduled streams run through the same engine.


def _run_spec_thread(spec, project_dir):
    from havn.server.pipeline_runner import run_pipeline

    try:
        run_pipeline(spec, project_dir, _emit, _cancel_flag)
    finally:
        _finish_operation()


def _run_transform_thread(targets, force, project_dir):
    """Run SQL transforms in background thread."""
    from havn.engine.pipeline_engine import RunSpec, Step

    label = "Transform"
    if targets:
        label += f" ({', '.join(targets)})"
    if force:
        label += " --force"
    spec = RunSpec(
        operation="transform", label=label, force=force,
        steps=[Step("transform", list(targets) if targets else ["all"])],
        ordered=False,
        # This endpoint never ran the pre-build gate; it builds what it is
        # asked to and reports each model's result.
        gate=False,
        rewind_trigger="ui",
        # Alerts as `havn transform` does, under the same target name.
        alert="when_built",
        alert_target="transform",
    )
    _run_spec_thread(spec, project_dir)


def _stream_spec(stream_name, stream_config, force, user):
    """The RunSpec for a stream run from the UI: the engine's, plus audit."""
    from havn.engine.pipeline_engine import stream_spec

    spec = stream_spec(stream_name, stream_config, force=force, user=user, trigger="ui")
    spec.audit_resource = stream_name
    spec.audit_detail = "stream completed"
    return spec


def _run_pipeline_thread(stream_name, stream_config, project_dir, db_path_str, force, user):
    """Run a stream from project.yml in the background, as `havn stream` would."""
    _run_spec_thread(_stream_spec(stream_name, stream_config, force, user), project_dir)


def _run_selective_pipeline_thread(steps, force, project_dir, user):
    """Run the chosen steps (ingest / transform / export) over everything.

    Not a stream: the chosen steps go into one DAG (``ordered=False``), so a
    failed load holds back only the models that read it, plus the exports.
    """
    from havn.engine.pipeline_engine import RunSpec, Step

    step_label = "+".join(steps)
    if force:
        step_label += " --force"
    spec = RunSpec(
        operation="pipeline",
        label=f"Pipeline ({step_label})",
        name="pipeline",
        steps=[Step(s, ["all"]) for s in steps],
        ordered=False,
        force=force,
        user=user,
        job=(f"pipeline ({step_label})", "pipeline", step_label),
        job_trigger="pipeline",
        audit_resource=f"pipeline({step_label})",
        audit_detail="selective pipeline completed",
        rewind_trigger="ui",
        alert="when_built",
        alert_target="pipeline",
    )
    _run_spec_thread(spec, project_dir)


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
        return {
            "status": "already_running",
            "stream_name": result["operation_label"] if result.get("scheduled") else _pipeline_state["stream_name"],
            "scheduled": result.get("scheduled", False),
            "message": result.get("message"),
        }
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
    # Lint reads and fixes SQL files, never the warehouse: no run gate.
    return _start_operation("lint", f"Lint{' --fix' if fix else ''}", _run_lint_thread,
                            (fix, project_dir, config), gated=False)


@router.post("/api/contracts/run/start")
def start_contracts(request: Request) -> dict:
    """Start contracts in background thread. Returns immediately."""
    _require_permission(request, "execute")
    project_dir = _get_project_dir()
    return _start_operation("contracts", "Contracts", _run_contracts_thread, (project_dir,))


@router.post("/api/run/start")
def start_script(request: Request, req: RunScriptRequest) -> dict:
    """Start a script in background thread. Returns immediately."""
    user = _require_permission(request, "execute")
    project_dir = _get_project_dir()
    script_path = (project_dir / req.script_path).resolve()
    # Path traversal protection
    if not script_path.is_relative_to(project_dir.resolve()):
        raise HTTPException(400, "Invalid script path")
    if not script_path.exists():
        raise HTTPException(404, f"Script not found: {req.script_path}")
    return _start_operation("script", req.script_path, _run_script_thread, (req.script_path, project_dir, req.force, user))


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
    with _pipeline_cond:
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
    """Run a full stream and wait for it; 409 while another run holds the gate.

    Runs through the pipeline engine like every other stream run (steps in
    listed order, retries, Rewind, webhook, alerts), on the server's shared
    connection.
    """
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

    from havn.engine.stream_runner import run_stream
    from havn.server.deps import _get_shared_conn

    with exclusive_run(f"stream '{stream_name}'"):
        result = run_stream(
            config, _get_project_dir(), stream_name, force=force, trigger="ui",
            conn=_get_shared_conn(), user=user,
        )

    return {
        "stream": stream_name,
        "steps": [{"action": s.action, "results": s.results} for s in result.steps],
        "status": "success" if result.ok else "failed",
        "duration_seconds": result.duration_s,
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


def _run_label(first: str | None, targets: list[str] | None) -> str | None:
    """What a run touched, in one line: its target, or the first plus a count."""
    distinct = [t for t in (targets or []) if t]
    if len(distinct) <= 1:
        return first
    return f"{first} + {len(distinct) - 1} more"


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
            -- The first thing the run built, not MIN(target): that is just
            -- the alphabetically first model, so every whole-DAG run was
            -- labelled "bronze.accounts" or similar.
            arg_min(target, started_at) AS first_target,
            MIN(started_at) AS started_at,
            -- Roll up to "failed" if ANY step failed. This has to be a numeric
            -- aggregate: MAX() over the status strings sorts alphabetically, and
            -- 'success' > 'failed' > 'error', so a run with one successful step
            -- reported "success" no matter how many steps errored.
            CASE
                WHEN SUM(CASE WHEN status IN ('error', 'failed') THEN 1 ELSE 0 END) > 0
                THEN 'failed'
                ELSE 'success'
            END AS status,
            -- Wall-clock time, not the sum of each model's time. The sum was
            -- wrong both ways: models in a tier run in parallel (overstated),
            -- and per-model time leaves out assertions, profiling and
            -- snapshots between models (understated, often by half).
            (MAX(epoch_ms(started_at) + COALESCE(duration_ms, 0)) - MIN(epoch_ms(started_at))) AS total_duration_ms,
            COUNT(*) AS model_count,
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
            SUM(CASE WHEN status IN ('error', 'failed') THEN 1 ELSE 0 END) AS error_count,
            SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped_count,
            SUM(rows_affected) AS total_rows,
            list(DISTINCT target ORDER BY target) AS targets
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
            "target": _run_label(r[2], r[11]),
            "targets": list(r[11] or []),
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


@router.get("/api/scheduler/history")
def get_scheduler_history(
    request: Request,
    conn: DbConn,
    limit: int = 50,
    name: str | None = None,
    status: str | None = None,
) -> dict:
    """Recorded schedule fires, newest first (``_havn.schedule_history``).

    One row per fire: when it was due (UTC), when it started and finished,
    how late it started, and its status (success, failed, skipped_overlap,
    missed, interrupted, cancelled, queued, running).
    """
    _require_permission(request, "read")
    from havn.engine.schedule_history import read_history

    return {"history": read_history(conn, limit=limit, name=name, status=status)}
