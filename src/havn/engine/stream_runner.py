"""Run a stream: the named list of steps under ``streams:`` in project.yml.

One implementation for ``havn stream`` and for streams fired by the
scheduler, so a scheduled run does exactly what a manual one does:

- every step kind (``seed``, ``ingest``, ``transform``, ``export``);
- the stream's ``retries`` / ``retry_delay``;
- a Pipeline Rewind run around the whole stream (snapshots taken by the
  transform step), finished with the stream's status, then snapshot GC;
- the stream's ``webhook_url`` notification;
- the project's ``alerts:`` pipeline success/failure alert.

A transform step fails on the statuses ``havn transform`` exits 1 for
(``error`` and ``assertion_failed``), so a model rejected by an
error-severity ``@assert`` stops the stream before its export step.

Progress is reported through an ``on_event(event, data)`` callback; the
CLI renders it, the scheduler logs. Nothing here prints on its own except
what the step engines (runner, transform, seeds) already print.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("havn.stream")

# Model statuses that fail a transform step. Kept in line with the
# `havn transform` command, which exits 1 on exactly these.
TRANSFORM_FAILURE_STATUSES = frozenset({"error", "assertion_failed"})

# Failures that a retry cannot change: the same inputs give the same
# result (orchestration jobs do not retry these either).
_DETERMINISTIC_FAILURES = frozenset({"assertion_failed"})

StreamEvent = Callable[[str, dict], None]


class StreamNotFound(KeyError):
    """The stream name is not defined in project.yml."""


@dataclass
class StepOutcome:
    action: str
    targets: list[str]
    ok: bool
    attempts: int = 1
    results: Any = None          # the step engine's raw return value
    error: str | None = None
    retryable: bool = True

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
        }


def send_stream_webhook(url: str, stream_name: str, status: str, duration_s: float) -> str | None:
    """POST the stream's completion to ``url``. Returns an error string, or None."""
    from urllib.request import Request, urlopen

    payload = json.dumps({
        "stream": stream_name,
        "status": status,
        "duration_seconds": duration_s,
        "timestamp": datetime.now().isoformat(),
    }).encode()
    try:
        req = Request(url, data=payload, headers={"Content-Type": "application/json"})
        urlopen(req, timeout=10)  # noqa: S310 - user-configured endpoint
        return None
    except Exception as e:
        return str(e)


def _script_step(conn, project_dir: Path, action: str, targets: list[str]) -> StepOutcome:
    from havn.engine.runner import run_scripts_in_dir

    results = run_scripts_in_dir(conn, project_dir / action, action, targets)
    failed = [r for r in results if r.get("status") == "error"]
    out = StepOutcome(action, targets, ok=not failed, results=[r.get("status") for r in results])
    if failed:
        names = ", ".join(Path(str(r.get("script", "?"))).name for r in failed)
        out.error = f"{action} failed: {names}"
    # A script still running after its timeout owns the connection; running
    # it again (or anything else) on that connection would race it.
    if any(r.get("orphaned") for r in results):
        out.retryable = False
    return out


def _run_step(conn, config, project_dir: Path, step, *, force: bool, run_id: str | None) -> StepOutcome:
    action = step.action
    targets = list(step.targets)
    if action in ("ingest", "export"):
        return _script_step(conn, project_dir, action, targets)
    if action == "transform":
        from havn.engine.transform import run_transform

        results = run_transform(
            conn,
            project_dir / "transform",
            targets=targets if targets != ["all"] else None,
            force=force,
            project_dir=project_dir,
            rewind_config=config.rewind if run_id else None,
            run_id=run_id,
        )
        failing = {n: s for n, s in results.items() if s in TRANSFORM_FAILURE_STATUSES}
        out = StepOutcome(action, targets, ok=not failing, results=dict(results))
        if failing:
            errors = sum(1 for s in failing.values() if s == "error")
            asserts = sum(1 for s in failing.values() if s == "assertion_failed")
            out.error = f"transform failed: {errors} errors, {asserts} assertion failures"
            out.retryable = not set(failing.values()) <= _DETERMINISTIC_FAILURES
        return out
    if action == "seed":
        from havn.engine.seeds import run_seeds

        results = run_seeds(conn, project_dir / "seeds", force=force)
        failed = [n for n, s in results.items() if s == "error"]
        out = StepOutcome(action, targets, ok=not failed, results=dict(results))
        if failed:
            out.error = f"seed failed: {', '.join(failed)}"
        return out
    return StepOutcome(
        action, targets, ok=False, retryable=False,
        error=f"unknown step action {action!r} (expected seed, ingest, transform or export)",
    )


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
) -> StreamResult:
    """Run stream ``name`` from ``config`` and return its result.

    Opens (and closes) its own warehouse connection unless ``conn`` is
    given. ``trigger`` is ``"manual"`` or ``"scheduled"`` and only labels the
    Rewind run. Raises :class:`StreamNotFound` for an unknown stream; every
    other failure is reported in the result, never raised.
    """
    stream_config = config.streams.get(name)
    if stream_config is None:
        raise StreamNotFound(name)

    def emit(event: str, **data) -> None:
        if on_event is None:
            return
        try:
            on_event(event, data)
        except Exception as exc:
            logger.debug("Stream event handler failed on %s: %s", event, exc)

    own_conn = conn is None
    if own_conn:
        from havn.engine.database import open_warehouse

        conn = open_warehouse(config, project_dir)

    result = StreamResult(stream=name)
    start = time.perf_counter()
    try:
        if (project_dir / "macros").is_dir():
            try:
                from havn.engine.macros import register_macros

                n_macros = register_macros(conn, project_dir)
                if n_macros:
                    emit("macros", count=n_macros)
            except Exception as exc:
                logger.warning("Macro registration failed for stream %s: %s", name, exc)

        run_id = None
        if config.rewind.enabled:
            try:
                from havn.engine.snapshots import start_run

                label = f"stream:{name}" if trigger == "manual" else f"{trigger}:{name}"
                run_id = start_run(project_dir, trigger=label)
            except Exception as exc:
                logger.warning("Could not start a Rewind run for stream %s: %s", name, exc)

        models_run: dict[str, None] = {}
        for step in stream_config.steps:
            outcome = _attempt(conn, config, project_dir, step, force=force, run_id=run_id, emit=emit)
            for retry in range(1, stream_config.retries + 1):
                if outcome.ok or not outcome.retryable:
                    break
                emit("retry", action=step.action, attempt=retry,
                     retries=stream_config.retries, delay=stream_config.retry_delay)
                logger.warning(
                    "Stream '%s' step '%s' failed; retry %d/%d after %ds",
                    name, step.action, retry, stream_config.retries, stream_config.retry_delay,
                )
                sleep(stream_config.retry_delay)
                attempts = outcome.attempts
                outcome = _attempt(conn, config, project_dir, step, force=force, run_id=run_id, emit=emit)
                outcome.attempts = attempts + 1
            result.steps.append(outcome)
            if step.action == "transform" and isinstance(outcome.results, dict):
                models_run.update(dict.fromkeys(outcome.results))
                result.models_built += sum(1 for s in outcome.results.values() if s == "built")
            if not outcome.ok:
                result.ok = False
                result.error = outcome.error or f"{step.action} step failed"
                break

        result.models_run = list(models_run)
        result.duration_s = round(time.perf_counter() - start, 1)
        emit("finished", ok=result.ok, duration_s=result.duration_s, error=result.error)

        if run_id is not None:
            _finish_rewind(config, project_dir, run_id, result, emit)

        if stream_config.webhook_url:
            err = send_stream_webhook(
                stream_config.webhook_url, name,
                "success" if result.ok else "failed", result.duration_s,
            )
            emit("webhook", url=stream_config.webhook_url, error=err)

        from havn.engine.pipeline_alerts import send_pipeline_alert

        sent = send_pipeline_alert(
            config.alerts, name, ok=result.ok, duration_s=result.duration_s,
            conn=conn, error=result.error, models_built=result.models_built,
        )
        if sent:
            emit("alert", results=sent)
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass

    if result.ok:
        logger.info("Stream '%s' completed in %ss", name, result.duration_s)
    else:
        logger.error("Stream '%s' failed after %ss: %s", name, result.duration_s, result.error)
    return result


def _attempt(conn, config, project_dir: Path, step, *, force: bool, run_id, emit) -> StepOutcome:
    """Run one step once, turning an exception into a failed outcome."""
    emit("step_start", action=step.action, targets=list(step.targets))
    try:
        outcome = _run_step(conn, config, project_dir, step, force=force, run_id=run_id)
    except Exception as exc:
        logger.exception("Stream step %s raised", step.action)
        outcome = StepOutcome(step.action, list(step.targets), ok=False, error=f"{step.action} failed: {exc}")
    emit("step_end", action=step.action, ok=outcome.ok, error=outcome.error)
    return outcome


def _finish_rewind(config, project_dir: Path, run_id: str, result: StreamResult, emit) -> None:
    try:
        from havn.engine.snapshots import RewindConfig, finish_run, run_gc

        finish_run(project_dir, run_id, "success" if result.ok else "failed", result.models_run)
        rw = config.rewind
        deleted = run_gc(project_dir, RewindConfig(
            enabled=rw.enabled,
            retention=rw.retention,
            max_storage=rw.max_storage,
            dedup=rw.dedup,
            exclude=rw.exclude,
        ))
        if deleted:
            emit("gc", deleted=deleted)
    except Exception as exc:
        logger.warning("Rewind bookkeeping failed for stream %s: %s", result.stream, exc)
