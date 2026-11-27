"""Rewriting user-owned YAML (project.yml, job files) without losing comments.

Every test here edits a real file in ``tmp_path`` and reads the result back
both as text (comments, quoting) and with PyYAML / ``load_project`` (what the
rest of havn sees).
"""
from __future__ import annotations

import copy
import os
import subprocess
import sys
import threading
from pathlib import Path

import duckdb
import pytest
import yaml
from fastapi.testclient import TestClient

from havn import yamlio

RICH_PROJECT = """\
# havn project config -- header comment
# second header line
name: "rich-project"   # quoted name, inline comment
description: 'single quoted'

database:
  path: warehouse.duckdb   # where the data lives
  memory_limit: "4GB"

connections:
  prod_pg:
    type: postgres
    host: ${DB_HOST}
    password: ${DB_PASSWORD}
    port: 5432
  # Example: connect to a MySQL database
  # legacy_mysql:
  #   type: mysql
  #   host: ${MYSQL_HOST}

streams:
  nightly:
    description: Nightly refresh
    steps:
    - ingest:
      - all
    - transform:
      - all
    schedule: "0 6 * * *"

# alerts:
#   slack:
#     webhook_url: ${SLACK_WEBHOOK}

lint:
  dialect: duckdb   # sqlfluff dialect
"""

RICH_COMMENTS = [ln.strip() for ln in RICH_PROJECT.splitlines() if "#" in ln]


def _comment_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if "#" in ln]


def _assert_comments_kept(before: str, after: str, *, dropped: tuple[str, ...] = ()) -> None:
    after_lines = after.splitlines()
    for line in before.splitlines():
        if "#" not in line or any(d in line for d in dropped):
            continue
        comment = line[line.index("#"):]
        assert any(comment in a for a in after_lines), f"comment lost: {comment!r}\n---\n{after}"


def _project(tmp_path: Path, text: str = RICH_PROJECT) -> Path:
    (tmp_path / "project.yml").write_text(text, encoding="utf-8")
    (tmp_path / "ingest").mkdir(exist_ok=True)
    (tmp_path / "transform" / "bronze").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".env").write_text("DB_HOST=db.local\nDB_PASSWORD=pw\n", encoding="utf-8")
    return tmp_path


def _read(path: Path) -> str:
    # write_text() on Windows writes CRLF, and the rewrite keeps the file's
    # convention; compare text with LF (test_crlf_and_bom checks the bytes).
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


# ---------------------------------------------------------------------------
# Connector setup / removal (engine/connector.py)
# ---------------------------------------------------------------------------


def _old_update(raw: dict, name: str, params: dict, script: str, schedule) -> dict:
    """What the PyYAML-based _update_project_yml produced (data only)."""
    raw = copy.deepcopy(raw)
    if "connections" not in raw or raw["connections"] is None:
        raw["connections"] = {}
    raw["connections"][name] = params
    if "streams" not in raw or raw["streams"] is None:
        raw["streams"] = {}
    raw["streams"][f"sync-{name}"] = {
        "description": f"Sync data from {name}",
        "steps": [{"ingest": [script.replace(".py", "")]}, {"transform": ["all"]}],
        "schedule": schedule,
    }
    return raw


def test_connector_add_and_remove_keep_comments(tmp_path):
    from havn.config import load_project
    from havn.engine.connector import _remove_from_project_yml, _update_project_yml

    project = _project(tmp_path)
    path = project / "project.yml"
    before = yaml.safe_load(RICH_PROJECT)
    params = {"type": "csv", "path": "data/in.csv", "token": "${CSV_TOKEN}", "flag": "on"}

    _update_project_yml(project, "my_csv", params, "connector_my_csv.py", "0 6 * * *")
    text = _read(path)
    _assert_comments_kept(RICH_PROJECT, text)
    assert 'name: "rich-project"   # quoted name, inline comment' in text
    assert "description: 'single quoted'" in text
    assert "host: ${DB_HOST}" in text
    assert "token: ${CSV_TOKEN}" in text
    assert 'memory_limit: "4GB"' in text
    # Same data as the old dump-based code, as PyYAML sees it.
    expected = _old_update(before, "my_csv", params, "connector_my_csv.py", "0 6 * * *")
    assert yaml.safe_load(text) == expected
    # New values are ordinary block YAML, in the file's sequence style.
    assert "  my_csv:\n    type: csv\n" in text
    assert "    steps:\n    - ingest:\n      - connector_my_csv\n" in text
    # The commented-out example still follows the connections block.
    assert text.index("my_csv:") < text.index("# Example: connect to a MySQL")
    assert text.index("# Example: connect to a MySQL") < text.index("\nstreams:")

    cfg = load_project(project)
    assert cfg.connections["my_csv"].type == "csv"
    assert cfg.connections["prod_pg"].params["host"] == "db.local"
    assert "sync-my_csv" in cfg.streams

    assert _remove_from_project_yml(project, "my_csv") is True
    after = _read(path)
    _assert_comments_kept(RICH_PROJECT, after)
    assert yaml.safe_load(after) == before
    # Removing what is not there writes nothing.
    mtime = path.stat().st_mtime_ns
    assert _remove_from_project_yml(project, "my_csv") is False
    assert path.stat().st_mtime_ns == mtime


def test_setup_connector_end_to_end_keeps_comments(tmp_path):
    from havn.config import load_project
    from havn.engine.connector import remove_connector, setup_connector

    project = _project(tmp_path)
    csv_file = project / "data.csv"
    csv_file.write_text("id,name\n1,a\n", encoding="utf-8")
    result = setup_connector(
        project_dir=project, connector_type="csv", connection_name="test_csv",
        config={"path": str(csv_file)}, target_schema="landing",
    )
    assert result["status"] == "success"
    text = _read(project / "project.yml")
    _assert_comments_kept(RICH_PROJECT, text)
    cfg = load_project(project)
    assert cfg.connections["test_csv"].type == "csv"
    assert "sync-test_csv" in cfg.streams

    assert remove_connector(project, "test_csv")["status"] == "success"
    after = _read(project / "project.yml")
    _assert_comments_kept(RICH_PROJECT, after)
    assert yaml.safe_load(after) == yaml.safe_load(RICH_PROJECT)


def test_connector_null_and_missing_sections(tmp_path):
    from havn.engine.connector import _update_project_yml

    # `connections:` present but null, `streams` missing entirely.
    project = _project(tmp_path, "name: p\n# conns below\nconnections:\nlint:\n  dialect: duckdb\n")
    _update_project_yml(project, "c1", {"type": "csv"}, "connector_c1.py", None)
    text = _read(project / "project.yml")
    assert "# conns below" in text
    data = yaml.safe_load(text)
    assert data["connections"] == {"c1": {"type": "csv"}}
    assert data["streams"]["sync-c1"]["schedule"] is None
    assert data["lint"] == {"dialect": "duckdb"}


def test_connector_creates_missing_project_yml(tmp_path):
    from havn.engine.connector import _update_project_yml

    _update_project_yml(tmp_path, "c1", {"type": "csv"}, "connector_c1.py", None)
    data = yaml.safe_load(_read(tmp_path / "project.yml"))
    assert data["name"] == tmp_path.name
    assert data["database"] == {"path": "warehouse.duckdb"}
    assert data["connections"] == {"c1": {"type": "csv"}}


def test_scaffolded_project_keeps_comments_after_connector(tmp_path):
    from typer.testing import CliRunner

    from havn.cli import app
    from havn.config import load_project
    from havn.engine.connector import _remove_from_project_yml, _update_project_yml

    target = tmp_path / "demo"
    result = CliRunner().invoke(app, ["init", "demo", "--dir", str(target)])
    assert result.exit_code == 0, result.output
    path = target / "project.yml"
    original = _read(path)
    assert original.count("#") > 5

    _update_project_yml(target, "pg", {"type": "postgres", "host": "localhost"}, "connector_pg.py", None)
    text = _read(path)
    _assert_comments_kept(original, text)
    assert load_project(target).connections["pg"].type == "postgres"

    _remove_from_project_yml(target, "pg")
    after = _read(path)
    _assert_comments_kept(original, after)
    expected = yaml.safe_load(original)
    expected["streams"] = {}
    assert yaml.safe_load(after) == expected
    # The scaffold's `connections: {}` + commented example reads as before.
    assert "connections: {}\n  # Example: connect to a PostgreSQL database\n" in after


# ---------------------------------------------------------------------------
# Server endpoints: anomaly config, database config
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    import havn.server.app as server_app
    from havn.server.deps import _clear_config_cache, reset_shared_conn

    project = _project(tmp_path)
    duckdb.connect(str(project / "warehouse.duckdb")).close()
    reset_shared_conn()
    _clear_config_cache()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = False
    yield TestClient(server_app.app), project
    reset_shared_conn()
    _clear_config_cache()


def test_anomaly_config_endpoint_keeps_comments(client):
    c, project = client
    # Read once so the mtime cache holds the old config.
    assert c.get("/api/anomalies/config").json()["lookback"] == 30
    resp = c.put("/api/anomalies/config", json={"enabled": False, "lookback": 14})
    assert resp.status_code == 200
    assert resp.json() == {"status": "updated", "enabled": False, "lookback": 14, "threshold": 2.0}

    text = _read(project / "project.yml")
    _assert_comments_kept(RICH_PROJECT, text)
    data = yaml.safe_load(text)
    assert data["quality"] == {"anomaly_detection": {"enabled": False, "lookback": 14}}
    # The mtime-keyed config cache sees the replaced file (no explicit clear).
    got = c.get("/api/anomalies/config").json()
    assert got["enabled"] is False and got["lookback"] == 14

    # A second edit changes the value in place.
    c.put("/api/anomalies/config", json={"threshold": 3.5})
    data = yaml.safe_load(_read(project / "project.yml"))
    assert data["quality"]["anomaly_detection"] == {"enabled": False, "lookback": 14, "threshold": 3.5}
    assert c.get("/api/anomalies/config").json()["threshold"] == 3.5


def test_database_config_endpoint_keeps_comments(client):
    from havn.config import load_project

    c, project = client
    resp = c.put("/api/config/database", json={"memory_limit": "8GB", "threads": 4})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "memory_limit": "8GB", "threads": 4}
    text = _read(project / "project.yml")
    _assert_comments_kept(RICH_PROJECT, text)
    # The old value was double-quoted; the new one keeps that style.
    assert 'memory_limit: "8GB"' in text
    assert "path: warehouse.duckdb   # where the data lives" in text
    cfg = load_project(project)
    assert cfg.database.memory_limit == "8GB"
    assert cfg.database.threads == 4
    assert c.get("/api/config/database").json() == {"memory_limit": "8GB", "threads": 4}

    # Blank / zero remove the keys again.
    c.put("/api/config/database", json={"memory_limit": "", "threads": 0})
    after = _read(project / "project.yml")
    _assert_comments_kept(RICH_PROJECT, after)
    expected = yaml.safe_load(RICH_PROJECT)
    del expected["database"]["memory_limit"]
    assert yaml.safe_load(after) == expected


def test_resources_allocation_keeps_comments(client):
    c, project = client
    resp = c.put("/api/resources/allocation", json={
        "category": "query", "memory_gb": 2.0, "threads": 2, "max_concurrent": 3,
    })
    assert resp.status_code == 200
    text = _read(project / "project.yml")
    _assert_comments_kept(RICH_PROJECT, text)
    assert yaml.safe_load(text)["resources"] == {
        "query": {"memory_gb": 2.0, "threads": 2, "max_concurrent": 3},
    }


# ---------------------------------------------------------------------------
# Job files: route, CLI, save_job
# ---------------------------------------------------------------------------

JOB = """\
# Nightly job -- edited by hand
name: test-job
description: Rebuild orders
targets:
  - silver.orders   # the main target
target: silver.orders
resolve: upstream
enabled: true
on_failure: true
retry: 1
# Uncomment to run automatically:
# schedules:
#   - "0 6 * * *"
"""


@pytest.fixture
def job_project(tmp_path):
    (tmp_path / "project.yml").write_text("name: jobs\ndatabase:\n  path: warehouse.duckdb\n", encoding="utf-8")
    (tmp_path / "transform" / "silver").mkdir(parents=True)
    (tmp_path / "transform" / "silver" / "orders.sql").write_text("SELECT 1 AS id\n", encoding="utf-8")
    (tmp_path / "orchestration").mkdir()
    (tmp_path / "orchestration" / "test-job.yml").write_text(JOB, encoding="utf-8")
    duckdb.connect(str(tmp_path / "warehouse.duckdb")).close()
    return tmp_path


def test_job_schedule_edit_through_route_keeps_comments(job_project):
    import havn.server.app as server_app
    from havn.engine.orchestration import discover_jobs
    from havn.server.deps import reset_shared_conn

    reset_shared_conn()
    server_app.PROJECT_DIR = job_project
    server_app.AUTH_ENABLED = False
    try:
        c = TestClient(server_app.app)
        resp = c.patch("/api/jobs/test-job", json={"schedules": ["0 6 * * *", "every 2 hours"]})
        assert resp.status_code == 200, resp.text
        path = job_project / "orchestration" / "test-job.yml"
        text = _read(path)
        _assert_comments_kept(JOB, text)
        # Indentation style of the file is kept for the new list too.
        assert "  - silver.orders   # the main target" in text
        assert "schedules:\n  - 0 6 * * *\n  - every 2 hours\n" in text
        data = yaml.safe_load(text)
        assert data["schedules"] == ["0 6 * * *", "every 2 hours"]
        assert data["cron"] == "0 6 * * *"
        assert data["on_failure"] is True
        job = [j for j in discover_jobs(job_project) if j.name == "test-job"][0]
        assert job.schedules == ["0 6 * * *", "every 2 hours"]

        # A rejected schedule leaves the file exactly as it was.
        bad = c.patch("/api/jobs/test-job", json={"schedules": ["not a cron"]})
        assert bad.status_code == 400
        assert _read(path) == text
    finally:
        reset_shared_conn()


def test_job_enable_disable_through_cli_keeps_comments(job_project):
    from typer.testing import CliRunner

    from havn.cli import app

    path = job_project / "orchestration" / "test-job.yml"
    r = CliRunner().invoke(app, ["jobs", "disable", "test-job", "--project", str(job_project)])
    assert r.exit_code == 0, r.output
    text = _read(path)
    _assert_comments_kept(JOB, text)
    expected = yaml.safe_load(JOB)
    expected["enabled"] = False
    assert yaml.safe_load(text) == expected
    assert "enabled: false\n" in text

    r = CliRunner().invoke(app, ["jobs", "enable", "test-job", "--project", str(job_project)])
    assert r.exit_code == 0, r.output
    assert _read(path) == JOB


def test_save_job_over_existing_file_keeps_comments(job_project):
    from havn.engine.orchestration import save_job

    path = save_job(job_project, {
        "name": "test-job", "description": "Rebuild orders", "targets": ["silver.orders"],
        "resolve": "upstream", "enabled": True, "on_failure": True, "retry": 2,
        "schedules": ["0 6 * * *"],
    })
    text = _read(path)
    assert "# Nightly job -- edited by hand" in text
    assert "# the main target" in text
    assert yaml.safe_load(text) == {
        "name": "test-job", "description": "Rebuild orders", "targets": ["silver.orders"],
        "target": "silver.orders", "resolve": "upstream", "enabled": True,
        "on_failure": True, "retry": 2, "schedules": ["0 6 * * *"], "cron": "0 6 * * *",
        "tags": [],
    }


def test_save_job_new_file_matches_old_output(job_project):
    from havn.engine.orchestration import save_job

    path = save_job(job_project, {"name": "fresh", "targets": ["silver.orders"], "full_refresh": True})
    data = yaml.safe_load(_read(path))
    assert data == {"name": "fresh", "targets": ["silver.orders"], "target": "silver.orders",
                    "cron": "", "tags": []}
    assert list(data) == ["name", "targets", "target", "cron", "tags"]


# ---------------------------------------------------------------------------
# migrate's database-block rewrite
# ---------------------------------------------------------------------------


def test_migrate_rewrite_keeps_comments(tmp_path):
    from havn.cli.migrate import _rewrite_project_yml
    from havn.config import DatabaseConfig

    project = _project(tmp_path)
    dest = DatabaseConfig(backend="ducklake", catalog=".havn/catalog.ducklake", data_path=".havn/data")
    _rewrite_project_yml(project, dest)
    text = _read(project / "project.yml")
    _assert_comments_kept(RICH_PROJECT, text, dropped=("where the data lives",))
    data = yaml.safe_load(text)
    assert data["database"] == {
        "backend": "ducklake", "catalog": ".havn/catalog.ducklake", "data_path": ".havn/data",
        "encrypted": False,
    }
    assert data["connections"] == yaml.safe_load(RICH_PROJECT)["connections"]
    assert not list(project.glob("*.tmp")) and not list(project.glob(".*.tmp"))


# ---------------------------------------------------------------------------
# YAML 1.1 (PyYAML) semantics across a rewrite
# ---------------------------------------------------------------------------

YAML11 = """\
enabled: yes
paused: off
legacy: On
answer: no
y: n
on_failure: true
cron: "0 6 * * *"
plain_cron: 0 6 * * *
octal: 012
sexagesimal: 1:30
big: 1_000
ratio: 1.5e+3
when: 2024-01-01
nothing: null
tilde: ~
quoted_yes: "yes"
"""


def test_yaml11_values_keep_their_meaning(tmp_path):
    path = tmp_path / "f.yml"
    path.write_text(YAML11, encoding="utf-8")
    before = yaml.safe_load(YAML11)
    with yamlio.edit_yaml(path) as doc:
        # Values read through the helper mean what they mean to PyYAML.
        assert doc["enabled"] is True and doc["paused"] is False
        assert doc["y"] == "n"
        assert doc["octal"] == 10 and doc["sexagesimal"] == 90
        yamlio.set_value(doc, "added", "x")
    text = _read(path)
    # Plain strings and numbers are written back as they were.
    for line in ("plain_cron: 0 6 * * *", 'cron: "0 6 * * *"', "octal: 012", "big: 1_000",
                 "ratio: 1.5e+3", "when: 2024-01-01", 'quoted_yes: "yes"', "y: n"):
        assert line + "\n" in text, line
    after = yaml.safe_load(text)
    del after["added"]
    assert after == before
    for k in before:
        assert type(after[k]) is type(before[k]), k


@pytest.mark.parametrize("value", [
    "on", "off", "yes", "no", "On", "NO", "true", "False", "y", "n", "~", "null", "",
    "1:30", "012", "0x1F", "1e3", "1.5", "1_000", ".inf", "2024-01-01", "0 6 * * *",
    "*/5 * * * *", "every 2 hours", "${VAR}", "a: b", "- x", "#hash", "@at", "=",
])
def test_new_strings_read_back_as_strings(tmp_path, value):
    path = tmp_path / "f.yml"
    path.write_text("a: 1\n", encoding="utf-8")
    with yamlio.edit_yaml(path) as doc:
        yamlio.set_value(doc, "v", value)
        yamlio.set_value(doc, "nested", {"list": [value, {"k": value}]})
    data = yaml.safe_load(_read(path))
    assert data["v"] == value and type(data["v"]) is str
    assert data["nested"] == {"list": [value, {"k": value}]}


def test_new_non_strings_keep_their_type(tmp_path):
    path = tmp_path / "f.yml"
    path.write_text("a: 1\n", encoding="utf-8")
    values = {"t": True, "f": False, "n": None, "i": 7, "fl": 2.5, "neg": -3, "e": {}, "l": []}
    with yamlio.edit_yaml(path) as doc:
        for k, v in values.items():
            yamlio.set_value(doc, k, v)
    data = yaml.safe_load(_read(path))
    for k, v in values.items():
        assert data[k] == v and type(data[k]) is type(v), k


def test_long_strings_are_not_rewrapped(tmp_path):
    path = tmp_path / "f.yml"
    long = " ".join(["word"] * 60)
    path.write_text(f"description: {long}\n", encoding="utf-8")
    with yamlio.edit_yaml(path) as doc:
        yamlio.set_value(doc, "other", long)
    text = _read(path)
    assert f"description: {long}\n" in text
    assert f"other: {long}\n" in text


# ---------------------------------------------------------------------------
# Empty / comment-only files, encodings, newlines
# ---------------------------------------------------------------------------


def test_empty_file(tmp_path):
    path = tmp_path / "f.yml"
    path.write_text("", encoding="utf-8")
    with yamlio.edit_yaml(path) as doc:
        assert dict(doc) == {}
        yamlio.ensure_map(doc, "connections")["c"] = {"type": "csv"}
    assert yaml.safe_load(_read(path)) == {"connections": {"c": {"type": "csv"}}}


def test_comment_only_file(tmp_path):
    path = tmp_path / "f.yml"
    original = "# only comments here\n#   key: value\n"
    path.write_text(original, encoding="utf-8")
    with yamlio.edit_yaml(path):
        pass
    assert _read(path) == original  # nothing added: untouched
    with yamlio.edit_yaml(path) as doc:
        yamlio.set_value(doc, "name", "p")
    text = _read(path)
    assert text.startswith(original)
    assert yaml.safe_load(text) == {"name": "p"}


def test_crlf_and_bom(tmp_path):
    path = tmp_path / "f.yml"
    path.write_bytes(b"\xef\xbb\xbf# comment \xc3\xb8\r\nname: Troms\xc3\xb8\r\n")
    with yamlio.edit_yaml(path) as doc:
        yamlio.set_value(doc, "city", "Bodø")
    data = path.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert data == b"# comment \xc3\xb8\r\nname: Troms\xc3\xb8\r\ncity: Bod\xc3\xb8\r\n"


def test_untouched_file_is_not_rewritten(tmp_path):
    path = tmp_path / "f.yml"
    path.write_text(RICH_PROJECT, encoding="utf-8")
    mtime = path.stat().st_mtime_ns
    with yamlio.edit_yaml(path) as doc:
        yamlio.set_value(doc["database"], "path", "warehouse.duckdb")  # same value
    assert path.stat().st_mtime_ns == mtime
    assert _read(path) == RICH_PROJECT


def test_invalid_yaml_is_refused_and_left_alone(tmp_path):
    path = tmp_path / "f.yml"
    bad = "a: [1, 2\nb: 2\n"
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(yamlio.YamlLoadError):
        with yamlio.edit_yaml(path):
            pass
    dup = "a: 1\na: 2\n"
    path.write_text(dup, encoding="utf-8")
    with pytest.raises(yamlio.YamlLoadError):
        with yamlio.edit_yaml(path):
            pass
    assert _read(path) == dup


# ---------------------------------------------------------------------------
# Atomic write, locking, cache invalidation
# ---------------------------------------------------------------------------


def test_interrupted_replace_leaves_original_and_no_temp(tmp_path, monkeypatch):
    path = tmp_path / "project.yml"
    path.write_text(RICH_PROJECT, encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(yamlio.os, "replace", boom)
    with pytest.raises(OSError, match="disk on fire"):
        with yamlio.edit_yaml(path) as doc:
            yamlio.set_value(doc, "x", 1)
    assert _read(path) == RICH_PROJECT
    assert sorted(p.name for p in tmp_path.iterdir()) == ["project.yml"]


def test_exception_in_block_writes_nothing(tmp_path):
    path = tmp_path / "project.yml"
    path.write_text(RICH_PROJECT, encoding="utf-8")
    with pytest.raises(RuntimeError):
        with yamlio.edit_yaml(path) as doc:
            yamlio.set_value(doc, "x", 1)
            raise RuntimeError("stop")
    assert _read(path) == RICH_PROJECT


def test_concurrent_edits_do_not_lose_updates(tmp_path):
    path = tmp_path / "project.yml"
    path.write_text(RICH_PROJECT, encoding="utf-8")
    errors: list[BaseException] = []

    def add(i: int) -> None:
        try:
            with yamlio.edit_yaml(path) as doc:
                yamlio.set_value(yamlio.ensure_map(doc, "connections"), f"c{i}", {"type": "csv"})
        except BaseException as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=add, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    conns = yaml.safe_load(_read(path))["connections"]
    assert {f"c{i}" for i in range(12)} <= set(conns)
    _assert_comments_kept(RICH_PROJECT, _read(path))


def test_replace_changes_mtime_even_on_a_coarse_clock(tmp_path, monkeypatch):
    # NTFS and ext4 timestamps are fine-grained, so simulate a filesystem
    # whose clock gives the new file the old file's mtime: the bump must
    # still make the config cache (keyed on mtime) see a change.
    path = tmp_path / "project.yml"
    path.write_text("a: 1\n", encoding="utf-8")
    old = path.stat()
    real_replace = yamlio._replace

    def coarse_replace(src, dst):
        real_replace(src, dst)
        os.utime(dst, ns=(old.st_atime_ns, old.st_mtime_ns))

    monkeypatch.setattr(yamlio, "_replace", coarse_replace)
    with yamlio.edit_yaml(path) as doc:
        yamlio.set_value(doc, "a", 2)
    assert path.stat().st_mtime_ns != old.st_mtime_ns
    assert path.stat().st_mtime != old.st_mtime  # what _get_config_cached compares


def test_file_mode_is_kept(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX permission bits")
    path = tmp_path / "project.yml"
    path.write_text("a: 1\n", encoding="utf-8")
    os.chmod(path, 0o640)
    with yamlio.edit_yaml(path) as doc:
        yamlio.set_value(doc, "a", 2)
    assert (path.stat().st_mode & 0o777) == 0o640


# ---------------------------------------------------------------------------
# Comments attached to deleted keys
# ---------------------------------------------------------------------------

DELETE_SRC = """\
# header
first: 1   # first eol
# after first
connections:   # conn eol
  # before pg
  pg:
    type: postgres  # pg eol
  # after pg
  my:
    type: mysql
  # after my
# about lint
lint:
  dialect: duckdb
streams:
  only:
    x: 1
  # after only
# end of file
"""


@pytest.mark.parametrize("path_, gone", [
    (("connections", "pg"), ["# pg eol"]),
    (("connections", "my"), []),
    (("first",), ["# first eol"]),
    (("streams", "only"), []),
    (("lint", "dialect"), []),
])
def test_delete_keeps_unrelated_comments(tmp_path, path_, gone):
    path = tmp_path / "f.yml"
    path.write_text(DELETE_SRC, encoding="utf-8")
    with yamlio.edit_yaml(path) as doc:
        assert yamlio.delete_key(doc, *path_)
    text = _read(path)
    for line in DELETE_SRC.splitlines():
        if "#" in line:
            comment = line[line.index("#"):]
            if comment in gone:
                assert comment not in text
            else:
                assert comment in text, (comment, text)
    expected = yaml.safe_load(DELETE_SRC)
    node = expected
    for k in path_[:-1]:
        node = node[k]
    del node[path_[-1]]
    assert yaml.safe_load(text) == expected


def test_delete_missing_key_returns_false(tmp_path):
    path = tmp_path / "f.yml"
    path.write_text(DELETE_SRC, encoding="utf-8")
    with yamlio.edit_yaml(path) as doc:
        assert yamlio.delete_key(doc, "connections", "nope") is False
        assert yamlio.delete_key(doc, "nope", "x") is False
        assert yamlio.delete_key(doc, "first", "x") is False
    assert _read(path) == DELETE_SRC


# ---------------------------------------------------------------------------
# CLI startup stays light
# ---------------------------------------------------------------------------


def test_cli_import_does_not_load_ruamel():
    src = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        "import sys; sys.path.insert(0, %r); import havn.cli; "
        "print('ruamel' in ' '.join(sys.modules))" % src
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


# ---------------------------------------------------------------------------
# Emptying a nested mapping that has comments before its first key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("src", [
    "name: p\nconnections:\n  # the warehouse\n  pg:\n    type: postgres\n",
    "name: p\nconnections:   # eol on the key\n  # the warehouse\n  pg:\n    type: postgres\nlint: 1\n",
    "name: p\nconnections:\n  # the warehouse\n\n  # second paragraph\n  pg:\n    type: postgres  # pg eol\n  # after pg\n# top level\nlint: 1\n",
])
def test_removing_the_only_connector_keeps_the_file_valid(tmp_path, src):
    from havn.engine.connector import _remove_from_project_yml

    project = _project(tmp_path, src)
    assert _remove_from_project_yml(project, "pg") is True
    text = _read(project / "project.yml")
    expected = yaml.safe_load(src)
    expected["connections"] = {}
    assert yaml.safe_load(text) == expected
    assert "# the warehouse" in text
    if "second paragraph" in src:
        assert "# second paragraph" in text and "# after pg" in text and "# top level" in text


def test_database_endpoint_removing_the_only_database_key(tmp_path):
    import havn.server.app as server_app
    from havn.server.deps import _clear_config_cache, reset_shared_conn

    src = "name: p\ndatabase:\n  # resource limits\n  memory_limit: 4GB\nlint:\n  dialect: duckdb\n"
    project = _project(tmp_path, src)
    duckdb.connect(str(project / "warehouse.duckdb")).close()
    reset_shared_conn()
    _clear_config_cache()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = False
    try:
        resp = TestClient(server_app.app).put("/api/config/database", json={"memory_limit": ""})
        assert resp.status_code == 200
    finally:
        reset_shared_conn()
        _clear_config_cache()
    text = _read(project / "project.yml")
    assert yaml.safe_load(text) == {"name": "p", "database": {}, "lint": {"dialect": "duckdb"}}
    assert "# resource limits" in text


# ---------------------------------------------------------------------------
# Anchors, aliases and merge keys
# ---------------------------------------------------------------------------


def test_editing_an_anchored_mapping_leaves_its_alias_alone(tmp_path):
    path = tmp_path / "project.yml"
    path.write_text(
        "# shared defaults\nresources:\n  transform: &d\n    threads: 2\n    memory_gb: 1.0\n"
        "  ingest: *d   # same as transform\n",
        encoding="utf-8",
    )
    with yamlio.edit_yaml(path) as doc:
        yamlio.set_value(doc["resources"], "transform", {"threads": 4, "memory_gb": 1.0})
    data = yaml.safe_load(_read(path))
    # As with PyYAML + dump: the alias keeps the old data, the key gets the new.
    assert data["resources"] == {
        "transform": {"threads": 4, "memory_gb": 1.0},
        "ingest": {"threads": 2, "memory_gb": 1.0},
    }
    assert "# shared defaults" in _read(path)


def test_resetting_a_connection_built_on_a_merge_drops_merged_keys(tmp_path):
    from havn.engine.connector import _update_project_yml

    src = (
        "defaults: &def\n  type: postgres\n  port: 5432\n"
        "connections:\n  pg:  # built on defaults\n    <<: *def\n    host: h\n"
    )
    project = _project(tmp_path, src)
    _update_project_yml(project, "pg", {"type": "postgres", "host": "h2"}, "connector_pg.py", None)
    data = yaml.safe_load(_read(project / "project.yml"))
    assert data["connections"]["pg"] == {"type": "postgres", "host": "h2"}
    assert data["defaults"] == {"type": "postgres", "port": 5432}


def test_save_job_over_a_file_with_a_top_level_merge(tmp_path):
    from havn.engine.orchestration import save_job

    (tmp_path / "orchestration").mkdir()
    (tmp_path / "orchestration" / "merged.yml").write_text(
        "# base settings\n_base: &b\n  retry: 1\n  resolve: upstream\n<<: *b\nname: merged\n"
        "targets:\n  - silver.orders\n",
        encoding="utf-8",
    )
    path = save_job(tmp_path, {"name": "merged", "targets": ["silver.orders"], "resolve": "upstream"})
    assert yaml.safe_load(_read(path)) == {
        "name": "merged", "targets": ["silver.orders"], "resolve": "upstream",
        "target": "silver.orders", "cron": "", "tags": [],
    }


def test_deleting_a_key_from_a_mapping_with_a_merge(tmp_path):
    path = tmp_path / "f.yml"
    path.write_text("base: &b\n  a: 1\n  b: 2\nm:\n  <<: *b\n  c: 3\n", encoding="utf-8")
    with yamlio.edit_yaml(path) as doc:
        assert yamlio.delete_key(doc, "m", "a")  # supplied by the merge
    assert yaml.safe_load(_read(path)) == {"base": {"a": 1, "b": 2}, "m": {"b": 2, "c": 3}}


# ---------------------------------------------------------------------------
# Files the helper cannot load faithfully
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("src", ["v: -012\nkeep: 1\n", "%YAML 1.2\n---\nv: 1:30\n"])
def test_awkward_numbers_never_raise_a_bare_error(tmp_path, src):
    path = tmp_path / "f.yml"
    path.write_text(src, encoding="utf-8")
    try:
        with yamlio.edit_yaml(path) as doc:
            yamlio.set_value(doc, "added", "x")
    except yamlio.YamlLoadError as e:
        assert "f.yml" in str(e)
        assert _read(path) == src
    else:
        data = yaml.safe_load(_read(path))
        expected = yaml.safe_load(src)
        expected["added"] = "x"
        assert data == expected


def test_resources_allocation_skips_an_unloadable_file(tmp_path):
    import havn.server.app as server_app
    from havn.server.deps import reset_shared_conn

    src = "%YAML 1.2\n---\nname: p\nv: 1:30\n"
    project = _project(tmp_path, src)
    duckdb.connect(str(project / "warehouse.duckdb")).close()
    reset_shared_conn()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = False
    try:
        resp = TestClient(server_app.app).put("/api/resources/allocation", json={
            "category": "query", "memory_gb": 2.0, "threads": 2, "max_concurrent": 3,
        })
    finally:
        reset_shared_conn()
    assert resp.status_code == 200
    assert _read(project / "project.yml") == src


# ---------------------------------------------------------------------------
# An empty root keeps its comments
# ---------------------------------------------------------------------------


def test_unchanged_empty_root_with_comments_is_not_rewritten(tmp_path):
    path = tmp_path / "f.yml"
    path.write_text("# c\n{}\n", encoding="utf-8")
    with yamlio.edit_yaml(path):
        pass
    assert _read(path) == "# c\n{}\n"


def test_deleting_the_last_root_key_keeps_comments(tmp_path):
    path = tmp_path / "f.yml"
    path.write_text("# c\na: 1\n# tail\n", encoding="utf-8")
    with yamlio.edit_yaml(path) as doc:
        yamlio.delete_key(doc, "a")
    text = _read(path)
    assert "# c" in text and "# tail" in text
    assert (yaml.safe_load(text) or {}) == {}


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def test_symlinked_project_yml_stays_a_link(tmp_path):
    real = tmp_path / "shared" / "project.yml"
    real.parent.mkdir()
    real.write_text("# shared config\nname: p\n", encoding="utf-8")
    link = tmp_path / "proj" / "project.yml"
    link.parent.mkdir()
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available here")
    with yamlio.edit_yaml(link) as doc:
        yamlio.set_value(doc, "description", "x")
    assert link.is_symlink()
    assert yaml.safe_load(_read(real)) == {"name": "p", "description": "x"}
    assert "# shared config" in _read(real)
    assert not list(link.parent.glob(".*.tmp")) and not list(real.parent.glob(".*.tmp"))


# ---------------------------------------------------------------------------
# Old call-site behaviour: empty non-mappings, unloadable files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("empty", ["[]", '""', "false", "0"])
def test_resources_empty_non_mapping_is_replaced(client, empty):
    c, project = client
    (project / "project.yml").write_text(RICH_PROJECT + f"resources: {empty}\n", encoding="utf-8")
    resp = c.put("/api/resources/allocation", json={
        "category": "query", "memory_gb": 2.0, "threads": 2, "max_concurrent": 3,
    })
    assert resp.status_code == 200
    data = yaml.safe_load(_read(project / "project.yml"))
    assert data["resources"] == {"query": {"memory_gb": 2.0, "threads": 2, "max_concurrent": 3}}


DUPLICATE = RICH_PROJECT + "lint:\n  dialect: postgres\n"


def test_routes_answer_400_for_an_unloadable_project_yml(client):
    c, project = client
    path = project / "project.yml"
    path.write_text(DUPLICATE, encoding="utf-8")
    for method, url, body in [
        ("put", "/api/anomalies/config", {"lookback": 10}),
        ("put", "/api/config/database", {"threads": 2}),
        ("post", "/api/connectors/setup", {
            "connector_type": "webhook", "connection_name": "hook", "config": {"table_name": "events"},
        }),
    ]:
        resp = getattr(c, method)(url, json=body)
        assert resp.status_code == 400, (url, resp.text)
        assert "project.yml" in resp.json()["detail"], url
    assert _read(path) == DUPLICATE

    (project / "ingest" / "connector_prod_pg.py").write_text("# script\n", encoding="utf-8")
    resp = c.delete("/api/connectors/prod_pg")
    assert resp.status_code == 400
    assert "project.yml" in resp.json()["detail"]
    assert (project / "ingest" / "connector_prod_pg.py").exists()  # nothing removed
    assert _read(path) == DUPLICATE


def test_job_routes_answer_400_for_an_unloadable_job_file(job_project):
    import havn.server.app as server_app
    from havn.server.deps import reset_shared_conn

    path = job_project / "orchestration" / "test-job.yml"
    path.write_text(JOB + "retry: 2\n", encoding="utf-8")
    reset_shared_conn()
    server_app.PROJECT_DIR = job_project
    server_app.AUTH_ENABLED = False
    try:
        c = TestClient(server_app.app)
        resp = c.patch("/api/jobs/test-job", json={"enabled": False})
        assert resp.status_code == 400, resp.text
        assert "test-job.yml" in resp.json()["detail"]
        resp = c.post("/api/jobs", json={"name": "test-job", "targets": ["silver.orders"]})
        assert resp.status_code == 400, resp.text
    finally:
        reset_shared_conn()
    assert _read(path) == JOB + "retry: 2\n"


def test_cli_paths_exit_1_for_an_unloadable_file(job_project):
    from typer.testing import CliRunner

    from havn.cli import app

    job = job_project / "orchestration" / "test-job.yml"
    job.write_text(JOB + "retry: 2\n", encoding="utf-8")
    r = CliRunner().invoke(app, ["jobs", "disable", "test-job", "--project", str(job_project)])
    assert r.exit_code == 1, r.output
    assert "test-job.yml" in r.output

    (job_project / "project.yml").write_text(
        "name: jobs\ndatabase:\n  path: warehouse.duckdb\nname: jobs\n", encoding="utf-8")
    r = CliRunner().invoke(app, ["connect", "webhook", "--name", "hook", "--set", "table_name=events",
                                 "--project", str(job_project)])
    assert r.exit_code == 1, r.output
    assert "project.yml" in r.output

    r = CliRunner().invoke(app, ["migrate", "--to", "ducklake", "--project", str(job_project)])
    assert r.exit_code == 1, r.output
    assert "project.yml" in r.output
    assert not (job_project / ".havn" / "catalog.ducklake").exists()


# ---------------------------------------------------------------------------
# Safety net: a bad round trip falls back to a plain, correct dump
# ---------------------------------------------------------------------------


def test_bad_round_trip_text_falls_back_to_a_plain_dump(tmp_path, monkeypatch, caplog):
    path = tmp_path / "project.yml"
    path.write_text(RICH_PROJECT, encoding="utf-8")
    monkeypatch.setattr(yamlio, "_render", lambda doc, text, header: "connections:\n  # x\n{}\n")
    with caplog.at_level("WARNING", logger="havn.yamlio"):
        with yamlio.edit_yaml(path) as doc:
            yamlio.set_value(yamlio.ensure_map(doc, "connections"), "c1", {"type": "csv", "flag": "on"})
    expected = yaml.safe_load(RICH_PROJECT)
    expected["connections"]["c1"] = {"type": "csv", "flag": "on"}
    assert yaml.safe_load(_read(path)) == expected
    assert "comments could not be preserved" in caplog.text and "project.yml" in caplog.text


def test_wrong_data_from_round_trip_falls_back(tmp_path, monkeypatch):
    path = tmp_path / "project.yml"
    path.write_text("a: 1\nflag: 'on'\n", encoding="utf-8")
    monkeypatch.setattr(yamlio, "_render", lambda doc, text, header: "a: 1\nflag: on\nb: 2\n")
    with yamlio.edit_yaml(path) as doc:
        yamlio.set_value(doc, "b", 2)
    data = yaml.safe_load(_read(path))
    assert data == {"a": 1, "flag": "on", "b": 2} and type(data["flag"]) is str


def test_round_trip_exception_falls_back(tmp_path, monkeypatch, caplog):
    path = tmp_path / "project.yml"
    path.write_text(RICH_PROJECT, encoding="utf-8")

    def boom(doc, text, header):
        raise RuntimeError("emitter broke")

    monkeypatch.setattr(yamlio, "_render", boom)
    with caplog.at_level("WARNING", logger="havn.yamlio"):
        with yamlio.edit_yaml(path) as doc:
            yamlio.delete_key(doc, "connections", "prod_pg")
    expected = yaml.safe_load(RICH_PROJECT)
    expected["connections"] = {}
    assert yaml.safe_load(_read(path)) == expected
    assert "emitter broke" in caplog.text


def test_comment_step_failure_does_not_abort_the_edit(tmp_path, monkeypatch):
    path = tmp_path / "project.yml"
    path.write_text(RICH_PROJECT, encoding="utf-8")

    def broken(*a, **k):
        raise AttributeError("ruamel internals moved")

    monkeypatch.setattr(yamlio, "_take_trailing", broken)
    monkeypatch.setattr(yamlio, "_move_comments_after_delete", broken)
    with yamlio.edit_yaml(path) as doc:
        yamlio.set_value(yamlio.ensure_map(doc, "connections"), "c1", {"type": "csv"})
        yamlio.delete_key(doc, "connections", "prod_pg")
    data = yaml.safe_load(_read(path))
    assert data["connections"] == {"c1": {"type": "csv"}}


# ---------------------------------------------------------------------------
# Fuzz: random edits on commented documents always load and match a model
# ---------------------------------------------------------------------------

FUZZ_DOCS = [
    RICH_PROJECT,
    JOB,
    DELETE_SRC,
    "# anchors\ndefaults: &def\n  type: postgres\n  port: 5432\nconnections:\n  a: *def\n"
    "  b:\n    <<: *def\n    host: h   # merged\n  # after b\nresources: {query: {threads: 2}}\n# end\n",
    "x: {}\n  # comment after flow\ny:\n  # before first\n  z: 1\n  w: [1, 2]\n# trailing\n",
    "# header only\n",
]

FUZZ_VALUES = [
    "on", "yes", "0 6 * * *", "${VAR}", "plain", "", "012", "1:30", True, False, None, 0, 7, 2.5,
    {"k": "v", "n": 1}, {"nested": {"deep": "on"}}, [], ["a", "b"], [{"ingest": ["all"]}], {},
]


def _model_maps(node, path=()):
    """Every mapping in the model, with its key path."""
    out = [path]
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, dict) and isinstance(k, str):
                out.extend(_model_maps(v, (*path, k)))
    return out


def _at(node, path):
    for k in path:
        node = node[k]
    return node


def test_fuzz_random_edits_keep_files_valid(tmp_path):
    import random

    rng = random.Random(20261127)
    path = tmp_path / "f.yml"
    steps = 0
    for doc_i, src in enumerate(FUZZ_DOCS):
        path.write_text(src, encoding="utf-8")
        model = yaml.safe_load(src) or {}
        for _ in range(25):
            maps = [p for p in _model_maps(model) if len(p) < 4]
            target = rng.choice(maps)
            node = _at(model, target)
            op = rng.choice(["set", "set", "delete", "ensure"])
            keys = [k for k in node if isinstance(k, str)]
            if op == "delete" and not keys:
                op = "set"
            if op == "set":
                key = rng.choice(keys + ["new_a", "new_b", "c1"]) if keys else rng.choice(["new_a", "c1"])
                value = copy.deepcopy(rng.choice(FUZZ_VALUES))
                node[key] = copy.deepcopy(value)
                with yamlio.edit_yaml(path) as doc:
                    yamlio.set_value(_at(doc, target), key, value)
            elif op == "delete":
                key = rng.choice(keys)
                del node[key]
                with yamlio.edit_yaml(path) as doc:
                    assert yamlio.delete_key(doc, *target, key)
            else:
                key = rng.choice(keys + ["fresh"])
                current = node.get(key)
                if key not in node or current is None or (not isinstance(current, dict) and not current):
                    node[key] = {}
                elif not isinstance(current, dict):
                    continue  # ensure_map refuses a non-empty scalar; the model agrees
                with yamlio.edit_yaml(path) as doc:
                    yamlio.ensure_map(doc, *target, key)
            steps += 1
            text = _read(path)
            loaded = yaml.safe_load(text) or {}
            assert yamlio._equal(loaded, model), (doc_i, steps, op, target, text)
    assert steps > 100
