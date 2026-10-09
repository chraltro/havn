"""A warehouse per git branch: resolution, build, status, diff, cleanup, server, CI.

Every test runs against a real git repository in tmp_path and real DuckDB
files: a base warehouse built on main, and branch warehouses built on top of
it through defer.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import duckdb
import pytest
import yaml

from havn.config import load_project
from havn.engine import branches as B
from havn.engine.database import ensure_meta_table, open_warehouse
from havn.engine.transform import run_transform


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _write(project: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        f = project / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")


def _commit(project: Path, files: dict[str, str], msg: str) -> None:
    _write(project, files)
    _git(project, "add", "-A")
    _git(project, "commit", "-m", msg)


ORDERS = "@config materialized=table\n\nSELECT * FROM landing.orders\n"
TOTALS = (
    "@config materialized=table, unique_key=id\n\n"
    "SELECT id, amount AS amount FROM bronze.orders\n"
)
TOTALS_CHANGED = (
    "@config materialized=table, unique_key=id\n\n"
    "SELECT id, amount * 2 AS amount, 'x' AS tag FROM bronze.orders WHERE id < 4\n"
)
BIG = "@config materialized=view\n\nSELECT id FROM silver.totals WHERE amount > 10\n"
REGIONS = "@config materialized=view\n\nSELECT id, region FROM bronze.orders\n"
PROJECT_YML = "name: b\ndatabase:\n  path: warehouse.duckdb\nbranches:\n  enabled: true\n"


def _init_repo(path: Path, branch: str = "main") -> None:
    _git(path, "init", "-b", branch)
    _git(path, "config", "user.email", "t@havn.dev")
    _git(path, "config", "user.name", "T")
    _git(path, "config", "commit.gpgsign", "false")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv(B.ENV_VAR, raising=False)
    root = tmp_path / "proj"
    root.mkdir()
    _init_repo(root)
    _commit(root, {
        "project.yml": PROJECT_YML,
        ".gitignore": "*.duckdb\n*.wal\n.havn/\n_snapshots/\n",
        "transform/bronze/orders.sql": ORDERS,
        "transform/silver/totals.sql": TOTALS,
        "transform/silver/big.sql": BIG,
        "transform/silver/regions.sql": REGIONS,
    }, "init")
    return root


def _seed_landing(path: Path) -> None:
    conn = duckdb.connect(str(path))
    conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
    conn.execute(
        "CREATE OR REPLACE TABLE landing.orders AS SELECT * FROM (VALUES "
        "(1, 5, 'eu'), (2, 20, 'us'), (3, 30, 'eu'), (4, 40, 'us'), (5, 50, 'eu')) t(id, amount, region)"
    )
    conn.close()


@pytest.fixture
def base(project):
    """The base warehouse, built from main."""
    _seed_landing(project / "warehouse.duckdb")
    cfg = load_project(project)
    assert not cfg.branch.active
    conn = open_warehouse(cfg)
    try:
        results = run_transform(conn, project / "transform", project_dir=project)
    finally:
        conn.close()
    assert set(results.values()) == {"built"}
    return project / "warehouse.duckdb"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _on_feature(project: Path, name: str = "feature/x", totals: str = TOTALS_CHANGED) -> None:
    _git(project, "checkout", "-b", name)
    if totals is not None:
        _commit(project, {"transform/silver/totals.sql": totals}, "change totals")


def _branch_build(project: Path, **kw):
    cfg = load_project(project)
    conn = open_warehouse(cfg)
    try:
        return cfg, B.build_branch(conn, cfg, **kw)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Git HEAD and names
# ---------------------------------------------------------------------------


def test_read_git_head_branch_detached_and_no_repo(project, tmp_path):
    head = B.read_git_head(project)
    assert head.in_repo and head.branch == "main" and not head.detached
    assert head.sha == _git(project, "rev-parse", "HEAD").stdout.strip()

    _git(project, "checkout", "--detach")
    head = B.read_git_head(project)
    assert head.detached and head.branch is None and head.sha
    assert head.key.startswith("detached:")

    plain = tmp_path / "plain"
    plain.mkdir()
    assert B.read_git_head(plain) == B.GitHead()
    assert B.read_git_head(plain).key == "none"


def test_read_git_head_in_a_subdirectory_and_a_worktree(project, tmp_path):
    sub = project / "transform"
    assert B.read_git_head(sub).branch == "main"

    wt = tmp_path / "wt"
    _git(project, "worktree", "add", "-b", "feature/wt", str(wt))
    head = B.read_git_head(wt)
    assert head.branch == "feature/wt"
    # Refs live in the main repository's common dir, so main is still detected.
    assert B.detect_main_branches(head) == ["main"]


def test_packed_refs_are_read(project):
    _git(project, "pack-refs", "--all")
    assert not (project / ".git" / "refs" / "heads" / "main").exists()
    head = B.read_git_head(project)
    assert head.sha == _git(project, "rev-parse", "HEAD").stdout.strip()
    assert B.detect_main_branches(head) == ["main"]


def test_master_is_the_main_branch_when_there_is_no_main(tmp_path, monkeypatch):
    monkeypatch.delenv(B.ENV_VAR, raising=False)
    root = tmp_path / "m"
    root.mkdir()
    _init_repo(root, "master")
    _commit(root, {"project.yml": PROJECT_YML}, "init")
    cfg = load_project(root)
    assert cfg.branch.main_branches == ["master"]
    assert not cfg.branch.active
    _git(root, "checkout", "-b", "work")
    assert load_project(root).branch.active


def test_branch_slug_is_safe_and_distinct():
    assert B.branch_slug("fix-orders") == "fix-orders"
    slash = B.branch_slug("feature/x")
    dash = B.branch_slug("feature-x")
    assert dash == "feature-x" and slash.startswith("feature-x-") and slash != dash
    assert B.branch_slug("Fix") != B.branch_slug("fix")
    assert B.branch_slug("con") != "con"
    weird = B.branch_slug("a/../../b:c*d")
    assert "/" not in weird and ".." not in weird and ":" not in weird
    assert len(B.branch_slug("x" * 200)) <= 60


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_disabled_projects_never_use_a_branch_warehouse(project):
    (project / "project.yml").write_text("name: b\ndatabase:\n  path: warehouse.duckdb\n")
    _git(project, "checkout", "-b", "feature/x")
    cfg = load_project(project)
    assert not cfg.branch.active
    assert "not enabled" in cfg.branch.reason
    assert cfg.database.path == "warehouse.duckdb"


def test_main_uses_the_base_and_a_feature_branch_its_own_file(project):
    cfg = load_project(project)
    assert not cfg.branch.active and "main branch" in cfg.branch.reason
    assert cfg.database.path == "warehouse.duckdb"

    _git(project, "checkout", "-b", "feature/x")
    cfg = load_project(project)
    assert cfg.branch.active
    assert cfg.branch.git_branch == "feature/x"
    assert cfg.database.path == f".havn/branches/{B.branch_slug('feature/x')}.duckdb"
    assert cfg.branch.base_path == "warehouse.duckdb"
    assert cfg.active_environment is None


def test_detached_head_and_no_repo_fall_back(project, tmp_path):
    _git(project, "checkout", "--detach")
    cfg = load_project(project)
    assert not cfg.branch.active and "detached" in cfg.branch.reason

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "project.yml").write_text(PROJECT_YML)
    cfg = load_project(plain)
    assert not cfg.branch.active and "not a git repository" in cfg.branch.reason


def test_explicit_environment_wins_and_havn_branch_forces(project, monkeypatch):
    (project / "project.yml").write_text(
        "name: b\ndatabase:\n  path: warehouse.duckdb\n"
        "environments:\n  dev:\n    database:\n      path: dev.duckdb\n"
        "  prod:\n    database:\n      path: prod.duckdb\n"
        "branches:\n  enabled: true\n  base: prod\n"
    )
    _git(project, "checkout", "-b", "feature/x")
    cfg = load_project(project)
    assert cfg.branch.active and cfg.branch.base == "prod"
    assert cfg.branch.base_path == "prod.duckdb"

    assert not load_project(project, env="dev").branch.active
    assert load_project(project, env="dev").database.path == "dev.duckdb"

    (project / ".havn-env").write_text("prod\n")
    cfg = load_project(project)
    assert not cfg.branch.active and ".havn-env" in cfg.branch.reason
    assert cfg.database.path == "prod.duckdb"

    # A forced branch (CI, --name) wins over .havn-env and the checkout.
    _git(project, "checkout", "--detach")
    monkeypatch.setenv(B.ENV_VAR, "pr/7")
    cfg = load_project(project)
    assert cfg.branch.active and cfg.branch.git_branch == "pr/7"
    assert cfg.branch.source == B.ENV_VAR
    assert load_project(project, branch="other").branch.git_branch == "other"
    assert not load_project(project, use_branches=False).branch.active


def test_base_override_and_validation(project, tmp_path):
    _git(project, "checkout", "-b", "feature/x")
    artifact = tmp_path / "artifact.duckdb"
    cfg = load_project(project, branch_base=str(artifact))
    assert cfg.branch.base_overridden and B.base_warehouse_path(cfg) == artifact

    (project / "project.yml").write_text(PROJECT_YML + "  base: nope\n")
    with pytest.raises(ValueError, match="unknown environment 'nope'"):
        load_project(project)
    (project / "project.yml").write_text(PROJECT_YML + "  path: .havn/fixed.duckdb\n")
    with pytest.raises(ValueError, match=r"\{branch\}"):
        load_project(project)
    (project / "project.yml").write_text(PROJECT_YML + "  main: [main, develop]\n")
    _git(project, "checkout", "-b", "develop")
    assert not load_project(project).branch.active


def test_ducklake_projects_do_not_get_branch_warehouses(project):
    (project / "project.yml").write_text(
        "name: b\ndatabase:\n  backend: ducklake\n  catalog: .havn/c.ducklake\n  data_path: .havn/data\n"
        "branches:\n  enabled: true\n"
    )
    _git(project, "checkout", "-b", "feature/x")
    cfg = load_project(project)
    assert not cfg.branch.active and "ducklake" in cfg.branch.reason


def test_branch_defers_to_its_base(project, base):
    from havn.engine.defer import DeferError, resolve_defer

    _git(project, "checkout", "-b", "feature/x")
    cfg = load_project(project)
    spec = resolve_defer(cfg, project)
    assert spec is not None and spec.path == base and spec.target == "base"
    assert resolve_defer(cfg, project, enabled=False) is None

    base.unlink()
    with pytest.raises(DeferError, match="no warehouse"):
        resolve_defer(cfg, project)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def test_build_materializes_only_what_changed_and_never_touches_the_base(project, base):
    before = _digest(base)
    _on_feature(project)
    cfg, result = _branch_build(project)

    # totals changed; big reads totals so it is downstream; orders and
    # regions did not change and stay in the base.
    assert result["plan"] == ["silver.totals", "silver.big"]
    assert result["failed"] == {}
    assert sorted(result["built"]) == ["silver.big", "silver.totals"]
    assert _digest(base) == before

    branch_file = B.branch_warehouse_path(cfg)
    assert branch_file.exists() and branch_file.parent == project / ".havn" / "branches"
    record = json.loads(branch_file.with_suffix(".branch.json").read_text())
    assert record["branch"] == "feature/x"

    conn = duckdb.connect(str(branch_file), read_only=True)
    try:
        tables = {
            f"{s}.{n}" for s, n in conn.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_catalog = current_database() AND table_schema NOT IN ('_havn')"
            ).fetchall()
        }
        assert tables == {"silver.totals", "silver.big"}
        assert conn.execute("SELECT sum(amount) FROM silver.totals").fetchone()[0] == 2 * (5 + 20 + 30)
        # silver.big reads only the local totals, so it stays a view and
        # works without the base attached.
        assert conn.execute("SELECT count(*) FROM silver.big").fetchone()[0] == 2
    finally:
        conn.close()

    # A second build has nothing new to do.
    _, again = _branch_build(project)
    assert again["built"] == [] and set(again["results"].values()) == {"skipped"}


def test_a_deferred_view_is_built_as_a_table_so_it_works_later(project, base):
    _git(project, "checkout", "-b", "feature/views")
    _commit(project, {"transform/silver/regions.sql": REGIONS.replace("region", "upper(region) AS region", 1)}, "regions")
    cfg, result = _branch_build(project)
    assert result["plan"] == ["silver.regions"]
    conn = duckdb.connect(str(B.branch_warehouse_path(cfg)), read_only=True)
    try:
        kind = conn.execute(
            "SELECT table_type FROM information_schema.tables "
            "WHERE table_schema = 'silver' AND table_name = 'regions'"
        ).fetchone()[0]
        assert kind == "BASE TABLE"
        assert conn.execute("SELECT count(*) FROM silver.regions WHERE region = 'EU'").fetchone()[0] == 3
    finally:
        conn.close()


def test_reverting_a_change_prunes_the_branch_copy(project, base):
    _on_feature(project)
    _branch_build(project)
    _commit(project, {"transform/silver/totals.sql": TOTALS}, "revert")

    cfg = load_project(project)
    conn = open_warehouse(cfg, read_only=True)
    try:
        plan = B.build_branch(conn, cfg, dry_run=True)
    finally:
        conn.close()
    assert plan["plan"] == [] and plan["prunable"] == ["silver.big", "silver.totals"]

    cfg, result = _branch_build(project)
    assert result["pruned"] == ["silver.big", "silver.totals"]
    conn = duckdb.connect(str(B.branch_warehouse_path(cfg)), read_only=True)
    try:
        left = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_catalog = current_database() AND table_schema = 'silver'"
        ).fetchone()[0]
        state = conn.execute("SELECT count(*) FROM _havn.model_state").fetchone()[0]
    finally:
        conn.close()
    assert left == 0 and state == 0


def test_build_needs_an_active_branch_and_a_base(project):
    cfg = load_project(project)
    conn = duckdb.connect()
    with pytest.raises(B.BranchError, match="Not on a branch warehouse"):
        B.build_branch(conn, cfg)
    _git(project, "checkout", "-b", "feature/x")
    cfg = load_project(project)
    with pytest.raises(B.BranchError, match="does not exist"):
        B.build_branch(conn, cfg)
    conn.close()


def test_a_never_built_base_plans_every_model(project):
    _seed_landing(project / "warehouse.duckdb")
    _git(project, "checkout", "-b", "feature/x")
    cfg, result = _branch_build(project)
    assert set(result["plan"]) == {"bronze.orders", "silver.totals", "silver.big", "silver.regions"}
    assert result["failed"] == {}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_status_reports_local_deferred_and_stale(project, base):
    _on_feature(project)
    cfg, _ = _branch_build(project)
    conn = open_warehouse(cfg, read_only=True)
    try:
        info = B.branch_status(conn, cfg)
    finally:
        conn.close()
    assert info["active"] and info["branch"] == "feature/x"
    assert [m["name"] for m in info["models"]["local"]] == ["silver.big", "silver.totals"]
    assert info["models"]["deferred"] == ["bronze.orders", "silver.regions"]
    assert info["models"]["needs_build"] == []
    assert info["base"]["readable"] and not info["stale"] and info["up_to_date"]

    # The base rebuilds bronze.orders after the branch built totals on it.
    _git(project, "checkout", "main")
    main_cfg = load_project(project)
    conn = open_warehouse(main_cfg)
    try:
        run_transform(conn, project / "transform", targets=["bronze.orders"], force=True, project_dir=project)
    finally:
        conn.close()
    _git(project, "checkout", "feature/x")
    cfg = load_project(project)
    conn = open_warehouse(cfg, read_only=True)
    try:
        info = B.branch_status(conn, cfg)
    finally:
        conn.close()
    stale = {m["name"]: m for m in info["models"]["local"]}
    assert stale["silver.totals"]["stale"]
    assert "bronze.orders was rebuilt in the base" in stale["silver.totals"]["stale_reasons"][0]
    assert not stale["silver.big"]["stale"]  # reads only the local totals
    assert info["stale"] and not info["up_to_date"]


def test_status_before_anything_is_built(project, base):
    _on_feature(project)
    cfg = load_project(project)
    conn = duckdb.connect()
    try:
        info = B.branch_status(conn, cfg)
    finally:
        conn.close()
    assert not info["warehouse"]["exists"]
    assert info["models"]["local"] == []
    assert info["models"]["needs_build"] == ["silver.totals", "silver.big"]


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def _diff(project: Path, **kw) -> dict:
    cfg = load_project(project)
    conn = open_warehouse(cfg, read_only=True)
    try:
        return B.diff_branch(conn, cfg, **kw)
    finally:
        conn.close()


def test_diff_reports_rows_and_schema_per_model(project, base):
    _on_feature(project)
    _branch_build(project)
    report = _diff(project)
    by = {e["model"]: e for e in report["models"]}
    assert set(by) == {"silver.totals", "silver.big"}

    totals = by["silver.totals"]
    assert totals["status"] == "changed"
    assert totals["primary_key"] == ["id"]
    assert (totals["before"], totals["after"]) == (5, 3)
    assert totals["removed"] == 2 and totals["modified"] == 3 and totals["added"] == 0
    assert totals["schema_changes"] == [
        {"column": "tag", "change": "added", "old_type": None, "new_type": "VARCHAR"}
    ]
    assert {r["id"] for r in totals["sample_removed"]} == {4, 5}

    big = by["silver.big"]
    assert big["status"] == "changed" and (big["before"], big["after"]) == (4, 2)
    assert report["summary"]["changed"] == 2
    assert report["commit"] == _git(project, "rev-parse", "HEAD").stdout.strip()
    assert B.diff_has_changes(report)


def test_diff_new_removed_and_not_built_models(project, base):
    _on_feature(project, totals=None)
    (project / "transform" / "silver" / "regions.sql").unlink()
    _commit(project, {"transform/gold/fresh.sql": "@config materialized=table\n\nSELECT 1 AS one\n"}, "new + removed")

    # Before a build, the new model is planned but not built.
    cfg = load_project(project)
    conn = duckdb.connect()
    try:
        early = B.diff_branch(conn, cfg)
    finally:
        conn.close()
    assert {e["model"]: e["status"] for e in early["models"]} == {
        "gold.fresh": "not_built", "silver.regions": "removed",
    }

    _branch_build(project)
    report = _diff(project)
    by = {e["model"]: e for e in report["models"]}
    assert by["gold.fresh"]["status"] == "added" and by["gold.fresh"]["after"] == 1
    assert by["silver.regions"]["status"] == "removed" and by["silver.regions"]["before"] == 5
    assert report["summary"] == {
        "added": 1, "changed": 0, "unchanged": 0, "removed": 1, "not_built": 0, "error": 0,
    }

    narrowed = _diff(project, models=["gold.fresh"])
    assert [e["model"] for e in narrowed["models"]] == ["gold.fresh"]
    with pytest.raises(B.BranchError, match="Unknown model"):
        _diff(project, models=["gold.nope"])


def test_markdown_comment(project, base):
    _on_feature(project)
    _branch_build(project)
    md = B.format_markdown(_diff(project))
    assert md.startswith(B.MARKDOWN_MARKER)
    assert "## havn data diff: `feature/x` vs `base`" in md
    assert "| `silver.totals` | changed | 5 → 3 | 0 | −2 | ~3 | +1 col |" in md
    assert "added column `tag` (VARCHAR)" in md
    assert "<details><summary>Sample rows (key: id)</summary>" in md


def test_markdown_escapes_cells_and_stays_under_the_limit():
    report = {
        "branch": "b", "base": "prod", "commit": None, "summary": {"changed": 1},
        "models": [{
            "model": "s.t", "status": "changed", "before": 1, "after": 1,
            "added": 1, "removed": 0, "modified": 0, "schema_changes": [],
            "primary_key": None, "error": None,
            "sample_added": [{"v": "a|b\nc<script>" + "x" * 50_000}] * 5,
            "sample_removed": [], "sample_modified": [],
        }],
    }
    md = B.format_markdown(report)
    assert "a\\|b c&lt;script&gt;" in md
    assert "<script>" not in md
    assert len(md) <= B.MARKDOWN_LIMIT

    empty = B.format_markdown({"branch": "b", "base": "prod", "summary": {}, "models": []})
    assert "No data changes" in empty


# ---------------------------------------------------------------------------
# List / reset / clean
# ---------------------------------------------------------------------------


def test_list_and_clean_merged_and_deleted_branches(project, base):
    _on_feature(project, "feature/merged")
    _branch_build(project)
    _git(project, "checkout", "main")
    _git(project, "merge", "--no-ff", "-m", "merge", "feature/merged")

    _on_feature(project, "feature/gone", totals=TOTALS.replace("amount AS", "amount + 1 AS"))
    _branch_build(project)
    _git(project, "checkout", "main")
    _git(project, "branch", "-D", "feature/gone")

    _on_feature(project, "feature/live", totals=TOTALS.replace("amount AS", "amount + 2 AS"))
    _branch_build(project)

    cfg = load_project(project)
    listing = {e["branch"]: e for e in B.list_branch_warehouses(cfg)}
    assert listing["feature/merged"]["git"] == "merged"
    assert listing["feature/gone"]["git"] == "gone"
    assert listing["feature/live"]["git"] == "current" and listing["feature/live"]["current"]

    dry = B.clean_branches(cfg, dry_run=True)
    assert {e["branch"] for e in dry["removed"]} == {"feature/merged", "feature/gone"}
    assert len(B.list_branch_warehouses(cfg)) == 3

    done = B.clean_branches(cfg)
    assert done["errors"] == {}
    assert [e["branch"] for e in B.list_branch_warehouses(cfg)] == ["feature/live"]
    assert base.exists()
    leftovers = sorted(p.name for p in (project / ".havn" / "branches").iterdir())
    slug = B.branch_slug("feature/live")
    assert leftovers == [f"{slug}.branch.json", f"{slug}.duckdb"]


def test_remove_refuses_anything_but_a_branch_warehouse(project, base):
    _git(project, "checkout", "-b", "feature/x")
    cfg = load_project(project)
    with pytest.raises(B.BranchError, match="not a branch warehouse"):
        B.remove_branch_warehouse(cfg, base)
    stray = project / "stray.duckdb"
    stray.write_bytes(b"")
    with pytest.raises(B.BranchError, match="does not match"):
        B.remove_branch_warehouse(cfg, stray)
    assert base.exists() and stray.exists()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def cli(monkeypatch):
    from typer.testing import CliRunner

    from havn.cli import app

    runner = CliRunner()

    def run(project: Path, *args: str):
        monkeypatch.chdir(project)
        return runner.invoke(app, list(args), catch_exceptions=False, env={"COLUMNS": "200"})

    return run


def test_cli_status_build_diff_markdown(project, base, cli, tmp_path):
    out = cli(project, "branch", "status")
    assert out.exit_code == 0 and "off" in out.output

    _on_feature(project)
    out = cli(project, "branch", "build", "--plan")
    assert out.exit_code == 0, out.output
    assert "build  silver.totals" in out.output

    out = cli(project, "branch", "build")
    assert out.exit_code == 0, out.output
    assert "2 built" in out.output

    out = cli(project, "branch", "status", "--json")
    info = json.loads(out.output)
    assert info["up_to_date"] and len(info["models"]["local"]) == 2

    md_file = tmp_path / "diff.md"
    out = cli(project, "branch", "diff", "--markdown", "--output", str(md_file))
    assert out.exit_code == 0, out.output
    assert md_file.read_text(encoding="utf-8").startswith(B.MARKDOWN_MARKER)

    out = cli(project, "branch", "diff", "--exit-nonzero-on-change")
    assert out.exit_code == 2 and "silver.totals" in out.output

    out = cli(project, "env", "show")
    assert out.exit_code == 0 and "Branch warehouse" in out.output

    out = cli(project, "branch", "list")
    assert out.exit_code == 0 and "feature/x" in out.output

    out = cli(project, "branch", "reset", "--yes")
    assert out.exit_code == 0
    assert not B.branch_warehouse_path(load_project(project)).exists()


def test_cli_transform_on_a_branch_defers_to_the_base(project, base, cli):
    _on_feature(project)
    out = cli(project, "transform", "silver.totals")
    assert out.exit_code == 0, out.output
    assert "defer" in out.output
    cfg = load_project(project)
    conn = duckdb.connect(str(B.branch_warehouse_path(cfg)), read_only=True)
    try:
        assert conn.execute("SELECT count(*) FROM silver.totals").fetchone()[0] == 3
    finally:
        conn.close()


def test_cli_ci_style_build_with_name_and_base_artifact(project, base, cli, tmp_path):
    """CI: detached checkout, base downloaded elsewhere, branch named explicitly."""
    import shutil

    _on_feature(project)
    _git(project, "checkout", "--detach")
    artifact = tmp_path / "ci-base" / "warehouse.duckdb"
    artifact.parent.mkdir()
    shutil.copy(base, artifact)
    base.unlink()  # CI has no local base: only the artifact

    out = cli(project, "branch", "build", "--name", "feature/x", "--base", str(artifact))
    assert out.exit_code == 0, out.output
    out = cli(project, "branch", "diff", "--name", "feature/x", "--base", str(artifact), "--markdown")
    assert out.exit_code == 0, out.output
    assert "`silver.totals` | changed" in out.output


def test_deploy_never_targets_a_branch_warehouse(project, base):
    _git(project, "checkout", "-b", "feature/x")
    assert load_project(project).database.path != "warehouse.duckdb"
    assert load_project(project, use_branches=False).database.path == "warehouse.duckdb"


def test_change_build_on_a_branch_diffs_against_main_not_the_branch(project, base):
    """The in-app PR build clones and diffs main's warehouse even when the
    connection it is handed is a branch warehouse."""
    from havn.engine.pr import build_pr, create_pr

    _on_feature(project)
    cfg = load_project(project)
    pr = create_pr(project, "Double totals", "", "main", "feature/x", "ada")
    conn = open_warehouse(cfg)
    try:
        ensure_meta_table(conn)
        record = build_pr(project, pr.id, conn)
    finally:
        conn.close()
    assert record["status"] == "success", record["error"]
    totals = record["data_diff"]["silver.totals"]
    assert totals["status"] == "modified"
    assert (totals["main_rows"], totals["pr_rows"]) == (5, 3)
    # landing came from main's warehouse, so it is in both and unchanged.
    assert record["data_diff"]["landing.orders"]["status"] == "unchanged"


def test_merge_ignores_branch_warehouses(project):
    from havn.engine.pr import merge_ignored_paths

    assert ".havn/branches/" in merge_ignored_paths(project)


# ---------------------------------------------------------------------------
# Server: follow the checkout
# ---------------------------------------------------------------------------


@pytest.fixture
def client(project, base):
    from fastapi.testclient import TestClient

    import havn.server.app as server_app
    from havn.server.deps import reset_shared_conn

    reset_shared_conn()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = False
    server_app.ACTIVE_ENV = None
    yield TestClient(server_app.app)
    reset_shared_conn()


def test_server_follows_a_checkout_to_the_branch_warehouse(project, client):
    r = client.get("/api/branch")
    assert r.status_code == 200 and not r.json()["active"]
    assert client.get("/api/environment").json()["database_path"] == "warehouse.duckdb"

    _git(project, "branch", "feature/x")
    r = client.post("/api/git/checkout", json={"branch": "feature/x"})
    assert r.status_code == 200, r.text
    assert r.json()["warehouse"]["head"] == "branch:feature/x"

    env = client.get("/api/environment").json()
    assert env["database_path"].startswith(".havn/branches/")
    assert env["branch"]["active"] and env["branch"]["branch"] == "feature/x"
    assert env["defer"]["target"] == "base" and env["defer"]["lockable"]

    # Back on main: the base again.
    _git(project, "checkout", "main")
    r = client.get("/api/branch")
    assert not r.json()["active"]
    assert client.get("/api/environment").json()["database_path"] == "warehouse.duckdb"


def test_server_holds_the_switch_while_a_pipeline_runs(project, client):
    from havn.server.routes.pipeline import _pipeline_state

    client.get("/api/branch")
    _git(project, "checkout", "-b", "feature/x")
    _pipeline_state["running"] = True
    _pipeline_state["operation_label"] = "transform"
    try:
        r = client.get("/api/branch").json()
        assert not r["active"]
        assert r["server"]["pending"]["branch"] == "feature/x"
        assert "transform is running" in r["server"]["pending"]["reason"]
    finally:
        _pipeline_state["running"] = False
    r = client.get("/api/branch").json()
    assert r["active"] and r["server"]["pending"] is None


def test_server_build_status_and_diff(project, client):
    client.get("/api/branch")
    _on_feature(project)
    assert client.get("/api/branch").json()["active"]

    status = client.get("/api/branch/status").json()
    assert status["models"]["needs_build"] == ["silver.totals", "silver.big"]

    r = client.post("/api/branch/build", json={})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["built"]) == ["silver.big", "silver.totals"]

    status = client.get("/api/branch/status").json()
    assert status["up_to_date"]

    r = client.post("/api/branch/diff", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["changed"] == 2
    assert body["markdown"].startswith(B.MARKDOWN_MARKER)

    listing = client.get("/api/branch/list").json()
    assert [e["branch"] for e in listing] == ["feature/x"]

    # Home calls unbuilt models "from base" on a branch, not "not built".
    home = client.get("/api/home").json()
    assert home["branch"] == {"name": "feature/x", "base": "base"}
    assert home["tiles"]["models"]["deferred"] == 2
    assert home["tiles"]["models"]["never_built"] == 0
    statuses = {m["full_name"]: m["status"] for layer in home["layers"] for m in layer["models"]}
    assert statuses["bronze.orders"] == "deferred"


def test_a_server_with_sign_in_never_follows_branches(project, client):
    """Users and tokens live in the warehouse; a fresh branch file has none."""
    import havn.server.app as server_app
    from havn.server import deps

    _git(project, "checkout", "-b", "feature/x")
    server_app.AUTH_ENABLED = True
    try:
        deps._clear_config_cache()
        deps.sync_branch_warehouse(force=True)
        cfg = deps._get_config()
        assert not cfg.branch.active and "--auth" in cfg.branch.reason
        assert cfg.database.path == "warehouse.duckdb"
    finally:
        server_app.AUTH_ENABLED = False
        deps._clear_config_cache()


def test_server_branch_endpoints_off_a_branch(client):
    assert client.post("/api/branch/build", json={}).status_code == 400
    assert client.post("/api/branch/diff", json={}).status_code == 400
    assert not client.get("/api/branch/status").json()["active"]


# ---------------------------------------------------------------------------
# CI
# ---------------------------------------------------------------------------


def test_ci_generates_pr_and_base_workflows(project):
    from havn.engine.ci import generate_workflow

    (project / "project.yml").write_text(
        "name: b\ndatabase:\n  path: warehouse.duckdb\n"
        "environments:\n  prod:\n    database:\n      path: data/prod.duckdb\n"
        "branches:\n  enabled: true\n  base: prod\n"
    )
    result = generate_workflow(project)
    pr = yaml.safe_load((project / result["path"]).read_text())
    base = yaml.safe_load((project / result["base_path"]).read_text())
    steps = " ".join(s.get("run", "") for s in pr["jobs"]["diff"]["steps"])
    assert "havn branch build --base .havn/ci-base/prod.duckdb" in steps
    assert "havn branch diff --base .havn/ci-base/prod.duckdb --markdown" in steps
    assert "havn ci comment" in steps
    assert pr["jobs"]["diff"]["env"]["HAVN_BRANCH"] == "${{ github.head_ref }}"
    assert pr[True]["pull_request"]["branches"] == ["main"]
    base_steps = base["jobs"]["base"]["steps"]
    assert any(s.get("run") == "havn transform --env prod" for s in base_steps)
    upload = next(s for s in base_steps if s.get("uses", "").startswith("actions/upload-artifact"))
    assert upload["with"] == {"name": "havn-base", "path": "data/prod.duckdb", "retention-days": 30}


def test_ci_comment_posts_once_then_updates(tmp_path, monkeypatch):
    from havn.engine.ci import post_markdown_comment

    md = tmp_path / "d.md"
    md.write_text(B.MARKDOWN_MARKER + "\n## first\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    comments: list[dict] = []
    calls: list[tuple] = []

    def fake(method, url, token, payload=None):
        calls.append((method, url))
        if method == "GET":
            return comments
        if method == "POST":
            comments.append({"id": 9, "body": payload["body"]})
            return {"id": 9}
        comments[0]["body"] = payload["body"]
        return {}

    first = post_markdown_comment(str(md), "o/r", 3, request=fake)
    assert first == {"pr": 3, "repo": "o/r", "updated": False, "comment_id": 9}
    md.write_text(B.MARKDOWN_MARKER + "\n## second\n", encoding="utf-8")
    second = post_markdown_comment(str(md), "o/r", 3, request=fake)
    assert second["updated"] and len(comments) == 1 and "second" in comments[0]["body"]
    assert ("PATCH", "https://api.github.com/repos/o/r/issues/comments/9") in calls

    monkeypatch.delenv("GITHUB_TOKEN")
    assert "GITHUB_TOKEN" in post_markdown_comment(str(md), "o/r", 3, request=fake)["error"]
