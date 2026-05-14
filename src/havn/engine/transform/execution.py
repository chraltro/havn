"""Model execution: incremental strategies, single-model runner."""

from __future__ import annotations

import logging
import time

import duckdb

from havn.engine.database import ensure_meta_table, log_run
from havn.engine.utils import validate_identifier

from .discovery import _compute_upstream_hash, _has_changed, _update_state
from .models import AssertionResult, ModelResult, ProfileResult, SQLModel
from .quality import (
    _save_assertions,
    _save_profile,
    profile_model,
    run_assertions,
)

logger = logging.getLogger("havn.transform")


def _execute_incremental(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
) -> tuple[int, int]:
    """Execute an incremental model.

    Strategies:
        delete+insert (default): Delete matching rows by unique_key, insert new.
        append: Always append, no deduplication.
        merge: True upsert — update existing rows, insert new ones.

    If the target table doesn't exist yet, performs a full load regardless of strategy.
    Handles schema evolution: new columns in the source query are auto-added to the target.
    Supports incremental_filter for filtering the query on incremental runs.
    Supports partition_by for partition-based pruning (deletes affected partitions before insert).
    """
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {model.schema}")
    start = time.perf_counter()

    # Check if target table exists
    exists = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
        [model.schema, model.name],
    ).fetchone()[0] > 0

    # Build the query, applying incremental_filter if this is not the first run.
    # @watermark sugar: when watermark=col is set and incremental_filter is
    # absent, synthesize ``WHERE col > (SELECT COALESCE(MAX(col), '1900-01-01') FROM {this})``
    # so users don't have to write the full filter expression by hand.
    query = model.query
    incremental_filter = model.incremental_filter
    if model.watermark and not incremental_filter:
        wm = model.watermark.strip()
        validate_identifier(wm, "watermark column")
        incremental_filter = (
            f"WHERE {wm} > (SELECT COALESCE(MAX({wm}), '1900-01-01') FROM {{this}})"
        )
    if exists and incremental_filter:
        # Replace {this} with the target table name. Wrap the user query in
        # a subquery so trailing clauses (GROUP BY / ORDER BY / LIMIT / ;)
        # don't produce malformed SQL when the filter is appended.
        filter_clause = incremental_filter.replace("{this}", model.full_name)
        inner = query.rstrip().rstrip(";").rstrip()
        query = f"SELECT * FROM (\n{inner}\n) _havn_src\n{filter_clause}"

    strategy = model.incremental_strategy

    if not exists:
        # First run — full load
        ddl = f"CREATE TABLE {model.full_name} AS\n{query}"
        conn.execute(ddl)
    elif strategy == "append" or not model.unique_key:
        # Append-only: just insert
        conn.execute(f"INSERT INTO {model.full_name}\n{query}")
    else:
        # Strategies that need staging: delete+insert, merge
        keys = [k.strip() for k in model.unique_key.split(",") if k.strip()]
        if not keys:
            raise ValueError(
                f"Model {model.full_name}: incremental strategy '{strategy}' requires a non-empty unique_key"
            )
        for k in keys:
            validate_identifier(k, "unique_key column")
        validate_identifier(model.name, "staging table name")
        staging_name = f"_havn_staging_{model.name}"

        # Create staging table with new data
        conn.execute(f"CREATE OR REPLACE TEMP TABLE {staging_name} AS\n{query}")

        # Handle schema evolution: detect new columns in staging that don't exist in target
        target_cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = ? ",
                [model.schema, model.name],
            ).fetchall()
        }
        staging_cols = conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ? "
            "ORDER BY ordinal_position",
            [staging_name],
        ).fetchall()

        for col_name, col_type in staging_cols:
            if col_name not in target_cols:
                conn.execute(
                    f'ALTER TABLE {model.full_name} ADD COLUMN "{col_name}" {col_type}'
                )

        # Get the final column list from staging for explicit INSERT
        staging_col_names = [r[0] for r in staging_cols]
        staging_select = ", ".join(f'"{c}"' for c in staging_col_names)
        key_cols = ", ".join(f'"{k}"' for k in keys)

        if strategy == "merge":
            # True upsert: UPDATE existing rows, INSERT new ones
            non_key_cols = [c for c in staging_col_names if c not in keys]
            if non_key_cols:
                set_clause = ", ".join(
                    f'"{c}" = staging."{c}"' for c in non_key_cols
                )
                join_cond = " AND ".join(
                    f'target."{k}" = staging."{k}"' for k in keys
                )
                conn.execute(
                    f"UPDATE {model.full_name} AS target SET {set_clause} "
                    f"FROM {staging_name} AS staging WHERE {join_cond}"
                )
            # Insert rows that don't already exist
            not_exists_cond = " AND ".join(
                f'staging."{k}" = target."{k}"' for k in keys
            )
            insert_cols = ", ".join(f'"{c}"' for c in staging_col_names)
            conn.execute(
                f"INSERT INTO {model.full_name} ({insert_cols}) "
                f"SELECT {staging_select} FROM {staging_name} AS staging "
                f"WHERE NOT EXISTS (SELECT 1 FROM {model.full_name} AS target WHERE {not_exists_cond})"
            )
        elif model.partition_by:
            # Partition-based pruning: delete entire affected partitions, then insert
            part_col = model.partition_by.strip()
            # Validate partition column is a safe identifier
            validate_identifier(part_col, "partition_by column")
            conn.execute(
                f'DELETE FROM {model.full_name} '
                f'WHERE "{part_col}" IN (SELECT DISTINCT "{part_col}" FROM {staging_name})'
            )
            insert_cols = ", ".join(f'"{c}"' for c in staging_col_names)
            conn.execute(
                f"INSERT INTO {model.full_name} ({insert_cols}) SELECT {staging_select} FROM {staging_name}"
            )
        else:
            # delete+insert strategy: delete by key, insert new
            conn.execute(
                f"DELETE FROM {model.full_name} "
                f"WHERE ({key_cols}) IN (SELECT {key_cols} FROM {staging_name})"
            )
            insert_cols = ", ".join(f'"{c}"' for c in staging_col_names)
            conn.execute(
                f"INSERT INTO {model.full_name} ({insert_cols}) SELECT {staging_select} FROM {staging_name}"
            )
        conn.execute(f"DROP TABLE IF EXISTS {staging_name}")

    duration_ms = int((time.perf_counter() - start) * 1000)
    result = conn.execute(f"SELECT count(*) FROM {model.full_name}").fetchone()
    row_count = result[0] if result else 0

    return duration_ms, row_count


def _drop_conflicting(
    conn: duckdb.DuckDBPyConnection,
    schema: str,
    name: str,
    target_type: str,
) -> None:
    """Drop an existing object if it conflicts with the desired materialization type."""
    row = conn.execute(
        "SELECT table_type FROM information_schema.tables "
        "WHERE table_schema = ? AND table_name = ?",
        [schema, name],
    ).fetchone()
    if not row:
        return
    existing = row[0]  # 'BASE TABLE' or 'VIEW'
    full_name = f"{schema}.{name}"
    if target_type == "view" and existing == "BASE TABLE":
        conn.execute(f"DROP TABLE {full_name}")
    elif target_type in ("table", "incremental") and existing == "VIEW":
        conn.execute(f"DROP VIEW {full_name}")


def execute_model(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
) -> tuple[int, int]:
    """Execute a single model. Returns (duration_ms, row_count)."""
    from havn.engine.observability import ROWS_PROCESSED, TRANSFORM_DURATION
    from havn.engine.resource_manager import get_resource_manager

    manager = get_resource_manager()
    with manager.acquire_sync("transform", f"model:{model.full_name}", conn=conn):
        manager_task_register_cancel(manager, conn)

        if model.materialized == "incremental":
            duration_ms, row_count = _execute_incremental(conn, model)
        else:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {model.schema}")
            start = time.perf_counter()
            _drop_conflicting(conn, model.schema, model.name, model.materialized)

            if model.materialized == "view":
                ddl = f"CREATE OR REPLACE VIEW {model.full_name} AS\n{model.query}"
            elif model.materialized == "table":
                ddl = f"CREATE OR REPLACE TABLE {model.full_name} AS\n{model.query}"
            else:
                raise ValueError(f"Unknown materialization: {model.materialized}")

            conn.execute(ddl)
            duration_ms = int((time.perf_counter() - start) * 1000)

            row_count = 0
            if model.materialized == "table":
                result = conn.execute(f"SELECT count(*) FROM {model.full_name}").fetchone()
                row_count = result[0] if result else 0

        TRANSFORM_DURATION.labels(schema=model.schema, status="success").observe(
            duration_ms / 1000.0
        )
        ROWS_PROCESSED.labels(category="transform").inc(row_count)
        return duration_ms, row_count


def manager_task_register_cancel(manager, conn: duckdb.DuckDBPyConnection) -> None:
    """Wire the current resource-manager task to ``conn.interrupt()`` for cancel."""
    from havn.engine.resource_manager import current_task

    task = current_task()
    if task is None:
        return
    manager.register_cancel(task.task_id, conn.interrupt)


def _execute_single_model(
    db_path: str,
    model: SQLModel,
    force: bool,
    model_map: dict[str, SQLModel],
    db_config: object | None = None,
    project_dir: object | None = None,
    pipeline_run_id: str | None = None,
) -> tuple[str, ModelResult]:
    """Execute a single model in its own connection (for parallel execution).

    If ``db_config`` is provided, the connection is opened through the
    warehouse backend (supports DuckLake). Otherwise falls back to the
    plain ``db_path`` open for the DuckDB backend.

    Returns (model_full_name, ModelResult).
    """
    if db_config is not None:
        from havn.engine.database import open_warehouse
        conn = open_warehouse(db_config, project_dir)
    else:
        conn = duckdb.connect(db_path)
    try:
        ensure_meta_table(conn)
        model.upstream_hash = _compute_upstream_hash(model, model_map)
        changed = force or _has_changed(conn, model)

        if not changed:
            try:
                log_run(conn, "transform", model.full_name, "skipped", 0, 0, pipeline_run_id=pipeline_run_id)
            except Exception:
                pass
            return model.full_name, ModelResult(status="skipped")

        duration_ms, row_count = execute_model(conn, model)
        _update_state(conn, model, duration_ms, row_count)
        log_run(conn, "transform", model.full_name, "success", duration_ms, row_count, pipeline_run_id=pipeline_run_id)

        # Run assertions
        assertion_results: list[AssertionResult] = []
        if model.assertions:
            assertion_results = run_assertions(conn, model)
            _save_assertions(conn, model, assertion_results)

        # Auto-profile
        profile: ProfileResult | None = None
        if model.materialized in ("table", "incremental"):
            profile = profile_model(conn, model)
            _save_profile(conn, model, profile)

        return model.full_name, ModelResult(
            status="built",
            duration_ms=duration_ms,
            row_count=row_count,
            assertions=assertion_results,
            profile=profile,
        )

    except Exception as e:
        try:
            log_run(conn, "transform", model.full_name, "error", error=str(e), pipeline_run_id=pipeline_run_id)
        except Exception as e2:
            logger.debug("Failed to log run error: %s", e2)
        return model.full_name, ModelResult(status="error", error=str(e))
    finally:
        conn.close()
