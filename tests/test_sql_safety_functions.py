"""Functions a read-only query may call (havn.engine.sql_functions).

A red-team pass found table functions that read any file the server can read
(``read_single_csv_file``), read Parquet metadata (``parquet_full_metadata``),
write log or profile files to a chosen path (``enable_logging``,
``enable_profiling``) and change instance-wide state (``checkpoint()``,
``truncate_duckdb_logs()``), all through ``/api/query`` as a viewer and
untouched by the READ ONLY transaction. Table functions are allowlisted now,
and every built-in one is classified; these tests fail when a DuckDB release
adds one that is not.
"""

from __future__ import annotations

import os

import duckdb
import pytest

from havn.engine import sql_functions as sf
from havn.engine.sql_safety import ReadOnlyQueryError, validate_read_only_query

DUCK2 = int(duckdb.__version__.split(".")[0]) >= 2


def _functions(types: tuple[str, ...]) -> set[str]:
    rows = duckdb.connect().execute(
        "SELECT DISTINCT function_name FROM duckdb_functions() "
        f"WHERE internal AND function_type IN ({', '.join('?' for _ in types)})",
        list(types),
    ).fetchall()
    return {r[0] for r in rows}


def _exists(name: str) -> bool:
    return bool(duckdb.connect().execute(
        "SELECT count(*) FROM duckdb_functions() WHERE function_name = ?", [name]
    ).fetchall()[0][0])


# ---------------------------------------------------------------------------
# Every built-in function is classified
# ---------------------------------------------------------------------------


def test_every_builtin_table_function_is_classified():
    classified = sf.ALLOWED_TABLE_FUNCTIONS | sf.ADMIN_TABLE_FUNCTIONS | sf.REFUSED_TABLE_FUNCTIONS
    missing = sorted(_functions(("table", "table_macro")) - classified)
    assert not missing, (
        f"DuckDB {duckdb.__version__} has table functions havn has not classified: {missing}. "
        "Add each to ALLOWED_, ADMIN_ or REFUSED_TABLE_FUNCTIONS in havn/engine/sql_functions.py."
    )


def test_classified_lists_do_not_overlap():
    assert not (sf.ALLOWED_TABLE_FUNCTIONS & sf.ADMIN_TABLE_FUNCTIONS)
    assert not (sf.ALLOWED_TABLE_FUNCTIONS & sf.REFUSED_TABLE_FUNCTIONS)
    assert not (sf.ADMIN_TABLE_FUNCTIONS & sf.REFUSED_TABLE_FUNCTIONS)
    reviewed = sf.REVIEWED_SAFE_SCALAR_FUNCTIONS
    assert not (reviewed & (sf.REFUSED_SCALAR_FUNCTIONS | sf.ADMIN_SCALAR_FUNCTIONS))


def test_every_scalar_that_looks_like_io_or_config_is_classified():
    con = duckdb.connect()
    rows = con.execute(
        "SELECT DISTINCT function_name, coalesce(description, '') FROM duckdb_functions() "
        "WHERE internal AND function_type IN ('scalar', 'macro', 'aggregate')"
    ).fetchall()
    known = sf.REVIEWED_SAFE_SCALAR_FUNCTIONS | sf.REFUSED_SCALAR_FUNCTIONS | sf.ADMIN_SCALAR_FUNCTIONS
    missing = sorted(
        name for name, desc in rows
        if (sf.SCALAR_REVIEW_HEURISTIC.search(name) or sf.SCALAR_REVIEW_HEURISTIC.search(desc))
        and name not in known
    )
    assert not missing, (
        f"DuckDB {duckdb.__version__} has scalar functions that look like file, config or "
        f"state access and are not classified: {missing}. Review each and add it to "
        "REVIEWED_SAFE_ or REFUSED_SCALAR_FUNCTIONS in havn/engine/sql_functions.py."
    )


@pytest.mark.parametrize("name", sorted(_functions(("pragma",))))
def test_every_pragma_is_refused(name):
    with pytest.raises(ReadOnlyQueryError):
        validate_read_only_query(f"PRAGMA {name}")
    with pytest.raises(ReadOnlyQueryError):
        validate_read_only_query(f"CALL {name}()")


@pytest.mark.parametrize("name", sorted(sf.REFUSED_TABLE_FUNCTIONS | sf.ADMIN_TABLE_FUNCTIONS))
def test_refused_and_admin_table_functions_are_refused_for_viewers(name):
    with pytest.raises(ReadOnlyQueryError):
        validate_read_only_query(f"SELECT * FROM {name}()")


@pytest.mark.parametrize("name", sorted(sf.REFUSED_TABLE_FUNCTIONS))
def test_refused_table_functions_are_refused_for_admins_too(name):
    with pytest.raises(ReadOnlyQueryError):
        validate_read_only_query(f"SELECT * FROM {name}()", admin=True)


def test_an_unknown_table_function_is_refused():
    # A new DuckDB release or an extension (st_read, sqlite_scan, ...).
    for sql in ("SELECT * FROM brand_new_reader('x')", "SELECT * FROM range(1), brand_new_reader('x')",
                "SELECT * FROM t WHERE i IN (SELECT * FROM brand_new_reader('x'))"):
        with pytest.raises(ReadOnlyQueryError):
            validate_read_only_query(sql)


# ---------------------------------------------------------------------------
# The red-team statements
# ---------------------------------------------------------------------------

REDTEAM = [
    # A. read any file (2.0)
    ("read_single_csv_file", "SELECT * FROM read_single_csv_file('{d}/secret.csv')"),
    ("read_single_json_file", "SELECT * FROM read_single_json_file('{d}/secret.json')"),
    ("read_single_parquet_file", "SELECT * FROM read_single_parquet_file('{d}/secret.parquet')"),
    # B. Parquet metadata (1.5 and 2.0; page index 2.0)
    ("parquet_full_metadata", "SELECT * FROM parquet_full_metadata('{d}/secret.parquet')"),
    ("parquet_page_index", "SELECT * FROM parquet_page_index('{d}/secret.parquet')"),
    # C. write log files to a chosen directory (both)
    ("enable_logging", "SELECT * FROM enable_logging(storage='file', storage_path='{d}/logs')"),
    # D. instance-wide logging settings (both)
    ("enable_logging", "SELECT * FROM enable_logging(level='TRACE')"),
    ("disable_logging", "SELECT * FROM disable_logging()"),
    ("truncate_duckdb_logs", "SELECT * FROM truncate_duckdb_logs()"),
    # E. write a profile at a chosen path (2.0)
    ("enable_profiling", "SELECT * FROM enable_profiling(format='json', save_location='{d}/p.json')"),
    # F. checkpoint (both)
    ("checkpoint", "SELECT * FROM checkpoint()"),
    ("force_checkpoint", "SELECT * FROM force_checkpoint()"),
    # Scalars with side effects or I/O
    ("nextval", "SELECT nextval('landing.seq')"),
    ("setval", "SELECT setval('landing.seq', 10)"),
    ("currval", "SELECT currval('landing.seq')"),
    ("setseed", "SELECT setseed(0.5)"),
    ("write_log", "SELECT write_log('x')"),
    ("getenv", "SELECT getenv('HOME')"),
    ("json_serialize_plan", "SELECT json_serialize_plan('SELECT 1')"),
    # Hidden in other positions
    ("checkpoint", "SELECT * FROM range(1), checkpoint()"),
    ("checkpoint", "SELECT * FROM range(1) r, LATERAL (SELECT * FROM checkpoint()) c"),
    ("read_single_csv_file", "SELECT (SELECT count(*) FROM read_single_csv_file('{d}/secret.csv'))"),
    ("read_single_csv_file", "WITH x AS (SELECT * FROM read_single_csv_file('{d}/secret.csv')) SELECT * FROM x"),
    ("read_single_csv_file", "SELECT * FROM \"read_single_csv_file\"('{d}/secret.csv')"),
    ("read_single_csv_file", "SELECT * FROM main.read_single_csv_file('{d}/secret.csv')"),
    ("enable_logging", "SUMMARIZE SELECT * FROM enable_logging(level='TRACE')"),
    ("enable_logging", "EXPLAIN ANALYZE SELECT * FROM enable_logging(level='TRACE')"),
    ("histogram_values", "SELECT * FROM histogram_values('landing.t', i)"),
    ("duckdb_table_sample", "SELECT * FROM duckdb_table_sample('landing.t')"),
]


@pytest.fixture
def warehouse(tmp_path):
    from havn.engine.auth import create_user
    from havn.engine.database import ensure_meta_table

    d = tmp_path.as_posix()
    (tmp_path / "secret.csv").write_text("k,v\nAPI_KEY,hunter2\n")
    (tmp_path / "secret.json").write_text('{"k":"API_KEY","v":"hunter2"}\n')
    duckdb.connect().execute(
        f"COPY (SELECT 'API_KEY' AS k, 'hunter2' AS v) TO '{d}/secret.parquet' (FORMAT parquet)"
    )
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    ensure_meta_table(conn)
    conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
    conn.execute("CREATE TABLE landing.t AS SELECT 1 AS i, 'x' AS s")
    conn.execute("CREATE SEQUENCE landing.seq START 1")
    create_user(conn, "v", "viewerpass1", "viewer")
    yield conn, tmp_path
    conn.close()


def _settings(conn):
    return dict(conn.execute("SELECT name, value FROM duckdb_settings()").fetchall())


@pytest.mark.parametrize("name, sql", REDTEAM, ids=[f"{i}-{n}" for i, (n, _s) in enumerate(REDTEAM)])
def test_redteam_statement_is_refused(name, sql, tmp_path):
    for admin in (False, True):
        with pytest.raises(ReadOnlyQueryError):
            validate_read_only_query(sql.format(d=tmp_path.as_posix()), admin=admin)


@pytest.mark.parametrize("name, sql", REDTEAM, ids=[f"{i}-{n}" for i, (n, _s) in enumerate(REDTEAM)])
def test_redteam_statement_changes_nothing_as_a_governed_viewer(name, sql, warehouse):
    from havn.engine.governed_query import GovernedQueryError, QueryIdentity, run_governed_query

    conn, root = warehouse
    before_files = set(os.listdir(root))
    before_settings = _settings(conn)
    with pytest.raises(GovernedQueryError) as exc:
        run_governed_query(conn, sql.format(d=root.as_posix()), QueryIdentity("v", "viewer"))
    assert exc.value.status_code in (400, 403)
    assert set(os.listdir(root)) == before_files
    after = _settings(conn)
    changed = {k for k in before_settings if before_settings[k] != after.get(k)}
    # The query governor sets threads / memory_limit for its slot.
    assert changed <= {"threads", "memory_limit", "max_memory"}, changed
    assert conn.execute("SELECT last_value FROM duckdb_sequences() WHERE sequence_name = 'seq'").fetchall() in (
        [(None,)], [(1,)], [(0,)],
    )


def test_what_the_check_stands_between(warehouse):
    """Without the check these really do what the review said (proof the list is live)."""
    conn, root = warehouse
    d = root.as_posix()
    assert conn.execute(f"SELECT count(*) FROM parquet_full_metadata('{d}/secret.parquet')").fetchall()[0][0] > 0
    conn.execute(f"SELECT * FROM enable_logging(storage='file', storage_path='{d}/logs')").fetchall()
    conn.execute("SELECT * FROM disable_logging()").fetchall()
    assert (root / "logs").exists()
    if DUCK2:
        rows = conn.execute(f"SELECT * FROM read_single_csv_file('{d}/secret.csv')").fetchall()
        assert "hunter2" in str(rows)


# ---------------------------------------------------------------------------
# Admin-only introspection and the _havn schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sql", [
    "SELECT * FROM duckdb_settings()",
    "SELECT current_setting('threads')",
    "SELECT * FROM duckdb_databases()",
    "SELECT * FROM duckdb_extensions()",
    "SELECT * FROM duckdb_memory()",
    "SELECT * FROM pragma_show('_havn.users')",
    "SELECT * FROM pragma_table_info('_havn.users')",
    "SELECT * FROM pragma_table_info('\"_HAVN\".users')",
])
def test_admin_only_introspection(sql, warehouse):
    from havn.engine.governed_query import GovernedQueryError, QueryIdentity, run_governed_query

    conn, _root = warehouse
    with pytest.raises(ReadOnlyQueryError):
        validate_read_only_query(sql)
    with pytest.raises(GovernedQueryError):
        run_governed_query(conn, sql, QueryIdentity("v", "viewer"))
    validate_read_only_query(sql, admin=True)
    assert run_governed_query(conn, sql, QueryIdentity("a", "admin"))["row_count"] >= 1


@pytest.mark.parametrize("sql", [
    "SELECT * FROM duckdb_secrets()",
    "SELECT * FROM which_secret('s3://bucket/x', 's3')",
    "SELECT * FROM duckdb_logs()",
])
def test_secrets_and_logs_are_refused_for_admins_too(sql):
    with pytest.raises(ReadOnlyQueryError):
        validate_read_only_query(sql, admin=True)


# ---------------------------------------------------------------------------
# What must keep working
# ---------------------------------------------------------------------------

LEGIT = [
    "SELECT * FROM range(3)",
    "SELECT * FROM generate_series(1, 3)",
    "SELECT * FROM unnest([1, 2, 3])",
    "SELECT unnest([1, 2]) AS x",
    "SELECT * FROM json_each('{\"a\": 1}')",
    "SELECT * FROM json_tree('{\"a\": [1, 2]}')",
    "SELECT * FROM repeat(1, 3)",
    "SELECT * FROM range(3) r, LATERAL unnest([r.range]) u",
    "SELECT table_name FROM information_schema.tables",
    "SELECT column_name FROM information_schema.columns WHERE table_schema = 'landing'",
    "SELECT * FROM duckdb_tables()",
    "SELECT * FROM duckdb_columns() WHERE schema_name = 'landing'",
    "SELECT * FROM pragma_table_info('landing.t')",
    "SELECT * FROM pragma_show('landing.t')",
    "SELECT * FROM pragma_version()",
    "SELECT current_database(), current_schema(), version()",
    "SELECT log(10), ln(2), url_encode('a b')",
    "DESCRIBE landing.t",
    "SUMMARIZE landing.t",
    "SHOW TABLES",
    "PIVOT landing.t ON s USING count(*)",
]


@pytest.mark.parametrize("sql", LEGIT)
def test_legit_reads_pass_and_run_as_a_viewer(sql, warehouse):
    from havn.engine.governed_query import QueryIdentity, run_governed_query

    conn, _root = warehouse
    validate_read_only_query(sql)
    run_governed_query(conn, sql, QueryIdentity("v", "viewer"))


def test_read_only_execution_turns_python_replacement_scans_off(warehouse):
    import pandas as pd

    from havn.engine.governed_query import read_only_execution

    conn, _root = warehouse
    secret_df = pd.DataFrame({"k": ["API_KEY"]})  # noqa: F841 - found by name
    assert conn.execute("SELECT count(*) FROM secret_df").fetchall() == [(1,)]
    with read_only_execution(conn):
        with pytest.raises(duckdb.Error):
            conn.execute("SELECT count(*) FROM secret_df").fetchall()
    assert conn.execute("SELECT count(*) FROM secret_df").fetchall() == [(1,)]  # restored


def test_explain_analyze_runs_in_a_read_only_transaction(warehouse):
    """/api/query/explain-analyze and /api/query/profile execute the query."""
    from havn.engine.explain import explain_analyze_query
    from havn.engine.governed_query import read_only_execution

    conn, _root = warehouse
    with pytest.raises(duckdb.Error):
        explain_analyze_query(conn, "SELECT nextval('landing.seq')", guard=read_only_execution)
    assert conn.execute("SELECT nextval('landing.seq')").fetchall() == [(1,)]  # never advanced
    plan, _raw = explain_analyze_query(conn, "SELECT * FROM landing.t", guard=read_only_execution)
    assert plan is not None


def test_explain_analyze_endpoint_as_viewer(tmp_path):
    from fastapi.testclient import TestClient

    import havn.server.app as server_app
    from havn.server import deps

    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: warehouse.duckdb\n")
    (tmp_path / "transform").mkdir()
    c = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    c.execute("CREATE SCHEMA landing")
    c.execute("CREATE TABLE landing.t AS SELECT 1 AS i")
    c.close()
    deps.reset_shared_conn()
    deps._clear_config_cache()
    old_dir, old_auth = server_app.PROJECT_DIR, server_app.AUTH_ENABLED
    server_app.PROJECT_DIR = tmp_path
    server_app.AUTH_ENABLED = False
    try:
        client = TestClient(server_app.app)
        for path in ("/api/query/explain-analyze", "/api/query/profile"):
            r = client.post(path, json={"sql": "SELECT * FROM checkpoint()"})
            assert r.status_code == 403, r.text
            r = client.post(path, json={"sql": "SELECT * FROM landing.t"})
            assert r.status_code == 200, r.text
    finally:
        deps.reset_shared_conn()
        server_app.PROJECT_DIR, server_app.AUTH_ENABLED = old_dir, old_auth


# ---------------------------------------------------------------------------
# Project macros
# ---------------------------------------------------------------------------


@pytest.fixture
def macro_project(tmp_path):
    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: warehouse.duckdb\n")
    (tmp_path / "macros").mkdir()
    (tmp_path / "macros" / "people.py").write_text(
        "from havn import table_macro\n\n"
        "@table_macro(schema={'id': 'INTEGER', 'name': 'VARCHAR'})\n"
        "def people(prefix: str) -> list:\n"
        "    return [{'id': 1, 'name': prefix + 'Ada'}]\n",
        encoding="utf-8",
    )
    (tmp_path / "macros" / "sql_macros.sql").write_text(
        "CREATE MACRO evens(n) AS TABLE SELECT * FROM range(n) WHERE range % 2 = 0;\n"
        "CREATE MACRO leak(p) AS TABLE SELECT * FROM read_csv(p);\n"
        "CREATE MACRO leak_twice(p) AS TABLE SELECT * FROM leak(p);\n"
        "CREATE MACRO env(k) AS getenv(k);\n",
        encoding="utf-8",
    )
    return tmp_path


def test_project_table_macros_are_callable_unless_their_body_is_refused(macro_project):
    from havn.engine.governed_query import GovernedQueryError, QueryIdentity, run_governed_query
    from havn.engine.macros import register_macros

    sf.reset_user_macros()
    conn = duckdb.connect(str(macro_project / "warehouse.duckdb"))
    try:
        register_macros(conn, macro_project)
        viewer = QueryIdentity("v", "viewer")
        assert run_governed_query(conn, "SELECT * FROM people('Dr ')", viewer)["rows"] == [[1, "Dr Ada"]]
        assert run_governed_query(conn, "SELECT count(*) FROM evens(6)", viewer)["rows"] == [[3]]
        for sql in (
            "SELECT * FROM leak('secret.csv')",
            "SELECT * FROM leak_twice('secret.csv')",
            "SELECT env('HOME')",
        ):
            with pytest.raises(GovernedQueryError):
                run_governed_query(conn, sql, viewer)
    finally:
        conn.close()
        sf.reset_user_macros()


def test_project_table_macros_are_refused_before_classification():
    sf.reset_user_macros()
    with pytest.raises(ReadOnlyQueryError):
        validate_read_only_query("SELECT * FROM people('x')")
