"""Inline ephemeral models into their consumers as CTEs.

An ephemeral model is never materialized. Every model that references it gets
the ephemeral's query prepended as a named CTE and its references rewritten to
that CTE, so DuckDB sees one query and error messages name the CTE, which is
the ephemeral model's own name. That is the same trade dbt makes.

The generated CTE names carry a ``__havn_`` prefix so they cannot collide with
a CTE the user wrote:

    silver.customers      -> __havn_silver_customers
    a CTE inside it       -> __havn_silver_customers__base
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from havn.engine.sql_analysis import parse_sql_with_error

if TYPE_CHECKING:
    from sqlglot import exp

    from .models import SQLModel

CTE_PREFIX = "__havn_"


class EphemeralInlineError(ValueError):
    """An ephemeral model cannot be inlined into its consumer.

    Raised at build time rather than handing DuckDB SQL that refers to a table
    which was deliberately never created.
    """


def cte_name_for(full_name: str) -> str:
    """The CTE name an ephemeral model is inlined under."""
    return CTE_PREFIX + full_name.replace(".", "_")


def _parse(model: SQLModel, what: str) -> exp.Expression:
    parsed, error = parse_sql_with_error(model.query)
    if parsed is None:
        raise EphemeralInlineError(
            f"{what} {model.full_name} does not parse, so it cannot be "
            f"inlined: {error}"
        )
    return parsed.copy()


def _check_inlinable(model: SQLModel) -> None:
    """Refuse the incremental-only features an ephemeral model cannot have.

    ``{this}`` names the target table, and an ephemeral model has none. A
    watermark or an incremental filter is the same thing by another name.
    """
    if "{this}" in model.query:
        raise EphemeralInlineError(
            f"Ephemeral model {model.full_name} uses {{this}}, which names the "
            "target table an ephemeral model never has. Make it incremental, "
            "or drop the reference."
        )
    for attr, label in (
        ("incremental_filter", "incremental_filter"),
        ("watermark", "@watermark"),
    ):
        if getattr(model, attr, None):
            raise EphemeralInlineError(
                f"Ephemeral model {model.full_name} sets {label}, which only "
                "means something for an incremental model. Remove it, or set "
                "materialized=incremental."
            )


def _collect_ephemerals(
    model: SQLModel,
    model_map: dict[str, SQLModel],
) -> list[SQLModel]:
    """Every ephemeral model reachable from ``model``, dependencies first.

    Depth-first post-order, which is a topological order on a DAG. ``build_dag``
    has already refused cycles by the time execution runs, but the visiting set
    keeps this function honest on its own.
    """
    ordered: list[SQLModel] = []
    done: set[str] = set()
    visiting: set[str] = set()

    def walk(current: SQLModel) -> None:
        for dep_name in current.depends_on:
            dep = model_map.get(dep_name)
            if dep is None or dep.materialized != "ephemeral":
                continue
            if dep.full_name in done:
                continue
            if dep.full_name in visiting:
                raise EphemeralInlineError(
                    f"Circular reference between ephemeral models: "
                    f"{current.full_name} -> {dep.full_name}."
                )
            visiting.add(dep.full_name)
            walk(dep)
            visiting.discard(dep.full_name)
            done.add(dep.full_name)
            ordered.append(dep)

    walk(model)
    return ordered


def _rewrite_refs(
    node: exp.Expression,
    table_map: dict[str, str],
    cte_map: dict[str, str],
) -> None:
    """Point table references at CTEs, in place.

    ``table_map`` maps ``schema.name`` of an ephemeral model to its CTE name;
    ``cte_map`` maps a bare (hoisted) CTE name to its prefixed name. A
    reference that had no alias gets one spelled like the original name, so
    ``customers.email`` still resolves after ``silver.customers`` becomes
    ``__havn_silver_customers``.
    """
    from sqlglot import exp as _exp

    for table in node.find_all(_exp.Table):
        name = table.name or ""
        db = table.db or ""
        if db:
            target = table_map.get(f"{db}.{name}".lower())
        else:
            target = cte_map.get(name.lower())
        if not target:
            continue
        if not table.args.get("alias"):
            table.set(
                "alias", _exp.TableAlias(this=_exp.to_identifier(name))
            )
        table.set("catalog", None)
        table.set("db", None)
        table.set("this", _exp.to_identifier(target))


def _with_arg(tree: exp.Expression) -> str | None:
    """The arg key holding the WITH clause, which sqlglot renamed to ``with_``."""
    for key in ("with_", "with"):
        if tree.args.get(key) is not None:
            return key
    return None


def _build_ctes(
    ephemerals: list[SQLModel],
    table_map: dict[str, str],
) -> list[exp.Expression]:
    """Turn each ephemeral into one or more CTEs, dependencies first."""
    from sqlglot import exp as _exp

    ctes: list[exp.Expression] = []
    for eph in ephemerals:
        _check_inlinable(eph)
        name = cte_name_for(eph.full_name)
        tree = _parse(eph, "Ephemeral model")

        # Hoist the ephemeral's own CTEs ahead of it, renamed so two
        # ephemerals that both define `base` do not fight over the name.
        cte_map: dict[str, str] = {}
        hoisted: list[exp.Expression] = []
        with_key = _with_arg(tree)
        if with_key:
            own_with = tree.args[with_key]
            for cte in own_with.expressions:
                alias = cte.alias or ""
                if not alias:
                    continue
                renamed = f"{name}__{alias}"
                cte_map[alias.lower()] = renamed
                cte.set(
                    "alias",
                    _exp.TableAlias(this=_exp.to_identifier(renamed)),
                )
                hoisted.append(cte)
            tree.set(with_key, None)

        for cte in hoisted:
            _rewrite_refs(cte, table_map, cte_map)
        _rewrite_refs(tree, table_map, cte_map)

        ctes.extend(hoisted)
        ctes.append(
            _exp.CTE(
                this=tree,
                alias=_exp.TableAlias(this=_exp.to_identifier(name)),
            )
        )
        table_map[eph.full_name.lower()] = name
    return ctes


def inline_ephemeral(
    model: SQLModel,
    model_map: dict[str, SQLModel],
) -> str:
    """Return ``model``'s query with every ephemeral reference inlined.

    Returns the query untouched when the model depends on no ephemeral model,
    so a project without any pays nothing and its SQL never goes through a
    sqlglot round trip.
    """
    from sqlglot import exp as _exp

    if not model_map:
        return model.query
    ephemerals = _collect_ephemerals(model, model_map)
    if not ephemerals:
        return model.query
    if model.materialized == "ephemeral":
        _check_inlinable(model)

    table_map: dict[str, str] = {}
    ctes = _build_ctes(ephemerals, table_map)

    tree = _parse(model, "Model")
    _rewrite_refs(tree, table_map, {})

    with_key = _with_arg(tree)
    if with_key:
        own_with = tree.args[with_key]
        # Ahead of the consumer's own CTEs: a non-recursive CTE may only
        # reference the ones declared before it.
        own_with.set("expressions", ctes + list(own_with.expressions))
    else:
        tree.set("with_", _exp.With(expressions=ctes))

    return tree.sql(dialect="duckdb", pretty=True)
