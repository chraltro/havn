"""One shape for DuckDB's JSON query profile, whichever DuckDB wrote it.

DuckDB 1.x writes a flat root (``latency``, ``cpu_time``,
``cumulative_rows_scanned``, ``system_peak_buffer_memory``, ``query_name``,
...) with the operator tree under ``children``, each operator carrying
``operator_name``, ``operator_type``, ``operator_cardinality``,
``operator_rows_scanned`` and ``operator_timing``.

DuckDB 2.0 groups the root into ``query``, ``system`` and ``io`` sections,
puts the operator tree under ``operator``, names operator fields ``name``,
``type``, ``intermediate_rows``, ``rows_scanned`` and ``timing``, and wraps
a SELECT's plan in a ``RESULT_COLLECTOR`` operator.

:func:`normalize_profile` turns either into the 1.x shape, which is what
EXPLAIN ANALYZE parsing (:mod:`havn.engine.explain`) and build capture
(:mod:`havn.engine.perf.capture`) read. It detects the shape from the
document, not from the installed version.
"""

from __future__ import annotations

from typing import Any

# 2.0 section -> {2.0 key: 1.x root key}
_ROOT_FIELDS: dict[str, dict[str, str]] = {
    "query": {
        "sql": "query_name",
        "total_time": "latency",
        "cpu_time": "cpu_time",
        "total_rows_scanned": "cumulative_rows_scanned",
    },
    "system": {
        "peak_buffer_memory": "system_peak_buffer_memory",
        "peak_temp_dir_size": "system_peak_temp_dir_size",
        "total_memory_allocated": "total_memory_allocated",
        "total_bytes_spilled": "total_bytes_spilled",
        "blocked_thread_time": "blocked_thread_time",
    },
    "io": {
        "total_bytes_read": "total_bytes_read",
        "total_bytes_written": "total_bytes_written",
    },
}

# Set on a normalised profile whose peak memory / temp-dir figures describe
# that one query (DuckDB 2.0), rather than the connection's high-water mark.
PER_QUERY_PEAKS = "per_query_peaks"

_NODE_FIELDS = {
    "name": "operator_name",
    "type": "operator_type",
    "intermediate_rows": "operator_cardinality",
    "rows_scanned": "operator_rows_scanned",
    "timing": "operator_timing",
}


def is_sectioned(profile: Any) -> bool:
    """True for a DuckDB 2.0-style profile (operator tree under ``operator``)."""
    return isinstance(profile, dict) and isinstance(profile.get("operator"), list)


def normalize_profile(profile: dict) -> dict:
    """Return ``profile`` in the DuckDB 1.x shape (unchanged if it already is)."""
    if not is_sectioned(profile):
        return profile
    out: dict = {}
    for section, mapping in _ROOT_FIELDS.items():
        values = profile.get(section)
        if not isinstance(values, dict):
            continue
        for new_key, old_key in mapping.items():
            if new_key in values:
                out[old_key] = values[new_key]
    # 1.x's system_peak_* were high-water marks for the connection that never
    # went down; 2.0's peaks are this query's own, and it reports what the
    # query spilled as total_bytes_spilled. Readers that compensated for the
    # 1.x marks (perf capture) check this flag.
    out[PER_QUERY_PEAKS] = True
    out["extra_info"] = {}
    out["children"] = [_normalize_node(n) for n in _unwrap(profile["operator"])]
    return out


def _unwrap(nodes: list) -> list:
    """Drop RESULT_COLLECTOR wrappers, which 1.x profiles never showed."""
    flat: list = []
    for node in nodes:
        if isinstance(node, dict) and node.get("type") == "RESULT_COLLECTOR":
            flat.extend(_unwrap(node.get("children") or []))
        else:
            flat.append(node)
    return flat


def _normalize_node(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key == "children":
            continue
        out[_NODE_FIELDS.get(key, key)] = value
    out["children"] = [_normalize_node(c) for c in node.get("children") or []]
    return out
