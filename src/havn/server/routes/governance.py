"""Row-level security, user attributes, classification report, preview-as-user."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from havn.server.deps import DbConn, _get_project_dir, _get_user, _require_permission, _serialize
from havn.server.routes.masking import MANAGE_MASKING_PERMISSION

router = APIRouter()


class RowPolicyCreate(BaseModel):
    schema_name: str = Field(..., min_length=1, max_length=200)
    table_name: str = Field(..., min_length=1, max_length=200)
    filter_sql: str = Field(..., min_length=1, max_length=10_000)
    name: str | None = Field(default=None, max_length=200)
    applies_to_roles: list[str] | None = None
    applies_to_users: list[str] | None = None
    exempted_roles: list[str] | None = None
    exempted_users: list[str] | None = None
    follow_lineage: bool = True
    enabled: bool = True
    description: str | None = Field(default=None, max_length=2000)


class RowPolicyUpdate(BaseModel):
    schema_name: str | None = None
    table_name: str | None = None
    filter_sql: str | None = Field(default=None, max_length=10_000)
    name: str | None = None
    applies_to_roles: list[str] | None = None
    applies_to_users: list[str] | None = None
    exempted_roles: list[str] | None = None
    exempted_users: list[str] | None = None
    follow_lineage: bool | None = None
    enabled: bool | None = None
    description: str | None = None


class AttributesUpdate(BaseModel):
    attributes: dict


class PreviewRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    sql: str = Field(..., min_length=1, max_length=100_000)
    limit: int = Field(default=200, gt=0, le=5000)


def _audit(conn, request: Request, user: dict, action: str, resource: str, detail: str) -> None:
    try:
        from havn.engine.audit import log_audit

        log_audit(conn, user=user["username"], action=action, resource=resource,
                  detail=detail, ip_address=request.client.host if request.client else None)
    except Exception:
        pass


@router.get("/api/governance/row-policies")
def list_row_policies(request: Request, conn: DbConn) -> list[dict]:
    """Explicit row policies (admin only)."""
    _require_permission(request, MANAGE_MASKING_PERMISSION)
    from havn.engine.row_policies import ensure_row_policy_table, load_row_policies

    ensure_row_policy_table(conn)
    return load_row_policies(conn)


@router.post("/api/governance/row-policies")
def create_row_policy_endpoint(request: Request, req: RowPolicyCreate, conn: DbConn) -> dict:
    user = _require_permission(request, MANAGE_MASKING_PERMISSION)
    from havn.engine.row_policies import create_row_policy

    try:
        policy = create_row_policy(conn, created_by=user["username"], **req.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    _audit(conn, request, user, "row_policy_create", f"{req.schema_name}.{req.table_name}",
           f"filter={req.filter_sql[:200]}")
    return policy


@router.put("/api/governance/row-policies/{policy_id}")
def update_row_policy_endpoint(request: Request, policy_id: str, req: RowPolicyUpdate, conn: DbConn) -> dict:
    user = _require_permission(request, MANAGE_MASKING_PERMISSION)
    from havn.engine.row_policies import update_row_policy

    updates = req.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "No fields to update")
    try:
        policy = update_row_policy(conn, policy_id, **updates)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if policy is None:
        raise HTTPException(404, "Row policy not found")
    _audit(conn, request, user, "row_policy_update", f"policy_id={policy_id}",
           f"updated fields: {', '.join(sorted(updates))}")
    return policy


@router.delete("/api/governance/row-policies/{policy_id}")
def delete_row_policy_endpoint(request: Request, policy_id: str, conn: DbConn) -> dict:
    user = _require_permission(request, MANAGE_MASKING_PERMISSION)
    from havn.engine.row_policies import delete_row_policy

    if not delete_row_policy(conn, policy_id):
        raise HTTPException(404, "Row policy not found")
    _audit(conn, request, user, "row_policy_delete", f"policy_id={policy_id}", "row policy deleted")
    return {"status": "deleted", "id": policy_id}


@router.put("/api/users/{username}/attributes")
def set_user_attributes_endpoint(request: Request, username: str, req: AttributesUpdate, conn: DbConn) -> dict:
    """Replace a user's attributes (read by row policies via havn_attr)."""
    user = _require_permission(request, "manage_users")
    from havn.engine.auth import set_user_attributes
    from havn.server.deps import invalidate_token_cache

    try:
        stored = set_user_attributes(conn, username, req.attributes)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if stored is None:
        raise HTTPException(404, f"User '{username}' not found")
    invalidate_token_cache()
    _audit(conn, request, user, "user_update", username, f"attributes={sorted(stored)}")
    return {"username": username, "attributes": stored}


@router.get("/api/governance")
def governance_report(request: Request, conn: DbConn) -> dict:
    """Classifications (explicit and inherited), row policies, declassifications."""
    _require_permission(request, "read")
    from havn.engine.governance.report import pii_report

    report = pii_report(conn, _get_project_dir())
    user = _get_user(request) or {}
    if user.get("role") != "admin":
        # Filters can name attribute values and users; only admins see them.
        report["row_policies"] = [
            {k: v for k, v in p.items() if k in ("relation", "inherited_from", "deny")}
            for p in report["row_policies"]
        ]
    return report


@router.post("/api/governance/preview")
def preview_as_user(request: Request, req: PreviewRequest, conn: DbConn) -> dict:
    """Run a read-only query the way another user would see it (admin only)."""
    admin = _require_permission(request, MANAGE_MASKING_PERMISSION)
    from havn.engine.auth import ensure_auth_tables, list_users
    from havn.engine.governance import run_governed
    from havn.engine.masking_rewriter import MaskedColumnAccessError
    from havn.engine.sql_safety import ReadOnlyQueryError, validate_read_only_query

    try:
        validate_read_only_query(req.sql)
    except ReadOnlyQueryError as e:
        raise HTTPException(e.status_code, str(e))
    ensure_auth_tables(conn)
    target = next((u for u in list_users(conn) if u["username"] == req.username), None)
    if target is None:
        raise HTTPException(404, f"User '{req.username}' not found")
    _audit(conn, request, admin, "query", req.sql[:500], f"preview as {req.username}")
    try:
        data = run_governed(conn, req.sql, target, project_dir=_get_project_dir(),
                            limit=req.limit, serialize=_serialize)
    except MaskedColumnAccessError as e:
        return {"columns": [], "rows": [], "refused": str(e), "username": req.username}
    except Exception as e:
        raise HTTPException(400, str(e))
    return {**data, "username": req.username, "role": target["role"],
            "attributes": target.get("attributes", {})}
