"""Warehouse-derived Prometheus series, read from ``_havn`` at scrape time.

The process registry in :mod:`havn.engine.observability` only knows what
happened in this process since it started. Most of what an operator wants
on a dashboard (how long each model takes, whether its last build failed,
how stale it is, how jobs are doing) lives in the warehouse's own metadata
and survives restarts, so it is read from there on every scrape by a
custom collector. Counters here are totals over the run log, which is what
a Prometheus counter means; if the log is pruned they drop, and Prometheus
treats that as a counter reset.

All series carry ``model`` as a label when ``include_models`` is set. That
is one series per model per metric, which is fine for hundreds of models;
switch it off for thousands.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable

logger = logging.getLogger("havn.telemetry")


def _safe_rows(conn: Any, sql: str, params: list | None = None) -> list[tuple]:
    try:
        return conn.execute(sql, params or []).fetchall()
    except Exception as e:
        logger.debug("metrics query skipped (%s): %s", sql.split()[0:6], e)
        return []


def _schema_of(model: str) -> str:
    return model.split(".", 1)[0] if "." in model else ""


class WarehouseCollector:
    """A prometheus_client collector over one warehouse connection."""

    def __init__(self, conn: Any, *, include_models: bool = True, extra: dict | None = None) -> None:
        self.conn = conn
        self.include_models = include_models
        self.extra = extra or {}

    def describe(self) -> Iterable:  # pragma: no cover - not registered globally
        return []

    def collect(self) -> Iterable:
        from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

        conn = self.conn

        # --- runs ---------------------------------------------------------
        runs = CounterMetricFamily(
            "havn_runs", "Entries in the run log, by run type and status.", labels=["run_type", "status"],
        )
        for run_type, status, n in _safe_rows(
            conn, "SELECT run_type, status, count(*) FROM _havn.run_log GROUP BY 1, 2"
        ):
            runs.add_metric([str(run_type), str(status)], float(n))
        yield runs

        last_run = GaugeMetricFamily(
            "havn_last_run_timestamp_seconds", "When the run log last recorded an entry, by run type.",
            labels=["run_type"],
        )
        for run_type, ts in _safe_rows(conn, "SELECT run_type, max(started_at) FROM _havn.run_log GROUP BY 1"):
            if isinstance(ts, datetime):
                last_run.add_metric([str(run_type)], ts.timestamp())
        yield last_run

        # --- per model ----------------------------------------------------
        if self.include_models:
            yield from self._model_families(GaugeMetricFamily, CounterMetricFamily)

        # --- jobs / scheduler ---------------------------------------------
        job_runs = CounterMetricFamily(
            "havn_job_runs", "Orchestration job runs, by job and final status.", labels=["job", "status"],
        )
        for job, status, n in _safe_rows(
            conn, "SELECT job_name, status, count(*) FROM _havn.job_runs GROUP BY 1, 2"
        ):
            job_runs.add_metric([str(job), str(status)], float(n))
        yield job_runs

        job_status = GaugeMetricFamily(
            "havn_job_last_run_success", "1 if the job's latest run succeeded, 0 if not (running counts as 1).",
            labels=["job", "status", "trigger"],
        )
        job_ts = GaugeMetricFamily(
            "havn_job_last_run_timestamp_seconds", "Start of each job's latest run.", labels=["job"],
        )
        job_dur = GaugeMetricFamily(
            "havn_job_last_run_duration_seconds", "Duration of each job's latest finished run.", labels=["job"],
        )
        for job, status, trigger, started, duration in _safe_rows(
            conn,
            """
            SELECT job_name, arg_max(status, started_at), arg_max(trigger, started_at),
                   max(started_at), arg_max(duration_ms, started_at)
            FROM _havn.job_runs GROUP BY job_name
            """,
        ):
            job_status.add_metric(
                [str(job), str(status), str(trigger or "")],
                1.0 if status in ("success", "running") else 0.0,
            )
            if isinstance(started, datetime):
                job_ts.add_metric([str(job)], started.timestamp())
            if duration is not None:
                job_dur.add_metric([str(job)], float(duration) / 1000.0)
        yield job_status
        yield job_ts
        yield job_dur

        # --- extras the caller measured (queue depth, pool size, ...) ------
        for name, (doc, value) in self.extra.items():
            g = GaugeMetricFamily(name, doc)
            g.add_metric([], float(value))
            yield g

    def _model_families(self, Gauge: Any, Counter: Any) -> Iterable:
        conn = self.conn
        state = _safe_rows(
            conn,
            "SELECT model_path, materialized_as, run_duration_ms, row_count, last_run_at FROM _havn.model_state",
        )
        duration = Gauge(
            "havn_model_last_build_duration_seconds", "Duration of each model's latest build.",
            labels=["model", "schema", "materialized"],
        )
        rows = Gauge("havn_model_rows", "Rows in each model after its latest build.", labels=["model", "schema"])
        fresh = Gauge(
            "havn_model_freshness_age_seconds", "Seconds since each model was last built.", labels=["model", "schema"],
        )
        now = datetime.now()
        for model, mat, dur, count, last in state:
            if mat == "ephemeral":
                continue
            labels = [str(model), _schema_of(str(model))]
            duration.add_metric(labels + [str(mat)], float(dur or 0) / 1000.0)
            rows.add_metric(labels, float(count or 0))
            if isinstance(last, datetime):
                fresh.add_metric(labels, max(0.0, (now - last).total_seconds()))
        yield duration
        yield rows
        yield fresh

        status = Gauge(
            "havn_model_last_build_success", "1 if the model's latest transform entry succeeded, else 0.",
            labels=["model", "schema", "status"],
        )
        for model, st in _safe_rows(
            conn,
            "SELECT target, arg_max(status, started_at) FROM _havn.run_log "
            "WHERE run_type = 'transform' AND status <> 'skipped' GROUP BY target",
        ):
            status.add_metric(
                [str(model), _schema_of(str(model)), str(st)],
                1.0 if st in ("success", "inlined") else 0.0,
            )
        yield status

        failures = Counter(
            "havn_model_build_failures", "Failed transform entries in the run log, per model.",
            labels=["model", "schema"],
        )
        for model, n in _safe_rows(
            conn,
            "SELECT target, count(*) FROM _havn.run_log WHERE run_type = 'transform' "
            "AND status = 'error' GROUP BY target",
        ):
            failures.add_metric([str(model), _schema_of(str(model))], float(n))
        yield failures

        assert_fail = Counter(
            "havn_assertion_failures", "Failed assertion checks, per model and severity.",
            labels=["model", "severity"],
        )
        for model, sev, n in _safe_rows(
            conn,
            "SELECT model_path, coalesce(severity, 'error'), count(*) FROM _havn.assertion_results "
            "WHERE NOT passed GROUP BY 1, 2",
        ):
            assert_fail.add_metric([str(model), str(sev)], float(n))
        yield assert_fail

        failing_now = Gauge(
            "havn_assertions_failing", "Assertions whose latest check failed, per model.", labels=["model"],
        )
        for model, n in _safe_rows(
            conn,
            """
            SELECT model_path, count(*) FROM (
                SELECT model_path, expression, arg_max(passed, checked_at) AS passed
                FROM _havn.assertion_results GROUP BY 1, 2
            ) WHERE NOT passed GROUP BY 1
            """,
        ):
            failing_now.add_metric([str(model)], float(n))
        yield failing_now

        peak = Gauge(
            "havn_model_peak_memory_bytes", "Peak buffer memory of each model's latest profiled build.",
            labels=["model"],
        )
        spill = Gauge(
            "havn_model_spill_bytes", "Bytes spilled to the temp directory by each model's latest profiled build.",
            labels=["model"],
        )
        for model, mem, sp in _safe_rows(
            conn,
            "SELECT model_path, arg_max(peak_memory_bytes, finished_at), arg_max(spill_bytes, finished_at) "
            "FROM _havn.model_perf WHERE status = 'success' AND peak_memory_bytes IS NOT NULL GROUP BY 1",
        ):
            peak.add_metric([str(model)], float(mem or 0))
            spill.add_metric([str(model)], float(sp or 0))
        yield peak
        yield spill

        regressions = Counter(
            "havn_perf_regressions", "Build-time regressions detected, per model.", labels=["model"],
        )
        for model, n in _safe_rows(
            conn, "SELECT model_path, count(*) FROM _havn.perf_regressions GROUP BY 1"
        ):
            regressions.add_metric([str(model)], float(n))
        yield regressions


def render_warehouse_metrics(conn: Any, *, include_models: bool = True, extra: dict | None = None) -> bytes:
    """Text exposition of the warehouse collector alone."""
    from prometheus_client import CollectorRegistry, generate_latest

    registry = CollectorRegistry(auto_describe=False)
    registry.register(WarehouseCollector(conn, include_models=include_models, extra=extra))
    return generate_latest(registry)
