"""The file watcher's rebuilds and the project's run gate.

A transform change is a pending rebuild; the watcher's loop runs it once
the burst of saves is over and only when no other run holds the gate. A
change that comes in while a UI or scheduled run is going is not dropped.
Driven through ``request_rebuild`` / ``try_rebuild`` with injected time;
no watchdog thread needed.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import pytest

from havn.engine.run_gate import GateBusy, gate_for
from havn.engine.scheduler import FileWatcher


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / "transform" / "bronze").mkdir(parents=True)
    (project / "project.yml").write_text("name: t\ndatabase:\n  path: warehouse.duckdb\n", encoding="utf-8")
    (project / "transform" / "bronze" / "one.sql").write_text(
        "@config materialized=table, schema=bronze\n\nSELECT 1 AS n\n", encoding="utf-8",
    )
    return project


def _tables(project: Path) -> list[str]:
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        return [r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'bronze'"
        ).fetchall()]
    finally:
        conn.close()


@pytest.fixture
def gate(tmp_path):
    g = gate_for(tmp_path / "proj")
    yield g
    assert g.wait_free(5)


def test_rebuild_waits_for_a_running_operation_and_then_runs(tmp_path, gate, caplog):
    project = _project(tmp_path)
    w = FileWatcher(project, debounce=2.0, clock=lambda: 100.0)

    assert w.try_rebuild(now=100.0) == "idle"
    w.request_rebuild(None)
    assert w.try_rebuild(now=101.0) == "waiting"       # still inside the burst

    ui = gate.try_exclusive("stream 'nightly'")
    assert not isinstance(ui, GateBusy)
    with caplog.at_level(logging.INFO, logger="havn.scheduler"):
        assert w.try_rebuild(now=103.0) == "busy"
        assert w.try_rebuild(now=104.0) == "busy"
    pending_logs = [r for r in caplog.records if "rebuild pending" in r.getMessage()]
    assert len(pending_logs) == 1                       # logged once, not per poll
    assert "nightly" in pending_logs[0].getMessage()
    assert w.pending == []                              # not dropped
    assert not (project / "warehouse.duckdb").exists()  # nothing built meanwhile

    # A change during the wait joins the pending rebuild.
    w.request_rebuild(["bronze.one"])
    ui.release()
    assert w.try_rebuild(now=105.0) == "ran"
    assert _tables(project) == ["one"]
    assert w.pending is None
    assert w.try_rebuild(now=200.0) == "idle"


def test_rebuild_holds_the_gate_exclusively(tmp_path, gate):
    project = _project(tmp_path)
    seen = []

    def connect():
        seen.append(gate.busy())
        return duckdb.connect(str(project / "warehouse.duckdb"))

    w = FileWatcher(project, debounce=0, connect=connect)
    w.request_rebuild(["bronze.one"])
    assert w.try_rebuild() == "ran"
    assert seen and seen[0] is not None and seen[0].kind == "exclusive"
    assert gate.busy() is None


def test_a_burst_of_changes_builds_once(tmp_path, gate):
    project = _project(tmp_path)
    now = [0.0]
    opened = []

    def connect():
        opened.append(1)
        return duckdb.connect(str(project / "warehouse.duckdb"))

    w = FileWatcher(project, debounce=2.0, connect=connect, clock=lambda: now[0])
    for t in (0.0, 0.5, 1.0, 1.5):
        now[0] = t
        w.request_rebuild(["bronze.one"])
        assert w.try_rebuild() == "waiting"
    now[0] = 3.6
    assert w.try_rebuild() == "ran"
    assert opened == [1]


def test_serve_watcher_builds_on_the_servers_shared_connection(tmp_path, monkeypatch):
    import uvicorn
    from typer.testing import CliRunner

    import havn.engine.scheduler as scheduler_mod
    import havn.server.app as server_app
    from havn.cli import app

    captured = []

    class _Fake:
        def __init__(self, project_dir, **kwargs):
            captured.append(kwargs)

        def start(self):
            pass

    project = _project(tmp_path)
    monkeypatch.setattr(scheduler_mod, "FileWatcher", _Fake)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(server_app, "PROJECT_DIR", server_app.PROJECT_DIR)
    monkeypatch.setattr(server_app, "ACTIVE_ENV", server_app.ACTIVE_ENV)
    monkeypatch.setattr(server_app, "AUTH_ENABLED", server_app.AUTH_ENABLED)
    result = CliRunner().invoke(app, ["serve", "--watch", "--port", "39872", "--project", str(project)])
    assert result.exit_code == 0, result.output
    (kwargs,) = captured
    assert callable(kwargs.get("connect"))
