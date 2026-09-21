"""Model execution: incremental strategies, single-model runner."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

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


def _begin_transaction(conn: duckdb.DuckDBPyConnection) -> bool:
    """Open a transaction on ``conn``, returning True when we opened it.

    DuckDB refuses a nested ``BEGIN TRANSACTION``. Connections are shared with
    the parallel runner (one connection per worker) and with the DuckLake
    ``_update_state`` path, which opens its own transaction, so an outer
    transaction may already be active. In that case we join it instead of
    starting a second one, and the caller must not commit or roll back work
    that it does not own.
    """
    try:
        conn.execute("BEGIN TRANSACTION")
        return True
    except duckdb.TransactionException as e:
        logger.debug("Transaction already active, joining the outer one: %s", e)
        return False


class SchemaChangeError(ValueError):
    """An incremental model's columns no longer match its target table.

    Raised before any write to the target, so the table still holds exactly
    the rows it held before the run. The message names the column, both
    types where a type is involved, and the ``on_schema_change`` policy that
    would accept the change.
    """


@dataclass
class _SchemaPlan:
    """What an incremental run must do to the target before it writes.

    ``columns`` is the column list the INSERT/UPDATE uses, in staging order.
    Under ``ignore`` it is the intersection of staging and target; under every
    other policy it is every staging column, because the ALTERs below make the
    target match.
    """

    add: list[tuple[str, str]] = field(default_factory=list)
    drop: list[str] = field(default_factory=list)
    retype: list[tuple[str, str, str]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)

    def describe(self) -> list[str]:
        """One human-readable line per action, for the log and the run log."""
        out = [f"added column {n} {t}" for n, t in self.add]
        out += [f"retyped column {n} from {old} to {new}" for n, old, new in self.retype]
        out += [f"dropped column {n}" for n in self.drop]
        return out


# Type families and relative widths, used to decide whether writing a staging
# column into a differently typed target column is lossless. Imported from
# the schema sentinel, which owns the same table for upstream schema diffs;
# that module pulls in nothing but the standard library and duckdb, so the
# import is free of side effects.
def _type_family_tables() -> tuple[dict[str, str], dict[str, int]]:
    from havn.engine.sentinel import _TYPE_GROUPS, _TYPE_WIDTH

    return _TYPE_GROUPS, _TYPE_WIDTH


def _base_type(sql_type: str) -> str:
    """``DECIMAL(18,3)`` -> ``DECIMAL``, for family and width lookups."""
    return sql_type.split("(")[0].strip().upper()


def _normalize_type(sql_type: str) -> str:
    """Compare types on a single normalized spelling."""
    return " ".join(sql_type.upper().split())


def _is_lossless_cast(from_type: str, to_type: str) -> bool:
    """Can every value of ``from_type`` be stored in ``to_type`` unchanged?

    True only for a widening step inside one type family: INTEGER into BIGINT
    keeps every value, DOUBLE into INTEGER does not (20.5 becomes 21), and
    INTEGER into VARCHAR crosses families so it is not treated as safe even
    though DuckDB would accept it.
    """
    groups, widths = _type_family_tables()
    src, dst = _base_type(from_type), _base_type(to_type)
    if src == dst:
        return True
    if groups.get(src) != groups.get(dst) or groups.get(src) is None:
        return False
    src_w, dst_w = widths.get(src), widths.get(dst)
    if src_w is None or dst_w is None:
        return False
    return dst_w >= src_w


def _plan_schema_change(
    model: SQLModel,
    target_cols: list[tuple[str, str]],
    staging_cols: list[tuple[str, str]],
    keys: list[str],
) -> _SchemaPlan:
    """Diff staging against target in both directions and apply the policy.

    Pure: it reads nothing and writes nothing, so a policy that refuses the
    change raises before the caller has touched the target table.
    """
    policy = model.on_schema_change
    target_by_name = {n.lower(): (n, t) for n, t in target_cols}
    staging_by_name = {n.lower(): (n, t) for n, t in staging_cols}
    key_set = {k.lower() for k in keys}

    added = [(n, t) for n, t in staging_cols if n.lower() not in target_by_name]
    removed = [n for n, _ in target_cols if n.lower() not in staging_by_name]
    retyped: list[tuple[str, str, str]] = []
    for name, staging_type in staging_cols:
        entry = target_by_name.get(name.lower())
        if entry is None:
            continue
        target_type = entry[1]
        if _normalize_type(target_type) != _normalize_type(staging_type):
            retyped.append((name, target_type, staging_type))

    plan = _SchemaPlan(columns=[n for n, _ in staging_cols])
    if not added and not removed and not retyped:
        return plan

    where = f"Model {model.full_name}"
    hint_tail = (
        " Set @config on_schema_change=... to choose a policy "
        "(append_new_columns, ignore, fail, sync_all_columns)."
    )

    if policy == "fail":
        parts = []
        if added:
            parts.append("added " + ", ".join(f"{n} {t}" for n, t in added))
        if removed:
            parts.append("removed " + ", ".join(removed))
        if retyped:
            parts.append(
                "retyped "
                + ", ".join(f"{n} from {old} to {new}" for n, old, new in retyped)
            )
        raise SchemaChangeError(
            f"{where}: on_schema_change=fail and the query's columns no longer "
            f"match the target table ({'; '.join(parts)}). "
            "Nothing was written. Rebuild the model with `havn transform --force`, "
            "or pick a policy that accepts the change."
        )

    if policy == "ignore":
        for name, target_type, staging_type in retyped:
            if not _is_lossless_cast(staging_type, target_type):
                raise SchemaChangeError(
                    f"{where}: column '{name}' is {target_type} in the target "
                    f"table but {staging_type} in the query, and writing "
                    f"{staging_type} into {target_type} is not lossless "
                    f"(a DOUBLE 20.5 written into an INTEGER column becomes 21). "
                    "on_schema_change=ignore keeps the target type, so nothing "
                    "was written. Use on_schema_change=sync_all_columns to alter "
                    "the column instead, or rebuild with `havn transform --force`."
                )
        # No ALTER at all: write only the columns both sides agree on.
        plan.columns = [n for n, _ in staging_cols if n.lower() in target_by_name]
        missing_keys = [k for k in keys if k.lower() not in {c.lower() for c in plan.columns}]
        if missing_keys:
            raise SchemaChangeError(
                f"{where}: unique_key column(s) {', '.join(missing_keys)} are not "
                "in both the query and the target table, so rows cannot be matched. "
                "Add the column back to the query, or rebuild with "
                "`havn transform --force`."
            )
        return plan

    if policy == "sync_all_columns":
        for name in removed:
            if name.lower() in key_set:
                raise SchemaChangeError(
                    f"{where}: column '{name}' is the unique_key but is missing "
                    "from the query, so rows could not be matched after the drop. "
                    "Add the column back to the query, or change unique_key."
                )
        plan.add = added
        plan.drop = removed
        plan.retype = retyped
        return plan

    # append_new_columns — the historical behavior for added columns, and a
    # hard stop for the two changes that used to corrupt data silently.
    if removed:
        raise SchemaChangeError(
            f"{where}: column(s) {', '.join(removed)} exist in the target table "
            "but not in the query. havn refuses the write because the column "
            "would diverge without a word: rows written from now on get NULL "
            "while every older row keeps its stale value. Use "
            "on_schema_change=sync_all_columns to drop the column, "
            "on_schema_change=ignore to leave it alone and write only the "
            "shared columns, or rebuild with `havn transform --force`."
            + hint_tail
        )
    if retyped:
        name, target_type, staging_type = retyped[0]
        raise SchemaChangeError(
            f"{where}: column '{name}' is {target_type} in the target table but "
            f"{staging_type} in the query. havn refuses the write because the "
            "values are cast into the old type without a word: a DOUBLE 20.5 "
            "written into an INTEGER column becomes 21. Use "
            "on_schema_change=sync_all_columns to alter the column to "
            f"{staging_type}, on_schema_change=ignore to keep {target_type} when "
            "the cast is a lossless widening, or rebuild with "
            "`havn transform --force`."
            + hint_tail
        )
    plan.add = added
    return plan


def _apply_schema_plan(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    plan: _SchemaPlan,
) -> None:
    """Run the plan's ALTERs. Called inside the incremental transaction."""
    for name, col_type in plan.add:
        conn.execute(f'ALTER TABLE {model.full_name} ADD COLUMN "{name}" {col_type}')
    for name, old_type, new_type in plan.retype:
        try:
            conn.execute(
                f'ALTER TABLE {model.full_name} ALTER COLUMN "{name}" TYPE {new_type}'
            )
        except Exception as e:
            raise SchemaChangeError(
                f"Model {model.full_name}: could not change column '{name}' from "
                f"{old_type} to {new_type}. DuckDB refuses to alter a column that "
                f"a constraint or an index depends on ({e}). Drop the index or "
                "constraint, or rebuild the model with `havn transform --force`."
            ) from e
    for name in plan.drop:
        try:
            conn.execute(f'ALTER TABLE {model.full_name} DROP COLUMN "{name}"')
        except Exception as e:
            raise SchemaChangeError(
                f"Model {model.full_name}: could not drop column '{name}'. DuckDB "
                f"refuses to drop a column that a constraint or an index depends "
                f"on ({e}). Drop the index or constraint, or rebuild the model "
                "with `havn transform --force`."
            ) from e
    for line in plan.describe():
        logger.info("%s: %s", model.full_name, line)


def resolve_query(
    model: SQLModel,
    model_map: dict[str, SQLModel] | None,
) -> str:
    """The SQL to build ``model`` from, with ephemeral upstreams inlined.

    Without a ``model_map`` there is nothing to resolve against, so the model's
    own query is returned and a project with no ephemeral models never pays for
    a sqlglot round trip.
    """
    if not model_map:
        return model.query
    from .inline import inline_ephemeral

    return inline_ephemeral(model, model_map)


def _execute_incremental(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    actions: list[str] | None = None,
    model_map: dict[str, SQLModel] | None = None,
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

    # A model switched from `view` to `incremental` leaves a VIEW behind at the
    # target name. Drop it first, then probe for a BASE TABLE specifically --
    # information_schema.tables counts views too, so without the type filter the
    # probe reported "exists" and every run failed with
    # "Binder Error: Can only delete from base table".
    _drop_conflicting(conn, model.schema, model.name, "incremental")
    exists = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = ? AND table_name = ? AND table_type = 'BASE TABLE'",
        [model.schema, model.name],
    ).fetchone()[0] > 0

    # Build the query, applying incremental_filter if this is not the first run.
    # @watermark sugar: when watermark=col is set and incremental_filter is
    # absent, synthesize a ``WHERE col <cmp> (SELECT MAX(col) FROM {this})`` filter
    # so users don't have to write it by hand.
    #
    # The comparison operator depends on the strategy. With a unique_key
    # (delete+insert / merge), we use ``>=`` so rows that tie the current max
    # watermark — common with second-granularity timestamps or batch IDs that
    # arrive late within the same tick — are re-read and upserted rather than
    # silently lost forever; the dedup on unique_key absorbs the re-read. For
    # append-only loads there is no dedup, so we keep strict ``>`` to avoid
    # inserting duplicates of the boundary rows.
    query = resolve_query(model, model_map)
    incremental_filter = model.incremental_filter
    if model.watermark and not incremental_filter:
        wm = model.watermark.strip()
        validate_identifier(wm, "watermark column")
        dedups = bool(model.unique_key) and model.incremental_strategy != "append"
        cmp = ">=" if dedups else ">"
        # NULL-safe on an empty target (MAX is NULL -> load everything) and
        # type-agnostic — unlike a hardcoded '1900-01-01' sentinel, this works
        # for integer/bigint watermark columns as well as dates/timestamps.
        incremental_filter = (
            f"WHERE (SELECT MAX({wm}) FROM {{this}}) IS NULL "
            f"OR {wm} {cmp} (SELECT MAX({wm}) FROM {{this}})"
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

        # Schema evolution: diff staging against target on name AND type, in
        # both directions, and resolve the difference with the model's
        # on_schema_change policy. The diff is computed here, before the
        # transaction below opens and before a single byte of the target is
        # touched, so a policy that refuses the change leaves the table with
        # exactly the rows it had.
        target_cols = [
            (r[0], r[1])
            for r in conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = ? "
                "ORDER BY ordinal_position",
                [model.schema, model.name],
            ).fetchall()
        ]
        staging_cols = conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            # table_catalog = 'temp' isolates the TEMP staging table so a
            # same-named non-temp table in another schema can't pollute the
            # column list (which drives ALTER ADD COLUMN and the INSERT list).
            "WHERE table_name = ? AND table_catalog = 'temp' "
            "ORDER BY ordinal_position",
            [staging_name],
        ).fetchall()

        plan = _plan_schema_change(model, target_cols, staging_cols, keys)

        # The column list the INSERT/UPDATE writes. Under `ignore` this is the
        # intersection of staging and target; otherwise every staging column,
        # because the plan's ALTERs make the target match.
        staging_col_names = list(plan.columns)
        staging_select = ", ".join(f'"{c}"' for c in staging_col_names)
        # NULL-safe key comparison. Plain `=` (and `(a,b) IN (SELECT ...)`)
        # evaluates to NULL rather than TRUE when a key column is NULL, so rows
        # with a NULL key were never matched by the DELETE and duplicated on
        # every single run. IS NOT DISTINCT FROM treats NULL = NULL as a match.
        key_match = " AND ".join(
            f'target."{k}" IS NOT DISTINCT FROM staging."{k}"' for k in keys
        )

        # Everything from here on is a dependent write: the schema-evolution
        # ALTERs, the DELETE/UPDATE that clears the rows being replaced, and
        # the INSERT that puts them back. Run under one transaction so a
        # failure part-way through cannot leave the target mangled. Without
        # it, an INSERT that failed to bind (e.g. a column retyped to VARCHAR
        # holding non-numeric values) landed after an already-committed
        # DELETE and the model lost every row it was supposed to keep.
        owns_tx = _begin_transaction(conn)
        try:
            _apply_schema_plan(conn, model, plan)
            if actions is not None:
                actions.extend(plan.describe())

            if strategy == "merge":
                # True upsert: UPDATE existing rows, INSERT new ones
                non_key_cols = [c for c in staging_col_names if c not in keys]
                if non_key_cols:
                    set_clause = ", ".join(
                        f'"{c}" = staging."{c}"' for c in non_key_cols
                    )
                    conn.execute(
                        f"UPDATE {model.full_name} AS target SET {set_clause} "
                        f"FROM {staging_name} AS staging WHERE {key_match}"
                    )
                # Insert rows that don't already exist
                insert_cols = ", ".join(f'"{c}"' for c in staging_col_names)
                conn.execute(
                    f"INSERT INTO {model.full_name} ({insert_cols}) "
                    f"SELECT {staging_select} FROM {staging_name} AS staging "
                    f"WHERE NOT EXISTS (SELECT 1 FROM {model.full_name} AS target WHERE {key_match})"
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
                    f"DELETE FROM {model.full_name} AS target "
                    f"WHERE EXISTS (SELECT 1 FROM {staging_name} AS staging WHERE {key_match})"
                )
                insert_cols = ", ".join(f'"{c}"' for c in staging_col_names)
                conn.execute(
                    f"INSERT INTO {model.full_name} ({insert_cols}) SELECT {staging_select} FROM {staging_name}"
                )
            conn.execute(f"DROP TABLE IF EXISTS {staging_name}")
            if owns_tx:
                conn.execute("COMMIT")
        except Exception:
            if owns_tx:
                try:
                    conn.execute("ROLLBACK")
                except Exception as rb_err:
                    logger.debug(
                        "Rollback after failed incremental write failed: %s", rb_err
                    )
            raise

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
    if target_type == "ephemeral":
        # The model used to be materialized and is now inlined into its
        # consumers. Whatever sits at schema.name is an orphan: nothing will
        # refresh it again, and leaving it would let a stale copy answer
        # queries that look like they hit the model.
        conn.execute(
            f"DROP VIEW {full_name}" if existing == "VIEW" else f"DROP TABLE {full_name}"
        )
    elif target_type == "view" and existing == "BASE TABLE":
        conn.execute(f"DROP TABLE {full_name}")
    elif target_type in ("table", "incremental") and existing == "VIEW":
        conn.execute(f"DROP VIEW {full_name}")


def execute_model(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    actions: list[str] | None = None,
    model_map: dict[str, SQLModel] | None = None,
) -> tuple[int, int]:
    """Execute a single model. Returns (duration_ms, row_count).

    ``actions`` collects human-readable schema-evolution lines ("added column
    region VARCHAR") when the caller wants them for the run log. Passing None
    discards them.

    ``model_map`` is the full project, keyed by full name. It is what lets an
    ephemeral upstream be inlined into this model's query; without it the query
    is built as written.
    """
    from havn.engine.observability import ROWS_PROCESSED, TRANSFORM_DURATION
    from havn.engine.resource_manager import get_resource_manager

    if model.materialized == "ephemeral":
        # Nothing to build: consumers carry the query as a CTE. The only work
        # is clearing whatever an earlier materialization left behind.
        _drop_conflicting(conn, model.schema, model.name, "ephemeral")
        return 0, 0

    manager = get_resource_manager()
    with manager.acquire_sync("transform", f"model:{model.full_name}", conn=conn):
        manager_task_register_cancel(manager, conn)

        if model.materialized == "incremental":
            duration_ms, row_count = _execute_incremental(
                conn, model, actions, model_map
            )
        else:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {model.schema}")
            start = time.perf_counter()
            _drop_conflicting(conn, model.schema, model.name, model.materialized)
            query = resolve_query(model, model_map)

            if model.materialized == "view":
                ddl = f"CREATE OR REPLACE VIEW {model.full_name} AS\n{query}"
            elif model.materialized == "table":
                ddl = f"CREATE OR REPLACE TABLE {model.full_name} AS\n{query}"
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


def _record_ephemeral(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    pipeline_run_id: str | None = None,
) -> ModelResult:
    """Settle an ephemeral model: drop any orphan, record it, report "inlined".

    The status is deliberately not "skipped". A skip means change detection
    found nothing to do and the table on disk is current; an ephemeral model
    has no table at all, and saying "skipped" would read as the former.

    A ``model_state`` row is written on every run, with ``materialized_as``
    "ephemeral" and a row count of zero. Without it ``_has_changed`` would
    answer True for this model forever, and the row would also be the last
    thing a reader saw from when the model was still a table.
    """
    execute_model(conn, model)
    _update_state(conn, model, 0, 0)
    try:
        log_run(
            conn, "transform", model.full_name, "inlined", 0, 0,
            pipeline_run_id=pipeline_run_id,
        )
    except Exception as e:
        logger.debug("Failed to log ephemeral model %s: %s", model.full_name, e)
    return ModelResult(status="inlined")


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

        if model.materialized == "ephemeral":
            return model.full_name, _record_ephemeral(
                conn, model, pipeline_run_id
            )

        changed = force or _has_changed(conn, model)

        if not changed:
            try:
                log_run(conn, "transform", model.full_name, "skipped", 0, 0, pipeline_run_id=pipeline_run_id)
            except Exception:
                pass
            return model.full_name, ModelResult(status="skipped")

        schema_changes: list[str] = []
        duration_ms, row_count = execute_model(
            conn, model, schema_changes, model_map
        )
        _update_state(conn, model, duration_ms, row_count)
        log_run(
            conn, "transform", model.full_name, "success", duration_ms, row_count,
            log_output="; ".join(schema_changes) or None,
            pipeline_run_id=pipeline_run_id,
        )

        # Run assertions (and the synthesised @grain check, if any). A
        # severity=error failure must surface as "assertion_failed" so the
        # orchestrator blocks descendants — same contract as the sequential path.
        assertion_results: list[AssertionResult] = []
        if model.assertions or model.grain:
            assertion_results = run_assertions(conn, model)
            _save_assertions(conn, model, assertion_results)
            failed_error = [
                ar for ar in assertion_results
                if not ar.passed and (ar.severity or "error") == "error"
            ]
            if failed_error:
                return model.full_name, ModelResult(
                    status="assertion_failed",
                    duration_ms=duration_ms,
                    row_count=row_count,
                    assertions=assertion_results,
                    schema_changes=schema_changes,
                )

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
            schema_changes=schema_changes,
        )

    except Exception as e:
        try:
            log_run(conn, "transform", model.full_name, "error", error=str(e), pipeline_run_id=pipeline_run_id)
        except Exception as e2:
            logger.debug("Failed to log run error: %s", e2)
        return model.full_name, ModelResult(status="error", error=str(e))
    finally:
        conn.close()
