"""Row policies reach every surface that shares the governed read path.

Dashboards, published links, reports, the semantic layer and `havn ask` all
run through ``engine/governed_query.py`` (``read_path`` wraps it). These
tests pin that row-level security and masking apply there, for identities
built the way each surface builds them.
"""

from __future__ import annotations

import duckdb
import pytest

from havn.engine.governance.catalog import clear_cache
from havn.engine.governed_query import GovernedQueryError, QueryIdentity, run_governed_query
from havn.engine.masking import create_policy
from havn.engine.read_path import run_read_query
from havn.engine.row_policies import create_row_policy


@pytest.fixture
def conn(tmp_path):
    clear_cache()
    c = duckdb.connect(str(tmp_path / "w.duckdb"))
    c.execute("CREATE SCHEMA silver")
    c.execute("""CREATE TABLE silver.customers AS SELECT * FROM (VALUES
        (1, 'alice', 'north', '111-11-1111'),
        (2, 'bob', 'south', '222-22-2222')) t(id, name, region, ssn)""")
    create_row_policy(c, schema_name="silver", table_name="customers",
                      filter_sql="region = havn_attr('region')")
    create_policy(c, schema_name="silver", table_name="customers", column_name="ssn", method="redact")
    yield c
    c.close()


def _names(result: dict) -> list[str]:
    i = result["columns"].index("name")
    return sorted(r[i] for r in result["rows"])


def test_dashboard_identity_from_user_carries_attributes(conn):
    user = {"username": "nora", "role": "editor", "attributes": {"region": "north"}}
    result = run_governed_query(
        conn, "SELECT * FROM silver.customers", QueryIdentity.from_user(user, source="dashboard"),
    )
    assert _names(result) == ["alice"]
    assert "111-11-1111" not in str(result["rows"])


def test_report_and_share_identity_reads_stored_attributes(conn):
    # Reports and "view as user" links build the identity from a username and
    # role only; the user's attributes come from _havn.users.
    from havn.engine.auth import create_user, ensure_auth_tables

    ensure_auth_tables(conn)
    create_user(conn, "sam", "pw-123456789", role="viewer")
    conn.execute(
        "UPDATE _havn.users SET attributes = ? WHERE username = 'sam'", ['{"region": "south"}'],
    )
    identity = QueryIdentity(username="sam", role="viewer", source="report")
    assert _names(run_governed_query(conn, "SELECT * FROM silver.customers", identity)) == ["bob"]


def test_role_only_share_link_sees_no_attribute_filtered_rows(conn):
    identity = QueryIdentity(username="share:abc", role="viewer", source="share", attributes={})
    assert run_governed_query(conn, "SELECT * FROM silver.customers", identity)["rows"] == []


def test_cache_key_separates_viewers_with_the_same_role():
    a = QueryIdentity("a", "viewer", attributes={"region": "north"})
    b = QueryIdentity("a", "viewer", attributes={"region": "south"})
    assert a.cache_key() != b.cache_key()


def test_read_path_used_by_semantic_and_ask_applies_row_policies(conn):
    user = {"username": "nora", "role": "editor", "attributes": {"region": "north"}}
    result = run_read_query(conn, "SELECT name, count(*) AS n FROM silver.customers GROUP BY 1", user=user)
    assert [r[0] for r in result.rows] == ["alice"]


def test_admin_is_exempt(conn):
    result = run_governed_query(
        conn, "SELECT * FROM silver.customers", QueryIdentity("root", "admin", attributes={}),
    )
    assert _names(result) == ["alice", "bob"]


def test_writes_still_refused(conn):
    with pytest.raises(GovernedQueryError):
        run_governed_query(conn, "DELETE FROM silver.customers", QueryIdentity("root", "admin"))


# ---------------------------------------------------------------------------
# Row cap and the truncated flag


@pytest.fixture
def plain_conn(tmp_path):
    c = duckdb.connect(str(tmp_path / "plain.duckdb"))
    yield c
    c.close()


_ADMIN = QueryIdentity(username="root", role="admin")


def test_exactly_limit_rows_is_not_reported_as_truncated(plain_conn):
    # The query UI said "results capped" for a result that had exactly as
    # many rows as the limit, when there was nothing more to show.
    r = run_governed_query(plain_conn, "SELECT * FROM range(10)", _ADMIN, limit=10)
    assert r["row_count"] == 10
    assert r["truncated"] is False


def test_one_row_past_the_limit_is_truncated_and_trimmed(plain_conn):
    r = run_governed_query(plain_conn, "SELECT * FROM range(11)", _ADMIN, limit=10)
    assert r["row_count"] == 10
    assert r["truncated"] is True
    assert r["limit"] == 10


def test_paging_reports_whether_another_page_exists(plain_conn):
    r = run_governed_query(plain_conn, "SELECT * FROM range(25) ORDER BY 1", _ADMIN, limit=10, offset=20)
    assert [row[0] for row in r["rows"]] == [20, 21, 22, 23, 24]
    assert r["truncated"] is False
    r = run_governed_query(plain_conn, "SELECT * FROM range(25) ORDER BY 1", _ADMIN, limit=10, offset=10)
    assert r["row_count"] == 10
    assert r["truncated"] is True


def test_limit_in_a_comment_or_string_does_not_skip_the_row_cap(plain_conn):
    for sql in (
        "SELECT * FROM range(100) -- TODO add a limit",
        "SELECT range, 'no limit' AS note FROM range(100)",
    ):
        r = run_governed_query(plain_conn, sql, _ADMIN, row_cap=50)
        assert r["row_count"] == 50, sql
        assert r["truncated"] is True, sql


def test_limit_in_a_subquery_still_caps_the_outer_result(plain_conn):
    # The SQL is left unwrapped because it has a LIMIT, but that LIMIT bounds
    # only the subquery: the fetch itself must still stop at the cap.
    sql = "SELECT * FROM (SELECT * FROM range(5) LIMIT 5) UNION ALL SELECT * FROM range(100)"
    r = run_governed_query(plain_conn, sql, _ADMIN, row_cap=50)
    assert r["row_count"] == 50
    assert r["truncated"] is True


def test_own_limit_under_the_cap_is_not_truncated(plain_conn):
    r = run_governed_query(plain_conn, "SELECT * FROM range(100) LIMIT 7", _ADMIN, row_cap=50)
    assert r["row_count"] == 7
    assert r["truncated"] is False


def test_timestamptz_results_serialize(plain_conn):
    # DuckDB needs pytz to hand TIMESTAMP WITH TIME ZONE values to Python.
    # Without it every query touching now() or current_timestamp failed, in
    # the UI and the CLI alike.
    r = run_governed_query(plain_conn, "SELECT current_timestamp AS ts, now() AS n", _ADMIN)
    assert r["columns"] == ["ts", "n"]
    assert isinstance(r["rows"][0][0], str) and r["rows"][0][0]


def test_error_context_shows_the_users_sql_not_the_paging_wrapper(plain_conn):
    with pytest.raises(GovernedQueryError) as exc:
        run_governed_query(plain_conn, "SELECT nope FROM range(3)", _ADMIN, limit=10)
    msg = str(exc.value)
    assert "LINE 1: SELECT nope FROM range(3)" in msg, msg
    assert "SELECT * FROM (" not in msg
    # The caret still points at the offending column.
    lines = msg.split("\n")
    i = next(n for n, line in enumerate(lines) if line.startswith("LINE 1: "))
    caret = lines[i + 1].index("^")
    assert lines[i][caret:].startswith("nope"), (lines[i], lines[i + 1])
