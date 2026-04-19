"""Notebook runner: execute all cells sequentially."""

from __future__ import annotations

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
) -> dict:
    """Execute all executable cells in a notebook sequentially.

    Handles code, sql, and ingest cell types. Markdown cells are skipped.
    Returns the notebook with updated outputs and per-cell timing.

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
        return _run_notebook_body(conn, notebook, project_dir)


def _run_notebook_body(
    conn: duckdb.DuckDBPyConnection,
    notebook: dict,
    project_dir: Path | None = None,
) -> dict:
    namespace: dict[str, Any] = {}
    total_ms = 0
    cell_results: list[dict] = []

    for cell in notebook.get("cells", []):
        cell_type = cell.get("type", "")
        source = cell.get("source", "")

        if cell_type == "code":
            result = execute_cell(conn, source, namespace)
            namespace = result["namespace"]
        elif cell_type == "sql":
            result = execute_sql_cell(conn, source)
        elif cell_type == "ingest":
            result = execute_ingest_cell(conn, source, project_dir)
        else:
            continue

        cell["outputs"] = result["outputs"]
        cell["duration_ms"] = result["duration_ms"]
        total_ms += result["duration_ms"]
        cell_results.append({
            "cell_id": cell.get("id"),
            "type": cell_type,
            "duration_ms": result["duration_ms"],
            "has_error": any(o.get("type") == "error" for o in result["outputs"]),
            "outputs": result["outputs"],
        })

    notebook["last_run_ms"] = total_ms
    notebook["cell_results"] = cell_results
    return notebook
