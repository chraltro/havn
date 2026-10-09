"""Pre-query SQL rewriting for column-level masking.

Rewrites SQL *before* execution to inject masking expressions at the
column-reference level, preventing alias-based bypass.  Falls back to
post-query masking (``masking.apply_masking``) when SQLGlot cannot
parse the query.
"""

from __future__ import annotations

import logging
from typing import Any

import duckdb
import sqlglot
from sqlglot import exp

from havn.engine.masking import load_policies

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SQL expression builders -- one per masking method
# ---------------------------------------------------------------------------
# Each builder receives the column SQL string and the policy's
# ``method_config`` dict and returns a DuckDB SQL expression string.


def _sql_hash(col: str, cfg: dict) -> str:
    return f"LEFT(SHA256(CAST({col} AS VARCHAR))::VARCHAR, 8)"


def _sql_redact(col: str, cfg: dict) -> str:
    return "'***'"


def _sql_null(col: str, cfg: dict) -> str:
    return "NULL"


def _sql_partial(col: str, cfg: dict) -> str:
    show_first = int(cfg.get("show_first", 0))
    show_last = int(cfg.get("show_last", 0))
    c = f"CAST({col} AS VARCHAR)"
    if show_first == 0 and show_last == 0:
        return f"REPEAT('*', LENGTH({c}))"
    parts = []
    if show_first:
        parts.append(f"LEFT({c}, {show_first})")
    parts.append(
        f"REPEAT('*', GREATEST(LENGTH({c}) - {show_first + show_last}, 0))"
    )
    if show_last:
        parts.append(f"RIGHT({c}, {show_last})")
    return " || ".join(parts)


def _sql_email(col: str, cfg: dict) -> str:
    c = f"CAST({col} AS VARCHAR)"
    return (
        f"CASE WHEN {c} LIKE '%@%' "
        f"THEN '***' || SUBSTRING({c} FROM POSITION('@' IN {c})) "
        f"ELSE '***' END"
    )


def _sql_phone(col: str, cfg: dict) -> str:
    show_last = int(cfg.get("show_last", 4))
    c = f"CAST({col} AS VARCHAR)"
    return f"'***' || RIGHT({c}, {show_last})"


def _sql_credit_card(col: str, cfg: dict) -> str:
    show_last = int(cfg.get("show_last", 4))
    c = f"REGEXP_REPLACE(CAST({col} AS VARCHAR), '[^0-9]', '', 'g')"
    return (
        f"REPEAT('*', GREATEST(LENGTH({c}) - {show_last}, 0)) || "
        f"RIGHT({c}, {show_last})"
    )


def _sql_first_initial(col: str, cfg: dict) -> str:
    # Simplified: first char + '.' -- complex multi-word logic stays in
    # post-query fallback for exact parity. This covers the common case.
    c = f"CAST({col} AS VARCHAR)"
    return f"LEFT({c}, 1) || '.'"


def _sql_ip_address(col: str, cfg: dict) -> str:
    keep_octets = int(cfg.get("keep_octets", 2))
    keep_octets = max(0, min(keep_octets, 3))
    c = f"CAST({col} AS VARCHAR)"
    parts = []
    for i in range(1, 5):
        if i <= keep_octets:
            parts.append(f"SPLIT_PART({c}, '.', {i})")
        else:
            parts.append("'x'")
    return " || '.' || ".join(parts)


def _sql_range(col: str, cfg: dict) -> str:
    bucket = int(cfg.get("bucket_size", 10000))
    return (
        f"CAST(CAST(FLOOR(CAST({col} AS DOUBLE) / {bucket}) * {bucket} AS BIGINT) AS VARCHAR)"
        f" || '-' || "
        f"CAST(CAST(FLOOR(CAST({col} AS DOUBLE) / {bucket}) * {bucket} + {bucket} AS BIGINT) AS VARCHAR)"
    )


def _sql_noise(col: str, cfg: dict) -> str | None:
    pct = float(cfg.get("percentage", 10.0))
    seed_key = str(cfg.get("seed_key", "")).replace("'", "''")
    # Deterministic noise using HASH for reproducibility.
    # DuckDB HASH returns a UBIGINT; we mod and scale to [-pct, +pct].
    scale = 1_000_000
    pct_scaled = int(pct * 2 * scale / 100)  # range width in micro-units
    if pct_scaled == 0:
        return None  # percentage too small to mask; leave for post-query
    return (
        f"CAST({col} AS DOUBLE) * "
        f"(1.0 + (CAST(HASH(CAST({col} AS VARCHAR) || '{seed_key}') % {pct_scaled} AS DOUBLE) "
        f"- {pct_scaled // 2}) / {scale}.0)"
    )


def _sql_date_shift(col: str, cfg: dict) -> str:
    max_days = int(cfg.get("max_days", 30))
    seed_key = str(cfg.get("seed_key", "")).replace("'", "''")
    range_width = max_days * 2 + 1
    return (
        f"CAST({col} AS DATE) + "
        f"CAST(CAST(HASH(CAST({col} AS VARCHAR) || '{seed_key}') % {range_width} AS INTEGER) "
        f"- {max_days} AS INTEGER)"
    )


def _sql_truncate(col: str, cfg: dict) -> str:
    length = int(cfg.get("length", 3))
    c = f"CAST({col} AS VARCHAR)"
    return (
        f"CASE WHEN LENGTH({c}) <= {length} THEN {c} "
        f"ELSE LEFT({c}, {length}) || '...' END"
    )


def _sql_consistent_hash(col: str, cfg: dict) -> str:
    prefix = cfg.get("prefix", "")
    length = int(cfg.get("length", 8))
    return f"'{prefix}' || LEFT(SHA256(CAST({col} AS VARCHAR))::VARCHAR, {length})"


# Map method name -> SQL builder.  Methods present here are rewritten
# pre-query; methods absent fall through to post-query masking.
_SQL_BUILDERS: dict[str, Any] = {
    "hash": _sql_hash,
    "redact": _sql_redact,
    "null": _sql_null,
    "partial": _sql_partial,
    "email": _sql_email,
    "phone": _sql_phone,
    "credit_card": _sql_credit_card,
    "first_initial": _sql_first_initial,
    "ip_address": _sql_ip_address,
    "range": _sql_range,
    "noise": _sql_noise,
    "date_shift": _sql_date_shift,
    "truncate": _sql_truncate,
    "consistent_hash": _sql_consistent_hash,
}


# ---------------------------------------------------------------------------
# AST rewriting
# ---------------------------------------------------------------------------


def _build_alias_map(parsed: exp.Expression) -> dict[str, str]:
    """Build table alias -> schema.table FQN map from the AST."""
    alias_map: dict[str, str] = {}
    for table in parsed.find_all(exp.Table):
        schema = (table.db or "").lower()
        name = (table.name or "").lower()
        if not name:
            continue
        fqn = f"{schema}.{name}" if schema else name
        alias = (table.alias or "").lower()
        if alias:
            alias_map[alias] = fqn
        alias_map[fqn] = fqn
        # Also map bare table name when schema is present
        if schema:
            alias_map[name] = fqn
    return alias_map


def _collect_cte_names(parsed: exp.Expression) -> set[str]:
    """Collect CTE alias names so we can skip them as table refs."""
    names: set[str] = set()
    for cte in parsed.find_all(exp.CTE):
        if cte.alias:
            names.add(cte.alias.lower())
    return names


def _policy_lookup(
    policies: list[dict],
) -> dict[tuple[str, str, str], dict]:
    """Build (schema, table, column) -> policy lookup."""
    lookup: dict[tuple[str, str, str], dict] = {}
    for p in policies:
        key = (
            p["schema_name"].lower(),
            p["table_name"].lower(),
            p["column_name"].lower(),
        )
        lookup[key] = p
    return lookup


def _resolve_column_table(
    column: exp.Column,
    alias_map: dict[str, str],
    cte_names: set[str],
) -> str | None:
    """Resolve a column's table reference to a schema.table FQN.

    Returns None if the table cannot be resolved (e.g. it's a CTE
    or there's no table qualifier).
    """
    table_ref = (column.table or "").lower()
    if not table_ref:
        return None
    if table_ref in cte_names:
        return None
    return alias_map.get(table_ref)


def _match_policy(
    fqn: str | None,
    col_name: str,
    lookup: dict[tuple[str, str, str], dict],
) -> dict | None:
    """Find the masking policy for a column, given its resolved table ref.

    Handles three cases so a masked column can't slip through:
      * ``schema.table`` FQN  -> exact (schema, table, column) match.
      * bare ``table`` name (no schema in the SQL, e.g. ``FROM customers c``)
        -> match any policy on that table name and column, regardless of
        schema. Over-masking across a same-named table in another schema is
        the safe direction; leaking unmasked PII is not.
      * no table qualifier at all -> match by column name only (best effort).
    """
    if fqn and "." in fqn:
        schema, table = fqn.split(".", 1)
        return lookup.get((schema, table, col_name))
    if fqn:  # bare table name, no schema resolved
        for (s, t, c), p in lookup.items():
            if t == fqn and c == col_name:
                return p
        return None
    # Unqualified column reference.
    for (s, t, c), p in lookup.items():
        if c == col_name:
            return p
    return None


def _mask_expression(col_sql: str, policy: dict) -> str | None:
    """Build the SQL masking expression for a policy.

    Returns None if the method has no SQL builder (residual).
    """
    builder = _SQL_BUILDERS.get(policy["method"])
    if builder is None:
        return None
    cfg = policy.get("method_config") or {}
    try:
        return builder(col_sql, cfg)
    except Exception:
        logger.debug("Failed to build SQL mask for method=%s", policy["method"], exc_info=True)
        return None


def _expand_star(
    conn: duckdb.DuckDBPyConnection,
    schema: str,
    table: str,
) -> list[str] | None:
    """Get column names for a table to expand SELECT *."""
    try:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_catalog = current_database() "
            "AND table_schema = ? AND table_name = ? ORDER BY ordinal_position",
            [schema, table],
        ).fetchall()
        return [r[0] for r in rows] if rows else None
    except Exception:
        return None


class MaskedColumnAccessError(Exception):
    """Raised when a query filters or sorts on a masked column."""
    pass


def _check_masked_column_access(
    parsed: exp.Expression,
    alias_map: dict[str, str],
    cte_names: set[str],
    lookup: dict[tuple[str, str, str], dict],
) -> None:
    """Raise MaskedColumnAccessError if WHERE/HAVING/JOIN ON/ORDER BY
    reference masked columns.

    Non-exempt users must not filter, sort, or join on masked columns
    because that allows confirmation/enumeration of hidden values.
    """
    # Clauses to check: WHERE, HAVING, JOIN ON conditions, ORDER BY
    nodes_to_check: list[exp.Expression] = []

    for select in parsed.find_all(exp.Select):
        where = select.find(exp.Where)
        if where:
            nodes_to_check.append(where)
        having = select.find(exp.Having)
        if having:
            nodes_to_check.append(having)
        # ORDER BY
        order = select.find(exp.Order)
        if order:
            nodes_to_check.append(order)

    # JOIN ON conditions
    for join in parsed.find_all(exp.Join):
        on_clause = join.args.get("on")
        if on_clause:
            nodes_to_check.append(on_clause)

    for node in nodes_to_check:
        for column in node.find_all(exp.Column):
            if isinstance(column.this, exp.Star):
                continue
            col_name = column.name.lower()
            fqn = _resolve_column_table(column, alias_map, cte_names)

            if _match_policy(fqn, col_name, lookup) is not None:
                raise MaskedColumnAccessError(
                    f"Column '{col_name}' is masked. "
                    f"Filtering, sorting, or joining on masked columns "
                    f"is not allowed for your role."
                )


_WHOLE_ROW_MESSAGE = (
    "This query reads a table with masked columns through {what}, which "
    "masking cannot follow. Select the columns by name, or use * / alias.*, "
    "instead."
)


def _policied_tables(lookup: dict[tuple[str, str, str], dict]) -> set[tuple[str, str]]:
    return {(s, t) for (s, t, _c) in lookup}


def _references_policied_table(
    parsed: exp.Expression,
    cte_names: set[str],
    policied: set[tuple[str, str]],
) -> bool:
    policied_names = {t for (_s, t) in policied}
    for table in parsed.find_all(exp.Table):
        name = (table.name or "").lower()
        schema = (table.db or "").lower()
        if not name or (not schema and name in cte_names):
            continue
        if schema and (schema, name) in policied:
            return True
        if not schema and name in policied_names:
            return True
    return False


def _check_whole_row_access(parsed: exp.Expression, scope: "_Scope") -> None:
    """Raise MaskedColumnAccessError for any whole-row or column-set read.

    Masking works per named column: the rewriter replaces a column reference
    with its mask, and the post-query pass masks result columns by name. A
    reference that carries a row's columns inside one value (``to_json(p)``,
    ``SELECT p``, ``row(p.*)``, ``struct_pack(*)``) or produces columns the
    rewriter never sees by name (``COLUMNS(...)``, PIVOT/UNPIVOT, SUMMARIZE)
    slips past both. Only called when the query touches a policied table, and
    any relation counts: a subquery or CTE over the table carries the raw
    column just the same.
    """
    def _deny(what: str) -> None:
        raise MaskedColumnAccessError(_WHOLE_ROW_MESSAGE.format(what=what))

    if isinstance(parsed, exp.Summarize) or parsed.find(exp.Summarize):
        _deny("SUMMARIZE")
    if parsed.find(exp.Pivot):
        _deny("PIVOT/UNPIVOT")
    if parsed.find(exp.Columns):
        _deny("COLUMNS(...)")

    # A star is fine as a select-list item (expanded or masked by name) and
    # inside COUNT(*); anywhere else it packs the row into one value.
    for star in parsed.find_all(exp.Star):
        node = star.parent if isinstance(star.parent, exp.Column) else None
        item = node if node is not None else star
        parent = item.parent
        if isinstance(parent, exp.Select) and item in parent.expressions:
            continue
        if isinstance(parent, exp.Count) and parent.this is item:
            continue
        _deny(f"{item.sql(dialect='duckdb')} inside an expression")

    # A bare name is a row reference only when DuckDB would bind it to a
    # relation: no relation in scope has a column of that name.
    qualified_tables = {
        ((t.db or "").lower(), (t.name or "").lower())
        for t in parsed.find_all(exp.Table) if t.db
    }
    for column in parsed.find_all(exp.Column):
        if isinstance(column.this, exp.Star) or column.find_ancestor(exp.Star):
            continue
        name = column.name.lower()
        qualifier = (column.table or "").lower()
        relations = scope.visible_relations(column)
        if not qualifier:
            if name in relations and not scope.is_visible_column(name, relations):
                _deny(f"row reference '{column.name}'")
        elif (qualifier, name) in qualified_tables:
            cols = relations.get(qualifier)
            if not (cols and name in cols):
                _deny(f"row reference '{column.sql(dialect='duckdb')}'")

    # The rewriter masks select-list expressions only, so a masked column used
    # inside a FROM item (``unnest([p.ssn]) u(v)``) would come out raw.
    masked_names = {c for (_s, _t, c) in scope.lookup}
    for column in parsed.find_all(exp.Column):
        if isinstance(column.this, exp.Star) or column.name.lower() not in masked_names:
            continue
        node = column
        while node.parent is not None and not isinstance(node.parent, exp.Select):
            if isinstance(node.parent, (exp.From, exp.Join)) and node is node.parent.this:
                _deny(f"the masked column '{column.name}' inside a FROM item")
            node = node.parent

    _check_renamed_output(parsed, scope, _deny)


def _check_renamed_output(parsed: exp.Expression, scope: "_Scope", deny) -> None:
    """Refuse shapes that move a masked column's value under another name.

    ``SELECT *`` over a masked table is masked after the query, by result
    column name. Anything that renames the column on the way out escapes that:
    alias column lists, ``* RENAME``/``* REPLACE``, a star in a later UNION
    branch (the first branch names the columns), and a star that emits the
    masked column twice (DuckDB renames the second copy ``ssn_1``).
    """
    for alias in parsed.find_all(exp.TableAlias):
        if not alias.columns:
            continue
        source = alias.parent
        body = source.this if isinstance(source, exp.CTE) else source
        if body is not None and scope.contains_policied(body):
            deny(f"the column alias list '{alias.sql(dialect='duckdb')}'")

    for star in parsed.find_all(exp.Star):
        if (star.args.get("rename") or star.args.get("replace")) and scope.contains_policied(
            star.find_ancestor(exp.Select) or parsed
        ):
            deny("* RENAME / * REPLACE")

    for setop in parsed.find_all(exp.SetOperation):
        branch = setop.expression
        if branch is not None and scope.has_star_over_policied(branch):
            deny("a * in a later UNION/EXCEPT/INTERSECT branch")

    masked_names = {c for (_s, _t, c) in scope.lookup}
    for select in parsed.find_all(exp.Select):
        emitted: list[str] = []
        for item in select.expressions:
            if isinstance(item, exp.Star):
                for src in scope.sources(select):
                    emitted.extend(scope.source_columns(src) or ())
            elif isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
                src = scope.source_by_name(select, (item.table or "").lower())
                if src is not None:
                    emitted.extend(scope.source_columns(src) or ())
            else:
                emitted.append((item.alias_or_name or "").lower())
        for name in masked_names:
            if emitted.count(name) > 1 and scope.has_star_over_policied(select):
                deny(f"two copies of the masked column '{name}'")


class _Scope:
    """Just enough name resolution to tell a column from a relation."""

    def __init__(
        self,
        parsed: exp.Expression,
        conn: duckdb.DuckDBPyConnection,
        lookup: dict[tuple[str, str, str], dict],
    ) -> None:
        self.conn = conn
        self.lookup = lookup
        self.policied = _policied_tables(lookup)
        self.ctes: dict[str, exp.CTE] = {}
        for cte in parsed.find_all(exp.CTE):
            if cte.alias:
                self.ctes[cte.alias.lower()] = cte
        self._catalog: dict[tuple[str, str], set[str] | None] = {}
        self._busy: set[int] = set()

    # -- sources ---------------------------------------------------------

    @staticmethod
    def sources(select: exp.Select) -> list[exp.Expression]:
        out: list[exp.Expression] = []
        frm = select.args.get("from_") or select.args.get("from")
        if frm is not None and frm.this is not None:
            out.append(frm.this)
        for join in select.args.get("joins") or []:
            if join.this is not None:
                out.append(join.this)
        return out

    @staticmethod
    def source_name(src: exp.Expression) -> str:
        alias = src.args.get("alias")
        if isinstance(alias, exp.TableAlias) and alias.name:
            return alias.name.lower()
        if isinstance(src, exp.Table):
            return (src.name or "").lower()
        return ""

    def source_by_name(self, select: exp.Select, name: str) -> exp.Expression | None:
        for src in self.sources(select):
            if self.source_name(src) == name:
                return src
        return None

    def _cte_for(self, src: exp.Expression) -> exp.CTE | None:
        if isinstance(src, exp.Table) and not src.db:
            return self.ctes.get((src.name or "").lower())
        return None

    # -- columns ---------------------------------------------------------

    def _catalog_columns(self, schema: str, table: str) -> set[str] | None:
        key = (schema, table)
        if key not in self._catalog:
            sql = (
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_catalog = current_database() AND table_name = ?"
            )
            params = [table]
            if schema:
                sql += " AND table_schema = ?"
                params.append(schema)
            try:
                rows = self.conn.execute(sql, params).fetchall()
                self._catalog[key] = {r[0].lower() for r in rows} if rows else None
            except Exception:
                self._catalog[key] = None
        return self._catalog[key]

    def source_columns(self, src: exp.Expression) -> set[str] | None:
        """Output column names of a FROM item, or None when unknown."""
        alias = src.args.get("alias")
        if isinstance(alias, exp.TableAlias) and alias.columns:
            return {c.name.lower() for c in alias.columns}
        if id(src) in self._busy:
            return None
        self._busy.add(id(src))
        try:
            cte = self._cte_for(src)
            if cte is not None:
                if cte.args.get("alias") is not None and cte.args["alias"].columns:
                    return {c.name.lower() for c in cte.args["alias"].columns}
                return self.query_columns(cte.this)
            if isinstance(src, exp.Table) and isinstance(src.this, exp.Identifier):
                return self._catalog_columns((src.db or "").lower(), (src.name or "").lower())
            if isinstance(src, (exp.Subquery, exp.Lateral)):
                return self.query_columns(src.this)
            return None
        finally:
            self._busy.discard(id(src))

    def query_columns(self, query: exp.Expression | None) -> set[str] | None:
        while isinstance(query, (exp.Subquery, exp.Paren)):
            query = query.this
        while isinstance(query, exp.SetOperation):
            query = query.this
        if not isinstance(query, exp.Select):
            return None
        cols: set[str] = set()
        for item in query.expressions:
            if isinstance(item, exp.Star):
                for src in self.sources(query):
                    cols |= self.source_columns(src) or set()
            elif isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
                src = self.source_by_name(query, (item.table or "").lower())
                if src is not None:
                    cols |= self.source_columns(src) or set()
            elif item.alias_or_name:
                cols.add(item.alias_or_name.lower())
        return cols

    def visible_relations(self, node: exp.Expression) -> dict[str, set[str] | None]:
        """Relation name -> its columns, for every SELECT enclosing ``node``."""
        out: dict[str, set[str] | None] = {}
        select = node.find_ancestor(exp.Select)
        while select is not None:
            for src in self.sources(select):
                name = self.source_name(src)
                if name and name not in out:
                    out[name] = self.source_columns(src)
            select = select.find_ancestor(exp.Select)
        return out

    @staticmethod
    def is_visible_column(name: str, relations: dict[str, set[str] | None]) -> bool:
        return any(cols and name in cols for cols in relations.values())

    # -- policied reach --------------------------------------------------

    def _is_policied_table(self, table: exp.Table) -> bool:
        name = (table.name or "").lower()
        schema = (table.db or "").lower()
        if not name:
            return False
        if schema:
            return (schema, name) in self.policied
        if name in self.ctes:
            return False
        return any(t == name for (_s, t) in self.policied)

    def contains_policied(self, node: exp.Expression) -> bool:
        """Whether ``node`` reads a policied table, directly or through a CTE."""
        if id(node) in self._busy:
            return False
        self._busy.add(id(node))
        try:
            tables = [node] if isinstance(node, exp.Table) else list(node.find_all(exp.Table))
            for table in tables:
                if self._is_policied_table(table):
                    return True
                cte = self._cte_for(table)
                if cte is not None and cte.this is not None and self.contains_policied(cte.this):
                    return True
            return False
        finally:
            self._busy.discard(id(node))

    def has_star_over_policied(self, node: exp.Expression) -> bool:
        """Whether a select-list star inside ``node`` expands a policied source."""
        selects = [node] if isinstance(node, exp.Select) else []
        selects += [s for s in node.find_all(exp.Select) if s is not node]
        for select in selects:
            for item in select.expressions:
                if isinstance(item, exp.Star):
                    if any(self.contains_policied(s) for s in self.sources(select)):
                        return True
                elif isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
                    src = self.source_by_name(select, (item.table or "").lower())
                    if src is not None and self.contains_policied(src):
                        return True
        return False


def _mentions_policied_table(sql: str, policied: set[tuple[str, str]]) -> bool:
    """Textual fallback for SQL sqlglot cannot analyse: does any identifier
    outside strings and comments name a policied table?"""
    import re

    from havn.engine.sql_safety import strip_sql_comments_and_strings

    names = {t for (_s, t) in policied}
    cleaned = strip_sql_comments_and_strings(sql)
    for ident in re.findall(r'"([^"]*)"|([A-Za-z_][A-Za-z0-9_$]*)', cleaned):
        word = (ident[0] or ident[1]).lower()
        if word in names:
            return True
    return False


def rewrite_query_with_masking(
    sql: str,
    user_role: str,
    conn: duckdb.DuckDBPyConnection,
) -> tuple[str, bool, set[str]]:
    """Rewrite SQL to inject masking expressions at the column source level.

    Parameters
    ----------
    sql : the original SQL query
    user_role : the requesting user's role (for exemption checks)
    conn : DuckDB connection (for loading policies and resolving ``SELECT *``)

    Returns
    -------
    (rewritten_sql, was_rewritten, handled_policy_ids)
        *was_rewritten*: True if pre-query masking was applied.
        *handled_policy_ids*: policy IDs successfully handled pre-query.
        Pass these as ``skip_policy_ids`` to ``apply_masking`` so they
        are not double-masked.

    Raises
    ------
    MaskedColumnAccessError
        If the query filters, sorts, or joins on a masked column.
    """
    # Load policies and filter by role exemption
    policies = load_policies(conn)
    if not policies:
        return sql, False, set()

    active_policies = [
        p for p in policies if user_role not in p["exempted_roles"]
    ]
    if not active_policies:
        return sql, False, set()

    lookup = _policy_lookup(active_policies)
    policied = _policied_tables(lookup)

    # Parse
    try:
        parsed = sqlglot.parse_one(sql, read="duckdb")
    except sqlglot.errors.ParseError:
        parsed = None
    if parsed is None or isinstance(parsed, exp.Command):
        # Post-query masking matches result columns by name, which is only
        # safe when we know the query's shape. EXPLAIN returns a plan, not
        # rows, so it keeps working.
        is_explain = (
            isinstance(parsed, exp.Command)
            and str(parsed.this).strip().upper() == "EXPLAIN"
        )
        if not is_explain and _mentions_policied_table(sql, policied):
            raise MaskedColumnAccessError(
                "This query reads a table with masked columns but could not be "
                "analysed for masking. Rewrite it as a plain SELECT."
            )
        logger.debug("SQLGlot parse failed, falling back to post-query masking")
        return sql, False, set()

    alias_map = _build_alias_map(parsed)
    cte_names = _collect_cte_names(parsed)

    # Deny queries that filter/sort/join on masked columns
    _check_masked_column_access(parsed, alias_map, cte_names, lookup)
    star_over_policied = False
    if _references_policied_table(parsed, cte_names, policied):
        scope = _Scope(parsed, conn, lookup)
        _check_whole_row_access(parsed, scope)
        star_over_policied = scope.has_star_over_policied(parsed)

    rewritten_any = False
    handled_ids: set[str] = set()

    # Process each SELECT statement in the AST
    for select in parsed.find_all(exp.Select):
        new_expressions = []
        select_modified = False

        for sel_expr in select.expressions:
            result = _rewrite_select_expression(
                sel_expr, alias_map, cte_names, lookup, conn,
            )
            if result is not None:
                new_expr, expr_handled = result
                new_expressions.append(new_expr)
                handled_ids.update(expr_handled)
                if expr_handled or new_expr is not sel_expr:
                    select_modified = True
            else:
                new_expressions.append(sel_expr)

        if select_modified:
            select.set("expressions", new_expressions)
            rewritten_any = True

    if not rewritten_any:
        return sql, False, set()

    if star_over_policied:
        # Callers skip the post-query pass for every policy in handled_ids,
        # but a * over the table still emits the raw column for that pass to
        # mask (SELECT *, ssn LIKE '1%' AS x / LATERAL (SELECT p.ssn AS z)).
        raise MaskedColumnAccessError(
            "This query combines * over a table with masked columns with "
            "expressions on a masked column, which masking cannot follow. "
            "List the columns by name instead."
        )

    try:
        rewritten_sql = parsed.sql(dialect="duckdb")
    except Exception:
        logger.debug("SQLGlot SQL generation failed, falling back")
        return sql, False, set()

    return rewritten_sql, rewritten_any, handled_ids


def _rewrite_select_expression(
    sel_expr: exp.Expression,
    alias_map: dict[str, str],
    cte_names: set[str],
    lookup: dict[tuple[str, str, str], dict],
    conn: duckdb.DuckDBPyConnection,
) -> tuple[exp.Expression, set[str]] | None:
    """Rewrite a single SELECT expression to apply masking.

    Returns (new_expression, handled_policy_ids) or None if unchanged.
    """
    handled: set[str] = set()

    # Handle SELECT *
    if isinstance(sel_expr, exp.Star):
        return _rewrite_star(sel_expr, alias_map, cte_names, lookup, conn, handled)

    # Handle table.* (e.g. SELECT c.* FROM customers c)
    if isinstance(sel_expr, exp.Column) and isinstance(sel_expr.this, exp.Star):
        table_ref = (sel_expr.table or "").lower()
        fqn = alias_map.get(table_ref)
        if fqn and "." in fqn:
            schema, table = fqn.split(".", 1)
            return _rewrite_table_star(
                sel_expr, schema, table, alias_map, cte_names, lookup, conn, handled
            )
        return None

    # Determine if sel_expr is a top-level column reference (possibly aliased)
    # vs a complex expression containing column references.
    # For top-level columns: we build a replacement expression with alias.
    # For complex expressions: we replace column nodes in-place.
    is_aliased = isinstance(sel_expr, exp.Alias)
    inner_expr = sel_expr.this if is_aliased else sel_expr
    existing_alias = sel_expr.alias if is_aliased else ""

    # If the inner expression is a bare column reference, handle it directly
    if isinstance(inner_expr, exp.Column) and not isinstance(inner_expr.this, exp.Star):
        col_name = inner_expr.name.lower()
        fqn = _resolve_column_table(inner_expr, alias_map, cte_names)
        matched_policy = _match_policy(fqn, col_name, lookup)

        if matched_policy and not matched_policy.get("condition_column"):
            col_sql = inner_expr.sql(dialect="duckdb")
            mask_sql = _mask_expression(col_sql, matched_policy)
            if mask_sql:
                try:
                    mask_node = sqlglot.parse_one(mask_sql, read="duckdb")
                    # Preserve or add alias
                    alias_name = existing_alias or inner_expr.output_name or col_name
                    result_expr = exp.Alias(
                        this=mask_node,
                        alias=exp.to_identifier(alias_name),
                    )
                    handled.add(matched_policy["id"])
                    return result_expr, handled
                except Exception:
                    logger.debug("Failed to parse mask: %s", mask_sql, exc_info=True)
        return None

    # For complex expressions (UPPER(email), email || ' ' || name, etc.):
    # Walk inner column references and replace in-place.
    changed = False
    columns_in_expr = list(inner_expr.find_all(exp.Column))
    for column in columns_in_expr:
        if isinstance(column.this, exp.Star):
            continue
        col_name = column.name.lower()
        fqn = _resolve_column_table(column, alias_map, cte_names)
        matched_policy = _match_policy(fqn, col_name, lookup)

        if matched_policy is None:
            continue
        if matched_policy.get("condition_column"):
            continue

        col_sql = column.sql(dialect="duckdb")
        mask_sql = _mask_expression(col_sql, matched_policy)
        if mask_sql is None:
            continue

        try:
            mask_node = sqlglot.parse_one(mask_sql, read="duckdb")
            column.replace(mask_node)
            changed = True
            handled.add(matched_policy["id"])
        except Exception:
            logger.debug("Failed to parse mask: %s", mask_sql, exc_info=True)

    if not changed:
        return None

    return sel_expr, handled


def _rewrite_star(
    star_expr: exp.Star,
    alias_map: dict[str, str],
    cte_names: set[str],
    lookup: dict[tuple[str, str, str], dict],
    conn: duckdb.DuckDBPyConnection,
    handled: set[str],
) -> tuple[exp.Expression, set[str]] | None:
    """Expand SELECT * and apply masking to matched columns.

    If we can't resolve the table columns, returns None (no rewrite).
    """
    # Find the tables in scope -- collect all FQNs from alias_map
    tables = set()
    for alias, fqn in alias_map.items():
        if "." in fqn and alias not in cte_names:
            tables.add(fqn)

    if not tables:
        return None

    # Check if any policies match these tables
    has_match = False
    for (s, t, c), p in lookup.items():
        if f"{s}.{t}" in tables and not p.get("condition_column"):
            has_match = True
            break
    if not has_match:
        return None

    # Expand * into explicit columns with masking
    # For simplicity with multiple tables, we only expand if there's
    # exactly one non-CTE table (most common case for SELECT *)
    if len(tables) == 1:
        fqn = next(iter(tables))
        schema, table = fqn.split(".", 1)
        columns = _expand_star(conn, schema, table)
        if not columns:
            return None

        # Build explicit column list with masking applied
        parts = []
        for col_name in columns:
            key = (schema, table, col_name.lower())
            policy = lookup.get(key)
            col_ref = f'"{schema}"."{table}"."{col_name}"'

            if policy and not policy.get("condition_column"):
                mask_sql = _mask_expression(col_ref, policy)
                if mask_sql:
                    parts.append(f"{mask_sql} AS \"{col_name}\"")
                    handled.add(policy["id"])
                    continue

            parts.append(f'"{col_name}"')

        if parts:
            try:
                combined = ", ".join(parts)
                # Parse as a select to extract expressions
                wrapper = sqlglot.parse_one(f"SELECT {combined}", read="duckdb")
                new_exprs = list(wrapper.find(exp.Select).expressions)
                if len(new_exprs) == 1:
                    return new_exprs[0], handled
                # Multiple expressions from star expansion -- can't splice
                # into the single-expression slot. Return None for post-query.
                return None
            except Exception:
                return None

    return None


def _rewrite_table_star(
    sel_expr: exp.Expression,
    schema: str,
    table: str,
    alias_map: dict[str, str],
    cte_names: set[str],
    lookup: dict[tuple[str, str, str], dict],
    conn: duckdb.DuckDBPyConnection,
    handled: set[str],
) -> tuple[exp.Expression, set[str]] | None:
    """Expand table.* and apply masking."""
    columns = _expand_star(conn, schema, table)
    if not columns:
        return None

    has_match = False
    for col_name in columns:
        key = (schema, table, col_name.lower())
        if key in lookup:
            has_match = True
            break
    if not has_match:
        return None

    # Same logic as _rewrite_star for a single table
    parts = []
    for col_name in columns:
        key = (schema, table, col_name.lower())
        policy = lookup.get(key)
        col_ref = f'"{schema}"."{table}"."{col_name}"'

        if policy and not policy.get("condition_column"):
            mask_sql = _mask_expression(col_ref, policy)
            if mask_sql:
                parts.append(f"{mask_sql} AS \"{col_name}\"")
                handled.add(policy["id"])
                continue

        parts.append(f'"{schema}"."{table}"."{col_name}"')

    if not parts:
        return None

    try:
        combined = ", ".join(parts)
        wrapper = sqlglot.parse_one(f"SELECT {combined}", read="duckdb")
        new_exprs = list(wrapper.find(exp.Select).expressions)
        if len(new_exprs) == 1:
            return new_exprs[0], handled
        return None
    except Exception:
        return None
