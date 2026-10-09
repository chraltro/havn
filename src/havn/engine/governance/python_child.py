"""Bootstrap of a governed Python process. Run by path; never imports havn.

A script or notebook run by a user that masking or row policies apply to runs
here, in a process of its own, instead of in the server. Its ``db`` is a
proxy: every statement goes to the server over an authenticated local
socket, and the server runs it governed for that user (masked, row-filtered,
refused where governance cannot follow). Nothing in this process holds a
DuckDB connection, so there is nothing to reach through.

Defence in depth inside the process (an audit hook, PEP 578):

* opening the warehouse file, its WAL, ``.havn/`` (Pipeline Rewind
  snapshots) or ``_backups/`` through Python's file APIs raises;
* importing ``duckdb`` (which could open those files natively) raises;
* starting processes and calling into native code through ``ctypes`` raise.

What it cannot stop is native code in an already-imported extension reading
a path directly (``pyarrow.parquet.read_table`` on a snapshot file, for
instance). On Windows the server holds the live warehouse and its WAL
locked, so those cannot be opened by any other process; the bootstrap checks
that before running user code and reports the result to the server.

Wire format: every message is one JSON frame, optionally followed by binary
frames (Arrow IPC streams) the header announces. Nothing is pickled: the
server must never unpickle what this process sends.
"""

from __future__ import annotations

import ast
import builtins
import io
import json
import os
import sys
import traceback

# ---------------------------------------------------------------------------
# Wire
# ---------------------------------------------------------------------------


class _Channel:
    def __init__(self, conn) -> None:
        self.conn = conn

    def send(self, header: dict, blobs: list[bytes] | None = None) -> None:
        blobs = blobs or []
        header = dict(header, blobs=len(blobs))
        self.conn.send_bytes(json.dumps(header, default=str).encode("utf-8"))
        for blob in blobs:
            self.conn.send_bytes(blob)

    def recv(self) -> tuple[dict, list[bytes]]:
        header = json.loads(self.conn.recv_bytes().decode("utf-8"))
        blobs = [self.conn.recv_bytes() for _ in range(int(header.get("blobs", 0)))]
        return header, blobs

    def request(self, header: dict, blobs: list[bytes] | None = None) -> tuple[dict, list[bytes]]:
        self.send(header, blobs)
        reply, data = self.recv()
        if reply.get("op") == "error":
            raise GovernedError(reply.get("message", "governed request failed"))
        return reply, data


class GovernedError(Exception):
    """A statement the server refused or that failed there."""


# ---------------------------------------------------------------------------
# Arrow helpers
# ---------------------------------------------------------------------------


def _to_arrow(obj):
    import pyarrow as pa

    if isinstance(obj, pa.Table):
        return obj
    if isinstance(obj, pa.RecordBatch):
        return pa.Table.from_batches([obj])
    if hasattr(obj, "read_all") and isinstance(obj, pa.RecordBatchReader):
        return obj.read_all()
    mod = type(obj).__module__.split(".")[0]
    if mod == "pandas":
        return pa.Table.from_pandas(obj, preserve_index=False)
    if mod == "polars":
        return obj.to_arrow()
    if hasattr(obj, "__arrow_c_stream__"):
        return pa.table(obj)
    return None


def _ipc(table) -> bytes:
    import pyarrow as pa

    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _from_ipc(blob: bytes):
    import pyarrow as pa

    return pa.ipc.open_stream(pa.py_buffer(blob)).read_all()


def _frame_candidates(sql: str) -> set[str]:
    import re

    names = set()
    for m in re.finditer(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)\b(?!\s*[.(])", sql, re.IGNORECASE):
        names.add(m.group(1))
    return names


# ---------------------------------------------------------------------------
# The db proxy
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, columns, types, table, rowcount=-1) -> None:
        self.columns = columns
        self.types = types
        self.table = table
        self.rowcount = rowcount
        self._rows = None
        self._pos = 0

    def rows(self):
        if self._rows is None:
            if self.table is None:
                self._rows = []
            else:
                cols = [c.to_pylist() for c in self.table.columns]
                self._rows = list(zip(*cols)) if cols else []
        return self._rows


class GovernedRelation:
    """What ``db.sql(...)`` returns: the SQL, run when its results are asked for."""

    def __init__(self, db: "GovernedDB", sql: str, params=None) -> None:
        self._db = db
        self._sql = sql
        self._params = params
        self._result: _Result | None = None

    def _run(self) -> _Result:
        if self._result is None:
            self._result = self._db._execute(self._sql, self._params, depth=3)
        return self._result

    def fetchall(self):
        return list(self._run().rows())

    def fetchone(self):
        rows = self._run().rows()
        return rows[0] if rows else None

    def fetchmany(self, size: int = 1):
        return list(self._run().rows()[:size])

    def df(self):
        t = self._run().table
        return t.to_pandas() if t is not None else None

    fetchdf = df
    to_df = df

    def arrow(self):
        return self._run().table

    fetch_arrow_table = arrow
    to_arrow_table = arrow

    def pl(self):
        import polars as pl

        return pl.from_arrow(self._run().table)

    def fetchnumpy(self):
        t = self._run().table
        return {name: col.to_numpy() for name, col in zip(t.column_names, t.columns)} if t is not None else {}

    @property
    def columns(self):
        return list(self._run().columns)

    @property
    def types(self):
        return list(self._run().types)

    @property
    def description(self):
        r = self._run()
        return [(c, t, None, None, None, None, None) for c, t in zip(r.columns, r.types)]

    @property
    def shape(self):
        r = self._run()
        return (len(r.rows()), len(r.columns))

    def __len__(self):
        return len(self._run().rows())

    def show(self, *args, **kwargs):
        t = self._run().table
        print(t.to_pandas().to_string() if t is not None else "")

    def create(self, table_name: str) -> None:
        self._db._execute(f"CREATE TABLE {table_name} AS {self._sql}", self._params, depth=3)

    def create_view(self, view_name: str, replace: bool = True) -> None:
        verb = "CREATE OR REPLACE VIEW" if replace else "CREATE VIEW"
        self._db._execute(f"{verb} {view_name} AS {self._sql}", self._params, depth=3)

    def insert_into(self, table_name: str) -> None:
        self._db._execute(f"INSERT INTO {table_name} {self._sql}", self._params, depth=3)

    def to_table(self, table_name: str) -> None:
        self.create(table_name)

    def __repr__(self) -> str:
        return f"<GovernedRelation {self._sql[:60]!r}>"


class GovernedDB:
    """The ``db`` a governed script sees. Every call goes to the server."""

    __slots__ = ("_channel", "_last")

    def __init__(self, channel: _Channel) -> None:
        object.__setattr__(self, "_channel", channel)
        object.__setattr__(self, "_last", None)

    # -- plumbing ----------------------------------------------------------

    def _frames(self, sql: str, depth: int) -> tuple[list[str], list[bytes]]:
        """DataFrames the SQL names, from the calling code's scope.

        DuckDB finds ``SELECT * FROM df`` by looking for ``df`` in the
        caller's frame (a replacement scan). The server cannot see this
        process's frames, so the proxy looks them up here and ships them
        along as Arrow.
        """
        names = _frame_candidates(sql)
        if not names:
            return [], []
        frame = sys._getframe(1)
        while frame is not None and frame.f_code.co_filename == __file__:
            frame = frame.f_back
        if frame is None:
            return [], []
        found_names: list[str] = []
        blobs: list[bytes] = []
        for name in sorted(names):
            obj = frame.f_locals.get(name, frame.f_globals.get(name))
            if obj is None or isinstance(obj, (str, int, float, bytes)):
                continue
            try:
                table = _to_arrow(obj)
            except Exception:
                table = None
            if table is None:
                continue
            found_names.append(name)
            blobs.append(_ipc(table))
        return found_names, blobs

    def _execute(self, sql, params=None, depth: int = 2) -> _Result:
        if not isinstance(sql, str):
            raise TypeError("SQL must be a string")
        names, blobs = self._frames(sql, depth + 1)
        if params is not None and not isinstance(params, (list, tuple, dict)):
            raise TypeError("parameters must be a list, tuple or dict")
        reply, data = self._channel.request(
            {"op": "execute", "sql": sql, "params": list(params) if isinstance(params, tuple) else params,
             "frames": names},
            blobs,
        )
        table = _from_ipc(data[0]) if data else None
        return _Result(reply.get("columns", []), reply.get("types", []), table, reply.get("rowcount", -1))

    # -- DuckDB connection API ---------------------------------------------

    def execute(self, query, parameters=None):
        object.__setattr__(self, "_last", self._execute(query, parameters))
        return self

    def executemany(self, query, parameters=None):
        for params in parameters or []:
            object.__setattr__(self, "_last", self._execute(query, params))
        return self

    def sql(self, query, params=None, **_kwargs):
        head = query.lstrip().split(None, 1)[0].lower() if query.strip() else ""
        if head in ("select", "with", "from", "values", "table", "pivot", "unpivot",
                    "summarize", "describe", "show", "explain", "("):
            return GovernedRelation(self, query, params)
        self._execute(query, params)
        return None

    query = sql

    def table(self, name):
        return GovernedRelation(self, f"SELECT * FROM {name}")

    view = table

    def values(self, *rows):
        raise GovernedError("db.values() is not available in governed scripts")

    def register(self, name, obj):
        table = _to_arrow(obj)
        if table is None:
            raise TypeError("db.register() takes a pandas/polars DataFrame or an Arrow table")
        self._channel.request({"op": "register", "name": str(name)}, [_ipc(table)])
        return self

    def unregister(self, name):
        self._channel.request({"op": "unregister", "name": str(name)})
        return self

    def from_df(self, df):
        import uuid

        name = f"df_{uuid.uuid4().hex[:10]}"
        self.register(name, df)
        return GovernedRelation(self, f"SELECT * FROM {name}")

    from_arrow = from_df

    def _reader(self, func: str, path, kwargs) -> GovernedRelation:
        def lit(v):
            if isinstance(v, bool):
                return "true" if v else "false"
            if isinstance(v, (int, float)):
                return repr(v)
            return "'" + str(v).replace("'", "''") + "'"

        if isinstance(path, (list, tuple)):
            target = "[" + ", ".join(lit(p) for p in path) + "]"
        else:
            target = lit(os.fspath(path))
        opts = "".join(f", {k} = {lit(v)}" for k, v in kwargs.items())
        return GovernedRelation(self, f"SELECT * FROM {func}({target}{opts})")

    def read_csv(self, path, **kwargs):
        return self._reader("read_csv", path, kwargs)

    read_csv_auto = read_csv

    def read_parquet(self, path, **kwargs):
        return self._reader("read_parquet", path, kwargs)

    from_parquet = read_parquet

    def read_json(self, path, **kwargs):
        return self._reader("read_json", path, kwargs)

    def install_extension(self, name, **_kwargs):
        self._execute(f"INSTALL {name}")

    def load_extension(self, name):
        self._execute(f"LOAD {name}")

    def begin(self):
        return self.execute("BEGIN TRANSACTION")

    def commit(self):
        return self.execute("COMMIT")

    def rollback(self):
        return self.execute("ROLLBACK")

    def cursor(self):
        return self

    def duplicate(self):
        return self

    def close(self):
        return None

    def interrupt(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # -- results of the last execute ----------------------------------------

    def _need(self) -> _Result:
        last = object.__getattribute__(self, "_last")
        if last is None:
            raise GovernedError("No open result set")
        return last

    def fetchall(self):
        r = self._need()
        rows = r.rows()[r._pos:]
        r._pos = len(r.rows())
        return list(rows)

    def fetchone(self):
        r = self._need()
        rows = r.rows()
        if r._pos >= len(rows):
            return None
        r._pos += 1
        return rows[r._pos - 1]

    def fetchmany(self, size: int = 1):
        r = self._need()
        rows = r.rows()[r._pos:r._pos + size]
        r._pos += len(rows)
        return list(rows)

    def fetchdf(self, *args, **kwargs):
        t = self._need().table
        return t.to_pandas() if t is not None else None

    df = fetchdf
    fetch_df = fetchdf

    def fetch_arrow_table(self, *args, **kwargs):
        return self._need().table

    arrow = fetch_arrow_table

    def pl(self):
        import polars as pl

        return pl.from_arrow(self._need().table)

    def fetchnumpy(self):
        t = self._need().table
        return {name: col.to_numpy() for name, col in zip(t.column_names, t.columns)} if t is not None else {}

    @property
    def description(self):
        last = object.__getattribute__(self, "_last")
        if last is None or not last.columns:
            return None
        return [(c, t, None, None, None, None, None) for c, t in zip(last.columns, last.types)]

    @property
    def rowcount(self):
        last = object.__getattribute__(self, "_last")
        return -1 if last is None else last.rowcount

    def __getattr__(self, name):
        raise AttributeError(
            f"db.{name} is not available in a governed script: db is a governed "
            "proxy that sends SQL to the server (masking and row policies apply)."
        )

    def __setattr__(self, name, value):
        raise AttributeError("The governed db proxy cannot be modified")

    def __repr__(self) -> str:
        return "<havn governed db>"


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

_BLOCKED_MODULES = frozenset({"duckdb", "_duckdb", "ctypes", "_ctypes", "cffi", "_cffi_backend"})
_BLOCKED_EVENT_PREFIXES = (
    "ctypes.", "subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn",
    "os.startfile", "os.fork", "os.forkpty", "os.link", "os.symlink", "_winapi.CreateProcess",
    "_posixsubprocess", "pty.spawn", "winreg.",
)
_PROTECTED_SUFFIXES = (".duckdb", ".duckdb.wal", ".ddb", ".wal")


class _ModuleBlocker:
    """A meta-path finder refusing modules that could reach the warehouse natively."""

    @staticmethod
    def find_spec(name, path=None, target=None):
        if name.split(".")[0] in _BLOCKED_MODULES:
            raise ImportError(
                f"import {name} is not available in a governed script; use db, which "
                "applies masking and row policies"
            )
        return None


def _install_guards(protected: list[str]) -> None:
    norm_roots = [os.path.normcase(os.path.abspath(p)) for p in protected]

    def _is_protected(path) -> bool:
        if isinstance(path, int):
            return False
        try:
            raw = os.fsdecode(path)
        except TypeError:
            return False
        full = os.path.normcase(os.path.abspath(raw))
        try:
            real = os.path.normcase(os.path.realpath(raw))
        except (OSError, ValueError):
            real = full
        for candidate in (full, real):
            low = candidate.lower()
            if low.endswith(_PROTECTED_SUFFIXES):
                return True
            for root in norm_roots:
                if candidate == root or candidate.startswith(root + os.sep):
                    return True
        return False

    def hook(event, args):
        if event == "open":
            if args and _is_protected(args[0]):
                raise PermissionError(f"{args[0]!r} is part of the warehouse and cannot be opened from a governed script")
            return
        if event in ("os.rename", "os.replace", "shutil.copyfile", "shutil.copytree", "shutil.move", "os.truncate", "os.remove"):
            if any(_is_protected(a) for a in args[:2] if isinstance(a, (str, bytes, os.PathLike))):
                raise PermissionError("Warehouse files cannot be touched from a governed script")
            return
        if event == "import":
            if args and str(args[0]).split(".")[0] in _BLOCKED_MODULES:
                raise ImportError(f"import {args[0]} is not available in a governed script; use db")
            return
        if event.startswith(_BLOCKED_EVENT_PREFIXES):
            raise PermissionError(f"{event} is not available in a governed script")

    for mod in list(sys.modules):
        if mod.split(".")[0] in ("duckdb", "_duckdb"):
            del sys.modules[mod]
    sys.meta_path.insert(0, _ModuleBlocker)
    sys.addaudithook(hook)


def _check_isolation(warehouse: str | None) -> bool:
    """True when this process cannot read the warehouse file at the OS level."""
    if not warehouse or not os.path.exists(warehouse):
        return True
    try:
        fd = os.open(warehouse, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return True
    try:
        os.read(fd, 16)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Running code
# ---------------------------------------------------------------------------


def _format_value(value) -> dict:
    """A cell's last expression as a notebook output."""
    try:
        import pandas as pd  # noqa: F401
    except Exception:
        pd = None
    table = None
    if isinstance(value, GovernedRelation):
        table = value.arrow()
    elif isinstance(value, GovernedDB):
        table = value.fetch_arrow_table() if object.__getattribute__(value, "_last") is not None else None
    elif type(value).__module__.split(".")[0] in ("pandas", "pyarrow", "polars"):
        try:
            table = _to_arrow(value)
        except Exception:
            table = None
    if table is not None:
        limit = 500
        rows = [list(r.values()) for r in table.slice(0, limit).to_pylist()]
        return {
            "type": "table",
            "columns": list(table.column_names),
            "rows": [[_serialize(v) for v in row] for row in rows],
            "displayed_rows": len(rows),
            "truncated": table.num_rows > limit,
        }
    return {"type": "text", "text": repr(value)}


def _serialize(value):
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


def _run_cell(source: str, namespace: dict) -> dict:
    outputs: list[dict] = []
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        tree = ast.parse(source)
        last = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            expr = tree.body.pop()
            if tree.body:
                exec(compile(tree, "<cell>", "exec"), namespace)
            last = eval(compile(ast.Expression(expr.value), "<cell>", "eval"), namespace)
        else:
            exec(compile(tree, "<cell>", "exec"), namespace)
        if last is not None:
            outputs.append(_format_value(last))
    except BaseException as e:  # noqa: BLE001 - a cell's failure is its output
        if isinstance(e, SystemExit) and e.code in (None, 0):
            pass
        else:
            outputs.append({"type": "error", "text": traceback.format_exc()})
    finally:
        sys.stdout, sys.stderr = saved
    if out.getvalue():
        outputs.insert(0, {"type": "text", "text": out.getvalue()})
    if err.getvalue():
        outputs.append({"type": "text", "text": err.getvalue()})
    return {"outputs": outputs}


def _run_script(path: str, source: str, db: GovernedDB) -> dict:
    namespace = {"db": db, "__file__": path, "__name__": os.path.splitext(os.path.basename(path))[0],
                 "__builtins__": builtins}
    try:
        import pandas as pd

        namespace["pd"] = pd
    except Exception:
        pass
    try:
        code = compile(source, path, "exec")
        tree = ast.parse(source)
        legacy = any(isinstance(n, ast.FunctionDef) and n.name == "run" for n in tree.body)
        exec(code, namespace)
        if legacy and callable(namespace.get("run")):
            namespace["run"](db)
        return {"ok": True}
    except SystemExit as e:
        if e.code in (None, 0):
            return {"ok": True}
        return {"ok": False, "error": f"SystemExit({e.code})", "traceback": traceback.format_exc()}
    except BaseException as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != here]
    hello = json.loads(sys.stdin.readline())
    try:
        sys.stdin.close()
    except Exception:
        pass
    sys.stdin = io.StringIO("")
    if hello.get("cwd"):
        os.chdir(hello["cwd"])
    from multiprocessing.connection import Client

    conn = Client((hello["host"], int(hello["port"])), authkey=bytes.fromhex(hello["authkey"]))
    channel = _Channel(conn)
    isolated = _check_isolation(hello.get("warehouse"))
    for name in hello.get("preload", []):
        try:
            __import__(name)
        except Exception:
            pass
    _install_guards(list(hello.get("protected", [])))
    del hello
    channel.send({"op": "hello", "isolated": isolated, "pid": os.getpid()})
    db = GovernedDB(channel)
    namespace: dict = {"db": db, "__name__": "__notebook__", "__builtins__": builtins}
    try:
        import pandas as pd

        namespace["pd"] = pd
    except Exception:
        pass
    while True:
        try:
            msg, _blobs = channel.recv()
        except (EOFError, OSError):
            return 0
        op = msg.get("op")
        if op == "run_script":
            result = _run_script(msg["path"], msg["source"], db)
            sys.stdout.flush()
            sys.stderr.flush()
            channel.send({"op": "done", **result})
        elif op == "run_cell":
            result = _run_cell(msg["source"], namespace)
            channel.send({"op": "done", **result})
        elif op == "reset":
            namespace.clear()
            namespace.update({"db": db, "__name__": "__notebook__", "__builtins__": builtins})
            channel.send({"op": "done"})
        elif op == "shutdown":
            channel.send({"op": "done"})
            return 0


if __name__ == "__main__":
    sys.exit(main())
