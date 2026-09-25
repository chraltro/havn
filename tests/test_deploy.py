"""Deploying a git ref to an environment's warehouse, with rollback."""
from __future__ import annotations

import subprocess
from pathlib import Path

import duckdb
import pytest

from havn.engine.database import ensure_meta_table
from havn.engine.deploy import list_deploys, new_record, plan_deploy, run_deploy


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _commit(project: Path, files: dict[str, str], msg: str) -> None:
    for rel, text in files.items():
        f = project / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    _git(project, "add", "-A")
    _git(project, "commit", "-m", msg)


ORDERS = "@config materialized=table, schema=bronze\n\nSELECT * FROM landing.orders\n"
TOTALS = "@config materialized=table, schema=gold\n@assert n > 0\n\nSELECT COUNT(*) AS n, SUM(amount) AS total FROM bronze.orders\n"
BIG = "@config materialized=view, schema=gold\n\nSELECT * FROM bronze.orders WHERE amount > 10\n"
OTHER = "@config materialized=table, schema=silver\n\nSELECT 1 AS one\n"


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
        ".gitignore": "*.duckdb\n*.wal\n.havn/deploy/\n_snapshots/\n",
        "transform/bronze/orders.sql": ORDERS,
        "transform/gold/totals.sql": TOTALS,
        "transform/gold/big.sql": BIG,
        "transform/silver/other.sql": OTHER,
    }, "init")
    return tmp_path


@pytest.fixture
def prod(project):
    conn = duckdb.connect(str(project / "prod.duckdb"))
    conn.execute("CREATE SCHEMA landing")
    conn.execute("CREATE TABLE landing.orders AS SELECT * FROM (VALUES (1, 5.0), (2, 20.0), (3, 30.0)) t(id, amount)")
    ensure_meta_table(conn)
    yield conn
    conn.close()


def _deploy(project, conn, ref="main"):
    rec = new_record("prod", ref, deployed_by="ada")
    return run_deploy(project, rec, conn, db_path=str(project / "prod.duckdb"))


def _state(conn):
    return sorted(conn.execute("SELECT model_path, content_hash, upstream_hash FROM _havn.model_state").fetchall())


def test_first_deploy_builds_everything_then_nothing(project, prod):
    rec = _deploy(project, prod)
    assert rec["status"] == "success", rec
    assert set(rec["models"]) == {"bronze.orders", "gold.totals", "gold.big", "silver.other"}
    assert rec["models"].index("bronze.orders") < rec["models"].index("gold.totals")
    assert prod.execute("SELECT n, total FROM gold.totals").fetchone() == (3, 55.0)
    assert len(rec["commit"]) == 40

    again = _deploy(project, prod)
    assert again["status"] == "up_to_date"
    assert again["models"] == []

    history = list_deploys(prod)
    assert [d["status"] for d in history] == ["up_to_date", "success"]
    assert history[1]["deployed_by"] == "ada"
    assert not (project / ".havn" / "deploy" / rec["id"]).exists()  # worktree removed


def test_deploy_plans_only_the_change_and_its_downstream(project, prod):
    _deploy(project, prod)
    _commit(project, {"transform/bronze/orders.sql": ORDERS.replace("SELECT *", "SELECT id, amount * 2 AS amount")}, "double")
    assert set(plan_deploy(project, "main", prod)["models"]) == {"bronze.orders", "gold.big", "gold.totals"}
    rec = _deploy(project, prod)
    assert rec["status"] == "success"
    assert "silver.other" not in rec["models"]
    assert prod.execute("SELECT total FROM gold.totals").fetchone() == (110.0,)


def test_failed_deploy_rolls_everything_back(project, prod):
    _deploy(project, prod)
    before_totals = prod.execute("SELECT * FROM gold.totals").fetchall()
    before_view = prod.execute("SELECT sql FROM duckdb_views() WHERE view_name = 'big'").fetchone()[0]
    before_state = _state(prod)

    # bronze.orders changes shape, gold.big (a view) changes, a new model
    # appears, and gold.totals' error-level check fails on the new data.
    _commit(project, {
        "transform/bronze/orders.sql": ORDERS.replace("SELECT *", "SELECT id, amount FROM landing.orders WHERE id > 99 --"),
        "transform/gold/big.sql": BIG.replace("amount > 10", "amount > 25"),
        "transform/gold/fresh.sql": "@config materialized=table, schema=gold\n\nSELECT id FROM bronze.orders\n",
    }, "break it")
    rec = _deploy(project, prod)

    assert rec["status"] == "rolled_back", rec
    assert rec["failed"]["gold.totals"]["status"] == "assertion_failed"
    assert set(rec["restored"]) == set(rec["models"])
    assert prod.execute("SELECT * FROM gold.totals").fetchall() == before_totals
    assert prod.execute("SELECT sql FROM duckdb_views() WHERE view_name = 'big'").fetchone()[0] == before_view
    assert prod.execute("SELECT COUNT(*) FROM bronze.orders").fetchone()[0] == 3
    # A model that did not exist before the deploy does not exist after it.
    assert prod.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'gold' AND table_name = 'fresh'"
    ).fetchone()[0] == 0
    # Build state is back too, so the next deploy plans the same models again.
    assert _state(prod) == before_state
    assert set(plan_deploy(project, "main", prod)["models"]) == set(rec["models"])


def test_deploys_the_ref_not_the_working_tree(project, prod):
    _git(project, "checkout", "-b", "wip")
    (project / "transform" / "gold" / "totals.sql").write_text(TOTALS.replace("SUM(amount)", "SUM(amount) * 100"))
    rec = _deploy(project, prod, ref="main")
    assert rec["status"] == "success"
    assert prod.execute("SELECT total FROM gold.totals").fetchone() == (55.0,)


def test_unknown_ref_is_an_error_and_changes_nothing(project, prod):
    rec = _deploy(project, prod, ref="no-such-branch")
    assert rec["status"] == "error"
    assert "Unknown ref" in rec["error"]
    assert prod.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'gold'"
    ).fetchone()[0] == 0


def test_cli_plan_deploy_and_rollback(project, prod):
    from typer.testing import CliRunner

    from havn.cli import app

    prod.close()  # the CLI opens the warehouse itself
    runner = CliRunner()
    out = runner.invoke(app, ["deploy", "prod", "--plan", "-p", str(project)])
    assert out.exit_code == 0, out.output
    assert "would rebuild" in out.output and "gold.totals" in out.output

    out = runner.invoke(app, ["deploy", "prod", "-p", str(project)])
    assert out.exit_code == 0, out.output
    assert "Deployed main" in out.output

    _commit(project, {"transform/gold/totals.sql": TOTALS.replace("n > 0", "n > 99")}, "impossible check")
    out = runner.invoke(app, ["deploy", "prod", "-p", str(project)])
    assert out.exit_code == 1
    assert "Rolled back" in out.output and "gold.totals" in out.output

    out = runner.invoke(app, ["deploy", "qa", "-p", str(project)])
    assert out.exit_code == 1 and "Unknown environment" in out.output
