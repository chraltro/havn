"""Regressions from the CLI audit: init, MCP stdout, lint, packages, connectors.

Each test pins one confirmed bug:
- `havn init` into a non-empty directory overwrote .env and project.yml.
- MCP run_transform let the engine's Rich output onto stdout (JSON-RPC).
- A fresh `havn init` project failed `havn lint` (LT13/LT15 on directives).
- `havn packages list` was documented but did not exist.
- The CSV connector broke on a path containing a quote.
- `havn connect` suggested a re-test command that does not exist.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from havn.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# havn init
# ---------------------------------------------------------------------------


def test_init_refuses_non_empty_directory(tmp_path):
    target = tmp_path / "proj"
    target.mkdir()
    (target / ".env").write_text("SECRET=keep-me\n", encoding="utf-8")
    (target / "project.yml").write_text("name: mine\n", encoding="utf-8")

    result = runner.invoke(app, ["init", "proj", "--dir", str(target)])

    assert result.exit_code == 1
    assert "not empty" in result.output
    assert (target / ".env").read_text(encoding="utf-8") == "SECRET=keep-me\n"
    assert (target / "project.yml").read_text(encoding="utf-8") == "name: mine\n"
    assert not (target / "transform").exists()


def test_init_force_adds_missing_files_but_never_overwrites(tmp_path):
    target = tmp_path / "proj"
    (target / "transform" / "gold").mkdir(parents=True)
    (target / ".env").write_text("SECRET=keep-me\n", encoding="utf-8")
    (target / "project.yml").write_text("name: mine\n", encoding="utf-8")
    model = target / "transform" / "gold" / "top_earthquakes.sql"
    model.write_text("SELECT 42 AS mine\n", encoding="utf-8")

    result = runner.invoke(app, ["init", "proj", "--dir", str(target), "--force"])

    assert result.exit_code == 0, result.output
    assert (target / ".env").read_text(encoding="utf-8") == "SECRET=keep-me\n"
    assert (target / "project.yml").read_text(encoding="utf-8") == "name: mine\n"
    assert model.read_text(encoding="utf-8") == "SELECT 42 AS mine\n"
    # Missing scaffold files are still added.
    assert (target / ".gitignore").exists()
    assert (target / "transform" / "bronze" / "earthquakes.sql").exists()


def test_init_allows_directory_with_only_git(tmp_path):
    target = tmp_path / "proj"
    (target / ".git").mkdir(parents=True)
    result = runner.invoke(app, ["init", "proj", "--dir", str(target), "--empty"])
    assert result.exit_code == 0, result.output
    assert (target / "project.yml").exists()


# ---------------------------------------------------------------------------
# havn lint on a fresh scaffold
# ---------------------------------------------------------------------------


def test_fresh_init_lints_clean_and_fix_keeps_directives(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init", "demo"]).exit_code == 0
    project = tmp_path / "demo"
    before = {
        p: p.read_text(encoding="utf-8") for p in (project / "transform").rglob("*.sql")
    }
    assert before

    result = runner.invoke(app, ["lint", "-p", str(project)])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["lint", "--fix", "-p", str(project)])
    assert result.exit_code == 0, result.output
    for path, text in before.items():
        assert path.read_text(encoding="utf-8") == text, path


def test_lint_text_keeps_line_numbers():
    from havn.lint.linter import _lint_text

    sql = "@config materialized=table\n@assert row_count > 0\n\nSELECT 1 AS a\n"
    out = _lint_text(sql)
    assert out.split("\n") == ["--", "--", "", "SELECT 1 AS a", ""]


# ---------------------------------------------------------------------------
# havn packages list
# ---------------------------------------------------------------------------


def test_packages_list_subcommand_exists(tmp_path):
    (tmp_path / "project.yml").write_text("name: p\n", encoding="utf-8")
    listed = runner.invoke(app, ["packages", "list", "-p", str(tmp_path)])
    bare = runner.invoke(app, ["packages", "-p", str(tmp_path)])
    assert listed.exit_code == 0, listed.output
    assert bare.exit_code == 0, bare.output
    assert "No packages installed" in listed.output
    assert listed.output == bare.output


# ---------------------------------------------------------------------------
# CSV connector quoting + follow-up hint
# ---------------------------------------------------------------------------


def test_csv_connector_handles_quote_in_path(tmp_path):
    from havn.connectors.csv_file import CSVConnector

    folder = tmp_path / "O'Brien's data"
    folder.mkdir()
    csv_file = folder / "sales.csv"
    csv_file.write_text("id,amount\n1,100\n2,200\n", encoding="utf-8")

    script = CSVConnector().generate_script({"path": str(csv_file)}, ["sales"], "landing")
    conn = duckdb.connect()
    try:
        exec(compile(script, "ingest.py", "exec"), {"db": conn})
        assert conn.execute("SELECT COUNT(*) FROM landing.sales").fetchone()[0] == 2
    finally:
        conn.close()


@pytest.mark.parametrize(
    "path",
    ['C:\\data\\a"b\'c.csv', 'https://example.com/a"b\'c.csv', "/tmp/x\ny.csv"],
)
def test_csv_connector_script_always_compiles(path):
    from havn.connectors.csv_file import CSVConnector

    script = CSVConnector().generate_script({"path": path}, ["t"], "landing")
    compile(script, "ingest.py", "exec")


def test_csv_connector_rejects_unknown_format():
    from havn.connectors.csv_file import CSVConnector

    with pytest.raises(ValueError, match="Unknown format"):
        CSVConnector().generate_script({"path": "a.csv", "format": 'csv"'}, ["t"], "landing")


def test_connect_retest_hint_is_a_real_command():
    source = (Path(__file__).parents[1] / "src" / "havn" / "cli" / "connectors.py").read_text(
        encoding="utf-8"
    )
    assert "havn connect --test --name" not in source
    assert "havn connectors test" in source


# ---------------------------------------------------------------------------
# MCP: engine output never reaches the protocol stream
# ---------------------------------------------------------------------------


def _mcp_project(tmp_path: Path) -> Path:
    (tmp_path / "project.yml").write_text(
        "name: test\ndatabase:\n  path: warehouse.duckdb\nrewind:\n  enabled: false\n",
        encoding="utf-8",
    )
    bronze = tmp_path / "transform" / "bronze"
    bronze.mkdir(parents=True)
    (bronze / "x.sql").write_text(
        "@config materialized=table, schema=bronze\n\nSELECT 1 AS a\n", encoding="utf-8"
    )
    duckdb.connect(str(tmp_path / "warehouse.duckdb")).close()
    return tmp_path


_MESSAGES = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "run_transform", "arguments": {"force": True}},
    },
]


def test_mcp_serve_keeps_engine_output_off_protocol_stream(tmp_path, monkeypatch):
    from havn.mcp.server import MCPServer

    project = _mcp_project(tmp_path)
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in _MESSAGES))
    out = io.StringIO()
    # In a real session the protocol stream *is* sys.stdout, and the engine's
    # module-level Rich Console resolves sys.stdout on every write.
    monkeypatch.setattr(sys, "stdout", out)
    MCPServer(project).serve(stdin=stdin, stdout=out)

    lines = [line for line in out.getvalue().splitlines() if line.strip()]
    frames = [json.loads(line) for line in lines]  # every line is a frame
    assert [f["id"] for f in frames] == [1, 2]
    payload = json.loads(frames[1]["result"]["content"][0]["text"])
    assert payload["results"] == {"bronze.x": "built"}


def test_mcp_real_stdio_session_stdout_is_pure_json(tmp_path):
    project = _mcp_project(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-c", "from havn.cli import app; app()", "mcp", "-p", str(project)],
        input="".join(json.dumps(m) + "\n" for m in _MESSAGES),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    frames = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert [f["id"] for f in frames] == [1, 2]
    # The engine did print progress; it just went to stderr.
    assert "bronze.x" in proc.stderr


def test_init_force_appends_missing_gitignore_lines(tmp_path):
    target = tmp_path / "proj"
    target.mkdir()
    (target / ".gitignore").write_text("node_modules/", encoding="utf-8")

    result = runner.invoke(app, ["init", "proj", "--dir", str(target), "--force", "--empty"])

    assert result.exit_code == 0, result.output
    lines = (target / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "node_modules/"
    assert ".env" in lines and "*.duckdb" in lines
    # Idempotent: a second run adds nothing.
    before = (target / ".gitignore").read_text(encoding="utf-8")
    runner.invoke(app, ["init", "proj", "--dir", str(target), "--force", "--empty"])
    assert (target / ".gitignore").read_text(encoding="utf-8") == before


def test_csv_connector_accepts_uppercase_format():
    from havn.connectors.csv_file import CSVConnector

    script = CSVConnector().generate_script({"path": "a.dat", "format": "PARQUET"}, ["t"], "landing")
    assert "read_parquet" in script
