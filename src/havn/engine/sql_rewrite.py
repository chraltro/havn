"""Table-reference rewriting for SQL models.

havn models reference upstream tables literally (``FROM bronze.orders``),
with no ``ref()`` indirection. Several features need to run a model's query
against something other than the table it names:

- unit tests, which point every upstream at an in-memory mock table,
- ephemeral models, which inline an upstream's SQL,
- defer, which points a missing upstream at another environment's object.

:func:`rewrite_table_refs` is the shared primitive for all three. It parses
the SQL with sqlglot, walks every :class:`sqlglot.exp.Table` node, and swaps
the ones whose ``schema.name`` appears in the mapping. Aliases are preserved,
CTE names are never rewritten (a CTE shadows a real table of the same name),
and unparseable SQL raises rather than silently falling through: a rewrite
that quietly does nothing would run the query against production data.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp


class SQLRewriteError(ValueError):
    """The SQL could not be parsed, so its table references cannot be rewritten."""


def table_key(table: exp.Table) -> str:
    """Return the lowercase lookup key for a table node.

    ``bronze.orders`` for a qualified reference, ``orders`` for a bare one.
    The catalog part (``db.schema.table``) is ignored: havn warehouses are a
    single DuckDB catalog, and model references never carry one.
    """
    schema = (table.db or "").lower()
    name = (table.name or "").lower()
    return f"{schema}.{name}" if schema else name


def find_table_refs(sql: str, *, dialect: str = "duckdb") -> list[str]:
    """List the distinct table references in ``sql``, excluding CTE names.

    Returns lowercase ``schema.name`` keys in first-seen order. Useful for
    callers that need to check which references a rewrite mapping covers
    before doing the rewrite.
    """
    tree = _parse(sql, dialect)
    cte_names = _cte_names(tree)
    seen: list[str] = []
    for table in tree.find_all(exp.Table):
        key = table_key(table)
        if "." not in key and key in cte_names:
            continue
        if key not in seen:
            seen.append(key)
    return seen


def rewrite_table_refs(
    sql: str,
    mapping: dict[str, str],
    *,
    dialect: str = "duckdb",
) -> str:
    """Replace table references in ``sql`` according to ``mapping``.

    Args:
        sql: The SQL to rewrite. Must parse as a single statement.
        mapping: ``{"bronze.orders": "_havn_mock_1"}``. Keys are matched
            case-insensitively against each table's ``schema.name`` (or bare
            ``name`` for unqualified references). Values are table names,
            optionally schema-qualified.
        dialect: sqlglot dialect for both parsing and generation.

    Returns:
        The regenerated SQL. Aliases on rewritten references are preserved,
        so ``FROM bronze.orders o`` becomes ``FROM _havn_mock_1 AS o`` and
        every ``o.column`` reference keeps resolving.

    Raises:
        SQLRewriteError: if the SQL cannot be parsed. There is no regex
            fallback on purpose: a partial rewrite would leave some
            references pointing at real warehouse tables.
    """
    if not mapping:
        return sql

    tree = _parse(sql, dialect)
    cte_names = _cte_names(tree)
    lookup = {str(k).lower(): v for k, v in mapping.items()}

    for table in tree.find_all(exp.Table):
        key = table_key(table)
        # A CTE name shadows a real table, so `WITH orders AS (...)` followed
        # by `FROM orders` must not be redirected to a mock of `orders`.
        if "." not in key and key in cte_names:
            continue
        target = lookup.get(key)
        if target is None:
            continue
        alias = table.alias
        replacement = exp.to_table(target, dialect=dialect)
        if alias:
            replacement.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
        table.replace(replacement)

    return tree.sql(dialect=dialect)


def _parse(sql: str, dialect: str):
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception as e:
        raise SQLRewriteError(f"could not parse SQL ({dialect}): {e}") from e
    if tree is None:
        raise SQLRewriteError("could not parse SQL: empty statement")
    return tree


def _cte_names(tree) -> set[str]:
    return {cte.alias.lower() for cte in tree.find_all(exp.CTE) if cte.alias}
