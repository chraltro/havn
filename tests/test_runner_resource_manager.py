"""Tests that ingest/export Python scripts and notebooks are wrapped by the ResourceManager."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from havn.engine.resource_manager import get_resource_manager, reset_resource_manager


@pytest.fixture(autouse=True)
def _fresh_manager():
    reset_resource_manager()
    yield
    reset_resource_manager()


def _fresh_conn() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    # Minimal _havn schema so log_run works.
    from havn.engine.database import ensure_meta_table
    ensure_meta_table(c)
    return c


def _write_ingest(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "ingest_demo.py"
    p.write_text(body)
    return p


def test_ingest_script_acquires_streaming_category(tmp_path):
    conn = _fresh_conn()
    script = _write_ingest(
        tmp_path,
        "db.execute('CREATE SCHEMA IF NOT EXISTS landing')\n"
        "db.execute('CREATE TABLE landing.demo(x INT)')\n",
    )
    from havn.engine.runner import run_script

    before = get_resource_manager().snapshot()
    assert before["total_active"] == 0

    result = run_script(conn, script, script_type="ingest", use_circuit_breaker=False)
    assert result["status"] == "success"

    after = get_resource_manager().snapshot()
    # Task has been moved to recent.
    latest = after["recent"][0]
    assert latest["category"] == "streaming"
    assert latest["label"].startswith("ingest:")
    assert latest["status"] == "completed"


def test_export_script_acquires_system_category(tmp_path):
    conn = _fresh_conn()
    conn.execute("CREATE TABLE t(x INT)")
    script = tmp_path / "export_demo.py"
    script.write_text("print(db.execute('SELECT count(*) FROM t').fetchone()[0])\n")
    from havn.engine.runner import run_script

    result = run_script(conn, script, script_type="export", use_circuit_breaker=False)
    assert result["status"] == "success"

    recent = get_resource_manager().snapshot()["recent"][0]
    assert recent["category"] == "system"
    assert recent["label"].startswith("export:")


def test_script_failure_is_recorded_in_recent(tmp_path):
    conn = _fresh_conn()
    script = tmp_path / "bad.py"
    script.write_text("raise ValueError('boom')\n")
    from havn.engine.runner import run_script

    result = run_script(conn, script, script_type="ingest", use_circuit_breaker=False)
    assert result["status"] == "error"

    # The script error is captured inside run_script (not bubbled). The
    # resource-manager task still records success of the acquire, but the
    # runner records the error in the returned dict. Either way the task
    # exits cleanly.
    snap = get_resource_manager().snapshot()
    assert snap["total_active"] == 0
    # The recent entry exists regardless.
    assert len(snap["recent"]) == 1


def test_notebook_run_acquires_query_category():
    conn = _fresh_conn()
    from havn.engine.notebook.runner import run_notebook

    nb = {
        "name": "demo",
        "cells": [
            {"type": "sql", "source": "SELECT 1 AS n", "id": "c1"},
            {"type": "code", "source": "x = 1 + 1", "id": "c2"},
        ],
    }
    run_notebook(conn, nb)

    recent = get_resource_manager().snapshot()["recent"][0]
    assert recent["category"] == "query"
    assert recent["label"].startswith("notebook:")
    assert recent["status"] == "completed"
