"""Audit log endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request

from havn.server.deps import DbConn, _require_permission

router = APIRouter()


@router.get("/api/audit")
def get_audit_log(
    request: Request,
    conn: DbConn,
    limit: int = 100,
    user: str | None = None,
    action: str | None = None,
    resource: str | None = None,
) -> list[dict]:
    """Return recent audit log entries with optional filters."""
    _require_permission(request, "read")
    from havn.engine.audit import query_audit_log

    return query_audit_log(
        conn,
        limit=limit,
        user=user,
        action=action,
        resource=resource,
    )
