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


def test_close_target_releases_the_warehouse_before_the_result_is_saved(project, prod, monkeypatch):
    """"success" must mean the target is free.

    The server's deploy runs on a thread and a client polls the record. When
    the record said "success" while the target was still open, a client that
    immediately queried the target hit "file is being used by another
    process" on Windows. Asserting the order directly keeps this from being a
    timing-dependent test.
    """
    import havn.engine.deploy as deploy

    record_conn = duckdb.connect()
    ensure_meta_table(record_conn)
    target_open_at_final_save = []
    real_save = deploy._save

    def spy(conn, record):
        if record.get("finished_at"):
            try:
                prod.execute("SELECT 1")
                target_open_at_final_save.append(True)
            except duckdb.ConnectionException:
                target_open_at_final_save.append(False)
        real_save(conn, record)

    monkeypatch.setattr(deploy, "_save", spy)
    rec = run_deploy(project, new_record("prod", "main"), prod, record_conn=record_conn,
                     db_path=str(project / "prod.duckdb"), close_target=True)
    record_conn.close()

    assert rec["status"] == "success", rec
    assert target_open_at_final_save == [False]


def test_close_target_requires_a_separate_record_connection(project, prod):
    with pytest.raises(ValueError, match="separate record_conn"):
        run_deploy(project, new_record("prod", "main"), prod, close_target=True)


def test_rollback_restores_microbatch_progress_and_blocks(tmp_path):
    """A rolled-back deploy must not leave windows marked done whose rows it removed.

    The rollback restored the tables and model_state but not batch_state, so
    the windows the failed deploy processed stayed "done" and the next run
    resumed after them: their rows were gone until someone forced a backfill.
    """
    from havn.engine.deploy import _ROLLBACK_META_TABLES, _meta_rows, _restore

    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    ensure_meta_table(conn)
    conn.execute(
        "INSERT INTO _havn.batch_state (model_path, window_start, window_end, status) "
        "VALUES ('silver.ev', '2026-01-01', '2026-01-02', 'success')"
    )
    names = ["silver.ev"]
    snap = {"objects": {"silver.ev": {"kind": None}}, "parquet": {}}
    snap.update({t: _meta_rows(conn, t, names) for t in _ROLLBACK_META_TABLES})

    # What the failed deploy wrote before it rolled back.
    conn.execute(
        "INSERT INTO _havn.batch_state (model_path, window_start, window_end, status) "
        "VALUES ('silver.ev', '2026-01-02', '2026-01-03', 'success')"
    )
    conn.execute("INSERT INTO _havn.model_blocked (model_path) VALUES ('silver.ev')")

    _restore(conn, tmp_path, snap)

    windows = conn.execute("SELECT CAST(window_start AS DATE)::VARCHAR FROM _havn.batch_state").fetchall()
    assert windows == [("2026-01-01",)]
    assert conn.execute("SELECT COUNT(*) FROM _havn.model_blocked").fetchone()[0] == 0
    conn.close()


def test_startup_closes_out_work_a_previous_server_left_running(tmp_path):
    """A restart kills the build/deploy thread; its row must not say running forever."""
    from havn.engine.deploy import ensure_deploys_table, mark_interrupted_deploys
    from havn.engine.pr import mark_interrupted_builds

    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    ensure_meta_table(conn)
    ensure_deploys_table(conn)
    conn.execute("INSERT INTO _havn.pr_builds (id, pr_id, status) VALUES ('b1', 'pr-1', 'running'), ('b2', 'pr-1', 'success')")
    conn.execute("INSERT INTO _havn.deploys (id, env, ref, status) VALUES ('d1', 'prod', 'main', 'running'), ('d2', 'prod', 'main', 'success')")

    assert mark_interrupted_builds(conn) == 1
    assert mark_interrupted_deploys(conn) == 1

    builds = dict(conn.execute("SELECT id, status FROM _havn.pr_builds").fetchall())
    deploys = dict(conn.execute("SELECT id, status FROM _havn.deploys").fetchall())
    assert builds == {"b1": "error", "b2": "success"}
    assert deploys == {"d1": "error", "d2": "success"}
    assert "Interrupted" in conn.execute("SELECT error FROM _havn.deploys WHERE id = 'd1'").fetchone()[0]

    # A warehouse that never ran either is fine.
    fresh = duckdb.connect()
    assert mark_interrupted_builds(fresh) == 0 and mark_interrupted_deploys(fresh) == 0
    fresh.close()
    conn.close()


def test_an_interrupted_deploy_can_be_restored_from_its_snapshot(project, prod, monkeypatch):
    """A server restart mid-deploy left the target half changed with no way
    back but a redeploy. The snapshot now survives on the record."""
    import havn.engine.transform as transform_mod
    from havn.engine.deploy import DeployError, mark_interrupted_deploys, restore_interrupted_deploy

    _deploy(project, prod)
    before_totals = prod.execute("SELECT * FROM gold.totals").fetchall()
    before_state = _state(prod)
    _commit(project, {"transform/bronze/orders.sql": ORDERS.replace("SELECT *", "SELECT id, amount * 2 AS amount")}, "double")

    real = transform_mod.run_transform

    def build_then_die(*args, **kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt  # stands in for the process going away

    monkeypatch.setattr(transform_mod, "run_transform", build_then_die)
    with pytest.raises(KeyboardInterrupt):
        _deploy(project, prod)
    monkeypatch.setattr(transform_mod, "run_transform", real)
    assert prod.execute("SELECT total FROM gold.totals").fetchone() == (110.0,)  # half-way state

    assert mark_interrupted_deploys(prod) == 1
    interrupted = list_deploys(prod)[0]
    assert interrupted["status"] == "error" and interrupted["restorable"] is True
    assert "Restore it to how it was before this deploy" in interrupted["error"]

    rec = restore_interrupted_deploy(prod, prod, project, interrupted["id"], restored_by="ada")
    assert rec["status"] == "rolled_back"
    assert rec["restorable"] is False
    assert set(rec["restored"]) == {"bronze.orders", "gold.big", "gold.totals"}
    assert "Restored to the pre-deploy snapshot by ada." in rec["error"]
    assert prod.execute("SELECT * FROM gold.totals").fetchall() == before_totals
    assert _state(prod) == before_state

    with pytest.raises(DeployError, match="nothing to restore"):
        restore_interrupted_deploy(prod, prod, project, interrupted["id"])


def test_finished_deploys_do_not_keep_their_snapshot(project, prod):
    rec = _deploy(project, prod)
    assert rec["status"] == "success"
    assert "rollback" not in rec
    detail = prod.execute("SELECT detail FROM _havn.deploys WHERE id = ?", [rec["id"]]).fetchone()[0]
    assert "rollback" not in detail
    assert list_deploys(prod)[0]["restorable"] is False


def _interrupt(project, conn, monkeypatch):
    """Deploy a change to bronze.orders that dies after building; returns its id."""
    import havn.engine.transform as transform_mod
    from havn.engine.deploy import mark_interrupted_deploys

    _commit(project, {"transform/bronze/orders.sql": ORDERS.replace("SELECT *", "SELECT id, amount * 2 AS amount")}, "double")
    real = transform_mod.run_transform

    def build_then_die(*args, **kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(transform_mod, "run_transform", build_then_die)
    with pytest.raises(KeyboardInterrupt):
        _deploy(project, conn)
    monkeypatch.setattr(transform_mod, "run_transform", real)
    mark_interrupted_deploys(conn)
    return list_deploys(conn)[0]["id"]


def test_a_later_deploy_makes_an_interrupted_one_unrestorable(project, prod, monkeypatch):
    """Restoring after a successful redeploy would silently undo the redeploy."""
    from havn.engine.deploy import DeployError, restore_interrupted_deploy

    _deploy(project, prod)
    interrupted = _interrupt(project, prod, monkeypatch)
    assert list_deploys(prod, deploy_id=interrupted)[0]["restorable"] is True

    _commit(project, {"transform/silver/other.sql": OTHER.replace("1 AS one", "2 AS one")}, "redeploy")
    assert _deploy(project, prod)["status"] == "success"
    assert list_deploys(prod, deploy_id=interrupted)[0]["restorable"] is False
    with pytest.raises(DeployError, match="later deploy"):
        restore_interrupted_deploy(prod, prod, project, interrupted)
    assert prod.execute("SELECT one FROM silver.other").fetchone() == (2,)


def test_a_restore_with_a_missing_snapshot_file_changes_nothing(project, prod, monkeypatch):
    import shutil

    from havn.engine.deploy import DeployError, restore_interrupted_deploy

    _deploy(project, prod)
    interrupted = _interrupt(project, prod, monkeypatch)
    shutil.rmtree(project / "_snapshots")  # e.g. `havn version cleanup`
    before = prod.execute("SELECT total FROM gold.totals").fetchone()

    with pytest.raises(DeployError, match="snapshot is incomplete"):
        restore_interrupted_deploy(prod, prod, project, interrupted)
    assert prod.execute("SELECT total FROM gold.totals").fetchone() == before


def test_a_restore_refuses_a_different_warehouse_file(project, prod, monkeypatch, tmp_path):
    from havn.engine.deploy import DeployError, restore_interrupted_deploy

    _deploy(project, prod)
    interrupted = _interrupt(project, prod, monkeypatch)
    with pytest.raises(DeployError, match="restore refused"):
        restore_interrupted_deploy(prod, prod, project, interrupted, target_path=str(tmp_path / "other.duckdb"))


def test_a_failed_rollback_leaves_the_deploy_restorable(project, prod, monkeypatch):
    """When the in-flight rollback itself fails, a later Restore is the way back."""
    import havn.engine.deploy as deploy_mod

    _deploy(project, prod)
    _commit(project, {"transform/gold/totals.sql": TOTALS.replace("COUNT(*)", "COUNT(*) * 0")}, "break the check")
    real_restore = deploy_mod._restore
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:  # the rollback, and the retry in the error path
            raise RuntimeError("disk full")
        return real_restore(*args, **kwargs)

    monkeypatch.setattr(deploy_mod, "_restore", flaky)
    rec = _deploy(project, prod)
    assert rec["status"] == "error" and "rollback failed" in rec["error"]
    assert list_deploys(prod)[0]["restorable"] is True
    restored = deploy_mod.restore_interrupted_deploy(prod, prod, project, rec["id"])
    assert restored["status"] == "rolled_back"
    assert prod.execute("SELECT n FROM gold.totals").fetchone() == (3,)


def test_a_build_in_the_target_since_the_snapshot_blocks_a_restore(project, prod, monkeypatch):
    """A CLI redeploy records itself in the target, not where the server looks;
    the target's run_log still shows the newer build."""
    from havn.engine.database import log_run
    from havn.engine.deploy import DeployError, restore_interrupted_deploy

    _deploy(project, prod)
    interrupted = _interrupt(project, prod, monkeypatch)
    prod.execute("DELETE FROM _havn.deploys WHERE id <> ?", [interrupted])  # records kept elsewhere
    log_run(prod, "transform", "gold.totals", "success", pipeline_run_id="deploy-elsewhere")

    with pytest.raises(DeployError, match="built in this environment since"):
        restore_interrupted_deploy(prod, prod, project, interrupted)
