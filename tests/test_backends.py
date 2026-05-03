"""Tests for the WarehouseBackend abstraction (SPEC 01)."""

from __future__ import annotations

import os
import pathlib

import duckdb
import pytest

from havn.config import DatabaseConfig
from havn.engine.backends import (
    DuckDBBackend,
    DuckLakeBackend,
    WarehouseBackend,
    create_backend,
)


# --- Factory -----------------------------------------------------------------


def test_factory_dispatches_duckdb(tmp_path):
    cfg = DatabaseConfig(backend="duckdb", path=str(tmp_path / "w.duckdb"))
    b = create_backend(cfg)
    assert isinstance(b, DuckDBBackend)
    assert b.name == "duckdb"


def test_factory_dispatches_ducklake(tmp_path):
    (tmp_path / "data").mkdir()
    cfg = DatabaseConfig(
        backend="ducklake",
        catalog=str(tmp_path / "catalog.ducklake"),
        data_path=str(tmp_path / "data"),
    )
    b = create_backend(cfg)
    assert isinstance(b, DuckLakeBackend)
    assert b.name == "ducklake"


def test_factory_rejects_unknown_backend():
    cfg = DatabaseConfig.model_construct(backend="sqlite")
    with pytest.raises(ValueError, match="Unknown backend"):
        create_backend(cfg)


def test_backends_satisfy_protocol(tmp_path):
    (tmp_path / "data").mkdir()
    duck = DuckDBBackend(DatabaseConfig(path=str(tmp_path / "w.duckdb")))
    lake = DuckLakeBackend(
        DatabaseConfig(
            backend="ducklake",
            catalog=str(tmp_path / "c.ducklake"),
            data_path=str(tmp_path / "data"),
        )
    )
    assert isinstance(duck, WarehouseBackend)
    assert isinstance(lake, WarehouseBackend)


# --- DuckDB backend ----------------------------------------------------------


def test_duckdb_backend_connect_roundtrip(tmp_path):
    b = DuckDBBackend(DatabaseConfig(path=str(tmp_path / "w.duckdb")))
    c = b.connect()
    c.execute("CREATE TABLE t AS SELECT 1 AS x, 'hello' AS s")
    assert c.execute("SELECT * FROM t").fetchone() == (1, "hello")
    c.close()
    assert b.exists()


def test_duckdb_backend_relative_path_resolves_against_project_dir(tmp_path):
    b = DuckDBBackend(DatabaseConfig(path="warehouse.duckdb"), project_dir=tmp_path)
    c = b.connect()
    c.execute("CREATE TABLE t AS SELECT 1 AS x")
    c.close()
    assert (tmp_path / "warehouse.duckdb").exists()


def test_duckdb_backend_status(tmp_path):
    b = DuckDBBackend(DatabaseConfig(path=str(tmp_path / "w.duckdb")))
    st = b.status()
    assert st["backend"] == "duckdb"
    # Status of a non-existent warehouse is still healthy (just empty)
    assert st["healthy"] is True
    # Create the DB
    b.connect().close()
    st = b.status()
    assert st["size_bytes"] > 0


def test_duckdb_backend_read_only(tmp_path):
    b = DuckDBBackend(DatabaseConfig(path=str(tmp_path / "w.duckdb")))
    # Create first
    c = b.connect()
    c.execute("CREATE TABLE t AS SELECT 1 AS x")
    c.close()
    # Read-only connection cannot write
    c = b.connect(read_only=True)
    with pytest.raises(duckdb.Error):
        c.execute("CREATE TABLE u AS SELECT 2 AS y")
    c.close()


def test_duckdb_backend_applies_threads(tmp_path):
    b = DuckDBBackend(
        DatabaseConfig(path=str(tmp_path / "w.duckdb"), threads=2)
    )
    c = b.connect()
    row = c.execute("SELECT current_setting('threads')").fetchone()
    assert int(row[0]) == 2
    c.close()


def test_progress_bar_disabled_on_non_tty(tmp_path, monkeypatch):
    """DuckDB's progress bar floods non-TTY stdout with carriage-return
    updates. Make sure it's off when stdout isn't a TTY."""
    import sys
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    monkeypatch.delenv("HAVN_PROGRESS", raising=False)
    b = DuckDBBackend(DatabaseConfig(path=str(tmp_path / "w.duckdb")))
    c = b.connect()
    row = c.execute("SELECT current_setting('enable_progress_bar')").fetchone()
    assert str(row[0]).lower() in ("false", "0"), row
    c.close()


def test_progress_bar_env_override(tmp_path, monkeypatch):
    """HAVN_PROGRESS=1 must force-enable even on a non-TTY (CI debugging)."""
    import sys
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    monkeypatch.setenv("HAVN_PROGRESS", "1")
    b = DuckDBBackend(DatabaseConfig(path=str(tmp_path / "w.duckdb")))
    c = b.connect()
    row = c.execute("SELECT current_setting('enable_progress_bar')").fetchone()
    assert str(row[0]).lower() in ("true", "1"), row
    c.close()


# --- DuckLake backend --------------------------------------------------------


def _ducklake_available() -> bool:
    """Probe whether the DuckDB `ducklake` extension can be installed/loaded.

    INSTALL ducklake reaches out to extensions.duckdb.org. In sandboxed CI
    environments where that host is firewalled, the call fails with HTTP 403.
    We cache the result on the function object so the probe runs once per
    test session.
    """
    cached = getattr(_ducklake_available, "_cached", None)
    if cached is not None:
        return cached
    try:
        c = duckdb.connect(":memory:")
        try:
            c.execute("INSTALL ducklake")
            c.execute("LOAD ducklake")
            result = True
        finally:
            c.close()
    except Exception:
        result = False
    _ducklake_available._cached = result
    return result


requires_ducklake = pytest.mark.skipif(
    not _ducklake_available(),
    reason="ducklake extension not installable (network restricted or unsupported)",
)


def _lake_backend(tmp_path, **overrides):
    (tmp_path / "data").mkdir(exist_ok=True)
    cfg = DatabaseConfig(
        backend="ducklake",
        catalog=str(tmp_path / "catalog.ducklake"),
        data_path=str(tmp_path / "data"),
        **overrides,
    )
    return DuckLakeBackend(cfg, project_dir=tmp_path)


@requires_ducklake
def test_ducklake_backend_connect_roundtrip(tmp_path):
    b = _lake_backend(tmp_path)
    c = b.connect()
    c.execute("CREATE SCHEMA silver")
    c.execute("CREATE TABLE silver.t AS SELECT 1 AS x, 'hello' AS s")
    c.execute("INSERT INTO silver.t VALUES (2, 'world')")
    rows = c.execute("SELECT x FROM silver.t ORDER BY x").fetchall()
    assert [r[0] for r in rows] == [1, 2]
    c.close()


@requires_ducklake
def test_ducklake_backend_status_tracks_snapshots(tmp_path):
    b = _lake_backend(tmp_path)
    c = b.connect()
    c.execute("CREATE SCHEMA silver")
    c.execute("CREATE TABLE silver.t AS SELECT 1 AS x")
    c.close()
    st = b.status()
    assert st["backend"] == "ducklake"
    assert st["healthy"] is True
    assert st["catalog_reachable"] is True
    assert st["snapshot_count"] >= 2


def test_ducklake_backend_status_unreachable_for_bad_catalog(tmp_path):
    b = DuckLakeBackend(
        DatabaseConfig(
            backend="ducklake",
            catalog="postgres:dbname=nonexistent host=127.0.0.1 port=1",
            data_path=str(tmp_path / "data"),
        )
    )
    st = b.status()
    assert st["backend"] == "ducklake"
    assert st["healthy"] is False
    assert st["catalog_reachable"] is False
    assert "error" in st


@requires_ducklake
def test_ducklake_backend_time_travel(tmp_path):
    b = _lake_backend(tmp_path)
    c = b.connect()
    c.execute("CREATE SCHEMA silver")
    c.execute("CREATE TABLE silver.t AS SELECT 1 AS x")
    snap1 = c.execute(
        "SELECT max(snapshot_id) FROM ducklake_snapshots('warehouse')"
    ).fetchone()[0]
    c.execute("INSERT INTO silver.t VALUES (2)")
    c.execute("INSERT INTO silver.t VALUES (3)")
    # Current state: 3 rows
    assert c.execute("SELECT count(*) FROM silver.t").fetchone()[0] == 3
    # Time-travel back to snap1: 1 row
    past = c.execute(
        f"SELECT count(*) FROM silver.t AT (VERSION => {snap1})"
    ).fetchone()[0]
    assert past == 1
    c.close()


@requires_ducklake
def test_ducklake_backend_relative_paths_resolve_against_project_dir(tmp_path):
    (tmp_path / ".havn" / "data").mkdir(parents=True)
    b = DuckLakeBackend(
        DatabaseConfig(
            backend="ducklake",
            catalog=".havn/catalog.ducklake",
            data_path=".havn/data",
        ),
        project_dir=tmp_path,
    )
    c = b.connect()
    c.execute("CREATE SCHEMA bronze")
    c.execute("CREATE TABLE bronze.x AS SELECT 1")
    c.close()
    assert (tmp_path / ".havn" / "catalog.ducklake").exists()


@requires_ducklake
def test_ducklake_backend_read_only(tmp_path):
    b = _lake_backend(tmp_path)
    c = b.connect()
    c.execute("CREATE SCHEMA bronze")
    c.execute("CREATE TABLE bronze.t AS SELECT 1 AS x")
    c.close()
    c = b.connect(read_only=True)
    with pytest.raises(duckdb.Error):
        c.execute("INSERT INTO bronze.t VALUES (2)")
    c.close()


# --- open_warehouse helper ---------------------------------------------------


def test_open_warehouse_with_database_config(tmp_path):
    from havn.engine.database import open_warehouse

    cfg = DatabaseConfig(path="w.duckdb")
    c = open_warehouse(cfg, tmp_path)
    c.execute("CREATE TABLE t AS SELECT 1")
    c.close()
    assert (tmp_path / "w.duckdb").exists()


def test_open_warehouse_with_project_config(tmp_path):
    from havn.config import load_project
    from havn.engine.database import open_warehouse

    (tmp_path / "project.yml").write_text(
        "name: t\ndatabase:\n  path: w.duckdb\n"
    )
    cfg = load_project(tmp_path)
    c = open_warehouse(cfg)
    c.execute("CREATE TABLE t AS SELECT 1")
    c.close()
    assert (tmp_path / "w.duckdb").exists()


def test_open_warehouse_rejects_bad_type():
    from havn.engine.database import open_warehouse

    with pytest.raises(TypeError):
        open_warehouse("not a config")  # type: ignore[arg-type]
