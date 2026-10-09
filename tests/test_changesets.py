"""Verified agent changes: change sets, verification, apply/discard, API, MCP."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from havn.engine.changesets import (
    ChangeSetConflict,
    ChangeSetError,
    apply_change_set,
    create_change_set,
    discard_change_set,
    get_change_set,
    list_change_sets,
    revise_change_set,
)
from havn.engine.changesets.overlay import AgentWorkspace
from havn.engine.changesets.service import report_text, verify_and_store
from havn.engine.changesets.store import check_path
from havn.engine.changesets.verify import verify_change_set

BRONZE = """@config materialized=table, schema=bronze

SELECT id, region, amount, status FROM landing.orders
"""

GOLD = """@config materialized=table, schema=gold
@assert row_count > 0

SELECT region, SUM(amount) AS revenue
FROM bronze.orders
GROUP BY region
"""

UNIT_TEST = """model: gold.revenue
tests:
  - name: sums by region
    given:
      bronze.orders:
        rows:
          - {id: 1, region: north, amount: 10, status: paid}
          - {id: 2, region: north, amount: 5, status: paid}
    expect:
      rows:
        - {region: north, revenue: 15}
"""

CONTRACT = """contracts:
  - name: revenue_positive
    model: gold.revenue
    assertions:
      - "revenue >= 0"
"""


def _make_project(root: Path) -> Path:
    (root / "project.yml").write_text("name: cs\ndatabase:\n  path: warehouse.duckdb\n")
    (root / "transform" / "bronze").mkdir(parents=True)
    (root / "transform" / "gold").mkdir(parents=True)
    (root / "transform" / "bronze" / "orders.sql").write_text(BRONZE)
    (root / "transform" / "gold" / "revenue.sql").write_text(GOLD)
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "tests" / "unit" / "revenue.yml").write_text(UNIT_TEST)
    (root / "contracts").mkdir()
    (root / "contracts" / "revenue.yml").write_text(CONTRACT)
    conn = duckdb.connect(str(root / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA landing")
    conn.execute(
        "CREATE TABLE landing.orders AS SELECT * FROM (VALUES "
        "(1, 'north', 10.0, 'paid'), (2, 'north', 5.0, 'cancelled'), "
        "(3, 'south', 7.0, 'paid'), (4, 'south', -2.0, 'paid')) t(id, region, amount, status)"
    )
    from havn.engine.transform import run_transform

    run_transform(conn, root / "transform", project_dir=root)
    conn.close()
    return root


@pytest.fixture
def project(tmp_path):
    return _make_project(tmp_path)


def _snapshot(project: Path) -> dict:
    conn = duckdb.connect(str(project / "warehouse.duckdb"), read_only=True)
    try:
        return {
            "revenue": conn.execute("SELECT * FROM gold.revenue ORDER BY 1").fetchall(),
            "bronze": conn.execute("SELECT COUNT(*) FROM bronze.orders").fetchone()[0],
            "state": conn.execute("SELECT COUNT(*) FROM _havn.model_state").fetchone()[0],
            "dbs": conn.execute("SELECT database_name FROM duckdb_databases()").fetchall(),
        }
    finally:
        conn.close()


def _verify(project: Path, cs_id: str):
    conn = duckdb.connect(str(project / "warehouse.duckdb"), read_only=True)
    try:
        return verify_and_store(project, cs_id, conn=conn)
    finally:
        conn.close()


def _check(report: dict, name: str) -> dict:
    return next(c for c in report["checks"] if c["name"] == name)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TestPaths:
    def test_allowed(self):
        assert check_path("transform/gold/x.sql") == "transform/gold/x.sql"
        assert check_path("transform\\gold\\x.sql") == "transform/gold/x.sql"
        assert check_path("tests/unit/a.yml") == "tests/unit/a.yml"
        assert check_path("metrics/m.yml") == "metrics/m.yml"

    @pytest.mark.parametrize("bad", [
        "../outside.sql", "/etc/passwd", "C:/x.sql", "project.yml", ".env",
        "transform/x.py", "ingest/a.py", "transform/../project.yml", "tests/b.yml", "",
    ])
    def test_rejected(self, bad):
        with pytest.raises(ChangeSetError):
            check_path(bad)


class TestStore:
    def test_create_drops_noops_and_classifies(self, project):
        cs = create_change_set(project, [
            {"path": "transform/bronze/orders.sql", "content": BRONZE},  # unchanged
            {"path": "transform/gold/new.sql", "content": "SELECT 1 AS x"},
            {"path": "transform/gold/revenue.sql", "content": GOLD + "\n-- edit\n"},
        ], source="test")
        actions = {f.path: f.action for f in cs.files}
        assert actions == {"transform/gold/new.sql": "create", "transform/gold/revenue.sql": "modify"}
        assert get_change_set(project, cs.id).files[1].base is not None
        assert [c.id for c in list_change_sets(project)] == [cs.id]

    def test_nothing_to_change(self, project):
        with pytest.raises(ChangeSetError):
            create_change_set(project, [{"path": "transform/bronze/orders.sql", "content": BRONZE}], source="t")

    def test_apply_requires_verification_unless_forced(self, project):
        cs = create_change_set(project, [{"path": "transform/gold/new.sql", "content": "SELECT 1 AS x"}], source="t")
        with pytest.raises(ChangeSetError, match="not been verified"):
            apply_change_set(project, cs.id)
        apply_change_set(project, cs.id, force=True, user="me")
        assert (project / "transform/gold/new.sql").read_text() == "SELECT 1 AS x"
        assert get_change_set(project, cs.id).status == "applied"

    def test_conflict_when_file_changed(self, project):
        cs = create_change_set(project, [
            {"path": "transform/gold/revenue.sql", "content": GOLD.replace("SUM", "MAX")},
        ], source="t")
        (project / "transform/gold/revenue.sql").write_text(GOLD + "\n-- someone else\n")
        with pytest.raises(ChangeSetConflict) as exc:
            apply_change_set(project, cs.id, force=True)
        assert exc.value.paths == ["transform/gold/revenue.sql"]
        assert "someone else" in (project / "transform/gold/revenue.sql").read_text()

    def test_apply_keeps_crlf(self, project):
        path = project / "transform/gold/revenue.sql"
        path.write_bytes(GOLD.replace("\n", "\r\n").encode())
        cs = create_change_set(project, [{"path": "transform/gold/revenue.sql", "content": GOLD.replace("SUM", "MAX")}], source="t")
        apply_change_set(project, cs.id, force=True)
        data = path.read_bytes()
        assert b"MAX(amount)" in data and b"\r\n" in data and b"\n\n" not in data.replace(b"\r\n", b"")

    def test_delete_and_discard(self, project):
        cs = create_change_set(project, [{"path": "contracts/revenue.yml", "content": None}], source="t")
        assert cs.files[0].action == "delete"
        discard_change_set(project, cs.id)
        assert get_change_set(project, cs.id).status == "discarded"
        with pytest.raises(ChangeSetError):
            apply_change_set(project, cs.id, force=True)
        assert (project / "contracts/revenue.yml").exists()

    def test_revise_bumps_revision(self, project):
        cs = create_change_set(project, [{"path": "transform/gold/new.sql", "content": "SELECT 1 AS x"}], source="t")
        cs2 = revise_change_set(project, cs.id, [{"path": "transform/gold/new.sql", "content": "SELECT 2 AS x"}])
        assert cs2.revision == 2 and cs2.files[0].content == "SELECT 2 AS x"


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class TestVerify:
    def test_passing_change_reports_diff_without_touching_warehouse(self, project):
        before = _snapshot(project)
        cs = create_change_set(project, [{
            "path": "transform/bronze/orders.sql",
            "content": BRONZE.rstrip() + "\nWHERE status = 'paid'\n",
        }], source="test")
        cs = _verify(project, cs.id)
        report = cs.report
        assert cs.status == "ready", report_text(cs)
        assert report["changed_models"] == ["bronze.orders"]
        assert report["affected_models"] == ["bronze.orders", "gold.revenue"]
        for name in ("safety", "validate", "bind", "unit_tests", "build", "assertions", "contracts", "data_diff"):
            assert _check(report, name)["status"] in ("pass", "skip"), name
        assert _check(report, "unit_tests")["status"] == "pass"
        diffs = {d["model"]: d for d in report["diffs"]}
        assert diffs["bronze.orders"]["total_before"] == 4
        assert diffs["bronze.orders"]["total_after"] == 3
        assert diffs["bronze.orders"]["removed"] == 1
        # gold.revenue was rebuilt from the scratch bronze, not the real one:
        # north drops from 15 to 10.
        assert diffs["gold.revenue"]["modified"] + diffs["gold.revenue"]["added"] >= 1
        assert _snapshot(project) == before
        assert (project / "transform/bronze/orders.sql").read_text() == BRONZE

    def test_failing_unit_test_and_contract(self, project):
        cs = create_change_set(project, [{
            "path": "transform/gold/revenue.sql",
            "content": GOLD.replace("SUM(amount)", "SUM(amount) - 100"),
        }], source="test")
        cs = _verify(project, cs.id)
        assert cs.status == "failed"
        assert _check(cs.report, "unit_tests")["status"] == "fail"
        assert _check(cs.report, "contracts")["status"] == "fail"
        assert _check(cs.report, "build")["status"] == "pass"

    def test_bind_error(self, project):
        cs = create_change_set(project, [{
            "path": "transform/gold/revenue.sql",
            "content": GOLD.replace("SUM(amount)", "SUM(no_such_column)"),
        }], source="test")
        cs = _verify(project, cs.id)
        assert cs.status == "failed"
        assert _check(cs.report, "bind")["status"] == "fail"
        assert _check(cs.report, "build")["status"] == "fail"

    def test_failing_assertion(self, project):
        cs = create_change_set(project, [{
            "path": "transform/gold/revenue.sql",
            "content": GOLD.replace("GROUP BY region", "WHERE false GROUP BY region"),
        }], source="test")
        cs = _verify(project, cs.id)
        assert _check(cs.report, "assertions")["status"] == "fail"

    def test_unsafe_sql_is_not_run(self, project):
        before = _snapshot(project)
        cs = create_change_set(project, [{
            "path": "transform/gold/revenue.sql",
            "content": "SELECT 1 AS x; DROP TABLE bronze.orders",
        }], source="test")
        cs = _verify(project, cs.id)
        assert cs.status == "failed"
        assert _check(cs.report, "safety")["status"] == "fail"
        build = next(b for b in cs.report["builds"] if b["model"] == "gold.revenue")
        assert build["status"] == "skipped"
        assert _snapshot(project) == before

    def test_file_reading_sql_is_rejected(self, project):
        cs = create_change_set(project, [{
            "path": "transform/gold/leak.sql",
            "content": "SELECT * FROM read_text('project.yml')",
        }], source="test")
        cs = _verify(project, cs.id)
        assert _check(cs.report, "safety")["status"] == "fail"
        assert all(b["status"] != "built" for b in cs.report["builds"] if b["model"] == "gold.leak")

    def test_new_model_and_cycle(self, project):
        cs = create_change_set(project, [{
            "path": "transform/gold/top.sql",
            "content": "@config materialized=table\nSELECT * FROM gold.revenue ORDER BY revenue DESC LIMIT 1\n",
        }], source="test")
        cs = _verify(project, cs.id)
        assert cs.status == "ready", report_text(cs)
        diff = next(d for d in cs.report["diffs"] if d["model"] == "gold.top")
        assert diff["is_new"] and diff["total_after"] == 1

        cyc = create_change_set(project, [{
            "path": "transform/bronze/orders.sql",
            "content": "@config materialized=table\nSELECT * FROM gold.revenue\n",
        }], source="test")
        cyc = _verify(project, cyc.id)
        assert cyc.status == "failed"
        assert _check(cyc.report, "validate")["status"] == "fail"

    def test_changed_unit_test_alone(self, project):
        bad = UNIT_TEST.replace("revenue: 15", "revenue: 16")
        cs = create_change_set(project, [{"path": "tests/unit/revenue.yml", "content": bad}], source="test")
        cs = _verify(project, cs.id)
        assert _check(cs.report, "unit_tests")["status"] == "fail"

    def test_changed_contract_on_unaffected_model_runs_on_warehouse(self, project):
        contract = CONTRACT.replace("revenue >= 0", "revenue > 1000")
        cs = create_change_set(project, [{"path": "contracts/revenue.yml", "content": contract}], source="test")
        cs = _verify(project, cs.id)
        check = _check(cs.report, "contracts")
        assert check["status"] == "fail"
        assert check["details"][0]["where"] == "warehouse"

    def test_no_warehouse_skips_data_checks(self, tmp_path):
        (tmp_path / "project.yml").write_text("name: x\n")
        (tmp_path / "transform" / "gold").mkdir(parents=True)
        cs = create_change_set(tmp_path, [{"path": "transform/gold/a.sql", "content": "SELECT 1 AS x"}], source="t")
        report = verify_change_set(tmp_path, cs, conn=None)
        assert _check(report, "build")["status"] == "skip"
        assert report["ok"]

    def test_verification_on_read_write_connection(self, project):
        cs = create_change_set(project, [{
            "path": "transform/bronze/orders.sql",
            "content": BRONZE.rstrip() + "\nWHERE amount > 0\n",
        }], source="test")
        conn = duckdb.connect(str(project / "warehouse.duckdb"))
        try:
            cs = verify_and_store(project, cs.id, conn=conn)
            dbs = [r[0] for r in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()]
        finally:
            conn.close()
        assert cs.status == "ready", report_text(cs)
        assert not any(d.startswith("_havn_verify") for d in dbs)


# ---------------------------------------------------------------------------
# Agent workspace
# ---------------------------------------------------------------------------


class TestWorkspace:
    def test_sync_and_diff(self, project, tmp_path_factory):
        (project / ".env").write_text("SECRET=1\n")
        ws = AgentWorkspace(project, tmp_path_factory.mktemp("ws"))
        try:
            ws.sync()
            assert (ws.root / "transform/gold/revenue.sql").exists()
            assert not (ws.root / ".env").exists()
            assert not (ws.root / "warehouse.duckdb").exists()
            (ws.root / "transform/gold/revenue.sql").write_text(GOLD.replace("SUM", "AVG"))
            (ws.root / "transform/gold/extra.sql").write_text("SELECT 1 AS one")
            (ws.root / "ingest").mkdir(exist_ok=True)
            (ws.root / "ingest/new.py").write_text("print(1)")
            (ws.root / "contracts/revenue.yml").unlink()
            proposals, ignored = ws.diff()
            by_path = {p["path"]: p["content"] for p in proposals}
            assert set(by_path) == {"transform/gold/revenue.sql", "transform/gold/extra.sql", "contracts/revenue.yml"}
            assert by_path["contracts/revenue.yml"] is None
            assert ignored == ["ingest/new.py"]
            # Re-sync keeps the pending proposal and drops everything else.
            ws.sync({"transform/gold/extra.sql": "SELECT 2 AS two"})
            assert (ws.root / "contracts/revenue.yml").exists()
            assert (ws.root / "transform/gold/extra.sql").read_text() == "SELECT 2 AS two"
            assert not (ws.root / "ingest/new.py").exists()
        finally:
            ws.close()
        assert not ws.root.exists()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture
def client(project):
    from fastapi.testclient import TestClient

    import havn.server.app as server_app
    from havn.server.deps import invalidate_config_cache, reset_shared_conn

    reset_shared_conn()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = False
    invalidate_config_cache()
    return TestClient(server_app.app)


class TestAPI:
    def test_submit_verify_apply(self, client, project):
        new_bronze = BRONZE.rstrip() + "\nWHERE status = 'paid'\n"
        resp = client.post("/api/changesets", json={
            "title": "only paid orders",
            "files": [{"path": "transform/bronze/orders.sql", "content": new_bronze}],
        })
        assert resp.status_code == 200, resp.text
        cs = resp.json()
        assert cs["status"] == "ready", json.dumps(cs["report"], indent=1)
        assert cs["report"]["ok"] and cs["stale_files"] == []
        listed = client.get("/api/changesets?open=true").json()["changesets"]
        assert [c["id"] for c in listed] == [cs["id"]]
        assert "content" not in listed[0]["files"][0]

        resp = client.post(f"/api/changesets/{cs['id']}/apply", json={})
        assert resp.status_code == 200, resp.text
        assert (project / "transform/bronze/orders.sql").read_text() == new_bronze
        assert client.get("/api/changesets?open=true").json()["changesets"] == []

    def test_failed_needs_force_and_conflict_is_409(self, client, project):
        bad = GOLD.replace("SUM(amount)", "SUM(amount) - 100")
        cs = client.post("/api/changesets", json={
            "files": [{"path": "transform/gold/revenue.sql", "content": bad}],
        }).json()
        assert cs["status"] == "failed"
        assert client.post(f"/api/changesets/{cs['id']}/apply", json={}).status_code == 400
        (project / "transform/gold/revenue.sql").write_text(GOLD + "\n-- edited meanwhile\n")
        resp = client.post(f"/api/changesets/{cs['id']}/apply", json={"force": True})
        assert resp.status_code == 409
        assert resp.json()["detail"]["paths"] == ["transform/gold/revenue.sql"]
        assert client.get(f"/api/changesets/{cs['id']}").json()["stale_files"] == ["transform/gold/revenue.sql"]

    def test_revise_and_reverify_and_discard(self, client):
        cs = client.post("/api/changesets", json={
            "files": [{"path": "transform/gold/revenue.sql", "content": GOLD.replace("SUM(amount)", "SUM(nope)")}],
        }).json()
        assert cs["status"] == "failed"
        cs2 = client.post("/api/changesets", json={
            "change_set_id": cs["id"],
            "files": [{"path": "transform/gold/revenue.sql", "content": GOLD.replace("SUM(amount)", "SUM(amount + 0)")}],
        }).json()
        assert cs2["id"] == cs["id"] and cs2["revision"] == 2 and cs2["status"] == "ready"
        assert client.post(f"/api/changesets/{cs['id']}/verify").json()["status"] == "ready"
        assert client.post(f"/api/changesets/{cs['id']}/discard").json()["status"] == "discarded"

    def test_rejects_paths_and_unknown_ids(self, client):
        resp = client.post("/api/changesets", json={"files": [{"path": "project.yml", "content": "x"}]})
        assert resp.status_code == 400
        assert client.get("/api/changesets/abcdef123456").status_code == 404
        assert client.get("/api/changesets/not-hex").status_code == 400


def test_diff_samples_are_masked_for_role(project):
    from havn.engine.masking import create_policy, ensure_masking_table

    rw = duckdb.connect(str(project / "warehouse.duckdb"))
    ensure_masking_table(rw)
    create_policy(rw, schema_name="bronze", table_name="orders", column_name="region",
                  method="redact", exempted_roles=["admin"])
    rw.close()
    cs = create_change_set(project, [{
        "path": "transform/bronze/orders.sql",
        "content": BRONZE.rstrip() + "\nWHERE status = 'paid'\n",
    }], source="test")
    conn = duckdb.connect(str(project / "warehouse.duckdb"), read_only=True)
    try:
        cs = verify_and_store(project, cs.id, conn=conn, role="viewer")
    finally:
        conn.close()
    diff = next(d for d in cs.report["diffs"] if d["model"] == "bronze.orders")
    assert diff["sample_removed"] and all(r["region"] != "north" for r in diff["sample_removed"])


# ---------------------------------------------------------------------------
# Agent sidebar review mode
# ---------------------------------------------------------------------------


class EditingAdapter:
    """A fake coding agent that edits files in whatever directory it is given."""

    name = "editor"
    display_name = "Editing Agent"
    instances: list = []

    def __init__(self):
        self.permission_mode = "auto"
        self.model = ""
        self.path = None
        self.prompt = None
        EditingAdapter.instances.append(self)

    async def start_session(self, project_path, system_prompt=None):
        self.path = Path(project_path)
        self.prompt = system_prompt

    async def send_message(self, message):
        target = self.path / "transform/gold/revenue.sql"
        if message == "break it":
            target.write_text(GOLD.replace("SUM(amount)", "SUM(amount) - 100"))
        else:
            target.write_text(GOLD.replace("SUM(amount)", "SUM(amount + 0)"))
        (self.path / "ingest").mkdir(exist_ok=True)
        (self.path / "ingest/side.py").write_text("print('x')")
        yield {"type": "text", "content": "edited"}
        yield {"type": "done", "content": ""}

    async def stop_session(self):
        pass

    @classmethod
    def is_available(cls):
        return True


def _receive_until(ws, predicate, limit=20):
    for _ in range(limit):
        msg = ws.receive_json()
        if predicate(msg):
            return msg
    raise AssertionError("message not received")


def test_review_mode_produces_verified_change_set(client, project):
    from havn.engine.agents import registry

    original = registry.AGENT_REGISTRY.copy()
    registry.AGENT_REGISTRY["editor"] = EditingAdapter
    EditingAdapter.instances.clear()
    try:
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json({"type": "start", "agent": "editor", "mode": "review"})
            assert ws.receive_json() == {"type": "ready", "agent": "editor", "mode": "review"}
            adapter = EditingAdapter.instances[-1]
            assert adapter.path != project and "Review mode" in adapter.prompt
            assert (adapter.path / "transform/gold/revenue.sql").exists()
            assert not (adapter.path / "warehouse.duckdb").exists()

            ws.send_json({"type": "message", "message": "break it"})
            first = _receive_until(ws, lambda m: m["type"] == "changeset")
            assert first["changeset"]["status"] == "verifying"
            assert first["changeset"]["ignored"] == ["ingest/side.py"]
            done = _receive_until(ws, lambda m: m["type"] == "changeset")
            cs_id = done["changeset"]["id"]
            assert done["changeset"]["status"] == "failed"

            # The project itself was never touched.
            assert (project / "transform/gold/revenue.sql").read_text() == GOLD
            assert not (project / "ingest/side.py").exists()

            # A follow-up revises the same change set.
            ws.send_json({"type": "message", "message": "fix it"})
            _receive_until(ws, lambda m: m["type"] == "changeset" and m["changeset"]["status"] == "verifying")
            fixed = _receive_until(ws, lambda m: m["type"] == "changeset")
            assert fixed["changeset"]["id"] == cs_id
            assert fixed["changeset"]["revision"] == 2
            assert fixed["changeset"]["status"] == "ready"
            workspace = adapter.path
        assert not workspace.exists()  # cleaned up on disconnect
    finally:
        registry.AGENT_REGISTRY.clear()
        registry.AGENT_REGISTRY.update(original)

    resp = client.post(f"/api/changesets/{cs_id}/apply", json={})
    assert resp.status_code == 200
    assert "SUM(amount + 0)" in (project / "transform/gold/revenue.sql").read_text()


def test_switching_into_review_restarts_in_workspace(client, project):
    from havn.engine.agents import registry

    original = registry.AGENT_REGISTRY.copy()
    registry.AGENT_REGISTRY["editor"] = EditingAdapter
    EditingAdapter.instances.clear()
    try:
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json({"type": "start", "agent": "editor", "mode": "auto"})
            assert ws.receive_json()["type"] == "ready"
            assert EditingAdapter.instances[-1].path == project
            ws.send_json({"type": "set_mode", "mode": "review"})
            assert ws.receive_json()["type"] == "ready"
            assert ws.receive_json() == {"type": "mode_changed", "mode": "review", "restarted": True}
            assert EditingAdapter.instances[-1].path != project
            ws.send_json({"type": "set_mode", "mode": "bogus"})
            assert ws.receive_json()["type"] == "error"
    finally:
        registry.AGENT_REGISTRY.clear()
        registry.AGENT_REGISTRY.update(original)


# ---------------------------------------------------------------------------
# MCP and CLI
# ---------------------------------------------------------------------------


def _mcp_call(server, name, arguments):
    resp = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments}})
    result = resp["result"]
    payload = None if result.get("isError") else json.loads(result["content"][0]["text"])
    return result, payload


def test_mcp_submit_and_iterate(project):
    from havn.mcp.server import MCPServer

    server = MCPServer(project)
    names = {t["name"] for t in server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
    assert {"submit_change_set", "get_change_set", "ask"} <= names

    _, payload = _mcp_call(server, "submit_change_set", {
        "title": "revenue minus a hundred",
        "files": [{"path": "transform/gold/revenue.sql", "content": GOLD.replace("SUM(amount)", "SUM(amount) - 100")}],
    })
    assert payload["status"] == "failed" and not payload["ok"]
    assert {f["name"] for f in payload["failures"]} >= {"unit_tests", "contracts"}
    assert "change_set_id" in payload["next"]

    _, payload2 = _mcp_call(server, "submit_change_set", {
        "change_set_id": payload["change_set_id"],
        "files": [
            {"path": "transform/gold/revenue.sql", "content": GOLD.replace("SUM(amount)", "MAX(amount)")},
            {"path": "tests/unit/revenue.yml", "content": UNIT_TEST.replace("revenue: 15", "revenue: 10")},
        ],
    })
    assert payload2["ok"], payload2["summary"]
    assert payload2["revision"] == 2

    _, got = _mcp_call(server, "get_change_set", {"change_set_id": payload["change_set_id"]})
    assert got["status"] == "ready"
    # MCP never applies: the project is unchanged.
    assert (project / "transform/gold/revenue.sql").read_text() == GOLD

    result, _ = _mcp_call(server, "submit_change_set", {"files": [{"path": ".env", "content": "X=1"}]})
    assert result["isError"]


def test_changes_cli(project):
    from typer.testing import CliRunner

    from havn.cli import app

    cs = create_change_set(project, [{"path": "transform/gold/revenue.sql",
                                      "content": GOLD.replace("SUM(amount)", "SUM(amount + 0)")}], source="t")
    runner = CliRunner()
    r = runner.invoke(app, ["changes", "--project", str(project)])
    assert cs.id in r.output
    r = runner.invoke(app, ["changes", "verify", cs.id, "--project", str(project)])
    assert r.exit_code == 0, r.output
    assert "[pass] unit_tests" in r.output
    r = runner.invoke(app, ["changes", "show", cs.id, "--diff", "--project", str(project)])
    assert "SUM(amount + 0)" in r.output
    r = runner.invoke(app, ["changes", "apply", cs.id, "--project", str(project)])
    assert r.exit_code == 0, r.output
    assert "SUM(amount + 0)" in (project / "transform/gold/revenue.sql").read_text()
