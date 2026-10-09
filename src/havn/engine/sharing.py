"""Published dashboards: share links, their identities, and the viewer-safe definition.

A dashboard is published through one or more *shares* in
``_havn.dashboard_shares``:

* ``signed_in`` -- a link (``/p/<share id>``) anyone with a havn account and
  read permission can open. Widgets run as the viewer, so the viewer's own
  masking applies.
* ``public`` -- an unguessable token (``/p/<token>``) that works without an
  account, optionally expiring, revocable, and created by admins only. It
  runs as an explicit "view as" identity: a havn user (whose *current* role
  is read on every request) or a bare role.

Only the token's SHA-256 is stored, the same way session tokens are, so the
link is shown once when it is created. A viewer never receives SQL: the
definition served to published pages (:func:`viewer_definition`) drops
widget queries and filter option queries, and the published query endpoint
runs only the saved queries of the dashboard the share points at.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

import duckdb

from havn.engine.governed_query import QueryIdentity

logger = logging.getLogger("havn.engine.sharing")

SHARE_MODES = ("signed_in", "public")
VIEW_AS_ROLES = ("viewer", "editor", "admin")


class ShareError(Exception):
    """A share could not be created or resolved. ``status_code`` follows HTTP."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def ensure_sharing_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Create ``_havn.dashboard_shares`` if it does not exist (lazy bootstrap)."""
    from havn.engine.database import _is_ducklake_connection, _strip_pk

    is_lake = _is_ducklake_connection(conn)
    conn.execute("CREATE SCHEMA IF NOT EXISTS _havn")
    conn.execute(_strip_pk("""
        CREATE TABLE IF NOT EXISTS _havn.dashboard_shares (
            id             VARCHAR PRIMARY KEY,
            dashboard_id   VARCHAR NOT NULL,
            mode           VARCHAR NOT NULL,
            token_hash     VARCHAR,
            token_hint     VARCHAR,
            view_as_user   VARCHAR,
            view_as_role   VARCHAR,
            label          VARCHAR DEFAULT '',
            expires_at     TIMESTAMP,
            revoked_at     TIMESTAMP,
            revoked_by     VARCHAR,
            created_by     VARCHAR NOT NULL,
            created_at     TIMESTAMP DEFAULT current_timestamp,
            last_viewed_at TIMESTAMP,
            view_count     BIGINT DEFAULT 0
        )
    """, is_lake))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _row_to_share(row: tuple) -> dict:
    (sid, dashboard_id, mode, _token_hash, token_hint, view_as_user, view_as_role,
     label, expires_at, revoked_at, revoked_by, created_by, created_at,
     last_viewed_at, view_count) = row
    return {
        "id": sid,
        "dashboard_id": dashboard_id,
        "mode": mode,
        "token_hint": token_hint,
        "view_as_user": view_as_user,
        "view_as_role": view_as_role,
        "label": label or "",
        "expires_at": _iso(expires_at),
        "revoked_at": _iso(revoked_at),
        "revoked_by": revoked_by,
        "created_by": created_by,
        "created_at": _iso(created_at),
        "last_viewed_at": _iso(last_viewed_at),
        "view_count": int(view_count or 0),
        "status": _status(expires_at, revoked_at),
    }


_SHARE_COLUMNS = (
    "id, dashboard_id, mode, token_hash, token_hint, view_as_user, view_as_role, "
    "label, expires_at, revoked_at, revoked_by, created_by, created_at, "
    "last_viewed_at, view_count"
)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    return str(value)


def _now() -> _dt.datetime:
    # DuckDB's current_timestamp is TIMESTAMPTZ; the column is a naive
    # TIMESTAMP written from Python in local time, so compare in local time.
    return _dt.datetime.now().replace(microsecond=0)


def _status(expires_at: Any, revoked_at: Any) -> str:
    if revoked_at is not None:
        return "revoked"
    if expires_at is not None and isinstance(expires_at, _dt.datetime) and expires_at <= _now():
        return "expired"
    return "active"


def _user_role(conn: duckdb.DuckDBPyConnection, username: str) -> str | None:
    try:
        row = conn.execute(
            "SELECT role FROM _havn.users WHERE username = ?", [username]
        ).fetchone()
    except duckdb.CatalogException:
        return None
    return row[0] if row else None


def create_share(
    conn: duckdb.DuckDBPyConnection,
    *,
    dashboard_id: str,
    mode: str,
    created_by: str,
    label: str = "",
    expires_at: _dt.datetime | None = None,
    view_as_user: str | None = None,
    view_as_role: str | None = None,
    max_public_days: int | None = None,
) -> tuple[dict, str | None]:
    """Create a share. Returns ``(share, token)``; ``token`` is set for public links only.

    The plaintext token is not stored and cannot be recovered later.
    """
    ensure_sharing_tables(conn)
    if mode not in SHARE_MODES:
        raise ShareError(400, f"mode must be one of: {', '.join(SHARE_MODES)}")
    if not conn.execute("SELECT 1 FROM _havn.dashboards WHERE id = ?", [dashboard_id]).fetchone():
        raise ShareError(404, "Dashboard not found")
    if expires_at is not None and expires_at <= _now():
        raise ShareError(400, "expires_at must be in the future")

    token: str | None = None
    token_hash = token_hint = None
    if mode == "public":
        if bool(view_as_user) == bool(view_as_role):
            raise ShareError(400, "A public link needs exactly one of view_as_user or view_as_role")
        if view_as_role and view_as_role not in VIEW_AS_ROLES:
            raise ShareError(400, f"view_as_role must be one of: {', '.join(VIEW_AS_ROLES)}")
        if view_as_user and _user_role(conn, view_as_user) is None:
            raise ShareError(400, f"User '{view_as_user}' does not exist")
        if max_public_days:
            cap = _now() + _dt.timedelta(days=max_public_days)
            if expires_at is None or expires_at > cap:
                raise ShareError(
                    400, f"Public links must expire within {max_public_days} days (sharing.max_public_link_days)"
                )
        token = secrets.token_urlsafe(32)
        token_hash = hash_token(token)
        token_hint = token[-4:]
    else:
        # Signed-in links run as whoever opens them.
        view_as_user = view_as_role = None

    share_id = "s" + secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")
    conn.execute(
        """
        INSERT INTO _havn.dashboard_shares
            (id, dashboard_id, mode, token_hash, token_hint, view_as_user,
             view_as_role, label, expires_at, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [share_id, dashboard_id, mode, token_hash, token_hint, view_as_user,
         view_as_role, label or "", expires_at, created_by, _now()],
    )
    return get_share(conn, share_id), token


def get_share(conn: duckdb.DuckDBPyConnection, share_id: str) -> dict | None:
    try:
        row = conn.execute(
            f"SELECT {_SHARE_COLUMNS} FROM _havn.dashboard_shares WHERE id = ?", [share_id]
        ).fetchone()
    except duckdb.CatalogException:
        return None
    return _row_to_share(row) if row else None


def list_shares(conn: duckdb.DuckDBPyConnection, dashboard_id: str | None = None) -> list[dict]:
    try:
        if dashboard_id:
            rows = conn.execute(
                f"SELECT {_SHARE_COLUMNS} FROM _havn.dashboard_shares "
                "WHERE dashboard_id = ? ORDER BY created_at DESC",
                [dashboard_id],
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_SHARE_COLUMNS} FROM _havn.dashboard_shares ORDER BY created_at DESC"
            ).fetchall()
    except duckdb.CatalogException:
        return []
    return [_row_to_share(r) for r in rows]


def revoke_share(conn: duckdb.DuckDBPyConnection, share_id: str, revoked_by: str) -> dict | None:
    ensure_sharing_tables(conn)
    conn.execute(
        "UPDATE _havn.dashboard_shares SET revoked_at = ?, revoked_by = ? "
        "WHERE id = ? AND revoked_at IS NULL",
        [_now(), revoked_by, share_id],
    )
    return get_share(conn, share_id)


def update_share_expiry(
    conn: duckdb.DuckDBPyConnection, share_id: str, expires_at: _dt.datetime | None,
    max_public_days: int | None = None,
) -> dict | None:
    share = get_share(conn, share_id)
    if share is None:
        return None
    if share["status"] == "revoked":
        raise ShareError(409, "A revoked link cannot be changed; create a new one")
    if expires_at is not None and expires_at <= _now():
        raise ShareError(400, "expires_at must be in the future")
    if share["mode"] == "public" and max_public_days:
        cap = _now() + _dt.timedelta(days=max_public_days)
        if expires_at is None or expires_at > cap:
            raise ShareError(400, f"Public links must expire within {max_public_days} days")
    conn.execute(
        "UPDATE _havn.dashboard_shares SET expires_at = ? WHERE id = ?", [expires_at, share_id]
    )
    return get_share(conn, share_id)


def delete_shares_for_dashboard(conn: duckdb.DuckDBPyConnection, dashboard_id: str) -> int:
    try:
        rows = conn.execute(
            "DELETE FROM _havn.dashboard_shares WHERE dashboard_id = ? RETURNING id", [dashboard_id]
        ).fetchall()
    except duckdb.CatalogException:
        return 0
    return len(rows)


@dataclass
class ResolvedShare:
    share: dict
    identity: QueryIdentity | None  # None for signed-in shares: the caller supplies the viewer


def resolve_share(
    conn: duckdb.DuckDBPyConnection,
    key: str,
    *,
    public_links_enabled: bool = True,
) -> ResolvedShare:
    """Find the active share for a ``/p/<key>`` link.

    ``key`` is a public token (looked up by hash among public shares) or a
    signed-in share id (looked up among signed-in shares). A public token
    therefore never matches a signed-in share and vice versa.

    Raises :class:`ShareError`: 404 unknown, 410 expired or revoked, 403
    when public links are disabled or the "view as" user no longer exists.
    """
    if not key or len(key) > 200:
        raise ShareError(404, "Link not found")
    try:
        row = conn.execute(
            f"SELECT {_SHARE_COLUMNS} FROM _havn.dashboard_shares "
            "WHERE mode = 'public' AND token_hash = ?",
            [hash_token(key)],
        ).fetchone()
        if row is None:
            row = conn.execute(
                f"SELECT {_SHARE_COLUMNS} FROM _havn.dashboard_shares "
                "WHERE mode = 'signed_in' AND id = ?",
                [key],
            ).fetchone()
    except duckdb.CatalogException:
        row = None
    if row is None:
        raise ShareError(404, "Link not found")
    share = _row_to_share(row)
    if share["status"] == "revoked":
        raise ShareError(410, "This link has been revoked")
    if share["status"] == "expired":
        raise ShareError(410, "This link has expired")
    if not conn.execute(
        "SELECT 1 FROM _havn.dashboards WHERE id = ?", [share["dashboard_id"]]
    ).fetchone():
        raise ShareError(404, "The dashboard behind this link no longer exists")

    if share["mode"] == "signed_in":
        return ResolvedShare(share=share, identity=None)

    if not public_links_enabled:
        raise ShareError(403, "Public links are disabled for this project")
    if share["view_as_user"]:
        role = _user_role(conn, share["view_as_user"])
        if role is None:
            raise ShareError(403, "This link's view-as user no longer exists")
        identity = QueryIdentity(username=share["view_as_user"], role=role, source="share")
    else:
        role = share["view_as_role"]
        if role not in VIEW_AS_ROLES:
            raise ShareError(403, "This link has no valid view-as identity")
        identity = QueryIdentity(username=f"share:{share['id']}", role=role, source="share")
    return ResolvedShare(share=share, identity=identity)


def record_view(conn: duckdb.DuckDBPyConnection, share_id: str) -> None:
    """Best-effort view counter (failures never break a page view)."""
    try:
        conn.execute(
            "UPDATE _havn.dashboard_shares SET view_count = COALESCE(view_count, 0) + 1, "
            "last_viewed_at = ? WHERE id = ?",
            [_now(), share_id],
        )
    except Exception:
        logger.debug("Failed to record share view", exc_info=True)


# ---------------------------------------------------------------------------
# Viewer-safe definition
# ---------------------------------------------------------------------------

_SAFE_FILTER_KEYS = ("id", "label", "type", "column", "placeholder", "options", "default")
_SAFE_PARAM_KEYS = ("name", "label", "type", "default", "options", "placeholder")


def _strip_sql(obj: Any) -> Any:
    """Drop any key that could carry SQL from nested widget config."""
    if isinstance(obj, dict):
        return {
            k: _strip_sql(v)
            for k, v in obj.items()
            if not (isinstance(k, str) and ("sql" in k.lower() or k.lower() in ("query", "queries")))
        }
    if isinstance(obj, list):
        return [_strip_sql(v) for v in obj]
    return obj


def viewer_definition(dashboard: dict) -> dict:
    """The dashboard as a published page sees it: layout and labels, no SQL."""
    from havn.engine.dashboard_queries import runs_query

    filters = []
    for f in dashboard.get("filters") or []:
        if not isinstance(f, dict) or not f.get("column"):
            continue
        safe = {k: f[k] for k in _SAFE_FILTER_KEYS if k in f}
        safe["has_options"] = bool(f.get("options_sql") or f.get("options"))
        filters.append(safe)
    settings = dashboard.get("settings") or {}
    params = [
        {k: p[k] for k in _SAFE_PARAM_KEYS if k in p}
        for p in settings.get("parameters") or []
        if isinstance(p, dict) and p.get("name")
    ]
    widgets = []
    for w in dashboard.get("widgets") or []:
        config = _strip_sql(w.get("config") or {})
        if w.get("widget_type") == "text" and not config.get("content") and w.get("sql_query"):
            # Text widgets fall back to sql_query for their markdown.
            config["content"] = w["sql_query"]
        widgets.append({
            "id": w["id"],
            "widget_type": w.get("widget_type"),
            "chart_type": w.get("chart_type"),
            "title": w.get("title") or "",
            "config": config,
            "position": w.get("position") or {},
            "sort_order": w.get("sort_order") or 0,
            "has_query": runs_query(w),
        })
    return {
        "id": dashboard["id"],
        "name": dashboard.get("name") or "",
        "description": dashboard.get("description") or "",
        "layout": dashboard.get("layout") or {},
        "filters": filters,
        "settings": {
            "parameters": params,
            "pages": [
                {"id": str(p.get("id")), "name": str(p.get("name") or "")}
                for p in settings.get("pages") or []
                if isinstance(p, dict) and p.get("id")
            ],
        },
        "widgets": widgets,
    }


# ---------------------------------------------------------------------------
# Rate limiting for unauthenticated published endpoints
# ---------------------------------------------------------------------------

_RATE_WINDOW_S = 60.0
_RATE_MAX = 240  # requests per window per (share, client)
_rate_lock = threading.Lock()
_rate_hits: dict[tuple[str, str], list[float]] = {}


def check_rate_limit(share_id: str, client: str) -> bool:
    """True if the request is within budget. A page load is ~2 requests per widget batch."""
    now = time.monotonic()
    key = (share_id, client)
    with _rate_lock:
        hits = [t for t in _rate_hits.get(key, []) if now - t < _RATE_WINDOW_S]
        allowed = len(hits) < _RATE_MAX
        if allowed:
            hits.append(now)
        _rate_hits[key] = hits
        if len(_rate_hits) > 10_000:
            for k in [k for k, v in _rate_hits.items() if not v or now - v[-1] > _RATE_WINDOW_S]:
                _rate_hits.pop(k, None)
    return allowed


def reset_rate_limits() -> None:
    with _rate_lock:
        _rate_hits.clear()
