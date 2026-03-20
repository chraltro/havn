"""Tests for file edit conflict detection (ETag/hash-based)."""

from __future__ import annotations

import duckdb
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def project(tmp_path):
    """Create a minimal test project."""
    (tmp_path / "project.yml").write_text("""
name: test
database:
  path: warehouse.duckdb
streams:
  test-stream:
    description: "Test"
    steps:
      - transform: [all]
""")
    (tmp_path / "transform" / "bronze").mkdir(parents=True)
    (tmp_path / "ingest").mkdir()
    (tmp_path / "export").mkdir()

    (tmp_path / "transform" / "bronze" / "test.sql").write_text(
        "-- config: materialized=view, schema=bronze\n\nSELECT 1 AS id\n"
    )

    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
    conn.close()

    return tmp_path


@pytest.fixture
def client(project):
    import havn.server.app as server_app

    server_app.PROJECT_DIR = project
    return TestClient(server_app.app)


def test_read_file_returns_hash(client):
    """Reading a file should return a file_hash field."""
    resp = client.get("/api/files/transform/bronze/test.sql")
    assert resp.status_code == 200
    data = resp.json()
    assert "file_hash" in data
    assert isinstance(data["file_hash"], str)
    assert len(data["file_hash"]) == 16


def test_read_file_returns_etag_header(client):
    """Reading a file should include an ETag response header."""
    resp = client.get("/api/files/transform/bronze/test.sql")
    assert resp.status_code == 200
    etag = resp.headers.get("etag")
    assert etag is not None
    # ETag should contain the hash
    data = resp.json()
    assert data["file_hash"] in etag


def test_save_with_correct_hash_succeeds(client):
    """Saving with the correct expected_hash should succeed."""
    # Read the file to get its hash
    resp = client.get("/api/files/transform/bronze/test.sql")
    assert resp.status_code == 200
    file_hash = resp.json()["file_hash"]

    # Save with correct hash
    new_content = "-- config: materialized=view, schema=bronze\n\nSELECT 2 AS id\n"
    resp = client.put(
        "/api/files/transform/bronze/test.sql",
        json={"content": new_content, "expected_hash": file_hash},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "saved"
    assert "file_hash" in data


def test_save_with_wrong_hash_returns_409(client, project):
    """Saving with a stale expected_hash should return 409 Conflict."""
    # Read the file to get its hash
    resp = client.get("/api/files/transform/bronze/test.sql")
    assert resp.status_code == 200
    old_hash = resp.json()["file_hash"]

    # Modify the file behind the scenes (simulate agent edit)
    (project / "transform" / "bronze" / "test.sql").write_text(
        "-- config: materialized=view, schema=bronze\n\nSELECT 999 AS id\n"
    )

    # Try to save with the old hash
    resp = client.put(
        "/api/files/transform/bronze/test.sql",
        json={"content": "SELECT 42 AS id\n", "expected_hash": old_hash},
    )
    assert resp.status_code == 409
    data = resp.json()
    assert data["conflict"] is True
    assert "current_hash" in data
    assert data["current_hash"] != old_hash
    assert "message" in data


def test_save_without_hash_succeeds_backward_compat(client):
    """Saving without expected_hash should always succeed (backward compatible)."""
    new_content = "-- config: materialized=view, schema=bronze\n\nSELECT 3 AS id\n"
    resp = client.put(
        "/api/files/transform/bronze/test.sql",
        json={"content": new_content},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "saved"
    assert "file_hash" in data


def test_hash_changes_when_content_changes(client):
    """Hash should change when file content is different."""
    resp1 = client.get("/api/files/transform/bronze/test.sql")
    hash1 = resp1.json()["file_hash"]

    # Save new content
    client.put(
        "/api/files/transform/bronze/test.sql",
        json={"content": "-- config: materialized=view\n\nSELECT 100 AS x\n"},
    )

    resp2 = client.get("/api/files/transform/bronze/test.sql")
    hash2 = resp2.json()["file_hash"]

    assert hash1 != hash2


def test_save_returns_new_hash(client):
    """Save response should include the hash of the newly saved content."""
    content = "-- config: materialized=view, schema=bronze\n\nSELECT 7 AS id\n"
    resp = client.put(
        "/api/files/transform/bronze/test.sql",
        json={"content": content},
    )
    assert resp.status_code == 200
    save_hash = resp.json()["file_hash"]

    # Read the file back and verify hash matches
    resp2 = client.get("/api/files/transform/bronze/test.sql")
    assert resp2.json()["file_hash"] == save_hash


def test_save_returns_etag_header(client):
    """Save response should include an ETag header."""
    content = "-- config: materialized=view, schema=bronze\n\nSELECT 8 AS id\n"
    resp = client.put(
        "/api/files/transform/bronze/test.sql",
        json={"content": content},
    )
    assert resp.status_code == 200
    etag = resp.headers.get("etag")
    assert etag is not None
    assert resp.json()["file_hash"] in etag


def test_save_new_file_with_expected_hash_ignored(client):
    """Saving a new file with expected_hash should succeed (file doesn't exist yet)."""
    content = "# new script\ndb.execute('SELECT 1')\n"
    resp = client.put(
        "/api/files/ingest/new_script.py",
        json={"content": content, "expected_hash": "nonexistent12345"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "saved"
