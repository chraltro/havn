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
SKIP_SCHEMAS = frozenset({"information_schema", "_havn", "pg_catalog", "sys"})

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
_GRAIN_PAREN = re.compile(r"^@grain\s*\(\s*(.+?)\s*\)$", re.MULTILINE)
_GRAIN_SPACE = re.compile(r"^@grain\s*:?\s+(.+)$", re.MULTILINE)
_OWNER_PAREN = re.compile(r"^@owner\s*\(\s*(.+?)\s*\)$", re.MULTILINE)
_OWNER_SPACE = re.compile(r"^@owner\s*:?\s+(.+)$", re.MULTILINE)
_SOURCE_FRESHNESS_PAREN = re.compile(r"^@source_freshness\s*\(\s*(.+?)\s*\)$", re.MULTILINE)
_SOURCE_FRESHNESS_SPACE = re.compile(r"^@source_freshness\s*:?\s+(.+)$", re.MULTILINE)

# Combined for use in parse functions (try paren first, then space)
CONFIG_PATTERN = (_CONFIG_PAREN, _CONFIG_SPACE)
DEPENDS_PATTERN = (_DEPENDS_PAREN, _DEPENDS_SPACE)
DESCRIPTION_PATTERN = (_DESCRIPTION_PAREN, _DESCRIPTION_SPACE)
COL_PATTERN = (_COL_PAREN, _COL_SPACE)
ASSERT_PATTERN = (_ASSERT_PAREN, _ASSERT_SPACE)
GRAIN_PATTERN = (_GRAIN_PAREN, _GRAIN_SPACE)
OWNER_PATTERN = (_OWNER_PAREN, _OWNER_SPACE)
SOURCE_FRESHNESS_PATTERN = (_SOURCE_FRESHNESS_PAREN, _SOURCE_FRESHNESS_SPACE)

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
    "@grain",
    "@owner",
    "@source_freshness",
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

    Multiple @depends_on lines are merged in order, preserving the first
    occurrence of each table. Legacy ``-- depends_on:`` lines are appended
    after canonical ones.
    """
    deps: list[str] = []
    seen: set[str] = set()
    matches = _finditer_patterns(DEPENDS_PATTERN, sql) + list(
        _LEGACY_DEPENDS_PATTERN.finditer(sql)
    )
    for m in matches:
        for dep in m.group(1).split(","):
            d = dep.strip()
            if d and d not in seen:
                deps.append(d)
                seen.add(d)
    return deps


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


def parse_assertion_specs(sql: str) -> list[tuple[str, str]]:
    """Parse assertions with optional ``severity=warn|error`` qualifier.

    Returns a list of (expression, severity) tuples. Default severity is
    ``error`` (matches historical behavior — ``severity=`` was not
    parsed, but assertion failures already failed loudly).

    Examples::

        @assert row_count > 0
        @assert no_nulls(email), severity=warn
        @assert unique(id), severity=error
    """
    raw = parse_assertions(sql)
    out: list[tuple[str, str]] = []
    for expr in raw:
        severity = "error"
        # Trailing ", severity=..." segment is the only contract here. We
        # split on the LAST comma so commas inside the expression itself
        # (e.g. ``accepted_values(col, ['a', 'b'])``) are preserved.
        if "severity=" in expr:
            head, _, tail = expr.rpartition("severity=")
            head = head.rstrip().rstrip(",").rstrip()
            sev = tail.strip().rstrip(")").rstrip(",").strip().lower()
            if sev in ("warn", "warning", "error"):
                severity = "warn" if sev.startswith("warn") else "error"
                expr = head
            elif sev:
                # Unknown severity literal — warn but accept the
                # assertion at the default severity. Strip the qualifier
                # from the expression so it doesn't end up parsed as
                # SQL by ``_evaluate_assertion``.
                logger.warning(
                    "Unrecognized severity %r on assertion %r; "
                    "defaulting to 'error'. Use severity=warn|error.",
                    sev, head,
                )
                expr = head
            else:
                # Empty severity= (e.g. trailing comma, blank value).
                # Strip the qualifier and use the default.
                logger.warning(
                    "Empty severity= qualifier on assertion %r; "
                    "defaulting to 'error'.", head,
                )
                expr = head
        out.append((expr.strip(), severity))
    return out


def parse_grain(sql: str) -> list[str]:
    """Parse grain columns from ``@grain`` directive.

    Returns list of column names (single or composite grain). Empty list
    if no @grain directive is present.

    Examples::

        @grain transaction_id
        @grain customer_id, reporting_month
    """
    match = _search_patterns(GRAIN_PATTERN, sql)
    if not match:
        return []
    return [c.strip() for c in match.group(1).split(",") if c.strip()]


def parse_owner(sql: str) -> str:
    """Parse owner label from ``@owner`` directive.

    Examples::

        @owner @data-platform-team
        @owner alice@example.com
    """
    match = _search_patterns(OWNER_PATTERN, sql)
    return match.group(1).strip() if match else ""


def parse_source_freshness(sql: str) -> list[dict]:
    """Parse source-freshness contracts from ``@source_freshness`` directives.

    Multiple lines allowed (one per source). Each yields a dict::

        {"table": "landing.transactions", "max_age_seconds": 86400,
         "on": "created_at", "severity": "error"}

    Syntax::

        @source_freshness landing.transactions, max_age=24h, on=created_at
        @source_freshness landing.customers, max_age=7d, on=loaded_at, severity=warn
    """
    matches = _finditer_patterns(SOURCE_FRESHNESS_PATTERN, sql)
    specs: list[dict] = []
    for m in matches:
        body = m.group(1).strip()
        # First token is the source table; remaining are key=value pairs.
        parts = [p.strip() for p in body.split(",") if p.strip()]
        if not parts:
            continue
        spec: dict = {
            "table": parts[0],
            "max_age_seconds": 86400,  # 24h default
            "on": None,
            "severity": "error",
        }
        for kv in parts[1:]:
            if "=" not in kv:
                continue
            key, value = (s.strip() for s in kv.split("=", 1))
            if key == "max_age":
                spec["max_age_seconds"] = _parse_duration(value)
            elif key == "on":
                spec["on"] = value
            elif key == "severity":
                v = value.lower()
                spec["severity"] = "warn" if v.startswith("warn") else "error"
        specs.append(spec)
    return specs


def _parse_duration(s: str) -> int:
    """Parse a duration string like ``24h``, ``7d``, ``30m``, ``45s`` into seconds.

    Bare integers are interpreted as seconds. Malformed inputs fall back
    to the 24h default and log a warning rather than crash, so a typo in
    one ``@source_freshness`` line doesn't take down the whole project's
    discovery pass.
    """
    s = s.strip().lower()
    if not s:
        return 86400  # 24h default
    try:
        if s[-1].isdigit():
            return int(s)
        n = int(s[:-1])
        unit = s[-1]
        if unit == "s":
            return n
        if unit == "m":
            return n * 60
        if unit == "h":
            return n * 3600
        if unit == "d":
            return n * 86400
    except ValueError:
        pass
    logger.warning(
        "Unrecognized duration %r in @source_freshness; falling back to 24h. "
        "Use s/m/h/d suffixes (e.g. 24h, 7d).", s,
    )
    return 86400


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

    # Discover CTE names first so they can be filtered out of the table map.
    # CTE references look identical to plain table refs in the AST.
    cte_names: set[str] = {
        (cte.alias or "").lower()
        for cte in parsed.find_all(exp.CTE)
        if cte.alias
    }
    cte_names.discard("")

    def _is_cte_ref(table: exp.Table) -> bool:
        return (table.name or "").lower() in cte_names and not (table.db or "")

    # Build alias -> fully-qualified table map (excluding CTE refs)
    alias_map: dict[str, str] = {}
    # Aliases pointing to CTE names: e.g. `FROM filtered f` -> {"f": "filtered"}.
    cte_alias_map: dict[str, str] = {}
    for table in parsed.find_all(exp.Table):
        if _is_cte_ref(table):
            cte_name = (table.name or "").lower()
            alias = (table.alias or "").lower()
            cte_alias_map[cte_name] = cte_name
            if alias:
                cte_alias_map[alias] = cte_name
            continue
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

    # Resolve column lists for any real table we may need (for SELECT *
    # expansion and per-scope unqualified-column resolution).
    table_columns: dict[str, list[str]] = {}
    if conn:
        candidates = set(depends_on) | set(alias_map.values())
        for dep in candidates:
            parts = dep.split(".")
            if len(parts) == 2:
                try:
                    cols = conn.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
                        [parts[0], parts[1]],
                    ).fetchall()
                    table_columns[dep] = [c[0] for c in cols]
                except Exception as e:
                    logger.debug("Failed to resolve table reference: %s", e)

    # Build per-CTE lineage in declaration order so later CTEs can resolve
    # through earlier ones.
    cte_column_map: dict[str, dict[str, list[dict[str, str]]]] = {}
    for cte in parsed.find_all(exp.CTE):
        cte_alias = (cte.alias or "").lower()
        if not cte_alias:
            continue
        cte_select = cte.this
        if isinstance(cte_select, exp.Select):
            cte_lineage = _trace_select_lineage(
                cte_select,
                alias_map,
                cte_names,
                cte_column_map,
                depends_on,
                table_columns,
            )
            cte_column_map[cte_alias] = cte_lineage

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
            # Unqualified `SELECT *` — expand from real tables in scope, then CTEs.
            for source_fqn in _scope_real_tables(main_select, alias_map, cte_names):
                if source_fqn in table_columns:
                    for col_name in table_columns[source_fqn]:
                        lineage[col_name.lower()] = [
                            {"source_table": source_fqn, "source_column": col_name.lower()}
                        ]
            for cte_alias in _scope_cte_refs(main_select, cte_names):
                for col_name, sources in cte_column_map.get(cte_alias, {}).items():
                    if col_name not in lineage:
                        lineage[col_name] = sources
            continue
        else:
            out_col = (
                select_expr.output_name.lower()
                if hasattr(select_expr, "output_name") and select_expr.output_name
                else "?"
            )
            inner = select_expr

        # Detect a `b.*` pattern — sqlglot wraps it as Column(this=Star(), table=b).
        target_node = inner if inner else select_expr
        star_table = _star_table_alias(target_node)
        if star_table is not None:
            cte_resolved = cte_alias_map.get(star_table)
            if cte_resolved and cte_resolved in cte_column_map:
                for col_name, sources in cte_column_map[cte_resolved].items():
                    lineage[col_name] = sources
                continue
            resolved = alias_map.get(star_table, star_table)
            if resolved in table_columns:
                for col_name in table_columns[resolved]:
                    lineage[col_name.lower()] = [
                        {"source_table": resolved, "source_column": col_name.lower()}
                    ]
            continue

        sources = _extract_sources(
            target_node,
            alias_map,
            cte_names,
            cte_column_map,
            depends_on,
            table_columns,
            scope=main_select,
            cte_alias_map=cte_alias_map,
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
    cte_column_map: dict[str, dict[str, list[dict[str, str]]]],
    depends_on: list[str],
    table_columns: dict[str, list[str]] | None = None,
    cte_alias_map: dict[str, str] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Trace column lineage within a SELECT expression (used for CTEs)."""
    lineage: dict[str, list[dict[str, str]]] = {}
    # Build a CTE alias map specific to this SELECT's FROM/JOIN clauses,
    # falling back to the parent map for outer-scope references.
    local_cte_alias_map: dict[str, str] = dict(cte_alias_map or {})
    for table in select.find_all(exp.Table):
        if table.find_ancestor(exp.Select) is not select:
            continue
        name = (table.name or "").lower()
        db = (table.db or "").lower()
        if name in cte_names and not db:
            alias = (table.alias or "").lower()
            local_cte_alias_map[name] = name
            if alias:
                local_cte_alias_map[alias] = name
    for select_expr in select.expressions:
        if isinstance(select_expr, exp.Alias):
            out_col = select_expr.alias.lower()
            inner = select_expr.this
        elif isinstance(select_expr, exp.Column):
            out_col = select_expr.name.lower()
            inner = select_expr
        else:
            continue

        sources = _extract_sources(
            inner,
            alias_map,
            cte_names,
            cte_column_map,
            depends_on,
            table_columns or {},
            scope=select,
            cte_alias_map=local_cte_alias_map,
        )
        lineage[out_col] = sources
    return lineage


def _scope_real_tables(
    select: exp.Select,
    alias_map: dict[str, str],
    cte_names: set[str],
) -> list[str]:
    """Real (non-CTE) tables referenced in a SELECT's FROM/JOIN clauses."""
    out: list[str] = []
    seen: set[str] = set()
    for table in select.find_all(exp.Table):
        # Only consider tables in this SELECT's own FROM/JOIN scope.
        if table.find_ancestor(exp.Select) is not select:
            continue
        name = (table.name or "").lower()
        db = (table.db or "").lower()
        if name in cte_names and not db:
            continue
        fqn = alias_map.get(name) if not db else alias_map.get(f"{db}.{name}", f"{db}.{name}")
        if not fqn:
            continue
        if fqn not in seen:
            seen.add(fqn)
            out.append(fqn)
    return out


def _scope_cte_refs(
    select: exp.Select,
    cte_names: set[str],
) -> list[str]:
    """CTE names referenced from a SELECT's FROM/JOIN clauses."""
    out: list[str] = []
    seen: set[str] = set()
    for table in select.find_all(exp.Table):
        if table.find_ancestor(exp.Select) is not select:
            continue
        name = (table.name or "").lower()
        db = (table.db or "").lower()
        if name in cte_names and not db and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _star_table_alias(node: exp.Expression) -> str | None:
    """If node is a `b.*` expansion, return `b` (lowercased). Else None."""
    if isinstance(node, exp.Column) and isinstance(node.this, exp.Star):
        return (node.table or "").lower() or None
    return None


def _extract_sources(
    node: exp.Expression,
    alias_map: dict[str, str],
    cte_names: set[str],
    cte_column_map: dict[str, dict[str, list[dict[str, str]]]],
    depends_on: list[str],
    table_columns: dict[str, list[str]] | None = None,
    scope: exp.Select | None = None,
    cte_alias_map: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Walk an expression and collect all column references, tracing through CTEs."""
    sources: list[dict[str, str]] = []
    table_columns = table_columns or {}
    cte_alias_map = cte_alias_map or {}

    for col in node.find_all(exp.Column):
        col_name = (col.name or "").lower()
        if not col_name or col_name == "*":
            continue
        table_ref = (col.table or "").lower()

        if table_ref:
            # CTE-qualified (or aliased CTE-qualified) reference?
            cte_resolved = cte_alias_map.get(table_ref)
            if cte_resolved:
                cte_lineage = cte_column_map.get(cte_resolved, {})
                if col_name in cte_lineage:
                    sources.extend(cte_lineage[col_name])
                continue

            resolved = alias_map.get(table_ref, table_ref)
            if resolved in cte_names and resolved in cte_column_map:
                cte_lineage = cte_column_map[resolved]
                if col_name in cte_lineage:
                    sources.extend(cte_lineage[col_name])
                    continue
            elif resolved in cte_names:
                continue
            sources.append({"source_table": resolved, "source_column": col_name})
            continue

        # Unqualified — try to attribute to a table actually visible in this
        # SELECT's scope rather than blindly defaulting to depends_on[0].
        attributed = False
        if scope is not None:
            real_in_scope = _scope_real_tables(scope, alias_map, cte_names)
            cte_in_scope = _scope_cte_refs(scope, cte_names)

            # Single real-table scope: easy attribution.
            if len(real_in_scope) == 1 and not cte_in_scope:
                sources.append({"source_table": real_in_scope[0], "source_column": col_name})
                attributed = True
            else:
                # Multi-source: prefer a CTE whose lineage knows the column.
                for cte_alias in cte_in_scope:
                    cte_lineage = cte_column_map.get(cte_alias, {})
                    if col_name in cte_lineage:
                        sources.extend(cte_lineage[col_name])
                        attributed = True
                        break
                if not attributed:
                    # Fall back to information_schema — pick the first real
                    # table that actually has this column.
                    for fqn in real_in_scope:
                        cols = table_columns.get(fqn, [])
                        if col_name in {c.lower() for c in cols}:
                            sources.append({"source_table": fqn, "source_column": col_name})
                            attributed = True
                            break

        if not attributed and depends_on:
            sources.append({"source_table": depends_on[0].lower(), "source_column": col_name})

    return sources
