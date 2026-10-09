"""Performance advice: rules over the DAG, build history and captured plans.

Every rule explains itself and shows its evidence (the numbers from the
plan or the history that made it fire), and every rule is held back by a
threshold in ``performance.advice`` so a project of small models gets no
advice at all. An item can be dismissed for good or snoozed for a number of
days, per model and rule; :func:`set_advice_state` records that.

The rules:

``incremental_candidate``
    A ``table`` that rebuilds a large row count every run while the history
    shows it only ever grows by a small fraction: the run rewrites millions
    of rows to add a few thousand. Suggests ``incremental`` with a
    ``unique_key`` and an event-time column picked from the model's columns
    (a column the profile shows as unique; a timestamp named like one).
``join_fanout``
    A join whose output is many times its largest input, or a nested-loop /
    cross product over many rows: a join key that is not unique on either
    side, or a missing column in ``ON``.
``materialize_view``
    A view with joins or aggregation that several models read: each of them
    recomputes it. A table computes it once.
``unused_table``
    A materialized model nothing reads: no downstream model, no exposure, no
    dashboard, no metric definition and no query in the audit or slow-query
    log for ``unused_days``.
``scan_small_slice``
    A scan that reads a large table to keep a sliver of it, either because
    the predicate could not be pushed into the scan (a function wrapped
    around the column) or because the data is not laid out on it.
``order_by_non_final``
    A top-level ``ORDER BY`` (without ``LIMIT``) in a model other models
    read: the sort is paid for and then thrown away by the next query.
``distinct_large``
    ``SELECT DISTINCT`` over a large row count, usually hiding a grain
    problem upstream.
``python_udf_hot_path``
    A Python ``@macro`` called on a large row count. Python UDFs run outside
    DuckDB's vectorised engine; a SQL macro does not.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import duckdb

from .capture import iter_nodes
from .store import _fetch, ensure_perf_tables, latest_builds, local_now, model_history

logger = logging.getLogger("havn.perf")

RULES = (
    "incremental_candidate",
    "join_fanout",
    "materialize_view",
    "unused_table",
    "scan_small_slice",
    "order_by_non_final",
    "distinct_large",
    "python_udf_hot_path",
)

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class AdviceItem:
    rule: str
    model: str
    severity: str
    title: str
    explanation: str
    suggestion: str
    evidence: dict = field(default_factory=dict)
    status: str = "open"            # open, dismissed, snoozed
    snoozed_until: str | None = None

    @property
    def key(self) -> str:
        return f"{self.rule}:{self.model}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["key"] = self.key
        return d


@dataclass
class AdviceContext:
    """Everything the rules read, gathered once per advice pass."""

    conn: duckdb.DuckDBPyConnection
    project_dir: Path | None
    cfg: Any
    models: dict[str, Any]
    consumers: dict[str, list[str]]
    builds: dict[str, dict]
    exposure_refs: set[str] = field(default_factory=set)
    python_udfs: set[str] = field(default_factory=set)
    _history: dict[str, list[dict]] = field(default_factory=dict)
    _profiles: dict[str, dict] | None = None

    def history(self, model: str, n: int = 8) -> list[dict]:
        if model not in self._history:
            self._history[model] = model_history(self.conn, model, limit=n)
        return self._history[model]

    def profile(self, model: str) -> dict:
        if self._profiles is None:
            self._profiles = {}
            try:
                for name, rows, distinct in self.conn.execute(
                    "SELECT model_path, row_count, distinct_counts FROM _havn.model_profiles"
                ).fetchall():
                    try:
                        dc = json.loads(distinct) if isinstance(distinct, str) else (distinct or {})
                    except ValueError:
                        dc = {}
                    self._profiles[name] = {"row_count": rows, "distinct_counts": dc}
            except duckdb.Error:
                pass
        return self._profiles.get(model, {})

    def columns(self, model: str) -> list[tuple[str, str]]:
        parts = model.split(".")
        if len(parts) != 2:
            return []
        try:
            return [
                (r[0], r[1]) for r in self.conn.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_catalog = current_database() AND table_schema = ? "
                    "AND table_name = ? ORDER BY ordinal_position",
                    parts,
                ).fetchall()
            ]
        except duckdb.Error:
            return []


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _rows(n: Any) -> str:
    if n is None:
        return "?"
    n = float(n)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{int(n)}"


def _ms(ms: Any) -> str:
    ms = float(ms or 0)
    if ms >= 60_000:
        return f"{ms / 60_000:.1f} min"
    if ms >= 1000:
        return f"{ms / 1000:.1f} s"
    return f"{ms:.0f} ms"


def _median(values: list[float]) -> float:
    from .regression import median

    return median(values)


def _build_ms(ctx: AdviceContext, model: str) -> float:
    durations = [float(h.get("duration_ms") or 0) for h in ctx.history(model)]
    return _median(durations) if durations else 0.0


def _top_level_select(ast: Any) -> Any:
    from sqlglot import exp

    if ast is None:
        return None
    if isinstance(ast, exp.Select):
        return ast
    return ast.find(exp.Select)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

_KEY_NAME = re.compile(r"(^id$|_id$|_key$|^key$|_pk$|^uuid$|_uuid$)", re.IGNORECASE)
_TIME_NAME = re.compile(
    r"(updated|modified|changed|loaded|ingested|event|created|inserted)(_?(at|on|time|ts|date))?$|_at$|_ts$|^ts$|timestamp",
    re.IGNORECASE,
)
_TIME_TYPES = ("TIMESTAMP", "DATE", "DATETIME")


def _key_candidates(ctx: AdviceContext, model: str, row_count: int) -> list[str]:
    profile = ctx.profile(model)
    distinct = profile.get("distinct_counts") or {}
    cols = [c for c, _t in ctx.columns(model)]
    unique = [c for c in cols if row_count and distinct.get(c) == row_count]
    named = [c for c in unique if _KEY_NAME.search(c)]
    return (named + [c for c in unique if c not in named])[:3]


def _time_candidates(ctx: AdviceContext, model: str) -> list[str]:
    cols = ctx.columns(model)
    typed = [c for c, t in cols if any(t.upper().startswith(x) for x in _TIME_TYPES)]
    # Change clocks first: updated_at beats created_at for an upsert.
    ranked = sorted(
        typed,
        key=lambda c: (
            0 if re.search(r"updated|modified|changed", c, re.I) else
            1 if re.search(r"loaded|ingested|event", c, re.I) else
            2 if _TIME_NAME.search(c) else 3
        ),
    )
    return ranked[:3]


def rule_incremental_candidate(ctx: AdviceContext, model: Any) -> AdviceItem | None:
    if model.materialized != "table":
        return None
    cfg = ctx.cfg
    hist = [h for h in ctx.history(model.full_name) if h.get("rows_out") is not None]
    if len(hist) < 3:
        return None
    rows = [int(h["rows_out"]) for h in reversed(hist)]  # oldest first
    latest = rows[-1]
    if latest < cfg.big_table_rows:
        return None
    med_ms = _build_ms(ctx, model.full_name)
    if med_ms < cfg.min_duration_ms:
        return None
    if any(b < a for a, b in zip(rows, rows[1:])):
        return None  # rows were removed at some point: not append-only
    growth = [(b - a) / a for a, b in zip(rows, rows[1:]) if a > 0]
    if not growth:
        return None
    mean_growth = sum(growth) / len(growth)
    if mean_growth > cfg.append_growth:
        return None
    keys = _key_candidates(ctx, model.full_name, latest)
    times = _time_candidates(ctx, model.full_name)
    added = rows[-1] - rows[0]
    config_bits = ["materialized=incremental"]
    if keys:
        config_bits += [f"unique_key={keys[0]}", "incremental_strategy=merge"]
    else:
        config_bits.append("incremental_strategy=append")
    suggestion = "@config " + ", ".join(config_bits)
    if times:
        suggestion += f"\n@watermark {times[0]}"
        suggestion += (
            f"\n(or incremental_strategy=microbatch, event_time={times[0]}, batch_size=day "
            "if late rows arrive by event time)"
        )
    else:
        suggestion += (
            "\nand an incremental_filter, e.g. WHERE <change_column> > "
            "(SELECT MAX(<change_column>) FROM {this})"
        )
    per_run = added / max(len(rows) - 1, 1)
    severity = "high" if latest >= 10 * cfg.big_table_rows or med_ms >= 10 * cfg.min_duration_ms else "medium"
    return AdviceItem(
        rule="incremental_candidate",
        model=model.full_name,
        severity=severity,
        title=f"Rebuilds {_rows(latest)} rows to add about {_rows(per_run)} per run",
        explanation=(
            f"{model.full_name} is a full-refresh table. Over its last {len(rows)} builds it "
            f"never lost a row and grew by {100 * mean_growth:.1f}% per run on average, yet every "
            f"run rewrites all {_rows(latest)} rows (median build {_ms(med_ms)}). An incremental "
            "model would only process what is new."
        ),
        suggestion=suggestion,
        evidence={
            "row_history": rows,
            "mean_growth_pct": round(100 * mean_growth, 2),
            "median_build_ms": round(med_ms),
            "unique_key_candidates": keys,
            "event_time_candidates": times,
        },
    )


_LOOP_JOINS = ("NESTED_LOOP_JOIN", "BLOCKWISE_NL_JOIN", "CROSS_PRODUCT", "PIECEWISE_MERGE_JOIN")


def rule_join_fanout(ctx: AdviceContext, model: Any) -> AdviceItem | None:
    build = ctx.builds.get(model.full_name) or {}
    plan = build.get("plan")
    if not plan:
        return None
    cfg = ctx.cfg
    worst = None
    for node, _depth, _path in iter_nodes(plan):
        op = node.get("operator", "")
        if "JOIN" not in op and op != "CROSS_PRODUCT":
            continue
        out = node.get("actual_rows") or 0
        if out < cfg.fanout_min_rows:
            continue
        inputs = [c.get("actual_rows") or 0 for c in node.get("children") or []]
        biggest = max(inputs) if inputs else 0
        ratio = out / biggest if biggest else float("inf")
        loop = op in _LOOP_JOINS and op != "PIECEWISE_MERGE_JOIN"
        if ratio >= cfg.fanout_ratio or (loop and out >= cfg.fanout_min_rows):
            if worst is None or ratio > worst[0]:
                worst = (ratio, node, inputs, loop)
    if worst is None:
        return None
    ratio, node, inputs, loop = worst
    extra = node.get("extra_info") or {}
    cond = extra.get("Conditions") or ""
    cond = ", ".join(cond) if isinstance(cond, list) else str(cond)
    op = node["operator"]
    out = node.get("actual_rows") or 0
    if loop:
        why = (
            f"A {op} has no equality condition to hash on, so every row on one side is "
            f"compared with every row on the other: {' x '.join(_rows(i) for i in inputs)} "
            f"inputs produced {_rows(out)} rows."
        )
    else:
        why = (
            f"A {op} ({cond or 'no condition shown'}) turned inputs of "
            f"{' and '.join(_rows(i) for i in inputs)} rows into {_rows(out)} rows, "
            f"{ratio:.0f}x the larger side. The join key is not unique on either side."
        )
    return AdviceItem(
        rule="join_fanout",
        model=model.full_name,
        severity="high" if ratio >= 10 * cfg.fanout_ratio or loop else "medium",
        title=f"Join fan-out: {_rows(out)} rows from {_rows(max(inputs) if inputs else 0)}",
        explanation=why + " This is usually a missing column in the ON clause or a many-to-many relation.",
        suggestion=(
            "Check the grain of both sides on the join columns (an @grain or unique() assertion "
            "makes it explicit), add the missing key column to ON, or aggregate one side to the "
            "join key before joining."
        ),
        evidence={
            "operator": op,
            "conditions": cond,
            "join_type": extra.get("Join Type"),
            "input_rows": inputs,
            "output_rows": out,
            "ratio": None if ratio == float("inf") else round(ratio, 1),
            "time_ms": node.get("actual_time_ms"),
        },
    )


def _heavy_constructs(ast: Any) -> list[str]:
    from sqlglot import exp

    if ast is None:
        return []
    found = []
    if ast.find(exp.Join):
        found.append("join")
    if ast.find(exp.Group):
        found.append("GROUP BY")
    if ast.find(exp.Window):
        found.append("window function")
    if ast.find(exp.Distinct):
        found.append("DISTINCT")
    return found


def rule_materialize_view(ctx: AdviceContext, model: Any) -> AdviceItem | None:
    if model.materialized != "view":
        return None
    readers = ctx.consumers.get(model.full_name, [])
    if len(readers) < ctx.cfg.view_consumers:
        return None
    heavy = _heavy_constructs(model.ast)
    if not heavy:
        return None
    reader_ms = sum(float((ctx.builds.get(r) or {}).get("duration_ms") or 0) for r in readers)
    return AdviceItem(
        rule="materialize_view",
        model=model.full_name,
        severity="medium" if reader_ms >= ctx.cfg.min_duration_ms else "low",
        title=f"View with {', '.join(heavy)} is recomputed by {len(readers)} models",
        explanation=(
            f"{model.full_name} is a view, so its {', '.join(heavy)} runs again inside every "
            f"model that reads it: {', '.join(sorted(readers))}. Their latest builds took "
            f"{_ms(reader_ms)} together. Materialized as a table it is computed once per run."
        ),
        suggestion="@config materialized=table",
        evidence={"readers": sorted(readers), "constructs": heavy, "readers_build_ms": round(reader_ms)},
    )


def _referenced_elsewhere(ctx: AdviceContext, name: str, since: datetime) -> list[str]:
    """Where a model is read outside the DAG: audit log, slow queries, dashboards, metrics."""
    places: list[str] = []
    like = f"%{name}%"
    probes = (
        ("audit log", "SELECT count(*) FROM _havn.audit_log WHERE action = 'query' "
                      "AND lower(resource) LIKE ? AND \"timestamp\" >= ?", True),
        ("slow query log", "SELECT count(*) FROM _havn.slow_queries "
                           "WHERE lower(query_text) LIKE ? AND executed_at >= ?", True),
        ("dashboard", "SELECT count(*) FROM _havn.dashboard_widgets WHERE lower(sql_query) LIKE ?", False),
    )
    for label, sql, timed in probes:
        try:
            params = [like, since] if timed else [like]
            if (ctx.conn.execute(sql, params).fetchone() or [0])[0]:
                places.append(label)
        except duckdb.Error:
            continue
    if ctx.project_dir is not None:
        from havn.textio import read_project_text

        metrics_dir = Path(ctx.project_dir) / "metrics"
        if metrics_dir.is_dir():
            for f in list(metrics_dir.glob("*.yml")) + list(metrics_dir.glob("*.yaml")):
                try:
                    if name in read_project_text(f).lower():
                        places.append("metric definition")
                        break
                except OSError:
                    continue
    return places


def rule_unused_table(ctx: AdviceContext, model: Any) -> AdviceItem | None:
    if model.materialized not in ("table", "incremental", "snapshot"):
        return None
    name = model.full_name
    if ctx.consumers.get(name) or name in ctx.exposure_refs:
        return None
    days = int(ctx.cfg.unused_days)
    since = local_now() - timedelta(days=days)
    try:
        first = ctx.conn.execute(
            "SELECT min(started_at) FROM _havn.run_log WHERE target = ? AND run_type = 'transform'",
            [name],
        ).fetchone()[0]
    except duckdb.Error:
        first = None
    if first is None or first > since:
        return None  # too young to call unused
    if _referenced_elsewhere(ctx, name, since):
        return None
    hist = ctx.history(name)
    rows = hist[0].get("rows_out") if hist else None
    ms = _build_ms(ctx, name)
    suggestion = (
        "Delete the model if nothing needs it. If it is read occasionally, make it a view "
        "(@config materialized=view) so it costs nothing to keep. If a tool outside havn reads "
        "it, declare it in exposures.yml and this advice goes away."
    )
    return AdviceItem(
        rule="unused_table",
        model=name,
        severity="low" if ms < ctx.cfg.min_duration_ms else "medium",
        title=f"Nothing has read this {model.materialized} in {days} days",
        explanation=(
            f"{name} is rebuilt ({_ms(ms)} median, {_rows(rows)} rows) but no model reads it, no "
            f"exposure declares it, no dashboard or metric uses it, and no query in the audit or "
            f"slow-query log has mentioned it in {days} days. Queries run from the CLI are not "
            "audited, so check before deleting."
        ),
        suggestion=suggestion,
        evidence={"days": days, "median_build_ms": round(ms), "rows": rows, "first_built": str(first)},
    )


_SCAN_OPS = ("SEQ_SCAN", "TABLE_SCAN", "READ_PARQUET", "PARQUET_SCAN", "READ_CSV", "READ_CSV_AUTO")


def rule_scan_small_slice(ctx: AdviceContext, model: Any) -> AdviceItem | None:
    build = ctx.builds.get(model.full_name) or {}
    plan = build.get("plan")
    if not plan:
        return None
    cfg = ctx.cfg
    if float(build.get("duration_ms") or 0) < cfg.min_duration_ms:
        return None
    worst = None

    def walk(node: dict, parent: dict | None) -> None:
        nonlocal worst
        op = node.get("operator", "")
        scanned = node.get("rows_scanned") or 0
        if (op in _SCAN_OPS or op.endswith("_SCAN")) and scanned >= cfg.big_table_rows:
            kept = node.get("actual_rows") or 0
            # A FILTER right above the scan holds a predicate the scan could not take.
            filt = parent if parent and parent.get("operator") == "FILTER" else None
            if filt is not None:
                kept = filt.get("actual_rows") or 0
            share = kept / scanned if scanned else 1.0
            if share <= cfg.scan_selectivity and (worst is None or scanned > worst[1]):
                worst = (node, scanned, kept, share, filt)
        for child in node.get("children") or []:
            walk(child, node)

    walk(plan, None)
    if worst is None:
        return None
    node, scanned, kept, share, filt = worst
    table = node.get("table") or "a table"
    extra = node.get("extra_info") or {}
    pushed = extra.get("Filters")
    pushed = ", ".join(pushed) if isinstance(pushed, list) else pushed
    if filt is not None:
        fx = (filt.get("extra_info") or {})
        pred = fx.get("Expression") or fx.get("Filters") or ""
        pred = ", ".join(pred) if isinstance(pred, list) else str(pred)
        why = (
            f"The scan of {table} read {_rows(scanned)} rows and a FILTER above it kept "
            f"{_rows(kept)} ({100 * share:.2f}%). The predicate ({pred[:120] or 'see plan'}) was not "
            "pushed into the scan, typically because it wraps the column in a function or a cast, "
            "so zone maps cannot skip anything."
        )
        fix = "Compare the raw column (col >= DATE '2024-01-01' rather than CAST(col AS VARCHAR) LIKE '2024%'). "
    else:
        why = (
            f"The scan of {table} read {_rows(scanned)} rows to keep {_rows(kept)} "
            f"({100 * share:.2f}%)"
            + (f" with the filter {pushed[:120]} pushed into it" if pushed else "")
            + ". The rows it needs are spread across the whole table, so DuckDB cannot skip "
            "row groups."
        )
        fix = ""
    if model.materialized == "table":
        fix += (
            "If this model only needs the new slice of the upstream on each run, make it "
            "incremental with an incremental_filter (or @watermark) on that column."
        )
    else:
        fix += "Sort or partition the upstream on the filter column (partition_by) so the slice is contiguous."
    return AdviceItem(
        rule="scan_small_slice",
        model=model.full_name,
        severity="medium",
        title=f"Reads {_rows(scanned)} rows of {table} to keep {_rows(kept)}",
        explanation=why,
        suggestion=fix.strip(),
        evidence={
            "table": node.get("table"),
            "rows_scanned": scanned,
            "rows_kept": kept,
            "share_pct": round(100 * share, 3),
            "pushed_filter": pushed,
            "filter_above_scan": filt is not None,
            "scan_ms": node.get("actual_time_ms"),
        },
    )


def rule_order_by_non_final(ctx: AdviceContext, model: Any) -> AdviceItem | None:
    readers = ctx.consumers.get(model.full_name, [])
    if not readers or model.materialized == "ephemeral":
        return None
    select = _top_level_select(model.ast)
    if select is None or not select.args.get("order") or select.args.get("limit"):
        return None
    order_sql = select.args["order"].sql(dialect="duckdb")
    sort_ms = None
    plan = (ctx.builds.get(model.full_name) or {}).get("plan")
    for node, _d, _p in iter_nodes(plan):
        if node.get("operator") == "ORDER_BY":
            sort_ms = (sort_ms or 0) + float(node.get("actual_time_ms") or 0)
    if model.materialized == "view":
        cost = "runs inside every model that reads it"
    else:
        cost = f"is paid on every build{f' ({_ms(sort_ms)})' if sort_ms else ''}"
    return AdviceItem(
        rule="order_by_non_final",
        model=model.full_name,
        severity="medium" if (sort_ms or 0) >= ctx.cfg.min_duration_ms else "low",
        title="ORDER BY in a model other models read",
        explanation=(
            f"{model.full_name} ends with {order_sql}, and {len(readers)} model(s) read it "
            f"({', '.join(sorted(readers)[:5])}). SQL gives no row order to the next query, "
            f"so the sort {cost} and buys nothing."
        ),
        suggestion="Remove the ORDER BY here; sort in the final query or in the BI tool.",
        evidence={"order_by": order_sql, "readers": sorted(readers), "sort_ms": sort_ms},
    )


def rule_distinct_large(ctx: AdviceContext, model: Any) -> AdviceItem | None:
    select = _top_level_select(model.ast)
    if select is None or not select.args.get("distinct"):
        return None
    cfg = ctx.cfg
    build = ctx.builds.get(model.full_name) or {}
    rows_in = None
    rows_out = build.get("rows_out")
    plan = build.get("plan")
    for node, _d, _p in iter_nodes(plan):
        if node.get("operator") in ("HASH_GROUP_BY", "PERFECT_HASH_GROUP_BY"):
            kids = node.get("children") or []
            if kids and kids[0].get("actual_rows") is not None:
                rows_in = max(rows_in or 0, int(kids[0]["actual_rows"]))
    size = rows_in or build.get("rows_in") or rows_out or 0
    if size < cfg.big_table_rows:
        return None
    if float(build.get("duration_ms") or 0) < cfg.min_duration_ms:
        return None
    removed = ""
    if rows_in and rows_out is not None:
        removed = f" It took {_rows(rows_in)} rows in and kept {_rows(rows_out)}."
    return AdviceItem(
        rule="distinct_large",
        model=model.full_name,
        severity="medium",
        title=f"SELECT DISTINCT over {_rows(size)} rows",
        explanation=(
            f"{model.full_name} de-duplicates with SELECT DISTINCT, which hashes every column "
            f"of every row.{removed} Duplicates at this size usually come from a join upstream "
            "that multiplies rows, and DISTINCT hides it instead of fixing it."
        ),
        suggestion=(
            "Find where the duplicates come from (often a join fan-out). If they are real, "
            "GROUP BY the key columns or keep one row per key with "
            "QUALIFY row_number() OVER (PARTITION BY <key> ORDER BY <ts> DESC) = 1."
        ),
        evidence={"rows_in": rows_in, "rows_out": rows_out, "build_ms": build.get("duration_ms")},
    )


def _called_functions(ast: Any) -> set[str]:
    from sqlglot import exp

    if ast is None:
        return set()
    names: set[str] = set()
    for fn in ast.find_all(exp.Func):
        name = fn.name if isinstance(fn, exp.Anonymous) else (fn.sql_name() if hasattr(fn, "sql_name") else "")
        if name:
            names.add(str(name).lower())
    return names


def rule_python_udf_hot_path(ctx: AdviceContext, model: Any) -> AdviceItem | None:
    if not ctx.python_udfs:
        return None
    used = sorted(_called_functions(model.ast) & ctx.python_udfs)
    if not used:
        return None
    build = ctx.builds.get(model.full_name) or {}
    rows = max(int(build.get("rows_out") or 0), int(build.get("rows_produced") or 0))
    if rows < ctx.cfg.udf_rows:
        return None
    ms = float(build.get("duration_ms") or 0)
    return AdviceItem(
        rule="python_udf_hot_path",
        model=model.full_name,
        severity="medium" if ms >= ctx.cfg.min_duration_ms else "low",
        title=f"Python UDF {', '.join(used)} over {_rows(rows)} rows",
        explanation=(
            f"{model.full_name} calls the Python macro(s) {', '.join(used)} on about {_rows(rows)} "
            f"rows (build {_ms(ms)}). A Python UDF leaves DuckDB's vectorised engine and runs in "
            "Python for every row, typically ten to a hundred times slower than the same logic in SQL."
        ),
        suggestion=(
            "Rewrite it as a SQL macro (CREATE MACRO in macros/*.sql) if it is expressible in SQL, "
            "or apply it after aggregation to fewer rows."
        ),
        evidence={"functions": used, "rows": rows, "build_ms": ms},
    )


_RULE_FUNCS: dict[str, Callable[[AdviceContext, Any], AdviceItem | None]] = {
    "incremental_candidate": rule_incremental_candidate,
    "join_fanout": rule_join_fanout,
    "materialize_view": rule_materialize_view,
    "unused_table": rule_unused_table,
    "scan_small_slice": rule_scan_small_slice,
    "order_by_non_final": rule_order_by_non_final,
    "distinct_large": rule_distinct_large,
    "python_udf_hot_path": rule_python_udf_hot_path,
}


# ---------------------------------------------------------------------------
# Dismiss / snooze
# ---------------------------------------------------------------------------


def set_advice_state(
    conn: duckdb.DuckDBPyConnection,
    model: str,
    rule: str,
    status: str,
    *,
    days: float | None = None,
    note: str | None = None,
    user: str | None = None,
) -> dict:
    """Dismiss (``dismissed``), snooze (``snoozed`` for ``days``) or reopen (``open``) an item."""
    if rule not in RULES:
        raise ValueError(f"Unknown advice rule '{rule}'. Known: {', '.join(RULES)}")
    if status not in ("dismissed", "snoozed", "open"):
        raise ValueError("status must be dismissed, snoozed or open")
    if status == "snoozed" and not days:
        raise ValueError("A snooze needs a number of days")
    ensure_perf_tables(conn)
    model = model.lower()
    conn.execute("DELETE FROM _havn.perf_advice_state WHERE model_path = ? AND rule = ?", [model, rule])
    until = None
    if status != "open":
        until = local_now() + timedelta(days=float(days)) if status == "snoozed" else None
        conn.execute(
            "INSERT INTO _havn.perf_advice_state (model_path, rule, status, until, note, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [model, rule, status, until, note, user, local_now()],
        )
    return {"model": model, "rule": rule, "status": status, "until": until.isoformat() if until else None}


def _advice_states(conn: duckdb.DuckDBPyConnection) -> dict[tuple[str, str], dict]:
    out = {}
    for r in _fetch(conn, "SELECT model_path, rule, status, until FROM _havn.perf_advice_state"):
        out[(r["model_path"], r["rule"])] = r
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _python_udf_names(project_dir: Path | None) -> set[str]:
    if project_dir is None:
        return set()
    try:
        from havn.engine.macros import list_macros

        return {m["name"].lower() for m in list_macros(Path(project_dir)) if m.get("kind") in ("scalar", "table")}
    except Exception as e:
        logger.debug("Could not list macros for advice: %s", e)
        return set()


def build_context(
    conn: duckdb.DuckDBPyConnection,
    project_dir: Path | None,
    cfg: Any,
    models: list[Any] | None = None,
    exposures: list[Any] | None = None,
) -> AdviceContext:
    if models is None:
        models = []
        if project_dir is not None:
            from havn.engine.transform.discovery import discover_all_models

            models = discover_all_models(Path(project_dir))
    by_name = {m.full_name: m for m in models}
    consumers: dict[str, list[str]] = {}
    for m in models:
        for dep in m.depends_on:
            consumers.setdefault(dep, []).append(m.full_name)
    exposure_refs: set[str] = set()
    for e in exposures or []:
        for dep in getattr(e, "depends_on", []) or []:
            exposure_refs.add(str(dep).lower())
    return AdviceContext(
        conn=conn,
        project_dir=Path(project_dir) if project_dir else None,
        cfg=cfg,
        models=by_name,
        consumers=consumers,
        builds=latest_builds(conn),
        exposure_refs=exposure_refs,
        python_udfs=_python_udf_names(project_dir),
    )


def compute_advice(
    conn: duckdb.DuckDBPyConnection,
    project_dir: Path | None,
    cfg: Any,
    *,
    model: str | None = None,
    include_dismissed: bool = False,
    models: list[Any] | None = None,
    exposures: list[Any] | None = None,
    rules: list[str] | None = None,
) -> list[AdviceItem]:
    """Run every rule over every model (or one), most severe first."""
    if cfg is not None and not getattr(cfg, "enabled", True):
        return []
    ctx = build_context(conn, project_dir, cfg, models=models, exposures=exposures)
    states = _advice_states(conn)
    now = local_now()
    targets = [ctx.models[model.lower()]] if model and model.lower() in ctx.models else (
        [] if model else list(ctx.models.values())
    )
    items: list[AdviceItem] = []
    for m in targets:
        for rule_name, fn in _RULE_FUNCS.items():
            if rules and rule_name not in rules:
                continue
            try:
                item = fn(ctx, m)
            except Exception as e:  # a rule tripping on odd SQL must not hide the rest
                logger.debug("Advice rule %s failed on %s: %s", rule_name, m.full_name, e)
                continue
            if item is None:
                continue
            state = states.get((item.model, item.rule))
            if state:
                until = state.get("until")
                until_dt = datetime.fromisoformat(until) if isinstance(until, str) and until else None
                if state["status"] == "dismissed":
                    item.status = "dismissed"
                elif state["status"] == "snoozed" and until_dt and until_dt > now:
                    item.status = "snoozed"
                    item.snoozed_until = until
            if item.status != "open" and not include_dismissed:
                continue
            items.append(item)
    items.sort(key=lambda i: (_SEVERITY_ORDER.get(i.severity, 9), i.model, i.rule))
    return items
