"""Authentication, user management, and secrets endpoints."""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("havn.server")

from havn.engine.auth import MIN_PASSWORD_LENGTH
from havn.server.deps import (
    DbConn,
    _check_login_user_limit,
    _check_rate_limit,
    _get_config,
    _get_project_dir,
    _release_rate_limit,
    _require_permission,
    _require_user,
    _get_auth_enabled,
)

router = APIRouter()


# --- Pydantic models ---


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    # No MIN_PASSWORD_LENGTH here: users created under the older 4-character
    # rule must still be able to sign in.
    password: str = Field(..., min_length=1, max_length=500)


class CreateUserRequest(BaseModel):
    username: str = Field(
        ..., min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$"
    )
    password: str = Field(..., min_length=MIN_PASSWORD_LENGTH, max_length=500)
    role: str = Field(default="viewer", pattern=r"^(admin|editor|viewer)$")
    display_name: str | None = Field(default=None, max_length=200)


class UpdateUserRequest(BaseModel):
    role: str | None = Field(default=None, pattern=r"^(admin|editor|viewer)$")
    password: str | None = Field(default=None, min_length=MIN_PASSWORD_LENGTH, max_length=500)
    display_name: str | None = Field(default=None, max_length=200)


class SetSecretRequest(BaseModel):
    key: str = Field(
        ..., min_length=1, max_length=200, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    )
    value: str = Field(..., max_length=10_000)


# --- Auth endpoints ---


@router.post("/api/auth/login")
def login(request: Request, req: LoginRequest, conn: DbConn) -> dict:
    """Authenticate and get a token (rate-limited)."""
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(f"login:{client_ip}")
    # Keyed on the typed username before any lookup, and answered with the
    # same 429, so it says nothing about whether the username exists.
    user_key = _check_login_user_limit(req.username)
    from havn.engine.auth import authenticate

    token = authenticate(conn, req.username, req.password)
    if token:
        _release_rate_limit(user_key)
    # Audit login attempt
    try:
        from havn.engine.audit import log_audit

        if token:
            log_audit(
                conn,
                user=req.username,
                action="login",
                resource="auth",
                detail="success",
                ip_address=client_ip,
            )
        else:
            log_audit(
                conn,
                user=req.username,
                action="auth_failed",
                resource="login",
                detail="Invalid credentials",
                ip_address=client_ip,
            )
    except Exception:
        pass  # don't break login on audit failure
    if not token:
        raise HTTPException(401, "Invalid credentials")
    return {"token": token, "username": req.username}


@router.get("/api/auth/me")
def get_current_user(request: Request) -> dict:
    """Get current authenticated user."""
    return _require_user(request)


@router.get("/api/auth/status")
def get_auth_status(conn: DbConn) -> dict:
    """Check if auth is enabled and if initial setup is needed."""
    if not _get_auth_enabled():
        return {"auth_enabled": False, "needs_setup": False}
    from havn.engine.auth import has_any_users

    return {"auth_enabled": True, "needs_setup": not has_any_users(conn)}


# Serialises first-run setup: without it two concurrent calls could both see an
# empty users table and both create an admin. The server is the only process
# writing the warehouse (DuckDB holds a file lock), so a process lock suffices.
_setup_lock = threading.Lock()


@router.post("/api/auth/setup")
def initial_setup(request: Request, req: CreateUserRequest, conn: DbConn) -> dict:
    """Create the first admin user (only works when no users exist)."""
    # Without --auth there is nothing to set up, and /api/auth/status says so,
    # so this endpoint is closed rather than left as an unauthenticated way
    # to create the first admin. It does not stop accounts being made on such
    # a server: with auth off every caller counts as an admin, so POST
    # /api/users and PUT /api/users/{username} create and change accounts
    # (and passwords) that take effect the day --auth is turned on. That is
    # by design: whoever can reach a server without --auth already controls
    # it, and tests manage users through these endpoints with auth off.
    if not _get_auth_enabled():
        raise HTTPException(400, "Authentication is not enabled on this server")
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(f"setup:{client_ip}")
    from havn.engine.auth import authenticate, create_user, has_any_users

    with _setup_lock:
        if has_any_users(conn):
            raise HTTPException(400, "Setup already completed")
        create_user(conn, req.username, req.password, "admin", req.display_name)
    token = authenticate(conn, req.username, req.password)
    return {"token": token, "username": req.username, "role": "admin"}


# --- User management ---


@router.get("/api/users")
def list_users(request: Request, conn: DbConn) -> list[dict]:
    """List all users (admin only)."""
    _require_permission(request, "manage_users")
    from havn.engine.auth import list_users as _list_users

    return _list_users(conn)


@router.post("/api/users")
def create_user_endpoint(
    request: Request, req: CreateUserRequest, conn: DbConn
) -> dict:
    """Create a new user (admin only)."""
    admin = _require_permission(request, "manage_users")
    from havn.engine.auth import create_user

    try:
        result = create_user(conn, req.username, req.password, req.role, req.display_name)
        try:
            from havn.engine.audit import log_audit

            client_ip = request.client.host if request.client else None
            log_audit(
                conn,
                user=admin["username"],
                action="user_create",
                resource=req.username,
                detail=f"role={req.role}",
                ip_address=client_ip,
            )
        except Exception:
            pass
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/api/users/{username}")
def update_user_endpoint(
    request: Request, username: str, req: UpdateUserRequest, conn: DbConn
) -> dict:
    """Update a user (admin only)."""
    admin = _require_permission(request, "manage_users")
    from havn.engine.auth import update_user
    from havn.server.deps import invalidate_token_cache

    # A password change ends every session of that user (update_user does
    # it). An admin changing their own password keeps the session making this
    # request, so they are not signed out mid-action; their other sessions end.
    keep_token = None
    if req.password and username == admin["username"]:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            keep_token = auth_header[7:]
    try:
        found = update_user(
            conn, username, req.role, req.password, req.display_name, keep_token=keep_token
        )
        if not found:
            raise HTTPException(404, f"User '{username}' not found")
        # Clear the token cache so a role change, or the sessions a password
        # change revoked, take effect now rather than after the 30 s TTL.
        invalidate_token_cache()
        try:
            from havn.engine.audit import log_audit

            changes = []
            if req.role:
                changes.append(f"role={req.role}")
            if req.password:
                changes.append("password=changed")
            if req.display_name is not None:
                changes.append(f"display_name={req.display_name}")
            client_ip = request.client.host if request.client else None
            log_audit(
                conn,
                user=admin["username"],
                action="user_update",
                resource=username,
                detail=", ".join(changes) if changes else "no changes",
                ip_address=client_ip,
            )
        except Exception:
            pass
        return {"status": "updated"}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/api/users/{username}")
def delete_user_endpoint(request: Request, username: str, conn: DbConn) -> dict:
    """Delete a user (admin only)."""
    admin = _require_permission(request, "manage_users")
    from havn.engine.auth import delete_user
    from havn.server.deps import invalidate_token_cache

    found = delete_user(conn, username)
    if not found:
        raise HTTPException(404, f"User '{username}' not found")
    invalidate_token_cache()
    try:
        from havn.engine.audit import log_audit

        client_ip = request.client.host if request.client else None
        log_audit(
            conn,
            user=admin["username"],
            action="user_delete",
            resource=username,
            detail="user deleted",
            ip_address=client_ip,
        )
    except Exception:
        logger.debug("Failed to write audit log for user_delete", exc_info=True)
    return {"status": "deleted"}


# --- Secrets management ---


@router.get("/api/secrets")
def list_secrets(request: Request) -> list[dict]:
    """List secrets (keys and masked values only)."""
    _require_permission(request, "manage_secrets")
    from havn.engine.secrets import list_secrets as _list_secrets

    return _list_secrets(_get_project_dir())


@router.post("/api/secrets")
def set_secret(request: Request, req: SetSecretRequest, conn: DbConn) -> dict:
    """Set or update a secret."""
    admin = _require_permission(request, "manage_secrets")
    from havn.engine.secrets import set_secret as _set_secret

    _set_secret(_get_project_dir(), req.key, req.value)
    try:
        from havn.engine.audit import log_audit

        client_ip = request.client.host if request.client else None
        log_audit(
            conn,
            user=admin["username"],
            action="secret_change",
            resource=req.key,
            detail="secret set/updated",
            ip_address=client_ip,
        )
    except Exception:
        pass
    return {"status": "set", "key": req.key}


@router.delete("/api/secrets/{key}")
def delete_secret(request: Request, key: str, conn: DbConn) -> dict:
    """Delete a secret."""
    admin = _require_permission(request, "manage_secrets")
    from havn.engine.secrets import delete_secret as _delete_secret

    found = _delete_secret(_get_project_dir(), key)
    if not found:
        raise HTTPException(404, f"Secret '{key}' not found")
    try:
        from havn.engine.audit import log_audit

        client_ip = request.client.host if request.client else None
        log_audit(
            conn,
            user=admin["username"],
            action="secret_change",
            resource=key,
            detail="secret deleted",
            ip_address=client_ip,
        )
    except Exception:
        pass
    return {"status": "deleted", "key": key}
