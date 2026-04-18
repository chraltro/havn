"""Resource Manager HTTP surface.

- ``GET /api/resources`` — one-shot snapshot (categories, active, recent).
- ``GET /api/resources/stream`` — SSE, pushes a snapshot every 2 seconds.
- ``POST /api/resources/cancel/{task_id}`` — cancel a running task.
- ``PUT /api/resources/allocation`` — persist dial changes to project.yml.
"""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from havn.engine.resource_manager import (
    CATEGORIES,
    CategoryBudget,
    get_resource_manager,
)
from havn.server.deps import _get_project_dir, _require_permission

router = APIRouter()


@router.get("/api/resources")
def get_resources(request: Request) -> dict:
    _require_permission(request, "read")
    return get_resource_manager().snapshot()


@router.get("/api/resources/stream")
async def stream_resources(request: Request) -> StreamingResponse:
    _require_permission(request, "read")
    manager = get_resource_manager()

    async def generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                payload = json.dumps(manager.snapshot())
                yield f"data: {payload}\n\n"
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/resources/cancel/{task_id}")
def cancel_task(task_id: str, request: Request) -> dict:
    _require_permission(request, "execute")
    manager = get_resource_manager()
    ok = manager.cancel(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="task not found")
    return {"cancelled": task_id}


class BudgetUpdate(BaseModel):
    category: Literal["transform", "query", "streaming", "system"]
    memory_gb: float = Field(..., gt=0, le=1024)
    threads: int = Field(..., ge=1, le=256)
    max_concurrent: int = Field(..., ge=1, le=256)


@router.put("/api/resources/allocation")
def update_allocation(body: BudgetUpdate, request: Request) -> dict:
    _require_permission(request, "write")
    manager = get_resource_manager()
    manager.update_budget(
        body.category,
        CategoryBudget(
            memory_gb=body.memory_gb,
            threads=body.threads,
            max_concurrent=body.max_concurrent,
        ),
    )
    _persist_allocation(body)
    return {"updated": body.category, "snapshot": manager.snapshot()}


def _persist_allocation(body: BudgetUpdate) -> None:
    """Write the updated budget back to project.yml under ``resources:``.

    No-op when project.yml is absent (useful in tests).
    """
    import yaml

    path = _get_project_dir() / "project.yml"
    if not path.exists():
        return
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return
    resources = raw.get("resources") or {}
    resources[body.category] = {
        "memory_gb": body.memory_gb,
        "threads": body.threads,
        "max_concurrent": body.max_concurrent,
    }
    raw["resources"] = resources
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
