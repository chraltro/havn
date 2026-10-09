"""The watermark bookkeeping around every build of a live incremental model.

Batch transforms, job runs and the live runner all build a live model the
same way, through ``execute_model``, which hands it to :func:`build_live`:

1. one transaction opens (or the caller's is joined);
2. the current watermark of each tracked input and what the model has
   already consumed are read *inside* it, so they belong to the same
   snapshot as the rows the query is about to read;
3. ``{watermark}`` placeholders become the consumed marks and the model is
   built incrementally;
4. the consumed marks move to the snapshot's watermarks and the model
   publishes its own watermark, all before the commit.

Because data and marks commit together, a crash cannot leave one without the
other, and because every builder runs the same steps, a batch run that gets
to a live model first simply consumes the pending batch itself; the runner
then finds nothing left to do. Two builders in different processes collide
on the ``live_consumed`` row and one fails, rather than both applying.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import duckdb

from havn.engine.transform.models import SQLModel

from . import events
from .graph import WATERMARK_RE, placeholder_text, resolve_placeholder_source, tracked_sources
from .sources import SEQ_COLUMN
from .state import (
    consumed_watermarks,
    ensure_live_tables,
    oldest_origin,
    record_advance,
    source_watermarks,
    utcnow,
    write_consumed,
)

logger = logging.getLogger("havn.live")


@dataclass
class LiveBuildResult:
    """What one build of a live model consumed and published."""

    model: str
    consumed_before: dict[str, int] = field(default_factory=dict)
    consumed_after: dict[str, int] = field(default_factory=dict)
    events: int = 0                     # source rows newly consumed
    origin_at: datetime | None = None   # arrival of the oldest consumed data
    committed_at: datetime | None = None
    published: int | None = None        # the model's own new watermark, if it moved
    full_load: bool = False

    @property
    def lag_ms(self) -> int | None:
        if self.origin_at is None or self.committed_at is None:
            return None
        return max(int((self.committed_at - self.origin_at).total_seconds() * 1000), 0)


# The last result per model, for the runner (which builds inside its own
# transaction and reads it back after committing). Keyed by model name;
# only ever written under that model's build lock.
_last_results: dict[str, LiveBuildResult] = {}


def last_result(model: str) -> LiveBuildResult | None:
    return _last_results.get(model)


def _relation_exists(conn: duckdb.DuckDBPyConnection, model: SQLModel) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_catalog = current_database() "
        "AND table_schema = ? AND table_name = ? AND table_type = 'BASE TABLE'",
        [model.schema, model.name],
    ).fetchone()
    return bool(row and row[0])


def _has_seq_column(conn: duckdb.DuckDBPyConnection, model: SQLModel) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_catalog = current_database() AND table_schema = ? "
        "AND table_name = ? AND column_name = ?",
        [model.schema, model.name, SEQ_COLUMN],
    ).fetchone()
    return bool(row and row[0])


def _events_between(
    conn: duckdb.DuckDBPyConnection, before: dict[str, int], after: dict[str, int]
) -> int:
    total = 0
    for source, wm in after.items():
        lo = before.get(source, 0)
        if wm <= lo:
            continue
        row = conn.execute(
            'SELECT COALESCE(SUM("rows"), 0) FROM _havn.live_advances '
            "WHERE source = ? AND wm_to > ? AND wm_to <= ?",
            [source, lo, wm],
        ).fetchone()
        total += int(row[0] or 0) if row else 0
    return total


def watermark_placeholders(
    model: SQLModel,
    model_map: dict[str, SQLModel] | None,
    consumed: dict[str, int],
) -> dict[str, str]:
    """``{"{watermark}": "41", "{watermark:landing.x}": "7"}`` for this build."""
    out: dict[str, str] = {}
    for match in WATERMARK_RE.finditer(placeholder_text(model)):
        token = match.group(0)
        if token in out:
            continue
        source = resolve_placeholder_source(model, model_map or {}, match.group(1))
        out[token] = str(int(consumed.get(source, 0)))
    return out


def build_live(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    model_map: dict[str, SQLModel] | None,
    build: Callable[[dict[str, str]], tuple[int, int]],
) -> tuple[int, int]:
    """Run ``build(placeholders)`` with the live bookkeeping around it.

    ``build`` is the ordinary incremental execution, given the placeholder
    values to substitute. Returns whatever it returns.
    """
    from havn.engine.utils import begin_transaction

    owns_tx = begin_transaction(conn)
    try:
        ensure_live_tables(conn)
        sources = tracked_sources(model, model_map or {})
        exists = _relation_exists(conn, model)
        current = source_watermarks(conn, sources)
        # No target means a full load: read everything, whatever an older
        # (dropped) table had consumed.
        consumed = consumed_watermarks(conn, model.full_name) if exists else {}
        placeholders = watermark_placeholders(model, model_map, consumed)

        duration_ms, row_count = build(placeholders)

        now = utcnow()
        marks = {s: max(current.get(s, 0), consumed.get(s, 0)) for s in sources}
        write_consumed(conn, model.full_name, marks, now)
        result = LiveBuildResult(
            model=model.full_name,
            consumed_before=dict(consumed),
            consumed_after=dict(marks),
            committed_at=now,
            full_load=not exists,
        )
        moved = {s: wm for s, wm in marks.items() if wm > consumed.get(s, 0)}
        if moved:
            result.events = _events_between(conn, consumed, marks)
            result.origin_at = oldest_origin(
                conn, {s: consumed.get(s, 0) for s in moved}, upto=moved
            )
        result.published = _publish_own_watermark(conn, model, result, bool(moved), now)
        if owns_tx:
            conn.execute("COMMIT")
    except Exception:
        if owns_tx:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
        raise
    _last_results[model.full_name] = result
    if owns_tx and result.published is not None:
        events.publish(events.SourceAdvanced(
            source=model.full_name, watermark=result.published, rows=result.events,
            advanced_at=now, kind="model",
        ))
    return duration_ms, row_count


def _publish_own_watermark(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    result: LiveBuildResult,
    consumed_new: bool,
    now: datetime,
) -> int | None:
    """Move the model's own watermark so its live consumers refresh.

    With a ``_havn_seq`` column (the model passes its input's sequence
    through) the watermark is the highest sequence in the table, never
    moving backwards, so a downstream ``WHERE _havn_seq > {watermark}``
    reads exactly the rows this refresh wrote. Without one it is a refresh
    counter: downstream still refreshes, but has to find the change itself
    (``{this}``, or reprocessing affected keys).
    """
    prev = source_watermarks(conn, [model.full_name]).get(model.full_name, 0)
    if _has_seq_column(conn, model):
        row = conn.execute(f"SELECT MAX({SEQ_COLUMN}) FROM {model.full_name}").fetchone()
        new = max(prev, int(row[0]) if row and row[0] is not None else 0)
    else:
        new = prev + 1 if (consumed_new or result.full_load) else prev
    if new <= prev:
        return None
    record_advance(
        conn, model.full_name, "model", prev, new, result.events, now,
        result.origin_at or now,
    )
    return new
