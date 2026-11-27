"""One stream, three entry points, the same outcome.

`havn stream` (the CLI), a scheduled fire (``SchedulerThread``) and the web
UI (``POST /api/stream/{name}/start`` and the blocking ``POST
/api/stream/{name}``) all run a stream through ``engine.pipeline_engine``.
These tests run the same stream through each entry against its own real
temporary warehouse and compare what each left behind: node statuses in
``run_log``, the ``job_runs`` row, perf rows, the Pipeline Rewind run and its
snapshots, the order the steps ran in, and what happens after a failed
ingest and a failed assertion.
"""

from __future__ import annotations

import datetime as dt
import textwrap
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

UTC = dt.timezone.utc

USER = {"username": "local", "role": "admin"}

STREAM_YML = """\
    name: parity
    database:
      path: warehouse.duckdb
    scheduler:
      timezone: UTC
    streams:
      daily:
        schedule: "0 6 * * *"
        steps:
          - ingest: [all]
          - transform: [all]
          - export: [all]
"""

FILES = {
    "ingest/orders.py": (
        "db.execute('CREATE SCHEMA IF NOT EXISTS landing')\n"
        "db.execute(\"CREATE OR REPLACE TABLE landing.orders AS "
        "SELECT * FROM (VALUES (1, 10.0), (2, 20.0)) t(order_id, amount)\")\n"
        "print('Loaded 2 rows')\n"
    ),
    "ingest/zones.py": (
        "db.execute('CREATE SCHEMA IF NOT EXISTS landing')\n"
        "db.execute(\"CREATE OR REPLACE TABLE landing.zones AS SELECT 1 AS zone_id\")\n"
    ),
    "transform/bronze/orders.sql": (
        "@config materialized=table, schema=bronze\n\n"
        "SELECT order_id, amount FROM landing.orders\n"
    ),
    "transform/silver/orders.sql": (
        "@config materialized=table, schema=silver\n"
        "@assert row_count > 0\n\n"
        "SELECT order_id, amount * 2 AS amount2 FROM bronze.orders\n"
    ),
    "export/report.py": (
        "from pathlib import Path\n"
        "n = db.execute('SELECT count(*) FROM silver.orders').fetchone()[0]\n"
        "Path(__file__).parent.parent.joinpath('exported.txt').write_text(str(n))\n"
    ),
}


def _make(root: Path, files: dict[str, str | None] | None = None, yml: str = STREAM_YML) -> Path:
    root.mkdir(parents=True)
    (root / "project.yml").write_text(textwrap.dedent(yml), encoding="utf-8")
    for rel, text in {**FILES, **(files or {})}.items():
        if text is None:
            continue
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


# --- the three entry points ---------------------------------------------------


def _run_cli(project: Path) -> bool:
    from havn.cli import app

    result = CliRunner().invoke(app, ["stream", "daily", "--project", str(project)])
    assert result.exit_code in (0, 1), result.output
    return result.exit_code == 0


def _run_scheduled(project: Path) -> bool:
    from havn.engine.scheduler import SchedulerThread

    t = SchedulerThread(project, clock=lambda: dt.datetime(2026, 4, 6, 6, 0, 5, tzinfo=UTC))
    try:
        t._started_at = dt.datetime(2026, 4, 6, 6, 0, 0, tzinfo=UTC)
        t._tick(now=dt.datetime(2026, 4, 6, 6, 0, 5, tzinfo=UTC))
        assert t.wait_idle(120)
    finally:
        t.stop()
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        (status,) = conn.execute(
            "SELECT status FROM _havn.schedule_history WHERE schedule_name = 'daily'"
        ).fetchone()
    finally:
        conn.close()
    return status == "success"


def _run_ui(project: Path) -> bool:
    import havn.server.app as server_app
    from havn.config import load_project
    from havn.server.routes import pipeline

    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = False
    with pipeline._pipeline_cond:
        pipeline._pipeline_state["events"] = []
        pipeline._pipeline_state["running"] = True
        pipeline._pipeline_state["finished"] = False
    pipeline._cancel_flag.clear()
    cfg = load_project(project)
    pipeline._run_pipeline_thread(
        "daily", cfg.streams["daily"], project, str(project / "warehouse.duckdb"), False, USER,
    )
    events = list(pipeline._pipeline_state["events"])
    assert events[-1]["event"] == "complete"
    return events[-1]["data"]["status"] == "success"


def _run_ui_blocking(project: Path) -> bool:
    import havn.server.app as server_app
    from fastapi.testclient import TestClient

    # The endpoint wants an existing warehouse (it returns 404 otherwise).
    duckdb.connect(str(project / "warehouse.duckdb")).close()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = False
    resp = TestClient(server_app.app).post("/api/stream/daily")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"stream", "steps", "status", "duration_seconds"}
    return body["status"] == "success"


ENTRIES = {"cli": _run_cli, "scheduled": _run_scheduled, "ui": _run_ui, "ui_blocking": _run_ui_blocking}


# --- what a run left behind ---------------------------------------------------


def _rows(project: Path, sql: str) -> list[tuple]:
    """Read the warehouse: through the server's connection while it holds it."""
    from havn.server import deps

    if deps._write_queue is not None:
        cur = deps._write_queue.conn.cursor()
        try:
            return cur.execute(sql).fetchall()
        finally:
            cur.close()
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()

def _outcome(project: Path) -> dict:
    from havn.engine.snapshots import get_runs, get_snapshots_for_run

    log = _rows(project, (
        "SELECT run_type, target, status, CASE WHEN run_type = 'transform' THEN error END, "
        "pipeline_run_id FROM _havn.run_log ORDER BY started_at, run_id"
    ))
    runs = get_runs(project, 10)
    return {
        # Every node of the stream, in the order it ran, under one run id.
        "run_log": [r[:4] for r in log],
        "run_ids": len({r[4] for r in log}),
        "job_runs": _rows(project, (
            "SELECT job_name, job_file, status, steps_total, steps_completed, steps_failed, "
            "steps_skipped FROM _havn.job_runs"
        )),
        "perf": sorted(_rows(project, "SELECT model_path, status FROM _havn.model_perf")),
        "rewind": [(r.status, sorted(r.models_run)) for r in runs],
        "snapshots": sorted(
            s.model_name for r in runs for s in get_snapshots_for_run(project, r.run_id)
        ),
        "tables": sorted(_rows(project, (
            "SELECT table_schema || '.' || table_name FROM information_schema.tables "
            "WHERE table_schema IN ('landing', 'bronze', 'silver')"
        ))),
        "exported": (project / "exported.txt").exists(),
    }


def _run_everywhere(tmp_path: Path, files: dict | None = None) -> dict[str, tuple[bool, dict]]:
    out = {}
    for name, entry in ENTRIES.items():
        out[name] = _run_one(tmp_path / name, entry, files)
    return out


def _close_circuits() -> None:
    """The script circuit breaker is process-wide and keyed by file name: three
    failed ``orders.py`` runs in earlier entries would open it for the next."""
    from havn.engine.circuit_breaker import default_breaker

    for state in default_breaker.get_all_states():
        default_breaker.reset(state["name"])


@pytest.fixture(autouse=True)
def _fresh_circuits():
    _close_circuits()
    yield
    _close_circuits()


def _run_one(root: Path, entry, files: dict | None = None, yml: str = STREAM_YML) -> tuple[bool, dict]:
    from havn.server import deps

    # The server's connection belongs to one project at a time.
    deps.reset_shared_conn()
    _close_circuits()
    project = _make(root, files, yml=yml)
    ok = entry(project)
    outcome = _outcome(project)
    deps.reset_shared_conn()
    return ok, outcome


def _assert_same(results: dict[str, tuple[bool, dict]]) -> tuple[bool, dict]:
    (first_name, first), *rest = results.items()
    for name, other in rest:
        assert other[0] == first[0], f"{name} ok={other[0]}, {first_name} ok={first[0]}"
        for key in first[1]:
            assert other[1][key] == first[1][key], (
                f"{key}: {name} {other[1][key]!r} != {first_name} {first[1][key]!r}"
            )
    return first


@pytest.fixture(autouse=True)
def _reset_server():
    yield
    from havn.server import deps

    try:
        deps.reset_shared_conn()
    except Exception:
        pass


# --- the tests ----------------------------------------------------------------


def test_successful_stream_is_the_same_from_every_entry(tmp_path):
    ok, outcome = _assert_same(_run_everywhere(tmp_path))
    assert ok
    # Steps in their listed order: both loads, then the models, then the export.
    assert outcome["run_log"] == [
        ("ingest", "orders.py", "success", None),
        ("ingest", "zones.py", "success", None),
        ("transform", "bronze.orders", "success", None),
        ("transform", "silver.orders", "success", None),
        ("export", "report.py", "success", None),
    ]
    assert outcome["run_ids"] == 1
    assert outcome["job_runs"] == [("stream (daily)", "stream", "success", 5, 5, 0, 0)]
    assert outcome["perf"] == [("bronze.orders", "success"), ("silver.orders", "success")]
    assert outcome["rewind"] == [("success", ["bronze.orders", "silver.orders"])]
    assert outcome["snapshots"] == ["bronze.orders", "silver.orders"]
    assert outcome["exported"]


def test_failed_ingest_stops_the_stream_the_same_way_everywhere(tmp_path):
    ok, outcome = _assert_same(_run_everywhere(tmp_path, {
        "ingest/orders.py": "raise RuntimeError('source is down')\n",
    }))
    assert not ok
    # The failed load stops the rest of its step (zones.py never runs) and
    # every later step: nothing is built or exported.
    assert outcome["run_log"] == [
        ("ingest", "orders.py", "error", None),
        ("transform", "bronze.orders", "skipped", "upstream failed: ingest/orders.py"),
        ("transform", "silver.orders", "skipped", "upstream failed: ingest/orders.py"),
    ]
    assert outcome["job_runs"] == [("stream (daily)", "stream", "failure", 5, 0, 1, 4)]
    assert outcome["tables"] == []
    assert outcome["perf"] == []
    assert not outcome["exported"]
    assert outcome["rewind"] == [("failed", ["bronze.orders", "silver.orders"])]
    assert outcome["snapshots"] == []


def test_failed_assertion_stops_before_the_export_everywhere(tmp_path):
    ok, outcome = _assert_same(_run_everywhere(tmp_path, {
        "transform/silver/orders.sql": (
            "@config materialized=table, schema=silver\n"
            "@assert row_count > 100\n\n"
            "SELECT order_id, amount * 2 AS amount2 FROM bronze.orders\n"
        ),
    }))
    assert not ok
    statuses = [(r[0], r[1], r[2]) for r in outcome["run_log"]]
    assert statuses[:3] == [
        ("ingest", "orders.py", "success"),
        ("ingest", "zones.py", "success"),
        ("transform", "bronze.orders", "success"),
    ]
    assert statuses[3][:2] == ("transform", "silver.orders")
    assert all(r[0] != "export" for r in outcome["run_log"])
    assert not outcome["exported"]
    assert outcome["job_runs"][0][2] == "failure"
    # Only an assertion failed: the Rewind run is partial, as for `havn transform`.
    assert outcome["rewind"] == [("partial", ["bronze.orders", "silver.orders"])]


def test_cli_exit_code_follows_the_engine(tmp_path):
    from havn.cli import app

    project = _make(tmp_path / "p", {"ingest/orders.py": "raise RuntimeError('down')\n"})
    result = CliRunner().invoke(app, ["stream", "daily", "--project", str(project)])
    assert result.exit_code == 1
    assert "ingest failed: orders.py" in result.output
    assert "upstream failed: ingest/orders.py" in result.output
    assert "Export:" not in result.output


def test_ui_stream_runs_steps_in_listed_order_not_by_dag(tmp_path):
    """An export listed before a transform runs before it, from every entry."""
    yml = STREAM_YML.replace(
        "          - transform: [all]\n          - export: [all]\n",
        "          - export: [all]\n          - transform: [all]\n",
    )
    files = {"export/report.py": "print('exported')\n"}
    results = {}
    for name in ("cli", "ui"):
        results[name] = _run_one(tmp_path / name, ENTRIES[name], files, yml=yml)
    ok, outcome = _assert_same(results)
    assert ok
    assert [r[1] for r in outcome["run_log"]] == [
        "orders.py", "zones.py", "report.py", "bronze.orders", "silver.orders",
    ]


def test_scheduled_and_cli_streams_record_job_runs_and_pipeline_ids(tmp_path):
    from havn.config import load_project
    from havn.engine.stream_runner import run_stream

    project = _make(tmp_path / "p")
    result = run_stream(load_project(project), project, "daily", trigger="scheduled")
    assert result.ok
    rows = _rows(project, "SELECT id, trigger FROM _havn.job_runs")
    assert rows == [(result.pipeline_run_id, "scheduled")]
    ids = _rows(project, "SELECT DISTINCT pipeline_run_id FROM _havn.run_log")
    assert ids == [(result.pipeline_run_id,)]
