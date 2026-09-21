"""SQL analysis using sqlglot AST parsing.

Provides shared functions for extracting table references, column lineage,
and parsing SQL config comments. Replaces regex-based parsing with proper
AST analysis that correctly handles CTEs, subqueries, UNION ALL, and
complex expressions.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
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


# Every key `@config` understands, in one place so later work extends this
# set rather than adding another silently-ignored key. Discovery reads
# exactly these; `validate_models` reports anything else instead of letting
# a typo like `materialised=table` quietly build a view.
CONFIG_KEYS = frozenset({
    "schema",
    "materialized",
    "unique_key",
    "incremental_strategy",
    "incremental_filter",
    "partition_by",
    "watermark",
    "on_schema_change",
    "tags",
})

# Accepted values of `materialized`, checked at validation time rather than
# only when execution reaches "Unknown materialization".
MATERIALIZATIONS = frozenset({"view", "table", "incremental", "ephemeral"})

# Accepted values of `on_schema_change`, the policy an incremental model
# applies when its query's columns no longer line up with the target table.
ON_SCHEMA_CHANGE_POLICIES = frozenset({
    "append_new_columns",
    "ignore",
    "fail",
    "sync_all_columns",
})


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
    return _split_config_pairs(match.group(1))


# A config value runs until the next ``, key=`` boundary (or the end of the
# header). Splitting naively on every comma truncates every setting whose value
# legitimately contains one -- a composite ``unique_key=customer_id, event_date``
# silently became ``customer_id``, which turns an incremental delete+insert into
# a destructive partial-key delete. Same for
# ``incremental_filter=WHERE x IN (1,2)``.
_CONFIG_PAIR_RE = re.compile(
    r"(\w+)\s*=\s*(.*?)\s*(?=,\s*\w+\s*=|$)",
    re.DOTALL,
)


def _split_config_pairs(body: str) -> dict[str, str]:
    """Parse a ``key=value, key=value`` config body, tolerating commas in values."""
    config: dict[str, str] = {}
    for key, value in _CONFIG_PAIR_RE.findall(body):
        config[key.strip()] = value.strip().rstrip(",").strip()
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
    """Blank out config/depends/description/col/assert lines, return the query.

    Directive lines are replaced by empty lines rather than deleted, and
    leading blanks are kept, so line N of the result is line N of the file.
    Directives are allowed anywhere (an ``@assert`` below the SQL is legal),
    so deleting them shifted every later line by a non-constant amount and
    nothing built on sqlglot or SQLFluff line numbers could point back at the
    right source line.

    Everything that consumes the result either wraps it in ``CREATE ... AS``,
    which tolerates leading blank lines, or strips it first.
    """
    query_lines = []
    for line in sql.split("\n"):
        stripped = line.strip()
        if any(stripped.startswith(prefix) for prefix in _META_PREFIXES):
            query_lines.append("")
            continue
        query_lines.append(line)
    return "\n".join(query_lines)


# --- AST-based table reference extraction ---


def parse_sql(sql: str) -> exp.Expression | None:
    """Parse ``sql`` with the DuckDB dialect, returning None when it will not parse.

    The single place that decides which sqlglot failures count as "this is not
    parseable SQL". Callers that need the message use
    :func:`parse_sql_with_error`.
    """
    return parse_sql_with_error(sql)[0]


def parse_sql_with_error(sql: str) -> tuple[exp.Expression | None, str]:
    """Like :func:`parse_sql`, but also returns the failure message (or "")."""
    try:
        return sqlglot.parse_one(sql, read="duckdb"), ""
    except sqlglot.errors.SqlglotError as e:
        return None, str(e)


def extract_table_refs(
    sql: str,
    *,
    exclude: str | None = None,
    ast: exp.Expression | None = None,
) -> list[str]:
    """Extract schema-qualified table references from SQL using sqlglot AST.

    Correctly handles CTEs, subqueries, UNION ALL, aliased subqueries,
    and complex expressions that regex-based parsing misses.

    Args:
        sql: The SQL query to analyze (config comments should be stripped first).
        exclude: A ``schema.table`` name to exclude (e.g. the model's own name).
        ast: Pre-parsed AST for ``sql``, when the caller already has one.
            Callers holding only SQL text can leave this out and the text is
            parsed here as before.

    Returns:
        Sorted list of unique ``schema.table`` references.
    """
    parsed = ast if ast is not None else parse_sql(sql)
    if parsed is None:
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
        # Skip CTE references. A CTE is always referenced *unqualified*, so only
        # an unqualified ref can be one -- and those are already dropped by the
        # `not schema` check above. Matching a qualified ref against cte_names
        # would silently drop a real dependency whenever a CTE happens to share
        # a name with a model or its schema: `WITH orders AS (...) SELECT ...
        # FROM orders o JOIN bronze.orders b` must still depend on
        # bronze.orders. See _is_cte_ref below, which gets this right.
        if not schema and name in cte_names:
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


def fetch_column_catalog(conn: Any) -> dict[str, list[str]]:
    """Read every column in the catalog in one query.

    Returns ``{"schema.table": [column, ...]}`` with the columns in
    ordinal position, which is what ``SELECT *`` expansion needs.

    One scan of ``information_schema.columns`` costs about the same as a
    single filtered one, so callers that resolve more than one table (a
    full-project lineage pass, impact analysis, validation) should fetch
    once here and pass the result down rather than querying per table.
    """
    catalog: dict[str, list[str]] = {}
    try:
        rows = conn.execute(
            "SELECT table_schema || '.' || table_name, column_name "
            "FROM information_schema.columns "
            "ORDER BY table_schema, table_name, ordinal_position"
        ).fetchall()
    except Exception as e:
        logger.debug("Could not read the column catalog: %s", e)
        return catalog
    for table_fqn, col_name in rows:
        catalog.setdefault(table_fqn.lower(), []).append(col_name)
    return catalog

def extract_column_lineage(
    query: str,
    depends_on: list[str] | None = None,
    conn: Any | None = None,
    column_catalog: dict[str, list[str]] | None = None,
    ast: exp.Expression | None = None,
    schema: dict[str, list[tuple[str, str]]] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Extract column-level lineage from SQL.

    The tracing itself is ``sqlglot.lineage`` over a scope built from
    ``sqlglot.optimizer.qualify``, fed with whatever column schema we can get
    hold of. That is what resolves the constructs a hand-rolled walker keeps
    getting wrong: a subquery in ``FROM`` (the subquery's alias is not a
    source table), every branch of a ``UNION``, a ``LATERAL``, and stars with
    ``EXCLUDE`` or ``REPLACE``. A statement-level ``PIVOT`` / ``UNPIVOT`` is
    not a ``SELECT`` at all and is handled separately, from the pivoted
    columns.

    Args:
        query: The SQL query to analyze (config comments should be stripped).
        depends_on: List of upstream ``schema.table`` dependencies.
        conn: Optional DuckDB connection. Used to read the column catalog when
            one was not supplied, and to enumerate the output columns of a
            ``PIVOT`` or ``COLUMNS('regex')``, which only the database knows.
        column_catalog: Pre-fetched ``{"schema.table": [column, ...]}`` map,
            as returned by :func:`fetch_column_catalog`. Supply it when
            tracing many models so the catalog is read once for the whole
            pass instead of once per model.
        ast: Pre-parsed AST for ``query``, when the caller already has one.
            It is never mutated: qualification runs on a copy.
        schema: Inferred schemas, ``{"schema.table": [(column, type), ...]}``,
            for tables a bind pass knows about but the catalog does not (an
            upstream that has never been built). Takes precedence over
            ``column_catalog`` for any table present in both.

    Returns:
        Mapping of output_column -> list of {source_table, source_column}.
        A source that could not be resolved to a real column (an unexpandable
        star) carries ``"resolved": False`` and a ``source_column`` of ``*``.
    """
    depends_on = depends_on or []

    parsed = ast if ast is not None else parse_sql(query)
    if parsed is None:
        return {}

    catalog = column_catalog
    if catalog is None and conn is not None:
        catalog = fetch_column_catalog(conn)

    mapping = _mapping_schema(parsed, depends_on, catalog, schema)

    # `PIVOT tbl ON ...` as a whole statement parses to exp.Pivot, which has
    # no projections to trace. Never memoized: a pivot's output columns are
    # values in the data, so the answer can change without the SQL or the
    # schema changing.
    if isinstance(parsed, exp.Pivot):
        return _pivot_lineage(parsed, query, mapping, conn)

    memo_key = _memo_key(query, mapping)
    cached = _LINEAGE_MEMO.get(memo_key)
    if cached is not None:
        return _copy_lineage(cached)

    try:
        from sqlglot.optimizer import build_scope, qualify
    except ImportError as e:  # pragma: no cover - sqlglot always ships it
        logger.debug("sqlglot optimizer unavailable: %s", e)
        return {}

    try:
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
        logger.debug("Could not qualify for lineage: %s", e)
        return {}

    if scope is None or not isinstance(scope.expression, exp.Query):
        return {}

    selects = scope.expression.selects
    if not selects:
        return {}

    # `COLUMNS('regex')` picks its columns out of the catalog at bind time;
    # sqlglot leaves it as a single opaque projection. Ask the database.
    if conn is not None and any(sel.find(exp.Columns) for sel in selects):
        described = _describe_lineage(query, conn, qualified, mapping)
        if described is not None:
            return described

    lineage = _trace_selects(scope, selects, qualified)
    _memoize(memo_key, lineage)
    return lineage


def _trace_selects(scope: Any, selects: list[exp.Expression], qualified: exp.Expression):
    """Run sqlglot's lineage walk over every projection of the outer scope."""
    from sqlglot.lineage import to_node

    alias_map = _table_alias_map(qualified)
    cache: dict[tuple, Any] = {}
    scope_meta: dict[int, Any] = {}

    lineage: dict[str, list[dict[str, str]]] = {}
    for index, select in enumerate(selects):
        name = (select.alias_or_name or "").lower() or f"_col_{index}"
        try:
            # By index, not by name: two projections can share a name
            # (`SELECT *` over a join with a shared key) and both of them
            # have lineage worth keeping.
            node = to_node(
                index,
                scope,
                _LINEAGE_DIALECT,
                # trim_selects exists to give a node a pretty label, and it
                # deep-copies the enclosing SELECT per column to build one.
                # Nothing here reads labels.
                trim_selects=False,
                _cache=cache,
                _scope_meta=scope_meta,
            )
        except Exception as e:
            logger.debug("Lineage failed for column %s: %s", name, e)
            sources: list[dict[str, str]] = []
        else:
            sources = _leaf_sources(node, alias_map)
        lineage[_disambiguate(name, lineage)] = sources
    return lineage


def _disambiguate(name: str, taken: dict[str, Any]) -> str:
    """The key DuckDB would give a duplicated output name.

    ``SELECT * FROM a JOIN b`` over a shared key materializes as
    ``customer_id`` and ``customer_id_1``, so those are the keys: they are
    the column names the built model actually has, which is what the UI
    matches against and what a downstream model has to select by.
    """
    if name not in taken:
        return name
    suffix = 1
    while f"{name}_{suffix}" in taken:
        suffix += 1
    return f"{name}_{suffix}"


def _leaf_sources(node: Any, alias_map: dict[str, str]) -> list[dict[str, str]]:
    """The real table columns at the leaves of one lineage graph."""
    sources: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for leaf in node.walk():
        if leaf.downstream:
            continue

        table = ""
        source = leaf.source
        if isinstance(source, exp.Table):
            table = _table_fqn(source)
        else:
            # sqlglot could not attach the column to a source (a LATERAL
            # subquery is its own scope). The qualified alias still names a
            # real table often enough to be worth resolving by hand.
            qualifier, _, _ = leaf.name.rpartition(".")
            table = alias_map.get(_unquote(qualifier), "") if qualifier else ""

        column = _unquote(leaf.name.rpartition(".")[2])
        if not table or not column:
            continue
        if table.split(".", 1)[0] in SKIP_SCHEMAS:
            continue

        key = (table, column)
        if key in seen:
            continue
        seen.add(key)

        entry = {"source_table": table, "source_column": column}
        if column == "*":
            # An unexpandable star: the upstream is right, the column is not
            # known. Consumers that only read source_table/source_column are
            # unaffected; the flag is there for the ones that care.
            entry["resolved"] = False  # type: ignore[assignment]
        sources.append(entry)

    return sources


# --- Memoization ---
#
# Tracing a model costs about a millisecond: qualification and scope
# resolution, where the walker this replaced did a couple of AST scans. The
# result is a pure function of the SQL text and the columns of the tables it
# reads, so a whole-project pass that runs again (every /api/lineage request,
# every impact analysis) pays for nothing it has already worked out. The key
# carries the upstream column lists, so a rebuilt or altered upstream misses.

_LINEAGE_MEMO: dict[tuple, dict[str, list[dict[str, str]]]] = {}
_LINEAGE_MEMO_MAX = 2048


def _memo_key(query: str, mapping: dict[str, dict[str, dict[str, str]]]) -> tuple:
    schema_signature = tuple(
        (f"{db}.{table}", tuple(columns))
        for db in sorted(mapping)
        for table, columns in sorted(mapping[db].items())
    )
    return (query, schema_signature)


def _copy_lineage(
    lineage: dict[str, list[dict[str, str]]],
) -> dict[str, list[dict[str, str]]]:
    """A caller-owned copy, so a consumer mutating its result cannot poison the memo."""
    return {col: [dict(source) for source in sources] for col, sources in lineage.items()}


def _memoize(key: tuple, lineage: dict[str, list[dict[str, str]]]) -> None:
    if len(_LINEAGE_MEMO) >= _LINEAGE_MEMO_MAX:
        for stale in list(_LINEAGE_MEMO)[: _LINEAGE_MEMO_MAX // 2]:
            del _LINEAGE_MEMO[stale]
    _LINEAGE_MEMO[key] = _copy_lineage(lineage)


def clear_lineage_cache() -> None:
    """Drop every memoized lineage result (tests, and after a schema change)."""
    _LINEAGE_MEMO.clear()


# --- Schema plumbing ---


_LINEAGE_DIALECT = "duckdb"


def _unquote(identifier: str) -> str:
    return identifier.strip().strip('"').strip("`").lower()


def _table_fqn(table: exp.Table) -> str:
    """``schema.table`` for a table node, or just the name when unqualified."""
    db = (table.db or "").lower()
    name = (table.name or "").lower()
    if not name:
        return ""
    return f"{db}.{name}" if db else name


def _table_alias_map(expression: exp.Expression) -> dict[str, str]:
    """``{alias or name: schema.table}`` for every qualified table in a query."""
    alias_map: dict[str, str] = {}
    for table in expression.find_all(exp.Table):
        if not table.db:
            continue
        fqn = _table_fqn(table)
        if not fqn:
            continue
        alias = (table.alias or "").lower()
        if alias:
            alias_map[alias] = fqn
        alias_map[(table.name or "").lower()] = fqn
        alias_map[fqn] = fqn
    return alias_map


def _safe_type(type_name: str) -> str:
    """A type string sqlglot can build, or ``UNKNOWN``.

    Inferred schemas come from DuckDB, whose type spellings sqlglot does not
    all parse. Lineage does not read types, so an unparseable one must not
    take the whole trace down with it.
    """
    if not type_name:
        return "UNKNOWN"
    try:
        exp.DataType.build(type_name, dialect=_LINEAGE_DIALECT)
    except Exception:
        return "UNKNOWN"
    return type_name


def _mapping_schema(
    parsed: exp.Expression,
    depends_on: list[str],
    catalog: dict[str, list[str]] | None,
    schema: dict[str, list[tuple[str, str]]] | None,
) -> dict[str, dict[str, dict[str, str]]]:
    """``{schema: {table: {column: type}}}`` for the tables this query reads.

    Only the tables in play, not the whole warehouse: sqlglot walks what it
    is given, and a project-wide catalog would be rebuilt per model.
    """
    inferred = {name.lower(): cols for name, cols in (schema or {}).items()}

    wanted: set[str] = {dep.lower() for dep in depends_on}
    for table in parsed.find_all(exp.Table):
        fqn = _table_fqn(table)
        if "." in fqn:
            wanted.add(fqn)

    mapping: dict[str, dict[str, dict[str, str]]] = {}
    for fqn in wanted:
        db, _, name = fqn.partition(".")
        if not db or not name or db in SKIP_SCHEMAS:
            continue
        columns: dict[str, str] = {}
        if fqn in inferred:
            # An inferred schema wins: it describes the model as it will be,
            # the catalog describes it as it was last built.
            columns = {col.lower(): _safe_type(type_name) for col, type_name in inferred[fqn]}
        elif catalog and fqn in catalog:
            columns = {col.lower(): "UNKNOWN" for col in catalog[fqn]}
        if columns:
            mapping.setdefault(db, {})[name] = columns
    return mapping


def _schema_columns(mapping: dict[str, dict[str, dict[str, str]]], fqn: str) -> list[str]:
    """The known columns of ``schema.table`` in a mapping schema, in order."""
    db, _, name = fqn.partition(".")
    return list(mapping.get(db, {}).get(name, {}))


# --- Constructs sqlglot cannot expand on its own ---


def _describe_outputs(query: str, conn: Any) -> list[str] | None:
    """The output column names DuckDB gives ``query``, or None."""
    try:
        rows = conn.execute(f"DESCRIBE {query}").fetchall()
    except Exception as e:
        logger.debug("DESCRIBE for lineage failed: %s", e)
        return None
    return [str(row[0]) for row in rows]


def _describe_lineage(
    query: str,
    conn: Any,
    qualified: exp.Expression,
    mapping: dict[str, dict[str, dict[str, str]]],
) -> dict[str, list[dict[str, str]]] | None:
    """Map DESCRIBE-derived outputs onto same-named source columns.

    The fallback for ``COLUMNS('regex')``: it is a star with a filter, so an
    output column keeps the name of the source column it came from.
    """
    outputs = _describe_outputs(query, conn)
    if outputs is None:
        return None

    tables = sorted({_table_fqn(t) for t in qualified.find_all(exp.Table) if t.db})
    lineage: dict[str, list[dict[str, str]]] = {}
    for out in outputs:
        name = out.lower()
        sources = [
            {"source_table": fqn, "source_column": name}
            for fqn in tables
            if name in _schema_columns(mapping, fqn)
        ]
        lineage[_disambiguate(name, lineage)] = sources
    return lineage


def _pivot_columns(pivot: exp.Pivot) -> list[str]:
    """Columns the pivot consumes: the ON list and the USING aggregates."""
    columns: list[str] = []
    for key in ("expressions", "using"):
        for expression in pivot.args.get(key) or []:
            for column in expression.find_all(exp.Column):
                name = (column.name or "").lower()
                if name and name not in columns:
                    columns.append(name)
    return columns


def _pivot_lineage(
    pivot: exp.Pivot,
    query: str,
    mapping: dict[str, dict[str, dict[str, str]]],
    conn: Any | None,
) -> dict[str, list[dict[str, str]]]:
    """Lineage for a statement-level PIVOT or UNPIVOT.

    A pivot's output columns are values in the data, not names in the SQL, so
    DuckDB is asked what they are. A column that passes through keeps its own
    source; every generated column maps to the pivoted columns as a group.
    Per-column precision through a pivot is not worth chasing.
    """
    source = next((t for t in pivot.find_all(exp.Table) if t.db), None)
    if source is None:
        return {}
    fqn = _table_fqn(source)
    known = _schema_columns(mapping, fqn)
    pivoted = _pivot_columns(pivot)

    outputs: list[str] | None = None
    if conn is not None:
        outputs = _describe_outputs(query, conn)
    if outputs is None and pivot.args.get("unpivot") and known:
        # UNPIVOT is enumerable without the data: everything not consumed,
        # plus the name and value columns.
        into = pivot.args.get("into")
        generated = []
        if into is not None:
            generated = [(c.name or "").lower() for c in into.find_all(exp.Column)]
        outputs = [c for c in known if c not in pivoted] + generated
    if outputs is None:
        return {}

    group = [
        (column.name or "").lower()
        for column in (pivot.args.get("group").expressions if pivot.args.get("group") else [])
    ]

    lineage: dict[str, list[dict[str, str]]] = {}
    for out in outputs:
        name = out.lower()
        if name in group or (name in known and name not in pivoted):
            sources = [{"source_table": fqn, "source_column": name}]
        else:
            sources = [{"source_table": fqn, "source_column": col} for col in pivoted]
        lineage[_disambiguate(name, lineage)] = sources
    return lineage


# --- Column reference index ---


@dataclass(frozen=True)
class ColumnRef:
    """One mention of a column in a query, with where it is written.

    Lineage answers "which source column feeds this output column"; it says
    nothing about a column a model only filters, joins or groups on. Renaming
    a column has to find those too, and has to know where they are, which is
    what this carries: ``start`` and ``end`` are character offsets into the
    query, so an edit is a splice.
    """

    table: str
    """Resolved ``schema.table``, a CTE name, or "" when it cannot be resolved."""

    column: str
    line: int
    """1-based line the identifier ends on, as sqlglot records it."""

    col: int
    """1-based column of the identifier's last character."""

    start: int
    """Character offset of the identifier's first character."""

    end: int
    """Character offset of the identifier's last character."""

    clause: str
    """select, where, join, group, order, having, qualify or window."""


_CLAUSE_TYPES: tuple[tuple[type[exp.Expression], str], ...] = (
    (exp.Where, "where"),
    (exp.Join, "join"),
    (exp.Group, "group"),
    (exp.Having, "having"),
    (exp.Qualify, "qualify"),
    (exp.Order, "order"),
)


def _clause_of(column: exp.Column) -> str:
    """Which clause a column sits in.

    A window's own PARTITION BY / ORDER BY reports ``window`` rather than
    ``order``: it is the window that owns those columns.
    """
    node: exp.Expression | None = column.parent
    while node is not None and not isinstance(node, exp.Select):
        if isinstance(node, exp.Window):
            return "window"
        node = node.parent

    node = column.parent
    while node is not None:
        if isinstance(node, exp.Select):
            return "select"
        for clause_type, name in _CLAUSE_TYPES:
            if isinstance(node, clause_type):
                return name
        node = node.parent
    return "select"


def _select_sources(parsed: exp.Expression, cte_names: set[str]) -> dict[int, list[str]]:
    """``{id(select): [real tables it reads]}`` for unqualified-column attribution."""
    sources: dict[int, list[str]] = {}
    for table in parsed.find_all(exp.Table):
        select = table.find_ancestor(exp.Select)
        if select is None:
            continue
        name = (table.name or "").lower()
        if not (table.db or "") and name in cte_names:
            continue
        fqn = _table_fqn(table)
        if not fqn:
            continue
        bucket = sources.setdefault(id(select), [])
        if fqn not in bucket:
            bucket.append(fqn)
    return sources


def extract_column_references(
    query: str,
    depends_on: list[str] | None = None,
    schema: dict[str, list[tuple[str, str]]] | None = None,
    ast: exp.Expression | None = None,
) -> list[ColumnRef]:
    """Every column mention in ``query``, in source order, with its position.

    This is the walk validation already does over ``exp.Column``, but it keeps
    the positions and the clause instead of throwing them away. Impact
    analysis reads it to notice a downstream model that only filters on a
    column; a rename will need the offsets.

    Positions come from the unqualified AST, so they point into ``query`` as
    written. Qualification is deliberately not run here: it rewrites the tree,
    and the offsets would stop matching the user's text.

    Args:
        query: The SQL to index (config comments should be stripped).
        depends_on: Upstream ``schema.table`` dependencies, used to attribute
            an unqualified column when the query's own scope does not.
        schema: ``{"schema.table": [(column, type), ...]}``, used to attribute
            an unqualified column to the one source that has it.
        ast: Pre-parsed AST for ``query``. Never mutated.

    Returns:
        A ColumnRef per mention, ordered by position in the query.
    """
    parsed = ast if ast is not None else parse_sql(query)
    if parsed is None:
        return []

    cte_names = {
        (cte.alias or "").lower() for cte in parsed.find_all(exp.CTE) if cte.alias
    }
    cte_names.discard("")

    alias_map: dict[str, str] = {}
    cte_alias_map: dict[str, str] = {}
    for table in parsed.find_all(exp.Table):
        name = (table.name or "").lower()
        alias = (table.alias or "").lower()
        if not (table.db or "") and name in cte_names:
            cte_alias_map[name] = name
            if alias:
                cte_alias_map[alias] = name
            continue
        fqn = _table_fqn(table)
        if not fqn:
            continue
        if alias:
            alias_map[alias] = fqn
        alias_map[name] = fqn
        alias_map[fqn] = fqn

    columns_by_table = {
        fqn.lower(): {column.lower() for column, _ in columns}
        for fqn, columns in (schema or {}).items()
    }
    select_sources = _select_sources(parsed, cte_names)
    depends_on = depends_on or []

    references: list[ColumnRef] = []
    for column in parsed.find_all(exp.Column):
        name = (column.name or "").lower()
        if not name or name == "*":
            continue

        qualifier = (column.table or "").lower()
        if qualifier:
            table = cte_alias_map.get(qualifier) or alias_map.get(qualifier, qualifier)
        else:
            table = _attribute_unqualified(
                column, name, select_sources, columns_by_table, depends_on
            )

        meta = getattr(column.this, "meta", None) or {}
        references.append(
            ColumnRef(
                table=table,
                column=name,
                line=int(meta.get("line", 0)),
                col=int(meta.get("col", 0)),
                start=int(meta.get("start", 0)),
                end=int(meta.get("end", 0)),
                clause=_clause_of(column),
            )
        )

    references.sort(key=lambda ref: (ref.start, ref.end))
    return references


def _attribute_unqualified(
    column: exp.Column,
    name: str,
    select_sources: dict[int, list[str]],
    columns_by_table: dict[str, set[str]],
    depends_on: list[str],
) -> str:
    """The table an unqualified column belongs to, or "" when it is a guess."""
    select = column.find_ancestor(exp.Select)
    in_scope = select_sources.get(id(select), []) if select is not None else []

    if len(in_scope) == 1:
        return in_scope[0]

    candidates = in_scope or [dep.lower() for dep in depends_on]
    matches = [fqn for fqn in candidates if name in columns_by_table.get(fqn, set())]
    return matches[0] if len(matches) == 1 else ""
