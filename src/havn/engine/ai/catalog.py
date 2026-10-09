"""What ``havn ask`` knows about the project, and what it tells the model.

Everything here is metadata: metric definitions, the names, types and
``@col`` docs of dimensions, model descriptions. Row data is never part of the
catalog unless ``ai.share_dimension_values`` is on, and even then only the
distinct values of declared dimensions, read through the governed read path
so masking applies to them.

The same module answers the "where did this number come from" half of an
answer: :func:`lineage_for` walks a metric's model back to its sources and
:func:`freshness_for` reports when each model in that chain was last built.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Any, Callable

from havn.engine.semantic import MetricDef

logger = logging.getLogger("havn.ai")

_MAX_DIM_VALUES = 25
_MAX_CATALOG_MODELS = 60
_MAX_MODEL_COLUMNS = 40

_STOPWORDS = {
    "the", "and", "for", "with", "what", "was", "were", "how", "many", "much",
    "per", "show", "give", "from", "this", "that", "last", "each", "all", "our",
    "are", "did", "does", "which", "who", "when", "where", "over", "into", "total",
    "top", "list", "get", "find", "only", "now", "month", "year", "week", "day",
    "quarter", "by", "is", "me", "of", "in", "on", "to", "a", "an",
}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    out = set()
    for w in words:
        if len(w) < 3 or w in _STOPWORDS:
            continue
        out.add(w)
        if w.endswith("s") and len(w) > 3:
            out.add(w[:-1])
    return out


def rank_metrics(question: str, metrics: dict[str, MetricDef], top: int = 3) -> list[dict]:
    """Metrics ordered by word overlap with the question (a deterministic hint).

    Used when a question cannot be answered: the model's own list of closest
    metrics is checked against the catalog, and this ranking fills the gap
    when it named none, so "closest metrics" never depends on the model alone.
    """
    q = _tokens(question)
    scored = []
    for m in metrics.values():
        name_tokens = _tokens(m.name.replace("_", " "))
        other = _tokens(" ".join([m.description, m.model, *m.dimensions]))
        score = 3 * len(q & name_tokens) + len(q & other)
        if score:
            scored.append((score, m.name))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [
        {"name": name, "description": metrics[name].description, "score": score}
        for score, name in scored[:top]
    ]


def _split(name: str) -> tuple[str, str] | None:
    parts = name.lower().split(".")
    return (parts[0], parts[1]) if len(parts) == 2 else None


def fetch_column_types(
    read: Callable[[str], Any],
    tables: list[str],
) -> dict[str, list[tuple[str, str]]]:
    """``{"gold.orders": [(column, type), ...]}`` for the named tables.

    ``read`` runs a SQL string through the governed read path and returns an
    object with ``rows``. information_schema is metadata, but going through
    the same path keeps a single way in.
    """
    from havn.engine.utils import validate_identifier

    pairs = []
    for t in tables:
        sp = _split(t)
        if sp is None:
            continue
        try:
            validate_identifier(sp[0], "schema")
            validate_identifier(sp[1], "table")
        except ValueError:
            continue
        pairs.append(sp)
    if not pairs:
        return {}
    cond = " OR ".join(
        f"(lower(table_schema) = '{s}' AND lower(table_name) = '{n}')" for s, n in pairs
    )
    sql = (
        "SELECT lower(table_schema), lower(table_name), column_name, data_type "
        "FROM information_schema.columns "
        "WHERE table_catalog = current_database() AND (" + cond + ") "
        "ORDER BY table_schema, table_name, ordinal_position"
    )
    try:
        result = read(sql)
    except Exception as e:
        logger.debug("Could not read column types: %s", e)
        return {}
    out: dict[str, list[tuple[str, str]]] = {}
    for schema, table, col, dtype in result.rows:
        out.setdefault(f"{schema}.{table}", []).append((str(col), str(dtype)))
    return out


_TEXTUAL = ("VARCHAR", "TEXT", "STRING", "CHAR", "ENUM", "BOOL")


def fetch_dimension_values(
    read: Callable[[str], Any],
    metrics: dict[str, MetricDef],
    column_types: dict[str, list[tuple[str, str]]],
) -> dict[str, dict[str, list]]:
    """Distinct values of textual dimensions: ``{model: {dimension: [...]}}``.

    Only called when ``ai.share_dimension_values`` is on. Values go through the
    governed read path, so a masked dimension arrives masked.
    """
    out: dict[str, dict[str, list]] = {}
    seen: set[tuple[str, str]] = set()
    for m in metrics.values():
        types = {c.lower(): t.upper() for c, t in column_types.get(m.model.lower(), [])}
        for d in m.dimensions:
            if (m.model, d) in seen:
                continue
            seen.add((m.model, d))
            dtype = types.get(d.lower(), "")
            if dtype and not any(k in dtype for k in _TEXTUAL):
                continue
            model_ref = ".".join(f'"{p}"' for p in m.model.split("."))
            sql = (
                f'SELECT DISTINCT "{d}" FROM {model_ref} WHERE "{d}" IS NOT NULL '
                f'ORDER BY 1 LIMIT {_MAX_DIM_VALUES + 1}'
            )
            try:
                result = read(sql)
            except Exception as e:
                logger.debug("Could not sample %s.%s: %s", m.model, d, e)
                continue
            values = [r[0] for r in result.rows]
            out.setdefault(m.model, {})[d] = values[:_MAX_DIM_VALUES] + (
                ["..."] if len(values) > _MAX_DIM_VALUES else []
            )
    return out


def build_catalog(
    metrics: dict[str, MetricDef],
    models: list | None = None,
    *,
    column_types: dict[str, list[tuple[str, str]]] | None = None,
    dimension_values: dict[str, dict[str, list]] | None = None,
    today: date | None = None,
) -> dict:
    """The catalog the model chooses from, as a JSON-serialisable dict."""
    by_name = {m.full_name: m for m in (models or [])}
    column_types = column_types or {}
    dimension_values = dimension_values or {}

    def col_type(model: str, col: str) -> str | None:
        for c, t in column_types.get(model.lower(), []):
            if c.lower() == col.lower():
                return t
        return None

    def col_doc(model: str, col: str) -> str:
        sql_model = by_name.get(model.lower())
        if sql_model is None:
            return ""
        return sql_model.column_docs.get(col, "") or sql_model.column_docs.get(col.lower(), "")

    metric_entries = []
    for m in metrics.values():
        dims = []
        for d in m.dimensions:
            entry: dict[str, Any] = {"name": d}
            t = col_type(m.model, d)
            if t:
                entry["type"] = t
            doc = col_doc(m.model, d)
            if doc:
                entry["description"] = doc
            values = dimension_values.get(m.model, {}).get(d)
            if values:
                entry["values"] = values
            dims.append(entry)
        metric_entries.append({
            "name": m.name,
            "description": m.description,
            "model": m.model,
            "measure": m.measure,
            "dimensions": dims,
            "time_dimension": m.time_dimension,
            "always_applied_filters": list(m.filters),
        })

    # Models are listed so the model can suggest a metric definition when a
    # question cannot be answered. Consumption layers first.
    order = {"gold": 0, "silver": 1, "bronze": 2}
    model_entries = []
    for sm in sorted(models or [], key=lambda x: (order.get(x.schema, 3), x.full_name)):
        if sm.materialized == "ephemeral":
            continue
        cols = column_types.get(sm.full_name, [])
        model_entries.append({
            "name": sm.full_name,
            "description": sm.description,
            "columns": [
                {"name": c, "type": t, **({"description": sm.column_docs[c]} if c in sm.column_docs else {})}
                for c, t in cols[:_MAX_MODEL_COLUMNS]
            ],
        })
        if len(model_entries) >= _MAX_CATALOG_MODELS:
            break

    return {
        "today": (today or date.today()).isoformat(),
        "metrics": metric_entries,
        "models": model_entries,
    }


def lineage_for(model_name: str, models: list) -> dict:
    """The upstream chain of ``model_name`` down to its sources.

    Nodes that are not transform models (landing tables, seeds, declared
    sources) are the chain's sources.
    """
    by_name = {m.full_name: m for m in models}
    root = model_name.lower()
    nodes: dict[str, dict] = {}
    edges: list[list[str]] = []
    stack = [root]
    while stack:
        name = stack.pop()
        if name in nodes:
            continue
        m = by_name.get(name)
        if m is None:
            nodes[name] = {"name": name, "kind": "source", "depends_on": []}
            continue
        nodes[name] = {
            "name": name,
            "kind": "model",
            "materialized": m.materialized,
            "depends_on": list(m.depends_on),
            "path": _rel_path(m),
        }
        for dep in m.depends_on:
            edges.append([dep, name])
            stack.append(dep)
    return {
        "root": root,
        "nodes": list(nodes.values()),
        "edges": edges,
        "sources": sorted(n for n, v in nodes.items() if v["kind"] == "source"),
    }


def _rel_path(model) -> str:
    try:
        parts = Path(model.path).parts
        if "transform" in parts:
            return Path(*parts[parts.index("transform"):]).as_posix()
        if "havn_packages" in parts:
            return Path(*parts[parts.index("havn_packages"):]).as_posix()
    except Exception:
        pass
    return Path(model.path).name


def freshness_for(conn, model_names: list[str], max_age_hours: float) -> list[dict]:
    """Last build time and stale flag for each named model.

    Uses the same ``check_freshness`` that ``havn freshness`` and the Quality
    panel use, so "stale" means the same thing everywhere.
    """
    from havn.engine.transform.analysis import check_freshness

    wanted = [n.lower() for n in model_names]
    try:
        rows = check_freshness(conn, max_age_hours=max_age_hours)
    except Exception as e:
        logger.debug("Freshness unavailable: %s", e)
        rows = []
    by_model = {str(r.get("model", "")).lower(): r for r in rows}
    out = []
    for name in wanted:
        r = by_model.get(name)
        if r is None:
            out.append({
                "model": name,
                "last_run_at": None,
                "hours_since_run": None,
                "is_stale": None,
                "never_built": True,
            })
        else:
            out.append({
                "model": name,
                "last_run_at": r.get("last_run_at"),
                "hours_since_run": r.get("hours_since_run"),
                "is_stale": bool(r.get("is_stale")),
                "row_count": r.get("row_count"),
                "never_built": False,
            })
    return out
