"""Connector management, data import, file upload, and CDC endpoints."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from havn.server.deps import (
    DbConn,
    DbConnAutoCreate,
    DbConnReadOnly,
    _get_project_dir,
    _require_permission,
    _validate_identifier,
    get_db_readonly_optional,
)

logger = logging.getLogger("havn.server")

router = APIRouter()


# --- Pydantic models ---

_BLOCKED_SCHEMAS = {"_havn", "information_schema"}


def _check_target_schema(v: str) -> str:
    if v.lower() in _BLOCKED_SCHEMAS:
        raise ValueError(f"Cannot use reserved schema: {v}")
    return v


class ImportFileRequest(BaseModel):
    file_path: str = Field(..., min_length=1, max_length=1000)
    target_schema: str = Field(default="landing", min_length=1, max_length=100)
    target_table: str | None = Field(default=None, max_length=100)

    _validate_schema = field_validator("target_schema")(_check_target_schema)


class TestConnectionRequest(BaseModel):
    connection_type: str = Field(..., min_length=1, max_length=50)
    params: dict


class ImportFromConnectionRequest(BaseModel):
    connection_type: str = Field(..., min_length=1, max_length=50)
    params: dict
    source_table: str = Field(..., min_length=1, max_length=500)
    target_schema: str = Field(default="landing", min_length=1, max_length=100)
    target_table: str | None = Field(default=None, max_length=100)

    _validate_schema = field_validator("target_schema")(_check_target_schema)


class ConnectorSetupRequest(BaseModel):
    connector_type: str = Field(..., min_length=1, max_length=50)
    connection_name: str = Field(
        ..., min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$"
    )
    config: dict
    tables: list[str] | None = None
    target_schema: str = Field(default="landing", min_length=1, max_length=100)
    schedule: str | None = None

    _validate_schema = field_validator("target_schema")(_check_target_schema)


class ConnectorTestRequest(BaseModel):
    connector_type: str = Field(..., min_length=1, max_length=50)
    config: dict


class ConnectorDiscoverRequest(BaseModel):
    connector_type: str = Field(..., min_length=1, max_length=50)
    config: dict


# --- Import endpoints ---


def _validate_import_path(file_path: str) -> Path:
    """Validate that an import file path is within the project directory."""
    from pathlib import Path

    project_dir = _get_project_dir()
    resolved = Path(file_path).resolve()
    # Allow absolute paths only if within project dir
    if not resolved.is_relative_to(project_dir.resolve()):
        # Also try relative to project dir
        resolved = (project_dir / file_path).resolve()
        if not resolved.is_relative_to(project_dir.resolve()):
            raise HTTPException(400, "File path must be within the project directory")
    return resolved


@router.post("/api/import/preview-file")
def preview_file_endpoint(request: Request, req: ImportFileRequest) -> dict:
    """Preview data from a file before importing."""
    _require_permission(request, "execute")
    from havn.engine.importer import preview_file

    validated_path = _validate_import_path(req.file_path)
    try:
        return preview_file(str(validated_path))
    except Exception as e:
        raise HTTPException(400, str(e))


@router.post("/api/import/file")
def import_file_endpoint(
    request: Request, req: ImportFileRequest, conn: DbConnAutoCreate
) -> dict:
    """Import a file into the warehouse (creates database if needed)."""
    _require_permission(request, "execute")
    from havn.engine.importer import import_file

    validated_path = _validate_import_path(req.file_path)
    return import_file(conn, str(validated_path), req.target_schema, req.target_table)


@router.post("/api/import/test-connection")
def test_connection_endpoint(
    request: Request, req: TestConnectionRequest
) -> dict:
    """Test a database connection."""
    _require_permission(request, "execute")
    from havn.engine.importer import test_connection

    return test_connection(req.connection_type, req.params)


@router.post("/api/import/from-connection")
def import_from_connection_endpoint(
    request: Request, req: ImportFromConnectionRequest, conn: DbConnAutoCreate
) -> dict:
    """Import from an external database (creates database if needed)."""
    _require_permission(request, "execute")
    from havn.engine.importer import import_from_connection

    return import_from_connection(
        conn,
        req.connection_type,
        req.params,
        req.source_table,
        req.target_schema,
        req.target_table,
    )


# --- Upload ---


@router.post("/api/upload")
async def upload_file(request: Request) -> dict:
    """Upload a file for data import."""
    _require_permission(request, "execute")

    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(400, "No file uploaded")

    data_dir = _get_project_dir() / "data"
    data_dir.mkdir(exist_ok=True)
    safe_name = Path(file.filename).name
    if not safe_name or safe_name.startswith("."):
        raise HTTPException(400, "Invalid filename")
    file_path = data_dir / safe_name
    if not file_path.resolve().is_relative_to(data_dir.resolve()):
        raise HTTPException(400, "Invalid filename")

    content = await file.read()
    file_path.write_bytes(content)

    return {"path": str(file_path), "name": safe_name, "size": len(content)}


# --- Connector endpoints ---


@router.get("/api/connectors/available")
def list_available_connectors(request: Request) -> list[dict]:
    """List all available connector types."""
    _require_permission(request, "read")
    import havn.connectors  # noqa: F401
    from havn.engine.connector import list_connectors

    return list_connectors()


@router.get("/api/connectors")
def list_configured_connectors_endpoint(request: Request) -> list[dict]:
    """List connectors configured in this project."""
    _require_permission(request, "read")
    import havn.connectors  # noqa: F401
    from havn.engine.connector import list_configured_connectors

    return list_configured_connectors(_get_project_dir())


@router.post("/api/connectors/test")
def test_connector_endpoint(
    request: Request, req: ConnectorTestRequest
) -> dict:
    """Test a connector without setting it up."""
    _require_permission(request, "execute")
    import havn.connectors  # noqa: F401
    from havn.engine.connector import test_connector

    try:
        return test_connector(req.connector_type, req.config)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/connectors/discover")
def discover_connector_endpoint(
    request: Request, req: ConnectorDiscoverRequest
) -> list[dict]:
    """Discover available resources for a connector."""
    _require_permission(request, "execute")
    import havn.connectors  # noqa: F401
    from havn.engine.connector import discover_connector

    try:
        return discover_connector(req.connector_type, req.config)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/connectors/setup")
def setup_connector_endpoint(
    request: Request, req: ConnectorSetupRequest, conn: DbConnAutoCreate
) -> dict:
    """Set up a new connector: test, generate script, update config."""
    user = _require_permission(request, "execute")
    import havn.connectors  # noqa: F401
    from havn.engine.connector import setup_connector

    try:
        result = setup_connector(
            project_dir=_get_project_dir(),
            connector_type=req.connector_type,
            connection_name=req.connection_name,
            config=req.config,
            tables=req.tables,
            target_schema=req.target_schema,
            schedule=req.schedule,
        )
        if result["status"] == "error":
            raise HTTPException(400, result.get("error", "Setup failed"))
        try:
            from havn.engine.audit import log_audit

            client_ip = request.client.host if request.client else None
            log_audit(
                conn,
                user=user["username"],
                action="connector_setup",
                resource=req.connection_name,
                detail=f"type={req.connector_type}",
                ip_address=client_ip,
            )
        except Exception:
            pass
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/connectors/regenerate/{connection_name}")
def regenerate_connector_endpoint(
    request: Request, connection_name: str, body: dict = {}
) -> dict:
    """Regenerate the ingest script for an existing connector."""
    _require_permission(request, "execute")
    _validate_identifier(connection_name, "connection name")
    import havn.connectors  # noqa: F401
    from havn.engine.connector import regenerate_connector

    result = regenerate_connector(_get_project_dir(), connection_name, body or None)
    if result["status"] == "error":
        raise HTTPException(400, result.get("error", "Regeneration failed"))
    return result


@router.post("/api/connectors/sync/{connection_name}")
def sync_connector_endpoint(request: Request, connection_name: str, conn: DbConn) -> dict:
    """Run sync for a configured connector."""
    user = _require_permission(request, "execute")
    _validate_identifier(connection_name, "connection name")
    import havn.connectors  # noqa: F401
    from havn.engine.connector import sync_connector

    result = sync_connector(_get_project_dir(), connection_name)
    if result.get("status") == "error":
        # Include log output so the user can see per-table errors
        error_msg = result.get("error", "Sync failed")
        log_output = result.get("log_output", "")
        if log_output:
            error_msg = f"{error_msg}\n\n{log_output}"
        raise HTTPException(400, error_msg)
    try:
        from havn.engine.audit import log_audit

        client_ip = request.client.host if request.client else None
        log_audit(
            conn,
            user=user["username"],
            action="connector_sync",
            resource=connection_name,
            detail=f"status={result.get('status', 'unknown')}",
            ip_address=client_ip,
        )
    except Exception:
        pass
    return result


@router.delete("/api/connectors/{connection_name}")
def remove_connector_endpoint(request: Request, connection_name: str) -> dict:
    """Remove a configured connector."""
    _require_permission(request, "write")
    _validate_identifier(connection_name, "connection name")
    import havn.connectors  # noqa: F401
    from havn.engine.connector import remove_connector

    result = remove_connector(_get_project_dir(), connection_name)
    if result["status"] == "error":
        raise HTTPException(404, result.get("error", "Not found"))
    return result


@router.get("/api/connectors/health")
def connector_health_endpoint(
    request: Request, conn: DbConnReadOnly
) -> list:
    """Get last sync status for each connector from run_log."""
    _require_permission(request, "read")
    try:
        rows = conn.execute(
            """
            SELECT target, status, started_at, duration_ms, error
            FROM _havn.run_log
            WHERE run_type = 'script' AND target LIKE 'ingest/%'
            ORDER BY started_at DESC
            """
        ).fetchall()
    except Exception:
        return []

    seen: dict[str, dict] = {}
    for target, status, started_at, duration_ms, error in rows:
        if target not in seen:
            seen[target] = {
                "target": target,
                "status": status,
                "started_at": str(started_at) if started_at else None,
                "duration_ms": duration_ms,
                "error": error,
            }
    return list(seen.values())


# --- Webhook receive ---


def _verify_webhook_secret(request: Request, webhook_name: str) -> None:
    """Verify webhook secret via header. Fails closed unless explicitly opened."""
    import hmac
    import os as _os

    secret_env = f"HAVN_WEBHOOK_SECRET_{webhook_name.upper()}"
    expected = _os.environ.get(secret_env) or _os.environ.get("HAVN_WEBHOOK_SECRET")
    if expected:
        provided = (
            request.headers.get("x-havn-webhook-secret")
            or request.headers.get("x-webhook-secret")
            or ""
        )
        if not provided or not hmac.compare_digest(provided, expected):
            raise HTTPException(401, "Invalid or missing webhook secret")
        return
    if _os.environ.get("HAVN_WEBHOOK_OPEN", "").lower() in ("1", "true", "yes"):
        return
    raise HTTPException(
        401,
        f"Webhook authentication required. Set {secret_env} or HAVN_WEBHOOK_SECRET, "
        "or set HAVN_WEBHOOK_OPEN=true to allow unauthenticated webhooks.",
    )


@router.post("/api/webhook/{webhook_name}")
async def receive_webhook(
    request: Request, webhook_name: str, conn: DbConn
) -> dict:
    """Receive webhook data and store it in the inbox table.

    Authentication: requires shared secret via X-Havn-Webhook-Secret header
    matching HAVN_WEBHOOK_SECRET_<NAME> or HAVN_WEBHOOK_SECRET env var. Set
    HAVN_WEBHOOK_OPEN=true to disable (development only).
    """
    _validate_identifier(webhook_name, "webhook name")
    _verify_webhook_secret(request, webhook_name)

    body = await request.body()
    if len(body) > 5_000_000:
        raise HTTPException(413, "Webhook payload too large")
    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")

    safe_name = webhook_name
    qualified_table = f'landing."{safe_name}_inbox"'
    conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {qualified_table} ("
        "id VARCHAR DEFAULT gen_random_uuid()::VARCHAR, "
        "received_at TIMESTAMP DEFAULT current_timestamp, "
        "payload JSON)"
    )
    conn.execute(
        f"INSERT INTO {qualified_table} (payload) VALUES (?::JSON)",
        [json.dumps(payload)],
    )
    return {"status": "received", "table": f"landing.{safe_name}_inbox"}


# --- CDC Status ---


@router.get("/api/cdc")
def get_cdc_status_endpoint(
    request: Request, conn: DbConnReadOnly
) -> list[dict]:
    """Get CDC state for all tracked connectors."""
    _require_permission(request, "read")
    from havn.engine.cdc import get_cdc_status

    return get_cdc_status(conn)


@router.get("/api/cdc/{connector_name}")
def get_cdc_connector_status(
    request: Request, connector_name: str, conn: DbConnReadOnly
) -> list[dict]:
    """Get CDC state for a specific connector."""
    _require_permission(request, "read")
    from havn.engine.cdc import get_cdc_status

    return get_cdc_status(conn, connector_name)


@router.post("/api/cdc/{connector_name}/reset")
def reset_cdc_state(
    request: Request, connector_name: str, conn: DbConn
) -> dict:
    """Reset CDC watermarks for a connector."""
    _require_permission(request, "write")
    from havn.engine.cdc import reset_watermark

    reset_watermark(conn, connector_name)
    return {"status": "reset", "connector": connector_name}


# --- API poll streaming endpoints ---


@router.get("/api/streaming/pollers")
def list_streaming_pollers(
    request: Request, conn: DbConnReadOnly
) -> list[dict]:
    """List all API poll sources with their current CDC state."""
    _require_permission(request, "read")
    from havn.engine.cdc import get_cdc_status

    entries = get_cdc_status(conn)
    project_dir = _get_project_dir()
    pidfile_dir = project_dir / ".havn" / "streaming"

    result = []
    for entry in entries:
        connector = entry["connector"]
        pid: int | None = None
        running = False
        import os

        pidfile = pidfile_dir / f"{connector}.pid"
        if pidfile.exists():
            try:
                pid = int(pidfile.read_text().strip())
                os.kill(pid, 0)
                running = True
            except (OSError, ValueError):
                running = False

        result.append(
            {
                "connector": connector,
                "cdc_mode": entry["cdc_mode"],
                "watermark": entry["watermark"],
                "last_poll_at": entry["last_sync_at"],
                "rows_synced": entry["rows_synced"],
                "status": "running" if running else "idle",
                "pid": pid if running else None,
            }
        )
    return result


@router.post("/api/streaming/pollers/{connector_name}/start")
def trigger_poll_once(
    request: Request, connector_name: str, conn: DbConn
) -> dict:
    """Trigger a one-shot poll for *connector_name* and return the result."""
    _require_permission(request, "execute")
    _validate_identifier(connector_name, "connector name")

    project_dir = _get_project_dir()
    from havn.config import load_project
    from havn.engine.streaming.api_poll import APIPollConsumer

    config = load_project(project_dir)
    connections = config.connections or {}
    if connector_name not in connections:
        raise HTTPException(404, f"Connector '{connector_name}' not found in project.yml")

    raw = connections[connector_name]
    # ConnectionConfig wraps params in .params; fall back to model_dump for any future shape
    if hasattr(raw, "params"):
        connector_cfg: dict = raw.params
    elif hasattr(raw, "model_dump"):
        connector_cfg = raw.model_dump()
    else:
        connector_cfg = dict(raw or {})

    consumer = APIPollConsumer(connector_name, connector_cfg, project_dir)
    result = consumer.poll_once()

    return {
        "connector": connector_name,
        "rows_inserted": result.rows_inserted,
        "new_watermark": result.new_watermark,
        "duration_ms": result.duration_ms,
        "error": result.error,
        "status": "error" if result.error else "ok",
    }
