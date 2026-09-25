"""Tests for the pull request REST API."""

from __future__ import annotations

import subprocess
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def project(tmp_path):
    """Git-initialized havn project with a minimal transform model and a feature branch."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@havn.dev")
    _git(tmp_path, "config", "user.name", "T")
    _git(tmp_path, "config", "commit.gpgsign", "false")

    (tmp_path / "project.yml").write_text(
        "name: test\ndatabase:\n  path: warehouse.duckdb\n"
    )
    (tmp_path / "transform" / "bronze").mkdir(parents=True)
    (tmp_path / "transform" / "bronze" / "customers.sql").write_text(
        "-- config: materialized=table, schema=bronze\n"
        "-- depends_on: landing.customers\n\n"
        "SELECT * FROM landing.customers\n"
    )
    (tmp_path / ".havn" / "prs").mkdir(parents=True)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "transform" / "bronze" / "customers.sql").write_text(
        "-- config: materialized=table, schema=bronze\n"
        "-- depends_on: landing.customers\n\n"
        "SELECT id, name FROM landing.customers\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "change")
    _git(tmp_path, "checkout", "main")

    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
    conn.execute(
        "CREATE TABLE landing.customers AS SELECT 1 AS id, 'A' AS name, 'a@x' AS email"
    )
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


def test_api_list_prs_empty(client):
    resp = client.get("/api/prs")
    assert resp.status_code == 200
    assert resp.json() == []


def test_api_create_and_get_pr(client):
    resp = client.post(
        "/api/prs",
        json={
            "title": "Test PR",
            "description": "testing",
            "base_ref": "main",
            "head_ref": "feature/x",
            "author": "alice",
        },
    )
    assert resp.status_code == 200
    pr = resp.json()
    assert pr["id"].startswith("pr-")
    assert pr["title"] == "Test PR"
    assert pr["status"] == "open"

    resp = client.get(f"/api/prs/{pr['id']}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Test PR"


def test_api_create_rejects_bad_branch(client):
    resp = client.post(
        "/api/prs",
        json={
            "title": "Bad",
            "description": "",
            "base_ref": "main",
            "head_ref": "branch with space",
            "author": "a",
        },
    )
    assert resp.status_code == 400


def test_api_update_pr(client):
    pr = client.post(
        "/api/prs",
        json={"title": "Old", "description": "", "base_ref": "main", "head_ref": "feature/x"},
    ).json()
    resp = client.patch(
        f"/api/prs/{pr['id']}",
        json={"title": "New"},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "New"


def test_api_close_pr(client):
    pr = client.post(
        "/api/prs",
        json={"title": "T", "description": "", "base_ref": "main", "head_ref": "feature/x"},
    ).json()
    resp = client.post(f"/api/prs/{pr['id']}/close", json={"user": "alice"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "closed"


def test_api_add_and_list_comments(client):
    pr = client.post(
        "/api/prs",
        json={"title": "T", "description": "", "base_ref": "main", "head_ref": "feature/x"},
    ).json()

    resp = client.post(
        f"/api/prs/{pr['id']}/comments",
        json={"body": "LGTM", "author": "bob"},
    )
    assert resp.status_code == 200

    resp = client.get(f"/api/prs/{pr['id']}/comments")
    assert resp.status_code == 200
    comments = resp.json()
    assert len(comments) == 1
    assert comments[0]["body"] == "LGTM"


def test_api_approve_pr(client):
    pr = client.post(
        "/api/prs",
        json={"title": "T", "description": "", "base_ref": "main", "head_ref": "feature/x"},
    ).json()
    resp = client.post(f"/api/prs/{pr['id']}/approve", json={"reviewer": "bob"})
    assert resp.status_code == 200
    assert "bob" in resp.json()["approvers"]


def test_api_request_changes(client):
    pr = client.post(
        "/api/prs",
        json={"title": "T", "description": "", "base_ref": "main", "head_ref": "feature/x"},
    ).json()
    resp = client.post(
        f"/api/prs/{pr['id']}/request-changes",
        json={"reviewer": "carol", "reason": "tests missing"},
    )
    assert resp.status_code == 200
    assert "carol" in resp.json()["change_requesters"]


def test_api_review_prompt(client):
    pr = client.post(
        "/api/prs",
        json={
            "title": "Test review",
            "description": "testing the prompt",
            "base_ref": "main",
            "head_ref": "feature/x",
        },
    ).json()
    resp = client.get(f"/api/prs/{pr['id']}/review-prompt")
    assert resp.status_code == 200
    text = resp.text
    assert pr["id"] in text
    assert "Test review" in text
    assert "Files changed" in text


def test_api_pr_diff(client):
    pr = client.post(
        "/api/prs",
        json={"title": "T", "description": "", "base_ref": "main", "head_ref": "feature/x"},
    ).json()
    resp = client.get(f"/api/prs/{pr['id']}/diff")
    assert resp.status_code == 200
    data = resp.json()
    assert "files" in data
    # transform/bronze/customers.sql changed between main and feature/x
    assert any("customers.sql" in f for f in data["files"])


def test_api_state_status(client):
    resp = client.get("/api/prs/state-status")
    assert resp.status_code == 200
    data = resp.json()
    assert "dirty" in data
    assert "is_git_repo" in data


def test_api_state_status_not_shadowed_by_pr_route(client):
    """Ensure /api/prs/state-status isn't accidentally matched by /api/prs/{pr_id}."""
    resp = client.get("/api/prs/state-status")
    assert resp.status_code == 200
    # If the parameterized route shadowed it, we'd get 404 "PR 'state-status' not found"
    assert "is_git_repo" in resp.json()


def test_api_pr_not_found(client):
    resp = client.get("/api/prs/nonexistent")
    assert resp.status_code == 404


def test_api_build_starts_background(client):
    pr = client.post(
        "/api/prs",
        json={"title": "T", "description": "", "base_ref": "main", "head_ref": "feature/x"},
    ).json()
    resp = client.post(f"/api/prs/{pr['id']}/build")
    assert resp.status_code == 200
    assert resp.json()["status"] == "started"


def test_api_lineage_impact_covers_package_models(client, project):
    """A package model reading a changed project model is part of the impact.

    Package checkouts are gitignored, so they never show up in the diff
    themselves; they matter because they read what the diff changed.
    """
    pkg = project / "havn_packages" / "crm" / "transform" / "silver"
    pkg.mkdir(parents=True)
    (project / "havn_packages" / "crm" / "havn_package.yml").write_text(
        "name: crm\nversion: 1.0.0\n"
    )
    (pkg / "enriched.sql").write_text(
        "@config materialized=table, schema=silver\n\n"
        "SELECT * FROM bronze.customers\n"
    )
    (project / "havn_packages.lock").write_text(
        "packages:\n"
        "  - name: crm\n"
        "    source: path\n"
        "    path: ../crm\n"
        "    rev: local\n"
        "    commit: local\n"
    )

    pr = client.post(
        "/api/prs",
        json={"title": "T", "description": "", "base_ref": "main", "head_ref": "feature/x"},
    ).json()
    resp = client.get(f"/api/prs/{pr['id']}/lineage-impact")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] == ["bronze.customers"]
    assert "crm_silver.enriched" in body["impacted"]


def test_api_merge_refuses_without_approval(client):
    pr = client.post(
        "/api/prs",
        json={"title": "T", "description": "", "base_ref": "main", "head_ref": "feature/x"},
    ).json()
    resp = client.post(f"/api/prs/{pr['id']}/merge", json={"user": "a"})
    # 400 with approval error message
    assert resp.status_code == 400
    assert "approval" in resp.text.lower()


# --- Ship review (GET /api/prs/{id}/review) ---


def _add_downstream_on_main(project: Path) -> None:
    """Commit a silver model reading bronze.customers, so the change has impact."""
    (project / "transform" / "silver").mkdir(parents=True, exist_ok=True)
    (project / "transform" / "silver" / "names.sql").write_text(
        "@config materialized=view, schema=silver\n\nSELECT name FROM bronze.customers\n"
    )
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "add silver")


def _ignore_warehouse(project: Path) -> None:
    """Commit the .gitignore a real `havn init` project has."""
    (project / ".gitignore").write_text("warehouse.duckdb\nwarehouse.duckdb.wal\n.havn/pr-build/\n")
    _git(project, "add", ".gitignore")
    _git(project, "commit", "-m", "ignore warehouse")


def _create_pr(client) -> dict:
    resp = client.post(
        "/api/prs",
        json={"title": "Trim customers", "base_ref": "main", "head_ref": "feature/x"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _gate(review: dict) -> dict:
    return {g["key"]: g for g in review["gate"]}


def test_review_before_build_and_approval(client, project):
    _ignore_warehouse(project)
    _add_downstream_on_main(project)
    pr = _create_pr(client)
    review = client.get(f"/api/prs/{pr['id']}/review").json()

    assert review["pr"]["title"] == "Trim customers"
    assert "transform/bronze/customers.sql" in review["files"]
    roles = {n["name"]: n["role"] for n in review["impact"]["nodes"]}
    # The changed model's direct upstream is drawn too, for context.
    assert roles == {
        "landing.customers": "upstream",
        "bronze.customers": "changed",
        "silver.names": "impacted",
    }
    assert review["impact"]["edges"] == [
        ["bronze.customers", "silver.names"],
        ["landing.customers", "bronze.customers"],
    ]

    gate = _gate(review)
    assert gate["build"]["state"] == "pending" and gate["build"]["required"] is False
    assert gate["approval"]["state"] == "pending"
    assert gate["changes"]["state"] == "pass"
    assert gate["conflicts"]["state"] == "pass"
    assert gate["clean"]["state"] == "pass"
    assert review["ready"] is False
    assert review["plan"][1] == "Check out main and merge feature/x with --no-ff"


def test_review_ready_after_approval_and_blocked_by_change_request(client, project):
    _ignore_warehouse(project)
    pr = _create_pr(client)
    client.post(f"/api/prs/{pr['id']}/approve", json={"reviewer": "ingrid"})
    review = client.get(f"/api/prs/{pr['id']}/review").json()
    assert _gate(review)["approval"]["detail"] == "Approved by ingrid."
    # The build is advisory: merge does not require it, so neither does ready.
    assert review["ready"] is True, review["gate"]

    client.post(f"/api/prs/{pr['id']}/request-changes", json={"reviewer": "mats", "reason": "no"})
    review = client.get(f"/api/prs/{pr['id']}/review").json()
    assert _gate(review)["changes"]["state"] == "fail"
    assert review["ready"] is False


def test_author_cannot_approve_own_change(client, project):
    pr = _create_pr(client)  # author defaults to "local"
    resp = client.post(f"/api/prs/{pr['id']}/approve", json={"reviewer": "local"})
    assert resp.status_code == 400
    assert "someone else has to approve" in resp.json()["detail"]
    review = client.get(f"/api/prs/{pr['id']}/review").json()
    assert _gate(review)["approval"]["state"] == "pending"
    assert _gate(review)["approval"]["detail"] == "Needs an approval from someone other than local."


def test_waiving_approval_lets_a_solo_author_ship(client, project):
    _ignore_warehouse(project)
    pr = _create_pr(client)
    client.patch(f"/api/prs/{pr['id']}", json={"require_approval": False})
    review = client.get(f"/api/prs/{pr['id']}/review").json()
    assert _gate(review)["approval"]["state"] == "pass"
    assert review["ready"] is True


def test_legacy_self_approval_does_not_count(client, project):
    """A PR file written before the rule may list its author as an approver."""
    import json as _json

    _ignore_warehouse(project)
    pr = _create_pr(client)
    path = project / ".havn" / "prs" / f"{pr['id']}.json"
    data = _json.loads(path.read_text())
    data["approvers"] = ["local"]
    path.write_text(_json.dumps(data))
    review = client.get(f"/api/prs/{pr['id']}/review").json()
    assert _gate(review)["approval"]["state"] == "pending"
    resp = client.post(f"/api/prs/{pr['id']}/merge", json={"user": "local"})
    assert resp.status_code == 400
    assert "other than its author" in resp.json()["detail"]


@pytest.fixture
def auth_client(project):
    """Auth on, with an admin (ada) and an editor (ed)."""
    import havn.server.app as server_app
    from havn.server.deps import reset_shared_conn

    reset_shared_conn()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = True
    tc = TestClient(server_app.app)
    admin = tc.post("/api/auth/setup", json={"username": "ada", "password": "adapass", "role": "admin"}).json()["token"]
    tc.post("/api/users", json={"username": "ed", "password": "edpass", "role": "editor"},
            headers={"Authorization": f"Bearer {admin}"})
    ed = tc.post("/api/auth/login", json={"username": "ed", "password": "edpass"}).json()["token"]
    tc.tokens = {"ada": {"Authorization": f"Bearer {admin}"}, "ed": {"Authorization": f"Bearer {ed}"}}
    yield tc
    server_app.AUTH_ENABLED = False
    reset_shared_conn()


def test_auth_uses_the_signed_in_user_not_the_claimed_one(auth_client):
    tc = auth_client
    pr = tc.post("/api/prs", json={"title": "T", "base_ref": "main", "head_ref": "feature/x", "author": "ada"},
                 headers=tc.tokens["ed"]).json()
    assert pr["author"] == "ed"  # the claim is ignored

    # ed claims to be ada to approve their own change: refused as ed.
    resp = tc.post(f"/api/prs/{pr['id']}/approve", json={"reviewer": "ada"}, headers=tc.tokens["ed"])
    assert resp.status_code == 400

    resp = tc.post(f"/api/prs/{pr['id']}/approve", json={"reviewer": "someone"}, headers=tc.tokens["ada"])
    assert resp.status_code == 200
    assert resp.json()["approvers"] == ["ada"]


def test_auth_only_admins_waive_approval(auth_client):
    tc = auth_client
    pr = tc.post("/api/prs", json={"title": "T", "base_ref": "main", "head_ref": "feature/x"},
                 headers=tc.tokens["ed"]).json()
    resp = tc.patch(f"/api/prs/{pr['id']}", json={"require_approval": False}, headers=tc.tokens["ed"])
    assert resp.status_code == 403
    resp = tc.patch(f"/api/prs/{pr['id']}", json={"require_approval": False}, headers=tc.tokens["ada"])
    assert resp.status_code == 200 and resp.json()["require_approval"] is False
    # Turning it back on needs no special role.
    resp = tc.patch(f"/api/prs/{pr['id']}", json={"require_approval": True}, headers=tc.tokens["ed"])
    assert resp.status_code == 200


def test_review_dirty_tree_blocks(client, project):
    _ignore_warehouse(project)
    pr = _create_pr(client)
    client.post(f"/api/prs/{pr['id']}/approve", json={"reviewer": "ingrid"})
    (project / "scratch.txt").write_text("wip")
    review = client.get(f"/api/prs/{pr['id']}/review").json()
    assert _gate(review)["clean"]["state"] == "fail"
    assert review["ready"] is False


def test_review_build_current_then_stale(client, project):
    from havn.engine.pr import build_pr
    from havn.server.deps import reset_shared_conn

    pr = _create_pr(client)
    reset_shared_conn()
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        record = build_pr(project, pr["id"], conn)
    finally:
        conn.close()
    assert record["status"] == "success", record.get("error")

    review = client.get(f"/api/prs/{pr['id']}/review").json()
    assert _gate(review)["build"]["state"] == "pass"
    assert review["build_current"] is True
    assert review["build"]["data_diff"]

    # A new commit on the branch makes that build stale.
    _git(project, "checkout", "feature/x")
    (project / "transform" / "bronze" / "customers.sql").write_text(
        "-- config: materialized=table, schema=bronze\n"
        "-- depends_on: landing.customers\n\n"
        "SELECT id FROM landing.customers\n"
    )
    _git(project, "commit", "-am", "more")
    _git(project, "checkout", "main")
    review = client.get(f"/api/prs/{pr['id']}/review").json()
    assert _gate(review)["build"]["state"] == "warn"
    assert review["build_current"] is False


def test_review_unknown_pr_is_404(client):
    assert client.get("/api/prs/pr-nope/review").status_code == 404
