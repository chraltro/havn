"""Cancelling a pipeline run (``engine/pipeline_engine.py``).

A cancelled run starts no new node, but it must not report itself over
while a node is still writing: the web UI frees the project's run gate (and
the scheduler may start the next run) as soon as the run ends. Running
scripts are stopped the way a timeout stops them; one that cannot be
stopped is waited for.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import duckdb

from havn.config import load_project
from havn.engine.pipeline_engine import run_pipeline, stream_spec


def _project(tmp_path: Path, ingest_source: str) -> Path:
    project = tmp_path / "proj"
    for d in ("ingest", "export", "transform/bronze"):
        (project / d).mkdir(parents=True)
    (project / "project.yml").write_text(
        "name: t\ndatabase:\n  path: warehouse.duckdb\nrewind:\n  enabled: false\n"
        "streams:\n  s:\n    steps:\n      - ingest: [slow]\n      - export: [all]\n",
        encoding="utf-8",
    )
    (project / "ingest" / "slow.py").write_text(ingest_source, encoding="utf-8")
    (project / "export" / "report.py").write_text(
        f"open(r'{project / 'exported.txt'}', 'w').write('x')\n", encoding="utf-8",
    )
    return project


def _cancel_when(path: Path, cancel: threading.Event) -> threading.Thread:
    """Set ``cancel`` once ``path`` exists (the script has started)."""
    def watch():
        deadline = time.monotonic() + 30
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel.set()

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    return t


def _run(project: Path, cancel: threading.Event):
    config = load_project(project)
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    events: list[tuple[str, dict]] = []
    try:
        result = run_pipeline(stream_spec("s", config.streams["s"]), project, config, conn,
                              lambda e, d: events.append((e, dict(d))), cancel=cancel)
    finally:
        conn.close()
    return result, events


def _script_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("havn-script:") and t.is_alive()]


def test_cancel_stops_a_running_script_and_reports_it_before_the_run_ends(tmp_path):
    started = tmp_path / "started.txt"
    project = _project(tmp_path, (
        "import time\n"
        f"open(r'{started}', 'w').write('x')\n"
        "for _ in range(3000):\n"
        "    time.sleep(0.01)\n"
        "db.execute('CREATE TABLE landing.done AS SELECT 1 AS n')\n"
    ))
    cancel = threading.Event()
    _cancel_when(started, cancel)
    t0 = time.monotonic()
    result, events = _run(project, cancel)

    assert time.monotonic() - t0 < 20  # stopped, not run for 30 s
    assert result.status == "cancelled"
    names = [e for e, _ in events]
    assert names[-1] == "run_end"
    ends = [d for e, d in events if e == "node_end"]
    # The node that was running gets its node_end, before run_end.
    assert [(d["name"], d["status"]) for d in ends] == [("slow.py", "cancelled")]
    assert not (project / "exported.txt").exists()
    assert not _script_threads()
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        assert conn.execute(
            "SELECT status FROM _havn.run_log WHERE target = 'slow.py'"
        ).fetchall() == [("cancelled",)]
        assert conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'done'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_run_waits_for_a_script_that_cannot_be_interrupted(tmp_path, monkeypatch):
    """A script blocked in one C call outlives the stop attempt: the run waits."""
    from havn.engine import runner

    monkeypatch.setattr(runner, "SCRIPT_STOP_GRACE_SECONDS", 0.2)
    started = tmp_path / "started.txt"
    project = _project(tmp_path, (
        "import time\n"
        f"open(r'{started}', 'w').write('x')\n"
        "time.sleep(2.5)\n"
    ))
    cancel = threading.Event()
    _cancel_when(started, cancel)
    result, events = _run(project, cancel)
    script_started = started.stat().st_mtime

    # The run ended only after the script thread had gone.
    assert time.time() - script_started >= 2.3
    assert not _script_threads()
    assert result.status == "cancelled"
    names = [e for e, _ in events]
    assert names.index("node_end") < names.index("run_end")
    assert "cancelling" in names
    assert not (project / "exported.txt").exists()


def test_cancel_before_a_node_starts_runs_nothing(tmp_path):
    project = _project(tmp_path, "db.execute('CREATE TABLE landing.done AS SELECT 1 AS n')\n")
    cancel = threading.Event()
    cancel.set()
    result, events = _run(project, cancel)
    assert result.status == "cancelled"
    assert [e for e, _ in events if e == "node_end"] == []
