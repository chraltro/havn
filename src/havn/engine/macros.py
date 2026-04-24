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
    # Double-quote param names so DuckDB reserved words (limit, order, …) work.
    quoted_params = [f'"{p["name"]}"' for p in params]
    param_list = ", ".join(quoted_params)

    cast_exprs = ", ".join(
        f"(value->>'$.{col}')::{dtype} AS {col}"
        for col, dtype in schema.items()
    )
    internal_call = f"_{name}_json({param_list})"

    return (
        f"CREATE OR REPLACE MACRO {name}({param_list}) AS TABLE\n"
        f"SELECT {cast_exprs}\n"
        f"FROM json_each({internal_call})"
    )


def register_macros(
    conn: duckdb.DuckDBPyConnection,
    project_dir: Path,
) -> int:
    """Discover and register all macros from ``project_dir/macros/``.

    Returns the number of macros registered.
    """
    macros_dir = project_dir / "macros"
    if not macros_dir.is_dir():
        return 0

    count = 0

    scalar_macros, table_macros = _discover_all_python_macros(macros_dir)

    # Scalar Python UDFs
    for info in scalar_macros:
        try:
            param_types = [p["type"] for p in info.params]
            conn.create_function(
                info.name,
                info.func,
                param_types,
                info.return_type,
            )
            count += 1
            logger.debug("Registered Python macro: %s", info.name)
        except Exception as exc:
            logger.warning("Failed to register macro '%s': %s", info.name, exc)

    # Table-returning Python UDFs
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

            # Step 1: register the internal JSON scalar UDF
            param_types = [p["type"] for p in info.params]
            json_udf = _make_json_udf(info.func, len(info.params))
            internal_name = f"_{info.name}_json"
            conn.create_function(
                internal_name,
                json_udf,
                param_types,
                "VARCHAR",
            )

            # Step 2: register the SQL TABLE MACRO wrapper
            macro_sql = _build_table_macro_sql(info.name, info.params, schema)
            conn.execute(macro_sql)

            count += 1
            logger.debug("Registered table macro: %s", info.name)
        except Exception as exc:
            logger.warning("Failed to register table macro '%s': %s", info.name, exc)

    # SQL macros
    for entry in _discover_sql_macros(macros_dir):
        try:
            conn.execute(entry["sql"])
            count += 1
            logger.debug("Registered SQL macro from %s", entry["source_file"])
        except Exception as exc:
            logger.warning(
                "Failed to execute SQL macro from %s: %s",
                entry["source_file"],
                exc,
            )

    return count


def _duckdb_type(type_str: str) -> str:
    """Return a DuckDB type string suitable for ``create_function``."""
    return type_str


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def list_macros(project_dir: Path) -> list[dict[str, Any]]:
    """Return metadata for all discovered macros (Python + SQL).

    Each dict has keys: name, params, return_type, docstring, source_file, kind.
    ``kind`` is one of: ``"scalar"``, ``"table"``, ``"sql"``.
    """
    macros_dir = project_dir / "macros"
    results: list[dict[str, Any]] = []

    scalar_macros, table_macros = _discover_all_python_macros(macros_dir)

    for info in scalar_macros:
        results.append({
            "name": info.name,
            "params": info.params,
            "return_type": info.return_type,
            "docstring": info.docstring,
            "source_file": info.source_file,
            "kind": "scalar",
        })

    for info in table_macros:
        results.append({
            "name": info.name,
            "params": info.params,
            "return_type": "TABLE",
            "schema": info.schema,
            "docstring": info.docstring,
            "source_file": info.source_file,
            "kind": "table",
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
