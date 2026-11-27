"""Run a stream: the named list of steps under ``streams:`` in project.yml.

The entry point for ``havn stream`` and for streams fired by the scheduler.
It opens the warehouse (unless given a connection), registers the project's
macros and hands the stream to the pipeline engine
(``engine.pipeline_engine``), the same executor the web UI's runs use. So a
CLI, scheduled or UI run of one stream does the same thing:

- its steps in their listed order (seed, ingest, transform, export); the
  models of a transform step build in parallel tiers, the scripts of a step
  one at a time;
- the pre-build validation gate before the models are built;
- the stream's ``retries`` / ``retry_delay`` for each failed script or model;
- a Pipeline Rewind run around the whole stream, then snapshot GC;
- ``run_log`` rows under one ``pipeline_run_id`` and a ``job_runs`` row;
- the stream's ``webhook_url`` notification and the project's ``alerts:``
  pipeline success/failure alert.

A step fails on the statuses ``havn transform`` exits 1 for
(``TRANSFORM_FAILURE_STATUSES``: ``error`` and ``assertion_failed``), on a
policy denial, and on a failed script or seed; a failed step stops the
stream, so a model rejected by an error-severity ``@assert`` stops it
before its export step.

Progress is reported through an ``on_event(event, data)`` callback; the CLI
renders it, the scheduler logs. See :func:`run_stream` for the events.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from havn.engine.pipeline_engine import send_stream_webhook  # noqa: F401 - re-exported

logger = logging.getLogger("havn.stream")

# Model statuses that fail a transform step. Kept in line with the
# `havn transform` command, which exits 1 on exactly these. (The engine
# also fails a step on a policy denial: see pipeline_engine.FAILED.)
TRANSFORM_FAILURE_STATUSES = frozenset({"error", "assertion_failed"})

StreamEvent = Callable[[str, dict], None]


class StreamNotFound(KeyError):
    """The stream name is not defined in project.yml."""


@dataclass
class StepOutcome:
    action: str
    targets: list[str]
    ok: bool
    attempts: int = 1
    results: Any = None          # transform/seed: {name: status}; scripts: [status]
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "targets": self.targets,
            "status": "success" if self.ok else "error",
            "attempts": self.attempts,
            "results": self.results,
            "error": self.error,
        }


@dataclass
class StreamResult:
    stream: str
    ok: bool = True
    steps: list[StepOutcome] = field(default_factory=list)
    duration_s: float = 0.0
    error: str | None = None
    models_run: list[str] = field(default_factory=list)
    models_built: int = 0
    pipeline_run_id: str | None = None
    nodes: list[dict] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "success" if self.ok else "error"

    def to_dict(self) -> dict:
        return {
            "stream": self.stream,
            "status": self.status,
            "duration_seconds": self.duration_s,
            "error": self.error,
            "steps": [s.to_dict() for s in self.steps],
            "pipeline_run_id": self.pipeline_run_id,
        }


def run_stream(
    config,
    project_dir: Path,
    name: str,
    *,
    force: bool = False,
    trigger: str = "manual",
    on_event: StreamEvent | None = None,
    conn=None,
    sleep: Callable[[float], None] = time.sleep,
    user: dict | None = None,
) -> StreamResult:
    """Run stream ``name`` from ``config`` and return its result.

    Opens (and closes) its own warehouse connection unless ``conn`` is
    given. ``trigger`` is ``"manual"``, ``"scheduled"`` or ``"ui"`` and
    labels the Rewind run and the ``job_runs`` row. ``user`` runs the
    scripts governed for that user. Raises :class:`StreamNotFound` for an
    unknown stream; every other failure is reported in the result, never
    raised.

    ``on_event`` receives the engine's events (see
    :func:`havn.engine.pipeline_engine.run_pipeline`) plus ``macros``
    [count] before the run and ``finished`` [ok, duration_s, error] when it
    ends; ``step_start`` / ``step_end`` / ``retry`` carry ``action``.
    """
    from havn.engine.pipeline_engine import run_pipeline, stream_spec

    stream_config = config.streams.get(name)
    if stream_config is None:
        raise StreamNotFound(name)

    def emit(event: str, data: dict) -> None:
        if on_event is None:
            return
        try:
            on_event(event, data)
        except Exception as exc:
            logger.debug("Stream event handler failed on %s: %s", event, exc)

    def sink(event: str, data: dict) -> None:
        if event == "run_end":
            emit("finished", {"ok": data["status"] == "success",
                              "duration_s": data["duration_s"], "error": data["error"]})
        emit(event, data)

    own_conn = conn is None
    if own_conn:
        from havn.engine.database import open_warehouse

        conn = open_warehouse(config, project_dir)

    try:
        if own_conn and (project_dir / "macros").is_dir():
            try:
                from havn.engine.macros import register_macros

                n_macros = register_macros(conn, project_dir)
                if n_macros:
                    emit("macros", {"count": n_macros})
            except Exception as exc:
                logger.warning("Macro registration failed for stream %s: %s", name, exc)

        spec = stream_spec(name, stream_config, force=force, user=user, trigger=trigger,
                           echo=trigger != "ui")
        run = run_pipeline(spec, project_dir, config, conn, sink, sleep=sleep)
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass

    result = StreamResult(
        stream=name, ok=run.ok, duration_s=run.duration_s, error=run.error,
        models_run=run.models_run, models_built=run.models_built,
        pipeline_run_id=run.run_id, nodes=run.nodes,
        steps=[
            StepOutcome(s.action, s.targets, ok=s.ok, attempts=s.attempts,
                        results=s.results, error=s.error)
            for s in run.steps if s.ran
        ],
    )
    if result.ok:
        logger.info("Stream '%s' completed in %ss", name, result.duration_s)
    else:
        logger.error("Stream '%s' failed after %ss: %s", name, result.duration_s, result.error)
    return result
