"""Read-only SQL validator: lexer parity with DuckDB and the parser cross-check.

Every case in BYPASSES was a way to hide a second statement (or a write under
EXPLAIN ANALYZE) from the validator while DuckDB happily ran it. Each is
checked twice: through the full validator, and through the hand lexer alone,
so neither layer silently depends on the other.
"""

from __future__ import annotations

import duckdb
import pytest

import havn.engine.sql_safety as sql_safety
from havn.engine.sql_safety import ReadOnlyQueryError, validate_read_only_query


BYPASSES = [
    # E'' escape strings: \' does not end the literal in DuckDB.
    "SELECT E'\\'' ; DROP TABLE t; UPDATE t SET i = 2; SELECT 'a'",
    "SELECT e'\\'' ; DROP TABLE t; SELECT 'a'",
    # Dollar-quoted strings.
    "SELECT $$ ' $$; DROP TABLE t; SELECT ' '",
    "SELECT $tag$ ' $tag$; DROP TABLE t; SELECT ' '",
    # Block comments nest.
    "/* /* */ ' */ SELECT 1; DROP TABLE t; SELECT 1 /* ' */",
    # A line comment ends at a carriage return too.
    "SELECT 1 --\r; DROP TABLE t; SELECT 1\n",
    # Parentheses inside a quoted identifier must not unbalance the splitter.
    'SELECT 1 AS "("; DROP TABLE t; SELECT 1 AS ")"',
    # EXPLAIN ANALYZE executes the statement it profiles.
    "EXPLAIN ANALYZE DELETE FROM t",
    "explain analyse update t set i = 2",
    "EXPLAIN (FORMAT json) DELETE FROM t",
    "/* c */ EXPLAIN ANALYZE -- x\n DELETE FROM t",
]

LEGIT = [
    "SELECT E'it\\'s' AS a",
    "SELECT E'back\\\\' AS b, 'c' AS d",
    "SELECT e'line\\nbreak' AS a",
    "SELECT $$a;b$$ AS a, $t$x'y$t$ AS b",
    'SELECT 1 AS "we""ird;(x"',
    "SELECT 1 /* a /* nested */ b */",
    "SELECT 1 -- trailing\r\n",
    "SELECT 'it''s'",
    "SELECT 1;",
    "EXPLAIN SELECT 1",
    "EXPLAIN ANALYZE SELECT * FROM t",
    "EXPLAIN (FORMAT json) SELECT 1",
    "WITH a AS (SELECT 1 AS x) SELECT * FROM a",
    "FROM t",
    "DESCRIBE t",
    "SUMMARIZE t",
    "PIVOT t ON i USING sum(i)",
    "SELECT * FROM t, (PIVOT t ON i USING sum(i))",
    "SELECT $a, $b",
    "SELECT ?",
]


@pytest.fixture
def hand_lexer_only(monkeypatch):
    monkeypatch.setattr(sql_safety, "_check_with_duckdb_parser", lambda sql, **k: None)


@pytest.mark.parametrize("sql", BYPASSES)
def test_bypass_rejected(sql):
    with pytest.raises(ReadOnlyQueryError) as exc:
        validate_read_only_query(sql)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("sql", BYPASSES)
def test_bypass_rejected_by_hand_lexer_alone(sql, hand_lexer_only):
    with pytest.raises(ReadOnlyQueryError):
        validate_read_only_query(sql)


@pytest.mark.parametrize("sql", BYPASSES)
def test_bypasses_are_real_multi_statement_or_write(sql):
    # Guard against the test list drifting: DuckDB must actually see a write.
    stmts = duckdb.connect().extract_statements(sql)
    kinds = {s.type for s in stmts}
    assert len(stmts) > 1 or kinds == {duckdb.StatementType.EXPLAIN}


@pytest.mark.parametrize("sql", LEGIT)
def test_legit_queries_pass(sql):
    validate_read_only_query(sql)


@pytest.mark.parametrize("sql", LEGIT)
def test_legit_queries_pass_hand_lexer(sql, hand_lexer_only):
    validate_read_only_query(sql)


def test_unparseable_sql_is_left_to_execution():
    # Model bodies with {start}/{end} placeholders are validated but never
    # executed verbatim; a DuckDB parse error must not reject them.
    validate_read_only_query("SELECT * FROM t WHERE ts >= {start}")


def test_escape_string_select_still_runs(tmp_path):
    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    sql = "SELECT E'it\\'s' AS a, $$x;y$$ AS b"
    validate_read_only_query(sql)
    assert conn.execute(sql).fetchone() == ("it's", "x;y")


def test_api_viewer_cannot_smuggle_writes(tmp_path):
    """The audit reproduction: a viewer turning itself into admin."""
    from fastapi.testclient import TestClient

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

        sql = (
            "SELECT E'\\'' ; DROP TABLE landing.people; "
            "UPDATE _havn.users SET role='admin' WHERE username='viewer'; SELECT 'x'"
        )
        r = viewer.post("/api/query", json={"sql": sql})
        assert r.status_code == 403
        r = viewer.post("/api/query", json={"sql": "EXPLAIN ANALYZE DELETE FROM landing.people"})
        assert r.status_code == 403

        users = {u["username"]: u["role"] for u in admin.get("/api/users").json()}
        assert users["viewer"] == "viewer"
        rows = admin.post(
            "/api/query", json={"sql": "SELECT count(*) FROM landing.people"}
        ).json()["rows"]
        assert rows == [[1]]
    finally:
        deps.reset_shared_conn()
        server_app.PROJECT_DIR, server_app.AUTH_ENABLED = old_dir, old_auth


# ---------------------------------------------------------------------------
# File scans found in DuckDB's parse tree, and SQL that only parses wrapped
# ---------------------------------------------------------------------------

FILE_SCANS = [
    "SELECT * FROM range(1), 'data/x.csv'",
    "SELECT * FROM range(1) r, 'data/x.csv' f",
    "SELECT * FROM range(1) AS r(a), \"C:/data/x\"",
    "SELECT * FROM (SELECT 1) a, 'secret.txt'",
    "SELECT * FROM range(1)\n,'x.csv'",
    "SELECT * FROM t WHERE a IN (FROM 'x.csv')",
    "SUMMARIZE 'x.csv'",
    "DESCRIBE 'x.csv'",
    "SHOW 'x.csv'",
    "TABLE 'x.csv'",
    "SELECT * FROM (DESCRIBE 'x.csv')",
    "PIVOT 'x.csv' ON b USING sum(1)",
    "PIVOT (SELECT * FROM range(1), 'x.csv') ON b USING sum(1)",
    "EXPLAIN SELECT * FROM range(1), 'x.csv'",
    "SELECT getenv('HOME')",
]


@pytest.mark.parametrize("sql", FILE_SCANS)
def test_file_scans_rejected(sql):
    with pytest.raises(ReadOnlyQueryError):
        validate_read_only_query(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM landing.people p, gold.orders o",
        "SELECT * FROM range(3) r, (SELECT 1) s",
        "SELECT 'C:/path/x.csv' AS p",
        "SELECT * FROM t WHERE f = 'a/b.csv'",
        "DESCRIBE landing.people",
        "SHOW TABLES",
    ],
)
def test_catalog_references_and_path_values_pass(sql):
    validate_read_only_query(sql)


def test_pivot_passes_after_parser_connection_has_run_queries():
    # Once the parser connection has run a query, DuckDB wraps a dynamic
    # PIVOT's hidden CREATE TYPE in SET statements; those must not count.
    validate_read_only_query("SELECT * FROM range(1), (SELECT 1) s")
    validate_read_only_query("PIVOT t ON a USING sum(b)")
    validate_read_only_query("PIVOT t ON a USING sum(b)")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1) AS a, 'x.csv' AS b, (SELECT 1",
        "SELECT 1) UNION (SELECT 2",
        "SELECT (1",
    ],
)
def test_unbalanced_parentheses_rejected(sql):
    # /api/query wraps the SQL as SELECT * FROM (<sql>) AS _q, under which an
    # unbalanced query becomes a different, valid one.
    with pytest.raises(ReadOnlyQueryError) as exc:
        validate_read_only_query(sql)
    assert exc.value.status_code == 400


def test_parens_inside_strings_and_identifiers_do_not_count():
    validate_read_only_query("SELECT ')' AS a, \"(\" FROM (SELECT 1 AS \"(\")")
