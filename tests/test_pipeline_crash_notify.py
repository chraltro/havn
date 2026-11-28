"""A pipeline run that breaks off still sends its webhook and alert.

Both crash paths of ``engine/pipeline_engine.py``: planning fails, or the
run raises part-way. Before, only a run that reached its normal end
notified anyone.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from havn.config import load_project
from havn.engine import pipeline_engine
from havn.engine.pipeline_engine import run_pipeline, stream_spec


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / "transform" / "bronze").mkdir(parents=True)
    (project / "project.yml").write_text(
        "name: t\ndatabase:\n  path: warehouse.duckdb\nrewind:\n  enabled: false\n"
        "alerts:\n  channels: [log]\n"
        "streams:\n  s:\n    webhook_url: http://127.0.0.1:9/hook\n"
        "    steps:\n      - transform: [all]\n",
        encoding="utf-8",
    )
    (project / "transform" / "bronze" / "one.sql").write_text(
        "@config materialized=table, schema=bronze\n\nSELECT 1 AS n\n", encoding="utf-8",
    )
    return project


def _run(project: Path):
    config = load_project(project)
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    events: list[tuple[str, dict]] = []
    try:
        result = run_pipeline(stream_spec("s", config.streams["s"]), project, config, conn,
                              lambda e, d: events.append((e, dict(d))))
        alerts = conn.execute(
            "SELECT alert_type, target FROM _havn.alert_log ORDER BY sent_at"
        ).fetchall()
    finally:
        conn.close()
    return result, events, alerts


def _boom(*_a, **_k):
    raise RuntimeError("boom")


@pytest.mark.parametrize("where", ["planning", "mid-run"])
def test_crashed_run_notifies(tmp_path, monkeypatch, where):
    project = _project(tmp_path)
    if where == "planning":
        monkeypatch.setattr(pipeline_engine, "_plan_run", _boom)
    else:
        from havn.engine.transform import orchestration

        monkeypatch.setattr(orchestration, "detect_run_anomalies", _boom)
    result, events, alerts = _run(project)

    assert result.crashed and result.status == "failed"
    names = [e for e, _ in events]
    assert names.count("run_end") == 1
    webhooks = [d for e, d in events if e == "webhook"]
    assert len(webhooks) == 1 and webhooks[0]["url"] == "http://127.0.0.1:9/hook"
    assert names.index("run_end") < names.index("webhook")
    assert alerts == [("pipeline_failure", "s")]
