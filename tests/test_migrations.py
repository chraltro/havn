"""Metadata schema migrations (engine/migrations).

Real temporary DuckDB files throughout. The "old release" warehouses are
built by hand from the DDL those releases shipped (git tags v0.2.16 and
v0.3.0/v0.3.1), with rows in them, and then upgraded.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from havn.engine import migrations
from havn.engine.database import ensure_meta_table
from havn.engine.migrations import (
    CURRENT_VERSION,
    MIGRATIONS,
    Migration,
    MigrationError,
    apply_migrations,
    ensure_migrated,
    forget_database,
    migration_status,
)
from havn.engine.migrations._0001_baseline import (
    ADDED_AFTER_FIRST_RELEASE,
    TABLES,
    column_name,
)

SRC = Path(__file__).resolve().parent.parent / "src" / "havn"


def _columns(conn) -> dict[str, list[str]]:
    rows = conn.execute(
        "SELECT table_name, column_name FROM duckdb_columns() "
        "WHERE database_name = current_database() AND schema_name = '_havn' "
        "ORDER BY table_name, column_index"
    ).fetchall()
    out: dict[str, list[str]] = {}
    for t, c in rows:
        out.setdefault(t, []).append(c)
    return out


def _recorded(conn) -> list[tuple[int, str]]:
    return conn.execute(
        "SELECT version, name FROM _havn.schema_migrations ORDER BY version"
    ).fetchall()


def _assert_full_schema(conn) -> None:
    have = _columns(conn)
    for table in TABLES:
        assert table.name in have, f"missing table _havn.{table.name}"
        expected = {column_name(c) for c in table.columns}
        missing = expected - set(have[table.name])
        assert not missing, f"_havn.{table.name} lacks {sorted(missing)}"


# ---------------------------------------------------------------------------
# Fresh warehouses
# ---------------------------------------------------------------------------


def test_fresh_warehouse_reaches_current_version(tmp_path):
    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    applied = apply_migrations(conn)
    assert [m.version for m in applied] == [m.version for m in MIGRATIONS]
    assert _recorded(conn) == [(m.version, m.name) for m in MIGRATIONS]
    _assert_full_schema(conn)
    st = migration_status(conn)
    assert st.version == CURRENT_VERSION and st.up_to_date
    havn_version = conn.execute("SELECT DISTINCT havn_version FROM _havn.schema_migrations").fetchall()
    assert len(havn_version) == 1 and havn_version[0][0]
    conn.close()


def test_running_twice_is_a_no_op(tmp_path):
    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    apply_migrations(conn)
    before = conn.execute("SELECT version, applied_at FROM _havn.schema_migrations ORDER BY 1").fetchall()
    assert apply_migrations(conn) == []
    ensure_meta_table(conn)
    forget_database(tmp_path / "w.duckdb")
    ensure_meta_table(conn)
    after = conn.execute("SELECT version, applied_at FROM _havn.schema_migrations ORDER BY 1").fetchall()
    assert before == after
    conn.close()


def test_writable_backend_connect_migrates_and_read_only_does_not(tmp_path):
    from havn.config import DatabaseConfig
    from havn.engine.backends import create_backend

    duckdb.connect(str(tmp_path / "w.duckdb")).close()
    backend = create_backend(DatabaseConfig(path="w.duckdb"), project_dir=tmp_path)

    ro = backend.connect(read_only=True)
    try:
        assert migration_status(ro).version == 0
        assert "_havn" not in {r[0] for r in ro.execute(
            "SELECT schema_name FROM duckdb_schemas() WHERE database_name = current_database()"
        ).fetchall()}
    finally:
        ro.close()

    rw = backend.connect()
    try:
        assert migration_status(rw).up_to_date
    finally:
        rw.close()


def test_read_only_connection_never_migrates(tmp_path):
    path = tmp_path / "w.duckdb"
    seed = duckdb.connect(str(path))
    seed.execute("CREATE SCHEMA landing")
    seed.close()

    ro = duckdb.connect(str(path), read_only=True)
    try:
        ensure_meta_table(ro)  # no-op, no error
        ensure_migrated(ro)
        st = migration_status(ro)
        assert st.version == 0 and len(st.pending) == len(MIGRATIONS)
        with pytest.raises(duckdb.InvalidInputException):
            apply_migrations(ro)
    finally:
        ro.close()

    check = duckdb.connect(str(path), read_only=True)
    try:
        assert check.execute(
            "SELECT count(*) FROM duckdb_schemas() WHERE schema_name = '_havn'"
        ).fetchone()[0] == 0
    finally:
        check.close()


# ---------------------------------------------------------------------------
# Upgrading warehouses written by older releases
# ---------------------------------------------------------------------------

# DDL exactly as v0.2.16 created it (abridged to the tables it shipped that
# later grew columns, plus a few that did not).
_V0_2_16 = [
    """CREATE TABLE _havn.model_state (
        model_path VARCHAR PRIMARY KEY, content_hash VARCHAR NOT NULL,
        upstream_hash VARCHAR NOT NULL, materialized_as VARCHAR NOT NULL,
        last_run_at TIMESTAMP DEFAULT current_timestamp,
        run_duration_ms BIGINT DEFAULT 0, row_count BIGINT DEFAULT 0)""",
    """CREATE TABLE _havn.run_log (
        run_id VARCHAR DEFAULT gen_random_uuid()::VARCHAR, pipeline_run_id VARCHAR,
        run_type VARCHAR NOT NULL, target VARCHAR NOT NULL, status VARCHAR NOT NULL,
        started_at TIMESTAMP DEFAULT current_timestamp, finished_at TIMESTAMP,
        duration_ms BIGINT, rows_affected BIGINT DEFAULT 0, error VARCHAR, log_output VARCHAR)""",
    """CREATE TABLE _havn.assertion_results (
        id VARCHAR DEFAULT gen_random_uuid()::VARCHAR, model_path VARCHAR NOT NULL,
        expression VARCHAR NOT NULL, passed BOOLEAN NOT NULL, detail VARCHAR,
        checked_at TIMESTAMP DEFAULT current_timestamp)""",
    """CREATE TABLE _havn.pr_builds (
        id VARCHAR PRIMARY KEY, pr_id VARCHAR NOT NULL, branch_head VARCHAR,
        status VARCHAR NOT NULL DEFAULT 'running',
        started_at TIMESTAMP DEFAULT current_timestamp, finished_at TIMESTAMP,
        duration_ms BIGINT, data_diff JSON, lineage_impact JSON,
        contract_results JSON, error VARCHAR)""",
    """CREATE TABLE _havn.job_runs (
        id VARCHAR PRIMARY KEY DEFAULT gen_random_uuid()::VARCHAR,
        job_name VARCHAR NOT NULL, job_file VARCHAR NOT NULL, target VARCHAR NOT NULL,
        status VARCHAR NOT NULL DEFAULT 'running', steps_total INTEGER NOT NULL DEFAULT 0,
        steps_completed INTEGER NOT NULL DEFAULT 0, steps_failed INTEGER NOT NULL DEFAULT 0,
        steps_skipped INTEGER NOT NULL DEFAULT 0, started_at TIMESTAMP DEFAULT current_timestamp,
        finished_at TIMESTAMP, duration_ms BIGINT, trigger VARCHAR DEFAULT 'manual',
        error VARCHAR, step_details JSON)""",
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
    """CREATE TABLE _havn.tokens (
        token VARCHAR PRIMARY KEY, username VARCHAR NOT NULL,
        created_at TIMESTAMP DEFAULT current_timestamp, expires_at TIMESTAMP)""",
    """CREATE TABLE _havn.contract_results (
        id VARCHAR DEFAULT gen_random_uuid()::VARCHAR, contract_name VARCHAR NOT NULL,
        model VARCHAR NOT NULL, passed BOOLEAN NOT NULL,
        severity VARCHAR NOT NULL DEFAULT 'error', detail JSON,
        checked_at TIMESTAMP DEFAULT current_timestamp)""",
    """CREATE TABLE _havn.cdc_state (
        connector_name VARCHAR NOT NULL, table_name VARCHAR NOT NULL,
        cdc_mode VARCHAR NOT NULL DEFAULT 'full_refresh', watermark_value VARCHAR,
        file_mtime DOUBLE, last_sync_at TIMESTAMP DEFAULT current_timestamp,
        rows_synced BIGINT DEFAULT 0, PRIMARY KEY (connector_name, table_name))""",
]


def _old_warehouse(path: Path) -> None:
    conn = duckdb.connect(str(path))
    conn.execute("CREATE SCHEMA _havn")
    for ddl in _V0_2_16:
        conn.execute(ddl)
    conn.execute(
        "INSERT INTO _havn.model_state (model_path, content_hash, upstream_hash, materialized_as, row_count) "
        "VALUES ('silver.customers', 'abc', 'def', 'table', 42)"
    )
    conn.execute(
        "INSERT INTO _havn.run_log (run_type, target, status, duration_ms) "
        "VALUES ('transform', 'silver.customers', 'success', 120)"
    )
    conn.execute(
        "INSERT INTO _havn.assertion_results (model_path, expression, passed) "
        "VALUES ('silver.customers', 'row_count > 0', true)"
    )
    conn.execute(
        "INSERT INTO _havn.pr_builds (id, pr_id, status) VALUES ('b1', 'pr-1', 'success')"
    )
    conn.execute(
        "INSERT INTO _havn.job_runs (job_name, job_file, target) VALUES ('nightly', 'n.yml', 'gold.*')"
    )
    conn.execute(
        "INSERT INTO _havn.audit_log (\"user\", action, resource) VALUES ('alice', 'query', 'SELECT 1')"
    )
    conn.execute(
        "INSERT INTO _havn.users (username, password_hash, password_salt, role) "
        "VALUES ('alice', 'h', 's', 'admin')"
    )
    conn.execute(
        "INSERT INTO _havn.contract_results (contract_name, model, passed) VALUES ('c', 'gold.x', false)"
    )
    conn.execute(
        "INSERT INTO _havn.cdc_state (connector_name, table_name, watermark_value) VALUES ('pg', 'users', '7')"
    )
    conn.close()


def test_old_release_warehouse_upgrades_with_data_intact(tmp_path):
    path = tmp_path / "w.duckdb"
    _old_warehouse(path)

    conn = duckdb.connect(str(path))
    assert migration_status(conn).version == 0
    apply_migrations(conn)
    _assert_full_schema(conn)
    assert _recorded(conn) == [(m.version, m.name) for m in MIGRATIONS]

    # Every column added after a table first shipped is now there.
    have = _columns(conn)
    for table, cols in ADDED_AFTER_FIRST_RELEASE.items():
        assert set(cols) <= set(have[table]), table

    # Old rows survive, with defaults/NULL in the new columns.
    assert conn.execute(
        "SELECT model_path, content_hash, row_count FROM _havn.model_state"
    ).fetchall() == [("silver.customers", "abc", 42)]
    assert conn.execute(
        "SELECT expression, passed, severity, owner FROM _havn.assertion_results"
    ).fetchall() == [("row_count > 0", True, "error", "")]
    assert conn.execute("SELECT pr_id, metric_diff FROM _havn.pr_builds").fetchall() == [("pr-1", None)]
    assert conn.execute(
        "SELECT role, attributes, password_iterations FROM _havn.users"
    ).fetchall() == [("admin", None, None)]
    assert conn.execute(
        "SELECT passed, consecutive_failures FROM _havn.contract_results"
    ).fetchall() == [(False, 0)]
    assert conn.execute("SELECT watermark_value FROM _havn.cdc_state").fetchall() == [("7",)]

    # Tables that release did not have exist now and work.
    conn.execute(
        "INSERT INTO _havn.batch_state (model_path, window_start, window_end, status) "
        "VALUES ('gold.events', TIMESTAMP '2024-01-01', TIMESTAMP '2024-01-02', 'done')"
    )
    conn.execute("INSERT INTO _havn.live_sources (source, kind, watermark) VALUES ('landing.e', 'table', 3)")

    # The writers in the code base work against the upgraded tables.
    from havn.engine.audit import log_audit, query_audit_log
    from havn.engine.database import log_run

    log_audit(conn, "bob", "query", "SELECT 2")
    ids = [r["id"] for r in query_audit_log(conn, limit=10)]
    assert sorted(ids) == [1, 2]
    log_run(conn, "transform", "gold.x", "success", duration_ms=5, pipeline_run_id="p1")
    assert conn.execute("SELECT count(*) FROM _havn.run_log").fetchone()[0] == 2
    conn.close()


def test_v0_3_users_table_gains_password_iterations(tmp_path):
    """0.3.0/0.3.1 had users.attributes but not password_iterations."""
    path = tmp_path / "w.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE SCHEMA _havn")
    conn.execute(
        "CREATE TABLE _havn.users (username VARCHAR PRIMARY KEY, password_hash VARCHAR NOT NULL, "
        "password_salt VARCHAR NOT NULL, role VARCHAR NOT NULL DEFAULT 'viewer', display_name VARCHAR, "
        "created_at TIMESTAMP DEFAULT current_timestamp, last_login TIMESTAMP, attributes JSON)"
    )
    conn.execute(
        "INSERT INTO _havn.users (username, password_hash, password_salt, attributes) "
        "VALUES ('u', 'h', 's', '{\"region\": \"north\"}')"
    )
    ensure_meta_table(conn)
    assert conn.execute(
        "SELECT attributes->>'region', password_iterations FROM _havn.users"
    ).fetchall() == [("north", None)]
    conn.close()


def test_mixed_case_keys_with_two_spellings_do_not_collide(tmp_path):
    """Migration 2 keeps one row when two mixed-case spellings share a key."""
    path = tmp_path / "w.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE SCHEMA _havn")
    conn.execute(_V0_2_16[0])
    conn.execute(
        "INSERT INTO _havn.model_state (model_path, content_hash, upstream_hash, materialized_as) "
        "VALUES ('Silver.customers', 'a', '', 'table'), ('silver.Customers', 'b', '', 'table')"
    )
    apply_migrations(conn)
    rows = conn.execute("SELECT model_path FROM _havn.model_state").fetchall()
    assert rows == [("silver.customers",)]
    conn.close()


# ---------------------------------------------------------------------------
# Failure, newer warehouses, concurrency
# ---------------------------------------------------------------------------


def _broken(conn, ctx):
    conn.execute("CREATE TABLE _havn.half_done (x INTEGER)")
    conn.execute("ALTER TABLE _havn.model_state ADD COLUMN half_col INTEGER")
    conn.execute("SELECT * FROM _havn.table_that_does_not_exist")


def test_failing_migration_rolls_back_is_not_recorded_and_raises(tmp_path, monkeypatch):
    path = tmp_path / "w.duckdb"
    conn = duckdb.connect(str(path))
    apply_migrations(conn)

    broken = Migration(CURRENT_VERSION + 1, "broken", _broken)
    monkeypatch.setattr(migrations, "MIGRATIONS", MIGRATIONS + (broken,))
    monkeypatch.setattr(migrations, "CURRENT_VERSION", broken.version)

    with pytest.raises(MigrationError) as info:
        apply_migrations(conn)
    message = str(info.value)
    assert f"migration {broken.version} (broken)" in message
    assert info.value.migration is broken

    assert [v for v, _ in _recorded(conn)] == [m.version for m in MIGRATIONS]
    tables = {r[0] for r in conn.execute(
        "SELECT table_name FROM duckdb_tables() WHERE schema_name = '_havn'"
    ).fetchall()}
    assert "half_done" not in tables
    assert "half_col" not in _columns(conn)["model_state"]
    # The connection is usable afterwards (no transaction left open).
    conn.execute("INSERT INTO _havn.run_log (run_type, target, status) VALUES ('t', 'x', 'ok')")

    # ensure_migrated raises the same way rather than swallowing it.
    forget_database(path)
    with pytest.raises(MigrationError):
        ensure_migrated(conn)
    conn.close()


def test_failure_on_writable_backend_connect_closes_the_connection(tmp_path, monkeypatch):
    from havn.config import DatabaseConfig
    from havn.engine.backends import create_backend

    broken = Migration(CURRENT_VERSION + 1, "broken", _broken)
    monkeypatch.setattr(migrations, "MIGRATIONS", MIGRATIONS + (broken,))
    monkeypatch.setattr(migrations, "CURRENT_VERSION", broken.version)
    backend = create_backend(DatabaseConfig(path="w.duckdb"), project_dir=tmp_path)
    with pytest.raises(MigrationError):
        backend.connect()
    # Earlier migrations were committed; the broken one was not.
    ro = duckdb.connect(str(tmp_path / "w.duckdb"), read_only=True)
    try:
        assert migration_status(ro).version == MIGRATIONS[-1].version
    finally:
        ro.close()


def test_warehouse_from_newer_havn_warns_and_is_left_alone(tmp_path, caplog):
    path = tmp_path / "w.duckdb"
    conn = duckdb.connect(str(path))
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO _havn.schema_migrations (version, name, havn_version) "
        "VALUES (?, 'from_the_future', '9.9.9')",
        [CURRENT_VERSION + 5],
    )
    forget_database(path)
    with caplog.at_level(logging.WARNING, logger="havn.migrations"):
        assert apply_migrations(conn) == []
        ensure_meta_table(conn)
    warnings = [r for r in caplog.records if "newer than this havn" in r.getMessage()]
    assert len(warnings) == 1
    st = migration_status(conn)
    assert st.newer_than_code and not st.pending and not st.up_to_date
    # Existing tables keep working.
    conn.execute("INSERT INTO _havn.run_log (run_type, target, status) VALUES ('t', 'x', 'ok')")
    conn.close()


def test_two_threads_opening_at_once_apply_each_migration_once(tmp_path):
    path = tmp_path / "w.duckdb"
    duckdb.connect(str(path)).close()
    # Two connections to one file in one process share the database, as two
    # server threads or a CLI command and a worker would.
    # Cursors of one connection are what the server's threads use.
    conns = [duckdb.connect(str(path)) for _ in range(2)]
    errors: list[BaseException] = []
    targets = [conns[0], conns[1], conns[0].cursor(), conns[0].cursor()]
    barrier = threading.Barrier(len(targets))

    def worker(c):
        try:
            barrier.wait()
            if c is conns[1]:
                migrations.on_writable_connect(c)
            else:
                ensure_migrated(c)
        except BaseException as e:  # noqa: BLE001 - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(c,)) for c in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    counts = conns[0].execute(
        "SELECT version, count(*) FROM _havn.schema_migrations GROUP BY 1 ORDER BY 1"
    ).fetchall()
    assert counts == [(m.version, 1) for m in MIGRATIONS]
    for c in targets[2:]:
        c.close()
    for c in conns:
        c.close()


def test_lazy_ensure_inside_a_caller_transaction_joins_it(tmp_path):
    """A first ensure_* call in the middle of a write cannot open its own
    transaction; it runs inside the caller's and commits with it."""
    path = tmp_path / "w.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("BEGIN TRANSACTION")
    ensure_meta_table(conn)
    conn.execute("INSERT INTO _havn.run_log (run_type, target, status) VALUES ('t', 'x', 'ok')")
    conn.execute("COMMIT")
    assert migration_status(conn).up_to_date
    assert conn.execute("SELECT count(*) FROM _havn.run_log").fetchone()[0] == 1
    conn.close()


def test_restored_file_is_checked_again_on_reconnect(tmp_path):
    """The per-process cache must not hide an older file put in place later."""
    path = tmp_path / "w.duckdb"
    conn = duckdb.connect(str(path))
    ensure_meta_table(conn)
    conn.close()
    path.unlink()
    _old_warehouse(path)

    from havn.engine.database import connect

    conn = connect(path)
    ensure_meta_table(conn)
    assert migration_status(conn).up_to_date
    _assert_full_schema(conn)
    conn.close()


# ---------------------------------------------------------------------------
# DuckLake
# ---------------------------------------------------------------------------


def _ducklake_available() -> bool:
    try:
        c = duckdb.connect()
        try:
            c.execute("LOAD ducklake")
        except duckdb.Error:
            c.execute("INSTALL ducklake")
            c.execute("LOAD ducklake")
        c.close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _ducklake_available(), reason="ducklake extension not available")
def test_ducklake_warehouse_migrates_on_connect(tmp_path):
    from havn.config import DatabaseConfig
    from havn.engine.backends import create_backend

    cfg = DatabaseConfig(
        backend="ducklake", catalog=str(tmp_path / "c.ducklake"), data_path=str(tmp_path / "data")
    )
    backend = create_backend(cfg, project_dir=tmp_path)
    conn = backend.connect()
    try:
        assert migration_status(conn).up_to_date
        _assert_full_schema(conn)
        # DuckLake has no sequences: audit ids are uuids there.
        from havn.engine.audit import log_audit

        log_audit(conn, "alice", "query", "SELECT 1")
        assert conn.execute("SELECT count(*) FROM _havn.audit_log").fetchone()[0] == 1
    finally:
        conn.close()
    conn = backend.connect()
    try:
        assert apply_migrations(conn) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_status_shows_metadata_schema_version(tmp_path):
    from havn.cli import app

    (tmp_path / "project.yml").write_text(
        "name: demo\ndatabase:\n  path: warehouse.duckdb\n", encoding="utf-8"
    )
    _old_warehouse(tmp_path / "warehouse.duckdb")
    runner = CliRunner()

    result = runner.invoke(app, ["status", "-p", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "metadata schema" in result.output
    assert f"version 0 of {CURRENT_VERSION}" in result.output
    assert "pending" in result.output

    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    apply_migrations(conn)
    conn.close()
    result = runner.invoke(app, ["status", "-p", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert f"version {CURRENT_VERSION} (up to date)" in result.output


# ---------------------------------------------------------------------------
# Keep the DDL in one place
# ---------------------------------------------------------------------------

# DDL on _havn that is allowed outside engine/migrations, with the reason.
# Keep this empty: add a migration instead. (Per-model tables built from
# f-strings, such as cdc_apply's _havn.cdc_tombstones__<schema>__<model>,
# are model data rather than metadata schema and are not matched here.)
_DDL_ALLOWED_OUTSIDE_MIGRATIONS: dict[str, str] = {}

_DDL_RE = re.compile(
    r"(CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|SEQUENCE|VIEW|INDEX)(?:\s+IF\s+NOT\s+EXISTS)?"
    r"|ALTER\s+TABLE|DROP\s+TABLE(?:\s+IF\s+EXISTS)?)\s+_havn\.(\w+)",
    re.IGNORECASE,
)


def test_havn_metadata_ddl_lives_only_in_migrations():
    offenders = []
    for py in SRC.rglob("*.py"):
        rel = py.relative_to(SRC).as_posix()
        if rel.startswith("engine/migrations/") or rel in _DDL_ALLOWED_OUTSIDE_MIGRATIONS:
            continue
        text = py.read_text(encoding="utf-8")
        for m in _DDL_RE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{rel}:{line}: {m.group(0)}")
    assert not offenders, (
        "DDL on _havn outside src/havn/engine/migrations/. Add a migration "
        "instead (see 'Changing havn's Metadata Tables' in CONTRIBUTING.md):\n"
        + "\n".join(offenders)
    )


def test_every_baseline_table_is_unique_and_column_names_are_unique():
    names = [t.name for t in TABLES]
    assert len(names) == len(set(names))
    for t in TABLES:
        cols = [column_name(c) for c in t.columns]
        assert len(cols) == len(set(cols)), t.name
