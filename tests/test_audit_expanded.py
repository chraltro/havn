"""Tests for expanded audit logging — new action types and wiring."""

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
    """Create a minimal test project with auth support."""
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

    # Create .env for secrets tests
    (tmp_path / ".env").write_text("EXISTING_KEY=existing_value\n")

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
    from havn.server.deps import reset_shared_conn

    reset_shared_conn()
    server_app.PROJECT_DIR = project
    yield TestClient(server_app.app)
    reset_shared_conn()


@pytest.fixture
def auth_client(project):
    """Client with auth enabled and an admin user created."""
    import havn.server.app as server_app
    from havn.server.deps import reset_shared_conn

    reset_shared_conn()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = True

    tc = TestClient(server_app.app)

    # Create admin user via setup endpoint
    resp = tc.post(
        "/api/auth/setup",
        json={
            "username": "admin",
            "password": "adminpass",
            "role": "admin",
            "display_name": "Admin User",
        },
    )
    token = resp.json()["token"]
    tc.headers["Authorization"] = f"Bearer {token}"

    yield tc
    server_app.AUTH_ENABLED = False
    reset_shared_conn()


# ---------------------------------------------------------------------------
# Unit tests: new action types can be logged
# ---------------------------------------------------------------------------


class TestNewActionTypes:
    """Test that all new audit action types can be written and queried."""

    NEW_ACTIONS = [
        "auth_failed",
        "permission_denied",
        "connector_sync",
        "connector_setup",
        "masking_policy_create",
        "masking_policy_update",
        "masking_policy_delete",
        "user_create",
        "user_update",
        "user_delete",
        "token_revoke",
        "snapshot_restore",
        "secret_change",
    ]

    @pytest.mark.parametrize("action", NEW_ACTIONS)
    def test_log_new_action(self, conn, action):
        """Each new action type should be loggable without warnings."""
        from havn.engine.audit import VALID_ACTIONS, log_audit

        assert action in VALID_ACTIONS

        log_audit(
            conn,
            user="testuser",
            action=action,
            resource="test-resource",
            detail=f"testing {action}",
        )

        rows = conn.execute(
            "SELECT action, detail FROM _dp_internal.audit_log WHERE action = ?",
            [action],
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == action
        assert rows[0][1] == f"testing {action}"

    def test_query_by_new_action(self, conn):
        """The query_audit_log function works with new action types."""
        from havn.engine.audit import log_audit, query_audit_log

        log_audit(conn, user="alice", action="auth_failed", resource="login")
        log_audit(conn, user="bob", action="permission_denied", resource="/api/users")
        log_audit(conn, user="alice", action="query", resource="SELECT 1")

        entries = query_audit_log(conn, action="auth_failed")
        assert len(entries) == 1
        assert entries[0]["user"] == "alice"
        assert entries[0]["action"] == "auth_failed"

        entries = query_audit_log(conn, action="permission_denied")
        assert len(entries) == 1
        assert entries[0]["user"] == "bob"


# ---------------------------------------------------------------------------
# Integration tests: auth_failed logged on bad login
# ---------------------------------------------------------------------------


class TestAuthFailedAudit:
    """Test that auth_failed is logged on failed login attempts."""

    def test_failed_login_creates_auth_failed_entry(self, auth_client):
        """A bad login should create an auth_failed audit entry."""
        # Attempt login with wrong password (no auth header needed for login)
        resp = auth_client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrongpassword"},
        )
        assert resp.status_code == 401

        # Check audit log
        resp = auth_client.get("/api/audit", params={"action": "auth_failed"})
        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) >= 1
        assert entries[0]["action"] == "auth_failed"
        assert entries[0]["user"] == "admin"
        assert "Invalid credentials" in entries[0]["detail"]

    def test_successful_login_does_not_create_auth_failed(self, auth_client):
        """A successful login should create 'login' not 'auth_failed'."""
        resp = auth_client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "adminpass"},
        )
        assert resp.status_code == 200

        resp = auth_client.get("/api/audit", params={"action": "login"})
        assert resp.status_code == 200
        entries = resp.json()
        login_entries = [e for e in entries if e["detail"] == "success"]
        assert len(login_entries) >= 1


# ---------------------------------------------------------------------------
# Integration tests: permission_denied logged
# ---------------------------------------------------------------------------


class TestPermissionDeniedAudit:
    """Test that permission_denied is logged when access is denied."""

    def test_viewer_denied_write_creates_audit_entry(self, project):
        """A viewer attempting a write operation should be logged."""
        import havn.server.app as server_app
        from havn.server.deps import reset_shared_conn

        reset_shared_conn()
        server_app.PROJECT_DIR = project
        server_app.AUTH_ENABLED = True

        tc = TestClient(server_app.app)

        # Create admin
        resp = tc.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "adminpass"},
        )
        admin_token = resp.json()["token"]

        # Create viewer user
        tc.headers["Authorization"] = f"Bearer {admin_token}"
        tc.post(
            "/api/users",
            json={
                "username": "viewer_user",
                "password": "viewerpass",
                "role": "viewer",
            },
        )

        # Login as viewer
        resp = tc.post(
            "/api/auth/login",
            json={"username": "viewer_user", "password": "viewerpass"},
        )
        viewer_token = resp.json()["token"]

        # Try a write operation as viewer (should be denied)
        tc.headers["Authorization"] = f"Bearer {viewer_token}"
        resp = tc.put(
            "/api/files/transform/bronze/test.sql",
            json={"content": "SELECT 1"},
        )
        assert resp.status_code == 403

        # Check audit log as admin
        tc.headers["Authorization"] = f"Bearer {admin_token}"
        resp = tc.get("/api/audit", params={"action": "permission_denied"})
        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) >= 1
        assert entries[0]["user"] == "viewer_user"
        assert "write" in entries[0]["detail"]

        server_app.AUTH_ENABLED = False
        reset_shared_conn()


# ---------------------------------------------------------------------------
# Integration tests: masking policy changes are audited
# ---------------------------------------------------------------------------


class TestMaskingPolicyAudit:
    """Test masking policy create/update/delete are audited."""

    def test_masking_policy_create_audited(self, client):
        """Creating a masking policy should create a masking_policy_create audit entry."""
        resp = client.post(
            "/api/masking/policies",
            json={
                "schema_name": "bronze",
                "table_name": "customers",
                "column_name": "email",
                "method": "redact",
            },
        )
        assert resp.status_code == 200

        resp = client.get("/api/audit", params={"action": "masking_policy_create"})
        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) >= 1
        assert "bronze.customers.email" in entries[0]["resource"]
        assert "method=redact" in entries[0]["detail"]

    def test_masking_policy_delete_audited(self, client):
        """Deleting a masking policy should create a masking_policy_delete entry."""
        # Create a policy first
        resp = client.post(
            "/api/masking/policies",
            json={
                "schema_name": "bronze",
                "table_name": "customers",
                "column_name": "phone",
                "method": "hash",
            },
        )
        assert resp.status_code == 200
        policy_id = resp.json().get("id")

        # Delete it
        resp = client.delete(f"/api/masking/policies/{policy_id}")
        assert resp.status_code == 200

        resp = client.get("/api/audit", params={"action": "masking_policy_delete"})
        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) >= 1
        assert "policy deleted" in entries[0]["detail"]


# ---------------------------------------------------------------------------
# Integration tests: user management audited
# ---------------------------------------------------------------------------


class TestUserManagementAudit:
    """Test user create/update/delete are audited when auth is enabled."""

    def test_user_create_audited(self, auth_client):
        """Creating a user should log a user_create audit entry."""
        resp = auth_client.post(
            "/api/users",
            json={
                "username": "newuser",
                "password": "newuserpass",
                "role": "editor",
            },
        )
        assert resp.status_code == 200

        resp = auth_client.get("/api/audit", params={"action": "user_create"})
        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) >= 1
        assert entries[0]["resource"] == "newuser"
        assert "role=editor" in entries[0]["detail"]

    def test_user_update_audited(self, auth_client):
        """Updating a user should log a user_update audit entry."""
        # Create user first
        auth_client.post(
            "/api/users",
            json={
                "username": "updateme",
                "password": "updatemepass",
                "role": "viewer",
            },
        )

        # Update user role
        resp = auth_client.put(
            "/api/users/updateme",
            json={"role": "editor"},
        )
        assert resp.status_code == 200

        resp = auth_client.get("/api/audit", params={"action": "user_update"})
        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) >= 1
        assert entries[0]["resource"] == "updateme"
        assert "role=editor" in entries[0]["detail"]

    def test_user_delete_audited(self, auth_client):
        """Deleting a user should log a user_delete audit entry."""
        # Create user first
        auth_client.post(
            "/api/users",
            json={
                "username": "deleteme",
                "password": "deletemepass",
                "role": "viewer",
            },
        )

        resp = auth_client.delete("/api/users/deleteme")
        assert resp.status_code == 200

        resp = auth_client.get("/api/audit", params={"action": "user_delete"})
        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) >= 1
        assert entries[0]["resource"] == "deleteme"


# ---------------------------------------------------------------------------
# Integration tests: secret changes audited
# ---------------------------------------------------------------------------


class TestSecretChangeAudit:
    """Test secret set/delete are audited."""

    def test_secret_set_audited(self, auth_client):
        """Setting a secret should log a secret_change entry."""
        resp = auth_client.post(
            "/api/secrets",
            json={"key": "MY_API_KEY", "value": "supersecretvalue"},
        )
        assert resp.status_code == 200

        resp = auth_client.get("/api/audit", params={"action": "secret_change"})
        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) >= 1
        assert entries[0]["resource"] == "MY_API_KEY"
        assert "set/updated" in entries[0]["detail"]
        # The value must NEVER appear in the audit log
        assert "supersecretvalue" not in str(entries)

    def test_secret_delete_audited(self, auth_client):
        """Deleting a secret should log a secret_change entry."""
        # Create a secret first
        auth_client.post(
            "/api/secrets",
            json={"key": "DELETE_ME_KEY", "value": "tempvalue"},
        )

        resp = auth_client.delete("/api/secrets/DELETE_ME_KEY")
        assert resp.status_code == 200

        resp = auth_client.get("/api/audit", params={"action": "secret_change"})
        assert resp.status_code == 200
        entries = resp.json()
        delete_entries = [e for e in entries if "deleted" in (e["detail"] or "")]
        assert len(delete_entries) >= 1
        assert delete_entries[0]["resource"] == "DELETE_ME_KEY"


# ---------------------------------------------------------------------------
# Integration tests: snapshot restore audited
# ---------------------------------------------------------------------------


class TestSnapshotRestoreAudit:
    """Test snapshot restore is audited (unit-level, since full rewind
    requires snapshot files on disk)."""

    def test_snapshot_restore_action_loggable(self, conn):
        """The snapshot_restore action can be logged and queried."""
        from havn.engine.audit import log_audit, query_audit_log

        log_audit(
            conn,
            user="admin",
            action="snapshot_restore",
            resource="gold.revenue",
            detail="run_id=abc-123, cascade=True",
        )

        entries = query_audit_log(conn, action="snapshot_restore")
        assert len(entries) == 1
        assert entries[0]["resource"] == "gold.revenue"
        assert "abc-123" in entries[0]["detail"]


# ---------------------------------------------------------------------------
# Integration tests: connector actions audited
# ---------------------------------------------------------------------------


class TestConnectorAudit:
    """Test connector setup and sync actions are loggable."""

    def test_connector_setup_action_loggable(self, conn):
        """The connector_setup action can be logged and queried."""
        from havn.engine.audit import log_audit, query_audit_log

        log_audit(
            conn,
            user="admin",
            action="connector_setup",
            resource="my_postgres",
            detail="type=postgres",
        )

        entries = query_audit_log(conn, action="connector_setup")
        assert len(entries) == 1
        assert entries[0]["resource"] == "my_postgres"

    def test_connector_sync_action_loggable(self, conn):
        """The connector_sync action can be logged and queried."""
        from havn.engine.audit import log_audit, query_audit_log

        log_audit(
            conn,
            user="admin",
            action="connector_sync",
            resource="my_postgres",
            detail="status=success",
        )

        entries = query_audit_log(conn, action="connector_sync")
        assert len(entries) == 1
        assert entries[0]["resource"] == "my_postgres"
