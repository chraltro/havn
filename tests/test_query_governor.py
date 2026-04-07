"""Tests for the query governor (timeout enforcement)."""

from __future__ import annotations

import duckdb
import pytest

from havn.engine.query_governor import (
    DEFAULT_ROLE_TIMEOUTS,
    QueryTimeoutError,
    execute_governed,
    get_timeout_for_role,
)


@pytest.fixture
def conn():
    db = duckdb.connect(":memory:")
    yield db
    db.close()


class TestExecuteGoverned:
    """Test the governed query execution with timeouts."""

    def test_fast_query_succeeds(self, conn):
        result, duration_ms = execute_governed(conn, "SELECT 42 AS x", timeout_s=5)
        rows = result.fetchall()
        assert rows == [(42,)]
        assert duration_ms < 5000

    def test_timeout_raises(self, conn):
        # Generate a query that takes a long time
        # Creating a large cross join that DuckDB will process slowly
        conn.execute("CREATE TABLE t AS SELECT range AS x FROM range(10000)")
        with pytest.raises(QueryTimeoutError, match="timeout"):
            execute_governed(
                conn,
                "SELECT COUNT(*) FROM t a, t b, t c",
                timeout_s=0.5,
            )

    def test_sql_error_propagates(self, conn):
        with pytest.raises(Exception, match="not_a_table"):
            execute_governed(conn, "SELECT * FROM not_a_table", timeout_s=5)

    def test_result_and_duration_returned(self, conn):
        result, duration_ms = execute_governed(conn, "SELECT 1, 2, 3", timeout_s=5)
        assert result is not None
        assert duration_ms >= 0
        assert result.fetchone() == (1, 2, 3)


class TestRoleTimeouts:
    """Test role-based timeout resolution."""

    def test_admin_gets_longest_timeout(self):
        assert get_timeout_for_role("admin") == 300

    def test_editor_gets_medium_timeout(self):
        assert get_timeout_for_role("editor") == 120

    def test_viewer_gets_shortest_timeout(self):
        assert get_timeout_for_role("viewer") == 60

    def test_unknown_role_gets_viewer_default(self):
        assert get_timeout_for_role("unknown_role") == 60

    def test_config_overrides(self):
        custom = {"admin": 600, "viewer": 30}
        assert get_timeout_for_role("admin", custom) == 600
        assert get_timeout_for_role("viewer", custom) == 30

    def test_config_override_fallback(self):
        custom = {"admin": 600}
        # No viewer in config, falls back to viewer key or 60
        assert get_timeout_for_role("viewer", custom) == 60


class TestQueryTimeoutError:
    """Test the error message is user-friendly."""

    def test_error_message(self):
        err = QueryTimeoutError("Query exceeded 30s timeout")
        assert "30s" in str(err)

    def test_timeout_error_is_catchable(self, conn):
        conn.execute("CREATE TABLE t AS SELECT range AS x FROM range(10000)")
        caught = False
        try:
            execute_governed(
                conn,
                "SELECT COUNT(*) FROM t a, t b, t c",
                timeout_s=0.5,
            )
        except QueryTimeoutError:
            caught = True
        assert caught
