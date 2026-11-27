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


# ---------------------------------------------------------------------------
# Models are built the way `havn transform` builds them
# ---------------------------------------------------------------------------


def _stream_project(tmp_path: Path, steps_yaml: str, files: dict | None = None, extra: str = "") -> Path:
    return _project(
        tmp_path, f"streams:\n  s:\n{extra}    steps:\n{steps_yaml}", files=files,
    )


def _run_stream(run_events, project: Path, name: str = "s", force: bool = False) -> list[dict]:
    from havn.config import load_project

    cfg = load_project(project)
    return run_events(
        project, "_run_pipeline_thread",
        name, cfg.streams[name], project, str(project / "warehouse.duckdb"), force, USER,
    )


def test_stream_builds_a_model_from_an_installed_package(tmp_path, run_events):
    """discover_models(transform/) never saw havn_packages/."""
    from havn.config import load_project
    from havn.engine.packages import install_packages

    pkg = tmp_path / "crm"
    _write(pkg / "havn_package.yml", "name: crm\nversion: 1.0.0\n")
    _write(pkg / "transform" / "bronze" / "contacts.sql",
           "@config materialized=table, schema=bronze\n\nSELECT 1 AS id, 'a@b.com' AS email\n")
    project = _stream_project(tmp_path, "      - ingest: [all]\n      - transform: [all]\n")
    with (project / "project.yml").open("a", encoding="utf-8") as f:
        f.write(f"packages:\n  - name: crm\n    path: {pkg.as_posix()}\n")
    results = install_packages(project, load_project(project))
    assert [r.status for r in results] == ["installed"]

    events = _run_stream(run_events, project)
    ends = _ends(events)
    assert ends["crm_bronze.contacts"]["status"] == "built"
    assert ends["bronze.orders"]["status"] == "built"
    assert events[-1]["data"]["status"] == "success"
    assert _query(project, "SELECT count(*) FROM crm_bronze.contacts") == [(1,)]

    # Hashes agree with the CLI's: `havn transform` right after finds nothing to do.
    from havn.engine.transform import run_transform
    from havn.server import deps

    cur = deps._write_queue.conn.cursor()
    try:
        cli = run_transform(cur, project / "transform", project_dir=project)
    finally:
        cur.close()
    assert set(cli.values()) == {"skipped"}, cli


def test_ui_run_records_perf_data(tmp_path, run_events):
    project = _project(tmp_path)
    events = run_events(project, "_run_selective_pipeline_thread",
                        ["ingest", "transform"], False, project, USER)
    run_id = events[0]["data"]["pipeline_run_id"]
    rows = _query(project, "SELECT status FROM _havn.model_perf "
                           "WHERE model_path = 'bronze.orders' AND pipeline_run_id = ?", [run_id])
    assert rows == [("success",)]


def test_stale_error_source_is_not_built(tmp_path, run_events):
    project = _project(tmp_path, files={
        "ingest/orders.py": (
            "db.execute('CREATE SCHEMA IF NOT EXISTS landing')\n"
            "db.execute(\"CREATE OR REPLACE TABLE landing.orders AS SELECT 1 AS order_id, "
            "1.0 AS amount, CAST('2020-01-01' AS TIMESTAMP) AS ts\")\n"
        ),
        "transform/bronze/orders.sql": (
            "@config materialized=table, schema=bronze\n"
            "@source_freshness landing.orders, max_age=1s, on=ts\n\n"
            "SELECT order_id, amount FROM landing.orders\n"
        ),
        "transform/silver/orders.sql": (
            "@config materialized=table, schema=silver\n\nSELECT * FROM bronze.orders\n"
        ),
        "export/report.py": None,
    })
    events = run_events(project, "_run_selective_pipeline_thread",
                        ["ingest", "transform"], False, project, USER)
    ends = _ends(events)
    # The browser shows a status it does not know as "Built", so a stale
    # source is reported as a skip, with the reason in `error`.
    assert ends["bronze.orders"]["status"] == "skipped"
    assert "source stale: landing.orders" in ends["bronze.orders"]["error"]
    assert ends["silver.orders"]["status"] == "skipped_upstream_blocked"
    assert _query(project, "SELECT count(*) FROM information_schema.tables "
                           "WHERE table_schema IN ('bronze', 'silver')") == [(0,)]
    assert events[-1]["data"]["status"] == "success"


def test_snapshot_model_is_profiled(tmp_path, run_events):
    project = _project(tmp_path, files={
        "transform/silver/order_history.sql": (
            "@config materialized=snapshot, schema=silver, unique_key=order_id\n\n"
            "SELECT order_id, amount FROM bronze.orders\n"
        ),
        "export/report.py": None,
    })
    events = run_events(project, "_run_selective_pipeline_thread",
                        ["ingest", "transform"], False, project, USER)
    assert _ends(events)["silver.order_history"]["status"] == "built"
    assert _query(project, "SELECT row_count FROM _havn.model_profiles "
                           "WHERE model_path = 'silver.order_history'") == [(2,)]


def test_parent_built_in_this_run_rebuilds_the_child(tmp_path, run_events):
    """A parent rebuilt in this run means new inputs, though the child's SQL did not change."""
    project = _project(tmp_path, files={
        "transform/silver/orders.sql": (
            "@config materialized=table, schema=silver\n\nSELECT * FROM bronze.orders\n"
        ),
        "export/report.py": None,
    })
    run_events(project, "_run_selective_pipeline_thread", ["ingest", "transform"], False, project, USER)
    # The parent changes (an extra column); the child's SQL does not.
    _write(project / "transform" / "bronze" / "orders.sql",
           "@config materialized=table, schema=bronze\n\n"
           "SELECT order_id, amount, 1 AS v FROM landing.orders\n")
    events = run_events(project, "_run_selective_pipeline_thread", ["transform"], False, project, USER)
    ends = _ends(events)
    assert ends["bronze.orders"]["status"] == "built"
    assert ends["silver.orders"]["status"] == "built"
    assert _query(project, "SELECT count(*) FROM information_schema.columns "
                           "WHERE table_schema = 'silver' AND column_name = 'v'") == [(1,)]


def test_rewind_snapshots_are_taken_for_a_ui_run(tmp_path, run_events):
    from havn.engine.snapshots import get_runs, get_snapshots_for_run

    project = _project(tmp_path)
    run_events(project, "_run_selective_pipeline_thread", ["ingest", "transform"], False, project, USER)
    runs = get_runs(project, 10)
    assert len(runs) == 1
    assert (runs[0].status, runs[0].trigger, runs[0].models_run) == ("success", "ui", ["bronze.orders"])
    snaps = get_snapshots_for_run(project, runs[0].run_id)
    assert [s.model_name for s in snaps] == ["bronze.orders"]


# ---------------------------------------------------------------------------
# POST /api/transform/start
# ---------------------------------------------------------------------------


def test_transform_run_fails_on_error_assertion(tmp_path, run_events):
    project = _project(tmp_path, files={
        "transform/bronze/orders.sql": (
            "@config materialized=table, schema=bronze\n"
            "@assert row_count > 100\n\n"
            "SELECT order_id, amount FROM landing.orders\n"
        ),
    })
    run_events(project, "_run_selective_pipeline_thread", ["ingest"], False, project, USER)
    events = run_events(project, "_run_transform_thread", None, False, project)
    assert _ends(events)["bronze.orders"]["status"] == "assertion_failed"
    assert events[-1]["event"] == "complete"
    assert events[-1]["data"]["status"] == "failed"


def test_transform_run_event_contract(tmp_path, run_events):
    """Same start / model_end / complete shapes as before, now with live progress."""
    project = _project(tmp_path)
    run_events(project, "_run_selective_pipeline_thread", ["ingest"], False, project, USER)
    events = run_events(project, "_run_transform_thread", ["bronze.orders"], True, project)
    assert _shape(events) == [
        ("start", ("label", "operation", "pipeline_run_id")),
        _MODEL_START_T,
        ("model_end", ("action", "duration_ms", "name", "num", "row_count", "status")),
        ("complete", ("duration_seconds", "operation", "pipeline_run_id", "status")),
    ]
    assert events[0]["data"]["label"] == "Transform (bronze.orders) --force"
    end = events[2]["data"]
    assert (end["name"], end["action"], end["status"], end["row_count"], end["num"]) == (
        "bronze.orders", "transform", "built", 2, 1,
    )
    assert events[-1]["data"]["status"] == "success"


# ---------------------------------------------------------------------------
# A stream runs its own steps
# ---------------------------------------------------------------------------


def test_stream_runs_only_its_steps_and_targets(tmp_path, run_events):
    project = _stream_project(
        tmp_path,
        "      - ingest: [orders]\n      - transform: [bronze.orders]\n",
        files={
            "ingest/customers.py": (
                "db.execute('CREATE SCHEMA IF NOT EXISTS landing')\n"
                "db.execute('CREATE OR REPLACE TABLE landing.customers AS SELECT 1 AS id')\n"
            ),
            "transform/bronze/customers.sql": (
                "@config materialized=table, schema=bronze\n\nSELECT id FROM landing.customers\n"
            ),
        },
    )
    events = _run_stream(run_events, project)
    ends = _ends(events)
    assert set(ends) == {"orders.py", "bronze.orders"}
    assert events[0]["data"]["total"] == 2
    assert events[-1]["data"]["status"] == "success"
    assert _query(project, "SELECT count(*) FROM information_schema.tables "
                           "WHERE table_name = 'customers'") == [(0,)]


def test_stream_transform_targets_are_graph_selectors(tmp_path, run_events):
    project = _stream_project(
        tmp_path,
        "      - transform: ['+silver.orders']\n",
        files={
            "transform/silver/orders.sql": (
                "@config materialized=table, schema=silver\n\nSELECT * FROM bronze.orders\n"
            ),
            "transform/silver/other.sql": (
                "@config materialized=table, schema=silver\n\nSELECT 1 AS x\n"
            ),
        },
    )
    run_events(project, "_run_selective_pipeline_thread", ["ingest"], False, project, USER)
    events = _run_stream(run_events, project)
    assert set(_ends(events)) == {"bronze.orders", "silver.orders"}
    assert [e["data"]["name"] for e in events if e["event"] == "model_start"] == [
        "bronze.orders", "silver.orders",
    ]


def test_stream_runs_seed_steps(tmp_path, run_events):
    project = _stream_project(
        tmp_path,
        "      - seed: [all]\n      - transform: [silver.countries]\n",
        files={
            "seeds/countries.csv": "code,name\nNO,Norway\nDK,Denmark\n",
            "transform/silver/countries.sql": (
                "@config materialized=table, schema=silver\n\nSELECT code FROM seeds.countries\n"
            ),
        },
    )
    events = _run_stream(run_events, project)
    starts = [(e["data"]["name"], e["data"]["action"]) for e in events if e["event"] == "model_start"]
    assert starts == [("seeds.countries", "seed"), ("silver.countries", "transform")]
    ends = _ends(events)
    assert ends["seeds.countries"]["status"] == "built"
    assert ends["seeds.countries"]["row_count"] == 2
    assert ends["silver.countries"]["status"] == "built"
    assert events[-1]["data"]["status"] == "success"


def test_stream_retries_a_failed_step(tmp_path, run_events):
    marker = (tmp_path / "attempted").as_posix()
    project = _stream_project(
        tmp_path,
        "      - ingest: [flaky]\n",
        extra="    retries: 1\n    retry_delay: 0\n",
        files={
            "ingest/flaky.py": (
                "from pathlib import Path\n"
                f"marker = Path(r'{marker}')\n"
                "if not marker.exists():\n"
                "    marker.write_text('x')\n"
                "    raise RuntimeError('first attempt fails')\n"
                "print('ok')\n"
            ),
        },
    )
    events = _run_stream(run_events, project)
    assert _ends(events)["flaky.py"]["status"] == "success"
    assert events[-1]["data"]["status"] == "success"


def test_stream_without_retries_reports_the_failure(tmp_path, run_events):
    project = _stream_project(
        tmp_path,
        "      - ingest: [broken]\n",
        files={"ingest/broken.py": "raise RuntimeError('nope')\n"},
    )
    events = _run_stream(run_events, project)
    assert _ends(events)["broken.py"]["status"] == "error"
    assert events[-1]["data"]["status"] == "failed"
    rows = _query(project, "SELECT status, steps_failed FROM _havn.job_runs WHERE id = ?",
                  [events[0]["data"]["pipeline_run_id"]])
    assert rows == [("failure", 1)]


def test_stream_with_an_unknown_step_fails(tmp_path, run_events):
    project = _stream_project(tmp_path, "      - notify: [all]\n")
    events = _run_stream(run_events, project)
    assert [e["event"] for e in events] == ["complete"]
    assert events[0]["data"]["status"] == "failed"
