"""Python SQL Macros (UDFs): discover, register, and list user-defined functions.

Users place Python files with ``@macro``-decorated functions in a ``macros/``
directory.  Plain ``.sql`` files containing ``CREATE MACRO`` statements are also
supported.  All discovered macros are registered on the DuckDB connection so
they can be used in SQL transforms.
"""

from __future__ import annotations

import datetime
import importlib.util
import logging
import sys
import types
from dataclasses import dataclass, field
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


# ---------------------------------------------------------------------------
# @macro decorator
# ---------------------------------------------------------------------------

_MACRO_ATTR = "__havn_macro__"


@dataclass
class MacroInfo:
    """Metadata attached to a decorated function."""

    name: str
    func: Callable[..., Any]
    params: list[dict[str, str]]  # [{"name": ..., "type": ...}, ...]
    return_type: str
    docstring: str
    source_file: str


def macro(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that marks a Python function for registration as a DuckDB UDF.

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


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _discover_python_macros(macros_dir: Path) -> list[MacroInfo]:
    """Import all ``.py`` files in *macros_dir* and collect decorated functions."""
    results: list[MacroInfo] = []
    if not macros_dir.is_dir():
        return results

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
            info: MacroInfo | None = getattr(obj, _MACRO_ATTR, None)
            if info is not None:
                info.source_file = str(py_file)
                results.append(info)

    return results


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

    # Python UDFs
    for info in _discover_python_macros(macros_dir):
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
    """
    macros_dir = project_dir / "macros"
    results: list[dict[str, Any]] = []

    # Python macros
    for info in _discover_python_macros(macros_dir):
        results.append({
            "name": info.name,
            "params": info.params,
            "return_type": info.return_type,
            "docstring": info.docstring,
            "source_file": info.source_file,
            "kind": "python",
        })

    # SQL macros — parse names from CREATE MACRO statements
    import re

    for entry in _discover_sql_macros(macros_dir):
        sql = entry["sql"]
        # Try to extract name from CREATE [OR REPLACE] MACRO <name>(
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
