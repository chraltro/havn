"""Tests for pre-query SQL masking rewriter."""

from __future__ import annotations

import pytest
import duckdb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    """DuckDB with test data and masking policies table."""
    db = duckdb.connect(str(tmp_path / "test.duckdb"))
    db.execute("CREATE SCHEMA IF NOT EXISTS _havn")
    db.execute("CREATE SCHEMA IF NOT EXISTS gold")
    db.execute("""
        CREATE TABLE gold.customers (
            id INTEGER,
            name VARCHAR,
            email VARCHAR,
            phone VARCHAR,
            status VARCHAR
        )
    """)
    db.execute("""
        INSERT INTO gold.customers VALUES
        (1, 'Alice', 'alice@example.com', '+1-555-0001', 'active'),
        (2, 'Bob', 'bob@example.com', '+1-555-0002', 'inactive'),
        (3, 'Charlie', 'charlie@example.com', '+1-555-0003', 'active')
    """)
    from havn.engine.masking import ensure_masking_table

    ensure_masking_table(db)
    yield db
    db.close()


def _create_policy(conn, **kwargs):
    from havn.engine.masking import create_policy

    defaults = {
        "schema_name": "gold",
        "table_name": "customers",
        "column_name": "email",
        "method": "redact",
    }
    defaults.update(kwargs)
    return create_policy(conn, **defaults)


# ---------------------------------------------------------------------------
# SQL builder unit tests
# ---------------------------------------------------------------------------


class TestSqlBuilders:
    """Verify each SQL builder produces valid DuckDB SQL."""

    def test_hash(self, conn):
        from havn.engine.masking_rewriter import _sql_hash

        expr = _sql_hash("email", {})
        row = conn.execute(f"SELECT {expr} FROM (SELECT 'test@example.com' AS email)").fetchone()
        assert isinstance(row[0], str)
        assert len(row[0]) == 8

    def test_redact(self, conn):
        from havn.engine.masking_rewriter import _sql_redact

        expr = _sql_redact("email", {})
        row = conn.execute(f"SELECT {expr} FROM (SELECT 'test' AS email)").fetchone()
        assert row[0] == "***"

    def test_null(self, conn):
        from havn.engine.masking_rewriter import _sql_null

        expr = _sql_null("email", {})
        row = conn.execute(f"SELECT {expr} FROM (SELECT 'test' AS email)").fetchone()
        assert row[0] is None

    def test_partial(self, conn):
        from havn.engine.masking_rewriter import _sql_partial

        expr = _sql_partial("name", {"show_first": 1, "show_last": 1})
        row = conn.execute(f"SELECT {expr} FROM (SELECT 'Alice' AS name)").fetchone()
        assert row[0].startswith("A")
        assert row[0].endswith("e")
        assert "***" in row[0] or "*" in row[0]

    def test_email(self, conn):
        from havn.engine.masking_rewriter import _sql_email

        expr = _sql_email("email", {})
        row = conn.execute(f"SELECT {expr} FROM (SELECT 'alice@example.com' AS email)").fetchone()
        assert row[0] == "***@example.com"

    def test_email_no_at(self, conn):
        from havn.engine.masking_rewriter import _sql_email

        expr = _sql_email("email", {})
        row = conn.execute(f"SELECT {expr} FROM (SELECT 'no-email' AS email)").fetchone()
        assert row[0] == "***"

    def test_phone(self, conn):
        from havn.engine.masking_rewriter import _sql_phone

        expr = _sql_phone("phone", {"show_last": 4})
        row = conn.execute(f"SELECT {expr} FROM (SELECT '+1-555-1234' AS phone)").fetchone()
        assert row[0].endswith("1234")
        assert row[0].startswith("***")

    def test_credit_card(self, conn):
        from havn.engine.masking_rewriter import _sql_credit_card

        expr = _sql_credit_card("cc", {"show_last": 4})
        row = conn.execute(f"SELECT {expr} FROM (SELECT '4111111111111111' AS cc)").fetchone()
        assert row[0].endswith("1111")
        assert row[0].startswith("*")

    def test_truncate(self, conn):
        from havn.engine.masking_rewriter import _sql_truncate

        expr = _sql_truncate("name", {"length": 3})
        row = conn.execute(f"SELECT {expr} FROM (SELECT 'Alice' AS name)").fetchone()
        assert row[0] == "Ali..."

    def test_consistent_hash(self, conn):
        from havn.engine.masking_rewriter import _sql_consistent_hash

        expr = _sql_consistent_hash("email", {"prefix": "usr_", "length": 8})
        row = conn.execute(f"SELECT {expr} FROM (SELECT 'alice@example.com' AS email)").fetchone()
        assert row[0].startswith("usr_")
        assert len(row[0]) == 12  # prefix(4) + hash(8)

    def test_range(self, conn):
        from havn.engine.masking_rewriter import _sql_range

        expr = _sql_range("val", {"bucket_size": 10000})
        row = conn.execute(f"SELECT {expr} FROM (SELECT 47382 AS val)").fetchone()
        assert row[0] == "40000-50000"

    def test_first_initial(self, conn):
        from havn.engine.masking_rewriter import _sql_first_initial

        expr = _sql_first_initial("name", {})
        row = conn.execute(f"SELECT {expr} FROM (SELECT 'Alice' AS name)").fetchone()
        assert row[0] == "A."

    def test_ip_address(self, conn):
        from havn.engine.masking_rewriter import _sql_ip_address

        expr = _sql_ip_address("ip", {"keep_octets": 2})
        row = conn.execute(f"SELECT {expr} FROM (SELECT '192.168.1.42' AS ip)").fetchone()
        assert row[0] == "192.168.x.x"


# ---------------------------------------------------------------------------
# Rewriter core tests
# ---------------------------------------------------------------------------


class TestRewriter:
    """Test rewrite_query_with_masking end-to-end."""

    def test_alias_bypass_fixed(self, conn):
        """The core bug: SELECT email AS x should still be masked."""
        _create_policy(conn)
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        sql = "SELECT email AS x FROM gold.customers"
        rewritten, ok, handled = rewrite_query_with_masking(sql, "viewer", conn)
        assert ok
        assert len(handled) > 0

        # Execute the rewritten SQL and verify masking
        rows = conn.execute(rewritten).fetchall()
        for row in rows:
            assert "@" not in str(row[0]) or row[0].startswith("***@")

    def test_table_alias(self, conn):
        """SELECT c.email FROM gold.customers c should be masked."""
        _create_policy(conn)
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        sql = "SELECT c.email FROM gold.customers c"
        rewritten, ok, handled = rewrite_query_with_masking(sql, "viewer", conn)
        assert ok

        rows = conn.execute(rewritten).fetchall()
        for row in rows:
            assert row[0] == "***"

    def test_no_alias(self, conn):
        """SELECT email FROM gold.customers should be masked, column name preserved."""
        _create_policy(conn)
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        sql = "SELECT email FROM gold.customers"
        rewritten, ok, handled = rewrite_query_with_masking(sql, "viewer", conn)
        assert ok

        result = conn.execute(rewritten)
        col_names = [desc[0] for desc in result.description]
        rows = result.fetchall()
        # Column name should be preserved via alias
        assert "email" in [c.lower() for c in col_names]
        for row in rows:
            assert row[0] == "***"

    def test_admin_exempt(self, conn):
        """Admin should get original SQL unchanged."""
        _create_policy(conn)
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        sql = "SELECT email FROM gold.customers"
        rewritten, ok, handled = rewrite_query_with_masking(sql, "admin", conn)
        assert not ok
        assert rewritten == sql

    def test_no_policies(self, conn):
        """No policies means no rewriting."""
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        sql = "SELECT email FROM gold.customers"
        rewritten, ok, handled = rewrite_query_with_masking(sql, "viewer", conn)
        assert not ok
        assert rewritten == sql

    def test_no_matching_columns(self, conn):
        """Policy on email but query selects name -- no rewrite needed."""
        _create_policy(conn)
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        sql = "SELECT name FROM gold.customers"
        rewritten, ok, handled = rewrite_query_with_masking(sql, "viewer", conn)
        assert not ok

    def test_parse_failure_fallback(self, conn):
        """Unparseable SQL should fall back gracefully."""
        _create_policy(conn)
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        sql = "THIS IS NOT VALID SQL !!!"
        rewritten, ok, handled = rewrite_query_with_masking(sql, "viewer", conn)
        assert not ok
        assert rewritten == sql
        assert len(handled) == 0

    def test_multiple_policies(self, conn):
        """Two columns with different masking methods."""
        _create_policy(conn, column_name="email", method="redact")
        _create_policy(conn, column_name="name", method="hash")
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        sql = "SELECT name, email FROM gold.customers"
        rewritten, ok, handled = rewrite_query_with_masking(sql, "viewer", conn)
        assert ok
        assert len(handled) == 2

        rows = conn.execute(rewritten).fetchall()
        for row in rows:
            # name should be hashed (8 hex chars)
            assert len(row[0]) == 8
            # email should be redacted
            assert row[1] == "***"

    def test_conditional_policy_not_rewritten(self, conn):
        """Conditional policies should not be rewritten (left for post-query)."""
        _create_policy(
            conn,
            condition_column="status",
            condition_value="inactive",
        )
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        sql = "SELECT email, status FROM gold.customers"
        rewritten, ok, handled = rewrite_query_with_masking(sql, "viewer", conn)
        # Should not rewrite conditional policies
        assert not ok or len(handled) == 0

    def test_column_name_only_matching(self, conn):
        """Ad-hoc query without table qualifier matches on column name."""
        _create_policy(conn)
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        sql = "SELECT email FROM gold.customers"
        rewritten, ok, handled = rewrite_query_with_masking(sql, "viewer", conn)
        assert ok

    def test_expression_wrapping_masked_column(self, conn):
        """SELECT UPPER(email) should mask email inside the expression."""
        _create_policy(conn)
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        sql = "SELECT UPPER(email) AS upper_email FROM gold.customers"
        rewritten, ok, handled = rewrite_query_with_masking(sql, "viewer", conn)
        assert ok

        rows = conn.execute(rewritten).fetchall()
        for row in rows:
            # Should be UPPER of masked value, not UPPER of real email
            assert "ALICE" not in str(row[0])
            assert "BOB" not in str(row[0])

    def test_combined_alias_and_table_alias(self, conn):
        """SELECT c.email AS contact FROM gold.customers c -- both aliases."""
        _create_policy(conn)
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        sql = "SELECT c.email AS contact FROM gold.customers c"
        rewritten, ok, handled = rewrite_query_with_masking(sql, "viewer", conn)
        assert ok

        result = conn.execute(rewritten)
        col_names = [desc[0] for desc in result.description]
        rows = result.fetchall()
        assert "contact" in [c.lower() for c in col_names]
        for row in rows:
            assert row[0] == "***"


# ---------------------------------------------------------------------------
# Integration: execute rewritten SQL against real data
# ---------------------------------------------------------------------------


class TestIntegration:
    """Full round-trip: create policy, rewrite, execute, verify results."""

    def test_hash_method_round_trip(self, conn):
        """Hash masking produces consistent 8-char hex strings."""
        _create_policy(conn, method="hash")
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        sql = "SELECT email AS e FROM gold.customers WHERE id = 1"
        rewritten, ok, _ = rewrite_query_with_masking(sql, "viewer", conn)
        assert ok

        rows = conn.execute(rewritten).fetchall()
        assert len(rows) == 1
        assert len(rows[0][0]) == 8
        # Should be deterministic
        rows2 = conn.execute(rewritten).fetchall()
        assert rows[0][0] == rows2[0][0]

    def test_email_method_round_trip(self, conn):
        """Email masking keeps domain."""
        _create_policy(conn, method="email")
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        sql = "SELECT email FROM gold.customers WHERE id = 1"
        rewritten, ok, _ = rewrite_query_with_masking(sql, "viewer", conn)
        assert ok

        rows = conn.execute(rewritten).fetchall()
        assert rows[0][0] == "***@example.com"

    def test_truncate_method_round_trip(self, conn):
        _create_policy(conn, column_name="name", method="truncate",
                       method_config={"length": 3})
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        sql = "SELECT name FROM gold.customers WHERE id = 1"
        rewritten, ok, _ = rewrite_query_with_masking(sql, "viewer", conn)
        assert ok

        rows = conn.execute(rewritten).fetchall()
        assert rows[0][0] == "Ali..."

    def test_partial_method_round_trip(self, conn):
        _create_policy(conn, column_name="name", method="partial",
                       method_config={"show_first": 1, "show_last": 1})
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        sql = "SELECT name FROM gold.customers WHERE id = 1"
        rewritten, ok, _ = rewrite_query_with_masking(sql, "viewer", conn)
        assert ok

        rows = conn.execute(rewritten).fetchall()
        val = rows[0][0]
        assert val[0] == "A"
        assert val[-1] == "e"
        assert "*" in val

    def test_consistent_hash_join_safe(self, conn):
        """Same value should produce the same hash across queries."""
        _create_policy(conn, method="consistent_hash",
                       method_config={"prefix": "usr_", "length": 8})
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        sql1 = "SELECT email FROM gold.customers WHERE id = 1"
        rw1, ok1, _ = rewrite_query_with_masking(sql1, "viewer", conn)
        sql2 = "SELECT email FROM gold.customers WHERE id = 1"
        rw2, ok2, _ = rewrite_query_with_masking(sql2, "viewer", conn)
        assert ok1 and ok2

        r1 = conn.execute(rw1).fetchone()[0]
        r2 = conn.execute(rw2).fetchone()[0]
        assert r1 == r2
        assert r1.startswith("usr_")


# ---------------------------------------------------------------------------
# Masked column access denial (WHERE/ORDER BY/JOIN/HAVING)
# ---------------------------------------------------------------------------


class TestMaskedColumnAccessDenial:
    """Non-exempt users must not filter, sort, or join on masked columns."""

    def test_where_on_masked_column_denied(self, conn):
        _create_policy(conn)
        from havn.engine.masking_rewriter import rewrite_query_with_masking, MaskedColumnAccessError

        sql = "SELECT id, name FROM gold.customers WHERE email = 'alice@example.com'"
        with pytest.raises(MaskedColumnAccessError, match="email.*masked"):
            rewrite_query_with_masking(sql, "viewer", conn)

    def test_where_on_masked_column_with_table_alias(self, conn):
        _create_policy(conn)
        from havn.engine.masking_rewriter import rewrite_query_with_masking, MaskedColumnAccessError

        sql = "SELECT c.id FROM gold.customers c WHERE c.email LIKE '%@example.com'"
        with pytest.raises(MaskedColumnAccessError, match="email.*masked"):
            rewrite_query_with_masking(sql, "viewer", conn)

    def test_order_by_masked_column_denied(self, conn):
        _create_policy(conn)
        from havn.engine.masking_rewriter import rewrite_query_with_masking, MaskedColumnAccessError

        sql = "SELECT id, name FROM gold.customers ORDER BY email"
        with pytest.raises(MaskedColumnAccessError, match="email.*masked"):
            rewrite_query_with_masking(sql, "viewer", conn)

    def test_having_on_masked_column_denied(self, conn):
        _create_policy(conn)
        from havn.engine.masking_rewriter import rewrite_query_with_masking, MaskedColumnAccessError

        sql = "SELECT email, COUNT(*) FROM gold.customers GROUP BY email HAVING email = 'x'"
        with pytest.raises(MaskedColumnAccessError, match="email.*masked"):
            rewrite_query_with_masking(sql, "viewer", conn)

    def test_where_on_unmasked_column_allowed(self, conn):
        _create_policy(conn)  # only email is masked
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        sql = "SELECT id, name FROM gold.customers WHERE name = 'Alice'"
        # Should not raise -- name is not masked
        rewritten, ok, handled = rewrite_query_with_masking(sql, "viewer", conn)
        # No masking needed in SELECT (email not selected)
        assert not ok or len(handled) == 0

    def test_admin_can_filter_on_masked_column(self, conn):
        _create_policy(conn)
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        sql = "SELECT id FROM gold.customers WHERE email = 'alice@example.com'"
        # Admin is exempt -- should not raise
        rewritten, ok, handled = rewrite_query_with_masking(sql, "admin", conn)
        assert not ok  # no rewriting for admin

    def test_where_on_non_existent_policy_allowed(self, conn):
        """No policies at all -- WHERE on any column is fine."""
        from havn.engine.masking_rewriter import rewrite_query_with_masking

        sql = "SELECT id FROM gold.customers WHERE email = 'test@test.com'"
        rewritten, ok, handled = rewrite_query_with_masking(sql, "viewer", conn)
        assert not ok  # no policies, no rewriting


# ---------------------------------------------------------------------------
# skip_policy_ids in apply_masking
# ---------------------------------------------------------------------------


class TestSkipPolicyIds:
    """Test the skip_policy_ids parameter in apply_masking."""

    def test_skip_works(self, conn):
        from havn.engine.masking import apply_masking, create_policy

        policy = create_policy(
            conn, schema_name="gold", table_name="customers",
            column_name="email", method="redact",
        )
        columns = ["id", "name", "email"]
        rows = [[1, "Alice", "alice@example.com"]]

        # Without skip -- should be masked
        result = apply_masking(columns, [r[:] for r in rows], "viewer", conn,
                               schema="gold", table="customers")
        assert result[0][2] == "***"

        # With skip -- should NOT be masked
        result = apply_masking(columns, [r[:] for r in rows], "viewer", conn,
                               schema="gold", table="customers",
                               skip_policy_ids={policy["id"]})
        assert result[0][2] == "alice@example.com"
