"""Macros API endpoint: list available Python and SQL macros."""

from __future__ import annotations

from fastapi import APIRouter, Request

from havn.server.deps import _get_project_dir, _require_permission

router = APIRouter()


@router.get("/api/macros")
def list_macros_endpoint(request: Request) -> list[dict]:
    """Return all discovered macros with metadata for editor autocomplete."""
    _require_permission(request, "read")

    from havn.engine.macros import list_macros

    project_dir = _get_project_dir()
    return list_macros(project_dir)
