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


def test_api_merge_refuses_without_approval(client):
    pr = client.post(
        "/api/prs",
        json={"title": "T", "description": "", "base_ref": "main", "head_ref": "feature/x"},
    ).json()
    resp = client.post(f"/api/prs/{pr['id']}/merge", json={"user": "a"})
    # 400 with approval error message
    assert resp.status_code == 400
    assert "approval" in resp.text.lower()
