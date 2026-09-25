"""Tests for the deploy API (targets, plan, start, history)."""
from __future__ import annotations

import time

import duckdb
import pytest
from fastapi.testclient import TestClient

from tests.test_deploy import ORDERS, OTHER, TOTALS, _commit, _git


@pytest.fixture
def project(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@havn.dev")
    _git(tmp_path, "config", "user.name", "T")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    _commit(tmp_path, {
        "project.yml": (
            "name: d\ndatabase:\n  path: dev.duckdb\n"
            "environments:\n  dev:\n    database:\n      path: dev.duckdb\n"
            "  prod:\n    database:\n      path: prod.duckdb\n"
        ),
        ".gitignore": "*.duckdb\n*.wal\n.havn/\n_snapshots/\n",
        "transform/bronze/orders.sql": ORDERS,
        "transform/gold/totals.sql": TOTALS,
        "transform/silver/other.sql": OTHER,
    }, "init")
    for db in ("dev.duckdb", "prod.duckdb"):
        conn = duckdb.connect(str(tmp_path / db))
        conn.execute("CREATE SCHEMA landing")
        conn.execute("CREATE TABLE landing.orders AS SELECT * FROM (VALUES (1, 5.0), (2, 20.0)) t(id, amount)")
        conn.close()
    return tmp_path


@pytest.fixture
def client(project):
    import havn.server.app as server_app
    from havn.server.deps import reset_shared_conn

    reset_shared_conn()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = False
    yield TestClient(server_app.app)
    reset_shared_conn()


def _wait(client, deploy_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        resp = client.get(f"/api/deploys/{deploy_id}")
        assert resp.status_code == 200, resp.text
        rec = resp.json()
        if rec["status"] != "running":
            return rec
        time.sleep(0.2)
    raise AssertionError("deploy did not finish")


def test_targets_list_environments(client):
    data = client.get("/api/deploy/targets").json()
    envs = {e["name"]: e for e in data["environments"]}
    assert envs["dev"]["active"] is True and envs["dev"]["production"] is False
    assert envs["prod"]["active"] is False and envs["prod"]["production"] is True
    assert data["default_ref"] == "main"


def test_plan_then_deploy_to_another_environment(client, project):
    plan = client.get("/api/deploy/plan", params={"env": "prod", "ref": "main"}).json()
    assert set(plan["models"]) == {"bronze.orders", "gold.totals", "silver.other"}

    rec = client.post("/api/deploys", json={"env": "prod", "ref": "main", "pr_id": "pr-1"}).json()
    assert rec["status"] == "running"
    done = _wait(client, rec["id"])
    assert done["status"] == "success", done
    assert done["deployed_by"] == "local"

    conn = duckdb.connect(str(project / "prod.duckdb"), read_only=True)
    assert conn.execute("SELECT n, total FROM gold.totals").fetchone() == (2, 25.0)
    conn.close()

    assert client.get("/api/deploy/plan", params={"env": "prod", "ref": "main"}).json()["models"] == []
    assert [d["id"] for d in client.get("/api/deploys", params={"pr_id": "pr-1"}).json()] == [rec["id"]]


def test_deploy_to_the_active_environment(client):
    rec = client.post("/api/deploys", json={"env": "dev", "ref": "main"}).json()
    done = _wait(client, rec["id"])
    assert done["status"] == "success", done
    q = client.post("/api/query", json={"sql": "SELECT total FROM gold.totals"}).json()
    assert float(q["rows"][0][0]) == 25.0


def test_unknown_environment_or_ref(client):
    assert client.get("/api/deploy/plan", params={"env": "qa", "ref": "main"}).status_code == 404
    assert client.post("/api/deploys", json={"env": "qa", "ref": "main"}).status_code == 404
    resp = client.get("/api/deploy/plan", params={"env": "prod", "ref": "nope"})
    assert resp.status_code == 400 and "Unknown ref" in resp.json()["detail"]
