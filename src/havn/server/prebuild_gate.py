"""The pre-build validation gate shared by the server's pipeline runners.

Both pipeline entry points ran ``validate_models`` up front and skipped it
entirely when the run included ingest steps, on the grounds that landing
tables do not exist yet on a fresh database. That reasoning holds for the
name-level checks, but it also meant the most common kind of run -- ingest
then transform -- got no gate at all.

The fix is to run the same gate, just later: once ingest has produced the
landing tables and immediately before the first transform. This module holds
the gate itself so the two call sites stay a couple of lines each.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import duckdb

logger = logging.getLogger("havn.server.pipeline")


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
        conn: The server's write connection. A cursor is taken off it, and
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
