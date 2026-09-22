"""Tests for defer: build here, read unbuilt upstreams from another environment.

Every test works with two real warehouse files, because defer is entirely
about what DuckDB does with a second file: the attach, the file lock, and the
catalog the rewriter reads. There is nothing here that a mock would exercise.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from havn.engine.defer import (
    DeferError,
    DeferLockedError,
    DeferSameProcessError,
    DeferSpec,
    attach_defer_target,
    catalog_objects,
    defer_session,
    make_defer_rewriter,
    resolve_defer,
    target_lockable,
)
from havn.engine.transform import discover_models, run_transform

# A process that holds the target open for writing, which is what makes the
# read-only attach fail. Printing before sleeping lets the test wait for the
# lock to exist rather than guess at a delay.
HOLDER = textwrap.dedent(
    """
    import sys, time, duckdb
    conn = duckdb.connect(sys.argv[1])
    conn.execute("CREATE TABLE IF NOT EXISTS _holder (i INTEGER)")
    print("held", flush=True)
    time.sleep(60)
    """
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path):
    """A project with dev and prod environments, dev deferring to prod."""
    (tmp_path / "transform" / "silver").mkdir(parents=True)
    (tmp_path / "project.yml").write_text(
        "name: deferproj\n"
        "database:\n"
        "  path: warehouse.duckdb\n"
        "environments:\n"
        "  dev:\n"
        "    database:\n"
        "      path: dev.duckdb\n"
        "    defer: prod\n"
        "  prod:\n"
        "    database:\n"
        "      path: prod.duckdb\n"
    )
    return tmp_path


@pytest.fixture
def prod_db(project):
    """prod.duckdb with bronze.customers and landing.raw_events built."""
    path = project / "prod.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE SCHEMA bronze")
    conn.execute(
        "CREATE TABLE bronze.customers AS "
        "SELECT 1 AS id, 'a@example.com' AS email "
        "UNION ALL SELECT 2, 'b@example.com'"
    )
    conn.execute("CREATE SCHEMA landing")
    conn.execute("CREATE TABLE landing.raw_events AS SELECT 1 AS id, 'click' AS kind")
    conn.close()
    return path


@pytest.fixture
def dev_conn(project):
    conn = duckdb.connect(str(project / "dev.duckdb"))
    yield conn
    conn.close()


def _model(project: Path, name: str, sql: str, schema: str = "silver") -> Path:
    path = project / "transform" / schema / f"{name}.sql"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sql)
    return path


def _spec(project: Path, prod_db: Path, **kwargs) -> DeferSpec:
    return DeferSpec(target="prod", path=prod_db, project_dir=project, **kwargs)


def _hold_lock(path: Path):
    """Start a process holding ``path`` open for writing; return it."""
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "held"
    # The lock exists as soon as the connection is open, but give the write
    # of the holder table a moment to settle so the failure under test is the
    # lock and not a half-created file.
    time.sleep(0.2)
    return proc


# ---------------------------------------------------------------------------
# The core behaviour: read there, write here
# ---------------------------------------------------------------------------


def test_deferred_run_reads_target_and_writes_locally(project, prod_db, dev_conn):
    _model(
        project,
        "customers",
        "@config materialized=table, schema=silver\n\n"
        "SELECT id, upper(email) AS email FROM bronze.customers\n",
    )

    results = run_transform(
        dev_conn,
        project / "transform",
        project_dir=project,
        defer=_spec(project, prod_db),
    )
    assert results == {"silver.customers": "built"}
    assert dev_conn.execute(
        "SELECT email FROM silver.customers ORDER BY id"
    ).fetchall() == [("A@EXAMPLE.COM",), ("B@EXAMPLE.COM",)]

    # dev has no bronze of its own, and prod gained nothing.
    assert dev_conn.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_catalog = current_database() AND table_schema = 'bronze'"
    ).fetchone()[0] == 0
    prod = duckdb.connect(str(prod_db), read_only=True)
    try:
        assert prod.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'silver'"
        ).fetchone()[0] == 0
    finally:
        prod.close()


def test_landing_table_falls_through_to_the_target(project, prod_db, dev_conn):
    """The predicate is the catalog, so landing needs no declaration."""
    _model(
        project,
        "events",
        "@config materialized=table, schema=silver\n\n"
        "SELECT id, kind FROM landing.raw_events\n",
    )
    results = run_transform(
        dev_conn,
        project / "transform",
        project_dir=project,
        defer=_spec(project, prod_db),
    )
    assert results == {"silver.events": "built"}
    assert dev_conn.execute("SELECT kind FROM silver.events").fetchall() == [("click",)]


def test_local_table_is_not_redirected(project, prod_db, dev_conn):
    """An object this warehouse holds is read from this warehouse."""
    dev_conn.execute("CREATE SCHEMA bronze")
    dev_conn.execute("CREATE TABLE bronze.customers AS SELECT 99 AS id, 'local' AS email")
    _model(
        project,
        "customers",
        "@config materialized=table, schema=silver\n\nSELECT * FROM bronze.customers\n",
    )
    run_transform(
        dev_conn,
        project / "transform",
        project_dir=project,
        defer=_spec(project, prod_db),
    )
    assert dev_conn.execute("SELECT id, email FROM silver.customers").fetchall() == [
        (99, "local")
    ]


def test_model_built_earlier_in_the_run_is_not_redirected(project, prod_db, dev_conn):
    """A model this run builds is local, even though it did not exist at attach."""
    _model(
        project,
        "customers",
        "@config materialized=table, schema=bronze\n\nSELECT 7 AS id, 'fresh' AS email\n",
        schema="bronze",
    )
    _model(
        project,
        "customers",
        "@config materialized=table, schema=silver\n\nSELECT * FROM bronze.customers\n",
    )
    results = run_transform(
        dev_conn,
        project / "transform",
        project_dir=project,
        defer=_spec(project, prod_db),
    )
    assert results["silver.customers"] == "built"
    assert dev_conn.execute("SELECT id, email FROM silver.customers").fetchall() == [
        (7, "fresh")
    ]


def test_missing_in_both_catalogs_keeps_duckdb_s_own_error(project, prod_db, dev_conn):
    _model(
        project,
        "nothing",
        "@config materialized=table, schema=silver\n\nSELECT * FROM bronze.nowhere\n",
    )
    run_transform(
        dev_conn,
        project / "transform",
        project_dir=project,
        defer=_spec(project, prod_db),
    )
    errors = dev_conn.execute(
        "SELECT error FROM _havn.run_log WHERE status = 'error'"
    ).fetchall()
    assert errors and "bronze.nowhere" in errors[0][0]
    assert "havn_defer" not in errors[0][0]


def test_ephemeral_upstream_is_inlined_not_redirected(project, prod_db, dev_conn):
    """An inlined ephemeral stops being a table reference before defer looks."""
    _model(
        project,
        "recent",
        "@config materialized=ephemeral, schema=bronze\n\n"
        "SELECT id, email FROM bronze.customers WHERE id = 1\n",
        schema="bronze",
    )
    # prod also has a bronze.recent table, so a redirect would be visible:
    # it holds a row the ephemeral query could never produce.
    prod = duckdb.connect(str(prod_db))
    prod.execute("CREATE TABLE bronze.recent AS SELECT 42 AS id, 'stale@example.com' AS email")
    prod.close()

    _model(
        project,
        "recent_customers",
        "@config materialized=table, schema=silver\n\nSELECT * FROM bronze.recent\n",
    )
    results = run_transform(
        dev_conn,
        project / "transform",
        project_dir=project,
        defer=_spec(project, prod_db),
    )
    assert results["bronze.recent"] == "inlined"
    assert dev_conn.execute("SELECT id FROM silver.recent_customers").fetchall() == [(1,)]


def test_ephemeral_plus_defer_plus_microbatch(project, prod_db, dev_conn):
    """An inlined ephemeral must not cost a microbatch model its window.

    Inlining round-trips the SQL through sqlglot before the defer rewriter
    sees it. Unmasked, ``{start}`` comes back as ``{'start': start}`` and the
    window substitution finds nothing, so every window fails to bind.
    """
    prod = duckdb.connect(str(prod_db))
    prod.execute(
        "CREATE TABLE landing.events AS "
        "SELECT 1 AS id, TIMESTAMP '2024-01-01 06:00:00' AS event_at "
        "UNION ALL SELECT 2, TIMESTAMP '2024-01-02 06:00:00'"
    )
    prod.close()

    _model(
        project,
        "clean_events",
        "@config materialized=ephemeral, schema=bronze\n\n"
        "SELECT id, event_at FROM landing.events WHERE id > 0\n",
        schema="bronze",
    )
    _model(
        project,
        "events",
        "@config materialized=incremental, incremental_strategy=microbatch, "
        "schema=gold, event_time=event_at, batch_size=day, begin=2024-01-01\n\n"
        "SELECT id, event_at FROM bronze.clean_events\n"
        "WHERE event_at >= {start} AND event_at < {end}\n",
        schema="gold",
    )

    from havn.engine.transform import BatchRange

    results = run_transform(
        dev_conn,
        project / "transform",
        project_dir=project,
        defer=_spec(project, prod_db),
        batch_range=BatchRange(datetime(2024, 1, 1), datetime(2024, 1, 3)),
    )

    assert results["gold.events"] == "built"
    assert dev_conn.execute(
        "SELECT id FROM gold.events ORDER BY id"
    ).fetchall() == [(1,), (2,)]


def test_incremental_model_that_also_exists_in_the_target(project, prod_db, dev_conn):
    """Existence and column probes must describe this warehouse, not the target.

    information_schema spans every attached database. A model that prod has
    built and dev has not used to look "already there" to the incremental
    path, which then tried to insert into a table dev does not have.
    """
    prod = duckdb.connect(str(prod_db))
    prod.execute("CREATE SCHEMA silver")
    prod.execute("CREATE TABLE silver.orders AS SELECT 999 AS id, 'prod-only' AS note")
    prod.close()

    _model(
        project,
        "orders",
        "@config materialized=incremental, schema=silver, unique_key=id\n\n"
        "SELECT id, email AS note FROM bronze.customers\n",
    )
    results = run_transform(
        dev_conn,
        project / "transform",
        project_dir=project,
        defer=_spec(project, prod_db),
    )
    assert results == {"silver.orders": "built"}
    rows = dev_conn.execute("SELECT id FROM silver.orders ORDER BY id").fetchall()
    assert rows == [(1,), (2,)]

    # A second, forced run exercises the incremental branch that probes the
    # target's columns.
    run_transform(
        dev_conn,
        project / "transform",
        force=True,
        project_dir=project,
        defer=_spec(project, prod_db),
    )
    assert dev_conn.execute("SELECT count(*) FROM silver.orders").fetchone()[0] == 2


def test_parallel_workers_share_the_attach(project, prod_db, dev_conn):
    """Workers open their own connections; the attach is on the shared instance."""
    for name in ("one", "two", "three"):
        _model(
            project,
            name,
            "@config materialized=table, schema=silver\n\n"
            f"SELECT id, '{name}' AS tag FROM bronze.customers\n",
        )
    results = run_transform(
        dev_conn,
        project / "transform",
        parallel=True,
        max_workers=3,
        db_path=str(project / "dev.duckdb"),
        project_dir=project,
        defer=_spec(project, prod_db),
    )
    assert set(results.values()) == {"built"}
    for name in ("one", "two", "three"):
        assert dev_conn.execute(f"SELECT count(*) FROM silver.{name}").fetchone()[0] == 2


def test_content_hash_is_unchanged_by_deferring(project, prod_db, dev_conn):
    sql = (
        "@config materialized=table, schema=silver\n\n"
        "SELECT id, email FROM bronze.customers\n"
    )
    path = _model(project, "customers", sql)
    before = discover_models(project / "transform")[0].content_hash

    run_transform(
        dev_conn,
        project / "transform",
        project_dir=project,
        defer=_spec(project, prod_db),
    )
    stored = dev_conn.execute(
        "SELECT content_hash FROM _havn.model_state WHERE model_path = ?",
        ["silver.customers"],
    ).fetchone()[0]

    assert stored == before
    assert discover_models(project / "transform")[0].content_hash == before
    # And the rewrite never touched the file.
    assert path.read_text() == sql
    assert "havn_defer" not in path.read_text()


def test_detached_after_the_run(project, prod_db, dev_conn):
    _model(
        project,
        "customers",
        "@config materialized=table, schema=silver\n\nSELECT * FROM bronze.customers\n",
    )
    run_transform(
        dev_conn,
        project / "transform",
        project_dir=project,
        defer=_spec(project, prod_db),
    )
    attached = dev_conn.execute(
        "SELECT count(*) FROM duckdb_databases() WHERE database_name = 'havn_defer'"
    ).fetchone()[0]
    assert attached == 0


# ---------------------------------------------------------------------------
# The lock, which is the whole caveat
# ---------------------------------------------------------------------------


def test_locked_target_raises_defer_locked_error(project, prod_db, dev_conn):
    holder = _hold_lock(prod_db)
    try:
        with pytest.raises(DeferLockedError) as exc:
            attach_defer_target(dev_conn, prod_db)
        message = str(exc.value)
        assert "PID" in message
        assert "--defer-snapshot" in message
    finally:
        holder.terminate()
        holder.wait()


def test_locked_target_is_reported_as_not_lockable(project, prod_db):
    holder = _hold_lock(prod_db)
    try:
        ok, reason = target_lockable(prod_db)
        assert ok is False
        assert "locked" in reason
    finally:
        holder.terminate()
        holder.wait()
    assert target_lockable(prod_db) == (True, None)


def test_same_process_conflict_raises_its_own_error(project, prod_db, dev_conn):
    own = duckdb.connect(str(prod_db))
    try:
        with pytest.raises(DeferSameProcessError) as exc:
            attach_defer_target(dev_conn, prod_db)
        assert "one handle per file per process" in str(exc.value)
    finally:
        own.close()


def test_defer_snapshot_works_against_a_locked_target(project, prod_db, dev_conn):
    """The case --defer-snapshot exists for: a job is running against prod."""
    from havn.engine.backup import create_backup

    create_backup(project, prod_db)
    _model(
        project,
        "customers",
        "@config materialized=table, schema=silver\n\n"
        "SELECT id, email FROM bronze.customers\n",
    )
    holder = _hold_lock(prod_db)
    try:
        results = run_transform(
            dev_conn,
            project / "transform",
            project_dir=project,
            defer=_spec(project, prod_db, snapshot=True),
        )
    finally:
        holder.terminate()
        holder.wait()
    assert results == {"silver.customers": "built"}
    assert dev_conn.execute("SELECT count(*) FROM silver.customers").fetchone()[0] == 2


def test_defer_snapshot_copies_the_database_when_it_is_free(project, prod_db):
    from havn.engine.defer import snapshot_defer_target

    snapshot = snapshot_defer_target(prod_db, project_dir=project)
    try:
        assert snapshot.source == "copy"
        assert snapshot.path != prod_db
        probe = duckdb.connect(str(snapshot.path), read_only=True)
        try:
            assert probe.execute("SELECT count(*) FROM bronze.customers").fetchone()[0] == 2
        finally:
            probe.close()
    finally:
        if snapshot.cleanup_dir is not None:
            import shutil

            shutil.rmtree(snapshot.cleanup_dir, ignore_errors=True)


def test_failed_attach_does_not_leak_the_snapshot_copy(
    project, prod_db, dev_conn, monkeypatch
):
    """A snapshot is a whole warehouse in /tmp; a failed ATTACH must not keep it.

    The copy is made before the attach, and the attach can fail for reasons
    that have nothing to do with the copy. The cleanup has to cover that gap.
    """
    from havn.engine import defer as defer_mod

    captured: dict[str, Path] = {}
    real_snapshot = defer_mod.snapshot_defer_target

    def spy(path, *, project_dir=None):
        snapshot = real_snapshot(path, project_dir=project_dir)
        captured["dir"] = snapshot.cleanup_dir
        return snapshot

    def refuse(*args, **kwargs):
        raise DeferError("attach refused")

    monkeypatch.setattr(defer_mod, "snapshot_defer_target", spy)
    monkeypatch.setattr(defer_mod, "attach_defer_target", refuse)

    with pytest.raises(DeferError):
        with defer_session(dev_conn, _spec(project, prod_db, snapshot=True)):
            pass

    assert captured.get("dir") is not None
    assert not captured["dir"].exists(), "the snapshot copy was left behind"


def test_defer_snapshot_without_a_backup_says_so(project, prod_db):
    from havn.engine.defer import snapshot_defer_target

    holder = _hold_lock(prod_db)
    try:
        with pytest.raises(DeferLockedError) as exc:
            snapshot_defer_target(prod_db, project_dir=project)
        assert "no verified backup" in str(exc.value)
    finally:
        holder.terminate()
        holder.wait()


# ---------------------------------------------------------------------------
# Masking still applies to deferred reads
# ---------------------------------------------------------------------------


def test_masking_policy_fires_on_a_deferred_read(project, prod_db, dev_conn):
    """A policy on bronze.customers covers havn_defer.bronze.customers.

    The rewriter resolves policies on ``schema.table`` and ignores the
    catalog part, so deferring cannot be used to read around masking. This
    goes through the same call the /api/query endpoint makes.
    """
    from havn.engine.masking import create_policy, ensure_masking_table
    from havn.engine.masking_rewriter import rewrite_query_with_masking

    ensure_masking_table(dev_conn)
    create_policy(
        dev_conn,
        schema_name="bronze",
        table_name="customers",
        column_name="email",
        method="redact",
    )
    attach_defer_target(dev_conn, prod_db)
    try:
        sql = "SELECT c.email AS addr FROM havn_defer.bronze.customers c"
        rewritten, ok, handled = rewrite_query_with_masking(sql, "viewer", dev_conn)
        assert ok, "a deferred read slipped past the masking rewriter"
        assert handled
        rows = dev_conn.execute(rewritten).fetchall()
        assert [r[0] for r in rows] == ["***", "***"]
    finally:
        dev_conn.execute("DETACH havn_defer")


# ---------------------------------------------------------------------------
# Rewriter unit behaviour
# ---------------------------------------------------------------------------


def test_rewriter_leaves_ctes_and_table_functions_alone(project, prod_db, dev_conn):
    attach_defer_target(dev_conn, prod_db)
    try:
        rewrite = make_defer_rewriter(
            dev_conn, "havn_defer", catalog_objects(dev_conn)
        )
        out = rewrite(
            "WITH customers AS (SELECT 1 AS id) "
            "SELECT * FROM customers UNION ALL SELECT id FROM bronze.customers"
        )
        assert "FROM customers" in out
        assert "havn_defer.bronze.customers" in out
        assert rewrite("SELECT * FROM range(3)") == "SELECT * FROM range(3)"
    finally:
        dev_conn.execute("DETACH havn_defer")


def test_rewriter_keeps_placeholders_intact(project, prod_db, dev_conn):
    attach_defer_target(dev_conn, prod_db)
    try:
        rewrite = make_defer_rewriter(
            dev_conn, "havn_defer", catalog_objects(dev_conn)
        )
        out = rewrite(
            "SELECT * FROM bronze.customers WHERE id > (SELECT max(id) FROM {this}) "
            "AND id >= {start} AND id < {end}"
        )
        assert "havn_defer.bronze.customers" in out
        for placeholder in ("{this}", "{start}", "{end}"):
            assert placeholder in out
    finally:
        dev_conn.execute("DETACH havn_defer")


def test_session_installs_and_clears_the_process_rewriter(project, prod_db, dev_conn):
    from havn.engine.defer import active_query_rewriter

    assert active_query_rewriter() is None
    with defer_session(dev_conn, _spec(project, prod_db)):
        assert active_query_rewriter() is not None
    assert active_query_rewriter() is None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_defer_target_must_exist(tmp_path):
    from havn.config import load_project

    (tmp_path / "project.yml").write_text(
        "name: p\nenvironments:\n  dev:\n    defer: staging\n  prod: {}\n"
    )
    with pytest.raises(ValueError) as exc:
        load_project(tmp_path, env="dev")
    assert "unknown environment 'staging'" in str(exc.value)


def test_defer_target_must_differ_from_the_environment(tmp_path):
    from havn.config import load_project

    (tmp_path / "project.yml").write_text(
        "name: p\nenvironments:\n  dev:\n    defer: dev\n"
    )
    with pytest.raises(ValueError) as exc:
        load_project(tmp_path, env="dev")
    assert "itself" in str(exc.value)


def test_resolve_defer_reads_the_active_environment(project, prod_db):
    from havn.config import load_project

    config = load_project(project, env="dev")
    spec = resolve_defer(config, project)
    assert spec is not None
    assert spec.target == "prod"
    assert spec.path == prod_db


def test_resolve_defer_honours_no_defer(project, prod_db):
    from havn.config import load_project

    config = load_project(project, env="dev")
    assert resolve_defer(config, project, enabled=False) is None


def test_resolve_defer_is_off_without_a_configured_target(project, prod_db):
    from havn.config import load_project

    config = load_project(project, env="prod")
    assert resolve_defer(config, project) is None
    with pytest.raises(DeferError) as exc:
        resolve_defer(config, project, enabled=True)
    assert "no defer target is configured" in str(exc.value)


def test_resolve_defer_needs_the_target_to_exist_on_disk(project):
    from havn.config import load_project

    config = load_project(project, env="dev")
    with pytest.raises(DeferError) as exc:
        resolve_defer(config, project)
    assert "no warehouse at" in str(exc.value)


def test_defer_is_refused_on_the_ducklake_backend(project, prod_db):
    from havn.config import load_project
    from havn.engine.defer import DeferUnsupportedError

    config = load_project(project, env="dev")
    config.database.backend = "ducklake"
    with pytest.raises(DeferUnsupportedError) as exc:
        resolve_defer(config, project)
    assert "DuckLake" in str(exc.value)
