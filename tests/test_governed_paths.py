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
