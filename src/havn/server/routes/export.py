"""Export endpoints: download the warehouse as a ``.duckdb`` file.

For the DuckDB backend this streams the existing database file directly.
For the DuckLake backend this materializes a fresh DuckDB snapshot via
``ATTACH ... AS plain`` + ``COPY FROM DATABASE`` and streams that.

This is the open-source "escape hatch" — no lock-in, your data comes
home with you.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import duckdb
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from havn.engine.resource_manager import get_resource_manager
from havn.server.deps import _get_backend, _get_config, _require_permission

router = APIRouter()


@router.get("/v1/export/duckdb")
async def export_as_duckdb(request: Request) -> StreamingResponse:
    _require_permission(request, "read")
    backend = _get_backend()
    manager = get_resource_manager()

    if backend.name == "duckdb":
        # Fast path: just stream the existing .duckdb file.
        from havn.server.deps import _get_db_path

        path = _get_db_path()
        if not path.exists():
            raise HTTPException(404, "warehouse not initialized")
        return FileResponse(
            str(path),
            media_type="application/octet-stream",
            filename="warehouse.duckdb",
        )

    # DuckLake: materialize to a temp plain DuckDB file.
    tmp_dir = Path(tempfile.mkdtemp(prefix="havn-export-"))
    out_path = tmp_dir / "warehouse.duckdb"

    with manager.acquire_sync("system", "export:duckdb"):
        src = backend.connect(read_only=True)
        try:
            src.execute(f"ATTACH '{out_path.as_posix()}' AS plain (TYPE DUCKDB)")
            src.execute("COPY FROM DATABASE warehouse TO plain")
            src.execute("DETACH plain")
        finally:
            src.close()

    def stream_and_cleanup():
        try:
            with open(out_path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return StreamingResponse(
        stream_and_cleanup(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="warehouse.duckdb"'},
    )
