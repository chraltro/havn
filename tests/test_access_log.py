"""The data-access log: who read which relations and PII columns, and when.

Pins that a governed read records the relations it touched, the PII-classified
columns in scope, the columns a policy masked, and the row-filtered tables, and
that the log can be queried by user, relation, PII column and date range — the
question the plain audit log (truncated SQL only) cannot answer.
"""

from __future__ import annotations

import duckdb
import pytest

from havn.engine.access_log import query_access_log
from havn.engine.governance.catalog import clear_cache
from havn.engine.governed_query import QueryIdentity, run_governed_query
from havn.engine.masking import create_policy
from havn.engine.row_policies import create_row_policy
from havn.engine.transform import run_transform


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
    create_policy(c, schema_name="silver", table_name="customers",
                  column_name="ssn", method="redact")
    yield c
    c.close()


def test_access_log_records_tables_masks_and_rows(conn):
    user = {"username": "nora", "role": "editor", "attributes": {"region": "north"}}
    run_governed_query(
        conn, "SELECT * FROM silver.customers",
        QueryIdentity.from_user(user, source="dashboard"),
        audit_conn=conn, ip_address="10.0.0.9",
    )

    entries = query_access_log(conn)
    assert len(entries) == 1
    e = entries[0]
    assert e["user"] == "nora"
    assert e["role"] == "editor"
    assert e["source"] == "dashboard"
    assert e["ip_address"] == "10.0.0.9"
    assert "silver.customers" in e["tables"]
    assert "silver.customers.ssn" in e["masked_columns"]
    assert "silver.customers" in e["row_filtered_tables"]
    assert e["policy_count"] >= 1
    # The row policy showed only the north row.
    assert e["row_count"] == 1


def test_access_log_not_written_without_audit_conn(conn):
    # Passing no writable connection leaves the log empty: callers opt in.
    user = {"username": "nora", "role": "editor", "attributes": {"region": "north"}}
    run_governed_query(
        conn, "SELECT * FROM silver.customers",
        QueryIdentity.from_user(user, source="dashboard"),
    )
    assert query_access_log(conn) == []


def test_access_log_query_filters(conn):
    north = {"username": "nora", "role": "editor", "attributes": {"region": "north"}}
    south = {"username": "sven", "role": "viewer", "attributes": {"region": "south"}}
    for user in (north, south):
        run_governed_query(
            conn, "SELECT * FROM silver.customers",
            QueryIdentity.from_user(user, source="query"),
            audit_conn=conn,
        )

    assert {e["user"] for e in query_access_log(conn)} == {"nora", "sven"}
    assert [e["user"] for e in query_access_log(conn, user="sven")] == ["sven"]
    # "Who touched silver.customers.ssn" — the headline question.
    masked_readers = {
        e["user"] for e in query_access_log(conn, table="silver.customers")
    }
    assert masked_readers == {"nora", "sven"}
    # Date range: everything is after the epoch, nothing is before it.
    assert len(query_access_log(conn, since="1970-01-01")) == 2
    assert query_access_log(conn, until="1970-01-01") == []


def test_access_log_captures_pii_classification(conn, tmp_path):
    # @pii marks a column; the access log should name it whenever a governed
    # read reaches it. The read must be governed (here, a masking policy on the
    # same column) or the rewriter short-circuits for an unrestricted viewer.
    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: w.duckdb\n")
    model_dir = tmp_path / "transform" / "silver"
    model_dir.mkdir(parents=True)
    (model_dir / "people.sql").write_text(
        "@config materialized=table\n@pii email\nSELECT 'a@x.com' AS email, 'north' AS region"
    )
    run_transform(conn, tmp_path / "transform")
    clear_cache()
    create_policy(conn, schema_name="silver", table_name="people",
                  column_name="email", method="redact")

    user = {"username": "nora", "role": "editor", "attributes": {"region": "north"}}
    run_governed_query(
        conn, "SELECT * FROM silver.people",
        QueryIdentity.from_user(user, source="query"),
        audit_conn=conn, project_dir=tmp_path,
    )
    e = query_access_log(conn, pii_column="silver.people.email")
    assert len(e) == 1
    assert "silver.people.email" in e[0]["pii_columns"]
