"""DuckDB connection management."""

from __future__ import annotations

from pathlib import Path

import duckdb


def connect(
    db_path: str | Path,
    read_only: bool = False,
    memory_limit: str | None = None,
    threads: int | None = None,
    project_dir: str | Path | None = None,
) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection to the given path.

    Args:
        db_path: Path to the DuckDB database file.
        read_only: Open in read-only mode.
        memory_limit: Max memory DuckDB can use (e.g. "4GB", "75%", "512MB").
                      Percentage values are resolved to a fraction of system RAM.
        threads: Max number of DuckDB threads (default: all cores).
        project_dir: If provided, auto-register macros from ``project_dir/macros/``.
    """
    db_path = str(db_path)
    conn = duckdb.connect(db_path, read_only=read_only)
    # Enable progress bar for long-running queries
    conn.execute("SET enable_progress_bar = true")

    if memory_limit:
        resolved = _resolve_memory_limit(memory_limit)
        conn.execute(f"SET memory_limit = '{resolved}'")

    # CPU control: cap threads to configured value, or half of available cores
    import os
    max_threads = threads if (threads is not None and threads > 0) else max(1, os.cpu_count() // 2)
    conn.execute(f"SET threads = {max_threads}")

    # Auto-register macros if a project directory is provided
    if project_dir is not None:
        from havn.engine.macros import register_macros
        register_macros(conn, Path(project_dir))

    return conn


def _resolve_memory_limit(limit: str) -> str:
    """Resolve a memory limit string. Supports percentage of system RAM."""
    limit = limit.strip()
    if limit.endswith("%"):
        try:
            import psutil
            pct = float(limit[:-1]) / 100.0
            total_bytes = psutil.virtual_memory().total
            limit_bytes = int(total_bytes * pct)
            return f"{limit_bytes // (1024 * 1024)}MB"
        except ImportError:
            # psutil not available — fall back to a conservative estimate
            # or try platform-specific methods
            try:
                import os
                if hasattr(os, "sysconf"):
                    pages = os.sysconf("SC_PHYS_PAGES")
                    page_size = os.sysconf("SC_PAGE_SIZE")
                    total_bytes = pages * page_size
                else:
                    # Windows fallback
                    import ctypes
                    kernel32 = ctypes.windll.kernel32
                    c_ulonglong = ctypes.c_ulonglong

                    class MEMORYSTATUSEX(ctypes.Structure):
                        _fields_ = [
                            ("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", c_ulonglong),
                            ("ullAvailPhys", c_ulonglong),
                            ("ullTotalPageFile", c_ulonglong),
                            ("ullAvailPageFile", c_ulonglong),
                            ("ullTotalVirtual", c_ulonglong),
                            ("ullAvailVirtual", c_ulonglong),
                            ("ullAvailExtendedVirtual", c_ulonglong),
                        ]

                    stat = MEMORYSTATUSEX()
                    stat.dwLength = ctypes.sizeof(stat)
                    kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
                    total_bytes = stat.ullTotalPhys

                pct = float(limit[:-1]) / 100.0
                limit_bytes = int(total_bytes * pct)
                return f"{limit_bytes // (1024 * 1024)}MB"
            except Exception:
                # Last resort: assume 8GB system
                pct = float(limit[:-1]) / 100.0
                return f"{int(8192 * pct)}MB"
    return limit


def ensure_schemas(conn: duckdb.DuckDBPyConnection, schemas: list[str]) -> None:
    """Create schemas if they don't exist."""
    for schema in schemas:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")


def ensure_meta_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the internal metadata tables for change tracking, profiling, and assertions."""
    conn.execute("""
        CREATE SCHEMA IF NOT EXISTS _havn
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.model_state (
            model_path   VARCHAR PRIMARY KEY,
            content_hash VARCHAR NOT NULL,
            upstream_hash VARCHAR NOT NULL,
            materialized_as VARCHAR NOT NULL,
            last_run_at  TIMESTAMP DEFAULT current_timestamp,
            run_duration_ms BIGINT DEFAULT 0,
            row_count    BIGINT DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.run_log (
            run_id       VARCHAR DEFAULT gen_random_uuid()::VARCHAR,
            pipeline_run_id VARCHAR,
            run_type     VARCHAR NOT NULL,
            target       VARCHAR NOT NULL,
            status       VARCHAR NOT NULL,
            started_at   TIMESTAMP DEFAULT current_timestamp,
            finished_at  TIMESTAMP,
            duration_ms  BIGINT,
            rows_affected BIGINT DEFAULT 0,
            error        VARCHAR,
            log_output   VARCHAR
        )
    """)
    # Migration for existing databases
    try:
        conn.execute("""
            ALTER TABLE _havn.run_log ADD COLUMN IF NOT EXISTS pipeline_run_id VARCHAR
        """)
    except Exception:
        pass  # column already exists or ALTER not supported
    # Model profiling stats (auto-computed after each build)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.model_profiles (
            model_path       VARCHAR PRIMARY KEY,
            row_count        BIGINT DEFAULT 0,
            column_count     INTEGER DEFAULT 0,
            null_percentages JSON,
            distinct_counts  JSON,
            profiled_at      TIMESTAMP DEFAULT current_timestamp
        )
    """)
    # Data quality assertion results
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.assertion_results (
            id          VARCHAR DEFAULT gen_random_uuid()::VARCHAR,
            model_path  VARCHAR NOT NULL,
            expression  VARCHAR NOT NULL,
            passed      BOOLEAN NOT NULL,
            detail      VARCHAR,
            checked_at  TIMESTAMP DEFAULT current_timestamp
        )
    """)
    # Masking policies
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.masking_policies (
            id               VARCHAR PRIMARY KEY DEFAULT gen_random_uuid()::VARCHAR,
            schema_name      VARCHAR NOT NULL,
            table_name       VARCHAR NOT NULL,
            column_name      VARCHAR NOT NULL,
            method           VARCHAR NOT NULL,
            method_config    JSON,
            condition_column VARCHAR,
            condition_value  VARCHAR,
            exempted_roles   JSON DEFAULT '["admin"]',
            created_at       TIMESTAMP DEFAULT current_timestamp
        )
    """)
    # Audit log
    try:
        conn.execute("CREATE SEQUENCE IF NOT EXISTS _havn.audit_log_seq START 1")
    except Exception:
        pass  # sequence already exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.audit_log (
            id INTEGER PRIMARY KEY DEFAULT nextval('_havn.audit_log_seq'),
            "user" VARCHAR NOT NULL,
            action VARCHAR NOT NULL,
            resource VARCHAR,
            detail VARCHAR,
            ip_address VARCHAR,
            "timestamp" TIMESTAMP DEFAULT current_timestamp
        )
    """)
    # Slow query tracking
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.slow_queries (
            id           VARCHAR DEFAULT gen_random_uuid()::VARCHAR,
            query_text   VARCHAR NOT NULL,
            duration_ms  BIGINT NOT NULL,
            row_count    BIGINT DEFAULT 0,
            executed_at  TIMESTAMP DEFAULT current_timestamp
        )
    """)
    # Alert/notification log
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.alert_log (
            id          VARCHAR DEFAULT gen_random_uuid()::VARCHAR,
            alert_type  VARCHAR NOT NULL,
            channel     VARCHAR NOT NULL,
            target      VARCHAR,
            message     VARCHAR NOT NULL,
            status      VARCHAR NOT NULL,
            sent_at     TIMESTAMP DEFAULT current_timestamp,
            error       VARCHAR
        )
    """)
    # Profile history (append-only for anomaly detection baselines)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.profile_history (
            id              VARCHAR DEFAULT gen_random_uuid()::VARCHAR,
            model_path      VARCHAR NOT NULL,
            row_count       BIGINT DEFAULT 0,
            column_count    INTEGER DEFAULT 0,
            null_percentages JSON,
            distinct_counts  JSON,
            profiled_at     TIMESTAMP DEFAULT current_timestamp
        )
    """)
    # Anomaly detection log
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.anomaly_log (
            id            VARCHAR DEFAULT gen_random_uuid()::VARCHAR,
            model_name    VARCHAR NOT NULL,
            metric        VARCHAR NOT NULL,
            current_value DOUBLE,
            mean_value    DOUBLE,
            stddev_value  DOUBLE,
            z_score       DOUBLE,
            direction     VARCHAR,
            message       VARCHAR,
            detected_at   TIMESTAMP DEFAULT current_timestamp
        )
    """)
    # Dashboard definitions
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.dashboards (
            id           VARCHAR PRIMARY KEY DEFAULT gen_random_uuid()::VARCHAR,
            name         VARCHAR NOT NULL,
            description  VARCHAR DEFAULT '',
            layout       JSON NOT NULL DEFAULT '{"columns":24,"rowHeight":64,"gap":12}',
            filters      JSON DEFAULT '[]',
            settings     JSON DEFAULT '{}',
            created_by   VARCHAR DEFAULT 'anonymous',
            updated_by   VARCHAR DEFAULT 'anonymous',
            created_at   TIMESTAMP DEFAULT current_timestamp,
            updated_at   TIMESTAMP DEFAULT current_timestamp,
            is_template  BOOLEAN DEFAULT FALSE
        )
    """)
    # Dashboard widget instances
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.dashboard_widgets (
            id            VARCHAR PRIMARY KEY DEFAULT gen_random_uuid()::VARCHAR,
            dashboard_id  VARCHAR NOT NULL,
            widget_type   VARCHAR NOT NULL,
            chart_type    VARCHAR,
            title         VARCHAR DEFAULT '',
            sql_query     VARCHAR,
            config        JSON DEFAULT '{}',
            position      JSON NOT NULL,
            filters       JSON DEFAULT '[]',
            cache_ttl     INTEGER DEFAULT 0,
            sort_order    INTEGER DEFAULT 0,
            created_at    TIMESTAMP DEFAULT current_timestamp
        )
    """)
    # Dashboard query result cache
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.dashboard_cache (
            cache_key   VARCHAR PRIMARY KEY,
            result_json JSON NOT NULL,
            row_count   INTEGER DEFAULT 0,
            cached_at   TIMESTAMP DEFAULT current_timestamp,
            expires_at  TIMESTAMP NOT NULL
        )
    """)
    # Orchestration job runs
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.job_runs (
            id              VARCHAR PRIMARY KEY DEFAULT gen_random_uuid()::VARCHAR,
            job_name        VARCHAR NOT NULL,
            job_file        VARCHAR NOT NULL,
            target          VARCHAR NOT NULL,
            status          VARCHAR NOT NULL DEFAULT 'running',
            steps_total     INTEGER NOT NULL DEFAULT 0,
            steps_completed INTEGER NOT NULL DEFAULT 0,
            steps_failed    INTEGER NOT NULL DEFAULT 0,
            steps_skipped   INTEGER NOT NULL DEFAULT 0,
            started_at      TIMESTAMP DEFAULT current_timestamp,
            finished_at     TIMESTAMP,
            duration_ms     BIGINT,
            trigger         VARCHAR DEFAULT 'manual',
            error           VARCHAR,
            step_details    JSON
        )
    """)
    # Migration for databases that predate steps_skipped
    try:
        conn.execute(
            "ALTER TABLE _havn.job_runs ADD COLUMN IF NOT EXISTS steps_skipped INTEGER DEFAULT 0"
        )
    except Exception:
        pass
    # Pull request build records
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.pr_builds (
            id               VARCHAR PRIMARY KEY,
            pr_id            VARCHAR NOT NULL,
            branch_head      VARCHAR,
            status           VARCHAR NOT NULL DEFAULT 'running',
            started_at       TIMESTAMP DEFAULT current_timestamp,
            finished_at      TIMESTAMP,
            duration_ms      BIGINT,
            data_diff        JSON,
            lineage_impact   JSON,
            contract_results JSON,
            error            VARCHAR
        )
    """)


def ensure_circuit_state_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the circuit breaker state table if it doesn't exist."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS _havn")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.circuit_state (
            name            VARCHAR PRIMARY KEY,
            state           VARCHAR NOT NULL,
            failure_count   INTEGER NOT NULL DEFAULT 0,
            last_failure_at DOUBLE,
            opens_at        DOUBLE
        )
    """)


def log_run(
    conn: duckdb.DuckDBPyConnection,
    run_type: str,
    target: str,
    status: str,
    duration_ms: int = 0,
    rows_affected: int = 0,
    error: str | None = None,
    log_output: str | None = None,
    pipeline_run_id: str | None = None,
) -> None:
    """Insert a run log entry."""
    conn.execute(
        """
        INSERT INTO _havn.run_log
            (run_type, target, status, duration_ms, rows_affected, error, log_output, pipeline_run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [run_type, target, status, duration_ms, rows_affected, error, log_output, pipeline_run_id],
    )
