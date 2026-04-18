"""Databricks-style SQL API.

- ``POST /v1/sql``                         — execute SQL; sync-fast or 202 + statement_id.
- ``GET  /v1/sql/{statement_id}``          — status + inline result.
- ``GET  /v1/sql/{statement_id}/result``   — stream result as JSON / NDJSON / Arrow IPC.
- ``DELETE /v1/sql/{statement_id}``        — cancel a running statement.

Statement state lives in an in-memory registry (``_StatementRegistry``). Cloud
swaps this for a Redis-backed registry; here in core we keep it simple.
"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import duckdb
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from havn.engine.resource_manager import get_resource_manager
from havn.server.deps import _get_write_queue, _require_permission

logger = logging.getLogger("havn.sql_api")
router = APIRouter()

SYNC_TIMEOUT_SECONDS = 10.0
RESULT_TTL_SECONDS = 3600.0
MAX_INLINE_ROWS = 10_000


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class StatementState:
    statement_id: str
    sql: str
    status: str = "pending"     # pending | running | succeeded | failed | cancelled
    rows: list[list[Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    row_count: int = 0
    task_id: str | None = None
    _arrow: bytes | None = None


class _StatementRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, StatementState] = {}

    def put(self, state: StatementState) -> None:
        self._gc()
        with self._lock:
            self._store[state.statement_id] = state

    def get(self, statement_id: str) -> StatementState | None:
        with self._lock:
            return self._store.get(statement_id)

    def delete(self, statement_id: str) -> bool:
        with self._lock:
            return self._store.pop(statement_id, None) is not None

    def _gc(self) -> None:
        cutoff = time.time() - RESULT_TTL_SECONDS
        with self._lock:
            stale = [k for k, v in self._store.items() if (v.finished_at or v.created_at) < cutoff]
            for k in stale:
                self._store.pop(k, None)


_registry = _StatementRegistry()


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _execute_statement(state: StatementState) -> None:
    """Run a single statement on the write queue, recording results on ``state``."""
    from havn.engine.resource_manager import current_task

    manager = get_resource_manager()
    wq = _get_write_queue()
    conn = wq.conn

    with manager.acquire_sync("query", f"sql:{state.statement_id[:8]}", conn=conn):
        task = current_task()
        if task is not None:
            state.task_id = task.task_id
            manager.register_cancel(task.task_id, conn.interrupt)

        state.status = "running"
        try:
            cur = conn.cursor()
            try:
                cur.execute(state.sql)
                if cur.description:
                    state.columns = [d[0] for d in cur.description]
                    import pyarrow as pa

                    table = cur.to_arrow_table() if hasattr(cur, "to_arrow_table") else cur.fetch_arrow_table()
                    state.row_count = table.num_rows
                    slice_ = table.slice(0, MAX_INLINE_ROWS) if MAX_INLINE_ROWS else table
                    # Convert to row-arrays without an intermediate dict-per-row
                    # (much faster and keeps value types intact).
                    col_arrays = [slice_.column(c).to_pylist() for c in state.columns]
                    state.rows = [list(row) for row in zip(*col_arrays)]
                    sink = io.BytesIO()
                    with pa.ipc.new_stream(sink, table.schema) as w:
                        w.write_table(table)
                    state._arrow = sink.getvalue()
                else:
                    # Statement had no result set (DDL / DML) — fine.
                    state.columns = []
                    state.rows = []
                    state.row_count = 0
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
            state.status = "succeeded"
        except duckdb.InterruptException:
            state.status = "cancelled"
            state.error = "interrupted"
            raise
        except Exception as e:
            state.status = "failed"
            state.error = str(e)[:1000]
        finally:
            state.finished_at = time.time()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ExecuteRequest(BaseModel):
    sql: str = Field(..., min_length=1)
    wait_seconds: float | None = Field(None, ge=0, le=120)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/v1/sql")
async def execute_sql(body: ExecuteRequest, request: Request) -> Response:
    _require_permission(request, "execute")

    state = StatementState(statement_id=str(uuid.uuid4()), sql=body.sql)
    _registry.put(state)

    wait = body.wait_seconds if body.wait_seconds is not None else SYNC_TIMEOUT_SECONDS

    loop = asyncio.get_event_loop()
    fut = loop.run_in_executor(None, _execute_statement, state)

    try:
        await asyncio.wait_for(fut, timeout=wait)
    except asyncio.TimeoutError:
        # Statement still running — return 202 with statement_id.
        return _json_response(202, {
            "statement_id": state.statement_id,
            "status": state.status,
        })

    return _json_response(200, _render_state(state, include_rows=True))


@router.get("/v1/sql/{statement_id}")
async def get_statement(statement_id: str, request: Request) -> Response:
    _require_permission(request, "read")
    state = _registry.get(statement_id)
    if state is None:
        raise HTTPException(status_code=404, detail="statement not found")
    return _json_response(200, _render_state(state, include_rows=False))


@router.get("/v1/sql/{statement_id}/result")
async def stream_result(statement_id: str, request: Request) -> Response:
    _require_permission(request, "read")
    state = _registry.get(statement_id)
    if state is None:
        raise HTTPException(status_code=404, detail="statement not found")
    if state.status not in ("succeeded",):
        raise HTTPException(status_code=409, detail=f"not ready: {state.status}")

    accept = (request.headers.get("accept") or "").lower()

    if "application/vnd.apache.arrow.stream" in accept and state._arrow:
        return Response(
            content=state._arrow,
            media_type="application/vnd.apache.arrow.stream",
        )

    if "application/x-ndjson" in accept:
        def gen():
            import json
            for row in state.rows:
                yield json.dumps(dict(zip(state.columns, row)), default=str) + "\n"
        return StreamingResponse(gen(), media_type="application/x-ndjson")

    # Default: JSON envelope
    return _json_response(200, _render_state(state, include_rows=True))


@router.delete("/v1/sql/{statement_id}")
async def cancel_statement(statement_id: str, request: Request) -> Response:
    _require_permission(request, "execute")
    state = _registry.get(statement_id)
    if state is None:
        raise HTTPException(status_code=404, detail="statement not found")
    if state.task_id:
        get_resource_manager().cancel(state.task_id)
    return _json_response(200, {"statement_id": statement_id, "status": "cancelling"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_state(state: StatementState, *, include_rows: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "statement_id": state.statement_id,
        "status": state.status,
        "row_count": state.row_count,
        "columns": state.columns,
        "error": state.error,
        "created_at": state.created_at,
        "finished_at": state.finished_at,
    }
    if include_rows:
        out["rows"] = state.rows
    return out


def _json_response(status_code: int, payload: dict[str, Any]) -> Response:
    import json

    return Response(
        status_code=status_code,
        content=json.dumps(payload, default=str),
        media_type="application/json",
    )
