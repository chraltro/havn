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
            "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
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

            if fqn and "." in fqn:
                schema, table = fqn.split(".", 1)
                if (schema, table, col_name) in lookup:
                    raise MaskedColumnAccessError(
                        f"Column '{col_name}' is masked. "
                        f"Filtering, sorting, or joining on masked columns "
                        f"is not allowed for your role."
                    )

            # No table qualifier -- check by column name
            if not fqn:
                for (s, t, c), p in lookup.items():
                    if c == col_name:
                        raise MaskedColumnAccessError(
                            f"Column '{col_name}' is masked. "
                            f"Filtering, sorting, or joining on masked "
                            f"columns is not allowed for your role."
                        )


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

    # Parse
    try:
        parsed = sqlglot.parse_one(sql, read="duckdb")
    except sqlglot.errors.ParseError:
        logger.debug("SQLGlot parse failed, falling back to post-query masking")
        return sql, False, set()

    alias_map = _build_alias_map(parsed)
    cte_names = _collect_cte_names(parsed)
    lookup = _policy_lookup(active_policies)

    # Deny queries that filter/sort/join on masked columns
    _check_masked_column_access(parsed, alias_map, cte_names, lookup)

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

        matched_policy = None
        if fqn and "." in fqn:
            schema, table = fqn.split(".", 1)
            matched_policy = lookup.get((schema, table, col_name))
        if matched_policy is None and not fqn:
            for (s, t, c), p in lookup.items():
                if c == col_name:
                    matched_policy = p
                    break

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

        matched_policy = None
        if fqn and "." in fqn:
            schema, table = fqn.split(".", 1)
            matched_policy = lookup.get((schema, table, col_name))
        if matched_policy is None and not fqn:
            for (s, t, c), p in lookup.items():
                if c == col_name:
                    matched_policy = p
                    break

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
