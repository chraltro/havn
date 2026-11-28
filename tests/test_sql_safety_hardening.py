"""Read-only gate: the third red-team pass.

* B1: naming an extension's function or type (``'1.2.3.4'::INET``, ``stem()``,
  ``excel_text()``) made DuckDB download, write and load that extension. The
  gate refuses names the connection's catalog does not already have.
* B2: on DuckDB 2.0 ``SHOW <setting>`` returned instance settings, secrets
  included, to a viewer.
* H1-H4: macros keyed by schema and name; every governed read route runs in
  the read-only guard; over-deep queries get a 400; ``_havn`` layout through
  folded arguments, DESCRIBE and SHOW.
* False positives: FROM inside expressions, ``histogram()`` the aggregate,
  CTEs named like functions, and two acceptance corpora (analyst queries and
  the web UI's own query shapes).

No test downloads an extension: every warehouse here turns
``autoinstall_known_extensions`` off, so a regression fails instead of
fetching from the network.
"""

from __future__ import annotations

import duckdb
import pytest

from havn.engine import sql_functions as sf
from havn.engine.governed_query import GovernedQueryError, QueryIdentity, run_governed_query
from havn.engine.sql_safety import ReadOnlyQueryError, validate_read_only_query

from tests.sql_acceptance_corpus import ANALYST_QUERIES, UI_QUERIES

DUCK2 = int(duckdb.__version__.split(".")[0]) >= 2
VIEWER = QueryIdentity("v", "viewer")
ADMIN = QueryIdentity("a", "admin")


def _offline(conn) -> None:
    conn.execute("SET autoinstall_known_extensions = false")


@pytest.fixture
def warehouse(tmp_path):
    from havn.engine.auth import create_user
    from havn.engine.database import ensure_meta_table

    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    _offline(conn)
    ensure_meta_table(conn)
    conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
    conn.execute("CREATE TABLE landing.t AS SELECT 1 AS i, 'x' AS s")
    conn.execute('CREATE TABLE landing.w ("delete" INT, "update" INT, copy INT, "insert" INT, merge INT, i INT)')
    conn.execute("INSERT INTO landing.w VALUES (1, 2, 3, 4, 5, 6)")
    conn.execute("CREATE SEQUENCE landing.seq START 1")
    for name in ["copy_t", "delete_log", "copy", "merge", "range", "summary", '"orders.csv"']:
        conn.execute(f"CREATE TABLE landing.{name} AS SELECT 1 AS x")
    conn.execute("CREATE TABLE landing.big AS SELECT range AS i FROM range(1000)")
    conn.execute("CREATE TABLE people AS SELECT 1 AS id")
    create_user(conn, "v", "viewerpass1", "viewer")
    yield conn, tmp_path
    conn.close()


def _refused(conn, sql, identity=VIEWER):
    with pytest.raises(GovernedQueryError) as exc:
        run_governed_query(conn, sql, identity)
    assert exc.value.status_code in (400, 403), exc.value
    return exc.value


# ---------------------------------------------------------------------------
# B1: no extension autoload from a query
# ---------------------------------------------------------------------------

AUTOLOAD = [
    "SELECT '127.0.0.1'::INET AS ip",
    "SELECT CAST('127.0.0.1' AS INET) AS ip",
    "SELECT TRY_CAST('127.0.0.1' AS INET) AS ip",
    "SELECT ['127.0.0.1'::INET] AS ips",
    "SELECT stem('running', 'english') AS w",
    "SELECT excel_text(1.5, '0.00') AS x",
    "SELECT html_escape('<a>') AS h",
    "SELECT * FROM landing.t WHERE stem(s, 'english') = 'x'",
    "SELECT (SELECT excel_text(1.5, '0.00')) AS x",
]


@pytest.mark.parametrize("sql", AUTOLOAD)
def test_extension_functions_and_types_are_not_autoloaded(sql, warehouse):
    conn, _root = warehouse
    before = conn.execute("SELECT extension_name FROM duckdb_extensions() WHERE loaded ORDER BY 1").fetchall()
    for identity in (VIEWER, ADMIN):
        err = _refused(conn, sql, identity)
        assert "Unknown function or type" in str(err)
    after = conn.execute("SELECT extension_name FROM duckdb_extensions() WHERE loaded ORDER BY 1").fetchall()
    assert after == before


def test_loaded_functions_and_user_udfs_still_work(warehouse):
    conn, _root = warehouse
    conn.create_function("twice", lambda x: x * 2, [int], int)
    conn.execute("CREATE MACRO plus_one(a) AS a + 1")
    out = run_governed_query(
        conn, "SELECT twice(21) AS a, plus_one(1) AS b, '{\"a\":1}'::JSON AS j, list_sum([1, 2]) AS l", VIEWER,
    )
    assert out["rows"][0][:2] == [42, 2]


# ---------------------------------------------------------------------------
# B2: SHOW <setting>
# ---------------------------------------------------------------------------

SHOW_SETTINGS = [
    "SHOW s3_secret_access_key",
    "SHOW s3_access_key_id",
    "SHOW temp_directory",
    "SHOW extension_directory",
    "SHOW home_directory",
    'SHOW "s3_secret_access_key"',
    "SELECT * FROM (SHOW temp_directory)",
]


@pytest.mark.parametrize("sql", SHOW_SETTINGS)
def test_show_of_a_setting_is_refused_for_viewers(sql, warehouse):
    # DuckDB 2.0 resolves SHOW <name> as a setting when no table has the name;
    # 1.x only ever looks for a table, so there the query just fails.
    conn, _root = warehouse
    if DUCK2:
        with pytest.raises(ReadOnlyQueryError):
            validate_read_only_query(sql)
    _refused(conn, sql)


@pytest.mark.parametrize("sql", ["SHOW TABLES", "SHOW ALL TABLES", "SHOW DATABASES", "DESCRIBE landing.t",
                                 "SHOW landing.t", "SHOW people", "DESCRIBE people", "SUMMARIZE landing.t",
                                 "DESCRIBE SELECT 1"])
def test_show_and_describe_of_tables_still_work(sql, warehouse):
    conn, _root = warehouse
    assert run_governed_query(conn, sql, VIEWER)["row_count"] >= 1


@pytest.mark.skipif(not DUCK2, reason="SHOW <setting> only resolves settings on DuckDB 2")
def test_admin_may_show_settings(warehouse):
    conn, _root = warehouse
    assert run_governed_query(conn, "SHOW temp_directory", ADMIN)["row_count"] == 1


# ---------------------------------------------------------------------------
# H4: the _havn layout
# ---------------------------------------------------------------------------

HAVN_LAYOUT = [
    "SELECT * FROM pragma_table_info(chr(95) || 'havn.tokens')",
    "SELECT * FROM pragma_table_info(lower('_HAVN.TOKENS'))",
    "SELECT * FROM pragma_show(concat('_ha', 'vn.users'))",
    "DESCRIBE _havn.tokens",
    "SHOW _havn.users",
    "SUMMARIZE _havn.users",
]


@pytest.mark.parametrize("sql", HAVN_LAYOUT)
def test_havn_layout_is_admin_only(sql, warehouse):
    conn, _root = warehouse
    _refused(conn, sql)


def test_admin_may_describe_havn(warehouse):
    conn, _root = warehouse
    assert run_governed_query(conn, "DESCRIBE _havn.users", ADMIN)["row_count"] > 1
    assert run_governed_query(conn, "SELECT * FROM pragma_table_info('_havn.users')", ADMIN)["row_count"] > 1


# ---------------------------------------------------------------------------
# H3: over-deep input
# ---------------------------------------------------------------------------


def test_deeply_nested_query_is_a_400_not_a_crash(warehouse):
    conn, _root = warehouse
    sql = "SELECT " + "(" * 3000 + "1" + ")" * 3000
    with pytest.raises(GovernedQueryError) as exc:
        run_governed_query(conn, sql, VIEWER)
    assert exc.value.status_code == 400
    with pytest.raises(ReadOnlyQueryError):
        validate_read_only_query("SELECT " + "(" * 201 + "1" + ")" * 201)
    validate_read_only_query("SELECT " + "(" * 50 + "1" + ")" * 50)


# ---------------------------------------------------------------------------
# H1: macros keyed by schema and name
# ---------------------------------------------------------------------------


def test_macro_classification_is_per_schema_and_a_refused_namesake_wins(warehouse):
    conn, root = warehouse
    (root / "secret.csv").write_text("k\nhunter2\n")
    conn.execute("CREATE SCHEMA a")
    conn.execute("CREATE SCHEMA b")
    conn.execute("CREATE MACRO a.m() AS TABLE SELECT 1 AS x")
    conn.execute(f"CREATE MACRO b.m() AS TABLE SELECT * FROM read_csv('{root.as_posix()}/secret.csv')")
    conn.execute("CREATE MACRO landing.only_safe() AS TABLE SELECT * FROM landing.t")
    conn.execute("CREATE MACRO over_users() AS TABLE SELECT username FROM _havn.users")
    conn.execute("CREATE MACRO hash_of(u) AS (SELECT max(password_hash) FROM _havn.users WHERE username = u)")
    sf.reset_user_macros()
    try:
        sf.refresh_user_macros(conn)
        assert run_governed_query(conn, "SELECT * FROM a.m()", VIEWER)["rows"] == [[1]]
        assert run_governed_query(conn, "SELECT * FROM landing.only_safe()", VIEWER)["row_count"] == 1
        for sql in ("SELECT * FROM b.m()", "SELECT * FROM m()", "SELECT * FROM over_users()",
                    "SELECT hash_of('v')"):
            _refused(conn, sql)
    finally:
        sf.reset_user_macros()


# ---------------------------------------------------------------------------
# False positives
# ---------------------------------------------------------------------------

FALSE_POSITIVES = [
    "SELECT * FROM landing.t WHERE s IS DISTINCT FROM 'x'",
    "SELECT * FROM landing.t WHERE s IS NOT DISTINCT FROM 'x'",
    "SELECT extract(year FROM '2024-01-01'::DATE)",
    "SELECT trim(' a ' FROM ' ')",
    "SELECT trim(BOTH 'x' FROM 'xxaxx')",
    "SELECT substring('abc' FROM 2)",
    "SELECT histogram(i) FROM landing.t",
    "WITH checkpoint(a) AS (SELECT 1) SELECT * FROM checkpoint",
    "WITH query(a) AS (SELECT 1) SELECT * FROM query",
    "WITH read_text(a) AS (SELECT 1) SELECT * FROM read_text",
    "SELECT * FROM landing.t AS summary",
    'SELECT * FROM landing."orders.csv"',
    "PIVOT (SELECT extract(year FROM DATE '2024-01-01') AS y, s FROM landing.t) ON s USING count(*)",
    "PIVOT (SELECT * FROM landing.t WHERE s IS DISTINCT FROM 'y') ON s USING count(*)",
    "PIVOT landing.w ON copy USING sum(i)",
    "PIVOT landing.w ON merge USING count(*)",
]


@pytest.mark.parametrize("sql", FALSE_POSITIVES)
def test_false_positives_pass_and_run(sql, warehouse):
    conn, _root = warehouse
    validate_read_only_query(sql, conn=conn)
    run_governed_query(conn, sql, VIEWER)


def test_histogram_table_macro_is_still_refused():
    with pytest.raises(ReadOnlyQueryError):
        validate_read_only_query("SELECT * FROM histogram('landing.t', i)")


# Queries in the corpora that fail at run time for reasons unrelated to the
# gate on one DuckDB version (overlay() missing on 1.5, the arrow lambda
# deprecated on 2.0, an unbound parameter).
_RUNTIME_ONLY = {"overlay_position", "list_lambda", "params_named"}


@pytest.mark.parametrize("label, sql", ANALYST_QUERIES + UI_QUERIES,
                         ids=[c[0] for c in ANALYST_QUERIES + UI_QUERIES])
def test_acceptance_corpus(label, sql, warehouse):
    conn, _root = warehouse
    try:
        validate_read_only_query(sql, conn=conn)
        run_governed_query(conn, sql, VIEWER)
    except (GovernedQueryError, ReadOnlyQueryError) as e:
        if label in _RUNTIME_ONLY or "pg_catalog" in sql:
            assert "not allowed" not in str(e) and "not available" not in str(e), e
        else:
            raise


# ---------------------------------------------------------------------------
# H2 and B1/B2 through every read route
# ---------------------------------------------------------------------------


@pytest.fixture
def server(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import havn.server.app as server_app
    from havn.server import deps

    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: warehouse.duckdb\n")
    (tmp_path / "transform").mkdir()
    c = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    c.execute("CREATE SCHEMA landing")
    c.execute("CREATE TABLE landing.people AS SELECT 1 AS id, 'a' AS s")
    c.execute("CREATE SEQUENCE landing.seq START 1")
    c.close()
    deps.reset_shared_conn()
    deps._clear_config_cache()
    deps.invalidate_token_cache()
    with deps._login_attempts_lock:
        deps._login_attempts.clear()
    old_dir, old_auth = server_app.PROJECT_DIR, server_app.AUTH_ENABLED
    server_app.PROJECT_DIR = tmp_path
    server_app.AUTH_ENABLED = True
    try:
        anon = TestClient(server_app.app)
        tok = anon.post(
            "/api/auth/setup", json={"username": "admin", "password": "adminpass1", "role": "admin"},
        ).json()["token"]
        admin = TestClient(server_app.app)
        admin.headers["Authorization"] = f"Bearer {tok}"
        admin.post("/api/users", json={"username": "viewer", "password": "viewerpass1", "role": "viewer"})
        vtok = anon.post("/api/auth/login", json={"username": "viewer", "password": "viewerpass1"}).json()["token"]
        viewer = TestClient(server_app.app)
        viewer.headers["Authorization"] = f"Bearer {vtok}"
        shared = deps._get_shared_conn()
        _offline(shared)
        yield admin, viewer, shared
    finally:
        deps.reset_shared_conn()
        server_app.PROJECT_DIR, server_app.AUTH_ENABLED = old_dir, old_auth


def _session(client) -> str:
    body = client.post("/api/sessions", json={"name": "s"}).json()
    return body.get("session_id") or body.get("id")


def _read_routes(client, sid):
    return [
        ("/api/query", lambda sql: client.post("/api/query", json={"sql": sql})),
        ("/api/query/export-csv", lambda sql: client.post("/api/query/export-csv", json={"sql": sql})),
        ("/api/query/explain-analyze", lambda sql: client.post("/api/query/explain-analyze", json={"sql": sql})),
        ("/api/query/profile", lambda sql: client.post("/api/query/profile", json={"sql": sql})),
        (f"/api/sessions/{sid}/query", lambda sql: client.post(f"/api/sessions/{sid}/query", json={"sql": sql})),
    ]


@pytest.mark.parametrize("sql", [
    "SELECT excel_text(1.5, '0.00') AS x",
    "SELECT '127.0.0.1'::INET AS ip",
    "SHOW s3_secret_access_key",
    "SHOW temp_directory",
    "SELECT current_setting('s3_secret_access_key')",
    "SELECT * FROM pragma_table_info(chr(95) || 'havn.tokens')",
])
def test_every_read_route_refuses_for_a_viewer(sql, server):
    admin, viewer, shared = server
    before = shared.execute("SELECT extension_name FROM duckdb_extensions() WHERE loaded ORDER BY 1").fetchall()
    for path, call in _read_routes(viewer, _session(admin)):
        r = call(sql)
        # Refused (400/403); the collaboration route reports a failed query as
        # 200 with an "error" field. Either way nothing was answered.
        refused = r.status_code in (400, 403) or (r.status_code == 200 and "\"error\"" in r.text)
        assert refused, (path, sql, r.status_code, r.text[:200])
        assert ".tmp" not in r.text and "TOPSECRET" not in r.text
    after = shared.execute("SELECT extension_name FROM duckdb_extensions() WHERE loaded ORDER BY 1").fetchall()
    assert after == before


def test_every_read_route_runs_in_the_read_only_guard(server, monkeypatch):
    """With the gate switched off, a write is still stopped on every route."""
    import havn.engine.sql_safety as sql_safety
    import havn.server.routes.query as query_routes

    admin, _viewer, shared = server
    monkeypatch.setattr(sql_safety, "validate_read_only_query", lambda sql, **k: None)
    monkeypatch.setattr(query_routes, "validate_read_only_query", lambda sql, **k: None)
    for path, call in _read_routes(admin, _session(admin)):
        r = call("SELECT nextval('landing.seq') AS n")
        assert r.status_code >= 400 or "read-only" in r.text.lower(), (path, r.status_code, r.text[:200])
    assert shared.execute("SELECT nextval('landing.seq')").fetchall() == [(1,)]


def test_read_routes_still_answer_ordinary_queries(server):
    admin, viewer, _shared = server
    for path, call in _read_routes(viewer, _session(admin)):
        r = call("SELECT count(*) AS n FROM landing.people WHERE s IS DISTINCT FROM 'x'")
        assert r.status_code == 200, (path, r.text[:200])
