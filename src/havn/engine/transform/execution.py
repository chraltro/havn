"""Model execution: incremental strategies, single-model runner."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

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

    # append_new_columns: the historical behavior for added columns, and a
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


# ---------------------------------------------------------------------------
# Microbatch incremental strategy
# ---------------------------------------------------------------------------


class MicrobatchError(ValueError):
    """A microbatch model cannot be turned into a sequence of windows.

    Raised before the first window runs, so a misconfigured model costs a run
    and never a half-processed target.
    """


BATCH_SIZES = ("hour", "day", "month", "year")

_TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


@dataclass(frozen=True)
class BatchRange:
    """An explicit event-time range to process, from the CLI or the API.

    Either end may be None: an open start falls back to the model's ``begin``
    and an open end to now, which is what ``--event-time-start`` alone means.
    """

    start: datetime | None = None
    end: datetime | None = None

    @property
    def is_set(self) -> bool:
        return self.start is not None or self.end is not None


def parse_event_time(value: str | datetime, label: str = "timestamp") -> datetime:
    """Parse a date or timestamp written in config or on the command line.

    Everything is UTC and naive: a microbatch window is a range on the event
    time column, and mixing an aware boundary with a naive column is a
    comparison DuckDB refuses rather than one it guesses at.
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    text = str(value).strip().rstrip("Z")
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise MicrobatchError(
        f"Could not read {label} {value!r}. Write it as 2024-01-01 or "
        "2024-01-01 06:00:00 (UTC)."
    )


def utc_now() -> datetime:
    """The naive UTC wall clock every window boundary is measured against.

    One function so a test can move the clock, and so "now" means the same
    thing to the window computation, the resume cursor and the state writer
    within a single run.
    """
    return datetime.utcnow()


def truncate_to_batch(ts: datetime, batch_size: str) -> datetime:
    """Round ``ts`` down to the start of its window."""
    if batch_size == "hour":
        return ts.replace(minute=0, second=0, microsecond=0)
    if batch_size == "day":
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    if batch_size == "month":
        return ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if batch_size == "year":
        return ts.replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    raise MicrobatchError(
        f"Unknown batch_size '{batch_size}'. Supported: {', '.join(BATCH_SIZES)}."
    )


def shift_batch(ts: datetime, batch_size: str, count: int) -> datetime:
    """Move ``ts`` ``count`` whole windows forward, or back when negative.

    Month and year steps are done on the calendar rather than with a fixed
    number of days, which is the whole reason this is not a timedelta: a
    month window that started on 1 January has to land on 1 February, not on
    31 January.
    """
    if batch_size == "hour":
        return ts + timedelta(hours=count)
    if batch_size == "day":
        return ts + timedelta(days=count)
    if batch_size == "month":
        total = ts.year * 12 + (ts.month - 1) + count
        return ts.replace(year=total // 12, month=total % 12 + 1)
    if batch_size == "year":
        return ts.replace(year=ts.year + count)
    raise MicrobatchError(
        f"Unknown batch_size '{batch_size}'. Supported: {', '.join(BATCH_SIZES)}."
    )


def compute_batch_windows(
    begin: datetime,
    end: datetime,
    batch_size: str,
) -> list[tuple[datetime, datetime]]:
    """The half-open windows covering ``begin`` to ``end``, on UTC boundaries.

    ``begin`` is rounded down to its window, so a model that begins at
    2024-01-15 with ``batch_size=month`` starts at 2024-01-01 and the first
    window is a whole month. The window holding ``end`` is included even
    though it is not over yet, because the alternative is that data written
    in the last hour is never picked up until the hour turns.
    """
    windows: list[tuple[datetime, datetime]] = []
    cursor = truncate_to_batch(begin, batch_size)
    guard = 0
    while cursor < end:
        nxt = shift_batch(cursor, batch_size, 1)
        windows.append((cursor, nxt))
        cursor = nxt
        guard += 1
        if guard > 100_000:
            raise MicrobatchError(
                "Refusing to build more than 100,000 batch windows. Check "
                "begin= and batch_size=; a begin of 1970 with batch_size=hour "
                "is over 480,000 windows."
            )
    return windows


def substitute_batch_window(sql: str, start: datetime, end: datetime) -> str:
    """Replace ``{start}`` and ``{end}`` with typed timestamp literals.

    Typed rather than bare strings so the model can write
    ``WHERE event_at >= {start}`` without a cast and still bind.
    """
    return sql.replace(
        "{start}", f"TIMESTAMP '{start:%Y-%m-%d %H:%M:%S}'"
    ).replace("{end}", f"TIMESTAMP '{end:%Y-%m-%d %H:%M:%S}'")


def _microbatch_config(model: SQLModel) -> tuple[str, str, datetime, int]:
    """Validate and unpack the model's microbatch settings."""
    if not model.event_time:
        raise MicrobatchError(
            f"Model {model.full_name}: incremental_strategy=microbatch needs "
            "event_time=<column>, the column each window is cut on."
        )
    validate_identifier(model.event_time, "event_time column")
    if not model.batch_size:
        raise MicrobatchError(
            f"Model {model.full_name}: incremental_strategy=microbatch needs "
            f"batch_size=<{'|'.join(BATCH_SIZES)}>."
        )
    if model.batch_size not in BATCH_SIZES:
        raise MicrobatchError(
            f"Model {model.full_name}: Unknown batch_size "
            f"'{model.batch_size}'. Supported: {', '.join(BATCH_SIZES)}."
        )
    if not model.begin:
        raise MicrobatchError(
            f"Model {model.full_name}: incremental_strategy=microbatch needs "
            "begin=<date>, the first window to process. There is no safe "
            "default: guessing it wrong either misses history or scans years "
            "of empty windows."
        )
    begin = parse_event_time(model.begin, f"{model.full_name} begin=")
    if model.lookback < 0:
        raise MicrobatchError(
            f"Model {model.full_name}: lookback must be zero or more, not "
            f"{model.lookback}."
        )
    return model.event_time, model.batch_size, begin, model.lookback


def _batch_resume_start(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    begin: datetime,
    batch_size: str,
    lookback: int,
) -> datetime:
    """Where the next ordinary run starts.

    Only *closed* windows count: one whose end is still in the future has not
    finished happening, so however it was recorded it has to be processed
    again. Among those, the first that is not ``done`` if there is one,
    otherwise the window after the last one recorded; then ``lookback``
    windows further back, so rows that arrived late for an already-processed
    window are picked up. Never earlier than ``begin``.

    Reading open windows as finished is what poisoned the cursor. A backfill
    with ``--event-time-end`` in the future recorded those windows as done
    with zero rows, ``max(window_start)`` then landed past today, and every
    ordinary run afterwards computed an empty window list and ingested
    nothing until the wall clock caught up. Runs no longer write a future
    window at all, and this filter also un-poisons a warehouse that already
    holds some.
    """
    try:
        rows = conn.execute(
            "SELECT window_start, window_end, status FROM _havn.batch_state "
            "WHERE model_path = ?",
            [model.full_name],
        ).fetchall()
    except Exception as e:
        logger.debug("No batch_state for %s yet: %s", model.full_name, e)
        return begin
    now = utc_now()
    closed = [(r[0], r[2]) for r in rows if r[1] is not None and r[1] <= now]
    if not closed:
        return begin
    pending = [start for start, status in closed if status != "done"]
    if pending:
        resume = min(pending)
    else:
        resume = shift_batch(max(start for start, _ in closed), batch_size, 1)
    resume = shift_batch(truncate_to_batch(resume, batch_size), batch_size, -lookback)
    return max(resume, truncate_to_batch(begin, batch_size))


def _record_batch(
    conn: duckdb.DuckDBPyConnection,
    model_path: str,
    window: tuple[datetime, datetime],
    status: str,
    rows: int,
    run_id: str | None,
) -> None:
    """Replace this window's row in ``_havn.batch_state``.

    A window that has not started yet is never recorded. Writing one would
    put the resume cursor past today, and the model would then sit idle until
    the wall clock reached it.
    """
    if window[0] > utc_now():
        logger.debug(
            "%s: refusing to record batch window %s, which is in the future",
            model_path, window[0],
        )
        return
    conn.execute(
        "DELETE FROM _havn.batch_state WHERE model_path = ? AND window_start = ?",
        [model_path, window[0]],
    )
    conn.execute(
        "INSERT INTO _havn.batch_state "
        '(model_path, window_start, window_end, status, "rows", run_id, finished_at) '
        "VALUES (?, ?, ?, ?, ?, ?, current_timestamp)",
        [model_path, window[0], window[1], status, rows, run_id],
    )


def _execute_microbatch(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    actions: list[str] | None = None,
    model_map: dict[str, SQLModel] | None = None,
    batch_range: BatchRange | None = None,
    force: bool = False,
    run_id: str | None = None,
    query_rewriter: Callable[[str], str] | None = None,
) -> tuple[int, int]:
    """Run a microbatch model one event-time window at a time.

    Each window is its own transaction: the query runs into a staging table,
    the window's existing rows are deleted from the target on ``event_time``,
    the staging rows are inserted, and the window is recorded as ``done``.
    A failure at window 17 of 30 therefore leaves windows 1 to 16 committed
    and recorded, and the next run resumes at 17 rather than starting over.

    The model does its own filtering on ``{start}`` and ``{end}``, exactly as
    in dbt. havn does not add a WHERE clause: an event-time predicate pushed
    into the wrong place in a query with a GROUP BY or a window function
    changes the answer, and only the model's author knows where it belongs.
    """
    event_time, batch_size, begin, lookback = _microbatch_config(model)
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {model.schema}")
    start_clock = time.perf_counter()

    _drop_conflicting(conn, model.schema, model.name, "incremental")
    exists = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_catalog = current_database() "
        "AND table_schema = ? AND table_name = ? AND table_type = 'BASE TABLE'",
        [model.schema, model.name],
    ).fetchone()[0] > 0

    now = utc_now()
    if batch_range is not None and batch_range.is_set:
        window_start = batch_range.start or begin
        window_end = batch_range.end or now
    elif force or not exists:
        # --force reprocesses the model from `begin`. Recorded state is left
        # in place and overwritten window by window, so an interrupted force
        # still resumes sensibly.
        window_start, window_end = begin, now
    else:
        window_start = _batch_resume_start(conn, model, begin, batch_size, lookback)
        window_end = now

    # An end past now is clamped rather than refused: asking for "everything
    # up to next Friday" is a reasonable thing to type, and the windows after
    # now hold nothing anyway. Running them would record empty windows as
    # done and leave the resume cursor sitting in the future, so the model
    # would ingest nothing until the wall clock caught up.
    if window_end > now:
        logger.warning(
            "%s: event-time end %s is in the future; processing up to %s "
            "instead. Windows after now are not run and not recorded.",
            model.full_name, window_end, now,
        )
        window_end = now

    windows = compute_batch_windows(window_start, window_end, batch_size)
    if not windows:
        logger.info(
            "%s: no microbatch windows between %s and %s",
            model.full_name, window_start, window_end,
        )
        duration_ms = int((time.perf_counter() - start_clock) * 1000)
        row_count = 0
        if exists:
            row_count = conn.execute(
                f"SELECT count(*) FROM {model.full_name}"
            ).fetchone()[0]
        return duration_ms, row_count

    validate_identifier(model.name, "staging table name")
    staging = f"_havn_batch_{model.name}"
    base_query = resolve_query(model, model_map, query_rewriter)
    logger.info(
        "%s: %d microbatch window(s) of one %s, %s to %s",
        model.full_name, len(windows), batch_size, windows[0][0], windows[-1][1],
    )

    # One transaction per window is the whole contract: window 17 failing
    # leaves 1 to 16 committed and recorded, and the next run resumes at 17.
    # An outer transaction the caller already opened would silently take that
    # away, committing everything at once or nothing at all, while the
    # failure message still promised the earlier windows were safe. No caller
    # does this today; refusing keeps it that way.
    if not _begin_transaction(conn):
        raise MicrobatchError(
            f"Model {model.full_name}: a transaction is already open on this "
            "connection. A microbatch model gives each window its own "
            "transaction so a failure part way through keeps the windows "
            "before it, which an outer transaction would undo. Run it "
            "outside the transaction."
        )
    conn.execute("ROLLBACK")

    for index, window in enumerate(windows, start=1):
        w_start, w_end = window
        query = substitute_batch_window(base_query, w_start, w_end)
        owns_tx = _begin_transaction(conn)
        try:
            if not exists:
                conn.execute(f"CREATE TABLE {model.full_name} AS\n{query}")
                exists = True
                written = conn.execute(
                    f"SELECT count(*) FROM {model.full_name}"
                ).fetchone()[0]
            else:
                conn.execute(
                    f"CREATE OR REPLACE TEMP TABLE {staging} AS\n{query}"
                )
                plan = _plan_schema_change(
                    model,
                    _table_columns(conn, model.schema, model.name),
                    _temp_columns(conn, staging),
                    [],
                )
                _apply_schema_plan(conn, model, plan)
                if actions is not None and index == 1:
                    actions.extend(plan.describe())
                conn.execute(
                    f"DELETE FROM {model.full_name} "
                    f'WHERE "{event_time}" >= ? AND "{event_time}" < ?',
                    [w_start, w_end],
                )
                cols = ", ".join(f'"{c}"' for c in plan.columns)
                conn.execute(
                    f"INSERT INTO {model.full_name} ({cols}) "
                    f"SELECT {cols} FROM {staging}"
                )
                written = conn.execute(
                    f"SELECT count(*) FROM {staging}"
                ).fetchone()[0]
                conn.execute(f"DROP TABLE IF EXISTS {staging}")
            _record_batch(conn, model.full_name, window, "done", written, run_id)
            if owns_tx:
                conn.execute("COMMIT")
        except Exception as e:
            if owns_tx:
                try:
                    conn.execute("ROLLBACK")
                except Exception as rb_err:
                    logger.debug("Rollback of batch window failed: %s", rb_err)
            # Recorded outside the rolled-back transaction, so the next run
            # can see which window to come back to.
            try:
                _record_batch(conn, model.full_name, window, "failed", 0, run_id)
            except Exception as rec_err:
                logger.debug("Could not record failed batch window: %s", rec_err)
            raise MicrobatchError(
                f"Model {model.full_name}: microbatch window {index} of "
                f"{len(windows)} ({w_start} to {w_end}) failed: {e}. "
                f"{index - 1} earlier window(s) are committed and recorded; "
                "re-running the model resumes from this one."
            ) from e
        logger.info(
            "%s: window %d/%d %s to %s, %d row(s)",
            model.full_name, index, len(windows), w_start, w_end, written,
        )

    duration_ms = int((time.perf_counter() - start_clock) * 1000)
    row_count = conn.execute(f"SELECT count(*) FROM {model.full_name}").fetchone()[0]
    return duration_ms, row_count


def _table_columns(
    conn: duckdb.DuckDBPyConnection, schema: str, name: str
) -> list[tuple[str, str]]:
    """``(name, type)`` for a catalog table, in ordinal order."""
    return [
        (str(r[0]), str(r[1]))
        for r in conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_catalog = current_database() "
            "AND table_schema = ? AND table_name = ? ORDER BY ordinal_position",
            [schema, name],
        ).fetchall()
    ]


def _temp_columns(
    conn: duckdb.DuckDBPyConnection, name: str
) -> list[tuple[str, str]]:
    """``(name, type)`` for a TEMP table, isolated from same-named catalog tables."""
    return [
        (str(r[0]), str(r[1]))
        for r in conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ? AND table_catalog = 'temp' "
            "ORDER BY ordinal_position",
            [name],
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# Snapshot (SCD2) materialization
# ---------------------------------------------------------------------------


class SnapshotError(ValueError):
    """A snapshot model cannot be merged into its history table.

    Always raised before the first write to the target, so the refusal costs
    the caller a run and never a row of history.
    """


# Only a literal-shaped expression is accepted as the ``valid_to`` sentinel.
# It comes out of project.yml and is spliced into DDL, so it is kept to the
# shape of ``'9999-12-31'::TIMESTAMP`` or ``TIMESTAMP '9999-12-31'``.
_SENTINEL_RE = re.compile(r"^[A-Za-z0-9_'\-:. ()]+$")

# The meta column roles a snapshot target carries, in the order they are
# appended to the user's own columns.
SNAPSHOT_META_ROLES = ("valid_from", "valid_to", "is_current", "row_hash", "is_deleted")


@dataclass(frozen=True)
class SnapshotSettings:
    """Names and shape of the meta columns a snapshot writes.

    Defaults are havn's own spelling. A project migrating from dbt renames
    them once in project.yml under ``snapshots.meta_columns`` rather than
    rewriting every downstream model.
    """

    valid_from: str = "valid_from"
    valid_to: str = "valid_to"
    is_current: str = "is_current"
    row_hash: str = "row_hash"
    is_deleted: str = "is_deleted"
    # SQL literal written into an open row's valid_to instead of NULL.
    valid_to_current: str | None = None

    def names(self, *, with_deleted: bool) -> list[str]:
        """The meta column names this snapshot's target actually has."""
        out = [self.valid_from, self.valid_to, self.is_current, self.row_hash]
        if with_deleted:
            out.append(self.is_deleted)
        return out

    def open_valid_to(self) -> str:
        """The SQL an open row's ``valid_to`` is set to."""
        if not self.valid_to_current:
            return "CAST(NULL AS TIMESTAMP)"
        return self.valid_to_current


def snapshot_settings_from_config(config: object | None) -> SnapshotSettings:
    """Read ``snapshots:`` off a loaded project config, or fall back to defaults."""
    block = getattr(config, "snapshots", None)
    if block is None:
        return SnapshotSettings()
    overrides = dict(getattr(block, "meta_columns", {}) or {})
    unknown = [k for k in overrides if k not in SNAPSHOT_META_ROLES]
    if unknown:
        raise SnapshotError(
            f"project.yml snapshots.meta_columns: unknown meta column "
            f"{', '.join(sorted(unknown))}. Known roles: "
            f"{', '.join(SNAPSHOT_META_ROLES)}."
        )
    for role, name in overrides.items():
        validate_identifier(str(name), f"snapshots.meta_columns.{role}")
    sentinel = getattr(block, "valid_to_current", None)
    if sentinel is not None and not _SENTINEL_RE.match(str(sentinel)):
        raise SnapshotError(
            "project.yml snapshots.valid_to_current must be a literal such as "
            "\"'9999-12-31'::TIMESTAMP\"; "
            f"{sentinel!r} is not."
        )
    return SnapshotSettings(
        valid_from=str(overrides.get("valid_from", "valid_from")),
        valid_to=str(overrides.get("valid_to", "valid_to")),
        is_current=str(overrides.get("is_current", "is_current")),
        row_hash=str(overrides.get("row_hash", "row_hash")),
        is_deleted=str(overrides.get("is_deleted", "is_deleted")),
        valid_to_current=str(sentinel) if sentinel is not None else None,
    )


def snapshot_settings_for(project_dir: object | None) -> SnapshotSettings:
    """Load snapshot settings from a project directory, defaults on any failure."""
    if project_dir is None:
        return SnapshotSettings()
    try:
        from havn.config import load_project

        return snapshot_settings_from_config(load_project(project_dir))
    except SnapshotError:
        raise
    except Exception as e:
        logger.debug("Could not read snapshots config from %s: %s", project_dir, e)
        return SnapshotSettings()


def _snapshot_hash_columns(
    model: SQLModel,
    source_cols: list[str],
    keys: list[str],
) -> list[str]:
    """Which source columns feed ``row_hash``.

    ``check_cols`` narrows it to a named subset; anything else hashes every
    non-key column. The hash is stored under both strategies, so a timestamp
    snapshot still records what the row looked like even though the change
    decision is made on ``updated_at``.
    """
    lower = {c.lower(): c for c in source_cols}
    spec = (model.check_cols or "all").strip()
    if model.strategy == "check" and spec.lower() not in ("", "all"):
        wanted = [c.strip() for c in spec.split(",") if c.strip()]
        missing = [c for c in wanted if c.lower() not in lower]
        if missing:
            raise SnapshotError(
                f"Model {model.full_name}: check_cols names column(s) "
                f"{', '.join(missing)}, which the model's query does not select. "
                f"Available columns: {', '.join(source_cols)}."
            )
        return [lower[c.lower()] for c in wanted]
    key_set = {k.lower() for k in keys}
    non_key = [c for c in source_cols if c.lower() not in key_set]
    # A snapshot whose every column is part of the key still needs a hash to
    # compare, and hashing the key is the only honest answer there.
    return non_key or list(source_cols)


def _execute_snapshot(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    actions: list[str] | None = None,
    model_map: dict[str, SQLModel] | None = None,
    settings: SnapshotSettings | None = None,
    query_rewriter: Callable[[str], str] | None = None,
) -> tuple[int, int]:
    """Merge the model's current rows into an SCD2 history table.

    Four statements, all inside one transaction:

    1. close the current row of every key whose source row changed,
    2. close (and optionally tombstone) every key that left the source,
    3. insert a row for every key that is new or changed,
    4. record the run.

    The key comparison is ``IS NOT DISTINCT FROM`` throughout, for the same
    reason the incremental path uses it: a plain ``=`` is NULL rather than
    TRUE when a key column is NULL, so a NULL-keyed row would never match its
    own history and would be re-inserted on every run.

    ``--force`` reaches here exactly like an ordinary run. It re-merges; it
    never drops the table, because forcing a rebuild must not be a way to
    lose history by accident. Dropping the table is the reset.
    """
    from havn.engine.diff import _row_hash_expr

    st = settings or SnapshotSettings()
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {model.schema}")
    start = time.perf_counter()

    if not model.unique_key:
        raise SnapshotError(
            f"Model {model.full_name}: materialized=snapshot needs a unique_key "
            "so a source row can be matched against its own history."
        )
    keys = [k.strip() for k in model.unique_key.split(",") if k.strip()]
    for k in keys:
        validate_identifier(k, "unique_key column")
    if model.strategy not in ("check", "timestamp"):
        raise SnapshotError(
            f"Model {model.full_name}: unknown snapshot strategy "
            f"'{model.strategy}'. Supported: check, timestamp."
        )
    if model.strategy == "timestamp":
        if not model.updated_at:
            raise SnapshotError(
                f"Model {model.full_name}: strategy=timestamp needs "
                "updated_at=<column> so the engine knows which column is the "
                "source's change clock."
            )
        validate_identifier(model.updated_at, "updated_at column")
    if model.hard_deletes not in ("ignore", "invalidate", "new_record"):
        raise SnapshotError(
            f"Model {model.full_name}: unknown hard_deletes policy "
            f"'{model.hard_deletes}'. Supported: ignore, invalidate, new_record."
        )

    track_deleted = model.hard_deletes == "new_record"
    meta_names = st.names(with_deleted=track_deleted)
    validate_identifier(model.name, "staging table name")
    staging = f"_havn_snapshot_{model.name}"
    deleted_tmp = f"_havn_snapshot_deleted_{model.name}"

    _drop_conflicting(conn, model.schema, model.name, "snapshot")
    exists = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_catalog = current_database() "
        "AND table_schema = ? AND table_name = ? AND table_type = 'BASE TABLE'",
        [model.schema, model.name],
    ).fetchone()[0] > 0

    query = resolve_query(model, model_map, query_rewriter)
    conn.execute(f"CREATE OR REPLACE TEMP TABLE {staging} AS\n{query}")
    staging_cols = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = ? AND table_catalog = 'temp' "
        "ORDER BY ordinal_position",
        [staging],
    ).fetchall()
    source_cols = [str(r[0]) for r in staging_cols]

    clash = [c for c in source_cols if c.lower() in {m.lower() for m in meta_names}]
    if clash:
        raise SnapshotError(
            f"Model {model.full_name}: the query selects column(s) "
            f"{', '.join(clash)}, which collide with the snapshot's own meta "
            f"columns ({', '.join(meta_names)}). Rename the column in the "
            "query, or rename the meta column under snapshots.meta_columns "
            "in project.yml."
        )
    missing_keys = [k for k in keys if k.lower() not in {c.lower() for c in source_cols}]
    if missing_keys:
        raise SnapshotError(
            f"Model {model.full_name}: unique_key column(s) "
            f"{', '.join(missing_keys)} are not selected by the query."
        )
    if model.strategy == "timestamp" and model.updated_at.lower() not in {
        c.lower() for c in source_cols
    }:
        raise SnapshotError(
            f"Model {model.full_name}: updated_at column '{model.updated_at}' "
            "is not selected by the query."
        )

    hash_cols = _snapshot_hash_columns(model, source_cols, keys)
    conn.execute(
        f"CREATE OR REPLACE TEMP TABLE {staging}_h AS "
        f'SELECT *, {_row_hash_expr(hash_cols)} AS "{st.row_hash}" FROM {staging}'
    )
    staged = f"{staging}_h"

    # Key uniqueness, checked before the target is touched at all. DuckDB's
    # UPDATE ... FROM picks an arbitrary row when the source matches a target
    # row more than once, so a duplicate key would write whichever version the
    # scan happened to reach first. Refusing is the only deterministic answer.
    key_list = ", ".join(f'"{k}"' for k in keys)
    dupes = conn.execute(
        f"SELECT {key_list}, COUNT(*) AS n FROM {staged} "
        f"GROUP BY {key_list} HAVING COUNT(*) > 1 ORDER BY n DESC LIMIT 3"
    ).fetchall()
    if dupes:
        sample = "; ".join(
            "(" + ", ".join(str(v) for v in row[:-1]) + f") x{row[-1]}"
            for row in dupes
        )
        raise SnapshotError(
            f"Model {model.full_name}: unique_key ({', '.join(keys)}) is not "
            f"unique in the query's output: {sample}. Nothing was written -- "
            "the history table still holds exactly the rows it held before. "
            "De-duplicate the query (a QUALIFY row_number() filter is the "
            "usual fix) or widen unique_key."
        )

    run_ts = "current_timestamp::TIMESTAMP"
    # The timestamp strategy dates a version from the source's own clock, so
    # replaying an old extract lands the version where it belongs in history
    # rather than at the moment of the replay.
    open_ts = f's."{model.updated_at}"::TIMESTAMP' if model.strategy == "timestamp" else run_ts
    src_select = ", ".join(f's."{c}"' for c in source_cols)
    col_list = ", ".join(f'"{c}"' for c in source_cols)

    if not exists:
        meta_select = (
            f'{open_ts} AS "{st.valid_from}", '
            f'{st.open_valid_to()} AS "{st.valid_to}", '
            f'TRUE AS "{st.is_current}", '
            f's."{st.row_hash}" AS "{st.row_hash}"'
        )
        if track_deleted:
            meta_select += f', FALSE AS "{st.is_deleted}"'
        conn.execute(
            f"CREATE TABLE {model.full_name} AS "
            f"SELECT {src_select}, {meta_select} FROM {staged} AS s"
        )
        duration_ms = int((time.perf_counter() - start) * 1000)
        row_count = conn.execute(
            f"SELECT count(*) FROM {model.full_name}"
        ).fetchone()[0]
        conn.execute(f"DROP TABLE IF EXISTS {staged}")
        conn.execute(f"DROP TABLE IF EXISTS {staging}")
        return duration_ms, row_count

    # Schema evolution. The target's meta columns are not in the query, so
    # they are held out of the diff; what is left is the user's own columns,
    # compared under append_new_columns: a new column is appended (NULL for
    # every historical row, exactly as dbt does it), a removed or retyped one
    # is refused, because rewriting history in place is not something a
    # snapshot is allowed to do quietly.
    #
    # Every meta name is held out, not only the ones this run writes.
    # Otherwise switching hard_deletes away from new_record left is_deleted
    # looking like a user column the query had dropped, and the policy
    # refused the write for good.
    from dataclasses import replace as _replace

    target_cols = [
        (str(r[0]), str(r[1]))
        for r in conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_catalog = current_database() "
            "AND table_schema = ? AND table_name = ? ORDER BY ordinal_position",
            [model.schema, model.name],
        ).fetchall()
    ]
    meta_lower = {m.lower() for m in st.names(with_deleted=True)}
    user_target_cols = [(n, t) for n, t in target_cols if n.lower() not in meta_lower]
    target_lower = {n.lower() for n, _ in target_cols}
    # A target built under hard_deletes=ignore or invalidate has no
    # is_deleted column. Switching to new_record makes it required, and the
    # evolution plan above never sees a meta column, so without this the
    # tombstone INSERT failed to bind on every run from then on.
    add_is_deleted = track_deleted and st.is_deleted.lower() not in target_lower
    plan = _plan_schema_change(
        _replace(model, on_schema_change="append_new_columns"),
        user_target_cols,
        [(str(n), str(t)) for n, t in staging_cols],
        keys,
    )

    key_match = " AND ".join(
        f'h."{k}" IS NOT DISTINCT FROM s."{k}"' for k in keys
    )
    if model.strategy == "timestamp":
        changed_pred = f's."{model.updated_at}"::TIMESTAMP > h."{st.valid_from}"'
        unchanged_pred = f'h."{st.valid_from}" >= s."{model.updated_at}"::TIMESTAMP'
    else:
        changed_pred = f'h."{st.row_hash}" IS DISTINCT FROM s."{st.row_hash}"'
        unchanged_pred = f'h."{st.row_hash}" IS NOT DISTINCT FROM s."{st.row_hash}"'
    # A tombstone is never "unchanged": the key coming back has to close the
    # tombstone and open a live version again.
    if track_deleted:
        changed_pred = f'(h."{st.is_deleted}" OR ({changed_pred}))'
        live_current = f'h."{st.is_current}" AND NOT h."{st.is_deleted}"'
    else:
        live_current = f'h."{st.is_current}"'

    owns_tx = _begin_transaction(conn)
    try:
        _apply_schema_plan(conn, model, plan)
        if actions is not None:
            actions.extend(plan.describe())

        if add_is_deleted:
            conn.execute(
                f'ALTER TABLE {model.full_name} ADD COLUMN "{st.is_deleted}" '
                "BOOLEAN DEFAULT FALSE"
            )
            # Explicit, rather than trusting the DEFAULT to reach rows that
            # are already there: every version written before the switch was
            # a live one, so none of them is a tombstone.
            conn.execute(
                f'UPDATE {model.full_name} SET "{st.is_deleted}" = FALSE '
                f'WHERE "{st.is_deleted}" IS NULL'
            )
            line = f"added column {st.is_deleted} BOOLEAN"
            logger.info("%s: %s", model.full_name, line)
            if actions is not None:
                actions.append(line)

        # 1. Close the current version of every key whose source row changed.
        conn.execute(
            f"UPDATE {model.full_name} AS h "
            f'SET "{st.valid_to}" = {open_ts if model.strategy == "timestamp" else run_ts}, '
            f'"{st.is_current}" = FALSE '
            f"FROM {staged} AS s "
            f'WHERE {key_match} AND h."{st.is_current}" AND {changed_pred}'
        )

        # 2. Keys that left the source.
        if model.hard_deletes != "ignore":
            absent = (
                f"NOT EXISTS (SELECT 1 FROM {staged} AS s WHERE {key_match})"
            )
            if track_deleted:
                # Capture before closing: once is_current flips there is no
                # way to tell this run's departures from an older run's.
                departed_select = ", ".join(f'h."{c}"' for c in plan.columns)
                conn.execute(
                    f"CREATE OR REPLACE TEMP TABLE {deleted_tmp} AS "
                    f'SELECT {departed_select}, h."{st.row_hash}" AS "{st.row_hash}" '
                    f"FROM {model.full_name} AS h "
                    f"WHERE {live_current} AND {absent}"
                )
            conn.execute(
                f"UPDATE {model.full_name} AS h "
                f'SET "{st.valid_to}" = {run_ts}, "{st.is_current}" = FALSE '
                f"WHERE {live_current} AND {absent}"
            )
            if track_deleted:
                tomb_cols = ", ".join(f'"{c}"' for c in plan.columns)
                tomb_select = ", ".join(f'd."{c}"' for c in plan.columns)
                conn.execute(
                    f"INSERT INTO {model.full_name} "
                    f'({tomb_cols}, "{st.valid_from}", "{st.valid_to}", '
                    f'"{st.is_current}", "{st.row_hash}", "{st.is_deleted}") '
                    f"SELECT {tomb_select}, {run_ts}, {st.open_valid_to()}, "
                    f'TRUE, d."{st.row_hash}", TRUE FROM {deleted_tmp} AS d'
                )
                conn.execute(f"DROP TABLE IF EXISTS {deleted_tmp}")

        # 3. Insert a version for every key that is new or changed. A key
        #    whose current version still matches is skipped, which is what
        #    makes an identical replay a no-op.
        insert_cols = ", ".join(f'"{c}"' for c in plan.columns)
        insert_select = ", ".join(f's."{c}"' for c in plan.columns)
        meta_cols = f'"{st.valid_from}", "{st.valid_to}", "{st.is_current}", "{st.row_hash}"'
        meta_values = (
            f'{open_ts}, {st.open_valid_to()}, TRUE, s."{st.row_hash}"'
        )
        if track_deleted:
            meta_cols += f', "{st.is_deleted}"'
            meta_values += ", FALSE"
        conn.execute(
            f"INSERT INTO {model.full_name} ({insert_cols}, {meta_cols}) "
            f"SELECT {insert_select}, {meta_values} FROM {staged} AS s "
            f"WHERE NOT EXISTS (SELECT 1 FROM {model.full_name} AS h "
            f"WHERE {key_match} AND {live_current} AND {unchanged_pred})"
        )

        conn.execute(f"DROP TABLE IF EXISTS {staged}")
        conn.execute(f"DROP TABLE IF EXISTS {staging}")
        if owns_tx:
            conn.execute("COMMIT")
    except Exception:
        if owns_tx:
            try:
                conn.execute("ROLLBACK")
            except Exception as rb_err:
                logger.debug("Rollback after failed snapshot merge failed: %s", rb_err)
        raise

    duration_ms = int((time.perf_counter() - start) * 1000)
    row_count = conn.execute(f"SELECT count(*) FROM {model.full_name}").fetchone()[0]
    return duration_ms, row_count


def resolve_query(
    model: SQLModel,
    model_map: dict[str, SQLModel] | None,
    query_rewriter: Callable[[str], str] | None = None,
) -> str:
    """The SQL to build ``model`` from, with ephemeral upstreams inlined.

    Without a ``model_map`` there is nothing to resolve against, so the model's
    own query is returned and a project with no ephemeral models never pays for
    a sqlglot round trip.

    ``query_rewriter`` is the run's last word on the SQL, applied after
    inlining so that an inlined ephemeral upstream has already stopped being a
    table reference. A deferred run builds one (see :mod:`havn.engine.defer`)
    and hands it down from ``run_transform``. It is an argument and nothing
    else: there is no process-wide slot to fall back on, because two transform
    runs can be in flight at once and a run that did not ask to defer must
    never have another run's redirects applied to its models.
    """
    if not model_map:
        query = model.query
    else:
        from .inline import inline_ephemeral

        query = inline_ephemeral(model, model_map)

    return query_rewriter(query) if query_rewriter is not None else query


def _execute_incremental(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    actions: list[str] | None = None,
    model_map: dict[str, SQLModel] | None = None,
    *,
    batch_range: BatchRange | None = None,
    force: bool = False,
    run_id: str | None = None,
    query_rewriter: Callable[[str], str] | None = None,
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
    if model.incremental_strategy == "microbatch":
        return _execute_microbatch(
            conn, model, actions, model_map,
            batch_range=batch_range, force=force, run_id=run_id,
            query_rewriter=query_rewriter,
        )

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
        "WHERE table_catalog = current_database() "
        "AND table_schema = ? AND table_name = ? AND table_type = 'BASE TABLE'",
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
    query = resolve_query(model, model_map, query_rewriter)
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
        # {start}/{end} are microbatch's placeholders, but an explicit
        # --event-time-start/--event-time-end range makes them meaningful for
        # any incremental model, so the same substitution runs here. Without
        # a range they are left alone rather than guessed at.
        if batch_range is not None and batch_range.start and batch_range.end:
            filter_clause = substitute_batch_window(
                filter_clause, batch_range.start, batch_range.end
            )
        inner = query.rstrip().rstrip(";").rstrip()
        query = f"SELECT * FROM (\n{inner}\n) _havn_src\n{filter_clause}"

    strategy = model.incremental_strategy

    if not exists:
        # First run — full load
        ddl = f"CREATE TABLE {model.full_name} AS\n{query}"
        conn.execute(ddl)
    elif strategy == "append" or not model.unique_key:
        # Append-only. It still goes through staging, because a bare
        # ``INSERT INTO target <query>`` matches columns by position: a
        # reordered projection wrote region into amount and amount into
        # region without a word, and a new or dropped column ignored
        # on_schema_change entirely. Staging gives the same schema diff and
        # the same policy as every other strategy, plus an explicit column
        # list so order stops mattering.
        validate_identifier(model.name, "staging table name")
        staging_name = f"_havn_staging_{model.name}"
        conn.execute(f"CREATE OR REPLACE TEMP TABLE {staging_name} AS\n{query}")
        plan = _plan_schema_change(
            model,
            _table_columns(conn, model.schema, model.name),
            _temp_columns(conn, staging_name),
            [],
        )
        cols = ", ".join(f'"{c}"' for c in plan.columns)
        owns_tx = _begin_transaction(conn)
        try:
            _apply_schema_plan(conn, model, plan)
            if actions is not None:
                actions.extend(plan.describe())
            conn.execute(
                f"INSERT INTO {model.full_name} ({cols}) "
                f"SELECT {cols} FROM {staging_name}"
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
                        "Rollback after failed append insert failed: %s", rb_err
                    )
            raise
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
                "WHERE table_catalog = current_database() "
                "AND table_schema = ? AND table_name = ? "
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
    """Drop an existing object if it conflicts with the desired materialization type.

    Scoped to the current database: ``information_schema`` spans every
    attached one, and a deferred run has another warehouse attached. Without
    the filter this saw the defer target's copy of the model and tried to drop
    it locally, where it does not exist.
    """
    row = conn.execute(
        "SELECT table_type FROM information_schema.tables "
        "WHERE table_catalog = current_database() "
        "AND table_schema = ? AND table_name = ?",
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
    elif target_type in ("table", "incremental", "snapshot") and existing == "VIEW":
        conn.execute(f"DROP VIEW {full_name}")


def execute_model(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    actions: list[str] | None = None,
    model_map: dict[str, SQLModel] | None = None,
    *,
    snapshot_settings: SnapshotSettings | None = None,
    batch_range: BatchRange | None = None,
    force: bool = False,
    run_id: str | None = None,
    query_rewriter: Callable[[str], str] | None = None,
) -> tuple[int, int]:
    """Execute a single model. Returns (duration_ms, row_count).

    ``actions`` collects human-readable schema-evolution lines ("added column
    region VARCHAR") when the caller wants them for the run log. Passing None
    discards them.

    ``model_map`` is the full project, keyed by full name. It is what lets an
    ephemeral upstream be inlined into this model's query; without it the query
    is built as written.

    ``snapshot_settings`` names the meta columns a ``materialized=snapshot``
    model writes. Omitting it uses havn's default names, which is what every
    caller that has no project config in hand wants.

    ``query_rewriter`` is the deferred run's redirect table, passed down from
    ``run_transform``. Omitting it means "no defer", including while another
    run in this process is deferring.
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
                conn, model, actions, model_map,
                batch_range=batch_range, force=force, run_id=run_id,
                query_rewriter=query_rewriter,
            )
        elif model.materialized == "snapshot":
            duration_ms, row_count = _execute_snapshot(
                conn, model, actions, model_map, snapshot_settings,
                query_rewriter=query_rewriter,
            )
        else:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {model.schema}")
            start = time.perf_counter()
            _drop_conflicting(conn, model.schema, model.name, model.materialized)
            query = resolve_query(model, model_map, query_rewriter)

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


def _log_build(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    duration_ms: int,
    row_count: int,
    schema_changes: list[str],
    assertion_results: list[AssertionResult],
    pipeline_run_id: str | None = None,
) -> None:
    """Record a finished build in the run log, after its assertions ran.

    A failed severity=error assertion logs the build as "error" (the model's
    descendants are blocked, so calling it a success would contradict the rest
    of the run). Warnings leave it a success. The build's duration and row
    count are kept either way.
    """
    failed = [
        ar for ar in assertion_results
        if not ar.passed and (ar.severity or "error") == "error"
    ]
    error = None
    if failed:
        error = "assertion failed: " + "; ".join(
            f"{ar.expression} ({ar.detail})" if ar.detail else ar.expression
            for ar in failed
        )
    log_run(
        conn, "transform", model.full_name, "error" if failed else "success",
        duration_ms, row_count,
        error=error,
        log_output="; ".join(schema_changes) or None,
        pipeline_run_id=pipeline_run_id,
    )


def _execute_single_model(
    db_path: str,
    model: SQLModel,
    force: bool,
    model_map: dict[str, SQLModel],
    db_config: object | None = None,
    project_dir: object | None = None,
    pipeline_run_id: str | None = None,
    batch_range: BatchRange | None = None,
    query_rewriter: Callable[[str], str] | None = None,
) -> tuple[str, ModelResult]:
    """Execute a single model in its own connection (for parallel execution).

    If ``db_config`` is provided, the connection is opened through the
    warehouse backend (supports DuckLake). Otherwise falls back to the
    plain ``db_path`` open for the DuckDB backend.

    ``query_rewriter`` is the deferred run's rewriter. Workers are threads
    inside one run, so it arrives as an argument rather than through any
    per-thread or process-wide state: a worker of a non-deferred run gets
    None even while another run is deferring.

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
            conn, model, schema_changes, model_map,
            snapshot_settings=(
                snapshot_settings_for(project_dir)
                if model.materialized == "snapshot"
                else None
            ),
            batch_range=batch_range,
            force=force,
            run_id=pipeline_run_id,
            query_rewriter=query_rewriter,
        )
        _update_state(conn, model, duration_ms, row_count)

        # Run assertions (and the synthesised @grain check, if any). A
        # severity=error failure must surface as "assertion_failed" so the
        # orchestrator blocks descendants — same contract as the sequential path.
        assertion_results: list[AssertionResult] = []
        if model.assertions or model.grain:
            assertion_results = run_assertions(conn, model)
            _save_assertions(conn, model, assertion_results)
        _log_build(
            conn, model, duration_ms, row_count, schema_changes,
            assertion_results, pipeline_run_id,
        )
        if assertion_results:
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
        if model.materialized in ("table", "incremental", "snapshot"):
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
