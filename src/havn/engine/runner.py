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
# Poll interval between activity checks while a script is running. Exposed as
# a module-level constant so tests can shorten it.
SCRIPT_POLL_INTERVAL_SECONDS = 5


class ScriptTimeoutError(Exception):
    """Raised when a script exceeds the execution timeout."""


_PRAGMA_RE = None  # lazily compiled in _parse_pragma


def _parse_pragma(source: str) -> dict[str, str]:
    """Parse `# @havn: key=value [key=value ...]` directives from the top of a
    script. Only the first 30 non-blank lines are scanned, and scanning stops
    at the first non-comment, non-docstring line.

    Returns a dict like ``{"schedule": "once"}``. Unknown keys are kept so the
    caller can decide how to handle them (and so we don't error on typos).
    """
    global _PRAGMA_RE
    if _PRAGMA_RE is None:
        import re
        # Match: optional leading whitespace, '#', whitespace, '@havn:', then
        # the directive body. Body is "key=value" pairs separated by whitespace
        # or commas.
        _PRAGMA_RE = re.compile(r"^\s*#\s*@havn\s*:\s*(.+?)\s*$")

    pragmas: dict[str, str] = {}
    inside_docstring = False
    docstring_quote: str | None = None

    for i, line in enumerate(source.splitlines()):
        if i > 30:
            break
        stripped = line.strip()

        # Track triple-quoted module docstring so we don't try to parse pragmas
        # from inside it (and so it doesn't end our scan early).
        if not inside_docstring:
            handled_docstring = False
            for q in ('"""', "'''"):
                if stripped.startswith(q):
                    rest = stripped[3:]
                    if q in rest:
                        # Single-line docstring like '"""hello"""'
                        handled_docstring = True
                    else:
                        inside_docstring = True
                        docstring_quote = q
                        handled_docstring = True
                    break
            if handled_docstring:
                continue
        else:
            if docstring_quote and docstring_quote in stripped:
                inside_docstring = False
                docstring_quote = None
            continue

        if not stripped:
            continue
        if stripped.startswith("#"):
            m = _PRAGMA_RE.match(line)
            if m:
                body = m.group(1)
                # Split on commas or whitespace
                import re as _re
                for part in _re.split(r"[,\s]+", body):
                    if "=" not in part:
                        continue
                    k, _, v = part.partition("=")
                    k = k.strip().lower()
                    v = v.strip()
                    if k:
                        pragmas[k] = v
            continue

        # First non-comment, non-blank, non-docstring line: stop scanning.
        # Pragmas only live at the top of the file.
        break

    return pragmas


def _has_prior_success(conn: duckdb.DuckDBPyConnection, target: str) -> bool:
    """True if `_havn.run_log` has a prior successful run for this target."""
    try:
        row = conn.execute(
            "SELECT 1 FROM _havn.run_log WHERE target = ? AND status = 'success' LIMIT 1",
            [target],
        ).fetchone()
        return row is not None
    except Exception:
        # Table may not exist yet on first run, or backend may not support it.
        return False


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

    # Build log_output from cell results so Job Results can show it
    log_lines: list[str] = []
    for cr in cell_results:
        cell_type = cr.get("type", "")
        cell_id = cr.get("cell_id", "")
        cell_dur = cr.get("duration_ms", 0)
        cell_label = f"[{cell_type}] {cell_id}" if cell_id else f"[{cell_type}]"
        for out in cr.get("outputs", []):
            text = out.get("text", "").strip()
            if text:
                log_lines.append(f"{cell_label}: {text}")
        if not any(out.get("text", "").strip() for out in cr.get("outputs", [])):
            if cr.get("has_error"):
                log_lines.append(f"{cell_label}: (error, {cell_dur}ms)")
            elif cell_dur:
                log_lines.append(f"{cell_label}: ok ({cell_dur}ms)")
    log_output = "\n".join(log_lines)

    if errors:
        error_msg = "\n".join(errors)
        return {
            "script": notebook_path.name,
            "status": "error",
            "duration_ms": duration_ms,
            "log_output": error_msg + ("\n" + log_output if log_output else ""),
            "error": error_msg,
            "cell_results": cell_results,
            "output_tables": output_tables,
        }

    return {
        "script": notebook_path.name,
        "status": "success",
        "duration_ms": duration_ms,
        "log_output": log_output,
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
    force: bool = False,
) -> dict:
    """Run a single script (.py or .dpnb).

    Every script execution is acquired against the ResourceManager so it
    shows up in the UI's Active Tasks list and can be cancelled. The
    category is picked from ``script_type``:

    - ``ingest`` → ``streaming`` (bringing data in)
    - ``export`` → ``system``    (writing data out / side-effects)
    - anything else → ``system``

    Scripts may declare a top-of-file pragma to control re-run behavior::

        # @havn: schedule=once

    With ``schedule=once`` the script is skipped on subsequent runs unless
    ``force=True`` is passed (e.g. via the ``--force`` CLI flag or the
    "Force run" UI button). The check looks for any prior ``success`` row
    in ``_havn.run_log`` for the same target filename.

    Args:
        conn: DuckDB connection
        script_path: Path to the .py or .dpnb file
        script_type: "ingest" or "export" (for logging)
        timeout: Maximum execution time in seconds
        use_circuit_breaker: If True, wrap execution with the default circuit breaker
        pipeline_run_id: Shared ID grouping all executions in a pipeline run
        force: If True, bypass the ``schedule=once`` skip check.

    Returns:
        Dict with keys: script, status, duration_ms, log_output, error
    """
    from havn.engine.resource_manager import current_task, get_resource_manager

    category = "streaming" if script_type == "ingest" else "system"
    manager = get_resource_manager()
    label = f"{script_type}:{script_path.name}"
    with manager.acquire_sync(category, label, conn=conn):
        task = current_task()
        if task is not None:
            manager.register_cancel(task.task_id, conn.interrupt)
        return _run_script_body(
            conn,
            script_path,
            script_type=script_type,
            timeout=timeout,
            use_circuit_breaker=use_circuit_breaker,
            pipeline_run_id=pipeline_run_id,
            force=force,
        )


def _run_script_body(
    conn: duckdb.DuckDBPyConnection,
    script_path: Path,
    script_type: str = "ingest",
    timeout: int = SCRIPT_TIMEOUT_SECONDS,
    use_circuit_breaker: bool = True,
    pipeline_run_id: str | None = None,
    force: bool = False,
) -> dict:
    """Inner implementation — unchanged script-execution logic."""
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

    # --- Schedule pragma (e.g. `# @havn: schedule=once`) ---
    # Read the file once and stash it so we can reuse it as the script body
    # below (avoids a second disk read).
    try:
        _source_cache = script_path.read_text()
    except Exception:
        _source_cache = ""

    pragmas = _parse_pragma(_source_cache)
    schedule = pragmas.get("schedule", "always").lower()
    if schedule == "once" and not force and _has_prior_success(conn, script_path.name):
        msg = (
            f"schedule=once and {script_path.name} has already succeeded. "
            "Pass force=True (CLI: --force, UI: Force run) to re-run."
        )
        console.print(
            f"  [yellow]skip[/yellow] [bold]{script_path.name}[/bold] "
            "(schedule=once, already loaded)"
        )
        logger.info("Skipping %s: schedule=once and prior success exists", script_path.name)
        return {
            "script": script_path.name,
            "status": "skipped",
            "duration_ms": 0,
            "log_output": msg,
            "error": None,
        }

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

    # .py scripts (reuse the source we already read for pragma parsing)
    source = _source_cache or script_path.read_text()
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
    #
    # The duckdb_queries() probe MUST run on a separate connection. DuckDB
    # connections are not thread-safe: probing on `conn` while the script
    # thread is also using `conn` clobbers cursor state, so the script's
    # next fetchone() returns None. cursor() gives us an independent
    # connection over the same database.
    try:
        probe_conn = conn.cursor()
    except Exception:
        probe_conn = None

    idle_timeout = SCRIPT_IDLE_TIMEOUT_SECONDS
    last_activity = time.perf_counter()
    last_stdout_len = 0
    timed_out = False
    idle_killed = False

    while thread.is_alive():
        thread.join(timeout=SCRIPT_POLL_INTERVAL_SECONDS)
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

        # 1. Check DuckDB for running queries (on a separate connection)
        if probe_conn is not None:
            try:
                running_queries = probe_conn.execute(
                    "SELECT count(*) FROM duckdb_queries() WHERE success IS NULL"
                ).fetchone()[0]
                if running_queries > 0:
                    has_activity = True
            except Exception:
                # duckdb_queries() may not be available in all versions
                has_activity = True  # assume active if we can't check
        else:
            has_activity = True

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

    if probe_conn is not None:
        try:
            probe_conn.close()
        except Exception:
            pass

    if thread.is_alive():
        try:
            conn.interrupt()
        except Exception:
            logger.debug("Script %s: conn.interrupt() failed", script_path.name, exc_info=True)
        thread.join(timeout=5)
        duration_ms = int((time.perf_counter() - start) * 1000)
        if idle_killed:
            error_msg = f"Script appears stuck. No DuckDB activity or output for {idle_timeout}s (total {duration_ms // 1000}s elapsed)"
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

        from havn.engine.deps import augment_import_error
        log_output = augment_import_error(log_output, e)
        error_summary = augment_import_error(str(e), e)

        log_run(conn, script_type, script_path.name, "error", duration_ms, error=error_summary, log_output=log_output, pipeline_run_id=pipeline_run_id)
        console.print(f"  [red]fail[/red] {label}: {error_summary}")

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
    - "Loaded 1,234 rows"      (comma thousand-separators tolerated)
    - "Loaded 1_234 rows"      (underscore thousand-separators tolerated)
    - "Exported 100 rows"
    - "42 rows"
    - "Got 15 earthquakes"

    Excludes byte counts (e.g. "Downloaded 1048576 bytes").
    """
    import re
    # Number pattern: leading digit, then any mix of digits / commas / underscores.
    # Lets us match "2,616,838" or "1_234_567" without splitting at the separator.
    NUM = r"\d[\d,_]*"
    patterns = [
        rf"(?<!\w)(?:loaded|exported|inserted|imported|fetched|got|wrote)\s+({NUM})",
        rf"({NUM})\s+(?:rows?|records?|entries|earthquakes|items?)\b",
    ]
    total = 0
    for pattern in patterns:
        for match in re.finditer(pattern, output, re.IGNORECASE):
            raw = match.group(1).replace(",", "").replace("_", "")
            try:
                n = int(raw)
            except ValueError:
                continue
            if n > total:
                total = n
    return total


def run_scripts_in_dir(
    conn: duckdb.DuckDBPyConnection,
    scripts_dir: Path,
    script_type: str = "ingest",
    targets: list[str] | None = None,
    pipeline_run_id: str | None = None,
    force: bool = False,
) -> list[dict]:
    """Run all scripts in a directory (or specific targets).

    Args:
        conn: DuckDB connection
        scripts_dir: Directory containing .py/.dpnb scripts
        script_type: "ingest" or "export"
        targets: Specific script names (without extension), or None for all
        pipeline_run_id: Shared ID grouping all executions in a pipeline run
        force: Bypass `schedule=once` skip in individual scripts.

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
        result = run_script(conn, script, script_type, pipeline_run_id=pipeline_run_id, force=force)
        results.append(result)
        # Stop on error for ingest (data integrity)
        if script_type == "ingest" and result["status"] == "error":
            console.print("[red]Stopping: ingest script failed[/red]")
            break

    return results
