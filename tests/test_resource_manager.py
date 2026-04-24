"""Tests for the ResourceManager and its HTTP surface."""

from __future__ import annotations

import duckdb
import pytest
from fastapi.testclient import TestClient

from havn.engine.resource_manager import (
    CATEGORIES,
    CategoryBudget,
    ResourceManager,
    get_resource_manager,
    reset_resource_manager,
)


def test_defaults_have_all_four_categories():
    m = ResourceManager()
    snap = m.snapshot()
    names = [c["name"] for c in snap["categories"]]
    assert names == list(CATEGORIES)


def test_acquire_sync_records_task_and_releases():
    m = ResourceManager()
    with m.acquire_sync("query", "select one"):
        assert m.snapshot()["total_active"] == 1
    assert m.snapshot()["total_active"] == 0
    assert m.snapshot()["recent"][0]["status"] == "completed"


def test_acquire_sync_failing_task_marks_failed():
    m = ResourceManager()
    with pytest.raises(RuntimeError):
        with m.acquire_sync("query", "bad"):
            raise RuntimeError("boom")
    snap = m.snapshot()
    assert snap["recent"][0]["status"] == "failed"
    assert "boom" in (snap["recent"][0]["error"] or "")


def test_update_budget_replaces_semaphore_max_concurrent():
    m = ResourceManager()
    m.update_budget("query", CategoryBudget(memory_gb=1, threads=2, max_concurrent=1))
    snap = m.snapshot()
    q = next(c for c in snap["categories"] if c["name"] == "query")
    assert q["max_concurrent"] == 1


def test_cancel_nonexistent_task_returns_false():
    m = ResourceManager()
    assert m.cancel("not-a-real-id") is False


def test_applies_duckdb_limits_to_connection():
    m = ResourceManager()
    m.update_budget("query", CategoryBudget(memory_gb=0.5, threads=1, max_concurrent=1))
    conn = duckdb.connect(":memory:")
    try:
        with m.acquire_sync("query", "limits-check", conn=conn):
            [(mem_limit,)] = conn.execute("SELECT current_setting('memory_limit')").fetchall()
            [(threads,)] = conn.execute("SELECT current_setting('threads')").fetchall()
        assert mem_limit.upper().endswith("IB") or mem_limit.upper().endswith("B")
        assert str(threads) == "1"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import havn.server.app as server_app

    monkeypatch.setattr(server_app, "PROJECT_DIR", tmp_path)
    (tmp_path / "project.yml").write_text(
        "name: test\ndatabase:\n  backend: duckdb\n  path: warehouse.duckdb\n"
    )
    reset_resource_manager()
    return TestClient(server_app.app)


def test_resources_endpoint_returns_snapshot(client):
    r = client.get("/api/resources")
    assert r.status_code == 200
    body = r.json()
    assert len(body["categories"]) == 4
    assert body["total_active"] == 0


def test_update_allocation_persists_to_project_yml(client, tmp_path):
    body = {"category": "query", "memory_gb": 3.5, "threads": 4, "max_concurrent": 6}
    r = client.put("/api/resources/allocation", json=body)
    assert r.status_code == 200
    yml = (tmp_path / "project.yml").read_text()
    assert "resources" in yml
    assert "memory_gb: 3.5" in yml


def test_update_allocation_validates_bounds(client):
    bad = {"category": "query", "memory_gb": -1, "threads": 4, "max_concurrent": 6}
    assert client.put("/api/resources/allocation", json=bad).status_code == 422
