"""Pre-query governance: masking plus row-level security, verified by the plan.

:func:`govern_query` is the one function every read path calls. It

1. resolves every relation the query names the way DuckDB will (quoting,
   case, ``catalog.table`` spellings, unqualified names) and rewrites the
   references as ``schema.name`` so the rest of the pipeline sees one
   spelling;
2. refuses what it cannot follow: ``_havn`` for non-admins, a view or model
   whose lineage could not be traced while it reads masked columns, a SQL
   macro whose body reads a governed table, a dynamic ``PIVOT`` over one,
   storage-statistics functions;
3. applies column masking (``masking_rewriter``, with explicit and
   lineage-inherited policies);
4. applies row policies: every view whose body reads a row-protected table
   is inlined, and every reference to a row-protected table becomes
   ``(SELECT * FROM t WHERE <filter>) AS t``, so aliases, CTEs, subqueries,
   joins, set operations, LATERAL and correlated subqueries all read the
   filtered rows;
5. asks DuckDB for the plan (``EXPLAIN``) and checks that no governed table
   is scanned more often than the rewritten query accounts for. That is the
   backstop for anything step 1 did not see: a path DuckDB resolves that the
   rewriter does not know about shows up as an extra scan, and the query is
   refused.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import duckdb
import sqlglot
from sqlglot import exp

from havn.engine.masking_rewriter import MaskedColumnAccessError

from .catalog import (
    INTERNAL_SCHEMA,
    CatalogInfo,
    Key,
    ViewerPolicies,
    called_functions,
    get_snapshot,
    is_cte_reference,
)
from .viewer import Viewer, viewer_from_user

logger = logging.getLogger("havn.governance")


class GovernanceError(MaskedColumnAccessError):
    """A query was refused because governance could not be applied to it.

    Subclasses MaskedColumnAccessError so every caller that already turns a
    masking refusal into a 403 handles this the same way.
    """


# Table functions that read storage-level statistics (per-segment min/max
# values), which would expose a masked column's values or a filtered
# table's rows without scanning it.
_STATS_FUNCTIONS = frozenset({
    "pragma_storage_info", "storage_info", "pragma_metadata_info",
})

_EXPLAIN_RE = re.compile(
    r"^\s*explain(?![A-Za-z0-9_])\s*(?:analy[sz]e(?![A-Za-z0-9_])\s*)?(?:\([^()]*\)\s*)?",
    re.IGNORECASE,
)


@dataclass
class GovernedQuery:
    """The SQL to run for a viewer, and what still has to happen to the result."""

    sql: str
    viewer: Viewer
    rewritten: bool = False
    handled_ids: set[str] = field(default_factory=set)
    masks: list[dict] = field(default_factory=list)
    masking_rewritten: bool = False

    @property
    def needs_post_mask(self) -> bool:
        """Whether some active policy may still need the post-query pass."""
        if not self.masks:
            return False
        return any(p["id"] not in self.handled_ids for p in self.masks)

    def post_mask(self, columns: list[str], rows: list[list[Any]], conn) -> list[list[Any]]:
        """Mask result rows by column name for policies the rewrite did not handle."""
        if not self.masks or not rows:
            return rows
        from havn.engine.masking import apply_masking

        return apply_masking(
            columns, rows, self.viewer.role, conn,
            skip_policy_ids=self.handled_ids if self.masking_rewritten else None,
            policies=self.masks,
        )


def viewer_policies(
    conn: duckdb.DuckDBPyConnection,
    viewer: Viewer | dict | None,
    project_dir: Path | None = None,
) -> ViewerPolicies:
    snapshot = get_snapshot(conn, project_dir)
    return snapshot.for_viewer(viewer_from_user(viewer))


def is_governed(conn, viewer, project_dir: Path | None = None) -> bool:
    """Whether any masking or row policy applies to ``viewer``."""
    v = viewer_from_user(viewer)
    try:
        return viewer_policies(conn, v, project_dir).subject_to_policies
    except Exception:
        logger.warning("Could not evaluate governance for %s", v.username, exc_info=True)
        # Fail closed for non-admins: treat them as governed.
        return not v.is_admin


def _refuse(message: str) -> None:
    raise GovernanceError(message)


def split_explain(sql: str) -> tuple[str, str]:
    """``("EXPLAIN ANALYZE ", "SELECT ...")`` for an EXPLAIN, else ``("", sql)``."""
    prefix = ""
    rest = sql
    while True:
        m = _EXPLAIN_RE.match(rest)
        if not m or m.end() == 0:
            break
        prefix += rest[: m.end()]
        rest = rest[m.end():]
    return prefix, rest


def _mentions(sql: str, names: set[str]) -> bool:
    from havn.engine.sql_safety import strip_sql_comments_and_strings

    cleaned = strip_sql_comments_and_strings(sql)
    for ident in re.findall(r'"([^"]*)"|([A-Za-z_][A-Za-z0-9_$]*)', cleaned):
        word = (ident[0] or ident[1]).lower()
        if word in names:
            return True
    return False


# ---------------------------------------------------------------------------
# Reference checks
# ---------------------------------------------------------------------------


def _write_targets(tree: exp.Expression) -> set[int]:
    """ids of Table nodes a statement writes to (never wrapped)."""
    targets: set[int] = set()

    def table_of(node):
        if isinstance(node, exp.Schema):
            node = node.this
        return node if isinstance(node, exp.Table) else None

    for node in tree.walk():
        if isinstance(node, (exp.Insert, exp.Create, exp.Merge)):
            t = table_of(node.this)
            if t is not None:
                targets.add(id(t))
        elif isinstance(node, (exp.Update, exp.Delete)):
            t = table_of(node.this)
            if t is not None:
                targets.add(id(t))
        elif isinstance(node, exp.Copy) and node.args.get("kind"):
            t = table_of(node.this)
            if t is not None:
                targets.add(id(t))
    return targets


def _check_references(
    tree: exp.Expression,
    vp: ViewerPolicies,
    catalog: CatalogInfo,
    targets: set[int],
) -> None:
    governed = vp.governed_base_tables()
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            fname = (table.this.name if hasattr(table.this, "name") else "") or ""
            if fname.lower() in _STATS_FUNCTIONS:
                _refuse(f"{fname}() reads storage statistics and is not available to you.")
            continue
        if is_cte_reference(table):
            continue
        key = catalog.resolve(table)
        if not isinstance(key, tuple):
            continue
        if key[0] in vp.blocked_schemas and id(table) not in targets:
            _refuse(f"The {INTERNAL_SCHEMA} schema holds havn's own metadata and is admin-only.")
        if key in vp.opaque:
            _refuse(f"{key[0]}.{key[1]} cannot be governed for you: {vp.opaque[key]}.")
        if catalog.is_view(key):
            reach = set(catalog.closure(key))
            blocked = {k for k in reach if k[0] in vp.blocked_schemas}
            if blocked:
                _refuse(
                    f"The view {key[0]}.{key[1]} reads {INTERNAL_SCHEMA} metadata, which is admin-only."
                )
            opaque = sorted(k for k in reach if k in vp.opaque)
            if opaque:
                k = opaque[0]
                _refuse(f"{key[0]}.{key[1]} reads {k[0]}.{k[1]}, which cannot be governed for you: {vp.opaque[k]}.")
            view = catalog.views[key]
            if view.query is None and reach & governed:
                _refuse(f"The view {key[0]}.{key[1]} could not be analysed, and it reads governed data.")
    for name in called_functions(tree):
        if name in _STATS_FUNCTIONS:
            _refuse(f"{name}() reads storage statistics and is not available to you.")
        macro = catalog.macros.get(name)
        if macro is None:
            continue
        if not macro.parsed or set(catalog.macro_closure(name)) & governed:
            _refuse(
                f"The macro {name}() reads governed data, which masking and row "
                "policies cannot follow into a macro body."
            )
    if isinstance(tree, exp.Pivot) and _reaches(tree, vp, catalog, governed):
        _refuse(
            "A PIVOT statement over governed data cannot be governed. Use "
            "PIVOT ... ON col IN (...) inside a SELECT, or aggregate with FILTER."
        )


def _reaches(tree: exp.Expression, vp: ViewerPolicies, catalog: CatalogInfo, governed: set[Key]) -> bool:
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier) or is_cte_reference(table):
            continue
        key = catalog.resolve(table)
        if isinstance(key, tuple) and set(catalog.closure(key)) & governed:
            return True
    return False


# ---------------------------------------------------------------------------
# Row-level security
# ---------------------------------------------------------------------------


def _filter_condition(policies: list[dict], viewer: Viewer) -> exp.Expression:
    from havn.engine.row_policies import render_filter

    parts: list[exp.Expression] = []
    for p in policies:
        if p.get("deny"):
            parts.append(exp.false())
            continue
        try:
            parts.append(exp.Paren(this=render_filter(p["filter_sql"], viewer)))
        except ValueError:
            parts.append(exp.false())
    cond = parts[0]
    for part in parts[1:]:
        cond = exp.and_(cond, part)
    return cond


def _alias_parts(node: exp.Expression, default: str) -> tuple[exp.Identifier, list]:
    alias = node.args.get("alias")
    if isinstance(alias, exp.TableAlias) and alias.this is not None:
        return alias.this.copy(), [c.copy() for c in alias.columns]
    return exp.to_identifier(default), []


def _wrap(source: exp.Expression, condition: exp.Expression, alias: exp.Identifier, columns: list) -> exp.Subquery:
    select = exp.Select(expressions=[exp.Star()]).from_(source, copy=False).where(condition, copy=False)
    return exp.Subquery(this=select, alias=exp.TableAlias(this=alias, columns=columns or None))


def _replace_relation(table: exp.Table, replacement: exp.Subquery) -> None:
    parent = table.parent
    if isinstance(parent, exp.Summarize):
        # SUMMARIZE takes a query, not an aliased subquery.
        table.replace(replacement.this)
        return
    pivots = table.args.get("pivots")
    if pivots:
        replacement.set("pivots", pivots)
    table.replace(replacement)


def apply_row_security(
    tree: exp.Expression,
    vp: ViewerPolicies,
    catalog: CatalogInfo,
    *,
    search: tuple[str, ...] = ("main",),
    targets: set[int] | None = None,
    skip: frozenset[str] = frozenset(),
    depth: int = 0,
) -> exp.Expression:
    """Inline views that read row-protected tables and wrap protected tables."""
    if depth > 16:
        _refuse("Views nest too deeply to apply row policies.")
    if not vp.rows:
        return tree
    targets = targets or set()
    viewer = vp.viewer
    for table in list(tree.find_all(exp.Table)):
        if not isinstance(table.this, exp.Identifier) or id(table) in targets:
            continue
        if is_cte_reference(table):
            continue
        if not table.db and not table.catalog and (table.name or "").lower() in skip:
            continue
        key = catalog.resolve(table, search=search)
        if not isinstance(key, tuple):
            continue
        if catalog.is_view(key) and vp.reaches_rls(key):
            view = catalog.views[key]
            if view.query is None:
                _refuse(f"The view {key[0]}.{key[1]} could not be analysed to apply row policies.")
            body = sqlglot.parse_one(view.query, read="duckdb")
            body = apply_row_security(body, vp, catalog, depth=depth + 1)
            alias, columns = _alias_parts(table, key[1])
            if not columns and view.columns:
                columns = [exp.to_identifier(c) for c in view.columns]
            sub = exp.Subquery(this=body, alias=exp.TableAlias(this=alias, columns=columns or None))
            if key in vp.rows:  # an explicit policy on the view itself
                inner = sub.copy()
                inner.set("alias", exp.TableAlias(this=exp.to_identifier(key[1]), columns=columns or None))
                sub = _wrap(inner, _filter_condition(vp.rows[key], viewer), alias, [])
            _replace_relation(table, sub)
        elif key in vp.rows:
            alias, columns = _alias_parts(table, key[1])
            inner = table.copy()
            inner.set("alias", None)
            inner.set("pivots", None)
            sub = _wrap(inner, _filter_condition(vp.rows[key], viewer), alias, columns)
            _replace_relation(table, sub)
    return tree


def _restrict_dml_target(tree: exp.Expression, vp: ViewerPolicies, catalog: CatalogInfo) -> None:
    """UPDATE/DELETE on a row-protected table touch only the rows the viewer sees."""
    stmt = tree
    if not isinstance(stmt, (exp.Update, exp.Delete, exp.Merge)):
        return
    target = stmt.this
    if not isinstance(target, exp.Table):
        return
    key = catalog.resolve(target)
    if not isinstance(key, tuple):
        return
    masked_cols = {
        p["column_name"].lower() for p in vp.masks
        if (p["schema_name"].lower(), p["table_name"].lower()) == key
    }
    if masked_cols:
        used = {c.name.lower() for c in stmt.find_all(exp.Column)}
        if used & masked_cols:
            _refuse(
                f"This statement reads masked columns of {key[0]}.{key[1]} "
                f"({', '.join(sorted(used & masked_cols))}), which would copy their raw values."
            )
    if key not in vp.rows:
        return
    if isinstance(stmt, exp.Merge):
        _refuse(f"MERGE into {key[0]}.{key[1]}, which has row policies, is not available to you.")
    cond = _filter_condition(vp.rows[key], vp.viewer)
    qualifier = target.alias or target.name

    def qualify(n: exp.Expression) -> exp.Expression:
        # Top-level filter columns only: a subquery's columns are its own.
        if isinstance(n, exp.Column) and not n.table and n.find_ancestor(exp.Select) is None:
            return exp.column(n.name, table=qualifier, quoted=True)
        return n

    cond = cond.transform(qualify)
    where = stmt.args.get("where")
    if where is not None:
        stmt.set("where", exp.Where(this=exp.and_(exp.Paren(this=where.this), cond)))
    else:
        stmt.set("where", exp.Where(this=cond))


def _fix_qualified_columns(tree: exp.Expression) -> None:
    """``silver.customers.x`` -> ``customers.x`` where the relation was wrapped.

    After wrapping, a relation is a subquery with an alias, and DuckDB no
    longer accepts a schema in front of its columns.
    """
    for column in tree.find_all(exp.Column):
        if column.args.get("db") is None:
            continue
        column.set("catalog", None)
        column.set("db", None)


# ---------------------------------------------------------------------------
# Plan verification
# ---------------------------------------------------------------------------


def plan_scans(conn: duckdb.DuckDBPyConnection, sql: str, params: Any = None) -> Counter:
    """Base tables in DuckDB's plan for ``sql``, with how often each is scanned."""
    rows = conn.execute("EXPLAIN (FORMAT JSON) " + sql, params).fetchall() if params else \
        conn.execute("EXPLAIN (FORMAT JSON) " + sql).fetchall()
    out: Counter = Counter()

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        info = node.get("extra_info")
        if isinstance(info, dict):
            table = info.get("Table")
            if isinstance(table, str) and table:
                out[table.lower()] += 1
        for child in node.get("children", []) or []:
            walk(child)

    for row in rows:
        try:
            walk(json.loads(row[1]))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def allowed_scans(tree: exp.Expression, catalog: CatalogInfo, skip: frozenset[str] = frozenset()) -> Counter:
    allowed: Counter = Counter()
    for key, n in catalog.reference_counts(tree).items():
        for base, m in catalog.closure(key).items():
            allowed[base] += n * m
    for name in called_functions(tree):
        for base, m in catalog.macro_closure(name).items():
            allowed[base] += m
    return allowed


def verify_plan(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    tree: exp.Expression | None,
    vp: ViewerPolicies,
    catalog: CatalogInfo,
    params: Any = None,
    skip: frozenset[str] = frozenset(),
) -> None:
    governed = vp.governed_base_tables()
    if not governed:
        return
    try:
        scans = plan_scans(conn, sql, params)
    except duckdb.Error as e:
        # The statement does not plan (a syntax or binder error the run will
        # report, or a statement kind EXPLAIN does not cover). Without a plan
        # nothing is verified, so refuse anything that reaches governed data.
        if tree is None or _reaches(tree, vp, catalog, governed) or called_functions(tree) & set(catalog.macros):
            raise GovernanceError(f"Query could not be verified for governance: {e}") from None
        return
    allowed = allowed_scans(tree, catalog, skip) if tree is not None else Counter()
    for fq, count in scans.items():
        cat, _, rest = fq.partition(".")
        schema, _, name = rest.partition(".")
        if cat != catalog.current_db or not name:
            continue
        key = (schema, name)
        if key in governed and count > allowed.get(key, 0):
            _refuse(
                f"This query reads {schema}.{name} in a way governance could not "
                "follow (through a function, macro or construct the rewriter does "
                "not see), so it was refused."
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def govern_query(
    sql: str,
    viewer: Viewer | dict | None,
    conn: duckdb.DuckDBPyConnection,
    *,
    project_dir: Path | None = None,
    params: Any = None,
    registered: frozenset[str] = frozenset(),
    allow_writes: bool = False,
) -> GovernedQuery:
    """Rewrite ``sql`` so it returns only what ``viewer`` may see.

    Raises GovernanceError (a MaskedColumnAccessError) when the query cannot
    be governed. ``registered`` names DataFrames registered on the
    connection (they shadow catalog tables). ``allow_writes`` admits DML/DDL
    statements, governing the data they read (governed Python and the SQL
    statement API); read-only surfaces validate read-only-ness separately.
    """
    v = viewer_from_user(viewer)
    vp = viewer_policies(conn, v, project_dir)
    if vp.unrestricted:
        return GovernedQuery(sql=sql, viewer=v)
    catalog = vp.snapshot.catalog
    prefix, body = split_explain(sql)

    try:
        tree = sqlglot.parse_one(body, read="duckdb")
    except sqlglot.errors.SqlglotError:
        tree = None
    if tree is None or isinstance(tree, exp.Command):
        names = {k[1] for k in vp.governed_base_tables()} | {k[1] for k in vp.masked}
        names |= {k[1] for k in catalog.views}  # any view may hide a governed read
        names |= set(vp.blocked_schemas)
        if _mentions(body, names):
            _refuse(
                "This query reads governed data but could not be analysed. "
                "Rewrite it as a plain SELECT."
            )
        if not isinstance(tree, exp.Command):
            verify_plan(conn, body, None, vp, catalog, params)
        return GovernedQuery(sql=sql, viewer=v, masks=vp.masks)
    if isinstance(tree, (exp.Describe, exp.Show)):
        return GovernedQuery(sql=sql, viewer=v)

    is_query = isinstance(tree, (exp.Query, exp.Summarize, exp.Pivot, exp.Values))
    if not is_query and not allow_writes:
        _refuse("Only queries can be governed on this surface.")

    targets = _write_targets(tree)
    catalog.canonicalize(tree, skip=registered)
    targets = _write_targets(tree)
    _check_references(tree, vp, catalog, targets)
    _restrict_dml_target(tree, vp, catalog)

    canonical = tree.sql(dialect="duckdb")
    handled: set[str] = set()
    masked_sql = canonical
    masking_rewritten = False
    # Only the policies of relations this query can reach: the masking
    # rewriter matches unqualified columns by name, and a policy on a table
    # the query never reads has no business masking a same-named column.
    reach: set[Key] = set()
    for key in catalog.reference_counts(tree):
        reach.add(key)
        reach |= set(catalog.closure(key))
    for name in called_functions(tree):
        reach |= set(catalog.macro_closure(name))
    masks = [p for p in vp.masks if (p["schema_name"].lower(), p["table_name"].lower()) in reach]
    if masks:
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        rewritten, ok, handled = rewrite_query_with_masking(canonical, v.role, conn, policies=masks)
        if ok:
            masked_sql = rewritten
            masking_rewritten = True

    final_tree = sqlglot.parse_one(masked_sql, read="duckdb")
    targets = _write_targets(final_tree)
    if vp.rows:
        final_tree = apply_row_security(final_tree, vp, catalog, targets=targets, skip=registered)
        _fix_qualified_columns(final_tree)
    final_sql = final_tree.sql(dialect="duckdb")
    verify_plan(conn, final_sql, final_tree, vp, catalog, params, skip=registered)
    return GovernedQuery(
        sql=prefix + final_sql,
        viewer=v,
        rewritten=final_sql != body,
        handled_ids=set(handled),
        masks=masks,
        masking_rewritten=masking_rewritten,
    )


def governed_relation_sql(
    schema: str,
    table: str,
    viewer: Viewer | dict | None,
    conn: duckdb.DuckDBPyConnection,
    *,
    project_dir: Path | None = None,
) -> GovernedQuery:
    """``SELECT * FROM schema.table`` governed for ``viewer`` (previews, profiles)."""
    quoted = f'"{schema}"."{table}"'
    return govern_query(f"SELECT * FROM {quoted}", viewer, conn, project_dir=project_dir)


def run_governed(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    viewer: Viewer | dict | None,
    *,
    project_dir: Path | None = None,
    params: Any = None,
    limit: int | None = None,
    serialize: Callable[[Any], Any] | None = None,
) -> dict:
    """Govern, execute and post-mask one read. Returns ``{columns, rows, column_types}``."""
    gq = govern_query(sql, viewer, conn, project_dir=project_dir, params=params)
    result = conn.execute(gq.sql, params) if params else conn.execute(gq.sql)
    columns = [d[0] for d in result.description] if result.description else []
    types = [str(d[1]) for d in result.description] if result.description else []
    raw = result.fetchmany(limit) if limit else result.fetchall()
    ser = serialize or (lambda x: x)
    rows = [[ser(val) for val in row] for row in raw]
    rows = gq.post_mask(columns, rows, conn)
    return {"columns": columns, "rows": rows, "column_types": types}
