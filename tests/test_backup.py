"""Tests for the backup/restore engine."""

from __future__ import annotations

import sys
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


# ---------------------------------------------------------------------------
# Audit regressions: checksum, atomic restore, locked warehouse, --keep
# ---------------------------------------------------------------------------


def _tamper(path: Path) -> None:
    """Flip a byte near the end: DuckDB may still open it, the SHA changes."""
    data = bytearray(path.read_bytes())
    data[-1] ^= 0xFF
    path.write_bytes(bytes(data))


class TestChecksumAgainstManifest:
    def test_verify_reports_sha_mismatch(self, project):
        from havn.engine.backup import create_backup, verify_backup

        project_dir, db_path = project
        entry = create_backup(project_dir, db_path)
        backup = Path(entry["path"])
        _tamper(backup)

        # Located through the manifest beside the backup, and via project_dir.
        for result in (verify_backup(backup), verify_backup(backup, project_dir=project_dir)):
            assert result["valid"] is False
            assert result["checksum_match"] is False
            assert "SHA-256 mismatch" in result["error"]

    def test_verify_untouched_backup_matches(self, project):
        from havn.engine.backup import create_backup, verify_backup

        project_dir, db_path = project
        entry = create_backup(project_dir, db_path)
        result = verify_backup(Path(entry["path"]))
        assert result["valid"] is True
        assert result["checksum_match"] is True

    def test_list_does_not_call_tampered_backup_verified(self, project):
        from havn.engine.backup import create_backup, list_backups

        project_dir, db_path = project
        entry = create_backup(project_dir, db_path)
        _tamper(Path(entry["path"]))

        [listed] = list_backups(project_dir)
        assert listed["checksum_match"] is False
        assert listed["verified"] is False

    def test_restore_refuses_tampered_backup(self, project):
        from havn.engine.backup import BackupError, create_backup, restore_backup

        project_dir, db_path = project
        entry = create_backup(project_dir, db_path)
        _tamper(Path(entry["path"]))
        before = db_path.read_bytes()

        with pytest.raises(BackupError, match="SHA-256 mismatch"):
            restore_backup(project_dir, db_path, Path(entry["path"]))
        assert db_path.read_bytes() == before

    def test_cli_backup_verify_and_list(self, project):
        from typer.testing import CliRunner

        from havn.cli import app
        from havn.engine.backup import create_backup

        project_dir, db_path = project
        (project_dir / "project.yml").write_text("name: t\n", encoding="utf-8")
        entry = create_backup(project_dir, db_path)
        _tamper(Path(entry["path"]))

        runner = CliRunner()
        result = runner.invoke(app, ["backup-verify", entry["path"], "-p", str(project_dir)])
        assert result.exit_code == 1
        assert "INVALID" in result.output
        result = runner.invoke(app, ["backup-list", "-p", str(project_dir)])
        assert result.exit_code == 0
        assert "mismatch" in result.output


class TestAtomicRestore:
    def test_failed_copy_leaves_warehouse_and_wal_untouched(self, project, monkeypatch):
        import shutil

        from havn.engine.backup import BackupError, create_backup, restore_backup

        project_dir, db_path = project
        entry = create_backup(project_dir, db_path)
        wal = Path(str(db_path) + ".wal")
        wal.write_bytes(b"pending")
        before = db_path.read_bytes()

        def half_copy(src, dst, *a, **k):
            Path(dst).write_bytes(Path(src).read_bytes()[:100])
            raise OSError("disk full")

        monkeypatch.setattr(shutil, "copyfile", half_copy)
        with pytest.raises(BackupError, match="disk full"):
            restore_backup(project_dir, db_path, Path(entry["path"]))

        assert db_path.read_bytes() == before
        assert wal.read_bytes() == b"pending"
        leftovers = [p.name for p in project_dir.iterdir() if ".restore-" in p.name]
        assert leftovers == []

    def test_restore_replaces_contents(self, project):
        from havn.engine.backup import create_backup, restore_backup

        project_dir, db_path = project
        entry = create_backup(project_dir, db_path)
        conn = duckdb.connect(str(db_path))
        conn.execute("DROP TABLE gold.customers")
        conn.close()

        restore_backup(project_dir, db_path, Path(entry["path"]))
        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            assert conn.execute("SELECT COUNT(*) FROM gold.customers").fetchone()[0] == 2
        finally:
            conn.close()

    @pytest.mark.skipif(sys.platform != "win32", reason="open files are only unreplaceable on Windows")
    def test_locked_warehouse_gives_clear_error(self, project):
        from havn.engine.backup import BackupError, create_backup, restore_backup

        project_dir, db_path = project
        entry = create_backup(project_dir, db_path)
        holder = duckdb.connect(str(db_path))
        try:
            with pytest.raises(BackupError, match="in use by another process"):
                restore_backup(project_dir, db_path, Path(entry["path"]))
        finally:
            holder.close()
        assert [p for p in project_dir.iterdir() if ".restore-" in p.name] == []


class TestKeepValidation:
    @pytest.mark.parametrize("keep", [0, -1])
    def test_engine_rejects_keep_below_one(self, project, keep):
        from havn.engine.backup import cleanup_backups, create_backup, list_backups

        project_dir, db_path = project
        for _ in range(3):
            create_backup(project_dir, db_path)
        with pytest.raises(ValueError, match="at least 1"):
            cleanup_backups(project_dir, keep=keep)
        assert len(list_backups(project_dir)) == 3

    @pytest.mark.parametrize("keep", ["0", "-2"])
    def test_cli_rejects_keep_below_one(self, project, keep):
        from typer.testing import CliRunner

        from havn.cli import app

        project_dir, _ = project
        (project_dir / "project.yml").write_text("name: t\n", encoding="utf-8")
        result = CliRunner().invoke(app, ["backup", "--keep", keep, "-p", str(project_dir)])
        assert result.exit_code == 2  # usage error, before any backup is taken
        assert not (project_dir / "_backups").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_restore_keeps_warehouse_mode(project):
    import os
    import stat

    from havn.engine.backup import create_backup, restore_backup

    project_dir, db_path = project
    entry = create_backup(project_dir, db_path)
    os.chmod(db_path, 0o664)
    restore_backup(project_dir, db_path, Path(entry["path"]))
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o664
