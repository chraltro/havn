"""Tests for `havn migrate` (DuckDB <-> DuckLake)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
import yaml

import click

from havn.cli.migrate import migrate
from havn.config import load_project
from havn.engine.database import ensure_meta_table, open_warehouse


def _ducklake_available() -> bool:
    """Probe whether the DuckDB ducklake extension can be installed/loaded.

    Cached on the function object so it runs once per test session. Mirrors
    the helper in tests/test_backends.py — duplicated here rather than shared
    via conftest so the suites stay decoupled.
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


def _seed_duckdb_project(tmp_path: Path) -> None:
    """Create a minimal project with a populated DuckDB warehouse."""
    (tmp_path / "project.yml").write_text(
        "name: t\ndatabase:\n  path: warehouse.duckdb\n"
    )
    cfg = load_project(tmp_path)
    c = open_warehouse(cfg, tmp_path)
    ensure_meta_table(c)
    c.execute("CREATE SCHEMA bronze")
    c.execute(
        "CREATE TABLE bronze.customers AS "
        "SELECT i AS id, 'c_' || i AS name FROM range(100) t(i)"
    )
    c.execute("CREATE SCHEMA silver")
    c.execute("CREATE TABLE silver.summary AS SELECT count(*) AS n FROM bronze.customers")
    c.close()


@requires_ducklake
def test_migrate_duckdb_to_ducklake_and_back(tmp_path, capsys):
    _seed_duckdb_project(tmp_path)
    (tmp_path / ".havn" / "data").mkdir(parents=True, exist_ok=True)

    # Migrate: duckdb -> ducklake  (typer commands raise Exit even on success)
    try:
        migrate(to="ducklake", project_dir=tmp_path)
    except click.exceptions.Exit as e:
        assert e.exit_code in (0, None), f"migrate to ducklake exited with {e.exit_code}"
    except SystemExit as e:
        assert e.code in (0, None), f"migrate to ducklake exited with {e.code}"

    # project.yml rewritten
    raw = yaml.safe_load((tmp_path / "project.yml").read_text())
    assert raw["database"]["backend"] == "ducklake"
    assert "path" not in raw["database"]
    assert raw["database"]["catalog"] == ".havn/catalog.ducklake"

    # Source backed up
    assert (tmp_path / "warehouse.duckdb.backup").exists()
    # Original warehouse file moved
    assert not (tmp_path / "warehouse.duckdb").exists()

    # Data present in DuckLake
    cfg = load_project(tmp_path)
    c = open_warehouse(cfg, tmp_path, read_only=True)
    assert c.execute("SELECT count(*) FROM bronze.customers").fetchone()[0] == 100
    assert c.execute("SELECT n FROM silver.summary").fetchone()[0] == 100
    c.close()

    # Migrate back: ducklake -> duckdb
    try:
        migrate(to="duckdb", project_dir=tmp_path)
    except click.exceptions.Exit as e:
        assert e.exit_code in (0, None), f"migrate back exited with {e.exit_code}"
    except SystemExit as e:
        assert e.code in (0, None)

    raw = yaml.safe_load((tmp_path / "project.yml").read_text())
    assert raw["database"]["backend"] == "duckdb"
    assert "catalog" not in raw["database"]

    # Catalog backed up
    assert (tmp_path / ".havn" / "catalog.ducklake.backup").exists()

    cfg = load_project(tmp_path)
    c = open_warehouse(cfg, tmp_path, read_only=True)
    assert c.execute("SELECT count(*) FROM bronze.customers").fetchone()[0] == 100
    c.close()


def test_migrate_noop_when_same_backend(tmp_path):
    _seed_duckdb_project(tmp_path)
    # No-op case returns normally (no Exit raised)
    migrate(to="duckdb", project_dir=tmp_path)


def test_migrate_rejects_invalid_target(tmp_path):
    _seed_duckdb_project(tmp_path)
    try:
        migrate(to="sqlite", project_dir=tmp_path)
        raise AssertionError("expected Exit")
    except click.exceptions.Exit as e:
        assert e.exit_code == 1
    except SystemExit as e:
        assert e.code == 1
