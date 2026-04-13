"""DuckDB file-based backend (default)."""

from __future__ import annotations

from pathlib import Path

import duckdb

from havn.config import DatabaseConfig
from havn.engine.backends.base import BackendStatus


class DuckDBBackend:
    """Backend that stores everything in a single .duckdb file."""

    name = "duckdb"

    def __init__(self, config: DatabaseConfig, project_dir: str | Path | None = None):
        self._config = config
        self._project_dir = Path(project_dir) if project_dir is not None else None
        path = Path(config.path)
        if not path.is_absolute() and self._project_dir is not None:
            path = self._project_dir / path
        self._db_path = path

    def connect(self, read_only: bool = False) -> duckdb.DuckDBPyConnection:
        from havn.engine.database import _resolve_memory_limit

        conn = duckdb.connect(str(self._db_path), read_only=read_only)
        conn.execute("SET enable_progress_bar = true")

        if self._config.memory_limit:
            resolved = _resolve_memory_limit(self._config.memory_limit)
            conn.execute(f"SET memory_limit = '{resolved}'")

        import os
        threads = self._config.threads
        max_threads = threads if (threads is not None and threads > 0) else max(1, os.cpu_count() // 2)
        conn.execute(f"SET threads = {max_threads}")

        if self._project_dir is not None:
            from havn.engine.macros import register_macros
            register_macros(conn, self._project_dir)

        return conn

    def status(self) -> BackendStatus:
        exists = self._db_path.exists()
        size = self._db_path.stat().st_size if exists else 0
        return BackendStatus(
            backend="duckdb",
            healthy=True,
            path=str(self._db_path),
            size_bytes=size,
        )

    def exists(self) -> bool:
        return self._db_path.exists()

    def close(self) -> None:
        pass
