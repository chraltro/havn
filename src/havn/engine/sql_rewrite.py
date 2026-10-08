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

import re
from typing import Callable

import sqlglot
from sqlglot import exp


class SQLRewriteError(ValueError):
    """The SQL could not be parsed, so its table references cannot be rewritten."""


# Model SQL carries ``{this}``, ``{start}`` and ``{end}`` placeholders that are
# substituted long after the query is resolved. sqlglot parses ``{start}`` as a
# struct literal and regenerates it as ``{'start': start}``, which the later
# substitution can no longer find. Every round trip of model SQL therefore
# masks them into a plain identifier first and puts them back afterwards.
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_PLACEHOLDER_TOKEN = "__havn_ph_{}__"


def mask_placeholders(sql: str) -> tuple[str, Callable[[str], str]]:
    """Hide ``{this}`` / ``{start}`` / ``{end}`` from the SQL parser.

    Returns the masked SQL and the function that puts the placeholders back.
    Without this an incremental or microbatch model would come out of a
    sqlglot round trip with ``{'start': start}`` where its placeholder used to
    be, and :func:`substitute_batch_window` would find nothing to replace.

    When the SQL holds no placeholder the original string is returned with an
    identity restore, so the common case costs one regex scan and no copy.
    """
    found: list[str] = []

    def _mask(match: re.Match) -> str:
        found.append(match.group(1))
        return _PLACEHOLDER_TOKEN.format(match.group(1))

    masked = _PLACEHOLDER_RE.sub(_mask, sql)
    if not found:
        return sql, _identity

    def restore(out: str) -> str:
        for name in found:
            out = out.replace(_PLACEHOLDER_TOKEN.format(name), "{" + name + "}")
        return out

    return masked, restore


def _identity(out: str) -> str:
    return out


_MASKED_RE = re.compile(r"__havn_ph_([A-Za-z_][A-Za-z0-9_]*)__")


def unmask_placeholders(sql: str) -> str:
    """Turn every mask token in ``sql`` back into its ``{placeholder}``.

    The counterpart to :func:`mask_placeholders` for callers that mask several
    separate queries and stitch the results into one string, where no single
    restore closure covers the whole output. Ephemeral inlining is the case:
    the consumer's query and each upstream's query are masked independently
    and come back as one SQL statement.
    """
    if "__havn_ph_" not in sql:
        return sql
    return _MASKED_RE.sub(lambda m: "{" + m.group(1) + "}", sql)


def table_key(table: exp.Table) -> str:
    """Return the lowercase lookup key for a table node.

    ``bronze.orders`` for a qualified reference, ``orders`` for a bare one.
    The catalog part (``db.schema.table``) is ignored: havn warehouses are a
    single DuckDB catalog, and model references never carry one.
    """
    schema = (table.db or "").lower()
    name = (table.name or "").lower()
    return f"{schema}.{name}" if schema else name


def find_table_refs(
    sql: str,
    *,
    dialect: str = "duckdb",
    skip_catalog_qualified: bool = False,
) -> list[str]:
    """List the distinct table references in ``sql``, excluding CTE names.

    Returns lowercase ``schema.name`` keys in first-seen order. Useful for
    callers that need to check which references a rewrite mapping covers
    before doing the rewrite.

    ``skip_catalog_qualified`` leaves out references that already name a
    catalog (``other.bronze.orders``). Defer wants that: a reference the
    author already pointed at a specific database must not be pointed
    somewhere else.
    """
    masked, _ = mask_placeholders(sql)
    tree = _parse(masked, dialect)
    cte_names = _cte_names(tree)
    seen: list[str] = []
    for table in tree.find_all(exp.Table):
        if _skip_table(table, cte_names, skip_catalog_qualified):
            continue
        key = table_key(table)
        if key not in seen:
            seen.append(key)
    return seen


def rewrite_table_refs(
    sql: str,
    mapping: dict[str, str],
    *,
    dialect: str = "duckdb",
    skip_catalog_qualified: bool = False,
) -> str:
    """Replace table references in ``sql`` according to ``mapping``.

    Args:
        sql: The SQL to rewrite. Must parse as a single statement.
        mapping: ``{"bronze.orders": "_havn_mock_1"}``. Keys are matched
            case-insensitively against each table's ``schema.name`` (or bare
            ``name`` for unqualified references). Values are table names,
            optionally schema-qualified.
        dialect: sqlglot dialect for both parsing and generation.
        skip_catalog_qualified: leave references that already name a catalog
            (``other.bronze.orders``) alone, instead of matching them on
            their ``schema.name`` tail.

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

    masked, restore = mask_placeholders(sql)
    tree = _parse(masked, dialect)
    cte_names = _cte_names(tree)
    lookup = {str(k).lower(): v for k, v in mapping.items()}

    for table in tree.find_all(exp.Table):
        if _skip_table(table, cte_names, skip_catalog_qualified):
            continue
        key = table_key(table)
        target = lookup.get(key)
        if target is None:
            continue
        alias = table.alias
        replacement = exp.to_table(target, dialect=dialect)
        if alias:
            replacement.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
        table.replace(replacement)

    return restore(tree.sql(dialect=dialect))


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


def _skip_table(
    table: exp.Table,
    cte_names: set[str],
    skip_catalog_qualified: bool,
) -> bool:
    """True when this table node must be left exactly as written.

    Three cases: a table function (``read_csv('x.csv')``, ``range(10)``),
    which parses as a Table with an empty name; a CTE name, which shadows a
    real table of the same name; and, for callers that ask, a reference that
    already names a catalog.
    """
    if not table.name:
        return True
    if skip_catalog_qualified and table.catalog:
        return True
    key = table_key(table)
    return "." not in key and key in cte_names
