"""`havn schedule` with job-only projects, and `havn transform` alerts on the log channel."""

from __future__ import annotations

import duckdb
import pytest
from typer.testing import CliRunner

from havn.cli import app

runner = CliRunner()


def _project(tmp_path, extra_yaml: str = ""):
    (tmp_path / "project.yml").write_text(
        "name: t\ndatabase:\n  path: warehouse.duckdb\n" + extra_yaml, encoding="utf-8"
    )
    (tmp_path / "transform" / "bronze").mkdir(parents=True)
    return tmp_path


class _FakeScheduler:
    """Stands in for SchedulerThread so the command returns instead of blocking."""

    started: list = []

    def __init__(self, project_dir, **kwargs):
        self.project_dir = project_dir

    def start(self):
        _FakeScheduler.started.append(self.project_dir)

    def join(self):
        return None

    def stop(self):
        return None


@pytest.fixture
def fake_scheduler(monkeypatch):
    import havn.engine.scheduler as scheduler

    _FakeScheduler.started = []
    monkeypatch.setattr(scheduler, "SchedulerThread", _FakeScheduler)
    return _FakeScheduler


def test_schedule_starts_for_a_project_with_only_scheduled_jobs(tmp_path, fake_scheduler):
    project = _project(tmp_path)
    (project / "orchestration").mkdir()
    (project / "orchestration" / "nightly.yml").write_text(
        'name: nightly\ntargets:\n  - bronze.*\nschedules:\n  - "0 6 * * *"\n', encoding="utf-8"
    )

    result = runner.invoke(app, ["schedule", "--project", str(project)])

    assert result.exit_code == 0, result.output
    assert "nightly" in result.output
    assert "0 6 * * *" in result.output
    assert fake_scheduler.started == [project]


def test_schedule_does_not_start_with_nothing_scheduled(tmp_path, fake_scheduler):
    project = _project(tmp_path)

    result = runner.invoke(app, ["schedule", "--project", str(project)])

    assert result.exit_code == 0, result.output
    assert "No scheduled streams or jobs" in result.output
    assert fake_scheduler.started == []


def test_schedule_ignores_a_disabled_job(tmp_path, fake_scheduler):
    project = _project(tmp_path)
    (project / "orchestration").mkdir()
    (project / "orchestration" / "off.yml").write_text(
        'name: off\nenabled: false\ntargets:\n  - bronze.*\nschedules:\n  - "0 6 * * *"\n',
        encoding="utf-8",
    )

    result = runner.invoke(app, ["schedule", "--project", str(project)])

    assert result.exit_code == 0, result.output
    assert fake_scheduler.started == []


def _alert_rows(project):
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        return conn.execute(
            "SELECT alert_type, channel FROM _havn.alert_log ORDER BY sent_at"
        ).fetchall()
    except duckdb.CatalogException:
        return []
    finally:
        conn.close()


def test_transform_alerts_on_the_log_channel_without_a_url(tmp_path):
    project = _project(tmp_path, "alerts:\n  channels: [log]\n")
    (project / "transform" / "bronze" / "one.sql").write_text(
        "@config materialized=table\n\nSELECT 1 AS id\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["transform", "--project", str(project)])

    assert result.exit_code == 0, result.output
    assert ("pipeline_success", "log") in _alert_rows(project)


def test_transform_failure_alert_respects_on_failure_false(tmp_path):
    project = _project(tmp_path, "alerts:\n  channels: [log]\n  on_failure: false\n")
    (project / "transform" / "bronze" / "bad.sql").write_text(
        "@config materialized=table\n\nSELECT * FROM landing.missing_table\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["transform", "--project", str(project), "--skip-check"])

    assert result.exit_code == 1, result.output
    assert _alert_rows(project) == []


def test_transform_failure_alert_is_sent_by_default(tmp_path):
    project = _project(tmp_path, "alerts:\n  channels: [log]\n")
    (project / "transform" / "bronze" / "bad.sql").write_text(
        "@config materialized=table\n\nSELECT * FROM landing.missing_table\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["transform", "--project", str(project), "--skip-check"])

    assert result.exit_code == 1, result.output
    assert ("pipeline_failure", "log") in _alert_rows(project)
