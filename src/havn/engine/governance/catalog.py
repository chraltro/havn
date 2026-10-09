"""The warehouse as governance sees it: relations, views, macros, policies.

Two layers:

* :class:`CatalogInfo` is a snapshot of the DuckDB catalog -- which schemas,
  tables and views exist, each view's body, each SQL macro's body -- with
  name resolution that matches DuckDB's (an unqualified name inside a view
  binds in the view's own schema first).

* :class:`GovernanceSnapshot` adds the policies: explicit masking and row
  policies, plus the ones *inherited* through column lineage. A column
  derived from a masked column is masked the same way unless its model
  declassifies it; a model that reads a row-protected table carries the row
  filter if it passes the filter's columns through unchanged, and shows a
  policy's subjects nothing if it does not (unless it declares
  ``@declassify rows``). Classification tags (``@pii``) propagate the same
  way, for reporting.

Snapshots are viewer-independent and cached on a fingerprint of everything
they are computed from; :meth:`GovernanceSnapshot.for_viewer` narrows one to
what applies to a given viewer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import sqlglot
from sqlglot import exp

from .viewer import Viewer

logger = logging.getLogger("havn.governance")

Key = tuple[str, str]  # (schema, name), lowercased, current database only

# Schemas no non-admin may read: password hashes, tokens, other users'
# cached dashboard results and audit trails live there.
INTERNAL_SCHEMA = "_havn"

# Strongest first: when a column derives from several masked columns with
# different methods, it takes the strongest.
_METHOD_STRENGTH = [
    "null", "redact", "hash", "consistent_hash", "credit_card", "partial",
    "email", "phone", "first_initial", "ip_address", "truncate", "range",
    "date_shift", "noise",
]


def _method_rank(method: str) -> int:
    try:
        return _METHOD_STRENGTH.index(method)
    except ValueError:
        return len(_METHOD_STRENGTH)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@dataclass
class ViewInfo:
    key: Key
    sql: str
    query: str | None  # canonical body (tables schema-qualified), or None if unparseable
    columns: list[str]  # CREATE VIEW v (a, b) column list, if any
    refs: Counter = field(default_factory=Counter)  # direct relation refs in the body
    macro_calls: set[str] = field(default_factory=set)


@dataclass
class MacroInfo:
    name: str
    kind: str  # "macro" or "table_macro"
    refs: Counter = field(default_factory=Counter)
    macro_calls: set[str] = field(default_factory=set)
    parsed: bool = True


class CatalogInfo:
    """Schemas, relations, views and macros of the current database."""

    def __init__(self) -> None:
        self.current_db = ""
        self.schemas: set[str] = set()
        self.catalogs: set[str] = set()
        self.tables: set[Key] = set()
        self.views: dict[Key, ViewInfo] = {}
        self.macros: dict[str, MacroInfo] = {}
        self._names: dict[str, set[Key]] = {}

    # -- loading ---------------------------------------------------------

    @classmethod
    def load(cls, conn: duckdb.DuckDBPyConnection) -> "CatalogInfo":
        info = cls()
        info.current_db = str(conn.execute("SELECT current_database()").fetchone()[0]).lower()
        try:
            info.catalogs = {
                str(r[0]).lower() for r in conn.execute(
                    "SELECT database_name FROM duckdb_databases()"
                ).fetchall()
            }
        except duckdb.Error:
            info.catalogs = {info.current_db}
        info.schemas = {
            str(r[0]).lower() for r in conn.execute(
                "SELECT schema_name FROM duckdb_schemas() WHERE database_name = current_database()"
            ).fetchall()
        }
        for schema, name in conn.execute(
            "SELECT schema_name, table_name FROM duckdb_tables() "
            "WHERE database_name = current_database() AND NOT temporary"
        ).fetchall():
            key = (str(schema).lower(), str(name).lower())
            info.tables.add(key)
        view_rows = conn.execute(
            "SELECT schema_name, view_name, sql FROM duckdb_views() "
            "WHERE database_name = current_database() AND NOT internal AND NOT temporary"
        ).fetchall()
        for schema, name, sql in view_rows:
            key = (str(schema).lower(), str(name).lower())
            info.views[key] = ViewInfo(key=key, sql=sql or "", query=None, columns=[])
        for key in list(info.tables) + list(info.views):
            info._names.setdefault(key[1], set()).add(key)
        try:
            macro_rows = conn.execute(
                "SELECT function_name, function_type, macro_definition, schema_name "
                "FROM duckdb_functions() WHERE function_type IN ('macro', 'table_macro') "
                "AND NOT internal"
            ).fetchall()
        except duckdb.Error:
            macro_rows = []
        for name, kind, definition, schema in macro_rows:
            info._load_macro(str(name).lower(), str(kind), definition or "", str(schema or "main").lower())
        for view in info.views.values():
            info._load_view(view)
        return info

    def _load_view(self, view: ViewInfo) -> None:
        try:
            parsed = sqlglot.parse_one(view.sql, read="duckdb")
        except sqlglot.errors.SqlglotError:
            parsed = None
        if not isinstance(parsed, exp.Create) or not isinstance(parsed.expression, exp.Query):
            view.query = None
            return
        target = parsed.this
        if isinstance(target, exp.Schema):
            view.columns = [c.name.lower() for c in target.expressions if isinstance(c, exp.Identifier) or hasattr(c, "name")]
        body = parsed.expression
        search = (view.key[0], "main")
        self.canonicalize(body, search=search)
        view.query = body.sql(dialect="duckdb")
        view.refs = self.reference_counts(body)
        view.macro_calls = called_functions(body)

    def _load_macro(self, name: str, kind: str, definition: str, schema: str) -> None:
        info = MacroInfo(name=name, kind=kind)
        text = definition.strip()
        try:
            parsed = sqlglot.parse_one(text if kind == "table_macro" else f"SELECT {text}", read="duckdb")
        except sqlglot.errors.SqlglotError:
            parsed = None
        if parsed is None:
            info.parsed = False
        else:
            for table in parsed.find_all(exp.Table):
                if not isinstance(table.this, exp.Identifier):
                    continue
                for key in self._conservative_keys(table, schema):
                    info.refs[key] += 1
            info.macro_calls = called_functions(parsed)
        self.macros[name] = info

    def _conservative_keys(self, table: exp.Table, schema: str) -> set[Key]:
        """Every relation an unqualified macro reference could bind to."""
        name = (table.name or "").lower()
        db = (table.db or "").lower()
        if db:
            key = self.resolve(table, search=(schema, "main"))
            return {key} if isinstance(key, tuple) else set()
        return set(self._names.get(name, set())) or {(schema, name)}

    # -- resolution ------------------------------------------------------

    def exists(self, key: Key) -> bool:
        return key in self.tables or key in self.views

    def is_view(self, key: Key) -> bool:
        return key in self.views

    def resolve(self, table: exp.Table, search: tuple[str, ...] = ("main",)) -> Key | str | None:
        """Which relation ``table`` names.

        Returns a ``(schema, name)`` key in the current database, the string
        ``"external"`` for another catalog (an ATTACHed source, ``system``,
        ``temp``), or None for an unqualified name that is not a relation
        here (a CTE the caller missed, a registered DataFrame, a replacement
        scan).
        """
        name = (table.name or "").lower()
        db = (table.db or "").lower()
        cat = (table.catalog or "").lower()
        if not name:
            return None
        if cat:
            if cat != self.current_db:
                return "external"
            return (db or "main", name)
        if db:
            if db in self.schemas:
                return (db, name)
            if db == self.current_db:
                return ("main", name)
            if db in self.catalogs:
                return "external"
            return (db, name)  # an unknown schema: the binder will refuse it
        for schema in search:
            if (schema, name) in self.tables or (schema, name) in self.views:
                return (schema, name)
        return None

    def canonicalize(
        self,
        tree: exp.Expression,
        search: tuple[str, ...] = ("main",),
        skip: frozenset[str] = frozenset(),
    ) -> None:
        """Rewrite every relation reference in ``tree`` as ``schema.name``.

        An alias is added where the original spelling was the only way the
        query's columns could refer to the relation (``FROM customers`` keeps
        ``customers.x`` working as ``FROM main.customers AS customers``).
        Three-part column references to a re-spelled relation are shortened
        to its alias. CTE references and names in ``skip`` (registered
        DataFrames) are left alone.
        """
        respelled: dict[tuple[str, str, str], str] = {}
        for table in list(tree.find_all(exp.Table)):
            if not isinstance(table.this, exp.Identifier):
                continue
            if is_cte_reference(table):
                continue
            if not table.db and not table.catalog and (table.name or "").lower() in skip:
                continue
            key = self.resolve(table, search=search)
            if not isinstance(key, tuple):
                continue
            original = ((table.catalog or "").lower(), (table.db or "").lower(), (table.name or "").lower())
            if original == ("", key[0], key[1]):
                continue
            alias = table.alias
            if not alias:
                alias = table.name
                table.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
            respelled[original] = alias
            table.set("catalog", None)
            table.set("db", exp.to_identifier(key[0]))
            table.set("this", exp.to_identifier(key[1]))
        if not respelled:
            return
        for column in tree.find_all(exp.Column):
            if not column.args.get("db"):
                continue
            original = (
                (column.text("catalog") or "").lower(),
                (column.text("db") or "").lower(),
                (column.table or "").lower(),
            )
            alias = respelled.get(original)
            if alias is not None:
                column.set("catalog", None)
                column.set("db", None)
                column.set("table", exp.to_identifier(alias))

    def reference_counts(self, tree: exp.Expression, search: tuple[str, ...] = ("main",)) -> Counter:
        """Relation keys referenced in ``tree``, counted once per time a scan
        of them can appear in a plan (a CTE body counts once per use)."""
        counts: Counter = Counter()
        cte_uses = _cte_use_counts(tree)
        for table in tree.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier) or is_cte_reference(table):
                continue
            key = self.resolve(table, search=search)
            if isinstance(key, tuple):
                counts[key] += _multiplicity(table, cte_uses)
        return counts

    def closure(self, key: Key, _seen: frozenset = frozenset()) -> Counter:
        """Base tables a relation reads, with multiplicity (views expanded)."""
        if key in _seen:
            return Counter()
        view = self.views.get(key)
        if view is None:
            return Counter({key: 1})
        out: Counter = Counter()
        for ref, n in view.refs.items():
            for base, m in self.closure(ref, _seen | {key}).items():
                out[base] += n * m
        for name in view.macro_calls:
            for base, m in self.macro_closure(name, _seen | {key}).items():
                out[base] += m
        return out

    def macro_closure(self, name: str, _seen: frozenset = frozenset()) -> Counter:
        macro = self.macros.get(name)
        if macro is None or ("macro", name) in _seen:
            return Counter()
        seen = _seen | {("macro", name)}
        out: Counter = Counter()
        for ref, n in macro.refs.items():
            for base, m in self.closure(ref, seen).items():
                out[base] += n * m
        for inner in macro.macro_calls:
            for base, m in self.macro_closure(inner, seen).items():
                out[base] += m
        return out


def called_functions(tree: exp.Expression) -> set[str]:
    """Lowercased names of the non-builtin functions ``tree`` calls."""
    return {(f.name or "").lower() for f in tree.find_all(exp.Anonymous) if f.name}


def is_cte_reference(table: exp.Table) -> bool:
    """Whether an unqualified ``table`` names a CTE visible where it stands."""
    if table.db or table.catalog:
        return False
    name = (table.name or "").lower()
    node: exp.Expression | None = table
    while node is not None:
        with_ = node.args.get("with_") or node.args.get("with") if isinstance(node, exp.Expression) else None
        if isinstance(with_, exp.With):
            for cte in with_.expressions:
                if (cte.alias or "").lower() == name:
                    return True
        node = node.parent
    return False


def _cte_of(table: exp.Table) -> exp.CTE | None:
    name = (table.name or "").lower()
    node = table.parent
    while node is not None:
        with_ = node.args.get("with_") or node.args.get("with")
        if isinstance(with_, exp.With):
            for cte in with_.expressions:
                if (cte.alias or "").lower() == name:
                    return cte
        node = node.parent
    return None


def _cte_use_counts(tree: exp.Expression) -> dict[int, int]:
    """id(CTE) -> how many times it is referenced (at least 1)."""
    uses: dict[int, int] = {}
    for table in tree.find_all(exp.Table):
        if table.db or table.catalog or not isinstance(table.this, exp.Identifier):
            continue
        cte = _cte_of(table)
        if cte is not None and not _inside(table, cte):
            uses[id(cte)] = uses.get(id(cte), 0) + 1
    return uses


def _inside(node: exp.Expression, ancestor: exp.Expression) -> bool:
    cur = node.parent
    while cur is not None:
        if cur is ancestor:
            return True
        cur = cur.parent
    return False


def _multiplicity(table: exp.Table, cte_uses: dict[int, int]) -> int:
    m = 1
    node = table.parent
    while node is not None:
        if isinstance(node, exp.CTE):
            m *= max(1, cte_uses.get(id(node), 1))
        node = node.parent
    return m


# ---------------------------------------------------------------------------
# Policies and inheritance
# ---------------------------------------------------------------------------


def _merge_masks(sources: list[dict], key: Key, column: str, origins: list[list[str]]) -> dict:
    """One inherited masking policy from the policies of a column's sources."""
    strongest = min(sources, key=lambda p: (_method_rank(p["method"]), str(p["id"])))
    exempt = set(sources[0].get("exempted_roles") or [])
    for p in sources[1:]:
        exempt &= set(p.get("exempted_roles") or [])
    return {
        "id": f"inherited:{key[0]}.{key[1]}.{column}",
        "schema_name": key[0],
        "table_name": key[1],
        "column_name": column,
        "method": strongest["method"],
        "method_config": strongest.get("method_config"),
        "condition_column": None,
        "condition_value": None,
        "exempted_roles": sorted(exempt),
        "inherited_from": [f"{t}.{c}" for t, c in origins],
        "source_policy_ids": sorted({str(p.get("source_policy_ids", [p["id"]])[0]) for p in sources}),
    }


def _rename_filter(filter_sql: str, mapping: dict[str, str]) -> str:
    from havn.engine.row_policies import parse_filter

    node = parse_filter(filter_sql).copy()
    outer = None

    def transform(n: exp.Expression) -> exp.Expression:
        if isinstance(n, exp.Column) and not n.table:
            new = mapping.get(n.name.lower())
            if new and n.find_ancestor(exp.Select) is outer:
                return exp.column(new, quoted=True)
        return n

    return node.transform(transform).sql(dialect="duckdb")


@dataclass
class GovernanceSnapshot:
    """Explicit plus inherited policies for the whole warehouse."""

    catalog: CatalogInfo
    masks: list[dict]
    row_policies: list[dict]
    classifications: dict[Key, dict[str, dict]]
    opaque: dict[Key, str]  # relation -> why masking cannot follow it
    lineage: dict[Key, dict]
    declassified: dict[Key, dict[str, str]]
    notes: list[dict]

    # -- per viewer ------------------------------------------------------

    def for_viewer(self, viewer: Viewer) -> "ViewerPolicies":
        from havn.engine.row_policies import policy_applies

        masks = [p for p in self.masks if viewer.role not in (p.get("exempted_roles") or [])]
        rows: dict[Key, list[dict]] = {}
        for p in self.row_policies:
            key = (p["schema_name"].lower(), p["table_name"].lower())
            if self.catalog.is_view(key):
                continue  # enforced on the tables the view reads (inlined)
            if policy_applies(p, viewer):
                rows.setdefault(key, []).append(p)
        masked = {(p["schema_name"].lower(), p["table_name"].lower()) for p in masks}
        masked_base: set[Key] = set()
        for key in masked:
            masked_base |= set(self.catalog.closure(key))
        opaque = {}
        for key, reason in self.opaque.items():
            reach = set(self.catalog.closure(key)) if self.catalog.is_view(key) else set(
                (self.lineage.get(key) or {}).get("_reads_keys", [])
            )
            if reach & masked_base or key in masked:
                opaque[key] = reason
        return ViewerPolicies(
            viewer=viewer,
            snapshot=self,
            masks=masks,
            rows=rows,
            masked=masked,
            opaque=opaque,
            blocked_schemas=set() if viewer.is_admin else {INTERNAL_SCHEMA},
        )


@dataclass
class ViewerPolicies:
    viewer: Viewer
    snapshot: GovernanceSnapshot
    masks: list[dict]
    rows: dict[Key, list[dict]]
    masked: set[Key]
    opaque: dict[Key, str]
    blocked_schemas: set[str]

    @property
    def subject_to_policies(self) -> bool:
        """Whether any masking or row policy applies to this viewer."""
        return bool(self.masks or self.rows)

    @property
    def unrestricted(self) -> bool:
        return not (self.masks or self.rows or self.blocked_schemas)

    def governed_base_tables(self) -> set[Key]:
        """Base tables whose scans must be accounted for in a plan."""
        cat = self.snapshot.catalog
        out: set[Key] = set(self.rows)
        for key in self.masked:
            out |= set(cat.closure(key))
        for schema in self.blocked_schemas:
            out |= {k for k in cat.tables if k[0] == schema}
        return out

    def reaches_rls(self, key: Key) -> bool:
        return any(base in self.rows for base in self.snapshot.catalog.closure(key))


# ---------------------------------------------------------------------------
# Computing a snapshot
# ---------------------------------------------------------------------------


_cache: dict[tuple, tuple[str, GovernanceSnapshot]] = {}
_cache_lock = threading.Lock()


def _fingerprint_part(conn: duckdb.DuckDBPyConnection, sql: str) -> str:
    try:
        row = conn.execute(sql).fetchone()
        return str(row[0]) if row else ""
    except duckdb.Error:
        return "-"


def fingerprint(conn: duckdb.DuckDBPyConnection, project_dir: Path | None) -> str:
    parts = [
        _fingerprint_part(conn, "SELECT current_database()"),
        _fingerprint_part(conn, "SELECT md5(string_agg(CAST(p AS VARCHAR), '|' ORDER BY id)) FROM _havn.masking_policies p"),
        _fingerprint_part(conn, "SELECT md5(string_agg(CAST(p AS VARCHAR), '|' ORDER BY id)) FROM _havn.row_policies p"),
        _fingerprint_part(conn, "SELECT md5(string_agg(model_path || coalesce(content_hash, '') || CAST(built_at AS VARCHAR), '|' ORDER BY model_path)) FROM _havn.model_lineage"),
        _fingerprint_part(conn, "SELECT md5(string_agg(schema_name || '.' || view_name || sql, '|' ORDER BY schema_name, view_name)) FROM duckdb_views() WHERE database_name = current_database() AND NOT internal"),
        _fingerprint_part(conn, "SELECT md5(string_agg(schema_name || '.' || table_name, '|' ORDER BY schema_name, table_name)) FROM duckdb_tables() WHERE database_name = current_database()"),
        _fingerprint_part(conn, "SELECT md5(string_agg(function_name || coalesce(macro_definition, ''), '|' ORDER BY function_name)) FROM duckdb_functions() WHERE function_type IN ('macro', 'table_macro') AND NOT internal"),
    ]
    if project_dir is not None:
        parts.append(_directive_fingerprint(project_dir))
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _sql_files(project_dir: Path) -> list[Path]:
    files: list[Path] = []
    transform = project_dir / "transform"
    if transform.is_dir():
        files.extend(transform.rglob("*.sql"))
    packages = project_dir / "havn_packages"
    if packages.is_dir():
        files.extend(packages.rglob("*.sql"))
    return sorted(files)


def _directive_fingerprint(project_dir: Path) -> str:
    h = hashlib.sha256()
    for f in _sql_files(project_dir):
        try:
            st = f.stat()
        except OSError:
            continue
        h.update(f"{f}:{st.st_mtime_ns}:{st.st_size}\n".encode())
    return h.hexdigest()


def _project_models(project_dir: Path | None) -> dict[str, Any]:
    if project_dir is None:
        return {}
    try:
        from havn.engine.transform import discover_all_models

        return {m.full_name.lower(): m for m in discover_all_models(project_dir)}
    except Exception as e:
        logger.debug("Governance could not discover models: %s", e)
        return {}


def get_snapshot(
    conn: duckdb.DuckDBPyConnection,
    project_dir: Path | None = None,
) -> GovernanceSnapshot:
    """The (cached) governance snapshot for this warehouse and project."""
    fp = fingerprint(conn, project_dir)
    cache_key = (str(project_dir) if project_dir else "", _fingerprint_part(conn, "SELECT current_database()"))
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit is not None and hit[0] == fp:
            return hit[1]
    snapshot = compute_snapshot(conn, project_dir)
    with _cache_lock:
        if len(_cache) > 16:
            _cache.clear()
        _cache[cache_key] = (fp, snapshot)
    return snapshot


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def compute_snapshot(conn: duckdb.DuckDBPyConnection, project_dir: Path | None) -> GovernanceSnapshot:
    from havn.engine.masking import load_policies
    from havn.engine.row_policies import filter_columns, load_row_policies
    from havn.engine.sql_analysis import fetch_column_catalog

    from .lineage import load_model_lineage, trace_columns

    catalog = CatalogInfo.load(conn)
    explicit_masks = load_policies(conn)
    explicit_rows = load_row_policies(conn)
    persisted = load_model_lineage(conn)
    models = _project_models(project_dir)
    column_catalog = fetch_column_catalog(conn)

    # -- what each relation was made from ---------------------------------
    lineage: dict[Key, dict] = {}
    for key, view in catalog.views.items():
        if view.query is None:
            lineage[key] = {"columns": {}, "reads": [], "traced": False, "kind": "view"}
            continue
        record = trace_columns(view.query, column_catalog) or {"columns": {}, "reads": [], "traced": False}
        if view.columns:
            # CREATE VIEW v (a, b): the body's projections are renamed in order.
            renamed: dict[str, dict] = {}
            for new, (_old, entry) in zip(view.columns, record["columns"].items()):
                renamed[new] = entry
            record["columns"] = renamed
        record["kind"] = "view"
        lineage[key] = record
    for key in catalog.tables:
        name = f"{key[0]}.{key[1]}"
        record = persisted.get(name)
        if record is None and name in models:
            model = models[name]
            try:
                tree = sqlglot.parse_one(model.query, read="duckdb")
                catalog.canonicalize(tree, search=("main",))
                record = trace_columns(tree.sql(dialect="duckdb"), column_catalog, list(model.depends_on))
            except Exception:
                record = None
            if record is not None:
                record["reads"] = sorted(set(record.get("reads", [])) | {d.lower() for d in model.depends_on})
        if record is not None:
            record = dict(record)
            record["kind"] = "table"
            lineage[key] = record

    for key, record in lineage.items():
        keys: list[Key] = []
        for ref in record.get("reads", []):
            schema, _, name = str(ref).lower().partition(".")
            if name and catalog.exists((schema, name)):
                keys.append((schema, name))
        record["_reads_keys"] = keys

    declassified: dict[Key, dict[str, str]] = {}
    pii_directives: dict[Key, list[str]] = {}
    for name, model in models.items():
        schema, _, rel = name.partition(".")
        if model.declassified:
            declassified[(schema, rel)] = dict(model.declassified)
        if model.pii:
            pii_directives[(schema, rel)] = list(model.pii)

    # -- topological order --------------------------------------------------
    order: list[Key] = []
    state: dict[Key, int] = {}

    def visit(key: Key) -> None:
        if state.get(key) == 2:
            return
        if state.get(key) == 1:
            return  # a cycle (should not happen): break it
        state[key] = 1
        for up in lineage.get(key, {}).get("_reads_keys", []):
            if up != key:
                visit(up)
        state[key] = 2
        order.append(key)

    for key in sorted(set(catalog.tables) | set(catalog.views)):
        visit(key)

    explicit_mask_at: dict[tuple[str, str, str], dict] = {}
    for p in explicit_masks:
        explicit_mask_at[(p["schema_name"].lower(), p["table_name"].lower(), p["column_name"].lower())] = p
    eff_mask: dict[tuple[str, str, str], dict] = {}  # column -> effective policy
    eff_class: dict[tuple[str, str, str], dict] = {}
    inherited_masks: list[dict] = []
    rows_at: dict[Key, list[dict]] = {}
    for p in explicit_rows:
        rows_at.setdefault((p["schema_name"].lower(), p["table_name"].lower()), []).append(p)
    inherited_rows: list[dict] = []
    opaque: dict[Key, str] = {}
    notes: list[dict] = []

    for (s, t, c), p in explicit_mask_at.items():
        eff_mask[(s, t, c)] = p
        eff_class[(s, t, c)] = {
            "classification": "pii", "source": "policy", "method": p["method"],
            "from": [], "policy_id": p["id"],
        }

    for key in order:
        record = lineage.get(key)
        declass = declassified.get(key, {})
        for col in pii_directives.get(key, []):
            eff_class.setdefault((key[0], key[1], col), {
                "classification": "pii", "source": "directive", "method": None, "from": [],
            })
        if record is None:
            continue
        reads = record.get("_reads_keys", [])
        reads_masked = any(
            (r[0], r[1]) == (s, t) for r in reads for (s, t, _c) in eff_mask
        )
        reads_classified = any(
            (r[0], r[1]) == (s, t) for r in reads for (s, t, _c) in eff_class
        )
        if not record.get("traced", True) and "*" not in declass and (reads_masked or reads_classified):
            if reads_masked:
                opaque[key] = (
                    f"the column lineage of {key[0]}.{key[1]} could not be traced, so the "
                    "masking of the columns it reads cannot follow it"
                )
            notes.append({
                "relation": f"{key[0]}.{key[1]}", "kind": "untraced",
                "message": "column lineage could not be traced; classifications do not follow it",
            })
        for col, entry in (record.get("columns") or {}).items():
            ckey = (key[0], key[1], col)
            if col in declass or "*" in declass:
                origin = [s for s in entry.get("sources", []) if (s[0].partition(".")[0], s[0].partition(".")[2], s[1]) in eff_class]
                if origin:
                    notes.append({
                        "relation": f"{key[0]}.{key[1]}", "column": col, "kind": "declassified",
                        "message": declass.get(col) or declass.get("*") or "",
                        "from": [f"{a}.{b}" for a, b in origin],
                    })
                continue
            src_masks: list[dict] = []
            src_classes: list[dict] = []
            origins: list[list[str]] = []
            for table, scol in entry.get("sources", []):
                skey = (str(table).partition(".")[0], str(table).partition(".")[2], str(scol).lower())
                if skey in eff_mask:
                    src_masks.append(eff_mask[skey])
                    origins.append([table, scol])
                if skey in eff_class:
                    src_classes.append(eff_class[skey])
                    if [table, scol] not in origins:
                        origins.append([table, scol])
            if ckey not in eff_mask and src_masks:
                inherited = _merge_masks(src_masks, key, col, origins)
                eff_mask[ckey] = inherited
                inherited_masks.append(inherited)
            if ckey not in eff_class and src_classes:
                eff_class[ckey] = {
                    "classification": "pii",
                    "source": "inherited",
                    "method": eff_mask[ckey]["method"] if ckey in eff_mask else None,
                    "from": [f"{a}.{b}" for a, b in origins],
                }

        # Row policies carried from what this relation reads.
        if "*rows" in declass:
            if any(rows_at.get(r) for r in reads):
                notes.append({
                    "relation": f"{key[0]}.{key[1]}", "kind": "rows_declassified",
                    "message": declass["*rows"],
                })
            continue
        seen_ids: set[str] = set()
        for up in reads:
            for p in rows_at.get(up, []):
                if not p.get("follow_lineage", True) or p["id"] in seen_ids:
                    continue
                seen_ids.add(p["id"])
                needed = filter_columns(p["filter_sql"])
                mapping: dict[str, str] = {}
                for col, entry in (record.get("columns") or {}).items():
                    through = entry.get("passthrough")
                    if through and str(through[0]).lower() == f"{up[0]}.{up[1]}" and str(through[1]).lower() in needed:
                        mapping.setdefault(str(through[1]).lower(), col)
                base_id = p.get("source_policy_id", p["id"])
                inherited = {
                    **p,
                    "id": f"inherited:{base_id}@{key[0]}.{key[1]}",
                    "source_policy_id": base_id,
                    "schema_name": key[0],
                    "table_name": key[1],
                    "inherited_from": f"{up[0]}.{up[1]}",
                }
                if needed and all(c in mapping for c in needed):
                    try:
                        inherited["filter_sql"] = _rename_filter(p["filter_sql"], mapping)
                        inherited["deny"] = False
                    except Exception:
                        inherited["filter_sql"] = "FALSE"
                        inherited["deny"] = True
                elif not needed:
                    inherited["deny"] = bool(p.get("deny"))
                else:
                    missing = sorted(c for c in needed if c not in mapping)
                    inherited["filter_sql"] = "FALSE"
                    inherited["deny"] = True
                    inherited["deny_reason"] = (
                        f"{key[0]}.{key[1]} reads {up[0]}.{up[1]} but does not pass "
                        f"{', '.join(missing)} through unchanged"
                    )
                rows_at.setdefault(key, []).append(inherited)
                inherited_rows.append(inherited)

    classifications: dict[Key, dict[str, dict]] = {}
    for (s, t, c), info in eff_class.items():
        entry = dict(info)
        if (s, t, c) in eff_mask:
            entry["method"] = eff_mask[(s, t, c)]["method"]
            entry["masked"] = True
        else:
            entry["masked"] = False
        classifications.setdefault((s, t), {})[c] = entry

    return GovernanceSnapshot(
        catalog=catalog,
        masks=list(explicit_masks) + inherited_masks,
        row_policies=list(explicit_rows) + inherited_rows,
        classifications=classifications,
        opaque=opaque,
        lineage=lineage,
        declassified=declassified,
        notes=notes,
    )


def describe(snapshot: GovernanceSnapshot) -> dict:
    """A JSON-friendly summary for the UI and CLI."""
    tables = []
    for (s, t), cols in sorted(snapshot.classifications.items()):
        tables.append({
            "relation": f"{s}.{t}",
            "columns": [{"column": c, **info} for c, info in sorted(cols.items())],
        })
    rows = []
    for p in snapshot.row_policies:
        rows.append({
            "id": p["id"],
            "relation": f"{p['schema_name']}.{p['table_name']}",
            "name": p.get("name", ""),
            "filter_sql": p["filter_sql"],
            "inherited_from": p.get("inherited_from"),
            "deny": bool(p.get("deny")),
            "deny_reason": p.get("deny_reason"),
            "applies_to_roles": p.get("applies_to_roles", []),
            "applies_to_users": p.get("applies_to_users", []),
            "exempted_roles": p.get("exempted_roles", []),
            "exempted_users": p.get("exempted_users", []),
            "enabled": p.get("enabled", True),
        })
    return {
        "classifications": tables,
        "row_policies": rows,
        "opaque": [{"relation": f"{s}.{t}", "reason": r} for (s, t), r in sorted(snapshot.opaque.items())],
        "notes": snapshot.notes,
        "inherited_masks": [p for p in snapshot.masks if str(p["id"]).startswith("inherited:")],
    }


def dumps(value: Any) -> str:
    return json.dumps(value, default=str)
