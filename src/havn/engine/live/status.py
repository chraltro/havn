"""What the live part of the warehouse looks like right now.

Read by ``havn live status``, ``GET /api/live/status``, the DAG's lag badges
and ``check_freshness``. Works from the warehouse alone, so it answers the
same whether or not a runner is up; a running runner adds what only it
knows (whether a refresh is in flight, its counters).

Lag is end-to-end: for a live model it is how long ago the oldest source
data it has not yet applied arrived in landing, or zero when it is caught
up. A model that is caught up is fresh no matter when it last refreshed --
no new data means nothing to do -- which is what freshness checks need to
hear about a live model.
"""

from __future__ import annotations

import logging
from datetime import datetime

import duckdb

from havn.engine.transform.models import SQLModel

from .graph import LiveGraph, tracked_sources
from .settings import LiveSettings
from .state import (
    all_consumed,
    events_per_second,
    load_states,
    oldest_origin,
    source_watermarks,
    utcnow,
)

logger = logging.getLogger("havn.live")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value is not None else None


def model_lags(
    conn: duckdb.DuckDBPyConnection, graph: LiveGraph, now: datetime | None = None
) -> dict[str, float]:
    """Current lag in seconds for every live model (views included)."""
    now = now or utcnow()
    consumed = all_consumed(conn)
    watermarks = source_watermarks(conn)
    lags: dict[str, float] = {}
    for name in graph.order:
        model = graph.models[name]
        if model.materialized != "incremental":
            continue
        mine = consumed.get(name, {})
        pending = {
            s: mine.get(s, 0) for s in graph.sources.get(name, [])
            if watermarks.get(s, 0) > mine.get(s, 0)
        }
        origin = oldest_origin(conn, pending) if pending else None
        lags[name] = max((now - origin).total_seconds(), 0.0) if origin else 0.0
    # A view reflects its inputs the moment they change, so it lags exactly
    # as far as the slowest live model it reads through.
    for name in graph.order:
        model = graph.models[name]
        if model.materialized == "incremental":
            continue
        ups = [s for s in tracked_sources(model, graph.models) if s in lags]
        lags[name] = max((lags[s] for s in ups), default=0.0)
    return lags


def display_status(
    state: dict | None, lag: float, held_by: str | None, behind: bool
) -> str:
    """One word for the UI: paused, failing, waiting, behind or live."""
    if state and state.get("paused"):
        return "paused"
    if state and state.get("status") == "failing":
        return "failing"
    if held_by:
        return "waiting"
    if behind or lag > 0:
        return "behind"
    return "live"


def live_status(
    conn: duckdb.DuckDBPyConnection,
    models: list[SQLModel],
    *,
    runner: dict | None = None,
    settings: LiveSettings | None = None,
) -> dict:
    """The full status payload (see the module docstring)."""
    settings = settings or LiveSettings()
    graph = LiveGraph.build(models)
    now = utcnow()
    try:
        states = {k: v.to_dict() for k, v in load_states(conn).items()}
        consumed = all_consumed(conn)
        watermarks = source_watermarks(conn)
        rates = events_per_second(conn)
        lags = model_lags(conn, graph, now)
        source_rows = conn.execute(
            "SELECT source, kind, watermark, rows_total, advanced_at FROM _havn.live_sources"
        ).fetchall() if watermarks or consumed else []
    except duckdb.CatalogException:
        states, consumed, watermarks, rates, lags, source_rows = {}, {}, {}, {}, {}, []

    runner_models = (runner or {}).get("models", {})
    held: dict[str, str] = {}
    out_models = []
    for name in graph.order:
        model = graph.models[name]
        state = dict(states.get(name) or {"model": name, "status": "active", "paused": False})
        state.update(runner_models.get(name, {}))
        ups = [s for s in graph.sources.get(name, []) if s in graph.sources]
        held_by = next(
            (u for u in ups
             if u in held
             or (states.get(u) or {}).get("paused")
             or (states.get(u) or {}).get("status") == "failing"),
            None,
        )
        if held_by or state.get("paused") or state.get("status") == "failing":
            held[name] = held_by or name
        inputs = []
        behind = False
        for src in graph.sources.get(name, []):
            have = consumed.get(name, {}).get(src, 0)
            wm = watermarks.get(src, 0)
            behind = behind or wm > have
            inputs.append({"source": src, "consumed": have, "watermark": wm, "behind": max(wm - have, 0)})
        lag = round(lags.get(name, 0.0), 3)
        out_models.append({
            "model": name,
            "materialized": model.materialized,
            "strategy": model.incremental_strategy if model.materialized == "incremental" else None,
            "cdc": bool(model.cdc_op),
            "live_interval": model.live_interval or None,
            "status": display_status(state, lag, held_by, behind),
            "waiting_on": held_by,
            "lag_seconds": lag,
            "events_per_second": round(sum(rates.get(i["source"], 0.0) for i in inputs), 3),
            "inputs": inputs,
            "paused": bool(state.get("paused")),
            "consecutive_failures": state.get("consecutive_failures", 0),
            "next_retry_at": state.get("next_retry_at"),
            "last_error": state.get("last_error"),
            "last_refresh_at": state.get("last_refresh_at"),
            "last_duration_ms": state.get("last_duration_ms", 0),
            "last_lag_ms": state.get("last_lag_ms"),
            "refreshes": state.get("refreshes", 0),
            "rows_total": state.get("rows_total", 0),
            "refreshing": bool(state.get("refreshing")),
        })

    sources = []
    for source, kind, wm, rows_total, advanced_at in sorted(source_rows):
        sources.append({
            "source": source,
            "kind": kind,
            "watermark": int(wm or 0),
            "rows_total": int(rows_total or 0),
            "advanced_at": _iso(advanced_at),
            "events_per_second": rates.get(source, 0.0),
            "consumers": graph.consumers.get(source, []),
        })

    return {
        "now": _iso(now),
        "runner": {k: v for k, v in (runner or {"running": False}).items() if k != "models"},
        "settings": settings.to_dict(),
        "models": out_models,
        "sources": sources,
        "max_lag_seconds": round(max((m["lag_seconds"] for m in out_models), default=0.0), 3),
    }


def live_freshness(
    conn: duckdb.DuckDBPyConnection, models: list[SQLModel]
) -> dict[str, float]:
    """``{live model: lag seconds}`` for ``check_freshness``; empty if none."""
    graph = LiveGraph.build(models)
    if not graph.order:
        return {}
    try:
        return model_lags(conn, graph)
    except duckdb.CatalogException:
        return {name: 0.0 for name in graph.order}
