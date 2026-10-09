"""Build-time regressions: a model that got slower than its own history.

The baseline is the model's last ``regression_lookback`` successful builds,
summarised by the median and the median absolute deviation (MAD) rather than
mean and standard deviation. One pathological run in the history (a cold
cache, a lock wait) drags a mean and inflates a standard deviation until
nothing looks unusual; the median and MAD barely move.

A build is a regression only when all three hold:

* its robust z-score, ``(x - median) / (1.4826 * MAD)``, reaches the threshold;
* it is at least ``regression_min_ratio`` times the median;
* it is at least ``regression_min_delta_ms`` slower in absolute terms.

The last two keep a 40 ms model that took 90 ms from paging anyone.

Durations are then normalised by rows when the history has a row basis
(rows scanned from the plan, else rows read from upstream, else rows
written). A model that took twice as long because it read twice the data
did not regress; it grew. Only a build that is also slower *per row* is
reported, and the report says so.

Each regression carries a plan diff between a representative fast build
(the captured plan closest to the median from below) and the slow one:
which operators got slower, and which join changed shape.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

import duckdb

from .capture import iter_nodes
from .store import ensure_perf_tables, get_build, local_now, model_history

logger = logging.getLogger("havn.perf")

_MAD_SCALE = 1.4826  # MAD -> standard deviation for normally distributed data
_FLOOR_FRACTION = 0.05  # a perfectly stable history still allows 5% of jitter


def median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    return float(ordered[mid]) if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def robust_z(value: float, history: list[float]) -> tuple[float, float, float]:
    """``(z, median, mad)`` of ``value`` against ``history``.

    The scale has a floor of 5% of the median, so a history of identical
    durations (MAD 0) does not turn a 1 ms wobble into an infinite z.
    """
    med = median(history)
    mad = median([abs(h - med) for h in history])
    scale = max(_MAD_SCALE * mad, _FLOOR_FRACTION * abs(med), 1e-9)
    return (value - med) / scale, med, mad


@dataclass
class Regression:
    model: str
    pipeline_run_id: str | None
    perf_id: str
    metric: str
    current_value: float
    baseline_median: float
    baseline_mad: float
    robust_z: float
    ratio: float
    normalized: bool
    message: str
    history_size: int
    baseline_perf_id: str | None = None
    plan_diff: dict | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _row_basis(build: dict) -> int | None:
    for key in ("rows_scanned", "rows_in", "rows_out"):
        v = build.get(key)
        if v:
            return int(v)
    return None


def _basis_key(current: dict, history: list[dict], min_history: int) -> str | None:
    """The row measure both the current build and enough of history have."""
    for key in ("rows_scanned", "rows_in", "rows_out"):
        if current.get(key) and sum(1 for h in history if h.get(key)) >= min_history:
            return key
    return None


def check_build(
    conn: duckdb.DuckDBPyConnection,
    perf_id: str,
    settings: Any,
) -> Regression | None:
    """Compare one recorded build with its model's history."""
    current = get_build(conn, perf_id)
    if current is None or current.get("status") != "success":
        return None
    lookback = int(getattr(settings, "regression_lookback", 20))
    min_history = int(getattr(settings, "regression_min_history", 5))
    threshold = float(getattr(settings, "regression_threshold", 3.5))
    min_ratio = float(getattr(settings, "regression_min_ratio", 1.5))
    min_delta = float(getattr(settings, "regression_min_delta_ms", 500))

    history = [
        h for h in model_history(conn, current["model_path"], limit=lookback + 1)
        if h["id"] != perf_id and h.get("finished_at", "") <= (current.get("finished_at") or "~")
    ][:lookback]
    if len(history) < min_history:
        return None

    cur_ms = float(current.get("duration_ms") or 0)
    durations = [float(h.get("duration_ms") or 0) for h in history]
    z, med, mad = robust_z(cur_ms, durations)
    ratio = cur_ms / med if med > 0 else float("inf")
    if not (z >= threshold and ratio >= min_ratio and cur_ms - med >= min_delta):
        return None

    metric = "duration_ms"
    normalized = False
    detail = ""
    key = _basis_key(current, history, min_history)
    if key:
        per = [
            float(h["duration_ms"] or 0) / (h[key] / 1000.0)
            for h in history if h.get(key)
        ]
        cur_per = cur_ms / (current[key] / 1000.0)
        zn, medn, _madn = robust_z(cur_per, per)
        ratio_n = cur_per / medn if medn > 0 else float("inf")
        if zn < threshold or ratio_n < min_ratio:
            # Slower, but in proportion to the data it read: growth, not a
            # regression. Said in the debug log so it can still be found.
            logger.debug(
                "%s took %.0f ms (%.1fx median) but per-row time is normal (%.2f vs %.2f ms per 1k %s)",
                current["model_path"], cur_ms, ratio, cur_per, medn, key,
            )
            return None
        normalized = True
        rows_label = key.replace("_", " ")
        detail = (
            f"; per 1k {rows_label}: {cur_per:.2f} ms vs a median of {medn:.2f} ms "
            f"({ratio_n:.1f}x), so this is not just more data"
        )

    message = (
        f"{current['model_path']} took {_fmt_ms(cur_ms)}, {ratio:.1f}x its median of "
        f"{_fmt_ms(med)} over the last {len(history)} builds (robust z {z:.1f}){detail}"
    )
    reg = Regression(
        model=current["model_path"],
        pipeline_run_id=current.get("pipeline_run_id"),
        perf_id=perf_id,
        metric=metric,
        current_value=cur_ms,
        baseline_median=med,
        baseline_mad=mad,
        robust_z=round(z, 2),
        ratio=round(ratio, 2),
        normalized=normalized,
        message=message,
        history_size=len(history),
    )

    baseline = _pick_baseline(conn, current["model_path"], history, med)
    if baseline is not None:
        reg.baseline_perf_id = baseline["id"]
        if baseline.get("plan") and current.get("plan"):
            reg.plan_diff = diff_plans(baseline["plan"], current["plan"])
            if reg.plan_diff.get("summary"):
                reg.message += ". " + reg.plan_diff["summary"][0]
    return reg


def _pick_baseline(
    conn: duckdb.DuckDBPyConnection, model: str, history: list[dict], med: float,
) -> dict | None:
    """A fast build with a plan: the one closest to the median from below."""
    with_plan = [h for h in history if h.get("plan_captured")]
    if not with_plan:
        return None
    at_or_below = [h for h in with_plan if float(h.get("duration_ms") or 0) <= med]
    pool = at_or_below or with_plan
    best = min(pool, key=lambda h: abs(float(h.get("duration_ms") or 0) - med))
    return get_build(conn, best["id"])


def _fmt_ms(ms: float) -> str:
    if ms >= 60_000:
        return f"{ms / 60_000:.1f} min"
    if ms >= 1000:
        return f"{ms / 1000:.1f} s"
    return f"{ms:.0f} ms"


def _fmt_rows(n: Any) -> str:
    if n is None:
        return "?"
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{int(n)}"


def _is_join(op: str) -> bool:
    return "JOIN" in op or op == "CROSS_PRODUCT"


def _keyed_nodes(plan: dict) -> dict[str, dict]:
    """Nodes keyed by ``operator|table#n``: stable across runs of the same SQL."""
    seen: Counter = Counter()
    out: dict[str, dict] = {}
    for node, _depth, _path in iter_nodes(plan):
        sig = f"{node.get('operator')}|{node.get('table') or ''}"
        seen[sig] += 1
        out[f"{sig}#{seen[sig]}"] = node
    return out


def _join_shape(node: dict) -> tuple:
    extra = node.get("extra_info") or {}
    return (
        node.get("operator"),
        str(extra.get("Join Type", "")),
        str(extra.get("Conditions", "")),
    )


def _label(node: dict) -> str:
    op = node.get("operator", "?")
    if node.get("table"):
        return f"{op} on {node['table']}"
    extra = node.get("extra_info") or {}
    if _is_join(op) and extra.get("Conditions"):
        cond = extra["Conditions"]
        cond = ", ".join(cond) if isinstance(cond, list) else str(cond)
        return f"{op} ({cond[:80]})"
    return op


def diff_plans(fast: dict, slow: dict) -> dict:
    """What changed between a fast build's plan and a slow one's.

    Returns ``operators`` (matched operators, largest slowdown first, with
    times and row counts on both sides), ``added`` / ``removed`` (operators
    only one plan has), ``join_changes`` (joins whose type or condition
    differ) and ``summary``, a few plain sentences for alerts and the CLI.
    """
    fast_nodes = _keyed_nodes(fast)
    slow_nodes = _keyed_nodes(slow)
    operators = []
    for key in fast_nodes.keys() & slow_nodes.keys():
        f, s = fast_nodes[key], slow_nodes[key]
        f_ms = float(f.get("actual_time_ms") or 0)
        s_ms = float(s.get("actual_time_ms") or 0)
        operators.append({
            "operator": s.get("operator"),
            "table": s.get("table"),
            "label": _label(s),
            "fast_ms": round(f_ms, 3),
            "slow_ms": round(s_ms, 3),
            "delta_ms": round(s_ms - f_ms, 3),
            "fast_rows": f.get("actual_rows"),
            "slow_rows": s.get("actual_rows"),
        })
    operators.sort(key=lambda o: o["delta_ms"], reverse=True)

    def _brief(node: dict) -> dict:
        return {
            "operator": node.get("operator"),
            "table": node.get("table"),
            "label": _label(node),
            "time_ms": node.get("actual_time_ms"),
            "rows": node.get("actual_rows"),
        }

    added = [_brief(slow_nodes[k]) for k in slow_nodes.keys() - fast_nodes.keys()]
    removed = [_brief(fast_nodes[k]) for k in fast_nodes.keys() - slow_nodes.keys()]

    fast_joins = Counter(_join_shape(n) for n in fast_nodes.values() if _is_join(n.get("operator", "")))
    slow_joins = Counter(_join_shape(n) for n in slow_nodes.values() if _is_join(n.get("operator", "")))
    join_changes = []
    gone = list((fast_joins - slow_joins).elements())
    new = list((slow_joins - fast_joins).elements())
    for i in range(max(len(gone), len(new))):
        before = gone[i] if i < len(gone) else None
        after = new[i] if i < len(new) else None
        join_changes.append({
            "before": {"operator": before[0], "join_type": before[1], "conditions": before[2]} if before else None,
            "after": {"operator": after[0], "join_type": after[1], "conditions": after[2]} if after else None,
        })

    total_fast = sum(float(n.get("actual_time_ms") or 0) for n in fast_nodes.values())
    total_slow = sum(float(n.get("actual_time_ms") or 0) for n in slow_nodes.values())

    summary: list[str] = []
    for jc in join_changes:
        b, a = jc["before"], jc["after"]
        if b and a:
            summary.append(
                f"The join changed: {b['operator']} ({b['conditions'] or 'no condition'}) "
                f"became {a['operator']} ({a['conditions'] or 'no condition'})"
            )
        elif a:
            summary.append(f"A new {a['operator']} appeared ({a['conditions'] or 'no condition'})")
        elif b:
            summary.append(f"The {b['operator']} ({b['conditions'] or 'no condition'}) is gone")
    for op in operators[:3]:
        if op["delta_ms"] <= max(1.0, 0.1 * max(total_slow - total_fast, 0)):
            continue
        rows = ""
        if op["fast_rows"] != op["slow_rows"]:
            rows = f", {_fmt_rows(op['fast_rows'])} -> {_fmt_rows(op['slow_rows'])} rows"
        summary.append(
            f"{op['label']} got {_fmt_ms(op['delta_ms'])} slower "
            f"({_fmt_ms(op['fast_ms'])} -> {_fmt_ms(op['slow_ms'])}{rows})"
        )
    return {
        "operators": operators[:15],
        "added": added,
        "removed": removed,
        "join_changes": join_changes,
        "total_fast_ms": round(total_fast, 3),
        "total_slow_ms": round(total_slow, 3),
        "summary": summary,
    }


def save_regression(conn: duckdb.DuckDBPyConnection, reg: Regression) -> None:
    ensure_perf_tables(conn)
    conn.execute(
        """
        INSERT INTO _havn.perf_regressions (
            id, model_path, pipeline_run_id, perf_id, baseline_perf_id, detected_at,
            metric, current_value, baseline_median, baseline_mad, robust_z, ratio,
            normalized, message, plan_diff, alerted
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE)
        """,
        [
            reg.id, reg.model, reg.pipeline_run_id, reg.perf_id, reg.baseline_perf_id,
            local_now(), reg.metric, reg.current_value, reg.baseline_median,
            reg.baseline_mad, reg.robust_z, reg.ratio, reg.normalized, reg.message,
            json.dumps(reg.plan_diff) if reg.plan_diff is not None else None,
        ],
    )


def detect_run_regressions(
    conn: duckdb.DuckDBPyConnection,
    perf_ids: list[str],
    settings: Any,
) -> list[Regression]:
    """Check every build of a run and record the regressions found."""
    found: list[Regression] = []
    for perf_id in perf_ids:
        try:
            reg = check_build(conn, perf_id, settings)
        except Exception as e:  # one odd row must not hide the others
            logger.debug("Regression check failed for %s: %s", perf_id, e)
            continue
        if reg is None:
            continue
        try:
            save_regression(conn, reg)
        except duckdb.Error as e:
            logger.debug("Could not save regression for %s: %s", reg.model, e)
        found.append(reg)
    return found


def alert_regressions(
    regressions: list[Regression],
    alerts_config: Any,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> int:
    """Send one ``perf_regression`` alert per regression; return how many went out."""
    if not regressions or alerts_config is None:
        return 0
    from havn.engine.alerts import Alert, AlertConfig, send_alert

    slack = getattr(alerts_config, "slack_webhook_url", None)
    webhook = getattr(alerts_config, "webhook_url", None)
    channels = list(getattr(alerts_config, "channels", []) or [])
    if not channels and not slack and not webhook:
        return 0
    sent = 0
    for reg in regressions:
        details: dict[str, Any] = {
            "model": reg.model,
            "duration": _fmt_ms(reg.current_value),
            "median": _fmt_ms(reg.baseline_median),
            "ratio": f"{reg.ratio}x",
            "robust_z": reg.robust_z,
        }
        if reg.plan_diff and reg.plan_diff.get("summary"):
            details["plan"] = "; ".join(reg.plan_diff["summary"][:3])
        send_alert(
            Alert(alert_type="perf_regression", target=reg.model, message=reg.message, details=details),
            # A fresh config each time: send_alert fills an empty channel
            # list in place from the webhook URLs.
            AlertConfig(slack_webhook_url=slack, webhook_url=webhook, channels=list(channels)),
            conn,
        )
        sent += 1
        if conn is not None:
            try:
                conn.execute("UPDATE _havn.perf_regressions SET alerted = TRUE WHERE id = ?", [reg.id])
            except duckdb.Error:
                pass
    return sent


def list_regressions(
    conn: duckdb.DuckDBPyConnection,
    days: int = 7,
    model: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Recorded regressions, newest first."""
    from .store import _fetch

    where = "detected_at >= current_timestamp - to_days(CAST(? AS INTEGER))"
    params: list = [int(days)]
    if model:
        where += " AND model_path = ?"
        params.append(model.lower())
    params.append(int(limit))
    return _fetch(
        conn,
        f"""
        SELECT id, model_path, pipeline_run_id, perf_id, baseline_perf_id, detected_at,
               metric, current_value, baseline_median, baseline_mad, robust_z, ratio,
               normalized, message, plan_diff, alerted
        FROM _havn.perf_regressions WHERE {where}
        ORDER BY detected_at DESC LIMIT ?
        """,
        params,
    )
