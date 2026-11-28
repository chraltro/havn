"""Tests for `havn migrate` (DuckDB <-> DuckLake)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
import yaml

# Catch typer.Exit, not click.exceptions.Exit: newer typer versions vendor
# click (typer._click), so the installed click's Exit is a different class.
import typer

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
    except typer.Exit as e:
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
    except typer.Exit as e:
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


# _havn as a 0.2.x release left it: users without attributes and
# password_iterations, the audit sequence, no schema_migrations table.
_OLD_HAVN = [
    "CREATE SCHEMA _havn",
    "CREATE SEQUENCE _havn.audit_log_seq START 1",
    """CREATE TABLE _havn.audit_log (
        id INTEGER PRIMARY KEY DEFAULT nextval('_havn.audit_log_seq'),
        "user" VARCHAR NOT NULL, action VARCHAR NOT NULL, resource VARCHAR,
        detail VARCHAR, ip_address VARCHAR, "timestamp" TIMESTAMP DEFAULT current_timestamp)""",
    """CREATE TABLE _havn.users (
        username VARCHAR PRIMARY KEY, password_hash VARCHAR NOT NULL,
        password_salt VARCHAR NOT NULL, role VARCHAR NOT NULL DEFAULT 'viewer',
        display_name VARCHAR, created_at TIMESTAMP DEFAULT current_timestamp,
        last_login TIMESTAMP)""",
    """CREATE TABLE _havn.model_state (
        model_path VARCHAR PRIMARY KEY, content_hash VARCHAR NOT NULL,
        upstream_hash VARCHAR NOT NULL, materialized_as VARCHAR NOT NULL,
        last_run_at TIMESTAMP DEFAULT current_timestamp,
        run_duration_ms BIGINT DEFAULT 0, row_count BIGINT DEFAULT 0)""",
    """INSERT INTO _havn.audit_log ("user", action, resource)
        VALUES ('alice', 'query', 'SELECT 1'), ('alice', 'query', 'SELECT 2')""",
    """INSERT INTO _havn.users (username, password_hash, password_salt, role)
        VALUES ('old', 'h', 's', 'admin')""",
    """INSERT INTO _havn.model_state (model_path, content_hash, upstream_hash, materialized_as)
        VALUES ('bronze.customers', 'a', 'b', 'table')""",
    "CREATE SCHEMA bronze",
    "CREATE TABLE bronze.customers AS SELECT i AS id FROM range(10) t(i)",
]


def _seed_old_duckdb_project(tmp_path: Path) -> None:
    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: warehouse.duckdb\n")
    c = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    for stmt in _OLD_HAVN:
        c.execute(stmt)
    c.close()


def _run_migrate(to: str, project_dir: Path) -> None:
    try:
        migrate(to=to, project_dir=project_dir)
    except typer.Exit as e:
        assert e.exit_code in (0, None), f"migrate --to {to} exited with {e.exit_code}"


def _metadata_shape(conn) -> tuple[set, set]:
    """Columns (with type, default, nullability) and constraints of _havn."""
    cols = conn.execute(
        "SELECT table_name, column_name, data_type, column_default, is_nullable "
        "FROM duckdb_columns() WHERE database_name = current_database() "
        "AND schema_name = '_havn'"
    ).fetchall()
    cons = conn.execute(
        "SELECT table_name, constraint_type, constraint_column_names::VARCHAR "
        "FROM duckdb_constraints() WHERE database_name = current_database() "
        "AND schema_name = '_havn'"
    ).fetchall()
    return set(cols), set(cons)


def _fresh_shape(tmp_path: Path, backend: str) -> tuple[set, set]:
    from havn.config import DatabaseConfig
    from havn.engine.backends import create_backend

    root = tmp_path / f"fresh_{backend}"
    root.mkdir()
    if backend == "duckdb":
        cfg = DatabaseConfig(path="w.duckdb")
    else:
        cfg = DatabaseConfig(
            backend="ducklake", catalog=str(root / "c.ducklake"), data_path=str(root / "data")
        )
    c = create_backend(cfg, project_dir=root).connect()
    try:
        return _metadata_shape(c)
    finally:
        c.close()


def _assert_metadata_works(conn, fresh: tuple[set, set], *, lake: bool = False) -> None:
    from havn.engine import auth, masking
    from havn.engine.audit import log_audit
    from havn.engine.migrations import MIGRATIONS, migration_status

    assert _metadata_shape(conn) == fresh
    assert migration_status(conn).up_to_date
    assert conn.execute(
        "SELECT version FROM _havn.schema_migrations ORDER BY 1"
    ).fetchall() == [(m.version,) for m in MIGRATIONS]
    # Rows written by the old release came across.
    assert conn.execute("SELECT username, role FROM _havn.users").fetchall() == [("old", "admin")]
    assert conn.execute("SELECT model_path FROM _havn.model_state").fetchall() == [
        ("bronze.customers",)
    ]
    # The writers work against the copied tables.
    auth.create_user(conn, "bob", "correct horse battery", "editor")
    assert auth.authenticate(conn, "bob", "correct horse battery")
    before = conn.execute("SELECT count(*) FROM _havn.audit_log").fetchone()[0]
    log_audit(conn, "bob", "query", "SELECT 3")
    ids = [r[0] for r in conn.execute("SELECT id FROM _havn.audit_log").fetchall()]
    assert len(ids) == before + 1 and None not in ids and len(set(ids)) == len(ids)
    if not lake:  # create_policy uses INSERT ... RETURNING, which DuckLake lacks
        policy = masking.create_policy(
            conn, schema_name="gold", table_name="t", column_name="c", method="redact"
        )
        assert policy["id"]


@requires_ducklake
def test_migrate_old_warehouse_to_ducklake_gets_the_current_metadata_schema(tmp_path):
    """A warehouse from before the migration system reaches DuckLake with
    every column, default and recorded version a fresh DuckLake warehouse has."""
    fresh = _fresh_shape(tmp_path, "ducklake")
    project = tmp_path / "p"
    project.mkdir()
    _seed_old_duckdb_project(project)

    _run_migrate("ducklake", project)

    c = open_warehouse(load_project(project), project)
    try:
        _assert_metadata_works(c, fresh, lake=True)
        assert c.execute("SELECT count(*) FROM bronze.customers").fetchone()[0] == 10
    finally:
        c.close()


@requires_ducklake
def test_migrate_round_trip_keeps_keys_defaults_and_audit_ids(tmp_path):
    """DuckDB -> DuckLake -> DuckDB ends with the keys, defaults and
    sequence a fresh DuckDB warehouse has, and audit ids that keep counting."""
    from havn.engine.audit import log_audit

    fresh = _fresh_shape(tmp_path, "duckdb")
    project = tmp_path / "p"
    project.mkdir()
    _seed_old_duckdb_project(project)

    _run_migrate("ducklake", project)
    c = open_warehouse(load_project(project), project)
    try:
        log_audit(c, "carol", "query", "on the lake")  # a uuid id on DuckLake
    finally:
        c.close()
    _run_migrate("duckdb", project)

    c = open_warehouse(load_project(project), project)
    try:
        # The lake's uuid row forced a renumbering by time: 1..3, in order.
        assert c.execute(
            'SELECT id, resource FROM _havn.audit_log ORDER BY id'
        ).fetchall() == [(1, "SELECT 1"), (2, "SELECT 2"), (3, "on the lake")]
        _assert_metadata_works(c, fresh)
        assert c.execute("SELECT max(id) FROM _havn.audit_log").fetchone()[0] == 4
    finally:
        c.close()


@requires_ducklake
def test_migrate_to_ducklake_and_back_keeps_integer_audit_ids(tmp_path):
    """Without a lake-written row the ids survive as they were, and the
    DuckDB sequence continues after them."""
    from havn.engine.audit import log_audit

    project = tmp_path / "p"
    project.mkdir()
    _seed_old_duckdb_project(project)
    c = duckdb.connect(str(project / "warehouse.duckdb"))
    c.execute("DELETE FROM _havn.audit_log WHERE resource = 'SELECT 1'")  # ids 2 only
    c.close()

    _run_migrate("ducklake", project)
    _run_migrate("duckdb", project)

    c = open_warehouse(load_project(project), project)
    try:
        assert c.execute("SELECT id FROM _havn.audit_log").fetchall() == [(2,)]
        log_audit(c, "dave", "query", "next")
        assert sorted(r[0] for r in c.execute("SELECT id FROM _havn.audit_log").fetchall()) == [2, 3]
    finally:
        c.close()


def test_migrate_refuses_a_source_newer_than_this_havn(tmp_path):
    from havn.engine.migrations import CURRENT_VERSION

    _seed_duckdb_project(tmp_path)
    c = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    c.execute(
        "INSERT INTO _havn.schema_migrations (version, name) VALUES (?, 'future')",
        [CURRENT_VERSION + 1],
    )
    c.close()
    with pytest.raises(typer.Exit) as info:
        migrate(to="ducklake", project_dir=tmp_path)
    assert info.value.exit_code == 1
    assert (tmp_path / "warehouse.duckdb").exists()
    assert not (tmp_path / ".havn" / "catalog.ducklake").exists()


def test_migrate_noop_when_same_backend(tmp_path):
    _seed_duckdb_project(tmp_path)
    # No-op case returns normally (no Exit raised)
    migrate(to="duckdb", project_dir=tmp_path)


def test_migrate_rejects_invalid_target(tmp_path):
    _seed_duckdb_project(tmp_path)
    try:
        migrate(to="sqlite", project_dir=tmp_path)
        raise AssertionError("expected Exit")
    except typer.Exit as e:
        assert e.exit_code == 1
    except SystemExit as e:
        assert e.code == 1
