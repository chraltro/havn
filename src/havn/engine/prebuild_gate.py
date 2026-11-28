"""The pre-build validation gate of the pipeline engine.

Runs ``validate_models`` (name-level checks and the bind pass) over the
models a run is about to build. With ingest or seeds in the run the landing
tables may not exist yet, so ``engine.pipeline_engine`` runs the gate once
the loaders are done and immediately before the first transform, rather than
skipping it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import duckdb

logger = logging.getLogger("havn.pipeline")


def run_prebuild_gate(
    conn: duckdb.DuckDBPyConnection,
    models: list,
    emit: Callable[[str, dict], None],
    *,
    project_dir: Path | None = None,
    bind: bool = True,
) -> bool:
    """Validate ``models`` and emit every finding. True when the run may go on.

    Args:
        conn: The run's warehouse connection. A cursor is taken off it, and
            the bind pass attaches its own throwaway catalog to it.
        models: The transform models this run would build.
        emit: The pipeline's event emitter.
        project_dir: Passed to the bind pass so Python macros resolve.
        bind: Run the shadow bind pass as well as the name-level checks.

    Returns:
        False when at least one error-severity finding was emitted.
    """
    from havn.engine.transform import validate_models
    from havn.engine.write_queue import cursor_for

    if not models:
        return True

    cur = cursor_for(conn)
    try:
        errors = validate_models(
            cur, models, bind=bind, project_dir=project_dir
        )
    except Exception as e:
        # A gate that cannot run must not block the build; the run itself
        # will surface any real problem.
        logger.warning("Pre-build validation could not run: %s", e)
        return True
    finally:
        cur.close()

    ok = True
    for err in errors:
        payload = {
            "model": err.model,
            "severity": err.severity,
            "message": err.message,
        }
        if err.line is not None:
            payload["line"] = err.line
        emit("validation", payload)
        if err.severity == "error":
            ok = False
    return ok
