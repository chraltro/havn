"""SQL analysis using sqlglot AST parsing.

Provides shared functions for extracting table references, column lineage,
and parsing SQL config comments. Replaces regex-based parsing with proper
AST analysis that correctly handles CTEs, subqueries, UNION ALL, and
complex expressions.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import sqlglot
from sqlglot import exp

logger = logging.getLogger("havn.sql_analysis")

# Schemas that are never real upstream dependencies
SKIP_SCHEMAS = frozenset({"information_schema", "_dp_internal", "pg_catalog", "sys"})

# --- Config directive patterns ---
# Primary syntax: bare @decorators on their own lines, before the SQL.
#   @config materialized=table, schema=silver
#   @config(materialized=table, schema=silver)
#   @depends_on bronze.customers, bronze.orders
#
# Legacy syntax (still supported): SQL comments with "-- config:" etc.

# Two sub-patterns per directive:
# 1. Parenthesised: @config(...)  — content is inside parens
# 2. Space/colon:   @config ...   — content follows directly
_CONFIG_PAREN = re.compile(r"^@config\s*\(\s*(.+?)\s*\)$", re.MULTILINE)
_CONFIG_SPACE = re.compile(r"^@config\s*:?\s+(.+)$", re.MULTILINE)
_DEPENDS_PAREN = re.compile(r"^@depends_on\s*\(\s*(.+?)\s*\)$", re.MULTILINE)
_DEPENDS_SPACE = re.compile(r"^@depends_on\s*:?\s+(.+)$", re.MULTILINE)
_DESCRIPTION_PAREN = re.compile(r"^@description\s*\(\s*(.+?)\s*\)$", re.MULTILINE)
_DESCRIPTION_SPACE = re.compile(r"^@description\s*:?\s+(.+)$", re.MULTILINE)
_COL_PAREN = re.compile(r"^@col\s*\(\s*(\w+):\s*(.+?)\s*\)$", re.MULTILINE)
_COL_SPACE = re.compile(r"^@col\s*:?\s+(\w+):\s*(.+)$", re.MULTILINE)
_ASSERT_PAREN = re.compile(r"^@assert\s*\(\s*(.+?)\s*\)$", re.MULTILINE)
_ASSERT_SPACE = re.compile(r"^@assert\s*:?\s+(.+)$", re.MULTILINE)

# Combined for use in parse functions (try paren first, then space)
CONFIG_PATTERN = (_CONFIG_PAREN, _CONFIG_SPACE)
DEPENDS_PATTERN = (_DEPENDS_PAREN, _DEPENDS_SPACE)
DESCRIPTION_PATTERN = (_DESCRIPTION_PAREN, _DESCRIPTION_SPACE)
COL_PATTERN = (_COL_PAREN, _COL_SPACE)
ASSERT_PATTERN = (_ASSERT_PAREN, _ASSERT_SPACE)

# Legacy "-- config:" syntax (still supported for backward compatibility)
_LEGACY_CONFIG_PATTERN = re.compile(r"^--\s*config:\s*(.+)$", re.MULTILINE)
_LEGACY_DEPENDS_PATTERN = re.compile(r"^--\s*depends_on:\s*(.+)$", re.MULTILINE)
_LEGACY_DESCRIPTION_PATTERN = re.compile(r"^--\s*description:\s*(.+)$", re.MULTILINE)
_LEGACY_COL_PATTERN = re.compile(r"^--\s*col:\s*(\w+):\s*(.+)$", re.MULTILINE)
_LEGACY_ASSERT_PATTERN = re.compile(r"^--\s*assert:\s*(.+)$", re.MULTILINE)

_META_PREFIXES = (
    "@config",
    "@depends_on",
    "@description",
    "@col",
    "@assert",
    # Legacy
    "-- config:",
    "-- depends_on:",
    "-- description:",
    "-- col:",
    "-- assert:",
)


def _search_patterns(patterns: tuple[re.Pattern, ...], sql: str) -> re.Match | None:
    """Try multiple patterns in order, returning the first match."""
    for p in patterns:
        m = p.search(sql)
        if m:
            return m
    return None


def _finditer_patterns(patterns: tuple[re.Pattern, ...], sql: str) -> list[re.Match]:
    """Collect all matches from multiple patterns."""
    matches = []
    for p in patterns:
        matches.extend(p.finditer(sql))
    return matches


def parse_config(sql: str) -> dict[str, str]:
    """Parse config from SQL header.

    Primary syntax::

        @config materialized=table, schema=silver
        @config(materialized=table, schema=silver)

    Legacy syntax (still supported)::

        -- config: materialized=table, schema=silver
    """
    match = _search_patterns(CONFIG_PATTERN, sql) or _LEGACY_CONFIG_PATTERN.search(sql)
    if not match:
        return {}
    config: dict[str, str] = {}
    for pair in match.group(1).split(","):
        pair = pair.strip()
        if "=" in pair:
            key, value = pair.split("=", 1)
            config[key.strip()] = value.strip()
    return config


def parse_depends(sql: str) -> list[str]:
    """Parse dependencies from SQL header.

    Primary syntax::

        @depends_on bronze.customers, bronze.orders
        @depends_on(bronze.customers, bronze.orders)

    Legacy syntax (still supported)::

        -- depends_on: bronze.customers, bronze.orders
    """
    match = _search_patterns(DEPENDS_PATTERN, sql) or _LEGACY_DEPENDS_PATTERN.search(sql)
    if not match:
        return []
    return [dep.strip() for dep in match.group(1).split(",") if dep.strip()]


def parse_assertions(sql: str) -> list[str]:
    """Parse assertion expressions from SQL header.

    Primary syntax::

        @assert row_count > 0
        @assert(unique(id))

    Legacy syntax (still supported)::

        -- assert: row_count > 0
    """
    results = [m.group(1).strip() for m in _finditer_patterns(ASSERT_PATTERN, sql)]
    results.extend(m.group(1).strip() for m in _LEGACY_ASSERT_PATTERN.finditer(sql))
    return results


def parse_description(sql: str) -> str:
    """Parse description from SQL header.

    Primary syntax::

        @description Customer dimension table
        @description(Customer dimension table)

    Legacy syntax (still supported)::

        -- description: Customer dimension table
    """
    match = _search_patterns(DESCRIPTION_PATTERN, sql) or _LEGACY_DESCRIPTION_PATTERN.search(sql)
    return match.group(1).strip() if match else ""


def parse_column_docs(sql: str) -> dict[str, str]:
    """Parse column documentation from SQL header.

    Primary syntax::

        @col id: Primary key
        @col(id: Primary key)

    Legacy syntax (still supported)::

        -- col: id: Primary key
    """
    docs = {m.group(1): m.group(2).strip() for m in _finditer_patterns(COL_PATTERN, sql)}
    docs.update({m.group(1): m.group(2).strip() for m in _LEGACY_COL_PATTERN.finditer(sql)})
    return docs


def strip_config_comments(sql: str) -> str:
    """Remove config/depends/description/col/assert comment lines, return the query."""
    lines = sql.split("\n")
    query_lines = []
    for line in lines:
        stripped = line.strip()
        if any(stripped.startswith(prefix) for prefix in _META_PREFIXES):
            continue
        query_lines.append(line)
    while query_lines and not query_lines[0].strip():
        query_lines.pop(0)
    return "\n".join(query_lines)


# --- AST-based table reference extraction ---


def extract_table_refs(
    sql: str,
    *,
    exclude: str | None = None,
) -> list[str]:
    """Extract schema-qualified table references from SQL using sqlglot AST.

    Correctly handles CTEs, subqueries, UNION ALL, aliased subqueries,
    and complex expressions that regex-based parsing misses.

    Args:
        sql: The SQL query to analyze (config comments should be stripped first).
        exclude: A ``schema.table`` name to exclude (e.g. the model's own name).

    Returns:
        Sorted list of unique ``schema.table`` references.
    """
    try:
        parsed = sqlglot.parse_one(sql, read="duckdb")
    except sqlglot.errors.ParseError:
        return _fallback_extract_table_refs(sql, exclude=exclude)

    # Collect CTE names so we can skip them
    cte_names: set[str] = set()
    for cte in parsed.find_all(exp.CTE):
        if cte.alias:
            cte_names.add(cte.alias.lower())

    refs: set[str] = set()
    for table in parsed.find_all(exp.Table):
        schema = (table.db or "").lower()
        name = (table.name or "").lower()

        if not schema or not name:
            continue
        if schema in SKIP_SCHEMAS:
            continue
        # Skip CTE references
        if name in cte_names or schema in cte_names:
            continue

        fqn = f"{schema}.{name}"
        if exclude and fqn == exclude:
            continue
        refs.add(fqn)

    return sorted(refs)


# Regex fallback for when sqlglot cannot parse (e.g. DuckDB-specific syntax)
_SQL_FROM_REF_PATTERN = re.compile(
    r"\b(?:FROM|JOIN)\s+([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)\b",
    re.IGNORECASE,
)


def _fallback_extract_table_refs(
    sql: str,
    *,
    exclude: str | None = None,
) -> list[str]:
    """Regex fallback for extracting table refs when sqlglot fails."""
    clean = re.sub(r"--[^\n]*", "", sql)
    refs: set[str] = set()
    for match in _SQL_FROM_REF_PATTERN.finditer(clean):
        schema, table = match.group(1).lower(), match.group(2).lower()
        if schema in SKIP_SCHEMAS:
            continue
        fqn = f"{schema}.{table}"
        if exclude and fqn == exclude:
            continue
        refs.add(fqn)
    return sorted(refs)


# --- Column-level lineage ---


def extract_column_lineage(
    query: str,
    depends_on: list[str] | None = None,
    conn: Any | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Extract column-level lineage from SQL using sqlglot AST parsing.

    Traces column references through CTEs, subqueries, CASE expressions,
    window functions, and UNION ALL queries.

    Args:
        query: The SQL query to analyze (config comments should be stripped).
        depends_on: List of upstream ``schema.table`` dependencies.
        conn: Optional DuckDB connection for resolving ``SELECT *``.

    Returns:
        Mapping of output_column -> list of {source_table, source_column}.
    """
    depends_on = depends_on or []
    lineage: dict[str, list[dict[str, str]]] = {}

    try:
        parsed = sqlglot.parse_one(query, read="duckdb")
    except sqlglot.errors.ParseError:
        return lineage

    # Build alias -> fully-qualified table map
    alias_map: dict[str, str] = {}
    for table in parsed.find_all(exp.Table):
        db = (table.db or "").lower()
        name = (table.name or "").lower()
        alias = (table.alias or "").lower()
        if db and name:
            fqn = f"{db}.{name}"
        elif name:
            fqn = name
        else:
            continue
        if alias:
            alias_map[alias] = fqn
        alias_map[fqn] = fqn

    # Build CTE name -> inner SELECT mapping for tracing through CTEs
    cte_names: set[str] = set()
    cte_column_map: dict[str, dict[str, list[dict[str, str]]]] = {}
    for cte in parsed.find_all(exp.CTE):
        cte_alias = (cte.alias or "").lower()
        if not cte_alias:
            continue
        cte_names.add(cte_alias)
        # Recursively trace lineage within the CTE
        cte_select = cte.this
        if isinstance(cte_select, exp.Select):
            cte_lineage = _trace_select_lineage(cte_select, alias_map, cte_names, depends_on)
            cte_column_map[cte_alias] = cte_lineage

    # Resolve SELECT * columns if we have a connection
    star_columns: dict[str, list[str]] = {}
    if conn:
        for dep in depends_on:
            parts = dep.split(".")
            if len(parts) == 2:
                try:
                    cols = conn.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
                        [parts[0], parts[1]],
                    ).fetchall()
                    star_columns[dep] = [c[0] for c in cols]
                except Exception as e:
                    logger.debug("Failed to resolve table reference: %s", e)

    # Find the outermost SELECT
    main_select = _find_main_select(parsed)
    if not main_select:
        return lineage

    # Process each SELECT expression
    for select_expr in main_select.expressions:
        if isinstance(select_expr, exp.Alias):
            out_col = select_expr.alias.lower()
            inner = select_expr.this
        elif isinstance(select_expr, exp.Column):
            out_col = select_expr.name.lower()
            inner = select_expr
        elif isinstance(select_expr, exp.Star):
            for dep in depends_on:
                if dep in star_columns:
                    for col_name in star_columns[dep]:
                        lineage[col_name.lower()] = [
                            {"source_table": dep, "source_column": col_name.lower()}
                        ]
            continue
        else:
            out_col = (
                select_expr.output_name.lower()
                if hasattr(select_expr, "output_name") and select_expr.output_name
                else "?"
            )
            inner = select_expr

        sources = _extract_sources(
            inner if inner else select_expr,
            alias_map,
            cte_names,
            cte_column_map,
            depends_on,
        )

        # Deduplicate
        seen: set[tuple[str, str]] = set()
        unique: list[dict[str, str]] = []
        for s in sources:
            key = (s["source_table"], s["source_column"])
            if key not in seen:
                seen.add(key)
                unique.append(s)

        lineage[out_col] = unique

    return lineage


def _find_main_select(parsed: exp.Expression) -> exp.Select | None:
    """Find the outermost SELECT in a parsed expression."""
    if isinstance(parsed, exp.Union):
        return parsed.find(exp.Select)
    if hasattr(parsed, "this") and isinstance(parsed.this, exp.Select):
        return parsed.this
    if isinstance(parsed, exp.Select):
        return parsed
    return parsed.find(exp.Select)


def _trace_select_lineage(
    select: exp.Select,
    alias_map: dict[str, str],
    cte_names: set[str],
    depends_on: list[str],
) -> dict[str, list[dict[str, str]]]:
    """Trace column lineage within a SELECT expression (used for CTEs)."""
    lineage: dict[str, list[dict[str, str]]] = {}
    for select_expr in select.expressions:
        if isinstance(select_expr, exp.Alias):
            out_col = select_expr.alias.lower()
            inner = select_expr.this
        elif isinstance(select_expr, exp.Column):
            out_col = select_expr.name.lower()
            inner = select_expr
        else:
            continue

        sources = _extract_sources(inner, alias_map, cte_names, {}, depends_on)
        lineage[out_col] = sources
    return lineage


def _extract_sources(
    node: exp.Expression,
    alias_map: dict[str, str],
    cte_names: set[str],
    cte_column_map: dict[str, dict[str, list[dict[str, str]]]],
    depends_on: list[str],
) -> list[dict[str, str]]:
    """Walk an expression and collect all column references, tracing through CTEs."""
    sources: list[dict[str, str]] = []

    for col in node.find_all(exp.Column):
        col_name = (col.name or "").lower()
        table_ref = (col.table or "").lower()

        if table_ref:
            resolved = alias_map.get(table_ref, table_ref)
            if resolved in cte_names and resolved in cte_column_map:
                # Trace through CTE: look up the column in the CTE's lineage
                cte_lineage = cte_column_map[resolved]
                if col_name in cte_lineage:
                    sources.extend(cte_lineage[col_name])
                    continue
            elif resolved in cte_names:
                # CTE without lineage info — skip
                continue
            sources.append({"source_table": resolved, "source_column": col_name})
        elif col_name and depends_on:
            sources.append({"source_table": depends_on[0].lower(), "source_column": col_name})

    return sources
