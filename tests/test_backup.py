"""Tests for the backup/restore engine."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest


@pytest.fixture
def project(tmp_path):
    """Create a minimal project with a warehouse database."""
    db_path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE SCHEMA IF NOT EXISTS gold")
    conn.execute("CREATE TABLE gold.customers (id INTEGER, name VARCHAR)")
    conn.execute("INSERT INTO gold.customers VALUES (1, 'Alice'), (2, 'Bob')")
    conn.execute("CHECKPOINT")
    conn.close()
    return tmp_path, db_path


# ---------------------------------------------------------------------------
# create_backup
# ---------------------------------------------------------------------------


class TestCreateBackup:
    def test_basic_backup(self, project):
        from havn.engine.backup import create_backup

        project_dir, db_path = project
        entry = create_backup(project_dir, db_path)

        assert entry["verified"] is True
        assert entry["sha256"]
        assert entry["size_bytes"] > 0
        assert Path(entry["path"]).exists()
        assert "timestamp" in entry

    def test_backup_to_default_directory(self, project):
        from havn.engine.backup import BACKUPS_DIR, create_backup

        project_dir, db_path = project
        entry = create_backup(project_dir, db_path)

        backup_path = Path(entry["path"])
        assert BACKUPS_DIR in str(backup_path)
        assert backup_path.suffix == ".duckdb"

    def test_backup_to_custom_path(self, project):
        from havn.engine.backup import create_backup

        project_dir, db_path = project
        custom = project_dir / "my_backup.duckdb"
        entry = create_backup(project_dir, db_path, output=custom)

        assert Path(entry["path"]) == custom
        assert custom.exists()

    def test_backup_without_verification(self, project):
        from havn.engine.backup import create_backup

        project_dir, db_path = project
        entry = create_backup(project_dir, db_path, verify=False)

        assert entry["verified"] is False
        assert Path(entry["path"]).exists()

    def test_backup_with_note(self, project):
        from havn.engine.backup import create_backup

        project_dir, db_path = project
        entry = create_backup(project_dir, db_path, note="pre-migration")

        assert entry["note"] == "pre-migration"

    def test_backup_nonexistent_db(self, tmp_path):
        from havn.engine.backup import BackupError, create_backup

        with pytest.raises(BackupError, match="not found"):
            create_backup(tmp_path, tmp_path / "nope.duckdb")

    def test_backup_data_intact(self, project):
        """Backup should contain the same data as the original."""
        from havn.engine.backup import create_backup

        project_dir, db_path = project
        entry = create_backup(project_dir, db_path)

        backup_conn = duckdb.connect(entry["path"], read_only=True)
        rows = backup_conn.execute("SELECT COUNT(*) FROM gold.customers").fetchone()[0]
        backup_conn.close()
        assert rows == 2


# ---------------------------------------------------------------------------
# restore_backup
# ---------------------------------------------------------------------------


class TestRestoreBackup:
    def test_basic_restore(self, project):
        from havn.engine.backup import create_backup, restore_backup

        project_dir, db_path = project
        entry = create_backup(project_dir, db_path)

        # Destroy the original
        db_path.unlink()
        assert not db_path.exists()

        result = restore_backup(project_dir, db_path, Path(entry["path"]))

        assert db_path.exists()
        assert result["size_bytes"] > 0

        # Verify data
        conn = duckdb.connect(str(db_path), read_only=True)
        rows = conn.execute("SELECT COUNT(*) FROM gold.customers").fetchone()[0]
        conn.close()
        assert rows == 2

    def test_restore_removes_wal(self, project):
        from havn.engine.backup import create_backup, restore_backup

        project_dir, db_path = project
        entry = create_backup(project_dir, db_path)

        # Create a fake WAL file
        wal_path = Path(str(db_path) + ".wal")
        wal_path.write_text("fake wal")

        restore_backup(project_dir, db_path, Path(entry["path"]))
        assert not wal_path.exists()

    def test_restore_nonexistent_backup(self, project):
        from havn.engine.backup import BackupError, restore_backup

        project_dir, db_path = project
        with pytest.raises(BackupError, match="not found"):
            restore_backup(project_dir, db_path, project_dir / "nope.duckdb")

    def test_restore_corrupted_backup(self, project):
        from havn.engine.backup import BackupError, restore_backup

        project_dir, db_path = project
        bad_backup = project_dir / "bad.duckdb"
        bad_backup.write_text("this is not a duckdb file")

        with pytest.raises(BackupError, match="Cannot open"):
            restore_backup(project_dir, db_path, bad_backup)


# ---------------------------------------------------------------------------
# verify_backup
# ---------------------------------------------------------------------------


class TestVerifyBackup:
    def test_verify_valid_backup(self, project):
        from havn.engine.backup import create_backup, verify_backup

        project_dir, db_path = project
        entry = create_backup(project_dir, db_path)

        result = verify_backup(Path(entry["path"]))

        assert result["valid"] is True
        assert result["sha256"]
        assert result["table_count"] >= 1
        assert "gold" in result["schemas"]

    def test_verify_nonexistent(self, tmp_path):
        from havn.engine.backup import BackupError, verify_backup

        with pytest.raises(BackupError, match="not found"):
            verify_backup(tmp_path / "nope.duckdb")

    def test_verify_corrupted(self, tmp_path):
        from havn.engine.backup import verify_backup

        bad = tmp_path / "bad.duckdb"
        bad.write_text("not a database")
        result = verify_backup(bad)
        assert result["valid"] is False


# ---------------------------------------------------------------------------
# list_backups / manifest
# ---------------------------------------------------------------------------


class TestListBackups:
    def test_empty_list(self, tmp_path):
        from havn.engine.backup import list_backups

        assert list_backups(tmp_path) == []

    def test_list_after_backup(self, project):
        from havn.engine.backup import create_backup, list_backups

        project_dir, db_path = project
        create_backup(project_dir, db_path, note="first")
        create_backup(project_dir, db_path, note="second")

        entries = list_backups(project_dir)
        assert len(entries) == 2
        assert entries[0]["note"] == "first"
        assert entries[1]["note"] == "second"
        assert all(e["exists"] for e in entries)

    def test_list_detects_missing_files(self, project):
        from havn.engine.backup import create_backup, list_backups

        project_dir, db_path = project
        entry = create_backup(project_dir, db_path)

        Path(entry["path"]).unlink()

        entries = list_backups(project_dir)
        assert len(entries) == 1
        assert entries[0]["exists"] is False


# ---------------------------------------------------------------------------
# cleanup_backups
# ---------------------------------------------------------------------------


class TestCleanupBackups:
    def test_cleanup_keeps_n(self, project):
        from havn.engine.backup import cleanup_backups, create_backup, list_backups

        project_dir, db_path = project
        for i in range(5):
            create_backup(project_dir, db_path, note=f"backup-{i}")

        removed = cleanup_backups(project_dir, keep=2)
        assert len(removed) == 3

        remaining = list_backups(project_dir)
        assert len(remaining) == 2
        assert remaining[0]["note"] == "backup-3"
        assert remaining[1]["note"] == "backup-4"

    def test_cleanup_noop_when_under_limit(self, project):
        from havn.engine.backup import cleanup_backups, create_backup

        project_dir, db_path = project
        create_backup(project_dir, db_path)

        removed = cleanup_backups(project_dir, keep=10)
        assert len(removed) == 0

    def test_cleanup_deletes_files(self, project):
        from havn.engine.backup import cleanup_backups, create_backup, list_backups

        project_dir, db_path = project
        for _ in range(3):
            create_backup(project_dir, db_path)

        cleanup_backups(project_dir, keep=1)

        remaining = list_backups(project_dir)
        assert len(remaining) == 1
        # The one remaining should still exist on disk
        assert Path(remaining[0]["path"]).exists()
