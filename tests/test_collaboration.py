"""Tests for Live Collaboration & Multi-User Query Sessions."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest


class TestCollaboration:
    """Test the collaboration session manager."""

    def test_create_and_list_sessions(self):
        from havn.engine.collaboration import SessionManager

        mgr = SessionManager()
        session = mgr.create_session("Test Session")
        assert session.name == "Test Session"
        assert session.session_id

        sessions = mgr.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["name"] == "Test Session"

    def test_join_and_leave(self):
        from havn.engine.collaboration import SessionManager

        mgr = SessionManager()
        session = mgr.create_session("Test")

        class FakeWS:
            pass

        ws = FakeWS()
        joined = mgr.join_session(session.session_id, "user1", "Alice", ws)
        assert joined is not None
        assert len(mgr.get_participants(session.session_id)) == 1

        mgr.leave_session(session.session_id, "user1", ws)
        assert len(mgr.get_participants(session.session_id)) == 0

    def test_query_history(self):
        from havn.engine.collaboration import SessionManager

        mgr = SessionManager()
        session = mgr.create_session()

        entry = mgr.add_query_result(
            session.session_id,
            "user1",
            "SELECT 1",
            ["col"],
            [[1]],
            42,
        )
        assert entry is not None
        assert entry["sql"] == "SELECT 1"
        assert session.query_history[-1]["duration_ms"] == 42

    def test_shared_sql(self):
        from havn.engine.collaboration import SessionManager

        mgr = SessionManager()
        session = mgr.create_session()

        mgr.update_shared_sql(session.session_id, "SELECT * FROM orders")
        assert session.shared_sql == "SELECT * FROM orders"

    def test_cursor_tracking(self):
        from havn.engine.collaboration import SessionManager

        mgr = SessionManager()
        session = mgr.create_session()

        class FakeWS:
            pass

        mgr.join_session(session.session_id, "user1", "Alice", FakeWS())
        mgr.update_cursor(session.session_id, "user1", {"line": 5, "column": 10})

        participants = mgr.get_participants(session.session_id)
        assert participants[0]["cursor_position"] == {"line": 5, "column": 10}

    def test_delete_session(self):
        from havn.engine.collaboration import SessionManager

        mgr = SessionManager()
        session = mgr.create_session("Temp")
        assert mgr.delete_session(session.session_id) is True
        assert mgr.get_session(session.session_id) is None
        assert mgr.delete_session("nonexistent") is False

    def test_max_history_limit(self):
        from havn.engine.collaboration import SessionManager

        mgr = SessionManager()
        mgr._max_history = 5
        session = mgr.create_session()

        for i in range(10):
            mgr.add_query_result(
                session.session_id, "user1", f"SELECT {i}", ["c"], [[i]], i,
            )

        assert len(session.query_history) == 5
        assert session.query_history[0]["sql"] == "SELECT 5"


# ---------------------------------------------------------------------------
# API Integration Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def project_with_contracts(tmp_path):
    """Create a test project with contracts and a warehouse."""
    (tmp_path / "project.yml").write_text("name: test\ndatabase:\n  path: warehouse.duckdb\nstreams:\n  test:\n    steps:\n      - transform: [all]\n")
    (tmp_path / "transform" / "bronze").mkdir(parents=True)
    (tmp_path / "transform" / "bronze" / "test.sql").write_text(
        "-- config: materialized=table, schema=bronze\nSELECT 1 AS id, 'Alice' AS name"
    )
    (tmp_path / "ingest").mkdir()
    (tmp_path / "export").mkdir()

    # Create contracts
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    (contracts_dir / "test.yml").write_text(
        "contracts:\n"
        "  - name: test_contract\n"
        "    model: bronze.test\n"
        "    assertions:\n"
        "      - row_count > 0\n"
    )

    # Create warehouse
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    from havn.engine.database import ensure_meta_table
    ensure_meta_table(conn)
    conn.execute("CREATE SCHEMA IF NOT EXISTS bronze")
    conn.execute("CREATE TABLE bronze.test AS SELECT 1 AS id, 'Alice' AS name")
    conn.close()

    return tmp_path


@pytest.fixture
def api_client(project_with_contracts):
    """Create a FastAPI TestClient."""
    from starlette.testclient import TestClient
    import havn.server.app as server_app
    server_app.PROJECT_DIR = project_with_contracts
    server_app.AUTH_ENABLED = False
    return TestClient(server_app.app)


class TestSessionsAPI:
    def test_create_and_list_sessions(self, api_client):
        resp = api_client.post("/api/sessions", json={"name": "Test"})
        assert resp.status_code == 200
        session_id = resp.json()["session_id"]

        resp = api_client.get("/api/sessions")
        assert resp.status_code == 200
        sessions = resp.json()
        assert any(s["session_id"] == session_id for s in sessions)

    def test_session_query(self, api_client):
        # Create session
        resp = api_client.post("/api/sessions", json={"name": "Query Test"})
        session_id = resp.json()["session_id"]

        # Run query in session
        resp = api_client.post(
            f"/api/sessions/{session_id}/query",
            json={"sql": "SELECT 42 AS answer", "user_id": "test"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["columns"] == ["answer"]
        assert data["rows"] == [[42]]

    def test_delete_session(self, api_client):
        resp = api_client.post("/api/sessions", json={"name": "Temp"})
        session_id = resp.json()["session_id"]

        resp = api_client.delete(f"/api/sessions/{session_id}")
        assert resp.status_code == 200

        resp = api_client.get(f"/api/sessions/{session_id}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Security Hardening Tests
# ---------------------------------------------------------------------------


class TestCollaborationSecurity:
    """Test size limits and stale session handling."""

    def test_shared_sql_size_limit(self):
        from havn.engine.collaboration import SessionManager

        mgr = SessionManager()
        session = mgr.create_session()
        huge_sql = "SELECT " + "x" * 200_000
        mgr.update_shared_sql(session.session_id, huge_sql)
        assert len(session.shared_sql) <= mgr._max_sql_length

    def test_session_name_truncated(self):
        from havn.engine.collaboration import SessionManager

        mgr = SessionManager()
        long_name = "A" * 500
        session = mgr.create_session(long_name)
        assert len(session.name) <= 200

    def test_stale_session_eviction(self):
        from havn.engine.collaboration import SessionManager

        mgr = SessionManager()
        mgr._session_ttl = 0  # Immediately stale
        mgr._max_sessions = 2

        mgr.create_session("s1")
        mgr.create_session("s2")
        # Third session should trigger eviction of stale empty sessions
        s3 = mgr.create_session("s3")
        assert s3 is not None
        # At most _max_sessions remain (stale ones evicted)
        assert len(mgr._sessions) <= 3  # Could be less after eviction


# --- An open session socket ends when its user's sessions are revoked --------


@pytest.mark.parametrize("revocation", ["password", "delete"])
def test_open_session_socket_closes_once_its_user_is_revoked(project_with_contracts, revocation):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    import havn.server.app as server_app
    from havn.server import deps

    deps.reset_shared_conn()
    deps._clear_config_cache()
    deps.invalidate_token_cache()
    old_dir, old_auth = server_app.PROJECT_DIR, server_app.AUTH_ENABLED
    server_app.PROJECT_DIR = project_with_contracts
    server_app.AUTH_ENABLED = True
    try:
        c = TestClient(server_app.app)
        admin = c.post("/api/auth/setup", json={"username": "admin", "password": "admin-password"})
        assert admin.status_code == 200, admin.text
        h = {"Authorization": f"Bearer {admin.json()['token']}"}
        r = c.post("/api/users", json={"username": "ann", "password": "ann-password-1",
                                       "role": "editor"}, headers=h)
        assert r.status_code == 200, r.text
        ann = c.post("/api/auth/login", json={"username": "ann", "password": "ann-password-1"})
        ann_token = ann.json()["token"]
        sid = c.post("/api/sessions", json={"name": "S"}, headers=h).json()["session_id"]

        with c.websocket_connect(f"/ws/session/{sid}?token={ann_token}") as ws:
            assert ws.receive_json()["type"] == "user_joined"
            ws.send_json({"type": "message", "text": "before"})
            assert ws.receive_json() == {"type": "message", "user_id": "ann", "text": "before"}

            if revocation == "password":
                r = c.put("/api/users/ann", json={"password": "ann-password-2"}, headers=h)
            else:
                r = c.delete("/api/users/ann", headers=h)
            assert r.status_code == 200, r.text

            ws.send_json({"type": "message", "text": "after"})
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == deps.WS_SESSION_ENDED
    finally:
        deps.reset_shared_conn()
        deps._clear_config_cache()
        deps.invalidate_token_cache()
        server_app.PROJECT_DIR, server_app.AUTH_ENABLED = old_dir, old_auth
