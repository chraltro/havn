"""Masking policy CRUD endpoints (admin-only)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from havn.server.deps import DbConn, _require_permission, _get_user

router = APIRouter()


# --- Pydantic models ---


_VALID_METHODS = (
    "hash|redact|null|partial|email|phone|credit_card|first_initial"
    "|ip_address|range|noise|date_shift|truncate|consistent_hash"
)
_METHOD_RE = rf"^({_VALID_METHODS})$"


class PolicyCreate(BaseModel):
    schema_name: str = Field(..., min_length=1)
    table_name: str = Field(..., min_length=1)
    column_name: str = Field(..., min_length=1)
    method: str = Field(..., pattern=_METHOD_RE)
    method_config: dict | None = None
    condition_column: str | None = None
    condition_value: str | None = None
    exempted_roles: list[str] | None = None


class PolicyUpdate(BaseModel):
    schema_name: str | None = None
    table_name: str | None = None
    column_name: str | None = None
    method: str | None = Field(default=None, pattern=_METHOD_RE)
    method_config: dict | None = None
    condition_column: str | None = None
    condition_value: str | None = None
    exempted_roles: list[str] | None = None


# --- Endpoints ---


@router.get("/api/masking/methods")
def list_methods(request: Request) -> list[dict]:
    """Return available masking methods with descriptions and config schemas."""
    _require_permission(request, "read")
    from havn.engine.masking import list_masking_methods

    return list_masking_methods()


@router.get("/api/masking/policies")
def list_policies(request: Request, conn: DbConn) -> list[dict]:
    """List all masking policies."""
    _require_permission(request, "write")
    from havn.engine.masking import list_policies as _list

    return _list(conn)


@router.post("/api/masking/policies")
def create_policy(request: Request, req: PolicyCreate, conn: DbConn) -> dict:
    """Create a new masking policy (admin-only).

    When auth is disabled, the default of ``exempted_roles=["admin"]`` would
    make the policy silently inert for the local user (who is admin in
    no-auth mode). To prevent that footgun, when ``exempted_roles`` is not
    provided AND auth is disabled, default to ``[]`` so the policy actually
    masks for the caller.
    """
    user = _require_permission(request, "write")
    from havn.engine.masking import create_policy as _create
    from havn.server import app as _server_app

    exempted = req.exempted_roles
    if exempted is None and not getattr(_server_app, "AUTH_ENABLED", False):
        exempted = []

    try:
        result = _create(
            conn,
            schema_name=req.schema_name,
            table_name=req.table_name,
            column_name=req.column_name,
            method=req.method,
            method_config=req.method_config,
            condition_column=req.condition_column,
            condition_value=req.condition_value,
            exempted_roles=exempted,
        )
        try:
            from havn.engine.audit import log_audit

            client_ip = request.client.host if request.client else None
            log_audit(
                conn,
                user=user["username"],
                action="masking_policy_create",
                resource=f"{req.schema_name}.{req.table_name}.{req.column_name}",
                detail=f"method={req.method}",
                ip_address=client_ip,
            )
        except Exception:
            pass
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/masking/policies/{policy_id}")
def get_policy(request: Request, policy_id: str, conn: DbConn) -> dict:
    """Get a single masking policy by ID."""
    _require_permission(request, "write")
    from havn.engine.masking import get_policy as _get

    policy = _get(conn, policy_id)
    if not policy:
        raise HTTPException(404, "Policy not found")
    return policy


@router.put("/api/masking/policies/{policy_id}")
def update_policy(request: Request, policy_id: str, req: PolicyUpdate, conn: DbConn) -> dict:
    """Update a masking policy."""
    user = _require_permission(request, "write")
    from havn.engine.masking import update_policy as _update

    updates = req.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "No fields to update")
    try:
        result = _update(conn, policy_id, **updates)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not result:
        raise HTTPException(404, "Policy not found")
    try:
        from havn.engine.audit import log_audit

        client_ip = request.client.host if request.client else None
        log_audit(
            conn,
            user=user["username"],
            action="masking_policy_update",
            resource=f"policy_id={policy_id}",
            detail=f"updated fields: {', '.join(updates.keys())}",
            ip_address=client_ip,
        )
    except Exception:
        pass
    return result


@router.delete("/api/masking/policies/{policy_id}")
def delete_policy(request: Request, policy_id: str, conn: DbConn) -> dict:
    """Delete a masking policy."""
    user = _require_permission(request, "write")
    from havn.engine.masking import delete_policy as _delete

    if not _delete(conn, policy_id):
        raise HTTPException(404, "Policy not found")
    try:
        from havn.engine.audit import log_audit

        client_ip = request.client.host if request.client else None
        log_audit(
            conn,
            user=user["username"],
            action="masking_policy_delete",
            resource=f"policy_id={policy_id}",
            detail="policy deleted",
            ip_address=client_ip,
        )
    except Exception:
        pass
    return {"status": "deleted", "id": policy_id}
