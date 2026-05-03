"""Python SQL Macros (UDFs): discover, register, and list user-defined functions.

Users place Python files with ``@macro``-decorated functions in a ``macros/``
directory.  Plain ``.sql`` files containing ``CREATE MACRO`` statements are also
supported.  All discovered macros are registered on the DuckDB connection so
they can be used in SQL transforms.

Table-returning macros (``@table_macro``) are registered as a pair:
  - an internal scalar JSON UDF named ``_<name>_json``
  - a SQL TABLE MACRO named ``<name>`` that wraps it via ``json_each``
This avoids any dependency on pyarrow.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import logging
import sys
import types
from dataclasses import dataclass
from inspect import signature
from pathlib import Path
from typing import Any, Callable, get_type_hints

import duckdb

logger = logging.getLogger("havn.macros")

# ---------------------------------------------------------------------------
# Type mapping: Python → DuckDB
# ---------------------------------------------------------------------------

_PY_TO_DUCKDB: dict[type, str] = {
    str: "VARCHAR",
    int: "INTEGER",
    float: "DOUBLE",
    bool: "BOOLEAN",
    datetime.date: "DATE",
    datetime.datetime: "TIMESTAMP",
}

_DEFAULT_TYPE = "VARCHAR"


def _map_type(py_type: type | None) -> str:
    """Map a Python type hint to a DuckDB type string."""
    if py_type is None:
        return _DEFAULT_TYPE
    return _PY_TO_DUCKDB.get(py_type, _DEFAULT_TYPE)


def _infer_schema_from_row(row: dict[str, Any]) -> dict[str, str]:
    """Infer a DuckDB schema from the first row of a table macro's output."""
    schema: dict[str, str] = {}
    for col, val in row.items():
        if isinstance(val, bool):
            schema[col] = "BOOLEAN"
        elif isinstance(val, int):
            schema[col] = "INTEGER"
        elif isinstance(val, float):
            schema[col] = "DOUBLE"
        else:
            schema[col] = "VARCHAR"
    return schema


# ---------------------------------------------------------------------------
# @macro decorator
# ---------------------------------------------------------------------------

_MACRO_ATTR = "__havn_macro__"
_TABLE_MACRO_ATTR = "__havn_table_macro__"


@dataclass
class MacroInfo:
    """Metadata attached to a scalar @macro-decorated function."""

    name: str
    func: Callable[..., Any]
    params: list[dict[str, str]]  # [{"name": ..., "type": ...}, ...]
    return_type: str
    docstring: str
    source_file: str


@dataclass
class TableMacroInfo:
    """Metadata attached to a @table_macro-decorated function."""

    name: str
    func: Callable[..., Any]
    params: list[dict[str, str]]  # [{"name": ..., "type": ...}, ...]
    schema: dict[str, str]        # column name → DuckDB type
    docstring: str
    source_file: str


def macro(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that marks a Python function for registration as a DuckDB scalar UDF.

    Usage::

        from havn import macro

        @macro
        def my_func(x: str) -> int:
            ...
    """
    # get_type_hints needs the function's global namespace to resolve
    # forward references (caused by ``from __future__ import annotations``).
    fn_globals = getattr(fn, "__globals__", {})
    try:
        hints = get_type_hints(fn, globalns=fn_globals)
    except Exception:
        # Fallback: inspect raw annotations without resolution.
        hints = {
            k: v for k, v in getattr(fn, "__annotations__", {}).items()
            if isinstance(v, type)
        }
    sig = signature(fn)

    params: list[dict[str, str]] = []
    for pname, p in sig.parameters.items():
        py_type = hints.get(pname)
        params.append({"name": pname, "type": _map_type(py_type)})

    return_type = _map_type(hints.get("return"))

    info = MacroInfo(
        name=fn.__name__,
        func=fn,
        params=params,
        return_type=return_type,
        docstring=(fn.__doc__ or "").strip(),
        source_file="",
    )
    setattr(fn, _MACRO_ATTR, info)
    return fn


def table_macro(
    schema: dict[str, str] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator that marks a Python function as a table-returning DuckDB UDF.

    The decorated function must return a list of dicts; each dict is a row and
    its keys are the column names.

    ``schema`` is a mapping of column names to DuckDB type strings
    (``VARCHAR``, ``INTEGER``, ``DOUBLE``, ``BOOLEAN``, ``DATE``,
    ``TIMESTAMP``).  When omitted, the schema is inferred from the first row
    returned by a zero-argument probe call — prefer explicit schemas.

    Usage::

        from havn import table_macro

        @table_macro(schema={"id": "INTEGER", "name": "VARCHAR"})
        def active_users(status: str) -> list[dict]:
            if status == "active":
                return [{"id": 1, "name": "Alice"}]
            return [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]

    Used in SQL as::

        SELECT * FROM active_users('active')
    """
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn_globals = getattr(fn, "__globals__", {})
        try:
            hints = get_type_hints(fn, globalns=fn_globals)
        except Exception:
            hints = {
                k: v for k, v in getattr(fn, "__annotations__", {}).items()
                if isinstance(v, type)
            }
        sig = signature(fn)

        params: list[dict[str, str]] = []
        for pname, _ in sig.parameters.items():
            py_type = hints.get(pname)
            params.append({"name": pname, "type": _map_type(py_type)})

        resolved_schema = schema

        info = TableMacroInfo(
            name=fn.__name__,
            func=fn,
            params=params,
            schema=resolved_schema or {},
            docstring=(fn.__doc__ or "").strip(),
            source_file="",
        )
        setattr(fn, _TABLE_MACRO_ATTR, info)
        return fn

    return decorator


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _discover_all_python_macros(
    macros_dir: Path,
) -> tuple[list[MacroInfo], list[TableMacroInfo]]:
    """Import all ``.py`` files in *macros_dir* and collect both decorator types.

    Each file is loaded exactly once so that files containing both ``@macro``
    and ``@table_macro`` functions are not executed twice.
    """
    scalars: list[MacroInfo] = []
    tables: list[TableMacroInfo] = []

    if not macros_dir.is_dir():
        return scalars, tables

    for py_file in sorted(macros_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            mod = _load_module(py_file)
        except Exception as exc:
            logger.warning("Failed to load macro file %s: %s", py_file, exc)
            continue

        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            scalar_info: MacroInfo | None = getattr(obj, _MACRO_ATTR, None)
            if scalar_info is not None:
                scalar_info.source_file = str(py_file)
                scalars.append(scalar_info)

            table_info: TableMacroInfo | None = getattr(obj, _TABLE_MACRO_ATTR, None)
            if table_info is not None:
                table_info.source_file = str(py_file)
                tables.append(table_info)

    return scalars, tables


def _discover_python_macros(macros_dir: Path) -> list[MacroInfo]:
    """Import all ``.py`` files in *macros_dir* and collect @macro functions."""
    scalars, _ = _discover_all_python_macros(macros_dir)
    return scalars


def _discover_stdlib_macros() -> tuple[list[MacroInfo], list[TableMacroInfo]]:
    """Discover macros bundled with havn under ``havn.stdlib.*``.

    Each submodule of ``havn.stdlib`` is imported and walked for
    ``@macro`` / ``@table_macro``-decorated functions. Modules whose
    name starts with ``_`` are skipped. Discovery failures are logged
    but never abort registration — a broken stdlib module shouldn't
    prevent user macros from loading.
    """
    import importlib
    import pkgutil

    scalars: list[MacroInfo] = []
    tables: list[TableMacroInfo] = []

    try:
        import havn.stdlib as stdlib_pkg
    except Exception as exc:  # pragma: no cover — package always present
        logger.debug("havn.stdlib unavailable: %s", exc)
        return scalars, tables

    for mod_info in pkgutil.iter_modules(stdlib_pkg.__path__):
        if mod_info.name.startswith("_"):
            continue
        full = f"havn.stdlib.{mod_info.name}"
        try:
            mod = importlib.import_module(full)
        except Exception as exc:
            logger.warning("Failed to import havn stdlib module %s: %s", full, exc)
            continue

        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            scalar_info: MacroInfo | None = getattr(obj, _MACRO_ATTR, None)
            if scalar_info is not None:
                scalar_info.source_file = f"<havn.stdlib.{mod_info.name}>"
                scalars.append(scalar_info)
            table_info: TableMacroInfo | None = getattr(obj, _TABLE_MACRO_ATTR, None)
            if table_info is not None:
                table_info.source_file = f"<havn.stdlib.{mod_info.name}>"
                tables.append(table_info)

    return scalars, tables


def _discover_table_macros(macros_dir: Path) -> list[TableMacroInfo]:
    """Import all ``.py`` files in *macros_dir* and collect @table_macro functions."""
    _, tables = _discover_all_python_macros(macros_dir)
    return tables


def _load_module(path: Path) -> types.ModuleType:
    """Dynamically import a Python file as a module."""
    module_name = f"havn_macros.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _discover_sql_macros(macros_dir: Path) -> list[dict[str, str]]:
    """Collect ``.sql`` files containing ``CREATE MACRO`` statements."""
    results: list[dict[str, str]] = []
    if not macros_dir.is_dir():
        return results

    for sql_file in sorted(macros_dir.glob("*.sql")):
        if sql_file.name.startswith("_"):
            continue
        try:
            text = sql_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to read SQL macro file %s: %s", sql_file, exc)
            continue
        if text.strip():
            results.append({"sql": text, "source_file": str(sql_file)})

    return results


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _resolve_table_schema(info: TableMacroInfo) -> dict[str, str]:
    """Return the column schema for a table macro, inferring if necessary."""
    if info.schema:
        return info.schema

    # Schema inference: probe with default/None args
    sig = signature(info.func)
    probe_args: list[Any] = []
    for param in sig.parameters.values():
        if param.default is not param.empty:
            probe_args.append(param.default)
        elif param.annotation in (str, "str"):
            probe_args.append("")
        elif param.annotation in (int, "int"):
            probe_args.append(0)
        elif param.annotation in (float, "float"):
            probe_args.append(0.0)
        elif param.annotation in (bool, "bool"):
            probe_args.append(False)
        else:
            probe_args.append(None)

    try:
        rows = list(info.func(*probe_args))
        if rows:
            return _infer_schema_from_row(rows[0])
    except Exception as exc:
        logger.warning(
            "Schema inference failed for table macro '%s': %s. "
            "Provide an explicit schema= argument.",
            info.name, exc,
        )
    return {}


def _make_json_udf(fn: Callable[..., Any], param_count: int) -> Callable[..., Any]:
    """Create a fixed-arity JSON-wrapping UDF around *fn*.

    DuckDB infers function arity from the Python function's signature, so
    ``*args`` (arity 1) won't match a 2-param registration.  We generate a
    thin wrapper with the correct number of positional parameters.
    """
    if param_count == 0:
        def _udf0() -> str:
            return json.dumps(list(fn()))
        return _udf0
    if param_count == 1:
        def _udf1(a: Any) -> str:
            return json.dumps(list(fn(a)))
        return _udf1
    if param_count == 2:
        def _udf2(a: Any, b: Any) -> str:
            return json.dumps(list(fn(a, b)))
        return _udf2
    if param_count == 3:
        def _udf3(a: Any, b: Any, c: Any) -> str:
            return json.dumps(list(fn(a, b, c)))
        return _udf3
    # General case: generate source code with the right arity
    param_names = [f"p{i}" for i in range(param_count)]
    sig_str = ", ".join(param_names)
    call_str = ", ".join(param_names)
    code = f"def _udf({sig_str}):\n    return __dumps__(list(__fn__({call_str})))\n"
    ns: dict[str, Any] = {"__fn__": fn, "__dumps__": json.dumps}
    exec(code, ns)  # noqa: S102
    return ns["_udf"]


def _build_table_macro_sql(name: str, params: list[dict[str, str]], schema: dict[str, str]) -> str:
    """Build the SQL TABLE MACRO statement for a table macro.

    Registers a SQL TABLE MACRO named *name* that calls the internal scalar
    JSON UDF (``_<name>_json``) and expands rows via ``json_each``.

    Parameter names are double-quoted to handle SQL reserved words.
    """
    internal_name = f"_{name}_json"
    return _build_table_macro_sql_versioned(name, internal_name, params, schema)


def _build_table_macro_sql_versioned(
    public_name: str,
    internal_json_name: str,
    params: list[dict[str, str]],
    schema: dict[str, str],
    temp: bool = False,
) -> str:
    """Build the SQL TABLE MACRO that wraps a versioned internal JSON UDF.

    Uses ``CREATE OR REPLACE MACRO`` so re-registering the public name with a
    different internal UDF (hot-reload) never conflicts. ``temp=True`` emits
    a TEMP MACRO (session-scoped, not catalog-stored).
    """
    quoted_params = [f'"{p["name"]}"' for p in params]
    param_list = ", ".join(quoted_params)

    cast_exprs = ", ".join(
        f"(value->>'$.{col}')::{dtype} AS {col}"
        for col, dtype in schema.items()
    )
    internal_call = f"{internal_json_name}({param_list})"

    kind = "TEMP MACRO" if temp else "MACRO"
    return (
        f"CREATE OR REPLACE {kind} {public_name}({param_list}) AS TABLE\n"
        f"SELECT {cast_exprs}\n"
        f"FROM json_each({internal_call})"
    )


def _force_reload_macro_modules(macros_dir: Path) -> None:
    """Remove previously cached module entries for macros/ so files are re-executed.

    Two layers of caching must be cleared:
    1. ``sys.modules`` — Python's import cache; without eviction, a second call
       to ``_load_module`` returns the exact same module object.
    2. ``__pycache__`` bytecode — Python skips source re-compilation when the
       ``.pyc`` mtime matches the ``.py`` mtime.  On Windows (and fast SSDs
       elsewhere), two writes within the same second produce identical mtimes,
       so the bytecode cache is never invalidated.  Removing ``__pycache__``
       forces a fresh compile on the next import.
    """
    import importlib
    import shutil

    prefix = "havn_macros."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for key in stale:
        del sys.modules[key]

    pycache = macros_dir / "__pycache__"
    if pycache.is_dir():
        try:
            shutil.rmtree(pycache)
        except Exception as exc:
            logger.debug("Could not remove macro __pycache__: %s", exc)

    importlib.invalidate_caches()


# ---------------------------------------------------------------------------
# Per-connection reload counter
# ---------------------------------------------------------------------------
# DuckDB 1.x does not support replacing a Python UDF registered via
# create_function even after remove_function (a write-write catalog conflict
# is raised on the same connection).  The workaround is to give every
# registered Python callable a versioned internal name
# (e.g. _udf_mask_email_3) and redirect the public name via a SQL
# CREATE OR REPLACE MACRO alias.  The counter advances on each call to
# register_macros, ensuring each reload gets a fresh internal name that
# does not conflict with any previously registered UDF on the same
# connection object.

# id(conn) → int  — tracks how many times macros have been registered on each conn
_conn_reload_counters: dict[int, int] = {}


def _next_reload_id(conn: duckdb.DuckDBPyConnection) -> int:
    """Return and increment the reload counter for this connection."""
    conn_id = id(conn)
    n = _conn_reload_counters.get(conn_id, 0)
    _conn_reload_counters[conn_id] = n + 1
    return n


# Connections (by id) that have already had macros registered.  We use this
# to make register_macros() idempotent so multiple call sites during startup
# don't fight DuckLake's "function already exists" catalog rule. Hot-reload
# clears the entry for a conn so the next call goes through.
_conn_macros_registered: set[int] = set()


# Strong references to every Python function we have ever handed to
# ``conn.create_function``.  DuckDB's UDF storage on the DuckLake backend
# keeps only a C-level pointer to the Python callable; if the function
# loses every Python ref (e.g. because we evicted its module from
# ``sys.modules``), CPython is free to GC it and reuse its memory address
# for another object.  When DuckDB later invokes the UDF, the C pointer
# now points at a totally unrelated Python callable and the user gets a
# bizarre traceback ("'float' has no attribute 'execute'", etc.).  Pinning
# the functions here keeps every registered UDF alive for the life of the
# process and eliminates that class of bug entirely.
_pinned_udfs: list[Callable[..., Any]] = []


def reset_macro_state() -> None:
    """Forget per-connection registration state.  Called by hot-reload."""
    _conn_macros_registered.clear()
    _conn_reload_counters.clear()


def _is_read_only_connection(conn: duckdb.DuckDBPyConnection) -> bool:
    """Best-effort check for whether ``conn`` was opened read-only.

    DuckDB doesn't expose a flag for it, so we probe with a non-TEMP
    ``CREATE OR REPLACE MACRO`` against the default catalog — TEMP
    objects live in the always-writable temp catalog and can't tell
    us whether the persistent default is read-only.
    """
    try:
        conn.execute("CREATE OR REPLACE MACRO __havn_ro_probe() AS 0")
        try:
            conn.execute("DROP MACRO IF EXISTS __havn_ro_probe")
        except Exception:
            pass
        return False
    except Exception as exc:
        if "read-only" in str(exc).lower():
            return True
        return False


def _build_scalar_macro_sql(
    public_name: str,
    internal_name: str,
    params: list[dict[str, str]],
    return_type: str,
    temp: bool = False,
) -> str:
    """Build a SQL MACRO that proxies a Python scalar UDF.

    When ``temp`` is True, emits ``CREATE OR REPLACE TEMP MACRO`` so the
    alias lives in the session-scoped ``temp`` catalog instead of the
    default database. Required on DuckLake, which rejects persistent
    CREATE MACRO in its catalog. TEMP macros resolve via the standard
    DuckDB search path so callers do not need to qualify the name.
    """
    quoted_params = [f'"{p["name"]}"' for p in params]
    param_list = ", ".join(quoted_params)
    call_args = ", ".join(f'"{p["name"]}"' for p in params)
    kind = "TEMP MACRO" if temp else "MACRO"
    return (
        f"CREATE OR REPLACE {kind} {public_name}({param_list}) AS "
        f"{internal_name}({call_args})"
    )


def register_macros(
    conn: duckdb.DuckDBPyConnection,
    project_dir: Path,
    *,
    force_reload: bool = False,
) -> int:
    """Discover and register all macros from ``project_dir/macros/``.

    Idempotent **per connection**: a connection that has already had macros
    registered is left alone, because re-registering the same UDF on the
    same connection is either a no-op (DuckDB) or a hard catalog error
    (DuckLake — see the hot-reload caveat in CHANGELOG). To pick up edits
    to ``macros/*.py`` while a connection is alive, pass ``force_reload=True``;
    this is what the macros file watcher does.

    Returns the number of macros registered (0 on a re-entrant call).
    Stdlib macros (``havn.stdlib.*``) are registered first; user macros
    in ``project_dir/macros/`` with the same name override them.
    """
    macros_dir = project_dir / "macros"
    has_user_macros = macros_dir.is_dir()
    # Even projects without a ``macros/`` directory still get the stdlib,
    # so don't bail here unless we have neither stdlib nor user macros to
    # register. Stdlib discovery is cheap.
    stdlib_scalars, stdlib_tables = _discover_stdlib_macros()
    if not has_user_macros and not stdlib_scalars and not stdlib_tables:
        return 0

    # Skip re-registration on connections we've already done.  Without this
    # the second call hits DuckLake's "function already exists" catalog rule
    # and the second module reload makes the *first* registration's function
    # objects GC-eligible — DuckDB's C pointer then dangles into reused
    # memory and queries hit unrelated Python callables.
    conn_id = id(conn)
    if not force_reload and conn_id in _conn_macros_registered:
        return 0

    # Module reload (and __pycache__ eviction) is destructive — only do it
    # when the caller is explicitly asking for a fresh read of the macros/
    # directory, e.g. on a file watcher event. Initial registration just
    # imports normally.
    if force_reload:
        _force_reload_macro_modules(macros_dir)

    from havn.engine.database import _is_ducklake_connection
    is_lake = _is_ducklake_connection(conn)

    # Read-only connections (ad-hoc ``havn query``, read pool members) can't
    # run ``CREATE MACRO`` to alias internal UDFs. ``create_function`` still
    # works, so for the DuckDB backend we install the Python UDFs and let the
    # public-name aliases come from the SQL MACRO already persisted in the
    # file catalog by the writer.
    is_read_only = _is_read_only_connection(conn)

    # Reload generation: used to construct unique internal UDF names on the
    # DuckDB-file backend (DuckLake takes the public name directly).
    gen = _next_reload_id(conn)

    count = 0

    user_scalars, user_tables = (
        _discover_all_python_macros(macros_dir) if has_user_macros else ([], [])
    )

    # Concatenate stdlib first, then user. If a name appears in both,
    # the user's entry wins because dict insertion order keeps the LAST
    # write — but we also log it so silent shadowing is debuggable.
    user_scalar_names = {s.name for s in user_scalars}
    user_table_names = {t.name for t in user_tables}
    for s in stdlib_scalars:
        if s.name in user_scalar_names:
            logger.warning(
                "User macro '%s' shadows havn.stdlib macro of the same name", s.name
            )
    for t in stdlib_tables:
        if t.name in user_table_names:
            logger.warning(
                "User table_macro '%s' shadows havn.stdlib macro of the same name", t.name
            )

    # Filter stdlib entries that the user overrides so we don't register
    # twice and trip DuckDB's "function already exists".
    stdlib_scalars_kept = [s for s in stdlib_scalars if s.name not in user_scalar_names]
    stdlib_tables_kept = [t for t in stdlib_tables if t.name not in user_table_names]

    scalar_macros = stdlib_scalars_kept + user_scalars
    table_macros = stdlib_tables_kept + user_tables

    # Scalar Python UDFs
    for info in scalar_macros:
        try:
            param_types = [p["type"] for p in info.params]
            if is_lake:
                # Pin the function so CPython can't GC it even if its module
                # gets evicted from sys.modules later — DuckDB on DuckLake
                # holds only a C pointer to it, so we have to keep the
                # Python object alive ourselves.
                _pinned_udfs.append(info.func)
                conn.create_function(info.name, info.func, param_types, info.return_type)
                count += 1
                logger.debug("Registered DuckLake UDF: %s", info.name)
                continue

            # DuckDB file backend: versioned internal + CREATE OR REPLACE MACRO alias.
            _pinned_udfs.append(info.func)
            internal_name = f"_udf_{info.name}_{gen}"
            conn.create_function(
                internal_name,
                info.func,
                param_types,
                info.return_type,
            )
            if not is_read_only:
                # Read-only conns inherit the public-name MACRO from the
                # file catalog written by the writer; they only need the
                # internal Python UDF re-registered for this conn.
                conn.execute(
                    _build_scalar_macro_sql(
                        info.name, internal_name, info.params, info.return_type,
                        temp=False,
                    )
                )
            count += 1
            logger.debug("Registered Python macro: %s (internal: %s)", info.name, internal_name)
        except Exception as exc:
            # Sibling connections to the same DuckDB file share the UDF
            # catalog, so a second registration sees "already exists". The
            # function is still callable from this conn — silent debug only.
            if "already exists" in str(exc):
                logger.debug("Macro '%s' already registered (sibling conn)", info.name)
            else:
                logger.warning("Failed to register macro '%s': %s", info.name, exc)

    # Table-returning Python UDFs. DuckLake accepts persistent (non-TEMP)
    # ``CREATE MACRO ... AS TABLE`` and the underlying JSON UDF registered
    # on the parent connection propagates to cursors, so the same path works
    # on both backends.
    for info in table_macros:
        try:
            schema = _resolve_table_schema(info)
            if not schema:
                logger.warning(
                    "Skipping table macro '%s': could not determine column schema. "
                    "Add a schema= argument to @table_macro.",
                    info.name,
                )
                continue

            # Step 1: register the internal JSON scalar UDF with a versioned name.
            param_types = [p["type"] for p in info.params]
            json_udf = _make_json_udf(info.func, len(info.params))
            _pinned_udfs.append(json_udf)
            _pinned_udfs.append(info.func)
            internal_name = f"_udf_{info.name}_{gen}_json"
            conn.create_function(
                internal_name,
                json_udf,
                param_types,
                "VARCHAR",
            )

            # Step 2: register the SQL TABLE MACRO wrapper (CREATE OR REPLACE is idempotent).
            if not is_read_only:
                macro_sql = _build_table_macro_sql_versioned(
                    info.name, internal_name, info.params, schema, temp=False,
                )
                conn.execute(macro_sql)

            count += 1
            logger.debug("Registered table macro: %s (internal: %s)", info.name, internal_name)
        except Exception as exc:
            if "already exists" in str(exc):
                logger.debug("Table macro '%s' already registered (sibling conn)", info.name)
            else:
                logger.warning("Failed to register table macro '%s': %s", info.name, exc)

    # SQL macros — CREATE OR REPLACE handles idempotency natively.
    import re as _re

    if is_read_only:
        # SQL macros only exist as catalog entries, which are already in place
        # from the writer's registration. Skip entirely on read-only conns.
        sql_macro_iter = []
    else:
        sql_macro_iter = _discover_sql_macros(macros_dir)

    for entry in sql_macro_iter:
        try:
            sql = entry["sql"]
            # Ensure idempotency: promote CREATE MACRO → CREATE OR REPLACE MACRO.
            sql = _re.sub(
                r"\bCREATE\s+MACRO\b",
                "CREATE OR REPLACE MACRO",
                sql,
                flags=_re.IGNORECASE,
            )
            conn.execute(sql)
            count += 1
            logger.debug("Registered SQL macro from %s", entry["source_file"])
        except Exception as exc:
            logger.warning(
                "Failed to execute SQL macro from %s: %s",
                entry["source_file"],
                exc,
            )

    # Mark this connection as having had macros registered so subsequent
    # callers (deps.py post-init, ReadPool slots) skip out instead of
    # fighting the catalog.
    _conn_macros_registered.add(conn_id)
    return count


def _duckdb_type(type_str: str) -> str:
    """Return a DuckDB type string suitable for ``create_function``."""
    return type_str


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def list_macros(project_dir: Path) -> list[dict[str, Any]]:
    """Return metadata for all discovered macros (stdlib + user Python + SQL).

    Each dict has keys: name, params, return_type, docstring, source_file,
    kind, is_stdlib. ``kind`` is one of: ``"scalar"``, ``"table"``,
    ``"sql"``. Stdlib entries shadowed by a user macro of the same name
    are filtered out (matching the registration override behavior).
    """
    macros_dir = project_dir / "macros"
    results: list[dict[str, Any]] = []

    user_scalar_macros, user_table_macros = _discover_all_python_macros(macros_dir)
    stdlib_scalar_macros, stdlib_table_macros = _discover_stdlib_macros()

    user_scalar_names = {m.name for m in user_scalar_macros}
    user_table_names = {m.name for m in user_table_macros}

    # Stdlib first, then user — same precedence the registrar uses.
    for info in stdlib_scalar_macros:
        if info.name in user_scalar_names:
            continue
        results.append({
            "name": info.name,
            "params": info.params,
            "return_type": info.return_type,
            "docstring": info.docstring,
            "source_file": info.source_file,
            "kind": "scalar",
            "is_stdlib": True,
        })
    for info in user_scalar_macros:
        results.append({
            "name": info.name,
            "params": info.params,
            "return_type": info.return_type,
            "docstring": info.docstring,
            "source_file": info.source_file,
            "kind": "scalar",
            "is_stdlib": False,
        })

    for info in stdlib_table_macros:
        if info.name in user_table_names:
            continue
        results.append({
            "name": info.name,
            "params": info.params,
            "return_type": "TABLE",
            "schema": info.schema,
            "docstring": info.docstring,
            "source_file": info.source_file,
            "kind": "table",
            "is_stdlib": True,
        })
    for info in user_table_macros:
        results.append({
            "name": info.name,
            "params": info.params,
            "return_type": "TABLE",
            "schema": info.schema,
            "docstring": info.docstring,
            "source_file": info.source_file,
            "kind": "table",
            "is_stdlib": False,
        })

    # SQL macros — parse names from CREATE MACRO statements
    import re

    for entry in _discover_sql_macros(macros_dir):
        sql = entry["sql"]
        m = re.search(r"CREATE\s+(?:OR\s+REPLACE\s+)?MACRO\s+(\w+)\s*\(", sql, re.IGNORECASE)
        name = m.group(1) if m else Path(entry["source_file"]).stem
        results.append({
            "name": name,
            "params": [],
            "return_type": "SQL",
            "docstring": "",
            "source_file": entry["source_file"],
            "kind": "sql",
        })

    return results
