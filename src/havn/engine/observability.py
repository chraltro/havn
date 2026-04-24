"""Central Prometheus metric registry for havn.

The registry is process-wide; callers import the metric objects and
``.inc()`` / ``.observe()`` / ``.set()`` them directly. Scrapers read the
text exposition from :func:`render_prometheus`.

Keeping everything in one module means there is one place to find every
metric name, one place to change labels, and no risk of accidentally
registering the same metric twice (which raises under prometheus_client).
"""

from __future__ import annotations

from typing import Any

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# One registry for the whole process. Tests that need isolation can call
# :func:`reset_registry` between runs.
REGISTRY = CollectorRegistry()


def _gauge(name: str, doc: str, labels: tuple[str, ...] = ()) -> Gauge:
    return Gauge(name, doc, labels, registry=REGISTRY)


def _counter(name: str, doc: str, labels: tuple[str, ...] = ()) -> Counter:
    return Counter(name, doc, labels, registry=REGISTRY)


def _histogram(name: str, doc: str, labels: tuple[str, ...] = (), buckets: tuple[float, ...] | None = None) -> Histogram:
    kwargs: dict[str, Any] = {"labelnames": labels, "registry": REGISTRY}
    if buckets is not None:
        kwargs["buckets"] = buckets
    return Histogram(name, doc, **kwargs)


# --- Query / transform latency ---------------------------------------------

QUERY_DURATION = _histogram(
    "havn_query_duration_seconds",
    "Duration of SQL queries executed through the resource manager.",
    labels=("category", "status"),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)

TRANSFORM_DURATION = _histogram(
    "havn_transform_duration_seconds",
    "Duration of a single SQL model build.",
    labels=("schema", "status"),
    buckets=(0.05, 0.25, 1, 5, 15, 60, 300, 900, 3600),
)

# --- Counters ---------------------------------------------------------------

QUERIES_TOTAL = _counter(
    "havn_queries_total",
    "Total queries executed, labeled by category and status.",
    labels=("category", "status"),
)

ROWS_PROCESSED = _counter(
    "havn_rows_processed_total",
    "Total rows written by transforms or ingested by streaming.",
    labels=("category",),
)

STREAMING_EVENTS = _counter(
    "havn_streaming_events_total",
    "Streaming events received (webhook / CDC / poll).",
    labels=("source", "status"),
)

# --- Gauges -----------------------------------------------------------------

ACTIVE_TASKS = _gauge(
    "havn_active_tasks",
    "Tasks currently executing, by resource manager category.",
    labels=("category",),
)

WAREHOUSE_SIZE = _gauge(
    "havn_warehouse_size_bytes",
    "Approximate warehouse size in bytes (DuckDB file size or DuckLake Parquet total).",
)

RESOURCE_BUDGET_MEMORY = _gauge(
    "havn_resource_budget_memory_bytes",
    "Configured memory budget per category.",
    labels=("category",),
)

RESOURCE_BUDGET_THREADS = _gauge(
    "havn_resource_budget_threads",
    "Configured thread budget per category.",
    labels=("category",),
)


def render_prometheus() -> bytes:
    """Return the Prometheus text exposition of every registered metric."""
    return generate_latest(REGISTRY)


def reset_registry() -> None:
    """Test helper — drop every metric and let the caller re-register."""
    # Prometheus client doesn't expose a public clear(); collectors live in
    # _names_to_collectors. Tests should only reach for this when they
    # need complete isolation (most tests can just assert counter deltas).
    for collector in list(REGISTRY._collector_to_names):  # type: ignore[attr-defined]
        REGISTRY.unregister(collector)
