"""API endpoints for the API poll consumer (streaming pollers)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from havn.server.deps import (
    DbConnReadOnly,
    DbConnAutoCreate,
    _get_project_dir,
    _require_permission,
    _validate_identifier,
    get_db_readonly_optional,
)

logger = logging.getLogger("havn.server")

router = APIRouter()


@router.get("/api/streaming/pollers")
def list_pollers(request: Request, conn: DbConnReadOnly) -> list[dict]:
    """List configured api-poll sources with their CDC state.

    Returns connector_name, cdc_mode, last watermark, last poll time, and
    rows_synced for each entry in ``_havn.cdc_state``.  Only entries whose
    ``cdc_mode`` is ``high_watermark`` or ``full_refresh`` (i.e. REST poll
    sources, not file-tracking sources) are returned.
    """
    _require_permission(request, "read")
    from havn.engine.cdc import get_cdc_status

    all_entries = get_cdc_status(conn)
    # Restrict to poll-mode entries (exclude file_tracking CDC entries)
    return [e for e in all_entries if e.get("cdc_mode") != "file_tracking"]


@router.post("/api/streaming/pollers/{connector_name}/poll-once")
def trigger_poll_once(
    request: Request,
    connector_name: str,
    conn: DbConnAutoCreate,
) -> dict:
    """Trigger a one-shot synchronous poll for the named connector.

    The connector config is read from ``project.yml``.  Returns a summary of
    the poll result including rows inserted and the new watermark.
    """
    _require_permission(request, "execute")
    _validate_identifier(connector_name, "connector_name")

    project_dir = _get_project_dir()

    from havn.config import load_project
    from havn.engine.streaming.api_poll import APIPollConsumer

    config = load_project(project_dir)
    connections = config.connections or {}
    if connector_name not in connections:
        raise HTTPException(404, f"Connector '{connector_name}' not found in project.yml")

    conn_cfg = connections[connector_name]
    if hasattr(conn_cfg, "params"):
        poll_cfg = conn_cfg.params
    elif hasattr(conn_cfg, "model_dump"):
        poll_cfg = conn_cfg.model_dump()
    else:
        poll_cfg = dict(conn_cfg) if conn_cfg else {}

    consumer = APIPollConsumer(connector_name, poll_cfg, project_dir)
    result = consumer.poll_once()

    return {
        "connector": connector_name,
        "rows_inserted": result.rows_inserted,
        "new_watermark": result.new_watermark,
        "duration_ms": result.duration_ms,
        "error": result.error,
        "status": "error" if result.error else "ok",
    }
