"""Python script runner for ingest and export scripts.

Scripts can be:
- .py files with top-level code (db connection is pre-injected)
- .py files with a legacy run(db) function (backward compatible)
- .dpnb notebooks (executed cell-by-cell)
"""

from __future__ import annotations

from havn.textio import read_project_text

import ast
import importlib.util
import io
import logging
import sys
import threading
import time
import traceback
from collections.abc import Callable
from contextlib import contextmanager
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
# Idle timeout: no output and no CPU use for this long (30 minutes) means
# stuck. Generous, because a script waiting on a slow remote source looks
# exactly like this; a script can change it with `# @havn: idle_timeout=N`.
SCRIPT_IDLE_TIMEOUT_SECONDS = 1800
# Poll interval between activity checks while a script is running. Exposed as
# a module-level constant so tests can shorten it.
SCRIPT_POLL_INTERVAL_SECONDS = 5
# After a timeout, how long to keep trying to stop the script thread
# (interrupting DuckDB and raising in the thread) before giving up on it.
SCRIPT_STOP_GRACE_SECONDS = 5


class ScriptTimeoutError(Exception):
    """Raised when a script exceeds the execution timeout."""


class _ScriptStop(BaseException):
    """Raised inside a timed-out script thread to stop it.

    A BaseException, so the ``except Exception:`` of a script's retry loop
    does not swallow it.
    """


# Script threads that timed out and could not be stopped, keyed by id() of
# the connection they hold. The thread keeps a reference to the connection,
# so the id cannot be reused while it is alive. A later run_script on the
# same connection refuses to start rather than race the orphan on a
# connection that is not thread-safe.
_orphaned: dict[int, tuple[list[threading.Thread], str]] = {}
_orphaned_lock = threading.Lock()


def orphaned_script(conn) -> str | None:
    """Name of a timed-out script still running on ``conn``, if any."""
    with _orphaned_lock:
        entry = _orphaned.get(id(conn))
        if entry is None:
            return None
        threads, name = entry
        if any(t.is_alive() for t in threads):
            return name
        del _orphaned[id(conn)]
        return None


def _raise_in_thread(thread: threading.Thread, exc_type: type[BaseException]) -> bool:
    """Ask the interpreter to raise ``exc_type`` in ``thread``.

    Takes effect at the thread's next bytecode, so it stops a Python loop
    but not a blocking C call (a long sleep, a socket read); DuckDB queries
    are covered separately by ``conn.interrupt()``. Returns True once the
    exception is pending.
    """
    import ctypes

    ident = thread.ident
    if ident is None or _is_importing(ident):
        return False
    n = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(ident), ctypes.py_object(exc_type)
    )
    if n > 1:  # should not happen; undo rather than hit other threads
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(ident), None)
        return False
    return n == 1


def _is_importing(ident: int) -> bool:
    """True if the thread is inside the import machinery right now.

    An exception raised mid-import leaves a half-initialised module in
    sys.modules ("partially initialized module 'pandas'") that breaks every
    later script in the process, so the stop is deferred to the next poll.
    """
    frame = sys._current_frames().get(ident)
    while frame is not None:
        if frame.f_code.co_filename.startswith("<frozen importlib"):
            return True
        frame = frame.f_back
    return False


def _stop_threads(conn, thread: threading.Thread, extra: list[threading.Thread], name: str) -> bool:
    """Try to stop a timed-out script. True once every thread has exited.

    The stop exception is raised in ``thread`` once (deferred while it is
    importing); raising it again could land in cleanup code, such as
    a context manager's ``__exit__``, and leave process state half restored.
    After that only DuckDB is interrupted, repeatedly, since a script may
    run several queries on its way out. ``extra`` are threads the script
    started (a notebook's sandboxed cells) that may also use ``conn``; they
    cannot be raised into safely and are only waited for.
    """
    deadline = time.perf_counter() + SCRIPT_STOP_GRACE_SECONDS
    raised = False
    while True:
        alive = [t for t in [thread, *extra] if t.is_alive()]
        if not alive or time.perf_counter() >= deadline:
            return not alive
        try:
            conn.interrupt()
        except Exception:
            logger.debug("Script %s: conn.interrupt() failed", name, exc_info=True)
        if not raised and thread.is_alive():
            raised = _raise_in_thread(thread, _ScriptStop)
        alive[0].join(timeout=min(0.25, max(0.0, deadline - time.perf_counter())))


class _StreamRouter:
    """A sys.stdout/sys.stderr stand-in that sends each thread's writes to
    that thread's buffer, and everyone else's to the stream it replaced.

    ``redirect_stdout`` swaps the process-wide stream, so a script that
    timed out and kept running held every other thread's output (CLI
    progress, server logs) in its buffer, possibly for good. The router is
    installed while a script runs and removed when the last one finishes.
    Threads a script starts itself write to the real stream.
    """

    def __init__(self, target):
        self.target = target
        self.routes: dict[int, io.StringIO] = {}

    def _out(self):
        return self.routes.get(threading.get_ident(), self.target)

    def write(self, s):
        return self._out().write(s)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        try:
            self._out().flush()
        except Exception:
            pass

    def __getattr__(self, name):  # encoding, isatty, fileno, ...
        return getattr(self.target, name)


_router_lock = threading.Lock()


@contextmanager
def _capture_thread_output(stdout_buf: io.StringIO, stderr_buf: io.StringIO):
    """Route the calling thread's stdout/stderr into the given buffers."""
    ident = threading.get_ident()
    attached: list[tuple[str, _StreamRouter]] = []
    with _router_lock:
        for attr, buf in (("stdout", stdout_buf), ("stderr", stderr_buf)):
            current = getattr(sys, attr)
            if not isinstance(current, _StreamRouter):
                current = _StreamRouter(current)
                setattr(sys, attr, current)
            current.routes[ident] = buf
            attached.append((attr, current))
    try:
        yield
    finally:
        with _router_lock:
            for attr, router in attached:
                router.routes.pop(ident, None)
                # Unwrap only if nobody replaced it meanwhile.
                if not router.routes and getattr(sys, attr) is router:
                    setattr(sys, attr, router.target)


class _BusyProbe:
    """Tells whether the process is doing work while a script runs.

    A running DuckDB query burns CPU (the calling thread executes tasks
    alongside DuckDB's workers), as does Python code; a stuck script, asleep
    or blocked on a socket, burns next to none. Busy means at least
    ``CPU_BUSY_FRACTION`` of a core over a window of ``CPU_WINDOW_SECONDS``.

    This replaces a ``duckdb_queries()`` probe, which DuckDB 1.5 does not
    have: it failed on every poll, the failure counted as activity, and the
    idle timeout never fired. ``conn.query_progress()`` was tried as well but
    is not usable: it stays at 0 for a result the script fetched only partly
    (a script that ran ``.fetchone()`` and then hung looked busy forever) and
    was seen at -1 for the whole of a running query.

    The figure is process-wide, so other work in a server process can only
    delay an idle kill, never cause one. It reads no connection state, so it
    cannot disturb the script's use of the connection.
    """

    CPU_BUSY_FRACTION = 0.05
    # process_time() ticks in ~16ms steps on Windows, so CPU is measured over
    # windows at least this long; shorter polls reuse the last verdict.
    CPU_WINDOW_SECONDS = 0.25

    def __init__(self, cpu_clock=time.process_time, wall_clock=time.perf_counter):
        self._cpu_clock, self._wall_clock = cpu_clock, wall_clock
        self._cpu = cpu_clock()
        self._wall = wall_clock()
        self._busy = True  # until the first full window has been measured

    def active(self) -> bool:
        cpu, wall = self._cpu_clock(), self._wall_clock()
        if wall - self._wall >= self.CPU_WINDOW_SECONDS:
            self._busy = (cpu - self._cpu) >= self.CPU_BUSY_FRACTION * (wall - self._wall)
            self._cpu, self._wall = cpu, wall
        return self._busy


def _run_supervised(
    conn,
    name: str,
    target: Callable[[], object],
    *,
    timeout: float,
    activity: Callable[[], object],
    stop_event: threading.Event | None = None,
    idle_timeout: float | None = None,
    track_child_threads: bool = False,
) -> dict:
    """Run ``target`` in a thread under the hard and idle timeouts.

    ``activity`` returns a token that changes whenever the script shows life
    (output length, cells started); so does the process using CPU, which
    covers a DuckDB query in flight (see ``_BusyProbe``).
    No sign of life for ``idle_timeout`` (default
    ``SCRIPT_IDLE_TIMEOUT_SECONDS``; 0 disables it) means stuck. A query
    that waits on the network burns little CPU, so a script that runs a long
    remote scan without printing should raise or disable its idle timeout
    (``# @havn: idle_timeout=0``).

    ``track_child_threads``: threads the target starts are treated as part
    of the script after a timeout (waited for, and kept in the orphan
    record), for notebooks, whose code cells run in a sandbox thread.

    Limitation: a script blocked in a C call that neither DuckDB's interrupt
    nor an injected exception reaches (``time.sleep(3600)``, a socket read
    with no timeout) cannot be killed from inside the process. It is
    reported as orphaned and left running; ``orphaned_script`` then stops
    later scripts from using the same connection.

    ``stop_event`` is set on timeout, for targets that can stop cooperatively
    (a notebook checks it between cells: its code cells run in their own
    sandbox thread, which an exception raised here cannot reach).

    Returns ``{"value", "error", "reason", "orphaned", "elapsed"}``, where
    ``reason`` is None, "timeout" or "idle".
    """
    box: dict = {}

    def _execute():
        try:
            box["value"] = target()
        except SystemExit as e:
            # sys.exit() / sys.exit(0) in a script is a clean finish.
            if e.code not in (None, 0):
                box["error"] = e
        except BaseException as e:  # includes the ScriptTimeoutError we inject
            box["error"] = e

    probe = _BusyProbe()
    streams_before = (sys.stdout, sys.stderr)
    before = set(threading.enumerate()) if track_child_threads else set()
    start = time.perf_counter()
    thread = threading.Thread(target=_execute, daemon=True, name=f"havn-script:{name}")
    thread.start()

    if idle_timeout is None:
        idle_timeout = SCRIPT_IDLE_TIMEOUT_SECONDS
    last_activity = start
    last_token = activity()
    reason: str | None = None
    while thread.is_alive():
        remaining = timeout - (time.perf_counter() - start)
        thread.join(timeout=max(0.0, min(SCRIPT_POLL_INTERVAL_SECONDS, remaining)))
        if not thread.is_alive():
            break
        now = time.perf_counter()
        if now - start >= timeout:
            reason = "timeout"
            break
        token = activity()
        if token != last_token or probe.active():
            last_token = token
            last_activity = now
        elif idle_timeout > 0 and now - last_activity > idle_timeout:
            reason = "idle"
            break

    orphaned = False
    if reason is not None:
        if stop_event is not None:
            stop_event.set()
        # Only the sandbox's cell threads (``notebook/sandbox.py`` starts
        # them with target ``_worker``, which Python puts in the default
        # thread name). Other threads that started meanwhile in a server
        # process are request workers that live forever; counting them
        # would block the connection for good.
        extra = [
            t for t in threading.enumerate()
            if t not in before and t is not thread and t.is_alive()
            and t.name.endswith("(_worker)")
        ] if track_child_threads else []
        orphaned = not _stop_threads(conn, thread, extra, name)
        if orphaned:
            # The thread still holds the connection: later scripts on it
            # are refused; see orphaned_script().
            with _orphaned_lock:
                _orphaned[id(conn)] = ([thread, *extra], name)
            # Code that swapped the process-wide streams (redirect_stdout in
            # a notebook cell) would keep everyone's output until it exits.
            for attr, saved in zip(("stdout", "stderr"), streams_before):
                current = getattr(sys, attr)
                if current is not saved and not isinstance(current, _StreamRouter):
                    setattr(sys, attr, saved)
            logger.error(
                "Script %s did not stop after timing out; it is still running "
                "and holds its DuckDB connection", name,
            )
    return {
        "value": box.get("value"),
        "error": box.get("error"),
        "reason": reason,
        "orphaned": orphaned,
        "elapsed": time.perf_counter() - start,
        "idle_timeout": idle_timeout,
    }


def _timeout_message(sup: dict, timeout: float, notebook: bool = False) -> str:
    total = int(sup["elapsed"])
    if sup["reason"] == "idle":
        where = (
            "as the first line of the notebook's first code cell" if notebook
            else "at the top of the script"
        )
        msg = (
            f"Script appears stuck. No DuckDB activity or output for "
            f"{sup['idle_timeout']:g}s (total {total}s elapsed). If it legitimately "
            "waits that long (a slow remote query), put `# @havn: idle_timeout=<seconds>` "
            f"(0 turns the check off) {where}"
        )
    else:
        msg = f"Script timed out after {timeout:g}s"
    if sup["orphaned"]:
        msg += (
            ". It could not be stopped and is still running in the background; "
            "later scripts on this connection will not start until it exits"
        )
    return msg


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


def _notebook_pragma_source(raw: str) -> str:
    """Source of a notebook's first code cell, where its pragmas live.

    The .dpnb file is JSON, so ``_parse_pragma`` on the file itself never
    found anything and a notebook could not raise its idle timeout.
    """
    import json

    try:
        cells = json.loads(raw).get("cells", [])
    except Exception:
        return ""
    for cell in cells:
        if isinstance(cell, dict) and cell.get("type") == "code":
            src = cell.get("source", "")
            return "".join(src) if isinstance(src, list) else str(src)
    return ""


def _idle_timeout_pragma(pragmas: dict[str, str], name: str) -> float | None:
    """``# @havn: idle_timeout=<seconds>`` (0 = off), or None for the default."""
    raw = pragmas.get("idle_timeout")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Script %s: ignoring idle_timeout=%r (not a number)", name, raw)
        return None
    return max(0.0, value)


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
    on_cell_start: Callable | None = None,
    should_stop: Callable[[], bool] | None = None,
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

    # A pipeline step stops at the first failing cell: later cells would run
    # against whatever the failed one left half-built.
    result_nb = run_notebook(
        conn, notebook, project_dir=project_dir,
        stop_on_error=True, on_cell_start=on_cell_start, should_stop=should_stop,
    )

    # Collect per-cell results
    cell_results = result_nb.get("cell_results", [])

    # Errors from the cells this run executed (not outputs saved in the file)
    errors = []
    for cr in cell_results:
        for output in cr.get("outputs", []):
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
            "skipped_cells": result_nb.get("skipped_cells", []),
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

    # Never start a script on a connection a timed-out script still holds:
    # DuckDB connections are not thread-safe, so the two would race. Checked
    # before anything else touches `conn` (the resource manager's SETs,
    # ensure_meta_table, the schedule=once lookup).
    orphan = orphaned_script(conn)
    if orphan is not None:
        msg = (
            f"Not starting {script_path.name}: {orphan} timed out earlier and is "
            "still running on this connection"
        )
        console.print(f"  [red]blocked[/red] [bold]{script_path.name}[/bold]: {msg}")
        logger.error(msg)
        return {
            "script": script_path.name, "status": "error", "duration_ms": 0,
            "log_output": msg, "error": msg, "orphaned": True,
        }

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
        _source_cache = read_project_text(script_path)
    except Exception:
        _source_cache = ""

    pragmas = _parse_pragma(
        _notebook_pragma_source(_source_cache) if script_path.suffix == ".dpnb" else _source_cache
    )
    idle_timeout = _idle_timeout_pragma(pragmas, script_path.name)
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

    # Dispatch .dpnb notebooks. They run under the same hard/idle timeout
    # as .py scripts; each started cell counts as activity.
    if script_path.suffix == ".dpnb":
        cells_started = [0]

        def _on_cell(_cell) -> None:
            cells_started[0] += 1

        stop = threading.Event()
        sup = _run_supervised(
            conn, script_path.name,
            lambda: _run_notebook_as_script(
                conn, script_path, on_cell_start=_on_cell, should_stop=stop.is_set,
            ),
            timeout=timeout,
            activity=lambda: cells_started[0],
            stop_event=stop,
            idle_timeout=idle_timeout,
            track_child_threads=True,
        )
        duration_ms = int(sup["elapsed"] * 1000)
        if sup["reason"] is not None:
            error_msg = _timeout_message(sup, timeout, notebook=True)
            logger.warning("Script %s: %s", script_path.name, error_msg)
            log_conn = conn.cursor() if sup["orphaned"] else conn
            log_run(log_conn, script_type, script_path.name, "error", duration_ms,
                    error=error_msg, pipeline_run_id=pipeline_run_id)
            console.print(f"  [red]timeout[/red] {label}: {error_msg}")
            if use_circuit_breaker:
                _get_circuit_breaker()._record_failure(script_path.name)
            return {
                "script": script_path.name, "status": "error", "duration_ms": duration_ms,
                "log_output": error_msg, "error": error_msg,
                "timed_out": True, "timeout_reason": sup["reason"], "orphaned": sup["orphaned"],
            }
        if sup["error"] is not None:
            e = sup["error"]
            error_msg = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            log_run(conn, script_type, script_path.name, "error", duration_ms, error=str(e), log_output=error_msg, pipeline_run_id=pipeline_run_id)
            console.print(f"  [red]fail[/red] {label}: {e}")
            if use_circuit_breaker:
                _get_circuit_breaker()._record_failure(script_path.name)
            return {"script": script_path.name, "status": "error", "duration_ms": duration_ms, "log_output": error_msg, "error": str(e)}
        result = sup["value"]
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

    # .py scripts (reuse the source we already read for pragma parsing)
    source = _source_cache or read_project_text(script_path)
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()

    def _execute():
        if _has_run_function(source):
            # Legacy mode: import module and call run(db)
            module = _load_module(script_path)
            with _capture_thread_output(stdout_capture, stderr_capture):
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
            with _capture_thread_output(stdout_capture, stderr_capture):
                exec(compile(source, str(script_path), "exec"), namespace)

    # Run in a thread under the hard timeout and an idle timeout: no DuckDB
    # query in flight and no new stdout/stderr for SCRIPT_IDLE_TIMEOUT_SECONDS
    # means stuck.
    sup = _run_supervised(
        conn, script_path.name, _execute,
        timeout=timeout,
        activity=lambda: (len(stdout_capture.getvalue()), len(stderr_capture.getvalue())),
        idle_timeout=idle_timeout,
    )

    if sup["reason"] is not None:
        duration_ms = int(sup["elapsed"] * 1000)
        error_msg = _timeout_message(sup, timeout)
        log_output = stdout_capture.getvalue() + stderr_capture.getvalue()
        logger.warning("Script %s: %s", script_path.name, error_msg)
        # An orphaned script still owns `conn`; log through a cursor (its own
        # connection to the same database) instead of racing it.
        log_conn = conn.cursor() if sup["orphaned"] else conn
        log_run(log_conn, script_type, script_path.name, "error", duration_ms, error=error_msg, log_output=log_output or None, pipeline_run_id=pipeline_run_id)
        console.print(f"  [red]timeout[/red] {label}: {error_msg}")
        if use_circuit_breaker:
            _get_circuit_breaker()._record_failure(script_path.name)
        return {
            "script": script_path.name, "status": "error", "duration_ms": duration_ms,
            "log_output": log_output, "error": error_msg,
            "timed_out": True, "timeout_reason": sup["reason"], "orphaned": sup["orphaned"],
        }

    if sup["error"] is not None:
        e = sup["error"]
        duration_ms = int(sup["elapsed"] * 1000)
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

    duration_ms = int(sup["elapsed"] * 1000)
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
        # A script that timed out and is still running owns the connection;
        # every later script would be refused anyway, so stop here.
        if result.get("orphaned"):
            console.print(f"[red]Stopping: {script.name} is still running after its timeout[/red]")
            break
        # Stop on error for ingest (data integrity)
        if script_type == "ingest" and result["status"] == "error":
            console.print("[red]Stopping: ingest script failed[/red]")
            break

    return results
