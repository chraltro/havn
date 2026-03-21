"""Python script runner for ingest and export scripts.

Scripts can be:
- .py files with top-level code (db connection is pre-injected)
- .py files with a legacy run(db) function (backward compatible)
- .dpnb notebooks (executed cell-by-cell)
"""

from __future__ import annotations

import ast
import importlib.util
import io
import logging
import signal
import sys
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import duckdb
from rich.console import Console

from havn.engine.database import ensure_meta_table, log_run

console = Console()
logger = logging.getLogger("havn.runner")

# Lazy import to avoid circular deps; actual instance lives in circuit_breaker module
_circuit_breaker = None


def _get_circuit_breaker():
    """Return the default CircuitBreaker instance (lazy import)."""
    global _circuit_breaker
    if _circuit_breaker is None:
        from havn.engine.circuit_breaker import default_breaker
        _circuit_breaker = default_breaker
    return _circuit_breaker

# Hard timeout: absolute max execution time (2 hours)
SCRIPT_TIMEOUT_SECONDS = 7200
# Idle timeout: if no DuckDB activity AND no stdout for this long, assume stuck
SCRIPT_IDLE_TIMEOUT_SECONDS = 120


class ScriptTimeoutError(Exception):
    """Raised when a script exceeds the execution timeout."""


def _has_run_function(source: str) -> bool:
    """Check if Python source defines a top-level run() function."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    return any(
        isinstance(node, ast.FunctionDef) and node.name == "run"
        for node in tree.body
    )


def _load_module(script_path: Path):
    """Dynamically load a Python module from a file path."""
    spec = importlib.util.spec_from_file_location(script_path.stem, script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_notebook_as_script(
    conn: duckdb.DuckDBPyConnection,
    notebook_path: Path,
) -> dict:
    """Run a .dpnb notebook as a pipeline script.

    Captures per-cell timing and output, detects output tables for DAG
    integration, and returns detailed execution results.
    """
    from havn.engine.notebook import extract_notebook_outputs, load_notebook, run_notebook

    notebook = load_notebook(notebook_path)

    # Resolve project dir from notebook path for ingest cell support
    project_dir = None
    for parent in notebook_path.parents:
        if (parent / "project.yml").exists():
            project_dir = parent
            break

    result_nb = run_notebook(conn, notebook, project_dir=project_dir)

    # Collect per-cell results
    cell_results = result_nb.get("cell_results", [])

    # Check cells for errors
    errors = []
    for cell in result_nb.get("cells", []):
        for output in cell.get("outputs", []):
            if output.get("type") == "error":
                errors.append(output.get("text", ""))

    duration_ms = result_nb.get("last_run_ms", 0)

    # Extract output table declarations
    output_tables = extract_notebook_outputs(notebook)

    # Count rows from output tables
    rows_affected = 0
    for table in output_tables:
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            rows_affected += count
        except Exception as e:
            logger.debug("Could not get result metadata: %s", e)

    if errors:
        error_msg = "\n".join(errors)
        return {
            "script": notebook_path.name,
            "status": "error",
            "duration_ms": duration_ms,
            "log_output": error_msg,
            "error": error_msg,
            "cell_results": cell_results,
            "output_tables": output_tables,
        }

    return {
        "script": notebook_path.name,
        "status": "success",
        "duration_ms": duration_ms,
        "log_output": "",
        "error": None,
        "rows_affected": rows_affected,
        "cell_results": cell_results,
        "output_tables": output_tables,
    }


def run_script(
    conn: duckdb.DuckDBPyConnection,
    script_path: Path,
    script_type: str = "ingest",
    timeout: int = SCRIPT_TIMEOUT_SECONDS,
    use_circuit_breaker: bool = True,
    pipeline_run_id: str | None = None,
) -> dict:
    """Run a single script (.py or .dpnb).

    Args:
        conn: DuckDB connection
        script_path: Path to the .py or .dpnb file
        script_type: "ingest" or "export" (for logging)
        timeout: Maximum execution time in seconds
        use_circuit_breaker: If True, wrap execution with the default circuit breaker
        pipeline_run_id: Shared ID grouping all executions in a pipeline run

    Returns:
        Dict with keys: script, status, duration_ms, log_output, error
    """
    ensure_meta_table(conn)

    # --- Circuit breaker guard ---
    if use_circuit_breaker:
        from havn.engine.circuit_breaker import CircuitOpenError
        breaker = _get_circuit_breaker()
        circuit_name = script_path.name
        try:
            state = breaker.get_state(circuit_name)
        except Exception:
            state = None

        if state is not None:
            from havn.engine.circuit_breaker import CircuitState
            if state == CircuitState.OPEN:
                msg = f"Circuit breaker OPEN for '{circuit_name}' — skipping execution"
                console.print(f"  [yellow]circuit open[/yellow] [bold]{circuit_name}[/bold] — skipped")
                logger.warning(msg)
                return {
                    "script": script_path.name,
                    "status": "skipped",
                    "duration_ms": 0,
                    "log_output": msg,
                    "error": msg,
                }

    if not script_path.exists():
        raise FileNotFoundError(f"Script not found: {script_path}")

    label = f"[bold]{script_path.name}[/bold]"
    console.print(f"  [blue]run [/blue] {label}")

    # Dispatch .dpnb notebooks
    if script_path.suffix == ".dpnb":
        start = time.perf_counter()
        try:
            result = _run_notebook_as_script(conn, script_path)
            duration_ms = result["duration_ms"]
            log_run(
                conn, script_type, script_path.name,
                result["status"], duration_ms,
                error=result["error"],
                log_output=result["log_output"] or None,
                pipeline_run_id=pipeline_run_id,
            )
            if result["status"] == "success":
                console.print(f"  [green]done[/green] {label} ({duration_ms}ms)")
                if use_circuit_breaker:
                    _get_circuit_breaker()._record_success(script_path.name)
            else:
                console.print(f"  [red]fail[/red] {label}: {result['error']}")
                if use_circuit_breaker:
                    _get_circuit_breaker()._record_failure(script_path.name)
            return result
        except Exception as e:
            duration_ms = int((time.perf_counter() - start) * 1000)
            error_msg = traceback.format_exc()
            log_run(conn, script_type, script_path.name, "error", duration_ms, error=str(e), log_output=error_msg, pipeline_run_id=pipeline_run_id)
            console.print(f"  [red]fail[/red] {label}: {e}")
            if use_circuit_breaker:
                _get_circuit_breaker()._record_failure(script_path.name)
            return {"script": script_path.name, "status": "error", "duration_ms": duration_ms, "log_output": error_msg, "error": str(e)}

    # .py scripts
    source = script_path.read_text()
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    start = time.perf_counter()

    # Run in a thread with timeout to prevent scripts from hanging indefinitely
    exec_error: list[Exception] = []

    def _execute():
        try:
            if _has_run_function(source):
                # Legacy mode: import module and call run(db)
                module = _load_module(script_path)
                with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                    module.run(conn)
            else:
                # New mode: exec top-level code with db pre-injected
                namespace = {
                    "db": conn,
                    "__file__": str(script_path),
                    "__name__": script_path.stem,
                    "__builtins__": __builtins__,
                }
                try:
                    import pandas as pd
                    namespace["pd"] = pd
                except ImportError:
                    pass
                with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                    exec(compile(source, str(script_path), "exec"), namespace)
        except Exception as e:
            exec_error.append(e)

    thread = threading.Thread(target=_execute, daemon=True)
    thread.start()

    # Poll for completion with activity-based idle detection.
    # Instead of a single hard timeout, we check:
    # 1. Is the thread still alive?
    # 2. Is DuckDB actively running a query? (via duckdb_queries())
    # 3. Is the script producing stdout output?
    # If none of these show activity for IDLE_TIMEOUT seconds, kill it.
    idle_timeout = SCRIPT_IDLE_TIMEOUT_SECONDS
    last_activity = time.perf_counter()
    last_stdout_len = 0
    timed_out = False
    idle_killed = False

    while thread.is_alive():
        thread.join(timeout=5)  # check every 5 seconds
        if not thread.is_alive():
            break

        elapsed = time.perf_counter() - start
        now = time.perf_counter()

        # Hard timeout
        if elapsed >= timeout:
            timed_out = True
            break

        # Check for activity
        has_activity = False

        # 1. Check DuckDB for running queries
        try:
            running_queries = conn.execute(
                "SELECT count(*) FROM duckdb_queries() WHERE success IS NULL"
            ).fetchone()[0]
            if running_queries > 0:
                has_activity = True
        except Exception:
            # duckdb_queries() may not be available in all versions
            has_activity = True  # assume active if we can't check

        # 2. Check for new stdout output
        current_stdout_len = len(stdout_capture.getvalue())
        if current_stdout_len > last_stdout_len:
            has_activity = True
            last_stdout_len = current_stdout_len

        if has_activity:
            last_activity = now
        elif now - last_activity > idle_timeout:
            idle_killed = True
            break

    if thread.is_alive():
        duration_ms = int((time.perf_counter() - start) * 1000)
        if idle_killed:
            error_msg = f"Script appears stuck — no DuckDB activity or output for {idle_timeout}s (total {duration_ms // 1000}s elapsed)"
        else:
            error_msg = f"Script timed out after {timeout}s"
        log_output = stdout_capture.getvalue() + stderr_capture.getvalue()
        logger.warning("Script %s: %s", script_path.name, error_msg)
        log_run(conn, script_type, script_path.name, "error", duration_ms, error=error_msg, log_output=log_output or None, pipeline_run_id=pipeline_run_id)
        console.print(f"  [red]timeout[/red] {label}: {error_msg}")
        if use_circuit_breaker:
            _get_circuit_breaker()._record_failure(script_path.name)
        return {"script": script_path.name, "status": "error", "duration_ms": duration_ms, "log_output": log_output, "error": error_msg}

    if exec_error:
        e = exec_error[0]
        duration_ms = int((time.perf_counter() - start) * 1000)
        error_msg = traceback.format_exception(type(e), e, e.__traceback__)
        error_str = "".join(error_msg)
        log_output = stdout_capture.getvalue() + stderr_capture.getvalue() + "\n" + error_str

        log_run(conn, script_type, script_path.name, "error", duration_ms, error=str(e), log_output=log_output, pipeline_run_id=pipeline_run_id)
        console.print(f"  [red]fail[/red] {label}: {e}")

        if use_circuit_breaker:
            _get_circuit_breaker()._record_failure(script_path.name)
        return {"script": script_path.name, "status": "error", "duration_ms": duration_ms, "log_output": log_output, "error": str(e)}

    duration_ms = int((time.perf_counter() - start) * 1000)
    log_output = stdout_capture.getvalue() + stderr_capture.getvalue()

    # Try to extract row count from log output (e.g., "Loaded 42 rows" or "42 rows")
    rows_affected = _extract_row_count(log_output)

    log_run(conn, script_type, script_path.name, "success", duration_ms, rows_affected=rows_affected, log_output=log_output or None, pipeline_run_id=pipeline_run_id)
    rows_msg = f", {rows_affected} rows" if rows_affected else ""
    console.print(f"  [green]done[/green] {label} ({duration_ms}ms{rows_msg})")

    if use_circuit_breaker:
        _get_circuit_breaker()._record_success(script_path.name)
    return {"script": script_path.name, "status": "success", "duration_ms": duration_ms, "log_output": log_output, "error": None, "rows_affected": rows_affected}


def _extract_row_count(output: str) -> int:
    """Extract row count from script output by matching common patterns.

    Matches patterns like:
    - "Loaded 42 rows"
    - "Exported 100 rows"
    - "42 rows"
    - "Got 15 earthquakes"
    """
    import re
    # Look for patterns: "<number> rows/records/entries" or "Loaded/Exported/Inserted <number>"
    patterns = [
        r"(?:loaded|exported|inserted|imported|fetched|got|wrote)\s+(\d+)",
        r"(\d+)\s+(?:rows?|records?|entries|earthquakes|items?)",
    ]
    total = 0
    for pattern in patterns:
        for match in re.finditer(pattern, output, re.IGNORECASE):
            n = int(match.group(1))
            if n > total:
                total = n
    return total


def run_scripts_in_dir(
    conn: duckdb.DuckDBPyConnection,
    scripts_dir: Path,
    script_type: str = "ingest",
    targets: list[str] | None = None,
    pipeline_run_id: str | None = None,
) -> list[dict]:
    """Run all scripts in a directory (or specific targets).

    Args:
        conn: DuckDB connection
        scripts_dir: Directory containing .py/.dpnb scripts
        script_type: "ingest" or "export"
        targets: Specific script names (without extension), or None for all
        pipeline_run_id: Shared ID grouping all executions in a pipeline run

    Returns:
        List of result dicts from run_script
    """
    if not scripts_dir.exists():
        console.print(f"[yellow]No {script_type}/ directory found[/yellow]")
        return []

    py_scripts = list(scripts_dir.glob("*.py"))
    nb_scripts = list(scripts_dir.glob("*.dpnb"))
    scripts = sorted(py_scripts + nb_scripts, key=lambda p: p.name)

    if targets and targets != ["all"]:
        target_set = {t.removesuffix(".py").removesuffix(".dpnb") for t in targets}
        scripts = [s for s in scripts if s.stem in target_set]

    if not scripts:
        console.print(f"[yellow]No {script_type} scripts found[/yellow]")
        return []

    results = []
    for script in scripts:
        if script.name.startswith("_"):
            continue
        result = run_script(conn, script, script_type, pipeline_run_id=pipeline_run_id)
        results.append(result)
        # Stop on error for ingest (data integrity)
        if script_type == "ingest" and result["status"] == "error":
            console.print("[red]Stopping: ingest script failed[/red]")
            break

    return results
