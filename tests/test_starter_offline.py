"""The `havn init` starter project builds cleanly on its offline sample data.

Behind a firewall (and on every first run without network) the ingest
notebook falls back to built-in sample earthquakes. That data has to exercise
every model: gold.region_risk keeps only regions with two or more events, so
a sample with one event per region left it empty and failing its own
`row_count > 0` assertion.
"""
from __future__ import annotations

import duckdb
from typer.testing import CliRunner

from havn.cli import app


def test_starter_project_builds_offline(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "demo"])
    assert result.exit_code == 0, result.output
    project = tmp_path / "demo"
    monkeypatch.chdir(project)

    # A proxy nobody listens on makes the USGS fetch fail fast, as offline.
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    result = runner.invoke(app, ["run", "ingest/earthquakes.dpnb"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["transform"])
    assert result.exit_code == 0, result.output

    conn = duckdb.connect(str(project / "warehouse.duckdb"), read_only=True)
    try:
        assert conn.execute("SELECT count(*) FROM landing.earthquakes").fetchone()[0] == 25
        assert conn.execute("SELECT count(*) FROM gold.region_risk").fetchone()[0] >= 3
        failed = conn.execute(
            "SELECT model_path, expression, detail FROM _havn.assertion_results WHERE NOT passed"
        ).fetchall()
        assert failed == []
        errors = conn.execute(
            "SELECT target, error FROM _havn.run_log WHERE status = 'error'"
        ).fetchall()
        assert errors == []
    finally:
        conn.close()
