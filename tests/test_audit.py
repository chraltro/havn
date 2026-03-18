"""Tests for audit logging."""

from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    """Provide a fresh DuckDB connection with audit tables."""
    db_path = tmp_path / "test.duckdb"
    c = duckdb.connect(str(db_path))
    from havn.engine.audit import _ensure_sequence, ensure_audit_table

    _ensure_sequence(c)
    ensure_audit_table(c)
    yield c
    c.close()


@pytest.fixture
def project(tmp_path):
    """Create a minimal test project."""
    (tmp_path / "project.yml").write_text(
        """
name: test
database:
  path: warehouse.duckdb
streams:
  test-stream:
    description: "Test"
    steps:
      - transform: [all]
"""
    )
    (tmp_path / "transform" / "bronze").mkdir(parents=True)
    (tmp_path / "ingest").mkdir()
    (tmp_path / "export").mkdir()

    (tmp_path / "transform" / "bronze" / "test.sql").write_text(
        "-- config: materialized=view, schema=bronze\n\n"
        "SELECT 1 AS id, 'hello' AS msg\n"
    )

    # Create a test file for deletion
    (tmp_path / "transform" / "bronze" / "deleteme.sql").write_text(
        "-- config: materialized=view, schema=bronze\n\nSELECT 1 AS x\n"
    )

    c = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    c.execute("CREATE SCHEMA IF NOT EXISTS landing")
    c.execute("CREATE TABLE landing.data AS SELECT 1 AS x")
    from havn.engine.database import ensure_meta_table

    ensure_meta_table(c)
    c.close()

    return tmp_path


@pytest.fixture
def client(project):
    import havn.server.app as server_app

    server_app.PROJECT_DIR = project
    return TestClient(server_app.app)


# ---------------------------------------------------------------------------
# Unit tests for the audit engine
# ---------------------------------------------------------------------------


class TestLogAudit:
    """Test log_audit writes correctly."""

    def test_basic_write(self, conn):
        from havn.engine.audit import log_audit

        log_audit(conn, user="alice", action="query", resource="SELECT 1")

        rows = conn.execute(
            'SELECT "user", action, resource FROM _dp_internal.audit_log'
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "alice"
        assert rows[0][1] == "query"
        assert rows[0][2] == "SELECT 1"

    def test_write_with_all_fields(self, conn):
        from havn.engine.audit import log_audit

        log_audit(
            conn,
            user="bob",
            action="file_edit",
            resource="transform/bronze/test.sql",
            detail="content updated",
            ip_address="192.168.1.1",
        )

        rows = conn.execute(
            'SELECT "user", action, resource, detail, ip_address '
            "FROM _dp_internal.audit_log"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "bob"
        assert rows[0][1] == "file_edit"
        assert rows[0][2] == "transform/bronze/test.sql"
        assert rows[0][3] == "content updated"
        assert rows[0][4] == "192.168.1.1"

    def test_multiple_entries(self, conn):
        from havn.engine.audit import log_audit

        log_audit(conn, user="alice", action="query", resource="SELECT 1")
        log_audit(conn, user="bob", action="login", resource="auth")
        log_audit(conn, user="alice", action="file_edit", resource="test.sql")

        count = conn.execute(
            "SELECT COUNT(*) FROM _dp_internal.audit_log"
        ).fetchone()[0]
        assert count == 3

    def test_timestamp_auto_set(self, conn):
        from havn.engine.audit import log_audit

        log_audit(conn, user="alice", action="query", resource="SELECT 1")

        ts = conn.execute(
            'SELECT "timestamp" FROM _dp_internal.audit_log'
        ).fetchone()[0]
        assert ts is not None

    def test_null_optional_fields(self, conn):
        from havn.engine.audit import log_audit

        log_audit(conn, user="alice", action="query", resource="SELECT 1")

        row = conn.execute(
            "SELECT detail, ip_address FROM _dp_internal.audit_log"
        ).fetchone()
        assert row[0] is None
        assert row[1] is None


# ---------------------------------------------------------------------------
# Tests for query_audit_log
# ---------------------------------------------------------------------------


class TestQueryAuditLog:
    """Test audit entries are queryable with filters."""

    def test_query_all(self, conn):
        from havn.engine.audit import log_audit, query_audit_log

        log_audit(conn, user="alice", action="query", resource="SELECT 1")
        log_audit(conn, user="bob", action="login", resource="auth")

        entries = query_audit_log(conn)
        assert len(entries) == 2
        # Most recent first
        assert entries[0]["user"] == "bob"
        assert entries[1]["user"] == "alice"

    def test_filter_by_user(self, conn):
        from havn.engine.audit import log_audit, query_audit_log

        log_audit(conn, user="alice", action="query", resource="SELECT 1")
        log_audit(conn, user="bob", action="login", resource="auth")
        log_audit(conn, user="alice", action="file_edit", resource="test.sql")

        entries = query_audit_log(conn, user="alice")
        assert len(entries) == 2
        assert all(e["user"] == "alice" for e in entries)

    def test_filter_by_action(self, conn):
        from havn.engine.audit import log_audit, query_audit_log

        log_audit(conn, user="alice", action="query", resource="SELECT 1")
        log_audit(conn, user="bob", action="login", resource="auth")
        log_audit(conn, user="alice", action="query", resource="SELECT 2")

        entries = query_audit_log(conn, action="query")
        assert len(entries) == 2
        assert all(e["action"] == "query" for e in entries)

    def test_filter_by_resource(self, conn):
        from havn.engine.audit import log_audit, query_audit_log

        log_audit(conn, user="alice", action="query", resource="SELECT * FROM users")
        log_audit(conn, user="bob", action="query", resource="SELECT 1")

        entries = query_audit_log(conn, resource="users")
        assert len(entries) == 1
        assert "users" in entries[0]["resource"]

    def test_combined_filters(self, conn):
        from havn.engine.audit import log_audit, query_audit_log

        log_audit(conn, user="alice", action="query", resource="SELECT 1")
        log_audit(conn, user="alice", action="login", resource="auth")
        log_audit(conn, user="bob", action="query", resource="SELECT 2")

        entries = query_audit_log(conn, user="alice", action="query")
        assert len(entries) == 1
        assert entries[0]["user"] == "alice"
        assert entries[0]["action"] == "query"

    def test_limit(self, conn):
        from havn.engine.audit import log_audit, query_audit_log

        for i in range(10):
            log_audit(conn, user="alice", action="query", resource=f"SELECT {i}")

        entries = query_audit_log(conn, limit=3)
        assert len(entries) == 3

    def test_entry_fields(self, conn):
        from havn.engine.audit import log_audit, query_audit_log

        log_audit(
            conn,
            user="alice",
            action="query",
            resource="SELECT 1",
            detail="ad-hoc",
            ip_address="127.0.0.1",
        )

        entries = query_audit_log(conn)
        assert len(entries) == 1
        entry = entries[0]
        assert "id" in entry
        assert entry["user"] == "alice"
        assert entry["action"] == "query"
        assert entry["resource"] == "SELECT 1"
        assert entry["detail"] == "ad-hoc"
        assert entry["ip_address"] == "127.0.0.1"
        assert entry["timestamp"] is not None


# ---------------------------------------------------------------------------
# Integration tests via API
# ---------------------------------------------------------------------------


class TestAuditAPI:
    """Test audit endpoint and audit logging through API calls."""

    def test_query_creates_audit_entry(self, client, project):
        """Running a query should create an audit log entry."""
        resp = client.post("/api/query", json={"sql": "SELECT 42 AS answer"})
        assert resp.status_code == 200

        # Check audit log via API
        resp = client.get("/api/audit")
        assert resp.status_code == 200
        entries = resp.json()
        query_entries = [e for e in entries if e["action"] == "query"]
        assert len(query_entries) >= 1
        assert "SELECT 42" in query_entries[0]["resource"]

    def test_audit_endpoint_filters(self, client, project):
        """Test filtering on the audit endpoint."""
        # Generate some audit entries
        client.post("/api/query", json={"sql": "SELECT 1"})
        client.post("/api/query", json={"sql": "SELECT 2"})

        # Filter by action
        resp = client.get("/api/audit", params={"action": "query"})
        assert resp.status_code == 200
        entries = resp.json()
        assert all(e["action"] == "query" for e in entries)

    def test_audit_endpoint_limit(self, client, project):
        """Test limit parameter on audit endpoint."""
        for i in range(5):
            client.post("/api/query", json={"sql": f"SELECT {i}"})

        resp = client.get("/api/audit", params={"limit": 2})
        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) <= 2

    def test_file_edit_creates_audit_entry(self, client, project):
        """Saving a file should create an audit log entry."""
        resp = client.put(
            "/api/files/transform/bronze/test.sql",
            json={"content": "-- config: materialized=view, schema=bronze\n\nSELECT 2 AS id\n"},
        )
        assert resp.status_code == 200

        resp = client.get("/api/audit", params={"action": "file_edit"})
        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) >= 1
        assert "test.sql" in entries[0]["resource"]

    def test_file_delete_creates_audit_entry(self, client, project):
        """Deleting a file should create an audit log entry."""
        resp = client.delete("/api/files/transform/bronze/deleteme.sql")
        assert resp.status_code == 200

        resp = client.get("/api/audit", params={"action": "file_delete"})
        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) >= 1
        assert "deleteme.sql" in entries[0]["resource"]

    def test_audit_endpoint_empty(self, client, project):
        """Audit endpoint returns empty list when no entries."""
        # First query creates an entry, so filter for a non-existent action
        resp = client.get("/api/audit", params={"action": "config_change"})
        assert resp.status_code == 200
        entries = resp.json()
        assert entries == []
