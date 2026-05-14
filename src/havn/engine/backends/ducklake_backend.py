"""DuckLake backend: DuckDB compute + Postgres/file catalog + local/S3 Parquet.

DuckLake stores analytical data as Parquet files addressed by a catalog
(Postgres or a .ducklake file). Every connect() returns a fresh in-memory
DuckDB that ATTACHes the catalog, so we inherit DuckDB's multi-connection
semantics without the single-writer-per-file constraint of the DuckDB backend.

The `_havn` metadata schema lives inside DuckLake alongside the analytical
schemas. DuckLake does not enforce PRIMARY KEY constraints, but no code path
in havn depends on PK enforcement (uuid defaults + INSERT OR REPLACE patterns).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import duckdb

from havn.config import DatabaseConfig
from havn.engine.backends.base import BackendStatus

logger = logging.getLogger("havn.backends.ducklake")

_extension_install_lock = threading.Lock()
_extension_install_done = False


class DuckLakeBackend:
    name = "ducklake"

    _STATUS_CACHE_TTL = 5.0

    def __init__(self, config: DatabaseConfig, project_dir: str | Path | None = None):
        self._config = config
        self._project_dir = Path(project_dir) if project_dir is not None else None
        self._catalog = self._resolve(config.catalog or "")
        self._data_path = self._resolve(config.data_path or "")
        self._encrypted = config.encrypted
        self._status_cache: BackendStatus | None = None
        self._status_cache_at = 0.0
        self._status_lock = threading.Lock()

    def _resolve(self, spec: str) -> str:
        """Resolve a relative catalog or data_path against project_dir.

        Leaves URIs (postgres:, s3://) untouched. Normalizes local paths to
        POSIX-style (forward slashes) so DuckLake's internal path comparison
        matches across connections on Windows.
        """
        if not spec:
            return spec
        if spec.startswith("postgres:") or spec.startswith("s3://"):
            return spec
        p = Path(spec)
        if not p.is_absolute() and self._project_dir is not None:
            p = self._project_dir / p
        return p.as_posix()

    def _eager_install(self) -> None:
        """Pre-install the DuckLake extension once per process."""
        global _extension_install_done
        if _extension_install_done:
            return
        with _extension_install_lock:
            if _extension_install_done:
                return
            try:
                conn = duckdb.connect(":memory:")
                try:
                    conn.execute("INSTALL ducklake")
                finally:
                    conn.close()
                _extension_install_done = True
            except Exception as e:
                logger.warning("DuckLake extension pre-install failed: %s", e)

    def _load_extensions(self, conn: duckdb.DuckDBPyConnection) -> None:
        conn.execute("INSTALL ducklake")
        conn.execute("LOAD ducklake")
        if self._catalog.startswith("postgres:"):
            conn.execute("INSTALL postgres")
            conn.execute("LOAD postgres")
        if self._data_path.startswith("s3://"):
            conn.execute("INSTALL httpfs")
            conn.execute("LOAD httpfs")
            self._register_s3_secret(conn)

    def _register_s3_secret(self, conn: duckdb.DuckDBPyConnection) -> None:
        endpoint = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
        access_key = os.environ.get("MINIO_ACCESS_KEY", "")
        secret_key = os.environ.get("MINIO_SECRET_KEY", "")
        use_ssl = os.environ.get("MINIO_USE_SSL", "false").lower() == "true"
        # Escape single quotes in env-supplied strings to prevent SQL injection
        # into the CREATE SECRET DDL.
        def _esc(v: str) -> str:
            return v.replace("'", "''")
        conn.execute(
            f"""
            CREATE OR REPLACE SECRET minio_secret (
                TYPE S3,
                KEY_ID '{_esc(access_key)}',
                SECRET '{_esc(secret_key)}',
                ENDPOINT '{_esc(endpoint)}',
                URL_STYLE 'path',
                USE_SSL {str(use_ssl).lower()},
                REGION 'us-east-1'
            )
            """
        )

    def _ensure_dirs(self) -> None:
        """Make sure the catalog's parent directory and the data path exist.

        ATTACH will create a fresh catalog file on first use, but it won't
        create the parent directory. For a local Postgres or S3 catalog
        there's nothing to create; skip in that case.
        """
        if not self._catalog.startswith("postgres:"):
            cat_path = Path(self._catalog)
            cat_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._data_path.startswith("s3://"):
            Path(self._data_path).mkdir(parents=True, exist_ok=True)

    def _attach(self, conn: duckdb.DuckDBPyConnection, read_only: bool) -> None:
        opts = [f"DATA_PATH '{self._data_path}'"]
        if self._config.metadata_schema:
            opts.append(f"METADATA_SCHEMA '{self._config.metadata_schema}'")
        if self._encrypted:
            opts.append("ENCRYPTED")
        if read_only:
            opts.append("READ_ONLY")
        # Auto-migrate older catalog versions (e.g. 0.3 created by a
        # pre-1.0 DuckLake extension) so users don't have to re-init
        # projects after the extension upgrades.
        opts.append("AUTOMATIC_MIGRATION TRUE")
        opts_str = ", ".join(opts)
        conn.execute(f"ATTACH 'ducklake:{self._catalog}' AS warehouse ({opts_str})")
        conn.execute("USE warehouse")

    def _apply_settings(self, conn: duckdb.DuckDBPyConnection) -> None:
        from havn.engine.database import _progress_bar_enabled, _resolve_memory_limit

        progress = "true" if _progress_bar_enabled() else "false"
        conn.execute(f"SET enable_progress_bar = {progress}")
        if self._config.memory_limit:
            resolved = _resolve_memory_limit(self._config.memory_limit)
            conn.execute(f"SET memory_limit = '{resolved}'")
        threads = self._config.threads
        cpu = os.cpu_count() or 2
        max_threads = threads if (threads is not None and threads > 0) else max(1, cpu // 2)
        conn.execute(f"SET threads = {max_threads}")

    def connect(self, read_only: bool = False) -> duckdb.DuckDBPyConnection:
        self._eager_install()
        self._ensure_dirs()
        conn = duckdb.connect(":memory:")
        self._load_extensions(conn)
        self._attach(conn, read_only)
        # Tag the connection so cursor_for() knows to USE warehouse on
        # cursors derived from it. DuckDB cursors do not inherit the
        # parent's USE state, and havn has many call sites that take
        # cursors off the shared write/read connections.
        from havn.engine.write_queue import tag_default_catalog
        tag_default_catalog(conn, "warehouse")
        self._apply_settings(conn)
        if self._project_dir is not None:
            from havn.engine.macros import register_macros
            register_macros(conn, self._project_dir)
        return conn

    def status(self) -> BackendStatus:
        now = time.monotonic()
        with self._status_lock:
            if (
                self._status_cache is not None
                and (now - self._status_cache_at) < self._STATUS_CACHE_TTL
            ):
                return self._status_cache
        try:
            conn = self.connect(read_only=True)
            try:
                count = conn.execute(
                    "SELECT count(*) FROM ducklake_snapshots('warehouse')"
                ).fetchone()[0]
            finally:
                conn.close()
            result = BackendStatus(
                backend="ducklake",
                healthy=True,
                catalog=self._catalog,
                data_path=self._data_path,
                encrypted=self._encrypted,
                snapshot_count=int(count),
                catalog_reachable=True,
            )
        except Exception as e:
            result = BackendStatus(
                backend="ducklake",
                healthy=False,
                catalog=self._catalog,
                data_path=self._data_path,
                encrypted=self._encrypted,
                catalog_reachable=False,
                error=str(e),
            )
        with self._status_lock:
            self._status_cache = result
            self._status_cache_at = time.monotonic()
        return result

    def exists(self) -> bool:
        # For a Postgres catalog, "exists" means reachable. For a local file
        # catalog, existence is enough — the file will be created by ATTACH
        # on first connect otherwise.
        if self._catalog.startswith("postgres:"):
            st = self.status()
            return bool(st.get("catalog_reachable"))
        return Path(self._catalog).exists()

    def close(self) -> None:
        pass
