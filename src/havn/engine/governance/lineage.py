"""Column lineage for governance: what each output column was made from.

``sql_analysis.extract_column_lineage`` answers "which source columns feed
this column". Governance needs one more fact per column: whether it is the
source column passed through unchanged (``passthrough``). Masking follows any
derivation (``upper(email)`` is still an email), but a row filter can only be
moved onto a downstream column that holds exactly the upstream value: a
``CASE`` that swaps regions would otherwise turn ``region = 'north'`` into
"show me the south".

What a built table holds is what its model said *when it was built*, so the
lineage of every model is recorded at build time in ``_havn.model_lineage``
(:func:`save_model_lineage`) and read back at query time. Editing a model
file without rebuilding it does not change what governance believes the
table contains.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import duckdb
from sqlglot import exp

logger = logging.getLogger("havn.governance")

# record = {"columns": {out: {"sources": [[table, col], ...], "passthrough": [t, c] | None}},
#           "reads": [table, ...], "traced": bool}


def _identity_source(node: Any) -> tuple[str, str] | None:
    """(table, column) when ``node`` is a bare column chain down to one table column."""
    from havn.engine.sql_analysis import _table_fqn, _unquote

    seen = 0
    while True:
        seen += 1
        if seen > 64:
            return None
        expression = node.expression
        if not node.downstream:
            source = node.source
            if isinstance(source, exp.Table) and source.db:
                column = _unquote(node.name.rpartition(".")[2])
                return (_table_fqn(source), column) if column and column != "*" else None
            return None
        inner = expression.this if isinstance(expression, exp.Alias) else expression
        if not isinstance(inner, exp.Column) or len(node.downstream) != 1:
            return None
        node = node.downstream[0]


def trace_columns(
    query: str,
    column_catalog: dict[str, list[str]] | None,
    reads: list[str] | None = None,
) -> dict | None:
    """Lineage record for ``query``, or None when it cannot be traced at all.

    ``query`` must reference its tables schema-qualified (the governance
    catalog canonicalises view bodies before tracing them).
    """
    from havn.engine.sql_analysis import (
        SKIP_SCHEMAS,
        _LINEAGE_DIALECT,
        _leaf_sources,
        _mapping_schema,
        _table_alias_map,
        _table_fqn,
        parse_sql,
    )

    parsed = parse_sql(query)
    if parsed is None or not isinstance(parsed, exp.Query):
        return None
    tables = sorted({
        _table_fqn(t) for t in parsed.find_all(exp.Table)
        if t.db and isinstance(t.this, exp.Identifier)
        and (t.db or "").lower() not in SKIP_SCHEMAS
    })
    mapping = _mapping_schema(parsed, list(reads or []), column_catalog, None)
    try:
        from sqlglot.lineage import to_node
        from sqlglot.optimizer import build_scope, qualify

        qualified = qualify.qualify(
            parsed.copy(),
            dialect=_LINEAGE_DIALECT,
            schema=mapping,
            infer_schema=True,
            validate_qualify_columns=False,
            quote_identifiers=False,
            identify=False,
        )
        scope = build_scope(qualified)
    except Exception as e:
        logger.debug("Could not qualify for governance lineage: %s", e)
        return {"columns": {}, "reads": tables, "traced": False}
    if scope is None or not isinstance(scope.expression, exp.Query):
        return {"columns": {}, "reads": tables, "traced": False}
    alias_map = _table_alias_map(qualified)
    columns: dict[str, dict] = {}
    traced = True
    for index, select in enumerate(scope.expression.selects):
        name = (select.alias_or_name or "").lower() or f"_col_{index}"
        if isinstance(select, exp.Star) or (
            isinstance(select, exp.Column) and isinstance(select.this, exp.Star)
        ):
            traced = False  # an unexpandable star: no idea which columns came out
            continue
        try:
            node = to_node(index, scope, _LINEAGE_DIALECT, trim_selects=False)
        except Exception as e:
            logger.debug("Governance lineage failed for %s: %s", name, e)
            traced = False
            columns[_dedupe(name, columns)] = {"sources": [], "passthrough": None, "unresolved": True}
            continue
        sources = _leaf_sources(node, alias_map)
        unresolved = any(s.get("resolved") is False for s in sources)
        entry = {
            "sources": [[s["source_table"], s["source_column"]] for s in sources if s.get("resolved") is not False],
            "passthrough": list(_identity_source(node) or ()) or None,
        }
        if unresolved:
            entry["unresolved"] = True
            traced = False
        columns[_dedupe(name, columns)] = entry
    return {"columns": columns, "reads": tables, "traced": traced}


def _dedupe(name: str, taken: dict) -> str:
    if name not in taken:
        return name
    n = 1
    while f"{name}_{n}" in taken:
        n += 1
    return f"{name}_{n}"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def ensure_lineage_table(conn: duckdb.DuckDBPyConnection) -> None:
    from havn.engine.database import _is_ducklake_connection, _strip_pk

    conn.execute("CREATE SCHEMA IF NOT EXISTS _havn")
    conn.execute(_strip_pk("""
        CREATE TABLE IF NOT EXISTS _havn.model_lineage (
            model_path   VARCHAR PRIMARY KEY,
            content_hash VARCHAR,
            materialized VARCHAR,
            lineage      JSON,
            built_at     TIMESTAMP DEFAULT current_timestamp
        )
    """, _is_ducklake_connection(conn)))


def save_model_lineage(conn: duckdb.DuckDBPyConnection, model: Any) -> None:
    """Record what ``model``'s built object was made from. Never raises."""
    try:
        from havn.engine.sql_analysis import fetch_column_catalog

        record = trace_columns(model.query, fetch_column_catalog(conn), list(model.depends_on or []))
        if record is None:
            record = {"columns": {}, "reads": sorted(set(model.depends_on or [])), "traced": False}
        # A model's own dependency list is the better record of what it read
        # (it also has @depends_on and refs sqlglot found outside projections).
        record["reads"] = sorted(set(record.get("reads", [])) | {d.lower() for d in model.depends_on or []})
        ensure_lineage_table(conn)
        conn.execute("DELETE FROM _havn.model_lineage WHERE model_path = ?", [model.full_name])
        conn.execute(
            "INSERT INTO _havn.model_lineage (model_path, content_hash, materialized, lineage, built_at) "
            "VALUES (?, ?, ?, ?, current_timestamp)",
            [model.full_name, model.content_hash, model.materialized, json.dumps(record)],
        )
    except Exception as e:
        logger.debug("Could not save lineage for %s: %s", getattr(model, "full_name", "?"), e)


def load_model_lineage(conn: duckdb.DuckDBPyConnection) -> dict[str, dict]:
    """``{model_path: record}`` for every model built since lineage was recorded."""
    try:
        rows = conn.execute(
            "SELECT model_path, materialized, lineage FROM _havn.model_lineage"
        ).fetchall()
    except duckdb.Error:
        return {}
    out: dict[str, dict] = {}
    for path, materialized, raw in rows:
        try:
            record = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except (TypeError, ValueError):
            continue
        record["materialized"] = materialized
        out[str(path).lower()] = record
    return out
