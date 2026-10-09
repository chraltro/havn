"""Where the performance advisor keeps what it measured: ``_havn.model_perf``.

One row per model build: wall time, rows before / in / out, what DuckDB's
profiler reported (peak memory, bytes spilled, rows scanned, CPU time) and,
when the build was profiled, a compact plan with real operator timings plus
its five most expensive operators.

Retention is two-level. Rows older than ``performance.retention_days`` go,
and only the newest ``performance.plan_retention`` builds of each model keep
their plan; older rows keep their numbers, which is what the trend and the
regression baseline read, and lose the few KB of plan.

No primary keys and Python-generated ids, so the same DDL works on DuckLake.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import duckdb

from .capture import BuildCapture, top_operators

logger = logging.getLogger("havn.perf")


def ensure_perf_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the advisor's tables if they are missing. Cheap to repeat."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS _havn")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.model_perf (
            id                VARCHAR NOT NULL,
            model_path        VARCHAR NOT NULL,
            pipeline_run_id   VARCHAR,
            status            VARCHAR NOT NULL,
            materialized      VARCHAR,
            strategy          VARCHAR,
            full_refresh      BOOLEAN,
            started_at        TIMESTAMP,
            finished_at       TIMESTAMP,
            duration_ms       BIGINT,
            rows_before       BIGINT,
            rows_out          BIGINT,
            rows_produced     BIGINT,
            rows_in           BIGINT,
            rows_scanned      BIGINT,
            peak_memory_bytes BIGINT,
            spill_bytes       BIGINT,
            bytes_read        BIGINT,
            bytes_written     BIGINT,
            cpu_time_ms       DOUBLE,
            plan_captured     BOOLEAN DEFAULT FALSE,
            plan              JSON,
            top_operators     JSON,
            error             VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.perf_regressions (
            id               VARCHAR NOT NULL,
            model_path       VARCHAR NOT NULL,
            pipeline_run_id  VARCHAR,
            perf_id          VARCHAR,
            baseline_perf_id VARCHAR,
            detected_at      TIMESTAMP,
            metric           VARCHAR,
            current_value    DOUBLE,
            baseline_median  DOUBLE,
            baseline_mad     DOUBLE,
            robust_z         DOUBLE,
            ratio            DOUBLE,
            normalized       BOOLEAN,
            message          VARCHAR,
            plan_diff        JSON,
            alerted          BOOLEAN DEFAULT FALSE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.perf_advice_state (
            model_path VARCHAR NOT NULL,
            rule       VARCHAR NOT NULL,
            status     VARCHAR NOT NULL,
            until      TIMESTAMP,
            note       VARCHAR,
            updated_by VARCHAR,
            updated_at TIMESTAMP
        )
    """)


def local_now() -> datetime:
    """Naive local time: what DuckDB stores when ``current_timestamp`` lands in a
    TIMESTAMP column (the session time zone defaults to the machine's), so
    these rows line up with ``run_log`` and with ``current_timestamp - ...``."""
    return datetime.now()


@dataclass
class BuildRecord:
    """Everything one build contributes to ``model_perf``."""

    model: str
    pipeline_run_id: str | None
    status: str
    materialized: str | None
    strategy: str | None
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    rows_before: int | None = None
    rows_out: int | None = None
    rows_in: int | None = None
    capture: BuildCapture | None = None
    error: str | None = None
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid.uuid4())


def record_build(conn: duckdb.DuckDBPyConnection, rec: BuildRecord) -> str:
    """Write one build's row and return its id.

    The tables are created only when the insert finds them missing: a
    ``CREATE TABLE IF NOT EXISTS`` per build from parallel workers is a
    catalog write that can conflict with another worker's.
    """
    try:
        return _insert_build(conn, rec)
    except duckdb.CatalogException:
        ensure_perf_tables(conn)
        return _insert_build(conn, rec)


def _insert_build(conn: duckdb.DuckDBPyConnection, rec: BuildRecord) -> str:
    cap = rec.capture
    plan = cap.plan if cap else None
    full_refresh = cap.full_refresh if cap else None
    if full_refresh is None and rec.materialized == "table":
        full_refresh = True
    conn.execute(
        """
        INSERT INTO _havn.model_perf (
            id, model_path, pipeline_run_id, status, materialized, strategy,
            full_refresh, started_at, finished_at, duration_ms, rows_before,
            rows_out, rows_produced, rows_in, rows_scanned, peak_memory_bytes,
            spill_bytes, bytes_read, bytes_written, cpu_time_ms, plan_captured,
            plan, top_operators, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            rec.id, rec.model, rec.pipeline_run_id, rec.status, rec.materialized,
            rec.strategy, full_refresh, rec.started_at, rec.finished_at,
            int(rec.duration_ms), rec.rows_before, rec.rows_out,
            cap.rows_produced if cap else None,
            rec.rows_in,
            cap.rows_scanned if cap and cap.statements else None,
            cap.peak_memory_bytes if cap and cap.statements else None,
            cap.spill_bytes if cap and cap.statements else None,
            cap.bytes_read if cap and cap.statements else None,
            cap.bytes_written if cap and cap.statements else None,
            round(cap.cpu_time_s * 1000, 3) if cap and cap.statements else None,
            plan is not None,
            json.dumps(plan) if plan is not None else None,
            json.dumps(top_operators(plan)) if plan is not None else None,
            (rec.error or "")[:2000] or None,
        ],
    )
    return rec.id


def prune(conn: duckdb.DuckDBPyConnection, retention_days: int, plan_retention: int) -> None:
    """Apply retention: drop old rows, then old plans beyond the newest N per model."""
    ensure_perf_tables(conn)
    try:
        if retention_days and retention_days > 0:
            conn.execute(
                "DELETE FROM _havn.model_perf WHERE finished_at < current_timestamp - to_days(CAST(? AS INTEGER))",
                [int(retention_days)],
            )
            conn.execute(
                "DELETE FROM _havn.perf_regressions WHERE detected_at < current_timestamp - to_days(CAST(? AS INTEGER))",
                [int(retention_days)],
            )
        conn.execute(
            """
            UPDATE _havn.model_perf SET plan = NULL, plan_captured = FALSE
            WHERE id IN (
                SELECT id FROM (
                    SELECT id, row_number() OVER (
                        PARTITION BY model_path ORDER BY finished_at DESC
                    ) AS rn
                    FROM _havn.model_perf WHERE plan IS NOT NULL
                ) WHERE rn > ?
            )
            """,
            [max(int(plan_retention), 0)],
        )
    except duckdb.Error as e:
        logger.debug("Perf retention skipped: %s", e)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

_LIGHT_COLUMNS = (
    "id, model_path, pipeline_run_id, status, materialized, strategy, full_refresh, "
    "started_at, finished_at, duration_ms, rows_before, rows_out, rows_produced, "
    "rows_in, rows_scanned, peak_memory_bytes, spill_bytes, bytes_read, bytes_written, "
    "cpu_time_ms, plan_captured, top_operators, error"
)


def _row_to_dict(cols: list[str], row: tuple) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for c, v in zip(cols, row):
        if c in ("plan", "top_operators", "plan_diff") and isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        elif isinstance(v, datetime):
            v = v.isoformat()
        out[c] = v
    return out


def _fetch(conn: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> list[dict]:
    """Rows as dicts. A warehouse that never recorded a build has no perf
    tables, and readers (often on read-only connections, which cannot
    create them) answer "nothing yet" rather than fail."""
    try:
        cur = conn.execute(sql, params or [])
    except duckdb.CatalogException as e:
        logger.debug("Perf tables not there yet: %s", e)
        return []
    cols = [d[0] for d in cur.description]
    return [_row_to_dict(cols, r) for r in cur.fetchall()]


def model_history(
    conn: duckdb.DuckDBPyConnection,
    model: str,
    limit: int = 50,
    *,
    with_plans: bool = False,
    status: str | None = "success",
) -> list[dict]:
    """A model's builds, newest first."""
    cols = _LIGHT_COLUMNS + (", plan" if with_plans else "")
    where = "model_path = ?"
    params: list = [model.lower()]
    if status:
        where += " AND status = ?"
        params.append(status)
    params.append(int(limit))
    return _fetch(
        conn,
        f"SELECT {cols} FROM _havn.model_perf WHERE {where} ORDER BY finished_at DESC LIMIT ?",
        params,
    )


def get_build(conn: duckdb.DuckDBPyConnection, perf_id: str) -> dict | None:
    rows = _fetch(conn, f"SELECT {_LIGHT_COLUMNS}, plan FROM _havn.model_perf WHERE id = ?", [perf_id])
    return rows[0] if rows else None


def latest_builds(conn: duckdb.DuckDBPyConnection) -> dict[str, dict]:
    """Each model's newest successful build, with its plan (the newest one that has one)."""
    rows = _fetch(
        conn,
        f"""
        SELECT {_LIGHT_COLUMNS} FROM (
            SELECT *, row_number() OVER (PARTITION BY model_path ORDER BY finished_at DESC) AS rn
            FROM _havn.model_perf WHERE status = 'success'
        ) WHERE rn = 1
        """,
    )
    out = {r["model_path"]: r for r in rows}
    plans = _fetch(
        conn,
        """
        SELECT model_path, id AS plan_perf_id, plan FROM (
            SELECT model_path, id, plan,
                   row_number() OVER (PARTITION BY model_path ORDER BY finished_at DESC) AS rn
            FROM _havn.model_perf WHERE status = 'success' AND plan IS NOT NULL
        ) WHERE rn = 1
        """,
    )
    for p in plans:
        if p["model_path"] in out:
            out[p["model_path"]]["plan"] = p["plan"]
            out[p["model_path"]]["plan_perf_id"] = p["plan_perf_id"]
    return out


def slowest_models(conn: duckdb.DuckDBPyConnection, days: int = 7, limit: int = 20) -> list[dict]:
    """Models ranked by median build time over the last ``days``."""
    return _fetch(
        conn,
        """
        SELECT model_path,
               count(*) AS builds,
               CAST(median(duration_ms) AS BIGINT) AS median_ms,
               max(duration_ms) AS max_ms,
               CAST(sum(duration_ms) AS BIGINT) AS total_ms,
               arg_max(duration_ms, finished_at) AS last_ms,
               arg_max(rows_out, finished_at) AS last_rows,
               max(peak_memory_bytes) AS peak_memory_bytes,
               max(spill_bytes) AS spill_bytes,
               max(finished_at) AS last_built_at,
               arg_max(materialized, finished_at) AS materialized
        FROM _havn.model_perf
        WHERE status = 'success'
          AND finished_at >= current_timestamp - to_days(CAST(? AS INTEGER))
        GROUP BY model_path
        ORDER BY median_ms DESC, model_path
        LIMIT ?
        """,
        [int(days), int(limit)],
    )


def trend(
    conn: duckdb.DuckDBPyConnection,
    models: list[str],
    days: int = 30,
    per_model: int = 60,
) -> dict[str, list[dict]]:
    """Duration over time for each model in ``models``, oldest first."""
    if not models:
        return {}
    placeholders = ", ".join("?" for _ in models)
    rows = _fetch(
        conn,
        f"""
        SELECT model_path, id, pipeline_run_id, finished_at, duration_ms, rows_out,
               plan_captured
        FROM (
            SELECT *, row_number() OVER (PARTITION BY model_path ORDER BY finished_at DESC) AS rn
            FROM _havn.model_perf
            WHERE status = 'success' AND model_path IN ({placeholders})
              AND finished_at >= current_timestamp - to_days(CAST(? AS INTEGER))
        ) WHERE rn <= ?
        ORDER BY model_path, finished_at
        """,
        [m.lower() for m in models] + [int(days), int(per_model)],
    )
    out: dict[str, list[dict]] = {m.lower(): [] for m in models}
    for r in rows:
        out.setdefault(r["model_path"], []).append(r)
    return out


def run_builds(conn: duckdb.DuckDBPyConnection, pipeline_run_id: str) -> list[dict]:
    """Every build recorded for one pipeline run, in finishing order."""
    return _fetch(
        conn,
        f"SELECT {_LIGHT_COLUMNS} FROM _havn.model_perf WHERE pipeline_run_id = ? ORDER BY finished_at",
        [pipeline_run_id],
    )


def recent_runs(conn: duckdb.DuckDBPyConnection, limit: int = 20) -> list[dict]:
    """Pipeline runs that recorded builds, newest first."""
    return _fetch(
        conn,
        """
        SELECT pipeline_run_id,
               min(started_at) AS started_at,
               max(finished_at) AS finished_at,
               count(*) AS builds,
               CAST(sum(duration_ms) AS BIGINT) AS busy_ms,
               CAST(date_diff('millisecond', min(started_at), max(finished_at)) AS BIGINT) AS wall_ms,
               count(*) FILTER (WHERE status <> 'success') AS failures
        FROM _havn.model_perf
        WHERE pipeline_run_id IS NOT NULL
        GROUP BY pipeline_run_id
        ORDER BY max(finished_at) DESC
        LIMIT ?
        """,
        [int(limit)],
    )
