"""Python code cell execution for notebooks."""

from __future__ import annotations

import ast
import io
import logging
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

import duckdb

from .formatting import _format_result
from .sandbox import (
    CELL_TIMEOUT_SECONDS,
    SQL_GUARD_NAME,
    SandboxViolation,
    _guarded_open,
    execute_with_timeout,
    guard_sql_calls,
    sql_guard,
    validate_ast,
)

logger = logging.getLogger("havn.notebook")


def execute_cell(
    conn: duckdb.DuckDBPyConnection,
    source: str,
    namespace: dict[str, Any] | None = None,
    *,
    sandboxed: bool = True,
    timeout: int = CELL_TIMEOUT_SECONDS,
) -> dict:
    """Execute a single Python code cell.

    Parameters
    ----------
    conn : DuckDB connection (SQL is guarded via AST rewriting when sandboxed)
    source : Python source code to execute
    namespace : shared namespace for variable persistence between cells
    sandboxed : if True, apply all sandbox restrictions (default True)
    timeout : max execution time in seconds (0 = no timeout)

    Returns dict with:
        - outputs: list of output items (text, table, error)
        - namespace: updated namespace for subsequent cells
        - duration_ms: execution time
    """
    if namespace is None:
        namespace = {}

    start = time.perf_counter()

    # --- Layer 1: AST validation ---
    if sandboxed:
        try:
            validate_ast(source)
        except SandboxViolation as e:
            return {
                "outputs": [{"type": "error", "text": f"Sandbox: {e}"}],
                "namespace": namespace,
                "duration_ms": int((time.perf_counter() - start) * 1000),
            }

    # Inject the real connection — DuckDB's replacement scan needs the real
    # object to find Python variables (like DataFrames) in the caller's frame.
    # SQL is validated at runtime by the __havn_sql_guard wrapper that
    # guard_sql_calls() injects around every .execute/.sql/.query argument.
    namespace["db"] = conn
    if sandboxed:
        namespace[SQL_GUARD_NAME] = sql_guard

    # Full builtins, but with open() replaced by guarded version
    namespace["__builtins__"] = __builtins__
    if sandboxed:
        # Overlay the guarded open onto builtins for this namespace
        if isinstance(namespace["__builtins__"], dict):
            namespace["__builtins__"] = {**namespace["__builtins__"], "open": _guarded_open}
        else:
            # __builtins__ is a module in the main module, dict elsewhere
            import builtins as _b
            ns_builtins = {k: getattr(_b, k) for k in dir(_b) if not k.startswith("_")}
            ns_builtins.update({k: getattr(_b, k) for k in dir(_b) if k.startswith("_")})
            ns_builtins["open"] = _guarded_open
            namespace["__builtins__"] = ns_builtins

    # Try importing pandas for convenience
    try:
        import pandas as pd
        namespace["pd"] = pd
    except ImportError:
        pass

    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    outputs: list[dict] = []

    def _run_cell():
        """Inner function executed (possibly with timeout)."""
        nonlocal outputs
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            tree = ast.parse(source)
            if sandboxed:
                # Wrap every .execute/.sql/.query argument in the runtime SQL
                # guard so blocked SQL is caught regardless of how `db` is
                # aliased, while keeping the call direct (replacement scan).
                tree = guard_sql_calls(tree)
            last_expr_value = None

            if tree.body and isinstance(tree.body[-1], ast.Expr):
                last_expr_node = tree.body.pop()
                if tree.body:
                    exec(compile(tree, "<cell>", "exec"), namespace)
                expr_code = compile(
                    ast.Expression(last_expr_node.value), "<cell>", "eval"
                )
                last_expr_value = eval(expr_code, namespace)
            else:
                exec(compile(tree, "<cell>", "exec"), namespace)

            if last_expr_value is not None:
                outputs.append(_format_result(last_expr_value))

    try:
        # --- Timeout ---
        if sandboxed and timeout > 0:
            execute_with_timeout(_run_cell, timeout=timeout)
        else:
            _run_cell()

        # Capture stdout
        stdout_text = stdout_capture.getvalue()
        if stdout_text:
            outputs.insert(0, {"type": "text", "text": stdout_text})

        stderr_text = stderr_capture.getvalue()
        if stderr_text:
            outputs.append({"type": "text", "text": stderr_text})

    except SandboxViolation as e:
        outputs.append({"type": "error", "text": f"Sandbox: {e}"})
    except Exception as e:
        error_text = traceback.format_exc()
        from havn.engine.deps import augment_import_error
        error_text = augment_import_error(error_text, e)
        outputs.append({"type": "error", "text": error_text})
    duration_ms = int((time.perf_counter() - start) * 1000)
    return {"outputs": outputs, "namespace": namespace, "duration_ms": duration_ms}
