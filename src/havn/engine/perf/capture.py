"""Plan capture for model builds, from DuckDB's own profiler.

A build is profiled by switching DuckDB's profiler on around the statement
that does the work (the ``CREATE TABLE AS``, or the staging ``CREATE TEMP
TABLE AS`` of an incremental, snapshot or microbatch model), and reading
``get_profiling_information()`` straight after. The query runs once: the
profile is a by-product of the real build, not an ``EXPLAIN ANALYZE`` re-run,
and DuckDB's per-operator timers cost a few percent.

The capture travels in a context variable that :func:`activate` sets for the
length of one ``execute_model`` call. Every executor routes its heavy
statement through :func:`run_build_statement`, which is a plain
``conn.execute`` whenever nothing is being captured. A context variable is
per thread here (parallel workers each build in their own thread), so two
models building at once never see each other's capture.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

import duckdb

logger = logging.getLogger("havn.perf")

# Long values in a plan's extra_info (a projection list of 300 columns, a
# filter with a 5,000-item IN list) are cut down to this, so a stored plan
# stays a few KB however wide the model is.
_MAX_TEXT = 300
_MAX_LIST = 12
_TOP_OPERATORS = 5


@dataclass
class BuildCapture:
    """What one model build measured. Filled by :func:`run_build_statement`."""

    model: str
    want_plan: bool = True
    statements: int = 0
    plan: dict | None = None
    plan_latency_s: float = 0.0
    latency_s: float = 0.0
    cpu_time_s: float = 0.0
    rows_scanned: int = 0
    rows_produced: int | None = None
    peak_memory_bytes: int = 0
    spill_bytes: int = 0
    bytes_read: int = 0
    bytes_written: int = 0
    full_refresh: bool | None = None
    extra: dict = field(default_factory=dict)

    def absorb(self, profile: dict, previous_marks: tuple[int, int] | None = None) -> None:
        """Fold one statement's profile into the build's totals.

        DuckDB's ``system_peak_buffer_memory`` and ``system_peak_temp_dir_size``
        are high-water marks for the connection that never go down, not
        per-query figures. They only describe this statement when it raised
        them (``previous_marks`` are the marks seen before it; None when this
        connection has not been profiled yet). Otherwise memory falls back to
        the statement's own ``total_memory_allocated`` and spill to zero.
        """
        self.statements += 1
        latency = _num(profile.get("latency"))
        self.latency_s += latency
        self.cpu_time_s += _num(profile.get("cpu_time"))
        self.rows_scanned += int(_num(profile.get("cumulative_rows_scanned")))
        prev_mem, prev_spill = previous_marks or (-1, -1)
        peak = int(_num(profile.get("total_memory_allocated")))
        system_peak = int(_num(profile.get("system_peak_buffer_memory")))
        if system_peak > prev_mem:
            peak = max(peak, system_peak)
        self.peak_memory_bytes = max(self.peak_memory_bytes, peak)
        spill = int(_num(profile.get("system_peak_temp_dir_size")))
        if spill > prev_spill:
            self.spill_bytes = max(self.spill_bytes, spill)
        self.bytes_read += int(_num(profile.get("total_bytes_read")))
        self.bytes_written += int(_num(profile.get("total_bytes_written")))
        plan = compact_plan(profile)
        if plan is None:
            return
        produced = _rows_produced(plan)
        if produced is not None:
            self.rows_produced = (self.rows_produced or 0) + produced
        # Keep the plan of the heaviest statement: a microbatch run of 30
        # windows stores one representative plan, not 30.
        if self.plan is None or latency >= self.plan_latency_s:
            self.plan = plan
            self.plan_latency_s = latency


_ACTIVE: ContextVar[BuildCapture | None] = ContextVar("havn_perf_capture", default=None)


@contextmanager
def activate(capture: BuildCapture | None) -> Iterator[BuildCapture | None]:
    """Make ``capture`` the target of :func:`run_build_statement` in this thread."""
    token = _ACTIVE.set(capture)
    try:
        yield capture
    finally:
        _ACTIVE.reset(token)


def active_capture() -> BuildCapture | None:
    return _ACTIVE.get()


def run_build_statement(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    *,
    full_refresh: bool | None = None,
):
    """Execute a build's main statement, profiling it when a capture is active.

    ``full_refresh`` says whether this statement rewrites the whole target
    (a table rebuild, an incremental's first load) or only a slice of it;
    the advisor's "this should be incremental" rule reads it.

    Profiling is restored to what it was before, so a connection someone
    had switched to ``enable_profiling='json'`` stays that way.
    """
    capture = _ACTIVE.get()
    if capture is None:
        return conn.execute(sql)
    if full_refresh is not None and capture.full_refresh is None:
        capture.full_refresh = full_refresh
    if not capture.want_plan:
        return conn.execute(sql)

    previous = _profiling_setting(conn)
    try:
        conn.execute("PRAGMA enable_profiling='no_output'")
    except duckdb.Error as e:
        logger.debug("Could not enable profiling for %s: %s", capture.model, e)
        return conn.execute(sql)
    try:
        result = conn.execute(sql)
        try:
            raw = conn.get_profiling_information(format="json")
            profile = json.loads(raw) if raw else None
            # The shared server connection is used from several threads; a
            # profile whose query is not ours belongs to someone else.
            if profile and _same_query(profile.get("query_name"), sql):
                key = id(conn)
                capture.absorb(profile, _marks.get(key))
                _remember_marks(key, profile)
        except Exception as e:  # a profile is a bonus, never a build failure
            logger.debug("Could not read the profile for %s: %s", capture.model, e)
        return result
    finally:
        _restore_profiling(conn, previous)


# High-water marks last seen per connection, keyed by id(). A recycled id
# carries over a higher mark, which only makes the next reading conservative
# (it falls back to the statement's own allocation).
_marks: dict[int, tuple[int, int]] = {}


def _remember_marks(key: int, profile: dict) -> None:
    if len(_marks) > 256:
        _marks.clear()
    _marks[key] = (
        int(_num(profile.get("system_peak_buffer_memory"))),
        int(_num(profile.get("system_peak_temp_dir_size"))),
    )


def _profiling_setting(conn: duckdb.DuckDBPyConnection) -> str | None:
    try:
        row = conn.execute("SELECT current_setting('enable_profiling')").fetchone()
        return row[0] if row else None
    except duckdb.Error:
        return None


def _restore_profiling(conn: duckdb.DuckDBPyConnection, previous: str | None) -> None:
    try:
        if previous:
            safe = previous.replace("'", "")
            conn.execute(f"PRAGMA enable_profiling='{safe}'")
        else:
            conn.execute("PRAGMA disable_profiling")
    except duckdb.Error as e:
        logger.debug("Could not restore profiling: %s", e)


def _same_query(profiled: Any, sql: str) -> bool:
    if not isinstance(profiled, str):
        return True  # older DuckDB without query_name: trust it
    return profiled.strip()[:200] == sql.strip()[:200]


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Compact plans
# ---------------------------------------------------------------------------


def _strip_catalog(table: Any) -> str | None:
    """``memory.silver.orders`` -> ``silver.orders``; ``orders`` stays."""
    if not table:
        return None
    parts = str(table).split(".")
    return ".".join(parts[-2:]) if len(parts) >= 3 else str(table)


def _trim(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= _MAX_TEXT else value[: _MAX_TEXT - 1] + "…"
    if isinstance(value, list):
        head = [_trim(v) for v in value[:_MAX_LIST]]
        if len(value) > _MAX_LIST:
            head.append(f"… (+{len(value) - _MAX_LIST} more)")
        return head
    if isinstance(value, dict):
        return {k: _trim(v) for k, v in list(value.items())[:_MAX_LIST]}
    return value


def _compact_node(node: dict) -> dict:
    extra = dict(node.get("extra_info") or {})
    table = _strip_catalog(extra.pop("Table", None))
    estimated = extra.pop("Estimated Cardinality", None)
    out: dict = {"operator": (node.get("operator_name") or node.get("operator_type") or "UNKNOWN").strip()}
    if table:
        out["table"] = table
    try:
        if estimated is not None:
            out["estimated_rows"] = int(estimated)
    except (TypeError, ValueError):
        pass
    if node.get("operator_cardinality") is not None:
        out["actual_rows"] = int(_num(node.get("operator_cardinality")))
    scanned = int(_num(node.get("operator_rows_scanned")))
    if scanned:
        out["rows_scanned"] = scanned
    if node.get("operator_timing") is not None:
        out["actual_time_ms"] = round(_num(node.get("operator_timing")) * 1000, 3)
    if extra:
        out["extra_info"] = _trim(extra)
    children = [_compact_node(c) for c in node.get("children") or []]
    if children:
        out["children"] = children
    return out


def compact_plan(profile: dict) -> dict | None:
    """The operator tree of a DuckDB JSON profile, in ``plan_to_dict`` shape.

    The shape is what :mod:`havn.engine.explain` produces for EXPLAIN
    ANALYZE (operator, table, actual_rows, actual_time_ms, extra_info,
    children), so the web UI renders a captured plan with the same
    component, plus ``rows_scanned`` where a scan read more than it kept.
    """
    children = profile.get("children") or []
    if not children:
        return None
    if len(children) == 1:
        return _compact_node(children[0])
    return {"operator": "QUERY", "children": [_compact_node(c) for c in children]}


def _rows_produced(plan: dict) -> int | None:
    """Rows the build query produced: the input of the CREATE / INSERT sink."""
    op = plan.get("operator", "")
    if op.endswith("CREATE_TABLE_AS") or op in ("INSERT", "CREATE_TABLE"):
        kids = plan.get("children") or []
        if kids and kids[0].get("actual_rows") is not None:
            return int(kids[0]["actual_rows"])
        return None
    return plan.get("actual_rows")


def iter_nodes(plan: dict | None, depth: int = 0, path: str = "") -> Iterator[tuple[dict, int, str]]:
    """Every node of a compact plan with its depth and a positional path."""
    if not plan:
        return
    here = f"{path}/{plan.get('operator', '?')}"
    yield plan, depth, here
    for i, child in enumerate(plan.get("children") or []):
        yield from iter_nodes(child, depth + 1, f"{here}[{i}]")


def top_operators(plan: dict | None, n: int = _TOP_OPERATORS) -> list[dict]:
    """The ``n`` operators that took the most time, with their share."""
    nodes = [node for node, _d, _p in iter_nodes(plan)]
    total = sum(node.get("actual_time_ms") or 0 for node in nodes)
    ranked = sorted(nodes, key=lambda node: node.get("actual_time_ms") or 0, reverse=True)
    out = []
    for node in ranked[:n]:
        t = node.get("actual_time_ms") or 0
        if t <= 0:
            continue
        out.append({
            "operator": node.get("operator"),
            "table": node.get("table"),
            "time_ms": round(t, 3),
            "rows": node.get("actual_rows"),
            "pct": round(100.0 * t / total, 1) if total else None,
        })
    return out
