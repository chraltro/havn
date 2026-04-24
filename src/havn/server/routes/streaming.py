"""Streaming ingestion HTTP surface.

- ``POST /api/ingest/webhook/{source}`` — new staged webhook receiver.
- ``GET  /api/streaming/webhook/status``  — backlog + flush worker stats.
- ``POST /api/streaming/webhook/flush``   — manual flush trigger.
- ``GET  /api/streaming/pollers``          — list API-poll sources + state.
- ``POST /api/streaming/pollers/{name}/poll-once`` — one-shot sync poll.

The legacy ``POST /api/webhook/{name}`` endpoint in routes/connectors.py stays
untouched for backward compat — it still writes straight to ``_inbox``. New
integrations should use ``/api/ingest/webhook/{source}``.
"""

from __future__ import annotations

import json
import logging
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from havn.engine.streaming.webhook import FlushWorker, append_event
from havn.engine.utils import validate_identifier
from havn.server.deps import (
    DbConnAutoCreate,
    DbConnReadOnly,
    _get_project_dir,
    _require_permission,
    _validate_identifier,
)

logger = logging.getLogger("havn.server")

router = APIRouter()


# ---------------------------------------------------------------------------
# Flush worker singleton (webhook staging)
# ---------------------------------------------------------------------------


_worker: FlushWorker | None = None
_worker_lock = threading.Lock()


def _get_flush_worker() -> FlushWorker:
    global _worker
    if _worker is not None:
        return _worker
    with _worker_lock:
        if _worker is None:
            from havn.server.deps import _get_write_queue

            wq = _get_write_queue()
            _worker = FlushWorker(shared_conn=wq.conn)
            _worker.start()
    return _worker


def shutdown_flush_worker() -> None:
    """Called from server shutdown hooks."""
    global _worker
    with _worker_lock:
        if _worker is not None:
            _worker.stop()
            _worker = None


# ---------------------------------------------------------------------------
# Webhook ingestion (staged + flush worker)
# ---------------------------------------------------------------------------


@router.post("/api/ingest/webhook/{source}")
async def ingest_webhook(
    source: str,
    request: Request,
    conn: DbConnAutoCreate,
) -> Response:
    """Accept a single webhook event into the staging table.

    The background flush worker moves it to ``landing.<source>`` within
    ``flush_interval`` seconds (default 15s).
    """
    _require_permission(request, "execute")
    try:
        validate_identifier(source, "source")
    except ValueError as e:
        raise HTTPException(400, str(e))

    body = await request.body()
    if not body:
        raise HTTPException(400, "empty body")
    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(400, "invalid JSON payload")

    append_event(conn, source, payload)
    _get_flush_worker()
    return Response(
        status_code=202,
        content=json.dumps({"status": "staged", "source": source}),
        media_type="application/json",
    )


@router.get("/api/streaming/webhook/status")
def webhook_status(request: Request) -> dict:
    _require_permission(request, "read")
    from havn.engine.streaming.webhook import WebhookStaging
    from havn.server.deps import _get_write_queue

    conn = _get_write_queue().conn
    WebhookStaging.ensure(conn)
    backlog = WebhookStaging.backlog(conn)
    worker = _worker
    return {
        "backlog": backlog,
        "worker_running": worker is not None,
        "stats": {
            "flushes": worker.stats.flushes if worker else 0,
            "rows_flushed": worker.stats.rows_flushed if worker else 0,
            "errors": worker.stats.errors if worker else 0,
            "last_error": worker.stats.last_error if worker else None,
        },
    }


@router.post("/api/streaming/webhook/flush")
def webhook_flush_now(request: Request) -> dict:
    """Manual flush trigger — bypasses the polling interval."""
    _require_permission(request, "execute")
    worker = _get_flush_worker()
    count = worker.flush_once()
    return {"rows_flushed": count}


# ---------------------------------------------------------------------------
# API poll consumer
# ---------------------------------------------------------------------------


@router.get("/api/streaming/pollers")
def list_pollers(request: Request, conn: DbConnReadOnly) -> list[dict]:
    """List configured api-poll sources with their CDC state.

    Restricted to poll-mode entries; excludes ``file_tracking`` CDC.
    """
    _require_permission(request, "read")
    from havn.engine.cdc import get_cdc_status

    all_entries = get_cdc_status(conn)
    return [e for e in all_entries if e.get("cdc_mode") != "file_tracking"]


@router.post("/api/streaming/pollers/{connector_name}/poll-once")
def trigger_poll_once(
    request: Request,
    connector_name: str,
    conn: DbConnAutoCreate,
) -> dict:
    """One-shot synchronous poll of the named connector."""
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
