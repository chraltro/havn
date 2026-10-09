"""Notebook runner: execute all cells sequentially."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb

from .code_cell import execute_cell
from .ingest_cell import execute_ingest_cell
from .sql_cell import execute_sql_cell


def run_notebook(
    conn: duckdb.DuckDBPyConnection,
    notebook: dict,
    project_dir: Path | None = None,
    stop_on_error: bool = False,
    on_cell_start: Callable[[dict], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """Execute all executable cells in a notebook sequentially.

    Handles code, sql, and ingest cell types. Markdown cells are skipped.
    Returns the notebook with updated outputs and per-cell timing.

    ``stop_on_error`` stops at the first cell with an error output; the
    remaining cells are not run and are listed in ``skipped_cells``. A
    pipeline step needs this: running later cells against the half-built
    state of a failed one writes wrong data. The interactive "run all"
    keeps running every cell so the user sees all failures at once.

    ``on_cell_start`` is called with each cell before it runs; the script
    runner uses it as a progress signal for its idle timeout.

    ``should_stop`` is checked before each cell; once it returns True the
    rest are skipped (the script runner sets it when the step times out, so
    a cell that outlives the timeout is the last one to run).

    Every notebook run is acquired against the ResourceManager under the
    ``query`` category — notebooks are interactive exploration and share
    the same budget as ad-hoc queries.
    """
    from havn.engine.resource_manager import current_task, get_resource_manager

    manager = get_resource_manager()
    label = f"notebook:{notebook.get('name', 'untitled')}"
    with manager.acquire_sync("query", label, conn=conn):
        task = current_task()
        if task is not None:
            manager.register_cancel(task.task_id, conn.interrupt)
        return _run_notebook_body(
            conn, notebook, project_dir,
            stop_on_error=stop_on_error, on_cell_start=on_cell_start,
            should_stop=should_stop,
        )


def _run_notebook_body(
    conn: duckdb.DuckDBPyConnection,
    notebook: dict,
    project_dir: Path | None = None,
    stop_on_error: bool = False,
    on_cell_start: Callable[[dict], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    namespace: dict[str, Any] = {}
    total_ms = 0
    cell_results: list[dict] = []
    skipped: list = []
    stopped = False

    for cell in notebook.get("cells", []):
        cell_type = cell.get("type", "")
        source = cell.get("source", "")

        if cell_type not in ("code", "sql", "ingest"):
            continue
        if not stopped and should_stop is not None and should_stop():
            stopped = True
        if stopped:
            # Drop outputs saved from an earlier run so nothing reads them
            # as this run's result.
            cell["outputs"] = []
            skipped.append(cell.get("id"))
            continue
        if on_cell_start is not None:
            on_cell_start(cell)

        if cell_type == "code":
            result = execute_cell(conn, source, namespace)
            namespace = result["namespace"]
        elif cell_type == "sql":
            result = execute_sql_cell(conn, source)
        else:
            result = execute_ingest_cell(conn, source, project_dir)

        cell["outputs"] = result["outputs"]
        cell["duration_ms"] = result["duration_ms"]
        total_ms += result["duration_ms"]
        has_error = any(o.get("type") == "error" for o in result["outputs"])
        cell_results.append({
            "cell_id": cell.get("id"),
            "type": cell_type,
            "duration_ms": result["duration_ms"],
            "has_error": has_error,
            "outputs": result["outputs"],
        })
        if has_error and stop_on_error:
            stopped = True

    notebook["last_run_ms"] = total_ms
    notebook["cell_results"] = cell_results
    if stop_on_error or should_stop is not None:
        notebook["skipped_cells"] = skipped
    return notebook
