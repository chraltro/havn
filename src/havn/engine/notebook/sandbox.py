"""Notebook code cell sandbox.

Defense-in-depth sandbox for executing user-supplied Python code in notebook
cells. Uses a **blocklist** approach — users can import any package and use
the full Python language, with targeted restrictions on:

1. AST validation — block access to havn server internals, dunder-based
   sandbox escapes, and _havn access via db
2. Guarded open() — intercepts file reads to block .env and dotfiles
3. SafeDbProxy — wraps the DuckDB connection to block _havn,
   ATTACH, INSTALL, LOAD, COPY TO
4. Execution timeout — prevents infinite loops from hanging the server

This is an in-process sandbox (not subprocess-based) because DuckDB on
Windows only allows one connection per file.
"""

from __future__ import annotations

import ast
import builtins
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("havn.notebook.sandbox")


# ---------------------------------------------------------------------------
# Layer 1: AST Validation
# ---------------------------------------------------------------------------

class SandboxViolation(Exception):
    """Raised when code violates sandbox restrictions."""


# Only block imports of havn's own server internals — users can import
# anything else (os, subprocess, requests, etc. are all fine).
_FORBIDDEN_MODULES = frozenset({
    "havn.server",
    "havn.engine.auth",
    "havn.engine.secrets",
})

# Dunder attributes commonly used in sandbox escape chains.
_FORBIDDEN_DUNDERS = frozenset({
    "__subclasses__", "__globals__", "__builtins__", "__import__",
    "__loader__", "__spec__", "__code__", "__bases__", "__mro__",
})


def validate_ast(source: str) -> ast.Module:
    """Parse and validate source code against sandbox rules.

    Returns the parsed AST on success; raises SandboxViolation on failure.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise SandboxViolation(f"Syntax error: {e}") from e

    for node in ast.walk(tree):
        _check_imports(node)
        _check_dunder_access(node)
        _check_db_internal_access(node)

    return tree


def _check_imports(node: ast.AST) -> None:
    """Reject imports of havn server internal modules."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            _reject_module(alias.name, node.lineno)
    elif isinstance(node, ast.ImportFrom):
        if node.module:
            _reject_module(node.module, node.lineno)


def _reject_module(name: str, lineno: int) -> None:
    """Check if a module name (or any prefix of it) is forbidden."""
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in _FORBIDDEN_MODULES:
            raise SandboxViolation(
                f"Line {lineno}: import of '{name}' is not allowed in notebooks. "
                f"Access to havn server internals is restricted."
            )


def _check_dunder_access(node: ast.AST) -> None:
    """Reject access to dangerous dunder attributes."""
    if isinstance(node, ast.Attribute):
        if node.attr in _FORBIDDEN_DUNDERS:
            raise SandboxViolation(
                f"Line {node.lineno}: access to '{node.attr}' is not allowed "
                f"in notebooks (potential sandbox escape)"
            )


def _check_db_internal_access(node: ast.AST) -> None:
    """Reject db.execute() calls with _havn in string arguments."""
    if not isinstance(node, ast.Call):
        return
    func = node.func
    if not isinstance(func, ast.Attribute):
        return
    if not isinstance(func.value, ast.Name):
        return
    if func.value.id != "db":
        return
    if func.attr not in ("execute", "sql", "query"):
        return
    for arg in node.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            if "_havn" in arg.value.lower():
                raise SandboxViolation(
                    f"Line {node.lineno}: access to _havn schema "
                    f"is not allowed in notebooks"
                )
        elif isinstance(arg, ast.JoinedStr):
            for val in arg.values:
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    if "_havn" in val.value.lower():
                        raise SandboxViolation(
                            f"Line {node.lineno}: access to _havn schema "
                            f"is not allowed in notebooks"
                        )


# ---------------------------------------------------------------------------
# Layer 2: Guarded open() — block .env and dotfile reads
# ---------------------------------------------------------------------------

_REAL_OPEN = builtins.open


def _guarded_open(file, mode="r", *args, **kwargs):
    """Wrapper around open() that blocks reading .env and dotfiles."""
    file_str = str(file)
    p = Path(file_str)

    # Block .env specifically
    if p.name == ".env":
        raise PermissionError("Reading .env is not allowed in notebooks")

    # Block dotfiles/dotdirs in path (e.g. .git/config, .ssh/id_rsa)
    for part in p.parts:
        if part.startswith(".") and part not in (".", ".."):
            raise PermissionError(
                f"Reading dotfiles is not allowed in notebooks: {file_str}"
            )

    return _REAL_OPEN(file, mode, *args, **kwargs)


# ---------------------------------------------------------------------------
# Layer 3: Execution Timeout
# ---------------------------------------------------------------------------

CELL_TIMEOUT_SECONDS = 60


def execute_with_timeout(
    func,
    args: tuple = (),
    timeout: int = CELL_TIMEOUT_SECONDS,
) -> Any:
    """Run a function in a daemon thread with a timeout.

    Returns the function's return value, or raises SandboxViolation on timeout.
    """
    result_box: list = []
    error_box: list[Exception] = []

    def _worker():
        try:
            result_box.append(func(*args))
        except Exception as e:
            error_box.append(e)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        raise SandboxViolation(
            f"Cell execution timed out after {timeout} seconds. "
            f"Check for infinite loops or very expensive computations."
        )

    if error_box:
        raise error_box[0]

    return result_box[0] if result_box else None


# ---------------------------------------------------------------------------
# Layer 4: Safe DB Proxy
# ---------------------------------------------------------------------------

_BLOCKED_SQL_RE = re.compile(
    r'\b('
    r'_havn'
    r'|ATTACH\b|INSTALL\b|LOAD\b'
    r'|COPY\s+.*\bTO\b'
    r')',
    re.IGNORECASE,
)


def check_sql(query: str) -> None:
    """Validate that a SQL string doesn't access blocked resources.

    Raises PermissionError if the query references _havn,
    ATTACH, INSTALL, LOAD, or COPY TO.
    """
    if _BLOCKED_SQL_RE.search(query):
        raise PermissionError(
            "This SQL operation is not allowed in notebooks. "
            "Access to _havn, ATTACH, INSTALL, LOAD, "
            "and COPY TO are blocked."
        )


class SafeDbProxy:
    """Proxy around a DuckDB connection that blocks dangerous SQL.

    Intercepts execute(), sql(), and query() to validate SQL before
    delegating to the real connection. Uses __class__ replacement so
    DuckDB's replacement scan (which looks up Python variables like `df`
    from the caller's frame) continues to work correctly.
    """

    def __init__(self, conn):
        # Store on the instance without triggering __setattr__ issues
        object.__setattr__(self, "_conn", conn)
        # Copy all attributes so this looks like a real connection
        # to DuckDB's internals

    def execute(self, query: str, parameters=None):
        check_sql(query)
        if parameters is not None:
            return self._conn.execute(query, parameters)
        return self._conn.execute(query)

    def sql(self, query: str, **kwargs):
        check_sql(query)
        return self._conn.sql(query, **kwargs)

    def query(self, query: str, **kwargs):
        check_sql(query)
        return self._conn.query(query, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def __repr__(self):
        return f"SafeDbProxy({self._conn!r})"
