"""Shared dependencies, helpers, and utilities for the server routes."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Annotated, Any, Generator

import duckdb
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

from havn.config import load_project
from havn.engine.database import ensure_meta_table
from havn.engine.transform import build_dag, discover_models, run_transform

logger = logging.getLogger("havn.server")


# ---------------------------------------------------------------------------
# State accessors (globals live in app.py for backward compat)
# ---------------------------------------------------------------------------


def _get_project_dir() -> Path:
    from havn.server.app import PROJECT_DIR

    return PROJECT_DIR


def _get_active_env() -> str | None:
    from havn.server.app import ACTIVE_ENV

    return ACTIVE_ENV


def _get_auth_enabled() -> bool:
    from havn.server.app import AUTH_ENABLED

    return AUTH_ENABLED


def _set_active_env(env: str) -> None:
    import havn.server.app as _app

    _app.ACTIVE_ENV = env


# ---------------------------------------------------------------------------
# Config cache
# ---------------------------------------------------------------------------

_config_cache: dict[str, Any] = {"config": None, "mtime": 0.0, "path": None}


def _get_config_cached():
    """Load project config with file-mtime-based caching."""
    active_env = _get_active_env()
    config_path = _get_project_dir() / "project.yml"
    try:
        mtime = config_path.stat().st_mtime
    except FileNotFoundError:
        return load_project(_get_project_dir(), env=active_env)

    cache_key = f"{config_path}:{active_env}"
    if (
        _config_cache["config"] is not None
        and _config_cache["path"] == cache_key
        and _config_cache["mtime"] == mtime
    ):
        return _config_cache["config"]

    config = load_project(_get_project_dir(), env=active_env)
    _config_cache["config"] = config
    _config_cache["mtime"] = mtime
    _config_cache["path"] = cache_key
    return config


def _get_config():
    return _get_config_cached()


def _clear_config_cache():
    """Clear the config cache so next call reloads from disk."""
    _config_cache["config"] = None
    _config_cache["mtime"] = 0.0
    _config_cache["path"] = None


def _get_db_path() -> Path:
    config = _get_config()
    return _get_project_dir() / config.database.path


def invalidate_config_cache() -> None:
    """Invalidate the config cache (e.g. after environment switch)."""
    _config_cache["config"] = None


# ---------------------------------------------------------------------------
# Model discovery cache
# ---------------------------------------------------------------------------

_MODEL_CACHE_VERSION = 2
_model_cache: dict[str, Any] = {
    "models": None,
    "mtime_map": None,
    "transform_dir": None,
    "version": None,
}


def _discover_models_cached(transform_dir: Path):
    """Discover models with file-mtime-based caching."""
    if not transform_dir.exists():
        return []

    current_mtimes = {}
    for sql_file in sorted(transform_dir.rglob("*.sql")):
        current_mtimes[str(sql_file)] = sql_file.stat().st_mtime

    if (
        _model_cache["models"] is not None
        and _model_cache["transform_dir"] == str(transform_dir)
        and _model_cache["mtime_map"] == current_mtimes
        and _model_cache["version"] == _MODEL_CACHE_VERSION
    ):
        return _model_cache["models"]

    models = discover_models(transform_dir)
    _model_cache["models"] = models
    _model_cache["mtime_map"] = current_mtimes
    _model_cache["transform_dir"] = str(transform_dir)
    _model_cache["version"] = _MODEL_CACHE_VERSION
    return models


# ---------------------------------------------------------------------------
# Identifier validation
# ---------------------------------------------------------------------------


def _validate_identifier(value: str, label: str = "identifier") -> str:
    """Validate that a value is a safe SQL identifier (no injection)."""
    from havn.engine.utils import validate_identifier

    try:
        return validate_identifier(value, label)
    except ValueError:
        raise HTTPException(400, f"Invalid {label}: {value!r}")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def _require_db(backend: "WarehouseBackend") -> None:
    """Raise 404 if the warehouse hasn't been initialized yet."""
    if not backend.exists():
        raise HTTPException(404, "Warehouse not found. Run a pipeline first.")


# ---------------------------------------------------------------------------
# Database dependency injection
# ---------------------------------------------------------------------------


def _get_db_resource_limits() -> tuple[str | None, int | None]:
    """Get memory_limit and threads from project config."""
    try:
        config = _get_config()
        return config.database.memory_limit, config.database.threads
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Write queue + read pool
# ---------------------------------------------------------------------------
# DuckDB allows one write connection at a time and unlimited concurrent
# readers.  The WriteQueue serializes all mutations through a single
# background thread.  The ReadPool provides separate read-only
# connections (Linux/Mac) or cursors from the write connection (Windows,
# where DuckDB only allows one connection per file).
#
# The FastAPI dependency injectors still yield cursors, so existing
# route handlers are unchanged.

import threading

from havn.engine.backends import WarehouseBackend, create_backend
from havn.engine.write_queue import WriteQueue, create_read_pool, ReadPool, SharedConnPool

_backend: WarehouseBackend | None = None
_write_queue: WriteQueue | None = None
_read_pool: ReadPool | SharedConnPool | None = None
_init_lock = threading.Lock()


def _get_backend() -> WarehouseBackend:
    """Return the active warehouse backend (singleton).

    Resolution order:
    1. ``app.state.backend_factory`` (set by havn-cloud's create_app call)
    2. ``create_backend(config.database)`` from the loaded project.yml
    """
    global _backend
    if _backend is not None:
        return _backend
    try:
        from havn.server.app import app as _app
        factory = getattr(_app.state, "backend_factory", None)
    except Exception:
        factory = None
    project_dir = _get_project_dir()
    if factory is not None:
        _backend = factory(project_dir, _get_config())
    else:
        _backend = create_backend(_get_config().database, project_dir=project_dir)
    return _backend


def _get_shared_conn(*, require_exists: bool = False) -> duckdb.DuckDBPyConnection:
    """Get the write connection (for backward compatibility).

    Prefer using ``_get_write_queue()`` or ``_get_read_pool()`` directly.
    """
    return _get_write_queue().conn


def _get_write_queue() -> WriteQueue:
    """Get or create the write queue singleton."""
    global _write_queue, _read_pool
    with _init_lock:
        if _write_queue is None or _read_pool is None:
            backend = _get_backend()
            project_dir = _get_project_dir()
            if _write_queue is None:
                _write_queue = WriteQueue(backend)
                from havn.engine.database import ensure_meta_table
                ensure_meta_table(_write_queue.conn)
                # Register user-defined macros from macros/ directory
                try:
                    from havn.engine.macros import register_macros
                    register_macros(_write_queue.conn, project_dir)
                except Exception:
                    logger.debug("Macro registration skipped (no macros/ dir or error)")
                # Clean up stale orchestration runs
                try:
                    from havn.engine.orchestration import mark_stale_runs_failed
                    mark_stale_runs_failed(_write_queue.conn)
                except Exception:
                    logger.debug("mark_stale_runs_failed skipped on startup")
            if _read_pool is None:
                _read_pool = create_read_pool(backend, _write_queue.conn)
        return _write_queue


def _get_read_pool() -> ReadPool | SharedConnPool:
    """Get the read pool, initializing if needed."""
    _get_write_queue()  # ensures both are initialized
    return _read_pool  # type: ignore[return-value]


def reregister_macros_on_shared_conns(project_dir: Path) -> None:
    """Re-register all macros on the live write connection after a macros/ file change.

    Called by the FileWatcher's macro-change callback so that edits to
    ``macros/*.py`` or ``macros/*.sql`` take effect immediately without
    restarting the server.  The operation is submitted through the WriteQueue
    so it executes on the single write thread, avoiding races with in-flight
    queries on the same connection.
    """
    global _write_queue
    wq = _write_queue
    if wq is None:
        # Server not yet initialised — nothing to refresh.
        return

    from havn.engine.macros import register_macros

    macro_logger = logging.getLogger("havn.macros")

    def _reload(conn, project_dir: Path) -> int:
        count = register_macros(conn, project_dir)
        macro_logger.info(
            "Hot-reloaded macros from %s (%d registered)", project_dir / "macros", count
        )
        return count

    try:
        future = wq.submit(_reload, project_dir, _queue_timeout=10.0)
        future.result(timeout=15.0)
    except Exception as exc:
        macro_logger.warning("Macro hot-reload failed: %s", exc)


def reset_shared_conn() -> None:
    """Close and reset connections (e.g. after DB file changes)."""
    global _backend, _write_queue, _read_pool
    with _init_lock:
        if _read_pool is not None:
            try:
                _read_pool.close()
            except Exception:
                pass
            _read_pool = None
        if _write_queue is not None:
            try:
                _write_queue.close()
            except Exception:
                pass
            _write_queue = None
        if _backend is not None:
            try:
                _backend.close()
            except Exception:
                pass
            _backend = None


def get_db() -> Generator[duckdb.DuckDBPyConnection, None, None]:
    """FastAPI dependency: yields a cursor from the write connection."""
    _require_db(_get_backend())
    wq = _get_write_queue()
    cursor = wq.cursor()
    try:
        yield cursor
    finally:
        cursor.close()


def get_db_autocreate() -> Generator[duckdb.DuckDBPyConnection, None, None]:
    """FastAPI dependency: yields a cursor, creating the database if it doesn't exist."""
    wq = _get_write_queue()
    cursor = wq.cursor()
    try:
        yield cursor
    finally:
        cursor.close()


def get_db_readonly() -> Generator[duckdb.DuckDBPyConnection, None, None]:
    """FastAPI dependency: yields a read cursor from the read pool."""
    _require_db(_get_backend())
    pool = _get_read_pool()
    with pool.connection() as cursor:
        yield cursor


def get_db_readonly_optional() -> Generator[duckdb.DuckDBPyConnection | None, None, None]:
    """FastAPI dependency: yields a read cursor, or None if DB doesn't exist."""
    if not _get_backend().exists():
        yield None
        return
    pool = _get_read_pool()
    with pool.connection() as cursor:
        yield cursor


DbConn = Annotated[duckdb.DuckDBPyConnection, Depends(get_db)]
DbConnAutoCreate = Annotated[duckdb.DuckDBPyConnection, Depends(get_db_autocreate)]
DbConnReadOnly = Annotated[duckdb.DuckDBPyConnection, Depends(get_db_readonly)]
DbConnReadOnlyOptional = Annotated[
    duckdb.DuckDBPyConnection | None, Depends(get_db_readonly_optional)
]


# ---------------------------------------------------------------------------
# Authentication & authorization
# ---------------------------------------------------------------------------


def _get_user(request: Request) -> dict | None:
    """Extract and validate user from auth header. Returns None if auth disabled."""
    if not _get_auth_enabled():
        return {"username": "local", "role": "admin", "display_name": "Local User"}

    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header[7:]
    # Use the write queue's cursor so we don't try to open a second
    # connection to the same DuckDB file with a different mode.
    wq = _get_write_queue()
    cursor = wq.cursor()
    try:
        from havn.engine.auth import validate_token

        return validate_token(cursor, token)
    finally:
        cursor.close()


def _require_user(request: Request) -> dict:
    """Require authentication. Raises 401 if not authenticated."""
    user = _get_user(request)
    if user is None:
        raise HTTPException(401, "Authentication required")
    return user


def _authenticate_websocket(websocket) -> dict | None:
    """Validate auth for a WebSocket connection.

    Checks the 'token' query parameter. Returns user dict if valid,
    or a default admin dict if auth is disabled. Returns None if invalid.
    """
    if not _get_auth_enabled():
        return {"username": "local", "role": "admin", "display_name": "Local User"}

    token = websocket.query_params.get("token", "")
    if not token:
        return None

    conn = _get_backend().connect(read_only=True)
    try:
        from havn.engine.auth import validate_token
        return validate_token(conn, token)
    finally:
        conn.close()


def _require_permission(request: Request, permission: str) -> dict:
    """Require a specific permission."""
    user = _require_user(request)
    from havn.engine.auth import has_permission

    if not has_permission(user["role"], permission):
        # Audit the permission denial
        try:
            from havn.engine.audit import log_audit

            conn = _get_shared_conn()
            cursor = conn.cursor()
            try:
                client_ip = request.client.host if request.client else None
                log_audit(
                    cursor,
                    user=user["username"],
                    action="permission_denied",
                    resource=str(request.url.path),
                    detail=f"Required: {permission}, role: {user['role']}",
                    ip_address=client_ip,
                )
            finally:
                cursor.close()
        except Exception:
            pass  # don't break auth flow on audit failure
        raise HTTPException(403, f"Permission denied: {permission}")
    return user


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

_login_attempts: dict[str, list[float]] = {}
_RATE_LIMIT_WINDOW = 60.0
_RATE_LIMIT_MAX = 5
_RATE_LIMIT_MAX_KEYS = 10_000


def _check_rate_limit(key: str) -> None:
    """Enforce rate limiting. Raises 429 if too many attempts."""
    now = time.time()
    attempts = _login_attempts.get(key, [])
    attempts = [t for t in attempts if now - t < _RATE_LIMIT_WINDOW]
    if len(attempts) >= _RATE_LIMIT_MAX:
        logger.warning("Rate limit exceeded for %s", key)
        raise HTTPException(429, "Too many login attempts. Try again later.")
    attempts.append(now)
    _login_attempts[key] = attempts
    if len(_login_attempts) > _RATE_LIMIT_MAX_KEYS:
        stale = [
            k
            for k, v in _login_attempts.items()
            if not v or now - v[-1] > _RATE_LIMIT_WINDOW
        ]
        for k in stale:
            del _login_attempts[k]


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def _serialize(value: Any) -> Any:
    """Make values JSON-serializable."""
    if value is None:
        return None
    if isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


def _detect_language(path: Path) -> str:
    return {
        "sql": "sql",
        "py": "python",
        "yml": "yaml",
        "yaml": "yaml",
        "dpnb": "json",
    }.get(path.suffix.lstrip("."), "text")
