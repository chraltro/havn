"""The process-wide run gate (engine/run_gate.py) between UI and scheduled runs.

Under ``havn serve --schedule`` the web UI and the scheduler's worker pool
run in one process against one warehouse. A UI run holds the gate alone; a
scheduled run shares it with other scheduled runs (``scheduler.max_concurrent``)
and waits while a UI run holds it; the UI answers "already running", saying
so when the holder is a scheduled run.
"""

from __future__ import annotations

import datetime as dt
import textwrap
import threading
import time
from pathlib import Path

import duckdb
import pytest

from havn.engine.run_gate import EXCLUSIVE, SHARED, GateBusy, GateTicket, RunGate, gate_for

UTC = dt.timezone.utc


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


def test_exclusive_is_refused_while_anyone_holds_the_gate():
    gate = RunGate("k")
    first = gate.try_exclusive("stream 'a'")
    assert isinstance(first, GateTicket)
    busy = gate.try_exclusive("transform")
    assert isinstance(busy, GateBusy)
    assert (busy.kind, busy.labels) == (EXCLUSIVE, ("stream 'a'",))
    first.release()
    first.release()  # twice is harmless
    assert isinstance(gate.try_exclusive("transform"), GateTicket)


def test_shared_holders_overlap_and_keep_exclusive_out():
    gate = RunGate("k")
    a = gate.acquire_shared("stream 'a'")
    b = gate.acquire_shared("job 'b'")
    busy = gate.try_exclusive("transform")
    assert isinstance(busy, GateBusy)
    assert busy.kind == SHARED
    assert busy.labels == ("stream 'a'", "job 'b'")
    assert "scheduled run is in progress" in busy.message
    a.release()
    assert isinstance(gate.try_exclusive("transform"), GateBusy)
    b.release()
    assert isinstance(gate.try_exclusive("transform"), GateTicket)


def test_shared_waits_for_exclusive_and_blocks_new_exclusive_meanwhile():
    gate = RunGate("k")
    ui = gate.try_exclusive("stream 'ui'")
    got: list[GateTicket] = []
    t = threading.Thread(target=lambda: got.append(gate.acquire_shared("stream 'nightly'")))
    t.start()
    deadline = time.monotonic() + 5
    while not gate.busy() or not gate._waiting:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert not got
    ui.release()
    t.join(5)
    assert got and got[0].kind == SHARED
    got[0].release()


def test_waiting_scheduled_run_keeps_new_ui_runs_out():
    gate = RunGate("k")
    ui = gate.try_exclusive("ui")
    proceed = threading.Event()
    held: list[GateTicket] = []

    def scheduled():
        held.append(gate.acquire_shared("nightly"))
        proceed.wait(5)
        held[0].release()

    waiter = threading.Thread(target=scheduled)
    waiter.start()
    while not gate._waiting:
        time.sleep(0.01)
    # A second UI run while the scheduled one waits: refused, it is next.
    busy = gate.try_exclusive("ui again")
    assert isinstance(busy, GateBusy)
    ui.release()
    # Whether or not it has woken yet, the scheduled run goes before any UI run.
    busy = gate.try_exclusive("ui again")
    assert isinstance(busy, GateBusy)
    assert "scheduled run" in busy.message
    proceed.set()
    waiter.join(5)
    assert isinstance(gate.try_exclusive("ui again"), GateTicket)


def test_waiting_shared_holder_gives_up_when_told_to_stop():
    gate = RunGate("k")
    ui = gate.try_exclusive("ui")
    stop = threading.Event()
    out: list = []
    t = threading.Thread(target=lambda: out.append(
        gate.acquire_shared("nightly", should_stop=stop.is_set, poll=0.05)))
    t.start()
    stop.set()
    t.join(5)
    assert out == [None]
    assert gate.busy().labels == ("ui",)
    ui.release()
    assert gate.busy() is None


def test_engine_modules_do_not_import_the_server():
    """The engine owns execution; the server only adapts its events."""
    import ast

    import havn.engine as engine_pkg

    root = Path(engine_pkg.__file__).parent
    for name in ("pipeline_engine.py", "stream_runner.py", "run_gate.py", "prebuild_gate.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            assert not any(m.startswith("havn.server") for m in mods), (name, mods)


def test_gate_is_one_per_project(tmp_path):
    assert gate_for(tmp_path) is gate_for(tmp_path / "." )
    assert gate_for(tmp_path) is not gate_for(tmp_path / "other")


# ---------------------------------------------------------------------------
# UI and scheduler in one process
# ---------------------------------------------------------------------------


YML = """\
    name: g
    database:
      path: warehouse.duckdb
    rewind:
      enabled: false
    scheduler:
      timezone: UTC
      max_concurrent: {max_concurrent}
    streams:
      nightly:
        schedule: "0 6 * * *"
        steps:
          - ingest: [tick]
      hourly:
        schedule: "0 6 * * *"
        steps:
          - ingest: [tock]
      manual:
        steps:
          - ingest: [tick]
"""


def _script(table: str) -> str:
    return (
        "db.execute('CREATE SCHEMA IF NOT EXISTS landing')\n"
        f"db.execute('CREATE TABLE IF NOT EXISTS landing.{table} (n INTEGER)')\n"
        f"db.execute('INSERT INTO landing.{table} VALUES (1)')\n"
    )


def _project(tmp_path: Path, max_concurrent: int = 1) -> Path:
    p = tmp_path / "proj"
    (p / "ingest").mkdir(parents=True)
    (p / "project.yml").write_text(textwrap.dedent(YML.format(max_concurrent=max_concurrent)),
                                   encoding="utf-8")
    (p / "ingest" / "tick.py").write_text(_script("ticks"), encoding="utf-8")
    (p / "ingest" / "tock.py").write_text(_script("tocks"), encoding="utf-8")
    # Metadata and landing exist up front: two runs creating them at the same
    # moment conflict, which is unrelated to the gate.
    from havn.engine.database import ensure_meta_table

    conn = duckdb.connect(str(p / "warehouse.duckdb"))
    ensure_meta_table(conn)
    conn.execute("CREATE SCHEMA landing")
    conn.close()
    return p


def _history(project: Path, name: str) -> list[tuple]:
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        return conn.execute(
            "SELECT status, lateness_ms FROM _havn.schedule_history WHERE schedule_name = ? "
            "ORDER BY scheduled_for", [name],
        ).fetchall()
    finally:
        conn.close()


_SCHEDULERS: list = []


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    while _SCHEDULERS:
        t = _SCHEDULERS.pop()
        t.shutdown_grace = 10
        t.stop()


class Clock:
    def __init__(self, t: dt.datetime):
        self.t = t

    def __call__(self) -> dt.datetime:
        return self.t


def _scheduler(project: Path, clock: Clock, runner=None):
    from havn.engine.scheduler import SchedulerThread

    t = SchedulerThread(project, clock=clock, runner=runner)
    t._started_at = clock.t
    _SCHEDULERS.append(t)
    return t


@pytest.fixture
def client_for():
    import havn.server.app as server_app
    from fastapi.testclient import TestClient
    from havn.server.routes import pipeline

    def make(project: Path) -> TestClient:
        server_app.PROJECT_DIR = project
        server_app.AUTH_ENABLED = False
        with pipeline._pipeline_cond:
            pipeline._pipeline_state["running"] = False
            pipeline._pipeline_state["finished"] = False
            pipeline._pipeline_state["events"] = []
        return TestClient(server_app.app)

    yield make


def _wait_ui_idle(timeout: float = 60) -> None:
    from havn.server.routes import pipeline

    deadline = time.monotonic() + timeout
    while pipeline._pipeline_state["running"]:
        assert time.monotonic() < deadline, "UI run never finished"
        time.sleep(0.05)


def test_ui_is_refused_while_a_scheduled_run_is_in_progress(tmp_path, client_for):
    project = _project(tmp_path)
    clock = Clock(dt.datetime(2026, 4, 6, 6, 0, 0, tzinfo=UTC))
    started, release = threading.Event(), threading.Event()
    holder: list = []

    def runner(task):
        if task.spec.name == "nightly":
            started.set()
            assert release.wait(60)
        return holder[0]._default_runner(task)

    t = _scheduler(project, clock, runner)
    holder.append(t)
    # nightly and hourly are both due; one worker, so nightly runs first.
    t._tick(now=dt.datetime(2026, 4, 6, 6, 0, 5, tzinfo=UTC))
    assert started.wait(30)

    client = client_for(project)
    r = client.post("/api/stream/manual/start").json()
    assert r["status"] == "already_running"
    assert r["scheduled"] is True
    assert "scheduled run is in progress" in r["message"]
    assert "stream 'nightly'" in r["message"]
    # The other UI operations that write to the warehouse are refused too.
    r = client.post("/api/transform/start", json={}).json()
    assert r["status"] == "already_running" and r["scheduled"] is True
    assert client.post("/api/stream/manual").status_code == 409
    assert client.post("/api/run", json={"script_path": "ingest/tick.py"}).status_code == 409
    # Lint never touches the warehouse: it is not held back.
    assert client.post("/api/lint/start").json()["status"] == "started"
    _wait_ui_idle()

    release.set()
    assert t.wait_idle(120)
    assert gate_for(project).wait_free(10)
    assert client.post("/api/stream/manual/start").json()["status"] == "started"
    _wait_ui_idle()


def test_scheduled_run_waits_for_a_ui_run_and_records_the_wait(tmp_path, client_for):
    from havn.server.routes import pipeline

    project = _project(tmp_path)
    clock = Clock(dt.datetime(2026, 4, 6, 6, 0, 0, tzinfo=UTC))
    ran: list[str] = []
    holder: list = []

    def runner(task):
        ran.append(task.spec.name)
        return holder[0]._default_runner(task)

    t = _scheduler(project, clock, runner)
    holder.append(t)

    # A UI operation holds the gate until we let it finish.
    ui_release = threading.Event()

    def ui_op():
        try:
            assert ui_release.wait(60)
        finally:
            pipeline._finish_operation()

    client_for(project)
    assert pipeline._start_operation("stream", "manual", ui_op, ())["status"] == "started"

    t._tick(now=dt.datetime(2026, 4, 6, 6, 0, 5, tzinfo=UTC))
    time.sleep(1.0)
    # Queued, not started, while the UI run holds the gate.
    assert ran == []
    assert {s for s, _ in _history(project, "nightly")} == {"queued"}

    clock.t = dt.datetime(2026, 4, 6, 6, 3, 0, tzinfo=UTC)   # the UI run took three minutes
    ui_release.set()
    assert t.wait_idle(120)
    assert sorted(ran) == ["hourly", "nightly"]
    (status, lateness), = _history(project, "nightly")
    assert status == "success"
    assert lateness == 180_000
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        assert conn.execute("SELECT count(*) FROM landing.ticks").fetchone()[0] == 1
    finally:
        conn.close()


def test_scheduled_runs_still_overlap_each_other_with_max_concurrent(tmp_path, client_for):
    project = _project(tmp_path, max_concurrent=2)
    clock = Clock(dt.datetime(2026, 4, 6, 6, 0, 0, tzinfo=UTC))
    both = threading.Barrier(3, timeout=60)
    release = threading.Event()
    holder: list = []

    def runner(task):
        both.wait()          # passes only when both runs hold the gate at once
        assert release.wait(60)
        return holder[0]._default_runner(task)

    t = _scheduler(project, clock, runner)
    holder.append(t)
    assert t.max_concurrent == 2
    t._tick(now=dt.datetime(2026, 4, 6, 6, 0, 5, tzinfo=UTC))
    both.wait()
    busy = gate_for(project).busy()
    assert busy.kind == SHARED
    assert sorted(busy.labels) == ["stream 'hourly'", "stream 'nightly'"]
    r = client_for(project).post("/api/stream/manual/start").json()
    assert r["status"] == "already_running" and r["scheduled"] is True
    release.set()
    assert t.wait_idle(120)
    assert [s for s, _ in _history(project, "nightly")] == ["success"]
    assert [s for s, _ in _history(project, "hourly")] == ["success"]


def test_legacy_sse_endpoint_refuses_while_busy_and_keeps_a_pending_cancel(tmp_path, client_for):
    """GET /api/stream/{name}/events while another operation runs."""
    from havn.server.routes import pipeline

    project = _project(tmp_path)
    client = client_for(project)
    release = threading.Event()

    def ui_op():
        try:
            assert release.wait(60)
        finally:
            pipeline._finish_operation()

    assert pipeline._start_operation("stream", "manual", ui_op, ())["status"] == "started"
    try:
        client.post("/api/stream/cancel")                 # aimed at the running op
        assert pipeline._cancel_flag.is_set()
        r = client.get("/api/stream/manual/events")
        assert r.status_code == 409
        assert "already running" in r.json()["detail"]
        assert pipeline._cancel_flag.is_set()             # not cleared by the refused request
    finally:
        release.set()
        _wait_ui_idle()

    # A scheduled run holding the gate: refused too, not another op's events.
    ticket = gate_for(project).acquire_shared("stream 'nightly'")
    try:
        r = client.get("/api/stream/manual/events")
        assert r.status_code == 409
        assert "scheduled run is in progress" in r.json()["detail"]
    finally:
        ticket.release()


def test_ungated_operation_finishes_without_waiting_for_an_open_transaction(tmp_path, client_for):
    """Lint's end does not checkpoint: a FORCE CHECKPOINT would wait for a
    scheduled run's open transaction with catalog changes."""
    from havn.engine.write_queue import cursor_for
    from havn.server import deps
    from havn.server.routes import pipeline

    project = _project(tmp_path)
    client_for(project)
    deps.reset_shared_conn()
    cur = cursor_for(deps._get_shared_conn())
    cur.execute("BEGIN")
    cur.execute("CREATE SCHEMA mid_run")
    cur.execute("CREATE TABLE mid_run.x AS SELECT range AS i FROM range(200000)")
    try:
        r = pipeline._start_operation("lint", "Lint", pipeline._finish_operation, (), gated=False)
        assert r["status"] == "started"
        deadline = time.monotonic() + 5
        while pipeline._pipeline_state["running"]:
            assert time.monotonic() < deadline, "lint's end waited for the open transaction"
            time.sleep(0.02)
    finally:
        cur.execute("ROLLBACK")
        cur.close()
        _wait_ui_idle()
        deps.reset_shared_conn()


def test_stopping_scheduler_cancels_a_run_waiting_for_the_gate(tmp_path):
    project = _project(tmp_path)
    clock = Clock(dt.datetime(2026, 4, 6, 6, 0, 0, tzinfo=UTC))
    ran: list[str] = []
    t = _scheduler(project, clock, lambda task: (ran.append(task.spec.name), ("success", None))[1])
    ui = gate_for(project).try_exclusive("stream 'ui'")
    try:
        t._tick(now=dt.datetime(2026, 4, 6, 6, 0, 5, tzinfo=UTC))
        time.sleep(0.5)
        t.shutdown_grace = 10
        t.stop()
        _SCHEDULERS.remove(t)
    finally:
        ui.release()
    assert ran == []
    statuses = {s for s, _ in _history(project, "nightly")}
    assert statuses == {"cancelled"}
