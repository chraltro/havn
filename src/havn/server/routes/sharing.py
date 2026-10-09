"""Published dashboards: share-link management and the read-only published API.

Management (``/api/dashboards/{id}/shares``, ``/api/shares/...``) needs an
account: editors publish signed-in links, only admins create or change public
links because those expose data to people without an account.

The published API (``/api/published/{key}``) serves the page at ``/p/<key>``.
It never accepts SQL: it runs the saved widget queries (and saved filter
option queries) of the dashboard the share points at, through the shared
governed read path, as the viewer (signed-in links) or the link's "view as"
identity (public links). Filter and parameter values are validated against
the dashboard's declared filters and bound as query parameters.
"""

from __future__ import annotations

import datetime as _dt
import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from havn.server.deps import (
    DbConn,
    DbConnReadOnly,
    _get_config,
    _require_permission,
)

logger = logging.getLogger("havn.server")

router = APIRouter()

# Admin-only, same gate as masking policy management: a public link hands
# warehouse data to people without an account.
MANAGE_PUBLIC_LINKS_PERMISSION = "manage_users"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ShareCreate(BaseModel):
    mode: str = Field(..., pattern=r"^(signed_in|public)$")
    label: str = Field(default="", max_length=200)
    expires_at: _dt.datetime | None = None
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    view_as_user: str | None = Field(default=None, max_length=200)
    view_as_role: str | None = Field(default=None, pattern=r"^(viewer|editor|admin)$")


class ShareUpdate(BaseModel):
    expires_at: _dt.datetime | None = None
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    clear_expiry: bool = False


class PublishedQuery(BaseModel):
    filters: dict = Field(default_factory=dict)
    parameters: dict = Field(default_factory=dict)
    widget_ids: list[str] | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sharing_config():
    try:
        return _get_config().sharing
    except Exception:
        from havn.config import SharingConfig

        return SharingConfig()


def _to_local_naive(value: _dt.datetime | None) -> _dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value.replace(microsecond=0)


def _expiry(expires_at: _dt.datetime | None, expires_in_days: int | None) -> _dt.datetime | None:
    if expires_in_days:
        return (_dt.datetime.now() + _dt.timedelta(days=expires_in_days)).replace(microsecond=0)
    return _to_local_naive(expires_at)


def _audit(conn, request: Request, user: dict, action: str, resource: str, detail: str) -> None:
    try:
        from havn.engine.audit import log_audit

        log_audit(
            conn,
            user=user.get("username", "anonymous"),
            action=action,
            resource=resource,
            detail=detail,
            ip_address=request.client.host if request.client else None,
        )
    except Exception:
        logger.debug("Audit write failed for %s", action, exc_info=True)


def _share_response(share: dict, token: str | None = None) -> dict:
    key = token if token else (share["id"] if share["mode"] == "signed_in" else None)
    out = dict(share)
    # The path is only known for signed-in links (their id is the key) and,
    # for a public link, at the moment it is created.
    out["path"] = f"/p/{key}" if key else None
    if token:
        out["token"] = token
    return out


def _share_error(e) -> HTTPException:
    return HTTPException(e.status_code, str(e))


def _write_cursor():
    from havn.engine.write_queue import cursor_for
    from havn.server.deps import _get_shared_conn

    return cursor_for(_get_shared_conn())


# ---------------------------------------------------------------------------
# Management
# ---------------------------------------------------------------------------


@router.get("/api/dashboards/{dashboard_id}/shares")
def list_dashboard_shares(request: Request, dashboard_id: str, conn: DbConn) -> list[dict]:
    """List a dashboard's share links (active, expired and revoked)."""
    _require_permission(request, "write")
    from havn.engine.sharing import list_shares

    return [_share_response(s) for s in list_shares(conn, dashboard_id)]


@router.get("/api/shares")
def list_all_shares(request: Request, conn: DbConn) -> list[dict]:
    """List every share link in the project, newest first."""
    _require_permission(request, "write")
    from havn.engine.sharing import list_shares

    shares = list_shares(conn)
    try:
        names = dict(conn.execute("SELECT id, name FROM _havn.dashboards").fetchall())
    except Exception:
        names = {}
    out = []
    for s in shares:
        r = _share_response(s)
        r["dashboard_name"] = names.get(s["dashboard_id"])
        out.append(r)
    return out


@router.post("/api/dashboards/{dashboard_id}/shares")
def create_dashboard_share(
    request: Request, dashboard_id: str, req: ShareCreate, conn: DbConn
) -> dict:
    """Publish a dashboard: create a signed-in link (editors) or a public link (admins)."""
    from havn.engine.sharing import ShareError, create_share

    if req.mode == "public":
        user = _require_permission(request, MANAGE_PUBLIC_LINKS_PERMISSION)
        cfg = _sharing_config()
        if not cfg.public_links:
            raise HTTPException(403, "Public links are disabled for this project (sharing.public_links)")
        max_days = cfg.max_public_link_days
    else:
        user = _require_permission(request, "write")
        max_days = None
    try:
        share, token = create_share(
            conn,
            dashboard_id=dashboard_id,
            mode=req.mode,
            created_by=user.get("username", "anonymous"),
            label=req.label,
            expires_at=_expiry(req.expires_at, req.expires_in_days),
            view_as_user=req.view_as_user or None,
            view_as_role=req.view_as_role or None,
            max_public_days=max_days,
        )
    except ShareError as e:
        raise _share_error(e)
    who = share["view_as_user"] or (f"role:{share['view_as_role']}" if share["view_as_role"] else "viewer")
    _audit(
        conn, request, user, "dashboard_publish", f"dashboard={dashboard_id}",
        f"share={share['id']} mode={share['mode']} view_as={who} expires={share['expires_at'] or 'never'}",
    )
    out = _share_response(share, token)
    if out["path"]:
        from havn.engine.embed import embed_snippet

        try:
            base = _get_config().reports.base_url
        except Exception:
            base = None
        url = (base or str(request.base_url)).rstrip("/") + out["path"]
        out["url"] = url
        name = conn.execute("SELECT name FROM _havn.dashboards WHERE id = ?", [dashboard_id]).fetchone()
        out["embed_html"] = embed_snippet(url, name[0] if name else "havn dashboard")
    return out


def _load_share_for_change(request: Request, conn, share_id: str) -> tuple[dict, dict]:
    from havn.engine.sharing import get_share

    share = get_share(conn, share_id)
    if share is None:
        # Check the caller can manage links before saying whether it exists.
        _require_permission(request, "write")
        raise HTTPException(404, "Share not found")
    perm = MANAGE_PUBLIC_LINKS_PERMISSION if share["mode"] == "public" else "write"
    user = _require_permission(request, perm)
    return share, user


@router.patch("/api/shares/{share_id}")
def update_share(request: Request, share_id: str, req: ShareUpdate, conn: DbConn) -> dict:
    """Change a link's expiry (or remove it)."""
    from havn.engine.sharing import ShareError, update_share_expiry

    share, user = _load_share_for_change(request, conn, share_id)
    expires = None if req.clear_expiry else _expiry(req.expires_at, req.expires_in_days)
    if not req.clear_expiry and expires is None:
        raise HTTPException(400, "Give expires_at, expires_in_days or clear_expiry")
    max_days = _sharing_config().max_public_link_days if share["mode"] == "public" else None
    try:
        updated = update_share_expiry(conn, share_id, expires, max_public_days=max_days)
    except ShareError as e:
        raise _share_error(e)
    _audit(
        conn, request, user, "dashboard_publish", f"dashboard={share['dashboard_id']}",
        f"share={share_id} expiry changed to {updated['expires_at'] or 'never'}",
    )
    return _share_response(updated)


@router.delete("/api/shares/{share_id}")
def revoke_share_endpoint(request: Request, share_id: str, conn: DbConn) -> dict:
    """Revoke a link. It stops working immediately; the record is kept for the audit trail."""
    from havn.engine.sharing import revoke_share

    share, user = _load_share_for_change(request, conn, share_id)
    revoked = revoke_share(conn, share_id, user.get("username", "anonymous"))
    if share["status"] != "revoked":
        _audit(
            conn, request, user, "dashboard_unpublish", f"dashboard={share['dashboard_id']}",
            f"share={share_id} mode={share['mode']}",
        )
    return _share_response(revoked)


# ---------------------------------------------------------------------------
# Published (read-only) API
# ---------------------------------------------------------------------------


def _freshness_for(share: dict, freshness: dict) -> dict:
    """Public viewers get the timestamps only; model and table names stay internal."""
    if share["mode"] != "public":
        return freshness
    return {
        "as_of": freshness.get("as_of"),
        "newest": freshness.get("newest"),
        "models": [],
        "unknown": [],
        "model_count": len(freshness.get("models") or []),
    }


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"


def _resolve(request: Request, conn, key: str):
    """Resolve a /p/<key> link to (share, identity), enforcing auth for signed-in links."""
    from havn.engine.governed_query import QueryIdentity
    from havn.engine.sharing import ShareError, check_rate_limit, resolve_share

    try:
        resolved = resolve_share(conn, key, public_links_enabled=_sharing_config().public_links)
    except ShareError as e:
        raise _share_error(e)
    share = resolved.share
    client = request.client.host if request.client else "unknown"
    if not check_rate_limit(share["id"], client):
        raise HTTPException(429, "Too many requests for this link. Try again in a minute.")
    if resolved.identity is not None:
        return share, resolved.identity
    user = _require_permission(request, "read")
    return share, QueryIdentity.from_user(user, source="share")


def _validated_inputs(dashboard: dict, filters: dict, parameters: dict) -> tuple[dict, dict, dict]:
    """Keep only declared filters/parameters, validated against their declared types."""
    from havn.engine.dashboard_queries import FilterValueError, validate_viewer_inputs

    try:
        return validate_viewer_inputs(dashboard, filters, parameters)
    except FilterValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/published/{key}")
def get_published(request: Request, response: Response, key: str, conn: DbConnReadOnly) -> dict:
    """The published page's definition: layout, labels, filters, freshness. No SQL."""
    from havn.engine.dashboard_queries import dashboard_freshness, load_dashboard
    from havn.engine.sharing import record_view, viewer_definition

    _no_store(response)
    share, identity = _resolve(request, conn, key)
    dashboard = load_dashboard(conn, share["dashboard_id"])
    if dashboard is None:
        raise HTTPException(404, "The dashboard behind this link no longer exists")
    cur = _write_cursor()
    try:
        record_view(cur, share["id"])
    finally:
        cur.close()
    return {
        "dashboard": viewer_definition(dashboard),
        "share": {
            "mode": share["mode"],
            "label": share["label"],
            "expires_at": share["expires_at"],
        },
        "viewer": {
            "username": identity.username if share["mode"] == "signed_in" else None,
        },
        "freshness": _freshness_for(share, dashboard_freshness(conn, dashboard["widgets"])),
    }


@router.post("/api/published/{key}/query")
def query_published(
    request: Request, response: Response, key: str, req: PublishedQuery, conn: DbConnReadOnly
) -> dict:
    """Run the dashboard's saved widget queries with the viewer's filter values."""
    from havn.engine.dashboard_queries import (
        FilterValueError,
        dashboard_freshness,
        load_dashboard,
        run_widget_query,
        runs_query,
    )
    from havn.engine.governed_query import GovernedQueryError
    from havn.server.routes.dashboards import _cache_key, _store_cache

    _no_store(response)
    share, identity = _resolve(request, conn, key)
    dashboard = load_dashboard(conn, share["dashboard_id"])
    if dashboard is None:
        raise HTTPException(404, "The dashboard behind this link no longer exists")
    filters, params, types = _validated_inputs(dashboard, req.filters, req.parameters)
    wanted = set(req.widget_ids) if req.widget_ids else None
    public = share["mode"] == "public"

    results: dict = {}
    for w in dashboard["widgets"]:
        if not runs_query(w) or (wanted is not None and w["id"] not in wanted):
            continue
        sql = w["sql_query"]
        ttl = int(w.get("cache_ttl") or 0)
        ck = _cache_key(w["id"], filters, params, sql, identity.cache_key()) if ttl > 0 else None
        if ck:
            try:
                cached = conn.execute(
                    "SELECT result_json FROM _havn.dashboard_cache "
                    "WHERE cache_key = ? AND expires_at > current_timestamp",
                    [ck],
                ).fetchone()
            except Exception:
                cached = None
            if cached:
                import json as _json

                r = cached[0]
                results[w["id"]] = _json.loads(r) if isinstance(r, str) else r
                continue
        try:
            r = run_widget_query(
                conn, sql, identity, filters=filters, parameters=params, filter_types=types,
            )
            result = {k: r[k] for k in ("columns", "column_types", "rows", "row_count", "truncated")}
            if ck:
                _store_cache(ck, result, ttl)
        except (GovernedQueryError, FilterValueError) as e:
            logger.info("Published widget %s failed for share %s: %s", w["id"], share["id"], e)
            # Public viewers get no detail: messages can name tables, columns
            # and masking rules the page does not otherwise reveal.
            message = "This widget could not be loaded." if public else str(e)
            if getattr(e, "status_code", 400) == 408:
                message = "This widget took too long to load."
            result = {"columns": [], "rows": [], "row_count": 0, "error": message}
        results[w["id"]] = result
    return {
        "results": results,
        "freshness": _freshness_for(share, dashboard_freshness(conn, dashboard["widgets"])),
    }


@router.post("/api/published/{key}/filters/{filter_id}/options")
def published_filter_options(
    request: Request, response: Response, key: str, filter_id: str, conn: DbConnReadOnly
) -> dict:
    """Options for a dropdown/multi-select filter, from its saved options query."""
    from havn.engine.dashboard_queries import load_dashboard
    from havn.engine.governed_query import GovernedQueryError, run_governed_query

    _no_store(response)
    share, identity = _resolve(request, conn, key)
    dashboard = load_dashboard(conn, share["dashboard_id"])
    if dashboard is None:
        raise HTTPException(404, "The dashboard behind this link no longer exists")
    f = next(
        (f for f in dashboard["filters"] if isinstance(f, dict) and f.get("id") == filter_id),
        None,
    )
    if f is None:
        raise HTTPException(404, "Filter not found")
    if f.get("options"):
        return {"options": [str(o) for o in f["options"]][:1000]}
    sql = (f.get("options_sql") or "").strip()
    if not sql:
        return {"options": []}
    try:
        r = run_governed_query(
            conn, sql, identity, row_cap=1000, timeout_s=30, task_label="share:filter-options",
        )
    except GovernedQueryError as e:
        logger.info("Filter options failed for share %s: %s", share["id"], e)
        raise HTTPException(400, "Filter options could not be loaded")
    return {"options": ["" if row[0] is None else str(row[0]) for row in r["rows"] if row]}
