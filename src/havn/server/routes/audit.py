"""Audit log endpoint."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from havn.server.deps import DbConn, _get_user, _require_permission

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


@router.get("/api/audit/access")
def get_access_log(
    request: Request,
    conn: DbConn,
    limit: int = 100,
    user: str | None = None,
    table: str | None = None,
    pii_column: str | None = None,
    source: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Return data-access log entries: who read which relations and PII columns.

    Admin only — the log names who read which personal-data columns, which is
    itself sensitive and would let a viewer profile other users' access.
    """
    _require_permission(request, "read")
    if (_get_user(request) or {}).get("role") != "admin":
        raise HTTPException(403, "The data-access log is admin only.")
    from havn.engine.access_log import query_access_log

    return query_access_log(
        conn,
        limit=limit,
        user=user,
        table=table,
        pii_column=pii_column,
        source=source,
        since=since,
        until=until,
    )
