"""Row-level security policies (``_havn.row_policies``).

A row policy restricts which rows of one table (``schema.table``) a viewer
sees. It is a SQL boolean expression over that table's columns, the
*filter*, and applies to the roles and/or users it names (nobody named means
everybody), minus its exemptions (``admin`` by default).

Several policies on one table that all apply to a viewer are ANDed: each one
narrows what that viewer sees. A viewer no policy applies to is not
restricted by that table's policies.

The filter can refer to the viewer through four functions, replaced by
literals before the query runs:

    havn_user()            the viewer's username
    havn_role()            the viewer's role
    havn_attr('region')    one of the viewer's attributes (NULL if unset)
    havn_attr_list('x')    an attribute as a VARCHAR[] (a scalar becomes [x])

``{user.username}``, ``{user.role}`` and ``{user.<attribute>}`` are accepted
as shorthand (quoted or not), so ``region = '{user.region}'`` works too.

Enforcement lives in :mod:`havn.engine.governance`, which wraps every
reference to a protected table in ``(SELECT * FROM t WHERE <filter>)``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

import duckdb
import sqlglot
from sqlglot import exp

if TYPE_CHECKING:
    from havn.engine.governance.viewer import Viewer

logger = logging.getLogger("havn.row_policies")

# The functions a filter may use to read the viewer. Anything else named
# havn_* is a typo worth refusing rather than leaving to DuckDB's binder.
VIEWER_FUNCTIONS = ("havn_user", "havn_role", "havn_attr", "havn_attr_list")

_PLACEHOLDER_RE = re.compile(r"'?\{user\.([A-Za-z_][A-Za-z0-9_]*)\}'?")


def ensure_row_policy_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Create ``_havn.row_policies`` if it does not exist."""
    from havn.engine.database import _is_ducklake_connection, _strip_pk

    conn.execute("CREATE SCHEMA IF NOT EXISTS _havn")
    conn.execute(_strip_pk("""
        CREATE TABLE IF NOT EXISTS _havn.row_policies (
            id               VARCHAR PRIMARY KEY DEFAULT gen_random_uuid()::VARCHAR,
            name             VARCHAR,
            schema_name      VARCHAR NOT NULL,
            table_name       VARCHAR NOT NULL,
            filter_sql       VARCHAR NOT NULL,
            applies_to_roles JSON,
            applies_to_users JSON,
            exempted_roles   JSON DEFAULT '["admin"]',
            exempted_users   JSON DEFAULT '[]',
            follow_lineage   BOOLEAN DEFAULT TRUE,
            enabled          BOOLEAN DEFAULT TRUE,
            description      VARCHAR,
            created_by       VARCHAR,
            created_at       TIMESTAMP DEFAULT current_timestamp,
            updated_at       TIMESTAMP
        )
    """, _is_ducklake_connection(conn)))


_COLUMNS = (
    "id, name, schema_name, table_name, filter_sql, applies_to_roles, "
    "applies_to_users, exempted_roles, exempted_users, follow_lineage, enabled, "
    "description, created_by, created_at, updated_at"
)


def _json_list(raw: Any, default: list[str]) -> list[str]:
    if raw is None or raw == "":
        return list(default)
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return list(default)
    return [str(x) for x in value] if isinstance(value, list) else list(default)


def _row_to_policy(r: tuple) -> dict:
    return {
        "id": r[0],
        "name": r[1] or "",
        "schema_name": r[2],
        "table_name": r[3],
        "filter_sql": r[4],
        "applies_to_roles": _json_list(r[5], []),
        "applies_to_users": _json_list(r[6], []),
        "exempted_roles": _json_list(r[7], ["admin"]),
        "exempted_users": _json_list(r[8], []),
        "follow_lineage": bool(r[9]) if r[9] is not None else True,
        "enabled": bool(r[10]) if r[10] is not None else True,
        "description": r[11] or "",
        "created_by": r[12],
        "created_at": str(r[13]) if r[13] else None,
        "updated_at": str(r[14]) if r[14] else None,
    }


def load_row_policies(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Every row policy. Safe on a read-only connection (``[]`` if none yet)."""
    try:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM _havn.row_policies ORDER BY schema_name, table_name, created_at"
        ).fetchall()
    except duckdb.Error:
        return []
    return [_row_to_policy(r) for r in rows]


def get_row_policy(conn: duckdb.DuckDBPyConnection, policy_id: str) -> dict | None:
    ensure_row_policy_table(conn)
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM _havn.row_policies WHERE id = ?", [policy_id]
    ).fetchone()
    return _row_to_policy(row) if row else None


def _clean_names(values: list[str] | None, label: str) -> list[str]:
    out: list[str] = []
    for v in values or []:
        if not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,100}", v):
            raise ValueError(f"Invalid {label}: {v!r}")
        if v not in out:
            out.append(v)
    return out


def create_row_policy(
    conn: duckdb.DuckDBPyConnection,
    *,
    schema_name: str,
    table_name: str,
    filter_sql: str,
    name: str | None = None,
    applies_to_roles: list[str] | None = None,
    applies_to_users: list[str] | None = None,
    exempted_roles: list[str] | None = None,
    exempted_users: list[str] | None = None,
    follow_lineage: bool = True,
    enabled: bool = True,
    description: str | None = None,
    created_by: str | None = None,
) -> dict:
    """Validate and insert a row policy, returning it."""
    from havn.engine.utils import validate_identifier

    validate_identifier(schema_name, "schema")
    validate_identifier(table_name, "table")
    filter_sql = validate_filter_sql(filter_sql, conn, schema_name, table_name)
    ensure_row_policy_table(conn)
    row = conn.execute(
        f"""
        INSERT INTO _havn.row_policies
            (name, schema_name, table_name, filter_sql, applies_to_roles,
             applies_to_users, exempted_roles, exempted_users, follow_lineage,
             enabled, description, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING {_COLUMNS}
        """,
        [
            (name or "").strip()[:200],
            schema_name.lower(),
            table_name.lower(),
            filter_sql,
            json.dumps(_clean_names(applies_to_roles, "role")),
            json.dumps(_clean_names(applies_to_users, "user")),
            json.dumps(_clean_names(
                exempted_roles if exempted_roles is not None else ["admin"], "role"
            )),
            json.dumps(_clean_names(exempted_users, "user")),
            bool(follow_lineage),
            bool(enabled),
            (description or "").strip()[:2000],
            created_by,
        ],
    ).fetchone()
    return _row_to_policy(row)


_UPDATABLE = {
    "name", "schema_name", "table_name", "filter_sql", "applies_to_roles",
    "applies_to_users", "exempted_roles", "exempted_users", "follow_lineage",
    "enabled", "description",
}


def update_row_policy(conn: duckdb.DuckDBPyConnection, policy_id: str, **updates) -> dict | None:
    """Update fields of a row policy. Returns the updated policy, or None."""
    from havn.engine.utils import validate_identifier

    existing = get_row_policy(conn, policy_id)
    if existing is None:
        return None
    merged = {**existing, **{k: v for k, v in updates.items() if k in _UPDATABLE}}
    validate_identifier(merged["schema_name"], "schema")
    validate_identifier(merged["table_name"], "table")
    if any(k in updates for k in ("filter_sql", "schema_name", "table_name")):
        merged["filter_sql"] = validate_filter_sql(
            merged["filter_sql"], conn, merged["schema_name"], merged["table_name"]
        )
    sets: list[str] = []
    params: list[Any] = []
    for key in sorted(_UPDATABLE & set(updates)):
        value = merged[key]
        if key in ("applies_to_roles", "exempted_roles"):
            value = json.dumps(_clean_names(value, "role"))
        elif key in ("applies_to_users", "exempted_users"):
            value = json.dumps(_clean_names(value, "user"))
        elif key in ("schema_name", "table_name"):
            value = str(value).lower()
        elif key in ("follow_lineage", "enabled"):
            value = bool(value)
        elif key in ("name", "description"):
            value = (value or "").strip()[:2000]
        sets.append(f"{key} = ?")
        params.append(value)
    if not sets:
        return existing
    sets.append("updated_at = current_timestamp")
    params.append(policy_id)
    conn.execute(f"UPDATE _havn.row_policies SET {', '.join(sets)} WHERE id = ?", params)
    return get_row_policy(conn, policy_id)


def delete_row_policy(conn: duckdb.DuckDBPyConnection, policy_id: str) -> bool:
    ensure_row_policy_table(conn)
    found = conn.execute(
        "SELECT COUNT(*) FROM _havn.row_policies WHERE id = ?", [policy_id]
    ).fetchone()[0]
    if not found:
        return False
    conn.execute("DELETE FROM _havn.row_policies WHERE id = ?", [policy_id])
    return True


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def _expand_placeholders(filter_sql: str) -> str:
    def repl(m: re.Match) -> str:
        key = m.group(1).lower()
        if key == "username":
            return "havn_user()"
        if key == "role":
            return "havn_role()"
        return f"havn_attr('{key}')"

    return _PLACEHOLDER_RE.sub(repl, filter_sql)


def parse_filter(filter_sql: str) -> exp.Expression:
    """Parse a filter into one boolean expression, or raise ValueError.

    Parsed as the WHERE of a one-table query so that text which would close
    the expression early (``1=1) UNION SELECT ...``) is a parse error here
    rather than SQL spliced into someone's query later. Placeholders are
    expanded to the viewer functions first.
    """
    text = _expand_placeholders((filter_sql or "").strip())
    if not text:
        raise ValueError("A row policy needs a filter expression")
    if len(text) > 10_000:
        raise ValueError("Row policy filter is too long")
    try:
        parsed = sqlglot.parse(f"SELECT 1 FROM _t WHERE {text}", read="duckdb")
    except sqlglot.errors.SqlglotError as e:
        raise ValueError(f"Row policy filter does not parse: {e}") from None
    if len(parsed) != 1 or not isinstance(parsed[0], exp.Select):
        raise ValueError("Row policy filter must be a single boolean expression")
    select = parsed[0]
    where = select.args.get("where")
    others = {k for k, v in select.args.items() if v and k not in ("expressions", "from", "from_", "where")}
    if where is None or others:
        raise ValueError("Row policy filter must be a single boolean expression")
    for func in where.find_all(exp.Anonymous):
        fname = (func.name or "").lower()
        if fname.startswith("havn_") and fname not in VIEWER_FUNCTIONS:
            raise ValueError(
                f"Unknown function {func.name}() in row policy filter; "
                f"use one of {', '.join(f + '()' for f in VIEWER_FUNCTIONS)}"
            )
    for agg in (exp.AggFunc, exp.Window):
        if where.find(agg):
            raise ValueError("Row policy filters cannot use aggregate or window functions")
    return where.this


def filter_columns(filter_sql: str) -> set[str]:
    """Lowercased names of the table columns a filter reads (outside subqueries)."""
    try:
        node = parse_filter(filter_sql)
    except ValueError:
        return set()
    outer = node.find_ancestor(exp.Select)  # the parse wrapper
    cols: set[str] = set()
    for column in node.find_all(exp.Column):
        if column.find_ancestor(exp.Select) is not outer:
            continue  # inside a subquery: another table's column
        cols.add(column.name.lower())
    return cols


def validate_filter_sql(
    filter_sql: str,
    conn: duckdb.DuckDBPyConnection | None = None,
    schema_name: str | None = None,
    table_name: str | None = None,
) -> str:
    """Check a filter parses, is read-only and binds against its table.

    Returns the filter text as given (stripped). Binding uses a sample viewer
    so the viewer functions resolve; a table that does not exist yet is not
    an error (the policy can be written ahead of the first build).
    """
    from havn.engine.sql_safety import ReadOnlyQueryError, validate_read_only_query

    text = (filter_sql or "").strip()
    node = parse_filter(text)
    try:
        validate_read_only_query(f"SELECT 1 WHERE {node.sql(dialect='duckdb')}")
    except ReadOnlyQueryError as e:
        raise ValueError(f"Row policy filter is not allowed: {e}") from None
    if conn is not None and schema_name and table_name:
        from havn.engine.governance.viewer import Viewer

        probe = Viewer(username="__probe__", role="viewer", attributes={})
        rendered = render_filter(text, probe).sql(dialect="duckdb")
        qualified = f'"{schema_name}"."{table_name}"'
        try:
            exists = conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_catalog = current_database() "
                "AND lower(table_schema) = lower(?) AND lower(table_name) = lower(?)",
                [schema_name, table_name],
            ).fetchone()
        except duckdb.Error:
            exists = None
        if exists:
            try:
                conn.execute(f"EXPLAIN SELECT * FROM {qualified} WHERE {rendered}").fetchall()
            except duckdb.Error as e:
                raise ValueError(f"Row policy filter does not bind against {schema_name}.{table_name}: {e}") from None
    return text


def _literal(value: Any) -> exp.Expression:
    if value is None:
        return exp.Null()
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)):
        return exp.Literal.number(value)
    return exp.Literal.string(str(value))


def _list_literal(value: Any) -> exp.Expression:
    items = value if isinstance(value, list) else ([] if value is None else [value])
    array = exp.Array(expressions=[exp.Literal.string(str(v)) for v in items])
    return exp.Cast(this=array, to=exp.DataType.build("VARCHAR[]", dialect="duckdb"))


def _attr_key(func: exp.Expression) -> str | None:
    args = list(func.expressions) if isinstance(func, exp.Anonymous) else []
    if len(args) != 1 or not isinstance(args[0], exp.Literal) or not args[0].is_string:
        return None
    return args[0].this.lower()


def render_filter(filter_sql: str, viewer: "Viewer") -> exp.Expression:
    """The filter as an expression with the viewer functions replaced by literals.

    A malformed ``havn_attr(...)`` call renders as FALSE: a policy that cannot
    say which rows the viewer may see shows none.
    """
    node = parse_filter(filter_sql).copy()
    attrs = {str(k).lower(): v for k, v in (viewer.attributes or {}).items()}

    def transform(n: exp.Expression) -> exp.Expression:
        if not isinstance(n, exp.Anonymous):
            return n
        fname = (n.name or "").lower()
        if fname == "havn_user":
            return exp.Literal.string(viewer.username)
        if fname == "havn_role":
            return exp.Literal.string(viewer.role)
        if fname in ("havn_attr", "havn_attr_list"):
            key = _attr_key(n)
            if key is None:
                return exp.false()
            value = attrs.get(key)
            if fname == "havn_attr_list":
                return _list_literal(value)
            if isinstance(value, list):
                # A list attribute read as a scalar: no single value to compare.
                return exp.Null()
            return _literal(value)
        return n

    return node.transform(transform)


def policy_applies(policy: dict, viewer: "Viewer") -> bool:
    """Whether ``policy`` restricts ``viewer``."""
    if not policy.get("enabled", True):
        return False
    if viewer.role in policy.get("exempted_roles", ["admin"]):
        return False
    if viewer.username in policy.get("exempted_users", []):
        return False
    roles = policy.get("applies_to_roles") or []
    users = policy.get("applies_to_users") or []
    if not roles and not users:
        return True
    return viewer.role in roles or viewer.username in users
