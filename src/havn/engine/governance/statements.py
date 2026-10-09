"""Governed execution of whole SQL statements, writes included.

Governed Python (a script or notebook run by a user that masking or row
policies apply to) does not get a DuckDB connection. Its ``db`` sends SQL to
the server, which runs it here, on a cursor of its own, for that user:

* reads (SELECT, EXPLAIN, the SELECT inside CREATE TABLE AS / INSERT ... SELECT
  / COPY ... TO) are governed with :func:`govern_query`, so what a script
  writes into a new table is what its author may read;
* UPDATE / DELETE on a row-protected table only touch the rows the user
  sees, and may not read masked columns;
* what could step around governance is refused: SQL macros and functions,
  secrets, PRAGMA/CALL, USE and search-path changes, prepared statements,
  SET VARIABLE, EXPORT/IMPORT DATABASE, attaching DuckDB files, loading an
  extension from a path, and any file path that points at the warehouse,
  its WAL, Pipeline Rewind snapshots or backups.

The SQL statement API (``/v1/sql``) uses the same session for governed callers.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import sqlglot
from sqlglot import exp

from .rewrite import GovernanceError, govern_query, verify_plan, viewer_policies
from .viewer import Viewer

logger = logging.getLogger("havn.governance")

# Settings a governed session may change on its own cursor: credentials and
# behaviour of remote readers. Anything that changes name resolution
# (search_path, schema), resource limits or what DuckDB may touch on disk is
# not on the list.
_SETTABLE_PREFIXES = (
    "s3_", "http_", "azure_", "gcs_", "hf_", "pg_", "mysql_", "sqlite_",
    "timezone", "calendar", "binary_as_string", "preserve_insertion_order",
    "default_order", "default_null_order", "errors_as_json", "progress_bar",
    "enable_progress_bar", "arrow_large_buffer_size", "ca_cert_file",
)
_ATTACH_TYPES = frozenset({"postgres", "postgres_scanner", "mysql", "mysql_scanner", "sqlite", "sqlite_scanner"})
_INTERNAL_WRITABLE = frozenset({"cdc_state"})  # connector watermarks
_PROTECTED_SUFFIXES = (".duckdb", ".duckdb.wal", ".ddb", ".wal")
_PROTECTED_SEGMENTS = (".havn", "_backups")
_BARE_EXTENSION_RE = re.compile(
    r"^\s*(?:force\s+)?(install|load)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:from\s+(core|community|core_nightly))?\s*;?\s*$",
    re.IGNORECASE,
)
_SET_RE = re.compile(r"^\s*(?:set|reset)\s+(?:(?:session|local|global)\s+)?([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


@dataclass
class StatementResult:
    columns: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    table: Any = None  # pyarrow.Table or None
    rowcount: int = -1


def protected_paths(project_dir: Path | None, warehouse: Path | None) -> list[Path]:
    """Files and directories governed sessions may never name."""
    out: list[Path] = []
    if warehouse is not None:
        w = Path(warehouse)
        out += [w, Path(str(w) + ".wal")]
    if project_dir is not None:
        out += [Path(project_dir) / ".havn", Path(project_dir) / "_backups"]
    return [p.resolve() if p.exists() else p.absolute() for p in out]


def _string_literals(sql: str) -> list[str]:
    """Every string constant and double-quoted identifier in ``sql``."""
    out: list[str] = []
    try:
        tokens = duckdb.tokenize(sql)
    except Exception:
        return re.findall(r"'((?:[^']|'')*)'", sql) + re.findall(r'"((?:[^"]|"")*)"', sql)
    for k, (offset, kind) in enumerate(tokens):
        end = tokens[k + 1][0] if k + 1 < len(tokens) else len(sql)
        text = sql[offset:end].strip()
        if kind == duckdb.token_type.string_const:
            out.append(text[1:-1].replace("''", "'") if text.startswith("'") else text.strip("'$"))
        elif text.startswith('"'):
            out.append(text.strip('"'))
    return out


def check_paths(sql: str, protected: list[Path], cwd: Path | None = None) -> None:
    """Refuse a statement that names the warehouse, its WAL, snapshots or backups."""
    import glob as _glob

    base = Path(cwd or os.getcwd())
    prot = [str(p).replace("\\", "/").lower() for p in protected]
    for literal in _string_literals(sql):
        text = literal.strip()
        if not text or "://" in text:
            continue
        norm = text.replace("\\", "/").lower()
        if any(norm.endswith(s) or (s + "/") in norm for s in _PROTECTED_SUFFIXES):
            raise GovernanceError("Reading or attaching DuckDB database files is not available to you.")
        if any(f"/{seg}/" in f"/{norm}/" or norm.startswith(seg) for seg in _PROTECTED_SEGMENTS):
            raise GovernanceError("Pipeline Rewind snapshots and backups are not available to you.")
        candidates = [text]
        if any(ch in text for ch in "*?["):
            try:
                pattern = text if os.path.isabs(text) else str(base / text)
                candidates += _glob.glob(pattern, recursive=True)[:5000]
            except Exception:
                pass
        for cand in candidates:
            try:
                p = Path(cand)
                p = p if p.is_absolute() else base / p
                resolved = str(p.resolve()).replace("\\", "/").lower()
            except (OSError, ValueError):
                continue
            for root in prot:
                if resolved == root or resolved.startswith(root + "/"):
                    raise GovernanceError("That path is part of the warehouse and is not available to you.")


_parser_local = threading.local()


def _parser() -> duckdb.DuckDBPyConnection:
    conn = getattr(_parser_local, "conn", None)
    if conn is None:
        conn = duckdb.connect(":memory:")
        _parser_local.conn = conn
    return conn


def _targets_internal(tree: exp.Expression) -> bool:
    for table in tree.find_all(exp.Table):
        if (table.db or "").lower() == "_havn" and (table.name or "").lower() not in _INTERNAL_WRITABLE:
            return True
        if isinstance(table.this, exp.Identifier) and (table.name or "").lower() == "_havn" and not table.db:
            return True
    return False


class GovernedSession:
    """One governed user's statements, on a cursor of their own."""

    def __init__(
        self,
        cursor: duckdb.DuckDBPyConnection,
        viewer: Viewer,
        *,
        project_dir: Path | None = None,
        warehouse: Path | None = None,
        cwd: Path | None = None,
    ) -> None:
        self.cursor = cursor
        self.viewer = viewer
        self.project_dir = project_dir
        self.cwd = cwd
        self.protected = protected_paths(project_dir, warehouse)
        self.registered: dict[str, Any] = {}
        self.attached: set[str] = set()
        self.requests = 0
        self.lock = threading.RLock()
        try:
            # A replacement scan finds Python objects by name in the calling
            # frame -- here, the server's. Governed SQL reads registered
            # DataFrames only.
            self.cursor.execute("SET python_enable_replacements = false")
        except duckdb.Error:
            pass

    # -- registration ----------------------------------------------------

    def register(self, name: str, table: Any) -> None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name or ""):
            raise GovernanceError(f"Invalid name for a registered DataFrame: {name!r}")
        with self.lock:
            self.cursor.register(name, table)
            self.registered[name.lower()] = table

    def unregister(self, name: str) -> None:
        with self.lock:
            if name.lower() in self.registered:
                self.cursor.unregister(name)
                self.registered.pop(name.lower(), None)

    def interrupt(self) -> None:
        try:
            self.cursor.interrupt()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.cursor.close()
        except Exception:
            pass

    # -- execution -------------------------------------------------------

    def execute(self, sql: str, params: Any = None, frames: dict[str, Any] | None = None) -> StatementResult:
        """Run ``sql`` (one or more statements). Returns the last statement's result."""
        with self.lock:
            self.requests += 1
            temp: list[str] = []
            try:
                for name, table in (frames or {}).items():
                    if name.lower() in self.registered:
                        continue
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name):
                        continue
                    self.cursor.register(name, table)
                    temp.append(name)
                statements = _parser().extract_statements(sql)  # a syntax error raises here
                if not statements:
                    return StatementResult()
                if params and len(statements) > 1:
                    raise GovernanceError("Parameters can only be bound to a single statement.")
                result = StatementResult()
                names = frozenset(self.registered) | {n.lower() for n in temp}
                for stmt in statements:
                    result = self._one(stmt, params, names)
                return result
            finally:
                for name in temp:
                    try:
                        self.cursor.unregister(name)
                    except Exception:
                        pass

    def _one(self, stmt, params: Any, registered: frozenset[str]) -> StatementResult:
        st = duckdb.StatementType
        text = stmt.query
        kind = stmt.type
        if not text.strip():
            raise GovernanceError(
                "This statement (a dynamic PIVOT) cannot be governed. List the pivot values with IN (...)."
            )
        check_paths(text, self.protected, self.cwd)
        if kind in (st.SELECT, st.EXPLAIN):
            return self._read(text, params, registered)
        if kind == st.TRANSACTION:
            return self._run(text, params)
        if kind == st.SET:
            m = _SET_RE.match(text)
            if text.strip().lower().startswith("use") or not m:
                raise GovernanceError("USE and SET VARIABLE are not available in governed sessions.")
            name = m.group(1).lower()
            if name == "variable" or not name.startswith(_SETTABLE_PREFIXES):
                raise GovernanceError(f"Setting {name} is not available in governed sessions.")
            return self._run(text, None)
        if kind == st.LOAD:
            if not _BARE_EXTENSION_RE.match(text):
                raise GovernanceError("Only named extensions can be installed or loaded (no paths or URLs).")
            return self._run(text, None)
        if kind == st.ATTACH:
            return self._attach(text)
        if kind == st.DETACH:
            m = re.match(r"^\s*detach\s+(?:database\s+)?(?:if\s+exists\s+)?\"?([A-Za-z_][A-Za-z0-9_]*)\"?", text, re.IGNORECASE)
            if not m or m.group(1).lower() not in self.attached:
                raise GovernanceError("You can only detach databases this session attached.")
            self.attached.discard(m.group(1).lower())
            return self._run(text, None)
        if kind in (st.INSERT, st.UPDATE, st.DELETE, st.CREATE, st.COPY, st.DROP, st.ALTER, st.MERGE_INTO):
            return self._write(text, params, registered)
        raise GovernanceError(f"{kind.name} statements are not available in governed sessions.")

    def _run(self, sql: str, params: Any) -> StatementResult:
        cur = self.cursor
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        return self._collect(None)

    def _collect(self, gq) -> StatementResult:
        cur = self.cursor
        desc = cur.description
        if not desc:
            return StatementResult()
        columns = [d[0] for d in desc]
        types = [str(d[1]) for d in desc]
        table = cur.fetch_arrow_table() if hasattr(cur, "fetch_arrow_table") else cur.arrow()
        if gq is not None and gq.needs_post_mask and table.num_rows:
            table = _post_mask_arrow(gq, table, self.cursor)
        return StatementResult(columns=columns, types=types, table=table, rowcount=table.num_rows)

    def _read(self, sql: str, params: Any, registered: frozenset[str]) -> StatementResult:
        gq = govern_query(
            sql, self.viewer, self.cursor, project_dir=self.project_dir,
            params=params, registered=registered,
        )
        if params:
            self.cursor.execute(gq.sql, params)
        else:
            self.cursor.execute(gq.sql)
        return self._collect(gq)

    def _attach(self, sql: str) -> StatementResult:
        m = re.search(r"\(\s*(?:[^()]*,\s*)?type\s*[= ]?\s*'?([A-Za-z_]+)'?", sql, re.IGNORECASE)
        kind = m.group(1).lower() if m else ""
        if kind not in _ATTACH_TYPES:
            raise GovernanceError(
                "Governed sessions can attach postgres, mysql and sqlite sources only "
                "(add TYPE postgres / mysql / sqlite)."
            )
        alias = re.search(r"\bas\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?", sql, re.IGNORECASE)
        result = self._run(sql, None)
        if alias:
            self.attached.add(alias.group(1).lower())
        return result

    def _write(self, sql: str, params: Any, registered: frozenset[str]) -> StatementResult:
        try:
            tree = sqlglot.parse_one(sql, read="duckdb")
        except sqlglot.errors.SqlglotError:
            tree = None
        if tree is None or isinstance(tree, exp.Command):
            raise GovernanceError("This statement could not be analysed for governance.")
        if isinstance(tree, exp.Create):
            kind = (tree.args.get("kind") or "").upper()
            if kind in ("FUNCTION", "MACRO", "TABLE MACRO", "SECRET", "PERSISTENT SECRET") or "MACRO" in kind:
                raise GovernanceError("Creating macros, functions and secrets is not available in governed sessions.")
        if _targets_internal(tree) and not self.viewer.is_admin:
            raise GovernanceError("The _havn schema holds havn's own metadata and is admin-only.")
        if isinstance(tree, exp.Create) and (tree.args.get("kind") or "").upper() == "VIEW":
            # A view's body is stored, not run; every read of it is governed.
            vp = viewer_policies(self.cursor, self.viewer, self.project_dir)
            from .rewrite import _check_references

            catalog = vp.snapshot.catalog
            catalog.canonicalize(tree.expression, skip=registered) if tree.expression is not None else None
            if tree.expression is not None:
                _check_references(tree.expression, vp, catalog, set())
            return self._run(sql, params)

        query = None
        if isinstance(tree, (exp.Create, exp.Insert)) and isinstance(tree.expression, exp.Query):
            query = tree.expression
        elif isinstance(tree, exp.Copy) and not tree.args.get("kind"):
            src = tree.this
            if isinstance(src, exp.Table):
                query = exp.select("*").from_(src.copy())
                tree.set("this", exp.Subquery(this=query))
                query = tree.this.this
            elif isinstance(src, exp.Subquery):
                query = src.this

        if query is None:
            # UPDATE / DELETE / MERGE / DDL without a query: govern in place.
            gq = govern_query(
                sql, self.viewer, self.cursor, project_dir=self.project_dir,
                params=params, registered=registered, allow_writes=True,
            )
            return self._run(gq.sql, params)

        gq = govern_query(
            query.sql(dialect="duckdb"), self.viewer, self.cursor,
            project_dir=self.project_dir, params=params, registered=registered,
        )
        if gq.needs_post_mask:
            # Some masking happens after the query (by result column name); a
            # write never sees a result, so stage the governed rows first.
            if params:
                self.cursor.execute(gq.sql, params)
            else:
                self.cursor.execute(gq.sql)
            staged = self._collect(gq).table
            name = f"__havn_governed_{uuid.uuid4().hex[:12]}"
            self.cursor.register(name, staged)
            try:
                replacement = sqlglot.parse_one(f"SELECT * FROM {name}", read="duckdb")
                _swap_query(tree, query, replacement)
                final = tree.sql(dialect="duckdb")
                return self._run(final, None)
            finally:
                try:
                    self.cursor.unregister(name)
                except Exception:
                    pass
        replacement = sqlglot.parse_one(gq.sql, read="duckdb")
        _swap_query(tree, query, replacement)
        final = tree.sql(dialect="duckdb")
        vp = viewer_policies(self.cursor, self.viewer, self.project_dir)
        final_tree = sqlglot.parse_one(final, read="duckdb")
        verify_plan(self.cursor, final, final_tree, vp, vp.snapshot.catalog, params, skip=registered)
        return self._run(final, params)


def _swap_query(tree: exp.Expression, old: exp.Expression, new: exp.Expression) -> None:
    if isinstance(tree, exp.Copy):
        tree.set("this", exp.Subquery(this=new))
    else:
        tree.set("expression", new)


def _post_mask_arrow(gq, table, conn):
    """Apply the by-name post-query masking pass to an Arrow table."""
    import pyarrow as pa

    columns = list(table.column_names)
    rows = [list(r.values()) for r in table.to_pylist()]
    rows = gq.post_mask(columns, rows, conn)
    data = {name: [row[i] for row in rows] for i, name in enumerate(columns)}
    arrays = []
    for i, name in enumerate(columns):
        values = data[name]
        try:
            arrays.append(pa.array(values, type=table.schema.field(i).type))
        except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError, ValueError):
            arrays.append(pa.array([None if v is None else str(v) for v in values], type=pa.string()))
    return pa.Table.from_arrays(arrays, names=columns)


class GovernedCursor:
    """A DB-API-ish face over a GovernedSession for engine code that wants a conn.

    ``execute_sql_cell`` and ``execute_ingest_cell`` call ``conn.execute`` and
    read ``description`` / ``fetchmany`` / ``fetchone``; giving them this runs
    every statement governed, server-side, without a child process.
    """

    def __init__(self, session: GovernedSession) -> None:
        self._session = session
        self._result: StatementResult | None = None
        self._rows: list[tuple] | None = None
        self._pos = 0

    def execute(self, sql: str, params: Any = None):
        self._result = self._session.execute(sql, params)
        self._rows = None
        self._pos = 0
        return self

    def _all(self) -> list[tuple]:
        if self._rows is None:
            table = self._result.table if self._result else None
            self._rows = [tuple(r.values()) for r in table.to_pylist()] if table is not None else []
        return self._rows

    @property
    def description(self):
        if not self._result or not self._result.columns:
            return None
        return [(c, t, None, None, None, None, None) for c, t in zip(self._result.columns, self._result.types)]

    def fetchall(self):
        rows = self._all()[self._pos:]
        self._pos = len(self._all())
        return rows

    def fetchone(self):
        rows = self._all()
        if self._pos >= len(rows):
            return None
        self._pos += 1
        return rows[self._pos - 1]

    def fetchmany(self, size: int = 1):
        rows = self._all()[self._pos:self._pos + size]
        self._pos += len(rows)
        return rows

    def fetchdf(self):
        table = self._result.table if self._result else None
        return table.to_pandas() if table is not None else None

    df = fetchdf

    def interrupt(self) -> None:
        self._session.interrupt()

    def cursor(self):
        return self

    def close(self) -> None:
        pass
