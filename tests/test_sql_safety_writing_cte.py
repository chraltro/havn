"""Data-modifying statements nested inside a read (DuckDB 2.0 writing CTEs).

DuckDB 2.0 accepts INSERT / UPDATE / DELETE / COPY as a CTE body, and the
statement as a whole still reports as a SELECT:

    WITH d AS (UPDATE _havn.users SET role = 'admin' ... RETURNING *) SELECT * FROM d

Released havn treated WITH as a read, so a viewer could make itself admin
through ``/api/query``. Each statement below is checked through the full
validator, through the hand lexer alone, and (on DuckDB 2.0, where they parse)
through DuckDB's parse tree alone, so no layer silently depends on another.
"""

from __future__ import annotations

import duckdb
import pytest

import havn.engine.sql_safety as sql_safety
from havn.engine.sql_safety import ReadOnlyQueryError, validate_read_only_query

DUCK2 = int(duckdb.__version__.split(".")[0]) >= 2
duck2_only = pytest.mark.skipif(not DUCK2, reason="DuckDB 1.x cannot parse a writing CTE")

# Writing CTEs, in every position and spelling found so far. On DuckDB 2.0 each
# of these parses (test_writing_ctes_really_parse_on_duckdb2 keeps the list honest).
WRITING_CTES = [
    "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d LIMIT 1",
    "WITH d AS (UPDATE _havn.users SET role='admin' WHERE username='v' "
    "RETURNING username, role) SELECT * FROM d LIMIT 1",
    "WITH d AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM d",
    "WITH d AS (INSERT OR REPLACE INTO t VALUES (1) RETURNING *) SELECT * FROM d",
    "WITH d AS (COPY t TO 'leak.csv') SELECT 1 LIMIT 1",
    "WITH d AS (COPY (SELECT * FROM t) TO 'leak.csv') SELECT 1",
    "WITH d AS MATERIALIZED (DELETE FROM t RETURNING *) SELECT * FROM d",
    "WITH d AS NOT MATERIALIZED (DELETE FROM t RETURNING *) SELECT * FROM d",
    "WITH a AS (SELECT 1), d AS (DELETE FROM t RETURNING *) SELECT * FROM a",
    "WITH d AS (UPDATE t SET i = 2 RETURNING *), e AS (SELECT 1) SELECT * FROM e",
    "WITH d AS (DELETE FROM t) SELECT 1",
    "SELECT * FROM (WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d)",
    "SELECT 1 UNION ALL SELECT * FROM (WITH d AS (DELETE FROM t RETURNING i) SELECT * FROM d)",
    "SELECT (SELECT count(*) FROM (WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d))",
    "SELECT * FROM t WHERE i IN (WITH d AS (DELETE FROM t RETURNING i) SELECT i FROM d)",
    "WITH d AS (WITH e AS (DELETE FROM t RETURNING *) SELECT * FROM e) SELECT * FROM d",
    "WITH d AS (WITH x AS (SELECT 1) DELETE FROM t RETURNING *) SELECT * FROM d",
    "with D as (dElEtE from T returning *) select * from D",
    "WITH/*x*/d/*x*/AS/*x*/(/*x*/DELETE/*x*/FROM t RETURNING *) SELECT * FROM d",
    "WITH d AS (-- c\nDELETE\n-- c\nFROM t RETURNING *) SELECT * FROM d",
    'WITH "select" AS (DELETE FROM t RETURNING *) SELECT * FROM "select"',
    'WITH d AS (DELETE FROM "t" RETURNING *) SELECT * FROM d',
    "WITH d AS (UPDATE t AS x SET i = 2 RETURNING *) SELECT * FROM d",
    "WITH d AS (UPDATE main.t SET i = 2 RETURNING *) SELECT * FROM d",
    "WITH d AS (DELETE FROM t RETURNING *) FROM d",
    "WITH d AS (DELETE FROM t RETURNING *) TABLE d",
    "WITH d AS (DELETE FROM t RETURNING *) VALUES (1)",
    "EXPLAIN ANALYZE WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
    "EXPLAIN WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
    "SUMMARIZE WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
    "DESCRIBE WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
    "SELECT * FROM range((SELECT count(*) FROM (WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d)))",
    # MULTI on DuckDB 2.0 (a dynamic PIVOT anywhere): no parse tree to inspect.
    "WITH d AS (DELETE FROM t RETURNING *) PIVOT d ON i USING count(*)",
    "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM (PIVOT t ON i USING count(*))",
    "PIVOT (WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d) ON i USING count(*)",
]

# Other ways to write or reach outside the database from a read surface. Some
# are parse errors on one version or the other; all must be refused on both.
OTHER_REFUSED = [
    # Bodies DuckDB 2.0 does not (yet) accept in a CTE: refused by the lexer
    # rather than trusted to stay a parse error.
    "WITH d AS (MERGE INTO t USING (SELECT 1 AS i) s ON t.i = s.i WHEN MATCHED THEN DELETE) SELECT 1",
    "WITH d AS (COPY FROM DATABASE memory TO memory) SELECT 1",
    "WITH d AS (CREATE TABLE z AS SELECT 1) SELECT 1",
    "WITH d AS (ATTACH 'x.db') SELECT 1",
    "WITH d AS (DROP TABLE t) SELECT 1",
    "WITH RECURSIVE d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
    "WITH d AS ((DELETE FROM t RETURNING *)) SELECT * FROM d",
    "SELECT * FROM (DELETE FROM t RETURNING *)",
    "SELECT * FROM (INSERT INTO t VALUES (2) RETURNING *) x",
    # Writes with a CTE attached, and CTEs before a write.
    "INSERT INTO t WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
    "WITH d AS (SELECT 1) DELETE FROM t",
    "DELETE FROM t WHERE i IN (SELECT 1)",
    # Multi-statement strings.
    "SELECT 1; WITH d AS (DELETE FROM t RETURNING *) SELECT 1",
    "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d; SELECT 1",
    # Statement types that are never reads.
    "PREPARE p AS WITH d AS (DELETE FROM t RETURNING *) SELECT 1",
    "PREPARE p AS SELECT 1",
    "EXECUTE p",
    "CALL pragma_version()",
    "ATTACH 'x.db' AS x",
    "SET threads = 1",
    "PRAGMA enable_profiling",
    "EXPORT DATABASE 'out'",
    "INSTALL httpfs",
    "LOAD httpfs",
    "CREATE TEMP TABLE x AS SELECT 1",
    "COPY t TO 'leak.csv'",
    # File access.
    "SELECT * FROM read_csv('x.csv')",
    "SELECT * FROM read_text('secret.txt')",
    "SELECT * FROM glob('*')",
]

LEGIT = [
    "WITH a AS (SELECT 1 AS x) SELECT * FROM a",
    "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT * FROM r",
    "WITH a AS MATERIALIZED (SELECT 1) SELECT * FROM a",
    "WITH a AS NOT MATERIALIZED (SELECT 1) SELECT * FROM a",
    "WITH d AS (FROM t) SELECT * FROM d",
    "WITH d AS (VALUES (1)) SELECT * FROM d",
    "WITH d AS (TABLE t) SELECT * FROM d",
    "SELECT * FROM t WHERE i IN (WITH d AS (SELECT 1) SELECT * FROM d)",
    "SELECT 1 AS delete, 2 AS update, 3 AS copy, 4 AS insert, 5 AS merge",
    'SELECT "delete", "update", "copy" FROM w',
    "SELECT update, copy, merge, insert, delete FROM w",
    "SELECT w.delete, w.update, w.copy FROM w",
    "SELECT (update) FROM w",
    "SELECT coalesce(update, 0), abs(copy), (delete + 1) FROM w",
    "SELECT 'DELETE FROM t' AS s, 'WITH d AS (DELETE FROM t RETURNING *) SELECT 1' AS s2",
    "SELECT 1 -- delete from t",
    "SELECT * FROM t /* WITH d AS (DELETE FROM t RETURNING *) SELECT 1 */",
    "SELECT sum(i) OVER x FROM t WINDOW x AS (ORDER BY i)",
    "PIVOT t ON i USING count(*)",
    "PIVOT t ON i IN (1, 2) USING count(*)",
    "DESCRIBE t",
    "SUMMARIZE t",
    "SHOW TABLES",
    "DESCRIBE SELECT 1",
    "DESCRIBE PIVOT t ON i USING count(*)",
    "SUMMARIZE PIVOT t ON i USING count(*)",
    "EXPLAIN ANALYZE WITH a AS (SELECT 1) SELECT * FROM a",
]


@pytest.fixture
def hand_lexer_only(monkeypatch):
    monkeypatch.setattr(sql_safety, "_check_with_duckdb_parser", lambda sql, **k: None)


@pytest.fixture
def parser_only(monkeypatch):
    monkeypatch.setattr(sql_safety, "_nested_write_in_cleaned", lambda cleaned: None, raising=False)


def test_adversarial_list_is_large_enough():
    assert len(WRITING_CTES) + len(OTHER_REFUSED) >= 40


@pytest.mark.parametrize("sql", WRITING_CTES + OTHER_REFUSED)
def test_refused(sql):
    with pytest.raises(ReadOnlyQueryError):
        validate_read_only_query(sql)


@pytest.mark.parametrize("sql", WRITING_CTES)
def test_refused_by_hand_lexer_alone(sql, hand_lexer_only):
    with pytest.raises(ReadOnlyQueryError):
        validate_read_only_query(sql)


@duck2_only
@pytest.mark.parametrize("sql", WRITING_CTES)
def test_refused_by_duckdb_parse_tree_alone(sql, parser_only):
    with pytest.raises(ReadOnlyQueryError):
        validate_read_only_query(sql)


@duck2_only
@pytest.mark.parametrize("sql", WRITING_CTES)
def test_writing_ctes_really_parse_on_duckdb2(sql):
    # Guard against the list drifting into parse errors that test nothing.
    stmts = duckdb.connect().extract_statements(sql)
    assert len(stmts) == 1


@pytest.mark.parametrize("sql", LEGIT)
def test_legit_reads_pass(sql):
    validate_read_only_query(sql)


@pytest.mark.parametrize("sql", LEGIT)
def test_legit_reads_pass_hand_lexer(sql, hand_lexer_only):
    validate_read_only_query(sql)


def test_legit_reads_run(tmp_path):
    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    conn.execute("CREATE TABLE t AS SELECT 1 AS i")
    conn.execute('CREATE TABLE w ("delete" INT, "update" INT, copy INT, "insert" INT, merge INT, i INT)')
    for sql in LEGIT:
        conn.execute(sql).fetchall()


def test_find_nested_write_ignores_the_top_level_verb():
    from havn.engine.sql_safety import find_nested_write

    assert find_nested_write("DELETE FROM t WHERE i = 1") is None
    assert find_nested_write("INSERT INTO t (update, copy) SELECT 1, 2") is None
    assert find_nested_write("COPY (SELECT 1) TO 'x.csv'") is None
    assert find_nested_write("CREATE TABLE x AS (SELECT 1)") is None
    assert find_nested_write("INSERT INTO t WITH d AS (DELETE FROM u RETURNING *) SELECT * FROM d") == "delete"


@duck2_only
def test_writing_cte_actually_writes_without_the_check(tmp_path):
    """What the check stands between: DuckDB 2.0 runs these."""
    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    conn.execute("CREATE TABLE t AS SELECT 1 AS i")
    conn.execute("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d LIMIT 1").fetchall()
    assert conn.execute("SELECT count(*) FROM t").fetchone() == (0,)


def _server(tmp_path):
    import havn.server.app as server_app
    from havn.server import deps

    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: warehouse.duckdb\n")
    (tmp_path / "transform").mkdir()
    c = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    c.execute("CREATE SCHEMA landing")
    c.execute("CREATE TABLE landing.people AS SELECT 1 AS id")
    c.close()
    deps.reset_shared_conn()
    deps._clear_config_cache()
    deps.invalidate_token_cache()
    with deps._login_attempts_lock:
        deps._login_attempts.clear()
    return server_app, deps


def test_viewer_cannot_become_admin_through_a_writing_cte(tmp_path):
    """The review's reproduction against /api/query."""
    from fastapi.testclient import TestClient

    server_app, deps = _server(tmp_path)
    old_dir, old_auth = server_app.PROJECT_DIR, server_app.AUTH_ENABLED
    server_app.PROJECT_DIR = tmp_path
    server_app.AUTH_ENABLED = True
    try:
        anon = TestClient(server_app.app)
        tok = anon.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "adminpass1", "role": "admin"},
        ).json()["token"]
        admin = TestClient(server_app.app)
        admin.headers["Authorization"] = f"Bearer {tok}"
        assert admin.post(
            "/api/users",
            json={"username": "viewer", "password": "viewerpass1", "role": "viewer"},
        ).status_code == 200
        vtok = anon.post(
            "/api/auth/login", json={"username": "viewer", "password": "viewerpass1"}
        ).json()["token"]
        viewer = TestClient(server_app.app)
        viewer.headers["Authorization"] = f"Bearer {vtok}"

        for sql in (
            "WITH d AS (UPDATE _havn.users SET role='admin' WHERE username='viewer' "
            "RETURNING username, role) SELECT * FROM d LIMIT 1",
            "WITH d AS (DELETE FROM landing.people RETURNING *) SELECT * FROM d LIMIT 1",
            f"WITH d AS (COPY landing.people TO '{(tmp_path / 'leak.csv').as_posix()}') SELECT 1 LIMIT 1",
        ):
            r = viewer.post("/api/query", json={"sql": sql})
            assert r.status_code in (400, 403), (sql, r.status_code, r.text)
            if DUCK2:
                assert r.status_code == 403

        users = {u["username"]: u["role"] for u in admin.get("/api/users").json()}
        assert users["viewer"] == "viewer"
        rows = admin.post("/api/query", json={"sql": "SELECT count(*) FROM landing.people"}).json()["rows"]
        assert rows == [[1]]
        assert not (tmp_path / "leak.csv").exists()
    finally:
        deps.reset_shared_conn()
        server_app.PROJECT_DIR, server_app.AUTH_ENABLED = old_dir, old_auth


def test_governed_reads_run_in_a_read_only_transaction(tmp_path, monkeypatch):
    """Last line of defence: even SQL the checker missed cannot write the database."""
    from havn.engine import governed_query
    from havn.engine.governed_query import GovernedQueryError, QueryIdentity, run_governed_query

    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    conn.execute("CREATE TABLE t AS SELECT 1 AS i")
    monkeypatch.setattr(
        governed_query, "prepare_governed_sql",
        lambda conn, sql, identity, **k: governed_query.PreparedQuery(original_sql=sql, sql=sql),
    )
    monkeypatch.setattr("havn.engine.sql_safety.validate_read_only_query", lambda sql, **k: None)
    with pytest.raises(GovernedQueryError) as exc:
        run_governed_query(conn, "INSERT INTO t VALUES (2)", QueryIdentity("v", "viewer"), row_cap=None)
    assert "read-only" in str(exc.value).lower()
    assert conn.execute("SELECT count(*) FROM t").fetchone() == (1,)
    # The connection is usable afterwards, and an ordinary read still works.
    out = run_governed_query(conn, "SELECT i FROM t", QueryIdentity("v", "viewer"))
    assert out["rows"] == [[1]]
    conn.execute("INSERT INTO t VALUES (3)")


def test_governed_read_leaves_a_callers_transaction_alone(tmp_path, monkeypatch):
    # BEGIN inside an open transaction fails and aborts it, so the read-only
    # transaction is only opened when the caller has none.
    from havn.engine import governed_query
    from havn.engine.governed_query import QueryIdentity, run_governed_query

    monkeypatch.setattr(
        governed_query, "prepare_governed_sql",
        lambda conn, sql, identity, **k: governed_query.PreparedQuery(original_sql=sql, sql=sql),
    )
    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    conn.execute("CREATE TABLE t AS SELECT 1 AS i")
    conn.execute("BEGIN")
    conn.execute("INSERT INTO t VALUES (2)")
    out = run_governed_query(conn, "SELECT count(*) FROM t", QueryIdentity("v", "admin"))
    assert out["rows"] == [[2]]
    conn.execute("COMMIT")
    assert conn.execute("SELECT count(*) FROM t").fetchone() == (2,)


# ---------------------------------------------------------------------------
# Governed sessions (governed Python, /v1/sql): a write must be its own
# statement, so it goes through _write and the _havn admin-only check.
# ---------------------------------------------------------------------------


@pytest.fixture
def session(tmp_path):
    from havn.engine.auth import create_user
    from havn.engine.database import ensure_meta_table
    from havn.engine.governance.statements import GovernedSession
    from havn.engine.governance.viewer import Viewer

    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    ensure_meta_table(conn)
    conn.execute("CREATE SCHEMA landing")
    conn.execute("CREATE TABLE landing.t AS SELECT 1 AS i")
    create_user(conn, "v", "viewerpass1", "editor")
    s = GovernedSession(conn.cursor(), Viewer("v", "editor"), project_dir=tmp_path,
                        warehouse=tmp_path / "warehouse.duckdb", cwd=tmp_path)
    yield s, conn
    s.close()
    conn.close()


GOVERNED_REFUSED = [
    "WITH d AS (UPDATE _havn.users SET role='admin' WHERE username='v' RETURNING username) SELECT * FROM d",
    "WITH d AS (DELETE FROM landing.t RETURNING *) SELECT * FROM d",
    "SELECT * FROM (WITH d AS (DELETE FROM landing.t RETURNING *) SELECT * FROM d)",
    "WITH d AS (COPY landing.t TO 'leak.csv') SELECT 1",
    "INSERT INTO landing.t WITH d AS (DELETE FROM _havn.users RETURNING 1) SELECT * FROM d",
    "EXPLAIN ANALYZE WITH d AS (DELETE FROM landing.t RETURNING *) SELECT * FROM d",
    "EXPLAIN ANALYZE DELETE FROM landing.t",
]


@pytest.mark.parametrize("sql", GOVERNED_REFUSED)
def test_governed_session_refuses_nested_writes(session, sql):
    from havn.engine.governance.rewrite import GovernanceError

    s, conn = session
    with pytest.raises((GovernanceError, duckdb.ParserException)):
        s.execute(sql)
    assert conn.execute("SELECT role FROM _havn.users WHERE username = 'v'").fetchone() == ("editor",)
    assert conn.execute("SELECT count(*) FROM landing.t").fetchone() == (1,)


def test_governed_session_still_reads_and_writes_plainly(session):
    s, conn = session
    assert s.execute("WITH a AS (SELECT i FROM landing.t) SELECT * FROM a").rowcount == 1
    s.execute("INSERT INTO landing.t VALUES (2)")
    s.execute("DELETE FROM landing.t WHERE i = 2")
    assert conn.execute("SELECT count(*) FROM landing.t").fetchone() == (1,)
