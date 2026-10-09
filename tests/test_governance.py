"""Row-level security, lineage-inherited policies and governed Python.

Real DuckDB throughout. The bypass tests assert that the raw values never
appear in what a governed viewer gets back: either the query is rewritten
(filtered / masked) or it is refused.
"""

from __future__ import annotations

import duckdb
import pytest
from fastapi.testclient import TestClient

from havn.engine.governance import Viewer, govern_query
from havn.engine.governance.catalog import clear_cache
from havn.engine.masking import create_policy
from havn.engine.masking_rewriter import MaskedColumnAccessError
from havn.engine.row_policies import create_row_policy, render_filter

SSN_SOUTH = "222-22-2222"
NORTH = Viewer("nora", "editor", {"region": "north"})
SOUTH = Viewer("sam", "viewer", {"region": "south"})
ADMIN = Viewer("root", "admin", {})


@pytest.fixture
def conn(tmp_path):
    clear_cache()
    c = duckdb.connect(str(tmp_path / "w.duckdb"))
    c.execute("CREATE SCHEMA silver; CREATE SCHEMA gold")
    c.execute("""CREATE TABLE silver.customers AS SELECT * FROM (VALUES
        (1, 'alice', 'north', '111-11-1111'),
        (2, 'bob', 'south', '222-22-2222'),
        (3, 'carol', 'north', '333-33-3333')) t(id, name, region, ssn)""")
    c.execute("CREATE TABLE silver.orders AS SELECT * FROM (VALUES (10, 1, 5), (11, 2, 7), (12, 3, 9)) t(oid, cid, amt)")
    create_row_policy(c, schema_name="silver", table_name="customers",
                      filter_sql="region = havn_attr('region')")
    create_policy(c, schema_name="silver", table_name="customers", column_name="ssn", method="redact")
    yield c
    c.close()


def run(c, sql, viewer=NORTH):
    g = govern_query(sql, viewer, c)
    cur = c.execute(g.sql)
    cols = [d[0] for d in cur.description]
    return g.post_mask(cols, [list(r) for r in cur.fetchall()], c)


def flat(rows):
    return " ".join(str(v) for r in rows for v in r)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM silver.customers",
    "SELECT c.* FROM silver.customers c",
    "SELECT * FROM SILVER.\"Customers\"",
    "SELECT * FROM w.silver.customers",
    "FROM silver.customers",
    "WITH x AS (SELECT * FROM silver.customers) SELECT a.name, b.region FROM x a JOIN x b USING (id)",
    "SELECT name, region FROM silver.customers UNION ALL SELECT name, region FROM silver.customers",
    "SELECT * FROM (SELECT * FROM (SELECT name, region FROM silver.customers) a) b",
    "SELECT o.oid, l.name, l.region FROM silver.orders o, "
    "LATERAL (SELECT name, region FROM silver.customers c WHERE c.id = o.cid) l",
    "SELECT o.*, (SELECT region FROM silver.customers c WHERE c.id = o.cid) r FROM silver.orders o",
    "SELECT silver.customers.name, silver.customers.region FROM silver.customers",
])
def test_rls_bypass_attempts_only_see_own_rows(conn, sql):
    rows = run(conn, sql)
    text = flat(rows)
    assert "bob" not in text and "south" not in text and SSN_SOUTH not in text


def test_rls_filters_exists_and_counts(conn):
    rows = run(conn, "SELECT count(*) FROM silver.orders o WHERE EXISTS "
                     "(SELECT 1 FROM silver.customers c WHERE c.id = o.cid)")
    assert rows == [[2]]


def test_view_over_protected_table_is_inlined(conn):
    conn.execute("CREATE VIEW gold.v AS SELECT id, name, region FROM silver.customers")
    conn.execute("CREATE VIEW gold.rev AS SELECT c.region, sum(o.amt) t FROM silver.orders o "
                 "JOIN silver.customers c ON o.cid = c.id GROUP BY 1")
    assert {r[1] for r in run(conn, "SELECT * FROM gold.v")} == {"alice", "carol"}
    assert run(conn, "SELECT * FROM gold.rev") == [["north", 14]]


def test_per_attribute_and_admin_exemption(conn):
    assert {r[1] for r in run(conn, "SELECT * FROM silver.customers", SOUTH)} == {"bob"}
    admin_rows = run(conn, "SELECT * FROM silver.customers", ADMIN)
    assert len(admin_rows) == 3 and SSN_SOUTH in flat(admin_rows)


def test_missing_attribute_sees_nothing(conn):
    rows = run(conn, "SELECT * FROM silver.customers", Viewer("x", "viewer", {}))
    assert rows == []


def test_masking_applies_with_rls(conn):
    rows = run(conn, "SELECT ssn AS s FROM silver.customers c")
    assert rows and all(r[0] == "***" for r in rows)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM _havn.row_policies",
    "SELECT * FROM pragma_storage_info('silver.customers')",
    "SELECT * FROM leak()",
    "SELECT ssn FROM silver.customers WHERE ssn = '222-22-2222'",
])
def test_refused(conn, sql):
    conn.execute("CREATE MACRO leak() AS TABLE SELECT * FROM silver.customers")
    with pytest.raises(MaskedColumnAccessError):
        govern_query(sql, NORTH, conn)


def test_scalar_macro_reaching_protected_table_refused(conn):
    conn.execute("CREATE MACRO first_ssn() AS (SELECT ssn FROM silver.customers LIMIT 1)")
    with pytest.raises(MaskedColumnAccessError):
        govern_query("SELECT first_ssn()", NORTH, conn)


def test_information_schema_has_no_values(conn):
    rows = run(conn, "SELECT * FROM information_schema.columns WHERE table_name = 'customers'")
    assert SSN_SOUTH not in flat(rows)


def test_filter_placeholders_render():
    node = render_filter("region = '{user.region}' AND owner = {user.username}", NORTH)
    sql = node.sql(dialect="duckdb")
    assert "'north'" in sql and "'nora'" in sql


def test_filter_rejects_injection(conn):
    with pytest.raises(ValueError):
        create_row_policy(conn, schema_name="silver", table_name="customers",
                          filter_sql="1=1) UNION SELECT * FROM silver.customers --")


def test_roles_and_users_targeting(conn):
    create_row_policy(conn, schema_name="silver", table_name="orders", filter_sql="amt > 6",
                      applies_to_users=["sam"])
    assert len(run(conn, "SELECT * FROM silver.orders", NORTH)) == 3
    assert len(run(conn, "SELECT * FROM silver.orders", SOUTH)) == 2


# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------


def _project(tmp_path, models: dict[str, str]):
    from havn.engine.transform import run_transform

    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: w.duckdb\n")
    for name, sql in models.items():
        schema, _, rel = name.partition(".")
        d = tmp_path / "transform" / schema
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{rel}.sql").write_text(sql)
    return run_transform


def test_masking_and_rows_follow_lineage(conn, tmp_path):
    run_transform = _project(tmp_path, {
        "gold.contacts": "@config materialized=table\nSELECT id, region, upper(ssn) AS tax_id FROM silver.customers",
        "gold.totals": "@config materialized=table\nSELECT count(*) AS n FROM silver.customers",
        "gold.safe": "@config materialized=table\n@declassify rows: one row, a count\nSELECT count(*) AS n FROM silver.customers",
    })
    run_transform(conn, tmp_path / "transform")
    clear_cache()
    g = govern_query("SELECT * FROM gold.contacts", NORTH, conn, project_dir=tmp_path)
    cur = conn.execute(g.sql)
    rows = g.post_mask([d[0] for d in cur.description], [list(r) for r in cur.fetchall()], conn)
    assert {r[1] for r in rows} == {"north"}
    assert all(r[2] == "***" for r in rows)
    # A model that drops the policy column shows the policy's subjects nothing...
    g = govern_query("SELECT * FROM gold.totals", NORTH, conn, project_dir=tmp_path)
    assert conn.execute(g.sql).fetchall() == []
    # ...unless it declassifies its rows.
    g = govern_query("SELECT * FROM gold.safe", NORTH, conn, project_dir=tmp_path)
    assert conn.execute(g.sql).fetchall() == [(3,)]

    from havn.engine.governance.report import governance_warnings, pii_report

    report = pii_report(conn, tmp_path)
    contacts = next(e for e in report["classifications"] if e["relation"] == "gold.contacts")
    tax = next(c for c in contacts["columns"] if c["column"] == "tax_id")
    assert tax["source"] == "inherited" and tax["method"] == "redact"
    messages = [w.message for w in governance_warnings(conn, tmp_path)]
    assert any("gold.totals" in m or "see no rows" in m for m in messages)


def test_pii_tag_without_policy_warns(conn, tmp_path):
    run_transform = _project(tmp_path, {
        "silver.people": "@config materialized=table\n@pii email\nSELECT 'a@x.com' AS email",
        "gold.people": "@config materialized=table\nSELECT email FROM silver.people",
    })
    run_transform(conn, tmp_path / "transform")
    clear_cache()
    from havn.engine.governance.report import governance_warnings

    messages = [w.message for w in governance_warnings(conn, tmp_path)]
    assert any("email carries PII" in m for m in messages)


# ---------------------------------------------------------------------------
# Governed Python
# ---------------------------------------------------------------------------


EVIL = r'''
print("ROWS", db.execute("SELECT * FROM silver.customers").fetchall())
import pandas as pd
df = pd.DataFrame({"a": [1, 2]})
db.execute("CREATE OR REPLACE TABLE landing.from_df AS SELECT * FROM df")
db.execute("CREATE OR REPLACE TABLE landing.copy AS SELECT * FROM silver.customers")
attempts = {
    "duckdb": "import duckdb; duckdb.connect(WH).execute('SELECT * FROM silver.customers').fetchall()",
    "open": "open(WH, 'rb').read()",
    "conn": "db._conn",
    "ctypes": "import ctypes; ctypes.CDLL('msvcrt' if __import__('os').name == 'nt' else None)",
    "subprocess": "import subprocess; subprocess.run(['python', '-c', 'print(1)'])",
    "attach": "db.execute(\"ATTACH '\" + WH + \"' AS w2\")",
    "blob": "db.execute(\"SELECT * FROM read_blob('\" + WH + \"')\").fetchall()",
    "meta": "db.execute('SELECT * FROM _havn.masking_policies').fetchall()",
}
for name, code in attempts.items():
    try:
        exec(code, {"db": db, "WH": WH})
        print("NOT BLOCKED", name)
    except BaseException as e:
        print("blocked", name, type(e).__name__)
'''


def test_editor_script_runs_governed(conn, tmp_path):
    from havn.engine.database import ensure_meta_table
    from havn.engine.runner import run_script

    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: w.duckdb\n")
    (tmp_path / "ingest").mkdir()
    conn.execute("CREATE SCHEMA landing")
    ensure_meta_table(conn)
    script = tmp_path / "ingest" / "evil.py"
    script.write_text(f"WH = {str(tmp_path / 'w.duckdb')!r}\n" + EVIL)
    result = run_script(conn, script, "ingest", run_as=NORTH)
    out = result["log_output"]
    assert result["status"] == "success", out
    assert result.get("governed")
    assert "NOT BLOCKED" not in out, out
    assert SSN_SOUTH not in out and "bob" not in out
    assert "111-11-1111" not in out  # masked even in the user's own rows
    assert conn.execute("SELECT count(*) FROM landing.from_df").fetchone()[0] == 2
    copy = conn.execute("SELECT * FROM landing.copy").fetchall()
    assert {r[1] for r in copy} == {"alice", "carol"} and all(r[3] == "***" for r in copy)


def test_admin_script_stays_in_process(conn, tmp_path):
    from havn.engine.database import ensure_meta_table
    from havn.engine.runner import run_script

    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: w.duckdb\n")
    ensure_meta_table(conn)
    script = tmp_path / "s.py"
    script.write_text("print(db.execute('SELECT count(*) FROM silver.customers').fetchone()[0])\n")
    result = run_script(conn, script, "ingest", run_as=ADMIN)
    assert result["status"] == "success" and not result.get("governed")
    assert "3" in result["log_output"]


def test_governed_script_timeout_kills_child(conn, tmp_path):
    from havn.engine.database import ensure_meta_table
    from havn.engine.runner import run_script

    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: w.duckdb\n")
    ensure_meta_table(conn)
    script = tmp_path / "slow.py"
    script.write_text("import time\nwhile True:\n    time.sleep(0.1)\n")
    result = run_script(conn, script, "ingest", timeout=3, run_as=NORTH)
    assert result["status"] == "error" and result.get("timed_out")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture
def api(tmp_path):
    import havn.server.app as server_app
    from havn.server import deps

    clear_cache()
    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: warehouse.duckdb\n")
    c = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    c.execute("CREATE SCHEMA silver")
    c.execute("""CREATE TABLE silver.customers AS SELECT * FROM (VALUES
        (1, 'alice', 'north', '111-11-1111'), (2, 'bob', 'south', '222-22-2222')) t(id, name, region, ssn)""")
    c.close()
    deps.reset_shared_conn()
    deps._clear_config_cache()
    deps.invalidate_token_cache()
    with deps._login_attempts_lock:
        deps._login_attempts.clear()
    old = server_app.PROJECT_DIR, server_app.AUTH_ENABLED
    server_app.PROJECT_DIR, server_app.AUTH_ENABLED = tmp_path, True
    anon = TestClient(server_app.app)
    tok = anon.post("/api/auth/setup", json={"username": "admin", "password": "adminpass1"}).json()["token"]
    clients = {"admin": TestClient(server_app.app)}
    clients["admin"].headers["Authorization"] = f"Bearer {tok}"
    for role in ("editor", "viewer"):
        assert clients["admin"].post("/api/users", json={
            "username": role, "password": role + "pass1", "role": role}).status_code == 200
        t = anon.post("/api/auth/login", json={"username": role, "password": role + "pass1"}).json()["token"]
        clients[role] = TestClient(server_app.app)
        clients[role].headers["Authorization"] = f"Bearer {t}"
    yield clients
    deps.reset_shared_conn()
    deps._clear_config_cache()
    server_app.PROJECT_DIR, server_app.AUTH_ENABLED = old


def test_api_row_policies_admin_only_and_enforced(api):
    body = {"schema_name": "silver", "table_name": "customers",
            "filter_sql": "region = havn_attr('region')"}
    assert api["editor"].post("/api/governance/row-policies", json=body).status_code == 403
    r = api["admin"].post("/api/governance/row-policies", json=body)
    assert r.status_code == 200, r.text
    assert api["admin"].put("/api/users/viewer/attributes",
                            json={"attributes": {"region": "south"}}).status_code == 200
    assert api["viewer"].put("/api/users/viewer/attributes",
                             json={"attributes": {"region": "north"}}).status_code == 403

    rows = api["viewer"].post("/api/query", json={"sql": "SELECT * FROM silver.customers"}).json()["rows"]
    assert [r[1] for r in rows] == ["bob"]
    sample = api["viewer"].get("/api/tables/silver/customers/sample").json()["rows"]
    assert [r[1] for r in sample] == ["bob"]
    profile = api["viewer"].get("/api/tables/silver/customers/profile").json()
    assert profile["row_count"] == 1
    # The editor has no region attribute: no rows, and no raw export either.
    assert api["editor"].post("/api/query", json={"sql": "SELECT * FROM silver.customers"}).json()["rows"] == []
    assert api["editor"].get("/v1/export/duckdb").status_code == 403
    # Preview as user (admin only).
    pv = api["admin"].post("/api/governance/preview",
                           json={"username": "viewer", "sql": "SELECT name FROM silver.customers"}).json()
    assert pv["rows"] == [["bob"]]
    assert api["editor"].post("/api/governance/preview",
                              json={"username": "viewer", "sql": "SELECT 1"}).status_code == 403
    # Internal metadata is admin-only through /api/query.
    assert api["viewer"].post("/api/query", json={"sql": "SELECT * FROM _havn.users"}).status_code == 403


def test_api_v1_sql_governed_and_owned(api):
    api["admin"].post("/api/governance/row-policies", json={
        "schema_name": "silver", "table_name": "customers", "filter_sql": "region = 'north'",
        "applies_to_roles": ["editor"]})
    r = api["editor"].post("/v1/sql", json={"sql": "SELECT name FROM silver.customers"}).json()
    assert r["rows"] == [["alice"]]
    # Another user cannot fetch the editor's result.
    assert api["viewer"].get(f"/v1/sql/{r['statement_id']}/result").status_code == 404
