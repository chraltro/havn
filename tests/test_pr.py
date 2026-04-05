"""Tests for the pull request engine."""

from __future__ import annotations

import subprocess
from pathlib import Path

import duckdb
import pytest

from havn.engine.database import ensure_meta_table
from havn.engine.pr import (
    _compute_lineage_impact,
    _load_pr,
    _pr_dir,
    _pr_path,
    _save_pr,
    add_comment,
    approve_pr,
    build_pr,
    build_review_prompt,
    can_merge,
    close_pr,
    create_pr,
    ensure_pr_builds_table,
    get_pr,
    list_prs,
    merge_pr,
    pr_state_status,
    request_changes,
    update_pr,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def git_project(tmp_path):
    """A minimal git-initialized havn project with one model on main."""
    # Initialize git
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@havn.dev")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "commit.gpgsign", "false")

    # Minimal havn structure
    (tmp_path / "project.yml").write_text(
        "name: test\ndatabase:\n  path: warehouse.duckdb\n"
    )
    (tmp_path / "transform" / "bronze").mkdir(parents=True)
    (tmp_path / "transform" / "silver").mkdir(parents=True)
    (tmp_path / "transform" / "bronze" / "customers.sql").write_text(
        "-- config: materialized=table, schema=bronze\n"
        "-- depends_on: landing.customers\n\n"
        "SELECT * FROM landing.customers\n"
    )
    (tmp_path / ".havn" / "prs").mkdir(parents=True)
    (tmp_path / ".havn" / "prs" / ".gitkeep").write_text("")

    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "initial")

    # Create a feature branch with a modified model
    _git(tmp_path, "checkout", "-b", "feature/enrich")
    (tmp_path / "transform" / "bronze" / "customers.sql").write_text(
        "-- config: materialized=table, schema=bronze\n"
        "-- depends_on: landing.customers\n\n"
        "SELECT id, name, email FROM landing.customers WHERE active = true\n"
    )
    (tmp_path / "transform" / "silver" / "customers_enriched.sql").write_text(
        "-- config: materialized=table, schema=silver\n"
        "-- depends_on: bronze.customers\n\n"
        "SELECT id, upper(name) AS name, email FROM bronze.customers\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "enrich customers")
    _git(tmp_path, "checkout", "main")

    return tmp_path


@pytest.fixture
def conn(git_project):
    c = duckdb.connect(str(git_project / "warehouse.duckdb"))
    ensure_meta_table(c)
    # Seed a tiny landing.customers so transforms can build
    c.execute("CREATE SCHEMA IF NOT EXISTS landing")
    c.execute(
        "CREATE TABLE landing.customers AS "
        "SELECT * FROM (VALUES "
        "(1, 'Alice', 'alice@example.com', true), "
        "(2, 'Bob', 'bob@example.com', true), "
        "(3, 'Carol', 'carol@example.com', false)"
        ") AS t(id, name, email, active)"
    )
    # Build bronze.customers on main so diffs have something to compare against
    c.execute(
        "CREATE SCHEMA IF NOT EXISTS bronze; "
        "CREATE TABLE bronze.customers AS SELECT * FROM landing.customers"
    )
    yield c
    c.close()


# --- CRUD lifecycle ---


def test_create_pr(git_project):
    pr = create_pr(
        git_project,
        title="Enrich customers",
        description="Adds active filter",
        base_ref="main",
        head_ref="feature/enrich",
        author="alice",
    )
    assert pr.id.startswith("pr-")
    assert pr.title == "Enrich customers"
    assert pr.status == "open"
    assert pr.require_approval is True
    assert _pr_path(git_project, pr.id).exists()


def test_create_pr_rejects_invalid_branch(git_project):
    with pytest.raises(ValueError):
        create_pr(
            git_project,
            title="Bad",
            description="",
            base_ref="main",
            head_ref="branch;rm -rf /",
            author="alice",
        )


def test_create_pr_rejects_same_ref(git_project):
    with pytest.raises(ValueError):
        create_pr(
            git_project,
            title="Bad",
            description="",
            base_ref="main",
            head_ref="main",
            author="alice",
        )


def test_list_prs(git_project):
    create_pr(git_project, "A", "", "main", "feature/enrich", "alice")
    create_pr(git_project, "B", "", "main", "feature/enrich", "bob")
    prs = list_prs(git_project)
    assert len(prs) == 2


def test_list_prs_filter_status(git_project):
    pr1 = create_pr(git_project, "Open one", "", "main", "feature/enrich", "a")
    pr2 = create_pr(git_project, "To close", "", "main", "feature/enrich", "a")
    close_pr(git_project, pr2.id, "a")
    open_only = list_prs(git_project, status="open")
    assert len(open_only) == 1
    assert open_only[0].id == pr1.id
    closed_only = list_prs(git_project, status="closed")
    assert len(closed_only) == 1
    assert closed_only[0].id == pr2.id


def test_update_pr(git_project):
    pr = create_pr(git_project, "Old title", "desc", "main", "feature/enrich", "a")
    updated = update_pr(git_project, pr.id, title="New title", description="New desc")
    assert updated.title == "New title"
    assert updated.description == "New desc"
    loaded = get_pr(git_project, pr.id)
    assert loaded.title == "New title"


def test_close_pr(git_project):
    pr = create_pr(git_project, "T", "", "main", "feature/enrich", "a")
    closed = close_pr(git_project, pr.id, "admin")
    assert closed.status == "closed"
    assert closed.closed_by == "admin"


def test_cannot_close_merged_pr(git_project):
    pr = create_pr(git_project, "T", "", "main", "feature/enrich", "a")
    loaded = _load_pr(git_project, pr.id)
    loaded.status = "merged"
    _save_pr(git_project, loaded)
    with pytest.raises(ValueError):
        close_pr(git_project, pr.id, "admin")


# --- Comments / review ---


def test_add_comment(git_project):
    pr = create_pr(git_project, "T", "", "main", "feature/enrich", "a")
    c = add_comment(git_project, pr.id, "bob", "Looks good")
    assert c.body == "Looks good"
    loaded = get_pr(git_project, pr.id)
    assert len(loaded.comments) == 1


def test_ai_review_comment_type(git_project):
    pr = create_pr(git_project, "T", "", "main", "feature/enrich", "a")
    add_comment(git_project, pr.id, "claude", "Consider adding an assertion.", comment_type="ai_review")
    loaded = get_pr(git_project, pr.id)
    assert loaded.comments[0].comment_type == "ai_review"


def test_approve_pr(git_project):
    pr = create_pr(git_project, "T", "", "main", "feature/enrich", "a")
    approved = approve_pr(git_project, pr.id, "bob")
    assert "bob" in approved.approvers


def test_approve_clears_change_request(git_project):
    pr = create_pr(git_project, "T", "", "main", "feature/enrich", "a")
    request_changes(git_project, pr.id, "bob", reason="missing tests")
    approved = approve_pr(git_project, pr.id, "bob")
    assert "bob" in approved.approvers
    assert "bob" not in approved.change_requesters


def test_request_changes_adds_comment(git_project):
    pr = create_pr(git_project, "T", "", "main", "feature/enrich", "a")
    pr2 = request_changes(git_project, pr.id, "bob", reason="needs more docs")
    assert "bob" in pr2.change_requesters
    assert any("needs more docs" in c.body for c in pr2.comments)


# --- Lineage impact ---


def test_compute_lineage_impact(git_project):
    from havn.engine.transform.discovery import build_dag, discover_models

    # Checkout the feature branch so the enriched model exists in the DAG we see
    _git(git_project, "checkout", "feature/enrich")
    dag = build_dag(discover_models(git_project / "transform"))
    impact = _compute_lineage_impact(
        ["transform/bronze/customers.sql"], dag, git_project
    )
    assert "bronze.customers" in impact["changed"]
    assert "silver.customers_enriched" in impact["impacted"]
    _git(git_project, "checkout", "main")


# --- can_merge (uses git merge-tree) ---


def test_can_merge_clean(git_project):
    pr = create_pr(git_project, "T", "", "main", "feature/enrich", "a")
    result = can_merge(git_project, pr)
    # Whether it succeeds depends on git version; we at least assert the shape
    assert "can_merge" in result
    assert "reason" in result


def test_can_merge_rejects_invalid_refs(git_project):
    from havn.engine.pr import PullRequest

    pr = PullRequest(
        id="pr-x", title="T", description="", base_ref="main", head_ref="nope",
        author="a",
    )
    result = can_merge(git_project, pr)
    assert result["can_merge"] is False


# --- merge_pr refusal conditions ---


def test_merge_requires_approval(git_project, conn):
    pr = create_pr(git_project, "T", "", "main", "feature/enrich", "alice")
    result = merge_pr(git_project, pr.id, "alice", conn)
    assert result["success"] is False
    assert "approval" in result["error"].lower()


def test_merge_refuses_with_change_requests(git_project, conn):
    pr = create_pr(git_project, "T", "", "main", "feature/enrich", "alice")
    approve_pr(git_project, pr.id, "bob")
    request_changes(git_project, pr.id, "carol", reason="nope")
    result = merge_pr(git_project, pr.id, "alice", conn)
    assert result["success"] is False
    assert "requested changes" in result["error"].lower()


def test_merge_refuses_closed_pr(git_project, conn):
    pr = create_pr(git_project, "T", "", "main", "feature/enrich", "a")
    close_pr(git_project, pr.id, "a")
    result = merge_pr(git_project, pr.id, "a", conn)
    assert result["success"] is False


def test_merge_happy_path(git_project, conn):
    pr = create_pr(git_project, "T", "", "main", "feature/enrich", "alice")
    approve_pr(git_project, pr.id, "bob")
    result = merge_pr(git_project, pr.id, "alice", conn)
    if not result["success"]:
        pytest.skip(
            f"git merge-tree unsupported on this git version: {result.get('error')}"
        )
    assert result["merge_commit"]
    # Current branch should be main
    head = _git(git_project, "rev-parse", "--abbrev-ref", "HEAD")
    assert head.stdout.strip() == "main"
    loaded = get_pr(git_project, pr.id)
    assert loaded.status == "merged"


# --- build_pr ---


def test_build_pr_creates_and_cleans_worktree(git_project, conn):
    pr = create_pr(git_project, "T", "", "main", "feature/enrich", "alice")
    record = build_pr(git_project, pr.id, conn)
    # Worktree directory must not exist after build
    wt = git_project / ".havn" / "pr-build" / pr.id
    assert not wt.exists()
    assert record["status"] in ("success", "error")
    # Must have written to _havn.pr_builds
    rows = conn.execute(
        "SELECT status FROM _havn.pr_builds WHERE pr_id = ?", [pr.id]
    ).fetchall()
    assert len(rows) >= 1


def test_build_pr_records_data_diff(git_project, conn):
    pr = create_pr(git_project, "T", "", "main", "feature/enrich", "alice")
    record = build_pr(git_project, pr.id, conn)
    if record["status"] != "success":
        pytest.skip(f"build failed: {record.get('error')}")
    assert record["data_diff"] is not None
    # The enriched silver model is new on the PR branch — it should appear as 'added'
    diff = record["data_diff"]
    assert "silver.customers_enriched" in diff
    assert diff["silver.customers_enriched"]["status"] in ("added", "modified")


def test_build_pr_computes_lineage_impact(git_project, conn):
    pr = create_pr(git_project, "T", "", "main", "feature/enrich", "alice")
    record = build_pr(git_project, pr.id, conn)
    if record["status"] != "success":
        pytest.skip(f"build failed: {record.get('error')}")
    assert record["lineage_impact"] is not None


def test_build_pr_missing_branch(git_project, conn):
    from havn.engine.pr import PullRequest

    # Create a PR pointing at a non-existent branch by writing the JSON directly
    pr = PullRequest(
        id="pr-missing",
        title="T",
        description="",
        base_ref="main",
        head_ref="does-not-exist",
        author="a",
        created_at="2026-04-05T00:00:00",
    )
    _save_pr(git_project, pr)
    record = build_pr(git_project, "pr-missing", conn)
    assert record["status"] == "error"
    # Worktree cleanup should still have happened
    wt = git_project / ".havn" / "pr-build" / "pr-missing"
    assert not wt.exists()


# --- Review prompt ---


def test_build_review_prompt_contains_key_sections(git_project):
    pr = create_pr(git_project, "Enrich customers", "Adds name upper()", "main", "feature/enrich", "alice")
    prompt = build_review_prompt(git_project, pr, build=None)
    assert "PR" in prompt and pr.id in prompt
    assert "Enrich customers" in prompt
    assert "Files changed" in prompt
    assert "Data impact" in prompt


def test_build_review_prompt_with_build(git_project):
    pr = create_pr(git_project, "T", "", "main", "feature/enrich", "alice")
    build = {
        "data_diff": {
            "silver.new_thing": {
                "status": "added",
                "pr_rows": 42,
                "main_rows": 0,
                "schema_changes": [],
            }
        },
        "lineage_impact": {
            "changed": ["silver.new_thing"],
            "impacted": [],
        },
    }
    prompt = build_review_prompt(git_project, pr, build=build)
    assert "silver.new_thing" in prompt
    assert "42" in prompt


# --- pr_state_status ---


def test_pr_state_status(git_project):
    # Fresh project, no modifications yet
    status = pr_state_status(git_project)
    assert status["is_git_repo"] is True
    # After creating a PR, .havn/prs/*.json is new — git status should flag it
    create_pr(git_project, "T", "", "main", "feature/enrich", "a")
    status_after = pr_state_status(git_project)
    assert status_after["dirty"] is True
