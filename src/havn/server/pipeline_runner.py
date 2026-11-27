"""The web UI's side of a pipeline run: engine events in, browser events out.

``POST /api/stream/{name}/start``, ``POST /api/pipeline/start`` and
``POST /api/transform/start`` (``server/routes/pipeline.py``) describe a run
as a :class:`havn.engine.pipeline_engine.RunSpec` and hand it to
:func:`run_pipeline` here on a background thread. The run itself is the
engine's (``engine/pipeline_engine.py``), on the server's shared connection;
this module only turns its events into the SSE events the browser reads.

The event contract with the browser (``frontend/src/PipelineContext.tsx``)
is fixed; see ``_start_payload``, ``_end_payload`` and ``_complete_payload``.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from havn.engine.pipeline_engine import RunSpec

logger = logging.getLogger("havn.server")

Emit = Callable[[str, dict], None]

# Statuses the engine reports, as the browser is told about them. The
# frontend knows "built", "skipped", "inlined", "error" and
# "assertion_failed", and shows anything else as a green "Built ...". So a
# model held back by a stale error-severity source, or by a blocked or
# failed upstream, is reported "skipped" with the reason in ``error``
# (``run_log`` records both as skipped too), and a model a ``policies.deny``
# rule refused is reported "error" (``run_log`` records it as an error).
_EVENT_STATUS = {
    "source_stale": "skipped",
    "skipped_upstream_blocked": "skipped",
    "policy_denied": "error",
}


def _start_payload(spec: RunSpec, run_id: str, total: int) -> dict:
    if spec.operation == "transform":
        return {"operation": "transform", "label": spec.label, "pipeline_run_id": run_id}
    return {
        "operation": spec.operation, "label": spec.label, "stream": spec.name,
        # The browser counts progress against "total"; "steps" has always
        # been the number of step kinds a selective pipeline asked for.
        "steps": _step_count(spec), "total": total, "pipeline_run_id": run_id,
    }


def _step_count(spec: RunSpec) -> int:
    return len(spec.steps) if spec.operation == "pipeline" else 1


def _end_payload(spec: RunSpec, data: dict) -> dict:
    raw = data.get("status") or "error"
    status = _EVENT_STATUS.get(raw, raw)
    if spec.operation == "transform":
        return {
            "name": data["name"], "action": data["kind"], "status": status,
            "duration_ms": data.get("duration_ms", 0),
            "row_count": data.get("row_count", 0), "num": data["num"],
            "error": data.get("error"),
        }
    return {
        "name": data["name"], "action": data["kind"], "status": status,
        "duration_ms": data.get("duration_ms", 0),
        "row_count": data.get("row_count", 0),
        "rows_affected": data.get("rows_affected", 0),
        "error": data.get("error"),
        "materialized": data.get("materialized"),
        "num": data["num"],
    }


def _complete_payload(
    spec: RunSpec, run_id: str, status: str, duration_s: float, error: str | None = None,
) -> dict:
    if spec.operation == "transform":
        data = {
            "operation": "transform", "status": status,
            "duration_seconds": duration_s, "pipeline_run_id": run_id,
        }
        if error is not None:
            data["error"] = error
        return data
    return {
        "stream": spec.name, "status": status,
        "duration_seconds": duration_s, "pipeline_run_id": run_id,
    }


class _SseEvents:
    """Turns the engine's events into the browser's, through ``emit``."""

    def __init__(self, spec: RunSpec, run_id: str, emit: Emit):
        self.spec = spec
        self.run_id = run_id
        self.emit = emit
        self.completed = False

    def __call__(self, event: str, data: dict) -> None:
        spec = self.spec
        if event == "run_start":
            if spec.operation != "transform":
                self.emit("start", _start_payload(spec, self.run_id, data["total"]))
        elif event == "node_start":
            start = {"name": data["name"], "action": data["kind"], "num": data["num"]}
            if data["kind"] == "transform":
                start["materialized"] = data["materialized"]
            self.emit("model_start", start)
        elif event == "node_end":
            self.emit("model_end", _end_payload(spec, data))
        elif event == "validation":
            self.emit("validation", dict(data))
        elif event == "run_end":
            # A run that broke off tells the browser why, as it always did
            # for a transform; a stream's complete event has no error field.
            error = data.get("error") if (data.get("crashed") and spec.operation == "transform") else None
            self.completed = True
            self.emit("complete", _complete_payload(
                spec, self.run_id, data["status"], data["duration_s"], error,
            ))
        # step_start / step_end / retry / gc / webhook / alert: not shown.


def run_pipeline(
    spec: RunSpec,
    project_dir: Path,
    emit: Emit,
    cancel: threading.Event,
) -> str:
    """Run ``spec`` on the server's shared connection, emitting browser events.

    Always ends with exactly one ``complete`` event and returns the final
    status. The caller marks the operation finished afterwards.
    """
    from havn.engine.pipeline_engine import run_pipeline as engine_run
    from havn.server.deps import _get_backend, _get_config, _get_shared_conn

    run_id = str(uuid.uuid4())
    started = time.perf_counter()
    events = _SseEvents(spec, run_id, emit)
    if spec.operation == "transform":
        emit("start", _start_payload(spec, run_id, 0))
    try:
        config = _get_config()
        conn = _get_shared_conn()
        # The server's backend may come from a factory (havn-cloud) rather
        # than project.yml; DuckLake there still means one worker.
        workers = 1 if _get_backend().name == "ducklake" else None
        result = engine_run(spec, project_dir, config, conn, events,
                            run_id=run_id, cancel=cancel, max_workers=workers)
        return result.status
    except Exception as e:
        logger.error("Pipeline run failed: %s", e, exc_info=True)
        if not events.completed:
            duration_s = round(time.perf_counter() - started, 1)
            error = str(e) if spec.operation == "transform" else None
            emit("complete", _complete_payload(spec, run_id, "failed", duration_s, error))
        return "failed"
