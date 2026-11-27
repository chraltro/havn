"""FastAPI backend for the web UI.

This module creates the FastAPI application and assembles all route modules.
Route handlers are defined in havn.server.routes.* submodules.
Shared dependencies (DB injection, auth, caching) live in havn.server.deps.
"""

from __future__ import annotations

import logging
import re

from pathlib import Path, PurePosixPath, PureWindowsPath

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response

from havn import setup_logging

setup_logging()

# ---------------------------------------------------------------------------
# Global state — set by CLI before starting uvicorn.
# Tests also set these directly via `import havn.server.app as server_app`.
# deps.py reads these lazily via function-level imports to avoid circular deps.
# ---------------------------------------------------------------------------

PROJECT_DIR: Path = Path.cwd()
AUTH_ENABLED: bool = False  # Set by CLI --auth flag
ACTIVE_ENV: str | None = None  # Set by CLI --env flag

# ---------------------------------------------------------------------------
# Create the FastAPI application
# ---------------------------------------------------------------------------

_maintenance = None


logger = logging.getLogger("havn.server")


def _close_out_interrupted_work() -> None:
    """Mark builds and deploys a previous server left "running" as interrupted."""
    try:
        from havn.engine.deploy import mark_interrupted_deploys
        from havn.engine.pr import mark_interrupted_builds
        from havn.engine.write_queue import cursor_for
        from havn.server.deps import _get_shared_conn

        cur = cursor_for(_get_shared_conn())
        try:
            builds, deploys = mark_interrupted_builds(cur), mark_interrupted_deploys(cur)
        finally:
            cur.close()
        if builds or deploys:
            logger.warning(
                "Marked %d build(s) and %d deploy(s) as interrupted by the last shutdown", builds, deploys
            )
    except Exception as e:
        # No warehouse yet, or read-only: nothing to close out.
        logger.debug("Skipped closing out interrupted work: %s", e)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Server lifecycle hooks.

    On startup, start the DuckLake maintenance scheduler if the configured
    backend is DuckLake (no-op for DuckDB).

    On shutdown, stop the streaming flush worker and the maintenance loop.
    """
    global _maintenance
    _close_out_interrupted_work()
    try:
        from havn.engine.streaming.maintenance import MaintenanceScheduler
        from havn.engine.write_queue import cursor_for
        from havn.server.deps import _get_backend, _get_shared_conn

        backend = _get_backend()
        # Reuse the shared write connection: DuckLake with a local-file
        # catalog rejects a second ATTACH from the same process, so a
        # fresh backend.connect() inside the scheduler thread fails. The
        # maintenance ops are catalog writes; running them on a cursor of
        # the shared write conn is both correct and conflict-free.
        _maintenance = MaintenanceScheduler(
            connection_factory=lambda: cursor_for(_get_shared_conn()),
            backend_name=backend.name,
        )
        _maintenance.start()
    except Exception:
        _maintenance = None

    # Live models: refresh continuously from streaming ingest. Started only
    # when the project has a live model (and live.enabled is not false); a
    # project without one never pays for the thread.
    try:
        from havn.server.routes.live import start_live_runner

        start_live_runner()
    except Exception as e:
        logger.warning("Live runner not started: %s", e)

    yield

    try:
        from havn.server.routes.live import stop_live_runner

        stop_live_runner()
    except Exception:
        pass
    try:
        from havn.server.routes.streaming import shutdown_flush_worker
        shutdown_flush_worker()
    except Exception:
        pass
    if _maintenance is not None:
        try:
            _maintenance.stop()
        except Exception:
            pass


from havn import __version__ as _havn_version

app = FastAPI(title="havn", version=_havn_version, lifespan=_lifespan)

# Added first so it is the innermost middleware: it must see a route's own
# body messages, before the @app.middleware("http") layers re-send every body
# as a stream. It gzips complete API bodies only; streams (SSE, NDJSON, CSV,
# downloads) pass through untouched. See havn.server.compression.
from havn.server.compression import ApiGzipMiddleware  # noqa: E402

app.add_middleware(ApiGzipMiddleware, prefix="/api/")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    # "0" disables the legacy XSS auditor, which could itself be abused to
    # leak data from a page (OWASP Secure Headers guidance).
    response.headers["X-XSS-Protection"] = "0"
    if request.url.path.startswith(_PUBLISHED_PAGE_PREFIX):
        # Published dashboard pages (/p/<key>) are the one document that may
        # be framed, and only by the origins in sharing.embed.allowed_origins.
        # The key in the URL is a credential for public links, so it must not
        # leak through Referer, and the page must not be indexed.
        response.headers["Content-Security-Policy"] = _published_frame_policy()
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Cache-Control"] = "no-store"
    else:
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


_PUBLISHED_PAGE_PREFIX = "/p/"


def _published_frame_policy() -> str:
    """CSP frame-ancestors for published pages from sharing.embed.allowed_origins."""
    from havn.engine.embed import frame_ancestors_policy

    try:
        from havn.server.deps import _get_config

        origins = _get_config().sharing.embed.allowed_origins
    except Exception:
        origins = []
    return frame_ancestors_policy(origins)

# ---------------------------------------------------------------------------
# OpenTelemetry: one span per API request, when telemetry.opentelemetry is on
# ---------------------------------------------------------------------------

@app.middleware("http")
async def otel_request_spans(request, call_next):
    from havn.server.tracing import request_tracer, traced_request

    tracer = request_tracer()
    if tracer is None:
        return await call_next(request)
    return await traced_request(tracer, request, call_next)

# ---------------------------------------------------------------------------
# Memory management: periodic checkpoint to release DuckDB buffers
# ---------------------------------------------------------------------------

import time as _time
import threading as _threading

_last_checkpoint = _time.time()
_checkpoint_lock = _threading.Lock()
_checkpoint_running = False
_CHECKPOINT_INTERVAL = 60  # seconds between checkpoints


def _force_checkpoint() -> None:
    """Run FORCE CHECKPOINT on a cursor of the shared write connection.

    Uses deps.background_write_cursor(), so a connection reset (environment
    switch, backup restore, branch switch, database settings) cannot close
    the connection while the checkpoint uses it, and the checkpoint never
    opens the warehouse when no write connection exists.
    """
    from havn.server.deps import background_write_cursor

    with background_write_cursor() as cur:
        if cur is None:
            return
        cur.execute("FORCE CHECKPOINT")


def _run_checkpoint() -> None:
    """Thread body; the caller has already counted it as in flight."""
    global _checkpoint_running
    from havn.server.deps import request_finished

    try:
        _force_checkpoint()
    except Exception as e:
        logger.debug("Periodic checkpoint skipped: %s", e)
    finally:
        with _checkpoint_lock:
            _checkpoint_running = False
        request_finished()


def _maybe_start_checkpoint() -> bool:
    """Start a background checkpoint if one is due and none is running.

    The checkpoint is counted as an in-flight request here, on the request's
    own thread, before the thread starts: counted inside the thread, the
    request that started it could finish first and leave the count at zero,
    and a branch switch would see an idle warehouse mid-checkpoint.
    """
    global _last_checkpoint, _checkpoint_running
    from havn.server.deps import request_finished, request_started

    now = _time.time()
    with _checkpoint_lock:
        if _checkpoint_running or now - _last_checkpoint <= _CHECKPOINT_INTERVAL:
            return False
        _last_checkpoint = now
        _checkpoint_running = True
    request_started()
    try:
        _threading.Thread(target=_run_checkpoint, name="havn-checkpoint", daemon=True).start()
    except Exception:
        with _checkpoint_lock:
            _checkpoint_running = False
        request_finished()
        return False
    return True


@app.middleware("http")
async def memory_management_middleware(request, call_next):
    """Periodically force DuckDB to release buffer memory, off the request path.

    Request activity sets the cadence (at most one checkpoint a minute); the
    checkpoint runs on its own thread, one at a time, so no response waits.
    """
    response = await call_next(request)
    _maybe_start_checkpoint()
    return response

# ---------------------------------------------------------------------------
# Branch warehouses: notice a git checkout and follow it
# ---------------------------------------------------------------------------

def _tracks_warehouse(path: str) -> bool:
    """API requests that may use the warehouse connection.

    Event streams stay open for as long as a tab does; counting them as
    in flight would hold every branch switch back forever.
    """
    return (
        path.startswith("/api/")
        and not path.endswith("/events")
        and not path.startswith("/api/export")
    )


@app.middleware("http")
async def branch_switch_middleware(request, call_next):
    """Switch to the checked-out branch's warehouse before serving a request.

    The check reads .git/HEAD at most once a second; the switch itself waits
    until nothing else is using the connection (see deps.sync_branch_warehouse).
    """
    if not _tracks_warehouse(request.url.path):
        return await call_next(request)
    from starlette.concurrency import run_in_threadpool

    from havn.server.deps import request_finished, request_started, sync_branch_warehouse

    request_started()
    try:
        try:
            await run_in_threadpool(sync_branch_warehouse)
        except Exception as e:
            logger.debug("Branch sync skipped: %s", e)
        return await call_next(request)
    finally:
        request_finished()

# ---------------------------------------------------------------------------
# Include all route modules
# ---------------------------------------------------------------------------

from havn.server.routes.auth import router as auth_router  # noqa: E402
from havn.server.routes.files import router as files_router  # noqa: E402
from havn.server.routes.models import router as models_router  # noqa: E402
from havn.server.routes.dag import router as dag_router  # noqa: E402
from havn.server.routes.query import router as query_router  # noqa: E402
from havn.server.routes.notebooks import router as notebooks_router  # noqa: E402
from havn.server.routes.connectors import router as connectors_router  # noqa: E402
from havn.server.routes.pipeline import router as pipeline_router  # noqa: E402
from havn.server.routes.quality import router as quality_router  # noqa: E402
from havn.server.routes.catalog import router as catalog_router  # noqa: E402
from havn.server.routes.home import router as home_router  # noqa: E402
from havn.server.routes.deploy import router as deploy_router  # noqa: E402
from havn.server.routes.collaboration import (  # noqa: E402
    register_websocket,
    router as collaboration_router,
)
from havn.server.routes.lint import router as lint_router  # noqa: E402
from havn.server.routes.masking import router as masking_router  # noqa: E402
from havn.server.routes.governance import router as governance_router  # noqa: E402
from havn.server.routes.wiki import router as wiki_router  # noqa: E402
from havn.server.routes.rewind import router as rewind_router  # noqa: E402
from havn.server.routes.sentinel import router as sentinel_router  # noqa: E402
from havn.server.routes.metrics import router as metrics_router  # noqa: E402
from havn.server.routes.audit import router as audit_router  # noqa: E402
from havn.server.routes.agent import (  # noqa: E402
    register_agent_websocket,
    router as agent_router,
)
from havn.server.routes.circuits import router as circuits_router  # noqa: E402
from havn.server.routes.git import router as git_router  # noqa: E402
from havn.server.routes.macros import router as macros_router  # noqa: E402
from havn.server.routes.packages import router as packages_router  # noqa: E402
from havn.server.routes.dashboards import router as dashboards_router  # noqa: E402
from havn.server.routes.jobs import router as jobs_router  # noqa: E402
from havn.server.routes.pr import router as pr_router  # noqa: E402
from havn.server.routes.backup import router as backup_router  # noqa: E402
from havn.server.routes.branch import router as branch_router  # noqa: E402
from havn.server.routes.prometheus import router as prometheus_router  # noqa: E402
from havn.server.routes.resources import router as resources_router  # noqa: E402
from havn.server.routes.semantic import router as semantic_router  # noqa: E402
from havn.server.routes.sql_api import router as sql_api_router  # noqa: E402
from havn.server.routes.export import router as export_router  # noqa: E402
from havn.server.routes.streaming import router as streaming_router  # noqa: E402
from havn.server.routes.live import router as live_router  # noqa: E402
from havn.server.routes.unit_tests import router as unit_tests_router  # noqa: E402
from havn.server.routes.bind import router as bind_router  # noqa: E402
from havn.server.routes.rename import router as rename_router  # noqa: E402
from havn.server.routes.perf import router as perf_router  # noqa: E402
from havn.server.routes.sharing import router as sharing_router  # noqa: E402
from havn.server.routes.reports import router as reports_router  # noqa: E402
from havn.server.routes.ask import router as ask_router  # noqa: E402
from havn.server.routes.changesets import router as changesets_router  # noqa: E402

app.include_router(auth_router)
app.include_router(files_router)
app.include_router(models_router)
app.include_router(bind_router)
app.include_router(rename_router)
app.include_router(dag_router)
app.include_router(query_router)
app.include_router(notebooks_router)
app.include_router(connectors_router)
app.include_router(pipeline_router)
app.include_router(quality_router)
app.include_router(catalog_router)
app.include_router(home_router)
app.include_router(deploy_router)
app.include_router(collaboration_router)
app.include_router(lint_router)
app.include_router(masking_router)
app.include_router(governance_router)
app.include_router(wiki_router)
app.include_router(rewind_router)
app.include_router(sentinel_router)
app.include_router(metrics_router)
app.include_router(agent_router)
app.include_router(audit_router)
app.include_router(circuits_router)
app.include_router(git_router)
app.include_router(macros_router)
app.include_router(packages_router)
app.include_router(dashboards_router)
app.include_router(jobs_router)
app.include_router(pr_router)
app.include_router(backup_router)
app.include_router(branch_router)
app.include_router(prometheus_router)
app.include_router(resources_router)
app.include_router(semantic_router)
app.include_router(sql_api_router)
app.include_router(export_router)
app.include_router(streaming_router)
app.include_router(live_router)
app.include_router(unit_tests_router)
app.include_router(perf_router)
app.include_router(sharing_router)
app.include_router(reports_router)
app.include_router(ask_router)
app.include_router(changesets_router)

# Register WebSocket endpoints (can't use APIRouter for WebSocket)
register_websocket(app)
register_agent_websocket(app)

# ---------------------------------------------------------------------------
# Backward compatibility — expose helpers used by tests
# ---------------------------------------------------------------------------

from havn.server.routes.notebooks import _resolve_notebook  # noqa: E402, F401

# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

def _find_frontend_dir() -> Path:
    """Resolve the frontend static directory from the installed package."""
    # 1. Check inside the installed package (pip install havn)
    pkg_static = Path(__file__).resolve().parent.parent / "static"
    if pkg_static.is_dir():
        return pkg_static
    # 2. Fallback to source tree layout (pip install -e . / development)
    return Path(__file__).resolve().parent.parent.parent.parent / "frontend" / "dist"


_FRONTEND_DIR = _find_frontend_dir()

# Reserved paths that should NOT be caught by the SPA catch-all.
_RESERVED_PATHS = {"docs", "redoc", "openapi.json"}


def _unsafe_static_path(path: str) -> bool:
    """True when a requested frontend path must not reach the filesystem.

    Refused, under POSIX and Windows rules alike: absolute paths, a leading
    ``/`` or ``\\``, drive or UNC anchors (``C:x``, ``//host/share``), NUL
    bytes, and any ``..`` segment (also ``...`` and ``.. ``, which Windows
    trims to ``..``) under either separator.
    """
    if not path:
        return False
    if "\x00" in path or path[0] in "/\\":
        return True
    win = PureWindowsPath(path)
    if PurePosixPath(path).is_absolute() or win.is_absolute() or win.drive or win.anchor:
        return True
    for segment in re.split(r"[\\/]", path):
        if ".." in segment and not segment.strip(". "):
            return True
    return False


@app.get("/", response_class=HTMLResponse)
@app.get("/{path:path}", response_class=HTMLResponse)
def serve_frontend(request: Request, path: str = "") -> Response:
    """Serve the frontend SPA (skips /docs, /redoc, /openapi.json, /api/*).

    Caching: files under ``assets/`` carry Vite's content hash in their name
    and are immutable; ``index.html`` (and the SPA fallback) is always
    revalidated by ETag so a new release is picked up; other root files get
    a short max-age. See havn.server.static_files for types and compression.
    """
    from fastapi import HTTPException

    from havn.server.static_files import (
        CACHE_IMMUTABLE,
        CACHE_REVALIDATE,
        CACHE_SHORT,
        file_response,
        looks_like_asset,
    )

    if path in _RESERVED_PATHS:
        raise HTTPException(404, "Not found")

    # Unknown /api/* paths must 404, not be masked by the SPA shell. Returning
    # index.html for an API miss makes API debugging miserable (looks like a
    # successful 200 with HTML body).
    if path == "api" or path.startswith("api/"):
        raise HTTPException(404, "API endpoint not found")

    accept_encoding = request.headers.get("accept-encoding")
    if_none_match = request.headers.get("if-none-match")

    # Path-traversal guard. Starlette decodes %2e%2e%2f into a literal "../"
    # *after* normalization, so the :path converter can hand us "../../etc/passwd".
    # A path that could leave the frontend dir is a miss before it touches
    # the filesystem: joining an absolute or UNC path replaces the directory,
    # and resolving "\\host\share\x" on Windows opens an SMB connection
    # (leaking the NTLM hash and stalling the worker) even though the
    # containment check below would refuse the file afterwards. The check
    # stays as the backstop (symlinks), so we never serve .env,
    # warehouse.duckdb or SSH keys to an unauthenticated client.
    file_path: Path | None = None
    if not _unsafe_static_path(path):
        frontend_root = _FRONTEND_DIR.resolve()
        file_path = (_FRONTEND_DIR / path).resolve()
        try:
            file_path.relative_to(frontend_root)
        except ValueError:
            file_path = None
    if file_path is not None and file_path.is_file():
        rel_parts = file_path.relative_to(frontend_root).parts
        if rel_parts and rel_parts[0] == "assets":
            cache_control = CACHE_IMMUTABLE
        elif file_path.name == "index.html":
            cache_control = CACHE_REVALIDATE
        else:
            cache_control = CACHE_SHORT
        return file_response(
            file_path,
            cache_control=cache_control,
            accept_encoding=accept_encoding,
            if_none_match=if_none_match,
        )

    # A missing file is a 404, not the SPA shell: a stale tab importing an old
    # hashed chunk (main.jsx lazy-loads App and PublishedDashboard) must get
    # a failed load, not HTML where it expected JavaScript. Client-side routes
    # (/develop/editor, /p/<key>) have no file extension and still fall back.
    if looks_like_asset(path):
        raise HTTPException(404, "Not found")

    index = _FRONTEND_DIR / "index.html"
    if index.is_file():
        return file_response(
            index,
            cache_control=CACHE_REVALIDATE,
            accept_encoding=accept_encoding,
            if_none_match=if_none_match,
        )
    return HTMLResponse(
        content="<h1>havn</h1><p>Frontend not built. Run <code>cd frontend && npm run build</code></p>",
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Cloud hook: allow havn-cloud to inject a per-tenant backend factory.
# OSS keeps using `uvicorn havn.server.app:app` — the module-level `app`
# object is unchanged. Cloud calls create_app(backend_factory=...) after
# import to set the factory, which deps._get_backend() consults first.
# ---------------------------------------------------------------------------


def create_app(backend_factory=None) -> FastAPI:
    """Return the FastAPI app, optionally with a backend factory injected.

    Args:
        backend_factory: Optional callable ``(project_dir, config) -> WarehouseBackend``.
            When provided, :func:`havn.server.deps._get_backend` uses it
            instead of the ``project.yml``-driven default. Used by
            havn-cloud for per-tenant backend resolution.
    """
    if backend_factory is not None:
        app.state.backend_factory = backend_factory
        # Ensure any cached singleton is rebuilt with the new factory
        try:
            from havn.server.deps import reset_shared_conn
            reset_shared_conn()
        except Exception:
            pass
    return app
