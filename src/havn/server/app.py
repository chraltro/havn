"""FastAPI backend for the web UI.

This module creates the FastAPI application and assembles all route modules.
Route handlers are defined in havn.server.routes.* submodules.
Shared dependencies (DB injection, auth, caching) live in havn.server.deps.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

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

app = FastAPI(title="havn", version="0.2.5")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

# ---------------------------------------------------------------------------
# Memory management: periodic checkpoint to release DuckDB buffers
# ---------------------------------------------------------------------------

import time as _time
import threading as _threading

_last_checkpoint = _time.time()
_checkpoint_lock = _threading.Lock()
_CHECKPOINT_INTERVAL = 60  # seconds between checkpoints

@app.middleware("http")
async def memory_management_middleware(request, call_next):
    """After heavy API calls, periodically force DuckDB to release buffer memory."""
    response = await call_next(request)
    global _last_checkpoint
    now = _time.time()
    if now - _last_checkpoint > _CHECKPOINT_INTERVAL:
        with _checkpoint_lock:
            if now - _last_checkpoint > _CHECKPOINT_INTERVAL:
                _last_checkpoint = now
                try:
                    from havn.server.deps import _get_shared_conn
                    conn = _get_shared_conn()
                    conn.execute("FORCE CHECKPOINT")
                except Exception:
                    pass
    return response

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
from havn.server.routes.collaboration import (  # noqa: E402
    register_websocket,
    router as collaboration_router,
)
from havn.server.routes.lint import router as lint_router  # noqa: E402
from havn.server.routes.masking import router as masking_router  # noqa: E402
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
from havn.server.routes.dashboards import router as dashboards_router  # noqa: E402

app.include_router(auth_router)
app.include_router(files_router)
app.include_router(models_router)
app.include_router(dag_router)
app.include_router(query_router)
app.include_router(notebooks_router)
app.include_router(connectors_router)
app.include_router(pipeline_router)
app.include_router(quality_router)
app.include_router(catalog_router)
app.include_router(collaboration_router)
app.include_router(lint_router)
app.include_router(masking_router)
app.include_router(wiki_router)
app.include_router(rewind_router)
app.include_router(sentinel_router)
app.include_router(metrics_router)
app.include_router(agent_router)
app.include_router(audit_router)
app.include_router(circuits_router)
app.include_router(git_router)
app.include_router(macros_router)
app.include_router(dashboards_router)

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


@app.get("/", response_class=HTMLResponse)
@app.get("/{path:path}", response_class=HTMLResponse)
def serve_frontend(path: str = "") -> HTMLResponse:
    """Serve the frontend SPA (skips /docs, /redoc, /openapi.json)."""
    from fastapi import HTTPException

    if path in _RESERVED_PATHS:
        raise HTTPException(404, "Not found")

    file_path = _FRONTEND_DIR / path
    if file_path.is_file():
        content_type = {
            ".html": "text/html",
            ".js": "application/javascript",
            ".css": "text/css",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }.get(file_path.suffix, "application/octet-stream")
        return HTMLResponse(content=file_path.read_bytes(), media_type=content_type)

    index = _FRONTEND_DIR / "index.html"
    if index.exists():
        return HTMLResponse(content=index.read_text())
    return HTMLResponse(
        content="<h1>havn</h1><p>Frontend not built. Run <code>cd frontend && npm run build</code></p>",
        status_code=200,
    )
