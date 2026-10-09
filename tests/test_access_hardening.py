"""Access-control hardening: database files over the file API, masking policy
management, and whole-row reads of masked tables.

All of these run with auth enabled and three real users (admin, editor,
viewer) against a real warehouse with a masked ``landing.people.ssn``.
"""

from __future__ import annotations

import duckdb
import pytest
from fastapi.testclient import TestClient

SSN = "123-45-6789"


@pytest.fixture
def env(tmp_path):
    import havn.server.app as server_app
    from havn.server import deps

    (tmp_path / "project.yml").write_text(
        "name: t\n"
        "database:\n  path: warehouse.duckdb\n"
        "environments:\n"
        "  dev:\n    database:\n      path: warehouse.duckdb\n"
        "  prod:\n    database:\n      path: data/prod_store.bin\n"
    )
    (tmp_path / "transform" / "bronze").mkdir(parents=True)
    (tmp_path / "transform" / "bronze" / "m.sql").write_text("SELECT 1 AS id\n")
    c = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    c.execute("CREATE SCHEMA landing")
    c.execute(
        "CREATE TABLE landing.people AS "
        f"SELECT 1 AS id, 'alice@example.com' AS email, '{SSN}' AS ssn"
    )
    c.close()

    deps.reset_shared_conn()
    deps._clear_config_cache()
    deps.invalidate_token_cache()
    with deps._login_attempts_lock:
        deps._login_attempts.clear()
    old_dir, old_auth = server_app.PROJECT_DIR, server_app.AUTH_ENABLED
    server_app.PROJECT_DIR = tmp_path
    server_app.AUTH_ENABLED = True

    anon = TestClient(server_app.app)
    tok = anon.post(
        "/api/auth/setup",
        json={"username": "admin", "password": "adminpass1", "role": "admin"},
    ).json()["token"]
    clients = {"admin": TestClient(server_app.app)}
    clients["admin"].headers["Authorization"] = f"Bearer {tok}"
    for role in ("editor", "viewer"):
        r = clients["admin"].post(
            "/api/users",
            json={"username": role, "password": role + "pass1", "role": role},
        )
        assert r.status_code == 200, r.text
        t = anon.post(
            "/api/auth/login", json={"username": role, "password": role + "pass1"}
        ).json()["token"]
        clients[role] = TestClient(server_app.app)
        clients[role].headers["Authorization"] = f"Bearer {t}"

    r = clients["admin"].post(
        "/api/masking/policies",
        json={"schema_name": "landing", "table_name": "people",
              "column_name": "ssn", "method": "redact"},
    )
    assert r.status_code == 200, r.text
    clients["policy_id"] = r.json()["id"]
    clients["dir"] = tmp_path
    yield clients
    deps.reset_shared_conn()
    deps._clear_config_cache()
    server_app.PROJECT_DIR, server_app.AUTH_ENABLED = old_dir, old_auth


# ---------------------------------------------------------------------------
# File API: database files
# ---------------------------------------------------------------------------


def _release_db():
    from havn.server import deps

    deps.reset_shared_conn()


def test_viewer_cannot_read_warehouse_file(env):
    r = env["viewer"].get("/api/files/warehouse.duckdb")
    assert r.status_code == 403
    assert SSN not in r.text


def test_viewer_cannot_read_backup(env):
    from havn.engine.backup import create_backup

    _release_db()
    create_backup(env["dir"], env["dir"] / "warehouse.duckdb")
    backups = env["viewer"].get("/api/backups").json()
    assert backups
    r = env["viewer"].get(f"/api/files/_backups/{backups[0]['filename']}")
    assert r.status_code == 403
    assert SSN not in r.text
    # Anything else under _backups/ (the manifest) is refused too.
    assert env["admin"].get("/api/files/_backups/manifest.json").status_code == 403


@pytest.mark.parametrize(
    "rel", ["copy.duckdb", "x.duckdb.wal", "store.wal", "a.ddb", "renamed.sql"],
)
def test_database_files_refused_by_name_or_magic(env, rel):
    _release_db()
    target = env["dir"] / rel
    if rel == "renamed.sql":
        # A database file under an innocent name is caught by its header.
        c = duckdb.connect(str(target))
        c.execute(f"CREATE TABLE s AS SELECT '{SSN}' AS ssn")
        c.close()
    else:
        target.write_bytes(b"not really a database " + SSN.encode())
    r = env["admin"].get(f"/api/files/{rel}")
    assert r.status_code == 403
    assert SSN not in r.text


def test_environment_database_path_refused(env):
    target = env["dir"] / "data" / "prod_store.bin"
    target.parent.mkdir()
    target.write_bytes(SSN.encode())
    assert env["viewer"].get("/api/files/data/prod_store.bin").status_code == 403


def test_editor_cannot_move_or_delete_database_files(env):
    _release_db()
    assert env["editor"].delete("/api/files/warehouse.duckdb").status_code == 403
    r = env["editor"].post("/api/files/warehouse.duckdb/move", json={"destination": "old.txt"})
    assert r.status_code == 403
    assert (env["dir"] / "warehouse.duckdb").exists()


def test_ordinary_files_still_readable(env):
    r = env["viewer"].get("/api/files/transform/bronze/m.sql")
    assert r.status_code == 200
    assert r.json()["content"] == "SELECT 1 AS id\n"


def test_file_listing_omits_database_files(env):
    def walk(items):
        for it in items:
            yield it["path"]
            yield from walk(it.get("children") or [])

    paths = list(walk(env["viewer"].get("/api/files").json()))
    assert not [p for p in paths if ".duckdb" in p]


def test_read_os_error_is_not_500(env, monkeypatch):
    from pathlib import Path

    real = Path.read_text

    def locked(self, *a, **k):
        if self.name == "m.sql":
            raise PermissionError(13, "The process cannot access the file")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", locked)
    r = env["viewer"].get("/api/files/transform/bronze/m.sql")
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Masking policy management is admin-only
# ---------------------------------------------------------------------------


def test_editor_cannot_create_update_or_delete_policy(env):
    e, pid = env["editor"], env["policy_id"]
    body = {"schema_name": "landing", "table_name": "people",
            "column_name": "email", "method": "redact"}
    assert e.post("/api/masking/policies", json=body).status_code == 403
    assert e.put(f"/api/masking/policies/{pid}", json={"method": "hash"}).status_code == 403
    assert e.delete(f"/api/masking/policies/{pid}").status_code == 403
    # Nor through the SQL-command shortcut on /api/query.
    r = e.post("/api/query", json={"sql": f"DROP MASKING POLICY {pid}"})
    assert r.status_code == 403
    r = e.post("/api/query", json={"sql": "CREATE MASKING POLICY ON landing.people.email METHOD redact"})
    assert r.status_code == 403
    # The policy survived and still masks.
    rows = env["viewer"].post("/api/query", json={"sql": "SELECT ssn FROM landing.people"}).json()["rows"]
    assert rows == [["***"]]


def test_editor_can_still_list_policies(env):
    r = env["editor"].get("/api/masking/policies")
    assert r.status_code == 200
    assert [p["id"] for p in r.json()] == [env["policy_id"]]


def test_admin_can_manage_policies(env):
    a, pid = env["admin"], env["policy_id"]
    assert a.put(f"/api/masking/policies/{pid}", json={"method": "hash"}).status_code == 200
    assert a.delete(f"/api/masking/policies/{pid}").status_code == 200


# ---------------------------------------------------------------------------
# Whole-row reads of a masked table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT to_json(p) AS j FROM landing.people p",
        "SELECT p FROM landing.people p",
        "SELECT people FROM landing.people",
        "SELECT row(p.*) FROM landing.people p",
        "SELECT struct_pack(*) FROM landing.people",
        "SELECT [p.*] FROM landing.people p",
        "SELECT COLUMNS(*) FROM landing.people",
        "SELECT to_json(x) FROM (SELECT * FROM landing.people) x",
        "WITH c AS (SELECT * FROM landing.people) SELECT to_json(c) FROM c",
        "SELECT * FROM landing.people p, LATERAL (SELECT to_json(p) AS j) t",
        "SUMMARIZE landing.people",
        "UNPIVOT landing.people ON ssn INTO NAME k VALUE v",
        "PIVOT landing.people ON id USING first(ssn)",
    ],
)
def test_whole_row_reads_rejected_for_viewer(env, sql):
    r = env["viewer"].post("/api/query", json={"sql": sql})
    assert r.status_code == 403, r.text
    assert SSN not in r.text


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT * FROM landing.people", [[1, "alice@example.com", "***"]]),
        ("SELECT p.* FROM landing.people p", [[1, "alice@example.com", "***"]]),
        ("SELECT * EXCLUDE (email) FROM landing.people", [[1, "***"]]),
        ("SELECT count(*) FROM landing.people", [[1]]),
        ("SELECT p.id, p.email FROM landing.people p", [[1, "alice@example.com"]]),
    ],
)
def test_star_and_named_columns_still_work(env, sql, expected):
    r = env["viewer"].post("/api/query", json={"sql": sql})
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == expected


def test_exempt_role_keeps_whole_row_access(env):
    r = env["admin"].post("/api/query", json={"sql": "SELECT to_json(p) AS j FROM landing.people p"})
    assert r.status_code == 200
    assert SSN in r.text


def test_whole_row_of_unmasked_table_allowed(env):
    r = env["viewer"].post(
        "/api/query", json={"sql": "SELECT to_json(t) AS j FROM (SELECT 1 AS a) t"}
    )
    assert r.status_code == 200


def test_wrapped_query_cannot_read_files(env, tmp_path):
    secret = tmp_path / "outside.csv"
    secret.write_text("a\ntopsecret-file\n")
    path = secret.as_posix()
    for sql in [
        f"SELECT * FROM range(1), '{path}'",
        f"SELECT 1) AS a, '{path}' AS b, (SELECT 1",
    ]:
        r = env["viewer"].post("/api/query", json={"sql": sql})
        assert r.status_code in (400, 403), r.text
        assert "topsecret-file" not in r.text


# ---------------------------------------------------------------------------
# Whole-row check: no false positives on ordinary column names
# ---------------------------------------------------------------------------


@pytest.fixture
def env_region(env):
    from havn.server import deps

    deps.reset_shared_conn()
    c = duckdb.connect(str(env["dir"] / "warehouse.duckdb"))
    c.execute("CREATE TABLE landing.region AS SELECT 1 AS id, 'EU' AS region")
    c.close()
    return env


@pytest.mark.parametrize(
    "sql",
    [
        "WITH totals AS (SELECT count(*) AS totals FROM landing.people) SELECT totals FROM totals",
        "SELECT p.id, region FROM landing.people p JOIN landing.region r USING (id)",
        "SELECT p.id, region.region FROM landing.people p JOIN landing.region USING (id)",
        "SELECT x FROM landing.people p, unnest([1, 2]) AS x(x)",
        "SELECT list_transform([1], people -> people + 1) FROM landing.people",
        "SELECT p.*, r.* FROM landing.people p JOIN landing.region r USING (id)",
        "SELECT * FROM (VALUES (1)) v(x), landing.people",
    ],
)
def test_ordinary_names_not_mistaken_for_rows(env_region, sql):
    r = env_region["viewer"].post("/api/query", json={"sql": sql})
    assert r.status_code == 200, r.text
    assert SSN not in r.text


# ---------------------------------------------------------------------------
# Renamed output would dodge the by-name post-query mask
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM landing.people AS p(a, b, c)",
        "SELECT c FROM (SELECT * FROM landing.people) AS t(a, b, c)",
        "WITH x(a, b, c) AS (SELECT * FROM landing.people) SELECT * FROM x",
        "SELECT * RENAME (ssn AS z) FROM landing.people",
        "SELECT * REPLACE (ssn AS email) FROM landing.people",
        "SELECT id, email, 'x' FROM landing.people UNION ALL SELECT * FROM landing.people",
        "SELECT * FROM (SELECT 1 AS a, 'e' AS b, 'x' AS q UNION ALL SELECT * FROM landing.people)",
        "SELECT * FROM landing.people p JOIN landing.people q USING (id)",
        "SELECT *, ssn LIKE '123%' AS leak FROM landing.people",
        "SELECT * FROM landing.people p, LATERAL (SELECT p.ssn AS z) t",
        "SELECT u.* FROM landing.people p, unnest([p.ssn]) u(v)",
    ],
)
def test_renamed_output_refused(env, sql):
    r = env["viewer"].post("/api/query", json={"sql": sql})
    assert r.status_code == 403, r.text
    assert SSN not in r.text


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT ssn FROM landing.people",
        "SELECT * FROM landing.people UNION ALL SELECT 1, 'a', 'b'",
        "SELECT * FROM (SELECT * FROM landing.people)",
        "WITH c AS (SELECT * FROM landing.people) SELECT * FROM c",
    ],
)
def test_masked_shapes_still_work(env, sql):
    r = env["viewer"].post("/api/query", json={"sql": sql})
    assert r.status_code == 200, r.text
    assert SSN not in r.text


# ---------------------------------------------------------------------------
# Upload cannot replace a database file
# ---------------------------------------------------------------------------


def test_upload_cannot_replace_environment_database(env):
    target = env["dir"] / "data" / "prod_store.bin"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(b"original")
    r = env["editor"].post(
        "/api/upload", files={"file": ("prod_store.bin", b"attacker", "application/octet-stream")},
    )
    assert r.status_code == 403
    assert target.read_bytes() == b"original"


@pytest.mark.parametrize("name", ["evil.duckdb", "evil.duckdb.wal"])
def test_upload_refuses_database_names(env, name):
    r = env["editor"].post("/api/upload", files={"file": (name, b"x", "application/octet-stream")})
    assert r.status_code == 403


def test_upload_of_csv_still_works(env):
    r = env["editor"].post("/api/upload", files={"file": ("ok.csv", b"a\n1\n", "text/csv")})
    assert r.status_code == 200
    assert (env["dir"] / "data" / "ok.csv").read_bytes() == b"a\n1\n"
