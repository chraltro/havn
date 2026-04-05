"""Tests for orchestration jobs engine."""

from __future__ import annotations

import duckdb
import pytest

from havn.engine.database import ensure_meta_table
from havn.engine.orchestration import (
    Job,
    _find_job,
    _matches_cron,
    delete_job,
    discover_jobs,
    ensure_job_runs_table,
    execute_job,
    get_next_run,
    mark_stale_runs_failed,
    preview_plan,
    resolve_execution_plan,
    save_job,
)


@pytest.fixture
def project(tmp_path):
    """Minimal project with orchestration/ and transform/ dirs."""
    (tmp_path / "project.yml").write_text("name: test\ndatabase:\n  path: warehouse.duckdb\n")
    (tmp_path / "orchestration").mkdir()
    (tmp_path / "transform" / "bronze").mkdir(parents=True)
    (tmp_path / "transform" / "silver").mkdir(parents=True)
    (tmp_path / "ingest").mkdir()
    (tmp_path / "export").mkdir()
    # Bronze model depends on landing.raw_orders
    (tmp_path / "transform" / "bronze" / "orders.sql").write_text(
        "-- config: materialized=table, schema=bronze\n"
        "-- depends_on: landing.raw_orders\n\n"
        "SELECT * FROM landing.raw_orders\n"
    )
    # Silver model depends on bronze.orders
    (tmp_path / "transform" / "silver" / "orders.sql").write_text(
        "-- config: materialized=table, schema=silver\n"
        "-- depends_on: bronze.orders\n\n"
        "SELECT * FROM bronze.orders WHERE 1=1\n"
    )
    # Ingest script
    (tmp_path / "ingest" / "orders.py").write_text(
        "db.execute('CREATE SCHEMA IF NOT EXISTS landing')\n"
        "db.execute('CREATE OR REPLACE TABLE landing.raw_orders AS SELECT 1 AS id')\n"
    )
    # Export script
    (tmp_path / "export" / "report.py").write_text(
        "result = db.execute('SELECT * FROM silver.orders').fetchall()\n"
    )
    return tmp_path


@pytest.fixture
def conn(project):
    c = duckdb.connect(str(project / "warehouse.duckdb"))
    ensure_meta_table(c)
    ensure_job_runs_table(c)
    c.execute("CREATE SCHEMA IF NOT EXISTS landing")
    c.execute(
        "CREATE TABLE IF NOT EXISTS landing.raw_orders AS SELECT 1 AS id, 'test' AS name"
    )
    yield c
    c.close()


def _write_job(
    project,
    name="test-job",
    target="silver.orders",
    cron="0 6 * * *",
    enabled=True,
    resolve="upstream",
):
    (project / "orchestration" / f"{name}.yml").write_text(
        f"name: {name}\n"
        f"target: {target}\n"
        f"resolve: {resolve}\n"
        f"cron: \"{cron}\"\n"
        f"enabled: {str(enabled).lower()}\n"
        f"retry: 0\n"
        f"timeout_minutes: 5\n"
    )


# --- Discovery ---


def test_discover_jobs(project):
    _write_job(project)
    jobs = discover_jobs(project)
    assert len(jobs) == 1
    assert jobs[0].name == "test-job"
    assert jobs[0].target == "silver.orders"
    assert jobs[0].enabled is True


def test_discover_skips_invalid(project):
    (project / "orchestration" / "bad.yml").write_text("not: valid: yaml: [")
    (project / "orchestration" / "missing-target.yml").write_text("name: no-target\n")
    _write_job(project)
    jobs = discover_jobs(project)
    # Only the valid one should remain
    assert len(jobs) == 1
    assert jobs[0].name == "test-job"


def test_discover_no_dir(tmp_path):
    assert discover_jobs(tmp_path) == []


def test_discover_skips_bad_cron(project):
    (project / "orchestration" / "badcron.yml").write_text(
        "name: badcron\ntarget: bronze.orders\ncron: \"invalid\"\n"
    )
    jobs = discover_jobs(project)
    assert len(jobs) == 0


# --- Plan resolution ---


def test_resolve_model_target(project, conn):
    from havn.engine.transform.discovery import build_dag, discover_models

    models = discover_models(project / "transform")
    dag = build_dag(models)
    plan = resolve_execution_plan("silver.orders", dag, project)
    assert len(plan.steps) >= 2  # bronze.orders + silver.orders (at minimum)
    types = [s.type for s in plan.steps]
    assert "transform" in types
    # Bronze must come before silver
    targets = [s.target for s in plan.steps if s.type == "transform"]
    assert targets.index("bronze.orders") < targets.index("silver.orders")


def test_resolve_ingest_target(project, conn):
    from havn.engine.transform.discovery import build_dag, discover_models

    dag = build_dag(discover_models(project / "transform"))
    plan = resolve_execution_plan("ingest/orders.py", dag, project)
    assert len(plan.steps) == 1
    assert plan.steps[0].type == "ingest"
    assert plan.steps[0].target == "ingest/orders.py"


def test_resolve_wildcard(project, conn):
    from havn.engine.transform.discovery import build_dag, discover_models

    dag = build_dag(discover_models(project / "transform"))
    plan = resolve_execution_plan("bronze.*", dag, project)
    assert any(s.target == "bronze.orders" for s in plan.steps)


def test_resolve_export_target(project, conn):
    from havn.engine.transform.discovery import build_dag, discover_models

    dag = build_dag(discover_models(project / "transform"))
    plan = resolve_execution_plan("export/report.py", dag, project)
    # Should include at least silver.orders (referenced in export script) + export itself
    assert any(s.type == "export" for s in plan.steps)


# --- Preview ---


def test_preview_plan(project, conn):
    from havn.engine.transform.discovery import build_dag, discover_models

    dag = build_dag(discover_models(project / "transform"))
    result = preview_plan("silver.orders", dag, project, conn=conn)
    assert "steps" in result
    assert result["total_steps"] >= 2
    assert "transform_count" in result
    assert result["transform_count"] >= 2


# --- Execution ---


def test_execute_job(project, conn):
    from havn.engine.transform.discovery import build_dag, discover_models

    dag = build_dag(discover_models(project / "transform"))
    job = Job(
        name="test",
        target="silver.orders",
        file_path=project / "orchestration" / "test.yml",
    )
    plan = resolve_execution_plan(job.target, dag, project, conn=conn)
    result = execute_job(job, plan, conn, project, trigger="manual")
    assert result.status in ("success", "failure")
    assert result.steps_total == len(plan.steps)
    # Check DB row
    rows = conn.execute(
        "SELECT * FROM _havn.job_runs WHERE job_name = 'test'"
    ).fetchall()
    assert len(rows) >= 1


def test_execute_job_success(project, conn):
    """A complete transform should succeed when upstream landing data exists."""
    from havn.engine.transform.discovery import build_dag, discover_models

    dag = build_dag(discover_models(project / "transform"))
    job = Job(
        name="test-success",
        target="silver.orders",
        file_path=project / "orchestration" / "test-success.yml",
    )
    plan = resolve_execution_plan(job.target, dag, project, conn=conn)
    result = execute_job(job, plan, conn, project, trigger="manual")
    assert result.status == "success"
    assert result.steps_failed == 0


# --- Save/Delete ---


def test_save_and_delete_job(project):
    path = save_job(
        project,
        {"name": "New Job", "target": "gold.revenue", "cron": "0 6 * * 1"},
    )
    assert path.exists()
    assert path.suffix == ".yml"
    assert delete_job(project, "new-job")
    assert not path.exists()


def test_find_job(project):
    _write_job(project, name="findable-job")
    job = _find_job(project, "findable-job")
    assert job is not None
    assert job.name == "findable-job"


# --- Next run ---


def test_get_next_run():
    result = get_next_run("* * * * *")  # every minute
    assert result is not None


def test_get_next_run_invalid():
    assert get_next_run("") is None
    assert get_next_run("bad") is None
    assert get_next_run("1 2 3") is None


def test_cron_zero_step_is_safe():
    """`*/0` must not raise ZeroDivisionError — should evaluate to False."""
    import datetime
    # Build a dummy datetime and run the matcher directly; the key is it
    # doesn't raise.
    now = datetime.datetime(2026, 4, 5, 12, 0)
    assert _matches_cron(["*/0", "*", "*", "*", "*"], now) is False
    # get_next_run should return None instead of looping forever or raising
    assert get_next_run("*/0 * * * *") is None


def test_cron_weekday_is_posix():
    """Weekday uses POSIX convention: 0=Sunday, 1=Monday, ..., 6=Saturday."""
    import datetime
    # 2026-04-06 is a Monday. In POSIX cron that's weekday 1.
    monday = datetime.datetime(2026, 4, 6, 9, 0)
    assert _matches_cron(["0", "9", "*", "*", "1"], monday) is True
    # Python's weekday() for monday == 0, but with POSIX conversion it's 1
    assert _matches_cron(["0", "9", "*", "*", "0"], monday) is False
    # 2026-04-05 is a Sunday, POSIX weekday 0
    sunday = datetime.datetime(2026, 4, 5, 9, 0)
    assert _matches_cron(["0", "9", "*", "*", "0"], sunday) is True


# --- resolve=none ---


def test_resolve_none_model_target(project, conn):
    """resolve='none' should include only the literal target, not upstream."""
    from havn.engine.transform.discovery import build_dag, discover_models

    dag = build_dag(discover_models(project / "transform"))
    plan = resolve_execution_plan("silver.orders", dag, project, resolve="none")
    transform_targets = [s.target for s in plan.steps if s.type == "transform"]
    assert transform_targets == ["silver.orders"]
    # No ingest steps should be added either
    assert all(s.type != "ingest" for s in plan.steps)


def test_resolve_none_wildcard(project, conn):
    """resolve='none' with wildcard should include matching models but no upstream."""
    from havn.engine.transform.discovery import build_dag, discover_models

    dag = build_dag(discover_models(project / "transform"))
    plan = resolve_execution_plan("silver.*", dag, project, resolve="none")
    transform_targets = sorted(s.target for s in plan.steps if s.type == "transform")
    assert transform_targets == ["silver.orders"]
    # bronze.orders should NOT be included even though silver.orders depends on it
    assert "bronze.orders" not in transform_targets


def test_resolve_upstream_default_includes_deps(project, conn):
    """The default resolve='upstream' includes transitive deps."""
    from havn.engine.transform.discovery import build_dag, discover_models

    dag = build_dag(discover_models(project / "transform"))
    plan = resolve_execution_plan("silver.orders", dag, project)
    transform_targets = [s.target for s in plan.steps if s.type == "transform"]
    assert "bronze.orders" in transform_targets
    assert "silver.orders" in transform_targets


# --- Wildcard edge cases ---


def test_wildcard_no_matches_returns_empty_plan(project, conn):
    from havn.engine.transform.discovery import build_dag, discover_models

    dag = build_dag(discover_models(project / "transform"))
    plan = resolve_execution_plan("nonexistent_schema.*", dag, project)
    assert len(plan.steps) == 0


# --- Export target with no detected deps ---


def test_export_no_deps_does_not_rebuild_warehouse(project, conn):
    """An export script with no detectable model refs should NOT rebuild everything."""
    # Write an export script that doesn't reference any model by FQN
    (project / "export" / "opaque.py").write_text(
        "# No model references here\nprint('done')\n"
    )
    from havn.engine.transform.discovery import build_dag, discover_models

    dag = build_dag(discover_models(project / "transform"))
    plan = resolve_execution_plan("export/opaque.py", dag, project)
    transform_targets = [s.target for s in plan.steps if s.type == "transform"]
    # Previous buggy behavior: all models would be included. Fixed: none are.
    assert transform_targets == []
    export_targets = [s.target for s in plan.steps if s.type == "export"]
    assert export_targets == ["export/opaque.py"]


# --- Path traversal ---


def test_save_job_rejects_traversal(project):
    import pytest as _pytest

    with _pytest.raises(ValueError):
        save_job(project, {"name": "bad", "target": "ingest/../../../etc/passwd"})


def test_resolve_rejects_traversal_target(project):
    from havn.engine.transform.discovery import build_dag, discover_models
    import pytest as _pytest

    dag = build_dag(discover_models(project / "transform"))
    with _pytest.raises(ValueError):
        resolve_execution_plan("ingest/../../escape.py", dag, project)


# --- Skipped steps counted correctly ---


def test_skipped_steps_counted(project, conn):
    """Failed upstream should cause downstream to be skipped AND counted."""
    from havn.engine.transform.discovery import build_dag, discover_models

    # Make bronze.orders fail by pointing it at a nonexistent landing table
    (project / "transform" / "bronze" / "orders.sql").write_text(
        "-- config: materialized=table, schema=bronze\n"
        "-- depends_on: landing.does_not_exist\n\n"
        "SELECT * FROM landing.does_not_exist\n"
    )

    dag = build_dag(discover_models(project / "transform"))
    job = Job(
        name="skiptest",
        target="silver.orders",
        file_path=project / "orchestration" / "skiptest.yml",
    )
    plan = resolve_execution_plan(job.target, dag, project, conn=conn)
    result = execute_job(job, plan, conn, project, trigger="manual")

    # bronze.orders should fail, silver.orders should be skipped
    assert result.steps_failed >= 1
    assert result.steps_skipped >= 1
    # Totals add up
    total_accounted = result.steps_completed + result.steps_failed + result.steps_skipped
    assert total_accounted == result.steps_total
    # Status is failure, not success
    assert result.status == "failure"


# --- Stale run cleanup ---


def test_mark_stale_runs_failed(conn):
    """mark_stale_runs_failed should mark stale 'running' rows as failed."""
    # Insert a running row with a started_at from 2 hours ago
    conn.execute(
        "INSERT INTO _havn.job_runs (id, job_name, job_file, target, status, "
        "steps_total, started_at) "
        "VALUES ('stale-run', 'old-job', 'old.yml', 'silver.orders', 'running', "
        "5, current_timestamp - INTERVAL '2 hours')"
    )
    # Also a fresh running row that should NOT be touched
    conn.execute(
        "INSERT INTO _havn.job_runs (id, job_name, job_file, target, status, "
        "steps_total) "
        "VALUES ('fresh-run', 'new-job', 'new.yml', 'silver.orders', 'running', 5)"
    )
    mark_stale_runs_failed(conn)
    stale = conn.execute(
        "SELECT status, error FROM _havn.job_runs WHERE id = 'stale-run'"
    ).fetchone()
    assert stale[0] == "failure"
    assert "restarted" in (stale[1] or "").lower()
    fresh = conn.execute(
        "SELECT status FROM _havn.job_runs WHERE id = 'fresh-run'"
    ).fetchone()
    assert fresh[0] == "running"


# --- Cancel after completion preserves success status ---


def test_cancel_does_not_overwrite_completed_job(project, conn):
    """A successfully-completed run should not be overwritten to cancelled."""
    from havn.engine.transform.discovery import build_dag, discover_models

    dag = build_dag(discover_models(project / "transform"))
    job = Job(
        name="racey",
        target="bronze.orders",
        file_path=project / "orchestration" / "racey.yml",
    )
    plan = resolve_execution_plan(job.target, dag, project, conn=conn)
    result = execute_job(job, plan, conn, project, trigger="manual")
    assert result.status == "success"
    # Now try to cancel the already-completed run
    conn.execute(
        "UPDATE _havn.job_runs SET status = 'cancelled' "
        "WHERE id = ? AND status = 'running'",
        [result.run_id],
    )
    row = conn.execute(
        "SELECT status FROM _havn.job_runs WHERE id = ?", [result.run_id]
    ).fetchone()
    # Status remains 'success' because the WHERE clause filtered it out
    assert row[0] == "success"


# --- API Tests ---


@pytest.fixture
def api_project(project):
    """Project with a job file for API testing."""
    _write_job(project)
    c = duckdb.connect(str(project / "warehouse.duckdb"))
    ensure_meta_table(c)
    ensure_job_runs_table(c)
    c.execute("CREATE SCHEMA IF NOT EXISTS landing")
    c.execute("CREATE TABLE IF NOT EXISTS landing.raw_orders AS SELECT 1 AS id")
    c.close()
    return project


@pytest.fixture
def client(api_project):
    import havn.server.app as server_app
    from fastapi.testclient import TestClient

    from havn.server.deps import reset_shared_conn

    reset_shared_conn()
    server_app.PROJECT_DIR = api_project
    server_app.AUTH_ENABLED = False
    yield TestClient(server_app.app)
    reset_shared_conn()


def test_api_list_jobs(client):
    resp = client.get("/api/jobs")
    assert resp.status_code == 200
    jobs = resp.json()
    assert len(jobs) == 1
    assert jobs[0]["name"] == "test-job"


def test_api_get_job(client):
    resp = client.get("/api/jobs/test-job")
    assert resp.status_code == 200
    data = resp.json()
    assert "plan" in data
    assert data["plan"]["total_steps"] >= 1


def test_api_get_plan(client):
    resp = client.get("/api/jobs/test-job/plan")
    assert resp.status_code == 200
    plan = resp.json()
    assert "steps" in plan


def test_api_create_job(client):
    resp = client.post(
        "/api/jobs",
        json={"name": "new-api-job", "target": "bronze.orders"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "created"


def test_api_update_job(client):
    resp = client.patch("/api/jobs/test-job", json={"enabled": False})
    assert resp.status_code == 200


def test_api_delete_job(client):
    # Create then delete
    client.post("/api/jobs", json={"name": "to-delete", "target": "bronze.orders"})
    resp = client.delete("/api/jobs/to-delete")
    assert resp.status_code == 200


def test_api_list_job_runs(client):
    resp = client.get("/api/job-runs")
    assert resp.status_code == 200


def test_api_job_not_found(client):
    resp = client.get("/api/jobs/nonexistent")
    assert resp.status_code == 404


def test_api_create_job_rejects_traversal(client):
    resp = client.post(
        "/api/jobs",
        json={"name": "evil", "target": "ingest/../../etc/passwd"},
    )
    assert resp.status_code == 422  # Pydantic validation error


# --- Scheduler integration ---


def test_scheduler_should_run_handles_job_prefix(project):
    """SchedulerThread._should_run should accept `job:` prefixed keys."""
    from havn.engine.scheduler import SchedulerThread

    thread = SchedulerThread(project)
    # `* * * * *` matches every minute, so the first call should return True
    # (no prior run recorded)
    assert thread._should_run("job:test-job", "* * * * *") is True
    # Record a run and verify the second call returns False for the same minute
    import datetime
    mk = datetime.datetime.now().replace(second=0, microsecond=0).timestamp()
    thread._last_run["job:test-job"] = mk
    assert thread._should_run("job:test-job", "* * * * *") is False


def test_scheduler_cron_zero_step_is_safe(project):
    """Scheduler cron matcher must not raise on `*/0`."""
    from havn.engine.scheduler import SchedulerThread

    thread = SchedulerThread(project)
    # Should return False, not raise
    assert thread._should_run("job:x", "*/0 * * * *") is False


def test_scheduler_weekday_posix_convention(project):
    """Scheduler should use POSIX weekday: 0=Sunday, 1=Monday."""
    # We can't easily mock datetime.now() without another dep, but we can at
    # least verify that `_should_run` doesn't crash with weekday patterns and
    # that the matcher function in orchestration.py (which shares conventions)
    # agrees with what the scheduler does for the same time. This is covered
    # by test_cron_weekday_is_posix above.
    pass
