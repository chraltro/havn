"""Streaming ingestion HTTP surface.

- ``POST /api/ingest/webhook/{source}`` — new staged webhook receiver.
- ``GET  /api/streaming/webhook/status``  — backlog + flush worker stats.
- ``GET  /api/streaming/cdc/status``      — active logical-replication consumers.

The legacy ``POST /api/webhook/{name}`` endpoint in routes/connectors.py stays
untouched for backward compat — it still writes straight to ``_inbox``. New
integrations should use ``/api/ingest/webhook/{source}``.
"""

from __future__ import annotations

import json
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from havn.engine.streaming.webhook import FlushWorker, append_event
from havn.engine.utils import validate_identifier
from havn.server.deps import DbConn, _get_backend, _require_permission

router = APIRouter()


# ---------------------------------------------------------------------------
# Worker singleton
# ---------------------------------------------------------------------------


_worker: FlushWorker | None = None
_worker_lock = threading.Lock()


def _get_flush_worker() -> FlushWorker:
    global _worker
    if _worker is not None:
        return _worker
    with _worker_lock:
        if _worker is None:
            backend = _get_backend()

            def factory():
                return backend.connect(read_only=False)

            _worker = FlushWorker(connection_factory=factory)
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
# Routes
# ---------------------------------------------------------------------------


@router.post("/api/ingest/webhook/{source}")
async def ingest_webhook(
    source: str,
    request: Request,
    conn: DbConn,
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
    # Kick the worker so it's running.
    _get_flush_worker()
    return Response(
        status_code=202,
        content=json.dumps({"status": "staged", "source": source}),
        media_type="application/json",
    )


@router.get("/api/streaming/webhook/status")
def webhook_status(request: Request, conn: DbConn) -> dict:
    _require_permission(request, "read")
    from havn.engine.streaming.webhook import WebhookStaging

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
