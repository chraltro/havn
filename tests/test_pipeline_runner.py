"""The web UI's pipeline runner (``server/routes/pipeline.py``).

The browser starts a run with ``POST /api/stream/{name}/start``,
``POST /api/pipeline/start`` or ``POST /api/transform/start`` and reads its
progress as server-sent events. These tests drive the background thread
functions directly (the endpoints only start them on a thread) against real
temporary warehouses, and read the events off the shared buffer.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _project(tmp_path: Path, extra_yaml: str = "", files: dict | None = None) -> Path:
    """A project with one ingest script, one model reading it and one export."""
    project = tmp_path / "proj"
    project.mkdir()
    _write(
        project / "project.yml",
        "name: runner\ndatabase:\n  path: warehouse.duckdb\n" + extra_yaml,
    )
    defaults = {
        "ingest/orders.py": (
            "db.execute('CREATE SCHEMA IF NOT EXISTS landing')\n"
            "db.execute(\"CREATE OR REPLACE TABLE landing.orders AS "
            "SELECT * FROM (VALUES (1, 10.0), (2, 20.0)) t(order_id, amount)\")\n"
            "print('Loaded 2 rows')\n"
        ),
        "transform/bronze/orders.sql": (
            "@config materialized=table, schema=bronze\n\n"
            "SELECT order_id, amount FROM landing.orders\n"
        ),
        "export/report.py": (
            "n = db.execute('SELECT count(*) FROM bronze.orders').fetchone()[0]\n"
            "print(f'Exported {n} rows')\n"
        ),
    }
    for rel, text in {**defaults, **(files or {})}.items():
        if text is None:
            continue
        _write(project / rel, text)
    return project


@pytest.fixture
def run_events():
    """Run a thread function with the server pointed at ``project``; return its events."""
    import havn.server.app as server_app
    from havn.server.routes import pipeline

    def _run(project: Path, fn_name: str, *args) -> list[dict]:
        server_app.PROJECT_DIR = project
        server_app.AUTH_ENABLED = False
        with pipeline._pipeline_cond:
            pipeline._pipeline_state["events"] = []
            pipeline._pipeline_state["running"] = True
            pipeline._pipeline_state["finished"] = False
        pipeline._cancel_flag.clear()
        getattr(pipeline, fn_name)(*args)
        return list(pipeline._pipeline_state["events"])

    return _run


def _query(project: Path, sql: str, params: list | None = None):
    """Read the warehouse once the server has let go of it."""
    from havn.server import deps

    if deps._write_queue is not None:
        cur = deps._write_queue.conn.cursor()
        try:
            return cur.execute(sql, params or []).fetchall()
        finally:
            cur.close()
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        return conn.execute(sql, params or []).fetchall()
    finally:
        conn.close()


def _shape(events: list[dict]) -> list[tuple]:
    """Each event as (name, sorted field names), without the timestamp."""
    return [(e["event"], tuple(sorted(k for k in e["data"] if k != "ts"))) for e in events]


def _ends(events: list[dict]) -> dict[str, dict]:
    return {e["data"]["name"]: e["data"] for e in events if e["event"] == "model_end"}


USER = {"username": "local", "role": "admin"}


# ---------------------------------------------------------------------------
# The event contract with the browser
# ---------------------------------------------------------------------------

_MODEL_START = ("model_start", ("action", "name", "num"))
_MODEL_START_T = ("model_start", ("action", "materialized", "name", "num"))
_MODEL_END = (
    "model_end",
    ("action", "duration_ms", "error", "materialized", "name", "num", "row_count", "rows_affected", "status"),
)
_COMPLETE = ("complete", ("duration_seconds", "pipeline_run_id", "status", "stream"))


def test_selective_pipeline_event_contract(tmp_path, run_events):
    """Pinned before the runner was refactored: names, fields and order."""
    project = _project(tmp_path)
    events = run_events(
        project, "_run_selective_pipeline_thread",
        ["ingest", "transform", "export"], False, project, USER,
    )

    assert _shape(events) == [
        ("start", ("label", "operation", "pipeline_run_id", "steps", "stream", "total")),
        _MODEL_START, _MODEL_END,
        _MODEL_START_T, _MODEL_END,
        _MODEL_START, _MODEL_END,
        _COMPLETE,
    ]
    start = events[0]["data"]
    assert start["operation"] == "pipeline"
    assert start["label"] == "Pipeline (ingest+transform+export)"
    assert start["stream"] == "pipeline"
    assert start["steps"] == 3
    assert start["total"] == 3

    starts = [e["data"] for e in events if e["event"] == "model_start"]
    assert [(s["name"], s["action"], s["num"]) for s in starts] == [
        ("orders.py", "ingest", 1), ("bronze.orders", "transform", 2), ("report.py", "export", 3),
    ]
    assert starts[1]["materialized"] == "table"

    ends = [e["data"] for e in events if e["event"] == "model_end"]
    assert [(d["name"], d["action"], d["status"], d["num"]) for d in ends] == [
        ("orders.py", "ingest", "success", 1),
        ("bronze.orders", "transform", "built", 2),
        ("report.py", "export", "success", 3),
    ]
    assert ends[0]["materialized"] is None and ends[2]["materialized"] is None
    assert ends[1]["materialized"] == "table"
    assert ends[1]["row_count"] == 2
    assert ends[0]["rows_affected"] == 2
    assert all(d["error"] is None for d in ends)

    done = events[-1]["data"]
    assert done["stream"] == "pipeline"
    assert done["status"] == "success"
    assert done["pipeline_run_id"] == start["pipeline_run_id"]


def test_stream_event_contract(tmp_path, run_events):
    project = _project(
        tmp_path,
        "streams:\n  daily:\n    steps:\n"
        "      - ingest: [all]\n      - transform: [all]\n      - export: [all]\n",
    )
    from havn.config import load_project

    cfg = load_project(project)
    events = run_events(
        project, "_run_pipeline_thread",
        "daily", cfg.streams["daily"], project, str(project / "warehouse.duckdb"), False, USER,
    )
    assert _shape(events) == [
        ("start", ("label", "operation", "pipeline_run_id", "steps", "stream", "total")),
        _MODEL_START, _MODEL_END,
        _MODEL_START_T, _MODEL_END,
        _MODEL_START, _MODEL_END,
        _COMPLETE,
    ]
    start = events[0]["data"]
    assert (start["operation"], start["label"], start["stream"], start["steps"], start["total"]) == (
        "stream", "Pipeline daily", "daily", 1, 3,
    )
    done = events[-1]["data"]
    assert (done["stream"], done["status"]) == ("daily", "success")
    assert [e["data"]["status"] for e in events if e["event"] == "model_end"] == [
        "success", "built", "success",
    ]

    # The run is recorded for Job Results.
    rows = _query(project, "SELECT job_name, status, steps_completed FROM _havn.job_runs WHERE id = ?",
                  [start["pipeline_run_id"]])
    assert rows == [("stream (daily)", "success", 3)]


def test_second_run_skips_unchanged_model(tmp_path, run_events):
    project = _project(tmp_path)
    first = run_events(project, "_run_selective_pipeline_thread",
                       ["ingest", "transform"], False, project, USER)
    assert _ends(first)["bronze.orders"]["status"] == "built"
    second = run_events(project, "_run_selective_pipeline_thread",
                        ["transform"], False, project, USER)
    assert _ends(second)["bronze.orders"]["status"] == "skipped"
    assert second[-1]["data"]["status"] == "success"
