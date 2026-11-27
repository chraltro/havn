"""DuckDB connection management.

Two entry points into the warehouse:

- :func:`open_warehouse` takes a ``DatabaseConfig`` + ``project_dir`` and
  returns a connection via the backend abstraction (DuckDB or DuckLake).
  This is the standard path for CLI commands and server dependencies.

- :func:`connect` opens a specific ``.duckdb`` file directly
  (no backend abstraction). Use this when you need to open a path that
  isn't the configured warehouse — PR worktree warehouses, ad-hoc
  migration destinations, test helpers.
"""

from __future__ import annotations

from pathlib import Path

import duckdb


def finish_pending_result(conn) -> None:
    """Run any open ``execute()`` result on ``conn`` to its end.

    DuckDB 2.0 streams ``execute()`` results: ``conn.execute(q).fetchone()``
    leaves the result open until a fetch returns no rows, and while it is
    open ``create_function``, ``register``, ``sql()`` and friends raise
    ``ResourceInUseException``. A following ``execute()`` silently replaces
    the open result, which is why most code never notices. Call this before
    any non-``execute`` call on a connection whose last ``execute()`` might
    not have been read to the end.

    DuckDB 1.x materialises every result, so the method does not exist and
    this is a no-op. Safe to call on an idle connection, a cursor, or any
    object that is not a DuckDB connection at all.
    """
    complete = getattr(conn, "complete", None)
    if complete is None:
        return
    try:
        complete()
    except Exception:
        # An error the open result would have raised on its next fetch
        # (e.g. a cast failure in row 10,000 of a SELECT) belongs to the
        # caller who abandoned it, not to the next unrelated call.
        abort = getattr(conn, "abort", None)
        if abort is not None:
            try:
                abort()
            except Exception:
                pass


def result_to_arrow(cur):
    """The rest of ``cur``'s ``execute()`` result as a pyarrow Table.

    ``to_arrow_table()`` on both 1.5 (where ``fetch_arrow_table()`` is the
    deprecated spelling) and 2.0. On 2.0 it copies the rows and warns that
    ``con.sql(query)`` would not; the statement has already run (it may have
    been a write), so the copy is the only way and the warning is silenced.
    """
    to_arrow = getattr(cur, "to_arrow_table", None)
    if to_arrow is None:
        return cur.fetch_arrow_table()
    import warnings

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Arrow output on a DB-API result")
        return to_arrow()


def open_warehouse(
    config,
    project_dir: str | Path | None = None,
    *,
    read_only: bool = False,
) -> duckdb.DuckDBPyConnection:
    """Open a connection to the configured warehouse via the backend factory.

    Args:
        config: ProjectConfig or DatabaseConfig.
        project_dir: Used to locate macros/ and resolve relative paths.
        read_only: Open in read-only mode.
    """
    from havn.config import DatabaseConfig, ProjectConfig
    from havn.engine.backends import create_backend

    if isinstance(config, ProjectConfig):
        db_config = config.database
        if project_dir is None:
            project_dir = config.project_dir
        if config.branch.active and not read_only:
            from havn.engine.branches import write_branch_record

            write_branch_record(config)
    elif isinstance(config, DatabaseConfig):
        db_config = config
    else:
        raise TypeError(
            f"open_warehouse expects ProjectConfig or DatabaseConfig, got {type(config).__name__}"
        )

    backend = create_backend(db_config, project_dir=project_dir)
    return backend.connect(read_only=read_only)


def connect(
    db_path: str | Path,
    read_only: bool = False,
    memory_limit: str | None = None,
    threads: int | None = None,
    project_dir: str | Path | None = None,
) -> duckdb.DuckDBPyConnection:
    """Open a specific DuckDB file directly (no backend abstraction).

    Use when the target isn't the configured warehouse — PR worktree
    warehouses, migration destinations, test fixtures, backup verification.

    Args:
        db_path: Path to the DuckDB database file.
        read_only: Open in read-only mode.
        memory_limit: Max memory (e.g. "4GB", "75%", "512MB").
        threads: Max DuckDB threads (default: half of CPU cores).
        project_dir: If provided, auto-register macros from ``project_dir/macros/``.
    """
    import os

    db_path = str(db_path)
    conn = duckdb.connect(db_path, read_only=read_only)
    if not read_only and db_path != ":memory:":
        # The file may have been replaced since this process last checked
        # its metadata schema (a restored backup); check again on next use.
        from havn.engine.migrations import forget_database

        forget_database(db_path)
    if _progress_bar_enabled():
        conn.execute("SET enable_progress_bar = true")
    else:
        try:
            conn.execute("SET enable_progress_bar = false")
        except Exception:
            pass

    if memory_limit:
        resolved = _resolve_memory_limit(memory_limit)
        conn.execute(f"SET memory_limit = '{resolved}'")

    cpu = os.cpu_count() or 2
    max_threads = threads if (threads is not None and threads > 0) else max(1, cpu // 2)
    conn.execute(f"SET threads = {max_threads}")

    if project_dir is not None:
        from havn.engine.macros import register_macros
        register_macros(conn, Path(project_dir))

    return conn


def _progress_bar_enabled() -> bool:
    """Decide whether DuckDB's progress bar is safe to enable.

    DuckDB renders progress with carriage-return updates that flood non-TTY
    stdout (CI logs, piped output) with thousands of lines. Honour an explicit
    `HAVN_PROGRESS=1`/`HAVN_PROGRESS=0` override; otherwise enable only when
    stdout is an interactive TTY and TERM is not 'dumb'.
    """
    import os
    import sys

    override = os.environ.get("HAVN_PROGRESS", "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    if override in ("0", "false", "no", "off"):
        return False
    if not sys.stdout.isatty():
        return False
    if os.environ.get("TERM", "").lower() in ("", "dumb"):
        return False
    return True


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


def _is_ducklake_connection(conn: duckdb.DuckDBPyConnection) -> bool:
    """True if this connection has a DuckLake catalog attached.

    Answered from the catalog first. Calling ``ducklake_snapshots()`` on a
    plain DuckDB connection raises, and inside an open transaction DuckDB 1.5
    aborts the caller's whole transaction on that error, so every statement
    after the check failed ("Current transaction is aborted"). That broke
    every live-model advance on a plain warehouse.
    """
    try:
        row = conn.execute(
            "SELECT type FROM duckdb_databases() WHERE database_name = 'warehouse'"
        ).fetchone()
    except Exception:
        return False
    if row is None:
        return False
    kind = str(row[0]).lower()
    if kind == "ducklake":
        return True
    if kind == "duckdb":
        return False
    # Any other attach type: ask DuckLake itself, as before.
    try:
        conn.execute("SELECT 1 FROM ducklake_snapshots('warehouse') LIMIT 1").fetchone()
        return True
    except Exception:
        return False


def _strip_pk(ddl: str, is_ducklake: bool) -> str:
    """Rewrite a DDL string for DuckLake compatibility.

    DuckLake 1.0's actual rejections (verified empirically):
    - PRIMARY KEY / UNIQUE / CHECK constraints
    - CREATE SEQUENCE (and therefore ``DEFAULT nextval(...)``)

    Function-call DEFAULTs (``current_timestamp``, ``gen_random_uuid()``,
    ``now()``) and boolean DEFAULTs (``TRUE``/``FALSE``) are accepted.
    Stripping these unnecessarily was breaking every INSERT site that
    relied on the default — e.g. ``_havn.run_log.started_at`` ended up
    NULL on DuckLake, which made the History panel look empty.

    havn doesn't rely on PK/UNIQUE enforcement at the engine level; the
    metadata writers use explicit DELETE+INSERT or unique-by-construction
    keys.
    """
    if not is_ducklake:
        return ddl
    import re as _re
    # Remove PRIMARY KEY (inline column-level and trailing-clause forms).
    ddl = _re.sub(r'\s+PRIMARY\s+KEY', '', ddl, flags=_re.IGNORECASE)
    # Remove UNIQUE (inline). The table-level UNIQUE(x,y) form is rare in havn.
    ddl = _re.sub(r'\s+UNIQUE\b', '', ddl, flags=_re.IGNORECASE)
    # Remove CHECK (...) constraints (inline column-level).
    ddl = _re.sub(r'\s+CHECK\s*\([^)]*\)', '', ddl, flags=_re.IGNORECASE)
    # DuckLake doesn't support sequences, so nextval(...) defaults can't work.
    ddl = _re.sub(r"\s+DEFAULT\s+nextval\([^)]*\)", "", ddl, flags=_re.IGNORECASE)
    return ddl


def ensure_meta_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Make sure the ``_havn`` metadata schema is at the current version.

    Kept for the many callers that predate the migration system: the tables
    themselves are defined once, in :mod:`havn.engine.migrations`. Cheap after
    the first call in a process (a single catalog lookup), and a no-op on a
    read-only connection, which cannot run DDL; readers there handle an
    absent table themselves.
    """
    from havn.engine.migrations import ensure_migrated

    ensure_migrated(conn)


def ensure_circuit_state_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Kept for compatibility; ``_havn.circuit_state`` is created by the migrations."""
    ensure_meta_table(conn)


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
    """Insert a run log entry.

    Supplies ``run_id`` and ``started_at`` explicitly so DuckLake projects
    created before the DDL was relaxed (which had the function-call defaults
    stripped) still produce non-NULL timestamps and the History panel
    populates correctly.

    The entry is written when the step finishes, so ``started_at`` is now
    minus ``duration_ms``. Stamping it with now made a slow step look as if
    it started after the steps that followed it, and a pipeline's span came
    out wrong on Home.
    """
    conn.execute(
        """
        INSERT INTO _havn.run_log
            (run_id, started_at, run_type, target, status, duration_ms, rows_affected, error, log_output, pipeline_run_id)
        VALUES (gen_random_uuid()::VARCHAR, current_timestamp - to_milliseconds(CAST(? AS BIGINT)),
                ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [max(int(duration_ms or 0), 0), run_type, target, status, duration_ms, rows_affected, error,
         log_output, pipeline_run_id],
    )
