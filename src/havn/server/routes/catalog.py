"""Catalog endpoints: seeds, sources, exposures, environment, versioning, and overview."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from havn.engine.database import ensure_meta_table
from havn.server.deps import (
    DbConn,
    DbConnReadOnly,
    DbConnReadOnlyOptional,
    _get_config,
    _get_project_dir,
    _require_permission,
    _set_active_env,
    invalidate_config_cache,
)

router = APIRouter()


# --- Pydantic models ---


class SeedRequest(BaseModel):
    force: bool = False
    schema_name: str = Field(
        default="seeds",
        max_length=100,
        pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$",
    )


# --- Seeds ---


@router.get("/api/seeds")
def list_seeds_endpoint(request: Request) -> list[dict]:
    """List all seed CSV files."""
    _require_permission(request, "read")
    from havn.engine.seeds import discover_seeds

    seeds_dir = _get_project_dir() / "seeds"
    seeds = discover_seeds(seeds_dir)
    return [
        {
            "name": s["name"],
            "full_name": s["full_name"],
            "schema": s["schema"],
            "path": str(s["path"].relative_to(_get_project_dir())),
        }
        for s in seeds
    ]


@router.post("/api/seeds")
def run_seeds_endpoint(request: Request, req: SeedRequest, conn: DbConn) -> dict:
    """Load all seeds."""
    _require_permission(request, "execute")
    from havn.engine.seeds import run_seeds

    results = run_seeds(
        conn, _get_project_dir() / "seeds", schema=req.schema_name, force=req.force
    )
    return {"results": results}


# --- Sources ---


@router.get("/api/sources")
def list_sources_endpoint(request: Request) -> list[dict]:
    """List declared sources from sources.yml."""
    _require_permission(request, "read")
    config = _get_config()
    return [
        {
            "name": s.name,
            "schema": s.schema,
            "description": s.description,
            "freshness_hours": s.freshness_hours,
            "connection": s.connection,
            "tables": [
                {
                    "name": t.name,
                    "description": t.description,
                    "columns": [
                        {"name": c.name, "description": c.description}
                        for c in t.columns
                    ],
                    "loaded_at_column": t.loaded_at_column,
                }
                for t in s.tables
            ],
        }
        for s in config.sources
    ]


@router.get("/api/sources/freshness")
def check_sources_freshness(
    request: Request, conn: DbConnReadOnly
) -> list[dict]:
    """Check source freshness against declared SLAs."""
    _require_permission(request, "read")
    config = _get_config()

    results = []
    ensure_meta_table(conn)
    for src in config.sources:
        sla_hours = src.freshness_hours
        if sla_hours is None:
            continue
        for tbl in src.tables:
            full_name = f"{src.schema}.{tbl.name}"
            last_loaded = None
            if tbl.loaded_at_column:
                try:
                    row = conn.execute(
                        f'SELECT MAX("{tbl.loaded_at_column}") FROM "{src.schema}"."{tbl.name}"'
                    ).fetchone()
                    if row and row[0]:
                        last_loaded = str(row[0])
                except Exception:
                    pass
            if not last_loaded:
                try:
                    row = conn.execute(
                        "SELECT MAX(started_at) FROM _havn.run_log "
                        "WHERE target = ? AND status = 'success'",
                        [full_name],
                    ).fetchone()
                    if row and row[0]:
                        last_loaded = str(row[0])
                except Exception:
                    pass

            hours_ago = None
            is_stale = last_loaded is None
            if last_loaded:
                try:
                    row = conn.execute(
                        "SELECT EXTRACT(EPOCH FROM (current_timestamp - ?::TIMESTAMP)) / 3600",
                        [last_loaded],
                    ).fetchone()
                    if row:
                        hours_ago = round(row[0], 1)
                        is_stale = hours_ago > sla_hours
                except Exception:
                    is_stale = True

            results.append(
                {
                    "source": src.name,
                    "table": full_name,
                    "sla_hours": sla_hours,
                    "last_loaded": last_loaded,
                    "hours_ago": hours_ago,
                    "is_stale": is_stale,
                }
            )
    return results


# --- Exposures ---


@router.get("/api/exposures")
def list_exposures_endpoint(request: Request) -> list[dict]:
    """List declared exposures from exposures.yml."""
    _require_permission(request, "read")
    config = _get_config()
    return [
        {
            "name": e.name,
            "description": e.description,
            "owner": e.owner,
            "depends_on": e.depends_on,
            "type": e.type,
            "url": e.url,
        }
        for e in config.exposures
    ]


# --- Environment management ---


# --- Database resource settings ---


@router.get("/api/config/database")
def get_database_config(request: Request) -> dict:
    """Get database resource settings."""
    _require_permission(request, "read")
    config = _get_config()
    return {
        "memory_limit": config.database.memory_limit or "",
        "threads": config.database.threads,
    }


class DatabaseConfigUpdate(BaseModel):
    memory_limit: str | None = None
    threads: int | None = None


@router.put("/api/config/database")
def update_database_config(request: Request, body: DatabaseConfigUpdate) -> dict:
    """Update database resource settings in project.yml."""
    _require_permission(request, "write")
    import yaml

    project_dir = _get_project_dir()
    yml_path = project_dir / "project.yml"
    if not yml_path.exists():
        raise HTTPException(404, "project.yml not found")

    raw = yaml.safe_load(yml_path.read_text()) or {}
    if "database" not in raw:
        raw["database"] = {}

    if body.memory_limit is not None:
        if body.memory_limit.strip():
            raw["database"]["memory_limit"] = body.memory_limit.strip()
        elif "memory_limit" in raw["database"]:
            del raw["database"]["memory_limit"]

    if body.threads is not None:
        if body.threads > 0:
            raw["database"]["threads"] = body.threads
        elif "threads" in raw["database"]:
            del raw["database"]["threads"]

    yml_path.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))

    # Reset shared connection so new settings take effect
    from havn.server.deps import reset_shared_conn, _clear_config_cache
    reset_shared_conn()
    _clear_config_cache()

    return {"status": "ok", "memory_limit": body.memory_limit, "threads": body.threads}


@router.get("/api/environment")
def get_environment(request: Request) -> dict:
    """Get current environment and available environments."""
    _require_permission(request, "read")
    config = _get_config()
    return {
        "active": config.active_environment,
        "available": list(config.environments.keys()),
        "database_path": config.database.path,
    }


@router.put("/api/environment/{env_name}")
def switch_environment(request: Request, env_name: str) -> dict:
    """Switch the active environment."""
    _require_permission(request, "write")
    config = _get_config()
    if env_name not in config.environments:
        raise HTTPException(404, f"Environment '{env_name}' not found")
    _set_active_env(env_name)
    invalidate_config_cache()
    # Reset DB connection so it reconnects to the new environment's database
    from havn.server.deps import reset_shared_conn
    reset_shared_conn()
    new_config = _get_config()
    return {
        "active": new_config.active_environment,
        "database_path": new_config.database.path,
    }


# --- Overview ---


@router.get("/api/overview")
def get_overview(request: Request, conn: DbConnReadOnlyOptional) -> dict:
    """Get an overview of the platform: pipeline health, warehouse stats, recent activity."""
    _require_permission(request, "read")

    result: dict[str, Any] = {
        "recent_runs": [],
        "schemas": [],
        "total_tables": 0,
        "total_rows": 0,
        "connectors": 0,
        "has_data": False,
        "streams": {},
    }

    config = _get_config()
    result["project_name"] = config.name
    result["is_sample"] = config.sample
    result["streams"] = {
        name: {"description": s.description, "schedule": s.schedule}
        for name, s in config.streams.items()
    }

    try:
        import havn.connectors  # noqa: F401
        from havn.engine.connector import list_configured_connectors

        result["connectors"] = len(list_configured_connectors(_get_project_dir()))
    except Exception:
        pass

    if conn is not None:
        try:
            rows = conn.execute(
                """
                SELECT r.run_id, r.run_type, r.target, r.status,
                       r.started_at, r.duration_ms, r.rows_affected, r.error
                FROM _havn.run_log r
                INNER JOIN (
                    SELECT target, MAX(started_at) AS max_started
                    FROM _havn.run_log
                    GROUP BY target
                ) latest ON r.target = latest.target
                        AND r.started_at = latest.max_started
                ORDER BY r.started_at DESC
                """
            ).fetchall()
            result["recent_runs"] = [
                {
                    "run_id": r[0],
                    "run_type": r[1],
                    "target": r[2],
                    "status": r[3],
                    "started_at": str(r[4]) if r[4] else None,
                    "duration_ms": r[5],
                    "rows_affected": r[6],
                    "error": r[7],
                }
                for r in rows
            ]
        except Exception:
            pass

    if conn is not None:
        try:
            tables = conn.execute(
                """
                SELECT table_schema, table_name, table_type
                FROM information_schema.tables
                WHERE table_catalog = current_database()
                UNION ALL
                SELECT DISTINCT schema_name, view_name, 'VIEW'
                FROM duckdb_views()
                WHERE schema_name = 'information_schema'
                ORDER BY 1, 2
                """
            ).fetchall()

            schema_map: dict[str, dict] = {}
            for schema, table_name, table_type in tables:
                if schema not in schema_map:
                    schema_map[schema] = {
                        "name": schema,
                        "tables": 0,
                        "views": 0,
                        "total_rows": 0,
                    }
                if table_type == "VIEW":
                    schema_map[schema]["views"] += 1
                else:
                    schema_map[schema]["tables"] += 1

            # Use cached row counts from model_state — never live COUNT(*)
            try:
                cached_rows = conn.execute(
                    "SELECT model_path, row_count FROM _havn.model_state WHERE row_count IS NOT NULL"
                ).fetchall()
                for model_path, rc in cached_rows:
                    parts = model_path.split(".", 1)
                    if len(parts) == 2 and parts[0] in schema_map:
                        schema_map[parts[0]]["total_rows"] += rc
            except Exception:
                pass

            SCHEMA_ORDER = ["landing", "bronze", "silver", "gold"]
            sorted_schemas = sorted(
                schema_map.values(),
                key=lambda s: (
                    SCHEMA_ORDER.index(s["name"])
                    if s["name"] in SCHEMA_ORDER
                    else 100,
                    s["name"],
                ),
            )
            result["schemas"] = sorted_schemas
            result["total_tables"] = sum(
                s["tables"] + s["views"] for s in sorted_schemas
            )
            result["total_rows"] = sum(s["total_rows"] for s in sorted_schemas)
            result["has_data"] = result["total_tables"] > 0
        except Exception:
            pass

    return result


@router.post("/api/project/clear-sample")
def clear_sample_project(request: Request) -> dict:
    """Clear sample project files and reset to an empty project."""
    _require_permission(request, "write")

    config = _get_config()
    if not config.sample:
        raise HTTPException(400, "This is not a sample project")

    import shutil

    from havn.server.deps import invalidate_config_cache, reset_shared_conn

    project_dir = _get_project_dir()

    # Close shared DB connection before deleting the file
    reset_shared_conn()

    # Delete warehouse (backend-aware)
    if config.database.backend == "duckdb":
        db_path = project_dir / config.database.path
        if db_path.exists():
            db_path.unlink()
        wal_path = Path(str(db_path) + ".wal")
        if wal_path.exists():
            wal_path.unlink()
    else:
        catalog = config.database.catalog or ""
        if catalog and not catalog.startswith(("postgres:", "s3://")):
            cp = project_dir / catalog if not Path(catalog).is_absolute() else Path(catalog)
            if cp.exists():
                cp.unlink()
            wp = Path(str(cp) + ".wal")
            if wp.exists():
                wp.unlink()
        data_path = config.database.data_path or ""
        if data_path and not data_path.startswith("s3://"):
            dp = project_dir / data_path if not Path(data_path).is_absolute() else Path(data_path)
            if dp.exists():
                shutil.rmtree(dp)

    # Delete snapshot/rewind data
    for meta_dir in [".havn", "_snapshots", "output"]:
        meta_path = project_dir / meta_dir
        if meta_path.exists():
            shutil.rmtree(meta_path)

    # Clear content directories (keep the dirs, delete contents)
    for subdir in ["ingest", "transform", "export", "macros", "seeds", "contracts", "notebooks"]:
        dir_path = project_dir / subdir
        if dir_path.exists():
            shutil.rmtree(dir_path)
            dir_path.mkdir(parents=True, exist_ok=True)

    # Recreate transform subdirectories
    for layer in ["bronze", "silver", "gold"]:
        (project_dir / "transform" / layer).mkdir(parents=True, exist_ok=True)

    # Rewrite project.yml without sample flag
    from havn.templates import PROJECT_YML_EMPTY_TEMPLATE

    (project_dir / "project.yml").write_text(
        PROJECT_YML_EMPTY_TEMPLATE.format(name=config.name)
    )

    # Invalidate caches
    invalidate_config_cache()

    return {"status": "ok", "message": "Sample project cleared"}


# --- Versioning / Time Travel ---


@router.get("/api/versions")
def list_versions_endpoint(
    request: Request, conn: DbConnReadOnly
) -> list[dict]:
    """List all warehouse versions."""
    _require_permission(request, "read")
    from havn.engine.versioning import list_versions

    return list_versions(conn)


@router.post("/api/versions")
def create_version_endpoint(request: Request, conn: DbConn) -> dict:
    """Create a new version snapshot."""
    _require_permission(request, "write")
    from havn.engine.versioning import create_version

    return create_version(conn, _get_project_dir())


@router.get("/api/versions/{version_id}")
def get_version_endpoint(
    request: Request, version_id: str, conn: DbConnReadOnly
) -> dict:
    """Get details of a specific version."""
    _require_permission(request, "read")
    from havn.engine.versioning import get_version

    result = get_version(conn, version_id)
    if not result:
        raise HTTPException(404, f"Version '{version_id}' not found")
    return result


@router.get("/api/versions/{from_version}/diff")
def diff_versions_endpoint(
    request: Request,
    from_version: str,
    to_version: str | None = None,
    conn: DbConnReadOnlyOptional = None,
) -> dict:
    """Diff two versions or a version against current state."""
    _require_permission(request, "read")
    from havn.engine.versioning import diff_versions

    if not conn:
        return {"error": "Database not found"}
    return diff_versions(conn, _get_project_dir(), from_version, to_version)


@router.post("/api/versions/{version_id}/restore")
def restore_version_endpoint(
    request: Request, version_id: str, conn: DbConn
) -> dict:
    """Restore tables from a version snapshot."""
    _require_permission(request, "write")
    from havn.engine.versioning import restore_version

    return restore_version(conn, _get_project_dir(), version_id)


@router.get("/api/versions/timeline/{table_name}")
def get_table_timeline(
    request: Request, table_name: str, conn: DbConnReadOnly
) -> list[dict]:
    """Get the version history timeline for a specific table."""
    _require_permission(request, "read")
    from havn.engine.versioning import table_timeline

    return table_timeline(conn, table_name)
