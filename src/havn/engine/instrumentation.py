"""One hook around every model build and every run: perf, traces, lineage.

``build_one_model`` (sequential runs, single-model tiers, job steps) and the
parallel worker ``_execute_single_model`` both wrap ``execute_model`` in
:func:`instrument_build`; ``run_transform`` and ``execute_job`` wrap the
whole run in :func:`instrument_run`. Between them they:

- profile the build statement and record a ``_havn.model_perf`` row
  (:mod:`havn.engine.perf`), and at the end of the run check those builds
  for regressions, alert, and apply retention;
- open OpenTelemetry spans, run -> step -> model (:mod:`.telemetry.otel`);
- emit OpenLineage START / COMPLETE / FAIL events (:mod:`.telemetry.openlineage`).

Nothing here may fail a build. Every step is wrapped, and a failure is
logged at debug level and dropped.

Runs are found by ``pipeline_run_id`` in a process-wide registry rather than
through context variables, because parallel workers are pool threads that
do not inherit the submitting thread's context. A model span finds its run
(and the run's current step) through the id it was built with.
"""

from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("havn.perf")


# ---------------------------------------------------------------------------
# Project settings, cached on the files they come from
# ---------------------------------------------------------------------------


@dataclass
class ProjectSettings:
    project_dir: Path | None
    name: str
    performance: Any
    telemetry: Any
    alerts: Any = None
    exposures: list = field(default_factory=list)
    warehouse: str = "warehouse.duckdb"


_settings_cache: dict[str, tuple[tuple, ProjectSettings]] = {}
_settings_lock = threading.Lock()


def _defaults(project_dir: Path | None) -> ProjectSettings:
    from havn.config import PerformanceConfig, TelemetryConfig

    return ProjectSettings(
        project_dir=project_dir,
        name=project_dir.name if project_dir else "havn",
        performance=PerformanceConfig(),
        telemetry=TelemetryConfig(),
    )


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def project_settings(project_dir: Path | str | None) -> ProjectSettings:
    """``performance`` / ``telemetry`` / ``alerts`` for a project, cached.

    Re-read whenever project.yml, .env or .havn-env changes, so turning an
    exporter on takes effect on the next build without a restart. A project
    whose config does not load gets the defaults (perf on, exporters off):
    the build itself will report the config problem.
    """
    if project_dir is None:
        return _defaults(None)
    p = Path(project_dir)
    key = (_mtime(p / "project.yml"), _mtime(p / ".env"), _mtime(p / ".havn-env"))
    cache_key = str(p.resolve())
    with _settings_lock:
        hit = _settings_cache.get(cache_key)
        if hit and hit[0] == key:
            return hit[1]
    try:
        from havn.config import load_project

        cfg = load_project(p)
        db = cfg.database
        warehouse = db.path if db.backend == "duckdb" else f"ducklake:{db.catalog or ''}"
        settings = ProjectSettings(
            project_dir=p,
            name=cfg.name,
            performance=cfg.performance,
            telemetry=cfg.telemetry,
            alerts=cfg.alerts,
            exposures=list(cfg.exposures),
            warehouse=warehouse,
        )
    except Exception as e:
        logger.debug("Using default perf/telemetry settings for %s: %s", p, e)
        settings = _defaults(p)
    with _settings_lock:
        _settings_cache[cache_key] = (key, settings)
    return settings


def clear_settings_cache() -> None:
    with _settings_lock:
        _settings_cache.clear()


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@dataclass
class RunState:
    pipeline_run_id: str
    kind: str
    name: str
    settings: ProjectSettings
    span: Any = None
    step_span: Any = None
    perf_ids: list[str] = field(default_factory=list)
    regressions: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def parent_span(self) -> Any:
        return self.step_span or self.span


_runs: dict[str, RunState] = {}
_runs_lock = threading.Lock()


def active_run(pipeline_run_id: str | None) -> RunState | None:
    if not pipeline_run_id:
        return None
    with _runs_lock:
        return _runs.get(pipeline_run_id)


@contextmanager
def instrument_run(
    conn: Any,
    *,
    project_dir: Path | str | None,
    pipeline_run_id: str | None,
    kind: str = "transform",
    name: str | None = None,
    attributes: dict | None = None,
) -> Iterator[RunState | None]:
    """Wrap one pipeline run. Nested calls for the same run id are no-ops."""
    if not pipeline_run_id or active_run(pipeline_run_id) is not None:
        yield active_run(pipeline_run_id)
        return
    try:
        settings = project_settings(project_dir)
        state = RunState(pipeline_run_id, kind, name or kind, settings)
        from havn.engine.telemetry.otel import get_tracer, start_span

        tracer = get_tracer(settings.telemetry.opentelemetry)
        state.span = start_span(tracer, f"havn {kind}" + (f" {name}" if name else ""), attributes={
            "havn.run.id": pipeline_run_id,
            "havn.run.kind": kind,
            "havn.run.name": name or kind,
            "havn.project": settings.name,
            **(attributes or {}),
        })
    except Exception as e:
        logger.debug("Run instrumentation unavailable: %s", e)
        yield None
        return
    with _runs_lock:
        _runs[pipeline_run_id] = state
    if settings.performance.enabled:
        # Created here, on the run's own connection, before any parallel
        # worker exists: workers racing CREATE TABLE IF NOT EXISTS on a
        # fresh warehouse hit DuckDB's catalog write-write conflict.
        try:
            from havn.engine.perf import ensure_perf_tables

            ensure_perf_tables(conn)
        except Exception as e:
            logger.debug("Could not create perf tables: %s", e)
    error: str | None = None
    try:
        yield state
    except BaseException as e:
        error = str(e) or type(e).__name__
        raise
    finally:
        with _runs_lock:
            _runs.pop(pipeline_run_id, None)
        try:
            _finish_run(conn, state)
        except Exception as e:
            logger.debug("Finishing run instrumentation failed: %s", e)
        from havn.engine.telemetry.otel import end_span, flush

        end_span(state.span, error=error, attributes={
            "havn.run.models_recorded": len(state.perf_ids),
            "havn.run.regressions": len(state.regressions),
        })
        if state.span is not None:
            flush()
        if settings.telemetry.openlineage.enabled:
            from havn.engine.telemetry.openlineage import flush_all

            # A CLI run exits right after this; give queued events a chance.
            flush_all(timeout=float(settings.telemetry.openlineage.timeout_s) + 1.0)


def _finish_run(conn: Any, state: RunState) -> None:
    perf = state.settings.performance
    if not perf.enabled:
        return
    from havn.engine.perf import alert_regressions, detect_run_regressions, prune

    if state.perf_ids:
        regs = detect_run_regressions(conn, list(state.perf_ids), perf)
        state.regressions = regs
        _report_regressions(regs)
        if regs and perf.alert_on_regression:
            try:
                alert_regressions(regs, state.settings.alerts, conn)
            except Exception as e:
                logger.debug("Regression alerting failed: %s", e)
    prune(conn, perf.retention_days, perf.plan_retention)


def _report_regressions(regs: list) -> None:
    if not regs:
        return
    try:
        from rich.console import Console

        console = Console()
        for r in regs:
            console.print(f"         [yellow]slower[/yellow]  {r.message}")
    except Exception:
        for r in regs:
            logger.warning("Performance regression: %s", r.message)


@contextmanager
def instrument_step(
    pipeline_run_id: str | None, name: str, kind: str, attributes: dict | None = None,
) -> Iterator[dict]:
    """A step span (a job step, the transform step of a run) under the run span.

    Yields a dict the caller may fill: ``error`` marks the span failed and
    ``attributes`` are added to it, for steps that report failure as a
    status rather than an exception.
    """
    outcome: dict = {}
    state = active_run(pipeline_run_id)
    if state is None or state.span is None:
        yield outcome
        return
    from havn.engine.telemetry.otel import end_span, get_tracer, start_span

    tracer = get_tracer(state.settings.telemetry.opentelemetry)
    span = start_span(tracer, f"step {name}", parent=state.span, attributes={
        "havn.step.name": name, "havn.step.kind": kind, **(attributes or {}),
    })
    previous = state.step_span
    state.step_span = span
    error = None
    try:
        yield outcome
    except BaseException as e:
        error = str(e) or type(e).__name__
        raise
    finally:
        state.step_span = previous
        end_span(span, error=error or outcome.get("error"), attributes=outcome.get("attributes"))


# ---------------------------------------------------------------------------
# Builds
# ---------------------------------------------------------------------------


class BuildProbe:
    """Handed to the caller of :func:`instrument_build` to report the result."""

    def __init__(self) -> None:
        self.duration_ms: int | None = None
        self.row_count: int | None = None

    def done(self, duration_ms: int, row_count: int) -> None:
        self.duration_ms = int(duration_ms)
        self.row_count = int(row_count)


def _want_plan(conn: Any, perf: Any, model: Any) -> bool:
    mode = perf.capture_plans
    if mode == "false" or model.materialized in ("view", "ephemeral"):
        return False
    if mode == "true":
        return True
    if random.random() < float(perf.sample_rate):
        return True
    # A sampled model is still always profiled while it has few plans, and
    # right after a build that regressed without one, so the next slow run
    # comes with evidence.
    try:
        row = conn.execute(
            """
            SELECT count(*) FILTER (WHERE plan_captured),
                   arg_max(id, finished_at),
                   arg_max(plan_captured, finished_at)
            FROM (
                SELECT id, plan_captured, finished_at FROM _havn.model_perf
                WHERE model_path = ? AND status = 'success'
                ORDER BY finished_at DESC LIMIT 20
            )
            """,
            [model.full_name],
        ).fetchone()
        # Retention clears old plans, so "few" is capped by plan_retention:
        # otherwise a plan_retention of 2 would profile every build.
        wanted = min(3, max(int(perf.plan_retention), 1))
        if not row or (row[0] or 0) < wanted:
            return True
        if row[2]:
            return False
        flagged = conn.execute(
            "SELECT 1 FROM _havn.perf_regressions WHERE perf_id = ? LIMIT 1", [row[1]]
        ).fetchone()
        return flagged is not None
    except Exception:
        return True


def _rows_before_and_in(conn: Any, model: Any) -> tuple[int | None, int | None]:
    before = None
    try:
        row = conn.execute(
            "SELECT row_count FROM _havn.model_state WHERE model_path = ?", [model.full_name]
        ).fetchone()
        before = int(row[0]) if row and row[0] is not None else None
    except Exception:
        pass
    deps = [d.lower() for d in model.depends_on]
    if not deps:
        return before, None
    total = 0
    found = False
    try:
        placeholders = ", ".join("?" for _ in deps)
        sizes = dict(conn.execute(
            f"SELECT lower(schema_name || '.' || table_name), estimated_size FROM duckdb_tables() "
            f"WHERE database_name = current_database() "
            f"AND lower(schema_name || '.' || table_name) IN ({placeholders})",
            deps,
        ).fetchall())
        missing = [d for d in deps if d not in sizes]
        if missing:
            placeholders = ", ".join("?" for _ in missing)
            sizes.update(dict(conn.execute(
                f"SELECT model_path, row_count FROM _havn.model_state WHERE model_path IN ({placeholders})",
                missing,
            ).fetchall()))
        for d in deps:
            if sizes.get(d) is not None:
                total += int(sizes[d])
                found = True
    except Exception as e:
        logger.debug("Could not size upstreams of %s: %s", model.full_name, e)
    return before, (total if found else None)


def _side_cursor(conn: Any) -> Any:
    try:
        from havn.engine.write_queue import cursor_for

        return cursor_for(conn)
    except Exception as e:
        logger.debug("No side cursor for perf bookkeeping: %s", e)
        return None


def _is_uuid(value: str | None) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False


class _Build:
    """State of one instrumented build between start and finish."""

    def __init__(self, conn: Any, model: Any, project_dir: Any, pipeline_run_id: str | None) -> None:
        from havn.engine.perf.capture import BuildCapture
        from havn.engine.perf.store import local_now
        from havn.engine.telemetry.openlineage import get_emitter
        from havn.engine.telemetry.otel import get_tracer, start_span

        self.model = model
        self.pipeline_run_id = pipeline_run_id
        self.run = active_run(pipeline_run_id)
        # Callers that run a transform without naming the project (the API
        # passes only transform/) still get the run's project settings.
        if project_dir is None and self.run is not None:
            self.settings = self.run.settings
        else:
            self.settings = project_settings(project_dir)
        self.probe = BuildProbe()
        perf = self.settings.performance
        self.perf_on = bool(perf.enabled)
        self.capture = None
        self.side = None
        self.rows_before = self.rows_in = None
        if self.perf_on:
            # Perf bookkeeping runs on a cursor of its own: a write or a
            # failed read on the build connection could abort a transaction
            # the caller holds, and a perf row written inside one that later
            # rolls back would vanish with it.
            self.side = _side_cursor(conn)
            side = self.side or conn
            self.capture = BuildCapture(model.full_name, want_plan=_want_plan(side, perf, model))
            self.rows_before, self.rows_in = _rows_before_and_in(side, model)

        tele = self.settings.telemetry
        self.tracer = get_tracer(tele.opentelemetry)
        self.emitter = get_emitter(tele.openlineage, self.settings.project_dir)
        self.started_at = local_now()
        self.t0 = time.perf_counter()

        parent = self.run.parent_span() if self.run else None
        self.span = start_span(self.tracer, f"model {model.full_name}", parent=parent, attributes={
            "havn.model": model.full_name,
            "havn.model.schema": model.schema,
            "havn.model.materialized": model.materialized,
            "havn.model.strategy": model.incremental_strategy if model.materialized == "incremental" else None,
            "havn.run.id": pipeline_run_id,
            "db.system": "duckdb",
        })
        self.ol_run_id = None
        if self.emitter is not None:
            self.ol_run_id = str(uuid.uuid4())
            self._emit_lineage("START", None, None)

    @property
    def active(self) -> bool:
        return self.perf_on or self.span is not None or self.emitter is not None

    # -- lineage ----------------------------------------------------------

    def _namespaces(self) -> tuple[str, str]:
        ol = self.settings.telemetry.openlineage
        job_ns = ol.namespace or f"havn://{self.settings.name}"
        if ol.dataset_namespace:
            return job_ns, ol.dataset_namespace
        wh = self.settings.warehouse
        if self.settings.project_dir is not None and not wh.startswith("ducklake:"):
            p = Path(wh)
            if not p.is_absolute():
                p = self.settings.project_dir / p
            wh = p.resolve().as_posix()
        return job_ns, f"duckdb://{wh}"

    def _emit_lineage(self, event_type: str, conn: Any, error: str | None) -> None:
        from havn.engine.telemetry.openlineage import (
            column_lineage_facet,
            dataset,
            run_event,
            schema_facet,
        )
        import duckdb

        ol = self.settings.telemetry.openlineage
        job_ns, ds_ns = self._namespaces()
        model = self.model
        inputs = [dataset(ds_ns, d) for d in model.depends_on]
        out_facets: dict = {}
        output_facets: dict = {}
        if event_type == "COMPLETE" and conn is not None:
            columns = _columns_of(conn, model.full_name)
            if columns:
                out_facets["schema"] = schema_facet(columns)
            if ol.column_lineage:
                facet = _column_lineage(conn, model, ds_ns, column_lineage_facet)
                if facet:
                    out_facets["columnLineage"] = facet
            if self.probe.row_count is not None and model.materialized != "view":
                from havn.engine.telemetry.openlineage import _facet

                output_facets["outputStatistics"] = _facet("outputStatistics", {"rowCount": self.probe.row_count})
        outputs = [dataset(ds_ns, model.full_name, out_facets or None, output_facets or None)]
        parent = None
        if _is_uuid(self.pipeline_run_id):
            parent = {
                "run_id": self.pipeline_run_id,
                "namespace": job_ns,
                "name": self.run.name if self.run else "pipeline",
            }
        event = run_event(
            event_type,
            run_id=self.ol_run_id,
            job_namespace=job_ns,
            job_name=model.full_name,
            inputs=inputs,
            outputs=outputs,
            parent=parent,
            sql=model.query.strip() if event_type == "START" else None,
            error=error,
            engine_version=duckdb.__version__,
        )
        self.emitter.emit(event)

    # -- finish -----------------------------------------------------------

    def finish(self, conn: Any, error: str | None = None) -> None:
        elapsed_ms = int((time.perf_counter() - self.t0) * 1000)
        duration = self.probe.duration_ms if self.probe.duration_ms is not None else elapsed_ms
        status = "error" if error else "success"
        cap = self.capture
        if self.perf_on:
            try:
                self._record(self.side or conn, status, duration, error)
            except Exception as e:
                logger.debug("Could not record perf for %s: %s", self.model.full_name, e)
            finally:
                if self.side is not None:
                    try:
                        self.side.close()
                    except Exception:
                        pass
                    self.side = None
        if error:
            try:
                from havn.engine.observability import TRANSFORM_DURATION

                TRANSFORM_DURATION.labels(schema=self.model.schema, status="error").observe(elapsed_ms / 1000.0)
            except Exception:
                pass
        if self.span is not None:
            from havn.engine.telemetry.otel import end_span

            attrs: dict = {"havn.duration_ms": duration, "havn.status": status}
            if self.probe.row_count is not None:
                attrs["havn.rows"] = self.probe.row_count
            if cap is not None and cap.statements:
                attrs.update({
                    "havn.rows_scanned": cap.rows_scanned,
                    "havn.peak_memory_bytes": cap.peak_memory_bytes,
                    "havn.spill_bytes": cap.spill_bytes,
                    "havn.plan_captured": cap.plan is not None,
                })
            end_span(self.span, error=error, attributes=attrs)
        if self.emitter is not None:
            try:
                self._emit_lineage("FAIL" if error else "COMPLETE", conn, error)
            except Exception as e:
                logger.debug("Could not emit lineage for %s: %s", self.model.full_name, e)

    def _record(self, conn: Any, status: str, duration: int, error: str | None) -> None:
        from havn.engine.perf.store import BuildRecord, local_now, record_build

        model = self.model
        rec = BuildRecord(
            model=model.full_name,
            pipeline_run_id=self.pipeline_run_id,
            status=status,
            materialized=model.materialized,
            strategy=model.incremental_strategy if model.materialized == "incremental" else None,
            # Wall-clock span of the whole build step, which is what the
            # critical path lines up; duration_ms is the build itself.
            started_at=self.started_at,
            finished_at=local_now(),
            duration_ms=duration,
            rows_before=self.rows_before,
            rows_out=self.probe.row_count if status == "success" else None,
            rows_in=self.rows_in,
            capture=self.capture,
            error=error,
        )
        perf_id = record_build(conn, rec)
        if status != "success":
            return
        if self.run is not None:
            with self.run.lock:
                self.run.perf_ids.append(perf_id)
        else:
            # Not part of an instrumented run: check this build on its own.
            from havn.engine.perf import alert_regressions, detect_run_regressions

            regs = detect_run_regressions(conn, [perf_id], self.settings.performance)
            _report_regressions(regs)
            if regs and self.settings.performance.alert_on_regression:
                alert_regressions(regs, self.settings.alerts, conn)


def _columns_of(conn: Any, full_name: str) -> list[tuple[str, str]]:
    parts = full_name.split(".")
    if len(parts) != 2:
        return []
    try:
        return [
            (r[0], r[1]) for r in conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_catalog = current_database() AND table_schema = ? AND table_name = ? "
                "ORDER BY ordinal_position",
                parts,
            ).fetchall()
        ]
    except Exception:
        return []


def _column_lineage(conn: Any, model: Any, namespace: str, to_facet: Any) -> dict | None:
    from havn.engine.sql_analysis import extract_column_lineage

    catalog: dict[str, list[str]] = {}
    for dep in model.depends_on:
        cols = _columns_of(conn, dep)
        if cols:
            catalog[dep.lower()] = [c for c, _t in cols]
    try:
        lineage = extract_column_lineage(
            model.query, model.depends_on, column_catalog=catalog, ast=model.ast,
        )
    except Exception as e:
        logger.debug("Column lineage failed for %s: %s", model.full_name, e)
        return None
    return to_facet(lineage, namespace)


@contextmanager
def instrument_build(
    conn: Any,
    model: Any,
    *,
    project_dir: Path | str | None = None,
    pipeline_run_id: str | None = None,
) -> Iterator[BuildProbe]:
    """Wrap one ``execute_model`` call. Call ``probe.done(ms, rows)`` on success.

    An exception from the build is recorded (perf row, span error, lineage
    FAIL) and re-raised unchanged.
    """
    from havn.engine.perf.capture import activate

    try:
        build = _Build(conn, model, project_dir, pipeline_run_id)
    except Exception as e:
        logger.debug("Build instrumentation unavailable for %s: %s", getattr(model, "full_name", model), e)
        build = None
    if build is None or not build.active:
        yield BuildProbe()
        return
    with activate(build.capture):
        try:
            yield build.probe
        except BaseException as e:
            try:
                build.finish(conn, error=str(e) or type(e).__name__)
            except Exception as fin_err:
                logger.debug("Build instrumentation failed: %s", fin_err)
            raise
    try:
        build.finish(conn)
    except Exception as e:
        logger.debug("Build instrumentation failed: %s", e)
