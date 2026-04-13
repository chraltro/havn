"""Warehouse backend factory."""

from __future__ import annotations

from pathlib import Path

from havn.config import DatabaseConfig
from havn.engine.backends.base import BackendStatus, WarehouseBackend
from havn.engine.backends.duckdb_backend import DuckDBBackend
from havn.engine.backends.ducklake_backend import DuckLakeBackend

__all__ = [
    "BackendStatus",
    "DuckDBBackend",
    "DuckLakeBackend",
    "WarehouseBackend",
    "create_backend",
]


def create_backend(
    config: DatabaseConfig, project_dir: str | Path | None = None
) -> WarehouseBackend:
    """Build the backend indicated by config.backend."""
    if config.backend == "duckdb":
        return DuckDBBackend(config, project_dir=project_dir)
    if config.backend == "ducklake":
        return DuckLakeBackend(config, project_dir=project_dir)
    raise ValueError(f"Unknown backend: {config.backend}")
