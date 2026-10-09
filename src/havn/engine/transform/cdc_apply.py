"""Applying a stream of change events (CDC) to an incremental model.

A model opts in with ``cdc_op=<op column>, cdc_seq=<sequence column>`` on an
incremental ``merge`` or ``delete+insert`` model with a ``unique_key``. Its
query returns change events rather than rows: several per key, possibly
repeated, possibly out of order. Each run:

1. keeps, per key, only the event with the highest ``cdc_seq`` in the batch
   (ties prefer a delete, so the result does not depend on arrival order);
2. drops events that are not newer than what the target already reflects
   for that key -- the stored row's ``cdc_seq``, or for a hard-deleted key
   the tombstone left by its delete. That is what makes replays, duplicates
   and late older events no-ops;
3. replaces each remaining key: its current row goes, and the event's row
   goes in unless the event is a delete.

``cdc_deletes=hard`` (the default) removes a deleted key's row and records
``(key, cdc_seq)`` in ``_havn.cdc_tombstones__<schema>__<name>``, so an older
insert replayed after the delete cannot bring the row back. ``soft`` keeps
the row with ``_havn_deleted = true`` (and its sequence), which also lets a
downstream live model see the delete; filter ``WHERE NOT _havn_deleted`` in
the models that read it.

An event is a delete when its op starts with ``d``/``D`` (``D``, ``delete``,
Debezium's ``d``); everything else (``I``, ``U``, ``c``, ``r``, ...) is an
upsert. ``cdc_seq`` must sort in change order: an LSN as a number, a
sequence, or an ISO timestamp.
"""

from __future__ import annotations

import logging

import duckdb

from havn.engine.utils import validate_identifier

from .models import SQLModel

logger = logging.getLogger("havn.transform")

DELETED_COLUMN = "_havn_deleted"


def tombstone_table(model: SQLModel) -> str:
    return f"_havn.cdc_tombstones__{model.schema}__{model.name}"


def _is_delete(alias: str, op: str) -> str:
    return (
        f"COALESCE(upper(left(trim(CAST({alias}.\"{op}\" AS VARCHAR)), 1)) = 'D', false)"
    )


def execute_cdc(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    query: str,
    exists: bool,
    actions: list[str] | None = None,
) -> None:
    """Apply the change events ``query`` returns to ``model``'s table."""
    from .execution import (
        _apply_schema_plan,
        _begin_transaction,
        _plan_schema_change,
        _table_columns,
        _temp_columns,
    )

    op, seq = model.cdc_op or "", model.cdc_seq or ""
    validate_identifier(op, "cdc_op column")
    validate_identifier(seq, "cdc_seq column")
    validate_identifier(model.name, "staging table name")
    keys = [k.strip() for k in (model.unique_key or "").split(",") if k.strip()]
    if not keys:
        raise ValueError(f"Model {model.full_name}: CDC apply needs a unique_key")
    for k in keys:
        validate_identifier(k, "unique_key column")
    soft = (model.cdc_deletes or "hard") == "soft"

    raw = f"_havn_cdc_raw_{model.name}"
    staging = f"_havn_staging_{model.name}"
    tomb = tombstone_table(model)
    partition = ", ".join(f'"{k}"' for k in keys)
    is_del_raw = _is_delete("r", op)
    is_del_stg = _is_delete("staging", op)
    key_match = " AND ".join(
        f'target."{k}" IS NOT DISTINCT FROM staging."{k}"' for k in keys
    )
    key_cols = ", ".join(f'"{k}"' for k in keys)

    conn.execute(f"CREATE OR REPLACE TEMP TABLE {raw} AS\n{query}")
    raw_cols = {c.lower() for c, _ in _temp_columns(conn, raw)}
    for col, label in ((op, "cdc_op"), (seq, "cdc_seq"), *((k, "unique_key") for k in keys)):
        if col.lower() not in raw_cols:
            raise ValueError(
                f"Model {model.full_name}: {label} column '{col}' is not in the query's output. "
                "A CDC model must select the key, the operation and the sequence column."
            )
    deleted_expr = f", {is_del_raw} AS {DELETED_COLUMN}" if soft else ""
    conn.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE {staging} AS
        SELECT r.*{deleted_expr} FROM {raw} AS r
        QUALIFY row_number() OVER (
            PARTITION BY {partition}
            ORDER BY r."{seq}" DESC NULLS LAST, {is_del_raw} DESC
        ) = 1
        """
    )
    conn.execute(f"DROP TABLE IF EXISTS {raw}")

    owns_tx = _begin_transaction(conn)
    try:
        if not exists:
            where = "" if soft else f"WHERE NOT {is_del_stg}"
            conn.execute(
                f"CREATE TABLE {model.full_name} AS SELECT * FROM {staging} AS staging {where}"
            )
            if not soft:
                conn.execute(
                    f'CREATE OR REPLACE TABLE {tomb} AS SELECT {key_cols}, "{seq}" '
                    f"FROM {staging} AS staging WHERE {is_del_stg}"
                )
        else:
            plan = _plan_schema_change(
                model,
                _table_columns(conn, model.schema, model.name),
                _temp_columns(conn, staging),
                keys,
            )
            _apply_schema_plan(conn, model, plan)
            if actions is not None:
                actions.extend(plan.describe())
            # 2. Stale or duplicate: the target already holds this key at the
            #    same or a later sequence (soft deletes keep that row too).
            conn.execute(
                f"DELETE FROM {staging} AS staging WHERE EXISTS ("
                f"SELECT 1 FROM {model.full_name} AS target WHERE {key_match} "
                f'AND target."{seq}" IS NOT NULL AND target."{seq}" >= staging."{seq}")'
            )
            if not soft:
                conn.execute(
                    f'CREATE TABLE IF NOT EXISTS {tomb} AS SELECT {key_cols}, "{seq}" '
                    f"FROM {staging} WHERE false"
                )
                conn.execute(
                    f"DELETE FROM {staging} AS staging WHERE EXISTS ("
                    f"SELECT 1 FROM {tomb} AS target WHERE {key_match} "
                    f'AND target."{seq}" >= staging."{seq}")'
                )
            # 3. Replace each surviving key.
            cols = ", ".join(f'"{c}"' for c in plan.columns)
            conn.execute(
                f"DELETE FROM {model.full_name} AS target "
                f"WHERE EXISTS (SELECT 1 FROM {staging} AS staging WHERE {key_match})"
            )
            where = "" if soft else f"WHERE NOT {is_del_stg}"
            conn.execute(
                f"INSERT INTO {model.full_name} ({cols}) "
                f"SELECT {cols} FROM {staging} AS staging {where}"
            )
            if not soft:
                conn.execute(
                    f"DELETE FROM {tomb} AS target "
                    f"WHERE EXISTS (SELECT 1 FROM {staging} AS staging WHERE {key_match})"
                )
                conn.execute(
                    f'INSERT INTO {tomb} ({key_cols}, "{seq}") '
                    f'SELECT {key_cols}, "{seq}" FROM {staging} AS staging WHERE {is_del_stg}'
                )
        conn.execute(f"DROP TABLE IF EXISTS {staging}")
        if owns_tx:
            conn.execute("COMMIT")
    except Exception:
        if owns_tx:
            try:
                conn.execute("ROLLBACK")
            except Exception as rb_err:
                logger.debug("Rollback after failed CDC apply failed: %s", rb_err)
        raise
