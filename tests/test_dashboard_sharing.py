"""Published dashboards: share links, governance, the published API, embedding."""

from __future__ import annotations

import datetime as dt

import duckdb
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def project(tmp_path):
    (tmp_path / "project.yml").write_text(
        "name: test\n"
        "database:\n  path: warehouse.duckdb\n"
        "sharing:\n  embed:\n    allowed_origins:\n"
        "      - https://intranet.example.com\n"
        "      - 'https://evil.example.com; script-src *'\n",
        encoding="utf-8",
    )
    (tmp_path / "transform").mkdir()
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA landing")
    conn.execute(
        "CREATE TABLE landing.customers AS SELECT i AS id, 'user' || i || '@example.com' AS email, "
        "CASE WHEN i % 2 = 0 THEN 'north' ELSE 'south' END AS region, i * 10 AS revenue, "
        "DATE '2024-01-01' + CAST(i AS INTEGER) AS signup_date FROM range(1, 21) t(i)"
    )
    from havn.engine.auth import authenticate, create_user
    from havn.engine.masking import create_policy, ensure_masking_table

    tokens = {}
    for name, role in (("ada", "admin"), ("eve", "editor"), ("vic", "viewer")):
        create_user(conn, name, "pw-" + name, role)
        tokens[name] = authenticate(conn, name, "pw-" + name)
    ensure_masking_table(conn)
    create_policy(conn, schema_name="landing", table_name="customers", column_name="email",
                  method="redact", exempted_roles=["admin"])
    conn.close()
    (tmp_path / "tokens").write_text("\n".join(f"{k}={v}" for k, v in tokens.items()), encoding="utf-8")
    return tmp_path


def _tokens(project):
    return dict(line.split("=", 1) for line in (project / "tokens").read_text(encoding="utf-8").splitlines())


@pytest.fixture
def client(project):
    import havn.server.app as server_app
    from havn.engine.sharing import reset_rate_limits
    from havn.server.deps import _clear_config_cache, invalidate_token_cache, reset_shared_conn

    reset_shared_conn()
    invalidate_token_cache()
    _clear_config_cache()
    reset_rate_limits()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = True
    c = TestClient(server_app.app)
    yield c
    server_app.AUTH_ENABLED = False
    reset_shared_conn()


def _h(project, who):
    return {"Authorization": f"Bearer {_tokens(project)[who]}"}


@pytest.fixture
def dashboard(client, project):
    admin = _h(project, "ada")
    d = client.post("/api/dashboards", json={"name": "Customers"}, headers=admin).json()
    did = d["id"]
    client.put(f"/api/dashboards/{did}", headers=admin, json={
        "filters": [
            {"id": "f_region", "label": "Region", "type": "dropdown", "column": "region",
             "options_sql": "SELECT DISTINCT region FROM landing.customers ORDER BY 1"},
            {"id": "f_rev", "label": "Revenue", "type": "number_range", "column": "revenue"},
            {"id": "f_signup", "label": "Signup", "type": "date_range", "column": "signup_date"},
            {"id": "f_multi", "label": "Regions", "type": "multi_select", "column": "region"},
        ],
        "settings": {"parameters": [{"name": "min_id", "label": "Min id", "type": "number", "default": 0}]},
    })
    emails = client.post(f"/api/dashboards/{did}/widgets", headers=admin, json={
        "widget_type": "table", "title": "Emails",
        "sql_query": "SELECT id, email, region, revenue FROM landing.customers WHERE id > ${min_id} ORDER BY id",
        "position": {"x": 1, "y": 1, "w": 12, "h": 6},
    }).json()["id"]
    kpi = client.post(f"/api/dashboards/{did}/widgets", headers=admin, json={
        "widget_type": "kpi", "title": "Revenue",
        "sql_query": "SELECT SUM(revenue) AS total FROM landing.customers",
        "position": {"x": 13, "y": 1, "w": 6, "h": 3},
    }).json()["id"]
    text = client.post(f"/api/dashboards/{did}/widgets", headers=admin, json={
        "widget_type": "text", "title": "Note", "sql_query": "Numbers are **gross**.",
        "position": {"x": 13, "y": 4, "w": 6, "h": 3},
    }).json()["id"]
    return {"id": did, "emails": emails, "kpi": kpi, "text": text}


def _public(client, project, dashboard, **kw):
    body = {"mode": "public", "view_as_role": "viewer", **kw}
    r = client.post(f"/api/dashboards/{dashboard['id']}/shares", json=body, headers=_h(project, "ada"))
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Creating links: who may
# ---------------------------------------------------------------------------


def test_only_admins_create_public_links(client, project, dashboard):
    r = client.post(f"/api/dashboards/{dashboard['id']}/shares",
                    json={"mode": "public", "view_as_role": "viewer"}, headers=_h(project, "eve"))
    assert r.status_code == 403
    r = client.post(f"/api/dashboards/{dashboard['id']}/shares",
                    json={"mode": "signed_in"}, headers=_h(project, "eve"))
    assert r.status_code == 200
    assert r.json()["path"] == f"/p/{r.json()['id']}"
    r = client.post(f"/api/dashboards/{dashboard['id']}/shares",
                    json={"mode": "signed_in"}, headers=_h(project, "vic"))
    assert r.status_code == 403


def test_public_link_requires_view_as_identity(client, project, dashboard):
    r = client.post(f"/api/dashboards/{dashboard['id']}/shares", json={"mode": "public"},
                    headers=_h(project, "ada"))
    assert r.status_code == 400
    r = client.post(f"/api/dashboards/{dashboard['id']}/shares",
                    json={"mode": "public", "view_as_user": "nobody"}, headers=_h(project, "ada"))
    assert r.status_code == 400


def test_token_shown_once_and_stored_hashed(client, project, dashboard):
    share = _public(client, project, dashboard)
    token = share["token"]
    assert len(token) >= 40 and share["path"] == f"/p/{token}"
    listed = client.get(f"/api/dashboards/{dashboard['id']}/shares", headers=_h(project, "ada")).json()
    assert listed[0]["path"] is None and "token" not in listed[0]
    assert listed[0]["token_hint"] == token[-4:]
    import havn.server.deps as deps

    cur = deps._get_shared_conn().cursor()
    try:
        stored = cur.execute("SELECT token_hash FROM _havn.dashboard_shares").fetchone()[0]
    finally:
        cur.close()
    assert token not in stored


def test_publish_and_unpublish_are_audited(client, project, dashboard):
    share = _public(client, project, dashboard)
    client.delete(f"/api/shares/{share['id']}", headers=_h(project, "ada"))
    log = client.get("/api/audit?limit=50", headers=_h(project, "ada")).json()
    actions = [e["action"] for e in log]
    assert "dashboard_publish" in actions and "dashboard_unpublish" in actions


# ---------------------------------------------------------------------------
# The published API
# ---------------------------------------------------------------------------


def test_published_definition_has_no_sql(client, project, dashboard):
    share = _public(client, project, dashboard)
    r = client.get(share["path"].replace("/p/", "/api/published/"))
    assert r.status_code == 200
    body = r.text
    assert "SELECT" not in body and "landing.customers" not in body
    data = r.json()
    assert {w["id"] for w in data["dashboard"]["widgets"]} == {dashboard["emails"], dashboard["kpi"], dashboard["text"]}
    text = next(w for w in data["dashboard"]["widgets"] if w["id"] == dashboard["text"])
    assert text["config"]["content"] == "Numbers are **gross**." and text["has_query"] is False
    assert all("options_sql" not in f for f in data["dashboard"]["filters"])
    assert r.headers["cache-control"] == "no-store"


def test_public_token_runs_only_saved_queries(client, project, dashboard):
    share = _public(client, project, dashboard)
    key = share["token"]
    r = client.post(f"/api/published/{key}/query", json={})
    assert r.status_code == 200
    results = r.json()["results"]
    # The text widget's markdown is never executed as SQL.
    assert set(results) == {dashboard["emails"], dashboard["kpi"]}
    assert results[dashboard["emails"]]["row_count"] == 20
    # There is no way to hand the published API SQL: unknown fields are ignored
    # and the ad-hoc query endpoint still needs an account.
    r = client.post(f"/api/published/{key}/query", json={"sql": "SELECT 42", "widget_ids": ["nope"]})
    assert r.json()["results"] == {}
    assert client.post("/api/query", json={"sql": "SELECT 1"}).status_code == 401
    assert client.post("/api/query", json={"sql": "SELECT 1"},
                       headers={"Authorization": f"Bearer {key}"}).status_code == 401


def test_public_link_applies_view_as_masking(client, project, dashboard):
    viewer_link = _public(client, project, dashboard)
    admin_link = _public(client, project, dashboard, view_as_role=None, view_as_user="ada")
    as_viewer = client.post(f"/api/published/{viewer_link['token']}/query", json={}).json()
    as_admin = client.post(f"/api/published/{admin_link['token']}/query", json={}).json()
    v = as_viewer["results"][dashboard["emails"]]
    a = as_admin["results"][dashboard["emails"]]
    ei = v["columns"].index("email")
    assert all("@example.com" not in str(row[ei]) for row in v["rows"])
    assert a["rows"][0][a["columns"].index("email")] == "user1@example.com"
    # Admin headers on a public request do not lift the link's identity.
    as_viewer_again = client.post(f"/api/published/{viewer_link['token']}/query", json={},
                                  headers=_h(project, "ada")).json()
    assert "@example.com" not in str(as_viewer_again["results"][dashboard["emails"]]["rows"][0][ei])


def test_view_as_user_follows_current_role_and_existence(client, project, dashboard):
    link = _public(client, project, dashboard, view_as_role=None, view_as_user="eve")
    r = client.post(f"/api/published/{link['token']}/query", json={})
    assert r.status_code == 200
    client.delete("/api/users/eve", headers=_h(project, "ada"))
    r = client.post(f"/api/published/{link['token']}/query", json={})
    assert r.status_code == 403


def test_expired_and_revoked_tokens_refused(client, project, dashboard):
    share = _public(client, project, dashboard, expires_in_days=1)
    key = share["token"]
    assert client.get(f"/api/published/{key}").status_code == 200
    import havn.server.deps as deps

    cur = deps._get_shared_conn().cursor()
    try:
        cur.execute("UPDATE _havn.dashboard_shares SET expires_at = ? WHERE id = ?",
                    [dt.datetime.now() - dt.timedelta(minutes=1), share["id"]])
    finally:
        cur.close()
    r = client.get(f"/api/published/{key}")
    assert r.status_code == 410 and "expired" in r.json()["detail"]
    assert client.post(f"/api/published/{key}/query", json={}).status_code == 410

    other = _public(client, project, dashboard)
    assert client.delete(f"/api/shares/{other['id']}", headers=_h(project, "ada")).status_code == 200
    r = client.post(f"/api/published/{other['token']}/query", json={})
    assert r.status_code == 410 and "revoked" in r.json()["detail"]
    assert client.get("/api/published/not-a-real-token").status_code == 404


def test_public_links_can_be_disabled_project_wide(client, project, dashboard):
    share = _public(client, project, dashboard)
    yml = project / "project.yml"
    yml.write_text(yml.read_text(encoding="utf-8") + "  public_links: false\n", encoding="utf-8")
    from havn.server.deps import _clear_config_cache

    _clear_config_cache()
    assert client.get(f"/api/published/{share['token']}").status_code == 403


def test_signed_in_link_needs_account_and_runs_as_viewer(client, project, dashboard):
    share = client.post(f"/api/dashboards/{dashboard['id']}/shares", json={"mode": "signed_in"},
                        headers=_h(project, "eve")).json()
    key = share["id"]
    assert client.get(f"/api/published/{key}").status_code == 401
    as_vic = client.post(f"/api/published/{key}/query", json={}, headers=_h(project, "vic")).json()
    as_ada = client.post(f"/api/published/{key}/query", json={}, headers=_h(project, "ada")).json()
    v, a = as_vic["results"][dashboard["emails"]], as_ada["results"][dashboard["emails"]]
    ei = v["columns"].index("email")
    assert "@example.com" not in str(v["rows"][0][ei])
    assert a["rows"][0][ei] == "user1@example.com"


def test_filters_are_bound_and_validated(client, project, dashboard):
    key = _public(client, project, dashboard)["token"]

    def q(**body):
        return client.post(f"/api/published/{key}/query", json=body)

    r = q(filters={"region": "north"})
    rows = r.json()["results"][dashboard["emails"]]["rows"]
    assert len(rows) == 10 and {row[2] for row in rows} == {"north"}
    # An injection attempt is just a value that matches nothing.
    r = q(filters={"region": "north' OR '1'='1"})
    assert r.json()["results"][dashboard["emails"]]["row_count"] == 0
    # Only declared filter columns are accepted.
    assert q(filters={"id": 1}).status_code == 400
    assert q(filters={'region" = region OR 1=1 --': "x"}).status_code == 400
    # Values must fit the filter's type.
    assert q(filters={"revenue": "100; DROP TABLE x"}).status_code == 400
    assert q(filters={"revenue": {"min": "abc"}}).status_code == 400
    assert q(filters={"signup_date": {"from": "2024-01-01'); DROP"}}).status_code == 400
    r = q(filters={"revenue": {"min": 50, "max": 100}})
    assert r.json()["results"][dashboard["emails"]]["row_count"] == 6
    r = q(filters={"signup_date": {"from": "2024-01-05", "to": "2024-01-06"}})
    assert r.json()["results"][dashboard["emails"]]["row_count"] == 2
    r = q(filters={"region": ["north", "nowhere"]})
    assert r.status_code == 200
    # Parameters: declared only, bound as values.
    r = q(parameters={"min_id": 15})
    assert r.json()["results"][dashboard["emails"]]["row_count"] == 5
    r = q(parameters={"min_id": "0 OR 1=1"})
    assert r.json()["results"][dashboard["emails"]]["row_count"] == 0 or "error" in r.json()["results"][dashboard["emails"]]
    assert q(parameters={"other": 1}).status_code == 400
    assert q(parameters={"min_id": [1, 2]}).status_code == 400


def test_filter_on_masked_column_is_refused_without_detail(client, project, dashboard):
    admin = _h(project, "ada")
    client.put(f"/api/dashboards/{dashboard['id']}", headers=admin, json={
        "filters": [{"id": "f_email", "label": "Email", "type": "text", "column": "email"}],
    })
    key = _public(client, project, dashboard)["token"]
    r = client.post(f"/api/published/{key}/query", json={"filters": {"email": "user1"}})
    result = r.json()["results"][dashboard["emails"]]
    assert result["row_count"] == 0 and result["error"] == "This widget could not be loaded."


def test_filter_options_run_saved_options_query(client, project, dashboard):
    key = _public(client, project, dashboard)["token"]
    r = client.post(f"/api/published/{key}/filters/f_region/options")
    assert r.status_code == 200 and r.json()["options"] == ["north", "south"]
    assert client.post(f"/api/published/{key}/filters/nope/options").status_code == 404


def test_freshness_reports_model_build_time(client, project, dashboard):
    import havn.server.deps as deps

    cur = deps._get_shared_conn().cursor()
    try:
        cur.execute(
            "INSERT INTO _havn.model_state (model_path, content_hash, upstream_hash, materialized_as, last_run_at) "
            "VALUES ('landing.customers', 'x', 'y', 'table', TIMESTAMP '2024-05-01 06:00:00')"
        )
    finally:
        cur.close()
    key = _public(client, project, dashboard)["token"]
    fresh = client.get(f"/api/published/{key}").json()["freshness"]
    assert fresh["as_of"].startswith("2024-05-01T06:00")
    # Public viewers see when, not what: no model or table names.
    assert fresh["models"] == [] and fresh["unknown"] == [] and fresh["model_count"] == 1
    signed = client.post(f"/api/dashboards/{dashboard['id']}/shares", json={"mode": "signed_in"},
                         headers=_h(project, "ada")).json()
    fresh = client.get(f"/api/published/{signed['id']}", headers=_h(project, "vic")).json()["freshness"]
    assert fresh["models"][0]["name"] == "landing.customers"


def test_published_page_frame_headers(client, project, dashboard):
    key = _public(client, project, dashboard)["token"]
    page = client.get(f"/p/{key}")
    csp = page.headers.get("content-security-policy", "")
    assert csp == "frame-ancestors 'self' https://intranet.example.com"
    assert "x-frame-options" not in page.headers
    assert page.headers["referrer-policy"] == "no-referrer"
    other = client.get("/data/dashboards")
    assert other.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" not in other.headers


def test_rate_limit(client, project, dashboard, monkeypatch):
    import havn.engine.sharing as sharing

    monkeypatch.setattr(sharing, "_RATE_MAX", 3)
    key = _public(client, project, dashboard)["token"]
    codes = [client.get(f"/api/published/{key}").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[-1] == 429


def test_deleting_dashboard_removes_links(client, project, dashboard):
    key = _public(client, project, dashboard)["token"]
    client.delete(f"/api/dashboards/{dashboard['id']}", headers=_h(project, "ada"))
    assert client.get(f"/api/published/{key}").status_code == 404


def test_editor_cannot_revoke_public_link(client, project, dashboard):
    share = _public(client, project, dashboard)
    assert client.delete(f"/api/shares/{share['id']}", headers=_h(project, "eve")).status_code == 403
    r = client.patch(f"/api/shares/{share['id']}", json={"expires_in_days": 2}, headers=_h(project, "ada"))
    assert r.status_code == 200 and r.json()["expires_at"]


# ---------------------------------------------------------------------------
# Engine-level
# ---------------------------------------------------------------------------


def test_build_widget_sql_binds_everything():
    from havn.engine.dashboard_queries import build_widget_sql

    sql, params = build_widget_sql(
        "SELECT region, SUM(x) FROM t WHERE y > ${lo} GROUP BY 1",
        {"region": ["a", "b'"], "bad name": "z"},
        {"lo": "1; DROP TABLE t"},
        {"region": "multi_select"},
    )
    assert "DROP" not in sql and "b'" not in sql
    assert "$p_lo" in sql and '"region" IN ($f0_0, $f0_1)' in sql
    assert sql.index("WHERE") < sql.index("GROUP BY")
    assert params == {"p_lo": "1; DROP TABLE t", "f0_0": "a", "f0_1": "b'"}


def test_embed_origins_are_sanitised():
    from havn.engine.embed import embed_snippet, frame_ancestors_policy

    assert frame_ancestors_policy(["https://a.example.com/", "javascript:alert(1)", "https://b.com 'unsafe-inline'"]) == \
        "frame-ancestors 'self' https://a.example.com"
    snippet = embed_snippet('https://h/p/abc"><script>', "T")
    assert "<script>" not in snippet and "embed=1" in snippet


def test_editor_cannot_change_sql_a_public_link_runs_as_admin(client, project, dashboard):
    # An admin's public link runs the widgets as admin; an editor rewriting a
    # widget would decide what admin-level SQL anonymous viewers get.
    _public(client, project, dashboard, view_as_role="admin")
    eve = _h(project, "eve")
    r = client.put(
        f"/api/dashboards/{dashboard['id']}/widgets/{dashboard['kpi']}",
        json={"sql_query": "SELECT * FROM _havn.users"}, headers=eve,
    )
    assert r.status_code == 403, r.text
    r = client.post(f"/api/dashboards/{dashboard['id']}/widgets", headers=eve, json={
        "widget_type": "table", "title": "x", "sql_query": "SELECT * FROM _havn.tokens",
        "position": {"x": 1, "y": 1, "w": 4, "h": 4},
    })
    assert r.status_code == 403, r.text
    # The admin who published it still can.
    r = client.put(
        f"/api/dashboards/{dashboard['id']}/widgets/{dashboard['kpi']}",
        json={"title": "Total revenue"}, headers=_h(project, "ada"),
    )
    assert r.status_code == 200, r.text


def test_editor_can_edit_when_links_run_as_viewer(client, project, dashboard):
    _public(client, project, dashboard, view_as_role="viewer")
    r = client.put(
        f"/api/dashboards/{dashboard['id']}/widgets/{dashboard['kpi']}",
        json={"title": "Revenue (gross)"}, headers=_h(project, "eve"),
    )
    assert r.status_code == 200, r.text
