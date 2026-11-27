"""Tests for authentication and user management."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import duckdb
import pytest
from fastapi.testclient import TestClient

import havn.engine.auth as auth

from havn.engine.auth import (
    authenticate,
    create_user,
    delete_user,
    ensure_auth_tables,
    has_any_users,
    has_permission,
    list_users,
    update_user,
    validate_token,
)


def _get_conn():
    conn = duckdb.connect(":memory:")
    ensure_auth_tables(conn)
    return conn


def test_create_and_authenticate():
    """Create a user and authenticate."""
    conn = _get_conn()
    create_user(conn, "alice", "password123", "admin")
    token = authenticate(conn, "alice", "password123")
    assert token is not None
    assert len(token) > 20

    # Wrong password
    bad = authenticate(conn, "alice", "wrong")
    assert bad is None

    # Non-existent user
    bad = authenticate(conn, "bob", "password123")
    assert bad is None
    conn.close()


def test_validate_token():
    """Tokens validate to user info."""
    conn = _get_conn()
    create_user(conn, "alice", "pass", "editor", "Alice Smith")
    token = authenticate(conn, "alice", "pass")
    user = validate_token(conn, token)
    assert user["username"] == "alice"
    assert user["role"] == "editor"
    assert user["display_name"] == "Alice Smith"

    # Invalid token
    assert validate_token(conn, "bogus") is None
    conn.close()


def test_list_and_delete_users():
    """List users and delete."""
    conn = _get_conn()
    create_user(conn, "alice", "pass1", "admin")
    create_user(conn, "bob", "pass2", "viewer")
    users = list_users(conn)
    assert len(users) == 2

    assert delete_user(conn, "bob") is True
    assert delete_user(conn, "bob") is False
    assert len(list_users(conn)) == 1
    conn.close()


def test_update_user():
    """Update user role and password."""
    conn = _get_conn()
    create_user(conn, "alice", "pass", "viewer")

    update_user(conn, "alice", role="admin")
    users = list_users(conn)
    assert users[0]["role"] == "admin"

    update_user(conn, "alice", password="newpass")
    token = authenticate(conn, "alice", "newpass")
    assert token is not None

    # Old password fails
    assert authenticate(conn, "alice", "pass") is None
    conn.close()


def test_has_any_users():
    """has_any_users returns False for empty, True after creation."""
    conn = _get_conn()
    assert has_any_users(conn) is False
    create_user(conn, "alice", "pass")
    assert has_any_users(conn) is True
    conn.close()


def test_permissions():
    """Role permissions work correctly."""
    assert has_permission("admin", "read") is True
    assert has_permission("admin", "manage_users") is True
    assert has_permission("editor", "execute") is True
    assert has_permission("editor", "manage_users") is False
    assert has_permission("viewer", "read") is True
    assert has_permission("viewer", "write") is False
    assert has_permission("viewer", "execute") is False


# --- Password changes end sessions ------------------------------------------


def test_password_change_revokes_existing_tokens():
    conn = _get_conn()
    create_user(conn, "ann", "first-password", "editor")
    old = authenticate(conn, "ann", "first-password")
    assert validate_token(conn, old) is not None

    update_user(conn, "ann", role="viewer")
    assert validate_token(conn, old) is not None, "a role change alone keeps sessions"

    update_user(conn, "ann", password="second-password")
    assert validate_token(conn, old) is None
    assert authenticate(conn, "ann", "first-password") is None
    new = authenticate(conn, "ann", "second-password")
    assert validate_token(conn, new)["username"] == "ann"
    conn.close()


def test_password_change_can_keep_the_callers_token():
    conn = _get_conn()
    create_user(conn, "ann", "first-password", "admin")
    create_user(conn, "bob", "bob-password", "viewer")
    here = authenticate(conn, "ann", "first-password")
    elsewhere = authenticate(conn, "ann", "first-password")
    bob = authenticate(conn, "bob", "bob-password")

    update_user(conn, "ann", password="second-password", keep_token=here)
    assert validate_token(conn, here) is not None
    assert validate_token(conn, elsewhere) is None
    assert validate_token(conn, bob) is not None, "other users are untouched"
    conn.close()


# --- Constant-time comparison -------------------------------------------------


def test_verify_password_compares_in_constant_time(monkeypatch):
    calls = []
    real = auth.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    h, salt = auth._hash_password("secret-pass")
    assert auth._verify_password("secret-pass", h, salt, auth.PBKDF2_ITERATIONS)
    assert not auth._verify_password("wrong-pass", h, salt, auth.PBKDF2_ITERATIONS)
    assert len(calls) == 2


# --- Iteration count -----------------------------------------------------------


def _stored_iterations(conn, username):
    return conn.execute(
        "SELECT password_iterations FROM _havn.users WHERE username = ?", [username]
    ).fetchone()[0]


def _insert_raw_user(conn, username, password, iterations, stored_iterations):
    """A row as an older havn (or another count) would have written it."""
    h, salt = auth._hash_password(password, iterations=iterations)
    conn.execute(
        "INSERT INTO _havn.users (username, password_hash, password_salt, "
        "password_iterations, role) VALUES (?, ?, ?, ?, 'viewer')",
        [username, h, salt, stored_iterations],
    )


def test_new_and_changed_passwords_store_the_current_count():
    conn = _get_conn()
    create_user(conn, "ann", "first-password")
    assert _stored_iterations(conn, "ann") == auth.PBKDF2_ITERATIONS
    conn.execute("UPDATE _havn.users SET password_iterations = NULL")
    update_user(conn, "ann", password="second-password")
    assert _stored_iterations(conn, "ann") == auth.PBKDF2_ITERATIONS
    conn.close()


@pytest.mark.real_pbkdf2
def test_legacy_row_logs_in_and_is_upgraded_to_the_production_default():
    """A row from before the count was stored (NULL = 100,000) still signs in,
    and is re-hashed at the real default, 600,000."""
    assert auth.PBKDF2_ITERATIONS == 600_000
    assert auth.LEGACY_PBKDF2_ITERATIONS == 100_000
    conn = _get_conn()
    _insert_raw_user(conn, "old", "old-password", auth.LEGACY_PBKDF2_ITERATIONS, None)
    old_hash = conn.execute("SELECT password_hash FROM _havn.users").fetchone()[0]

    assert authenticate(conn, "old", "old-password") is not None
    assert _stored_iterations(conn, "old") == 600_000
    assert conn.execute("SELECT password_hash FROM _havn.users").fetchone()[0] != old_hash
    assert authenticate(conn, "old", "old-password") is not None
    conn.close()


def test_failed_login_does_not_upgrade_a_legacy_row(monkeypatch):
    monkeypatch.setattr(auth, "LEGACY_PBKDF2_ITERATIONS", 500)
    conn = _get_conn()
    _insert_raw_user(conn, "old", "old-password", 500, None)
    assert authenticate(conn, "old", "wrong-password") is None
    assert _stored_iterations(conn, "old") is None
    conn.close()


def test_higher_stored_count_is_not_downgraded():
    conn = _get_conn()
    higher = auth.PBKDF2_ITERATIONS * 3
    _insert_raw_user(conn, "strong", "strong-password", higher, higher)
    before = conn.execute("SELECT password_hash FROM _havn.users").fetchone()[0]
    assert authenticate(conn, "strong", "strong-password") is not None
    assert _stored_iterations(conn, "strong") == higher
    assert conn.execute("SELECT password_hash FROM _havn.users").fetchone()[0] == before
    conn.close()


def test_unknown_username_and_wrong_password_hash_the_same_amount(monkeypatch):
    monkeypatch.setattr(auth, "LEGACY_PBKDF2_ITERATIONS", 300)
    conn = _get_conn()
    create_user(conn, "ann", "ann-password")
    _insert_raw_user(conn, "old", "old-password", 300, None)

    requested: list[int] = []
    real = auth._pbkdf2

    def counting(password, salt, iterations):
        requested.append(iterations)
        return real(password, salt, iterations)

    monkeypatch.setattr(auth, "_pbkdf2", counting)

    def cost(username, password):
        requested.clear()
        assert authenticate(conn, username, password) is None
        return sum(requested)

    target = auth.PBKDF2_ITERATIONS
    assert cost("nobody", "whatever-pass") == target
    assert cost("ann", "wrong-password") == target
    assert cost("old", "wrong-password") == target, "legacy rows are padded up"
    conn.close()


def test_users_table_gains_the_new_columns():
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA _havn")
    conn.execute(
        "CREATE TABLE _havn.users (username VARCHAR PRIMARY KEY, password_hash VARCHAR NOT NULL,"
        " password_salt VARCHAR NOT NULL, role VARCHAR NOT NULL DEFAULT 'viewer',"
        " display_name VARCHAR, created_at TIMESTAMP DEFAULT current_timestamp,"
        " last_login TIMESTAMP)"
    )
    ensure_auth_tables(conn)
    cols = {r[0] for r in conn.execute(
        "SELECT column_name FROM duckdb_columns() WHERE schema_name = '_havn' "
        "AND table_name = 'users'"
    ).fetchall()}
    assert {"attributes", "password_iterations"} <= cols
    conn.close()


# --- API -----------------------------------------------------------------------


@pytest.fixture
def api(tmp_path):
    """Auth-enabled server with one admin (password ``admin-password``)."""
    import havn.server.app as server_app
    from havn.server import deps

    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: warehouse.duckdb\n")
    (tmp_path / "transform" / "bronze").mkdir(parents=True)
    duckdb.connect(str(tmp_path / "warehouse.duckdb")).close()
    deps.reset_shared_conn()
    deps._clear_config_cache()
    deps.invalidate_token_cache()
    old_dir, old_auth = server_app.PROJECT_DIR, server_app.AUTH_ENABLED
    server_app.PROJECT_DIR = tmp_path
    server_app.AUTH_ENABLED = True
    r = TestClient(server_app.app).post(
        "/api/auth/setup", json={"username": "admin", "password": "admin-password"}
    )
    assert r.status_code == 200, r.text
    with deps._login_attempts_lock:
        deps._login_attempts.clear()
    yield SimpleNamespace(app=server_app.app, token=r.json()["token"], dir=tmp_path)
    deps.reset_shared_conn()
    deps._clear_config_cache()
    deps.invalidate_token_cache()
    server_app.PROJECT_DIR, server_app.AUTH_ENABLED = old_dir, old_auth


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _login(app, username, password, ip="10.0.0.1"):
    return TestClient(app, client=(ip, 50000)).post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _server_sql(sql, params=None):
    """Run SQL on the server's own write connection; returns all rows."""
    from havn.server import deps

    cur = deps._get_write_queue().cursor()
    try:
        cur.execute(sql, params or [])
        try:
            return cur.fetchall()
        except duckdb.Error:
            return []
    finally:
        cur.close()


def test_api_password_change_rejects_the_old_token(api):
    c = TestClient(api.app)
    r = c.post("/api/users", json={"username": "ann", "password": "ann-password-1"},
               headers=_bearer(api.token))
    assert r.status_code == 200, r.text
    ann = _login(api.app, "ann", "ann-password-1").json()["token"]
    assert c.get("/api/auth/me", headers=_bearer(ann)).status_code == 200  # now cached

    r = c.put("/api/users/ann", json={"password": "ann-password-2"}, headers=_bearer(api.token))
    assert r.status_code == 200, r.text
    assert c.get("/api/auth/me", headers=_bearer(ann)).status_code == 401
    assert _login(api.app, "ann", "ann-password-2").status_code == 200


def test_api_admin_changing_own_password_keeps_this_session_only(api):
    c = TestClient(api.app)
    other = _login(api.app, "admin", "admin-password").json()["token"]
    assert c.get("/api/auth/me", headers=_bearer(other)).status_code == 200

    r = c.put("/api/users/admin", json={"password": "new-admin-password"},
              headers=_bearer(api.token))
    assert r.status_code == 200, r.text
    assert c.get("/api/auth/me", headers=_bearer(api.token)).status_code == 200
    assert c.get("/api/auth/me", headers=_bearer(other)).status_code == 401
    assert _login(api.app, "admin", "admin-password").status_code == 401
    assert _login(api.app, "admin", "new-admin-password").status_code == 200


def test_minimum_password_length_on_create_and_update(api):
    c = TestClient(api.app)
    h = _bearer(api.token)
    assert c.post("/api/users", json={"username": "ann", "password": "7chars!"},
                  headers=h).status_code == 422
    assert c.post("/api/users", json={"username": "ann", "password": "8chars!!"},
                  headers=h).status_code == 200
    assert c.put("/api/users/ann", json={"password": "short"}, headers=h).status_code == 422
    assert c.put("/api/users/ann", json={"password": "longer-pw"}, headers=h).status_code == 200


def test_setup_rejects_a_short_password(api):
    _server_sql("DELETE FROM _havn.users")
    r = TestClient(api.app).post("/api/auth/setup", json={"username": "a", "password": "1234567"})
    assert r.status_code == 422


def test_login_still_accepts_a_short_legacy_password(api):
    from havn.server import deps

    cur = deps._get_write_queue().cursor()
    try:
        create_user(cur, "shorty", "abcd")  # allowed under the old 4-character rule
    finally:
        cur.close()
    assert _login(api.app, "shorty", "abcd").status_code == 200


def test_cli_users_create_enforces_minimum_length(api):
    from typer.testing import CliRunner

    from havn.cli import app as cli_app
    from havn.server import deps

    deps.reset_shared_conn()  # release the warehouse for the CLI's own connection
    runner = CliRunner()
    r = runner.invoke(cli_app, ["users", "create", "cli1", "1234567", "-p", str(api.dir)])
    assert r.exit_code == 1
    assert "at least 8" in r.output
    r = runner.invoke(cli_app, ["users", "create", "cli1", "12345678", "-p", str(api.dir)])
    assert r.exit_code == 0, r.output


# --- Rate limits ---------------------------------------------------------------


def test_per_username_limit_trips_across_addresses_and_ignores_existence(api):
    from havn.server import deps

    limit = deps._LOGIN_USER_MAX
    results = {}
    for n, username in enumerate(("admin", "ghost")):
        statuses = [
            _login(api.app, username, "wrong-password", ip=f"10.1.{n}.{i}").status_code
            for i in range(limit)
        ]
        assert statuses == [401] * limit
        # A fresh address and a case variant of the same name: still limited.
        r = _login(api.app, username.upper(), "wrong-password", ip=f"10.2.{n}.1")
        results[username] = (r.status_code, r.json())
    assert results["admin"] == results["ghost"]
    assert results["admin"][0] == 429
    # The right password is refused too while the bucket is full.
    assert _login(api.app, "admin", "admin-password", ip="10.3.0.1").status_code == 429


def test_successful_logins_do_not_use_up_the_username_limit(api):
    from havn.server import deps

    for i in range(deps._LOGIN_USER_MAX + 3):
        assert _login(api.app, "admin", "admin-password", ip=f"10.4.0.{i}").status_code == 200


def test_setup_is_rate_limited(api):
    c = TestClient(api.app, client=("10.5.0.1", 1))
    codes = [
        c.post("/api/auth/setup", json={"username": f"x{i}", "password": "long-enough"}).status_code
        for i in range(6)
    ]
    assert codes == [400] * 5 + [429]


def test_concurrent_setup_creates_one_admin(api, monkeypatch):
    _server_sql("DELETE FROM _havn.users")
    real = auth.has_any_users

    def slow(conn):
        found = real(conn)
        time.sleep(0.3)  # widen the gap between the check and the insert
        return found

    monkeypatch.setattr(auth, "has_any_users", slow)
    codes = []

    def go(i):
        r = TestClient(api.app, client=(f"10.6.0.{i}", 1)).post(
            "/api/auth/setup", json={"username": f"first{i}", "password": "long-enough"}
        )
        codes.append(r.status_code)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [200, 400]
    assert _server_sql("SELECT COUNT(*) FROM _havn.users")[0][0] == 1


# --- WebSocket auth uses the token cache --------------------------------------


def test_websocket_auth_is_served_from_the_token_cache(api, monkeypatch):
    from havn.server import deps

    c = TestClient(api.app)
    assert c.get("/api/auth/me", headers=_bearer(api.token)).status_code == 200  # primes cache

    calls = []
    real = auth.validate_token

    def counting(conn, token):
        calls.append(token)
        return real(conn, token)

    monkeypatch.setattr(auth, "validate_token", counting)
    ws = SimpleNamespace(query_params={"token": api.token})
    assert deps._authenticate_websocket(ws)["username"] == "admin"
    assert calls == []

    deps.invalidate_token_cache()
    assert deps._authenticate_websocket(ws)["username"] == "admin"
    assert calls == [api.token]
    assert deps._authenticate_websocket(SimpleNamespace(query_params={"token": "bogus"})) is None
