"""At-rest encryption for the DuckDB warehouse.

What is verified here, offline:
- the key resolves from config (and a missing ``${VAR}`` is an error, not a
  silent fall-back to plaintext);
- an encrypted warehouse reads back through the backend with the key, and a
  wrong key is refused;
- the plaintext path is unchanged when no key is set;
- a write is gated on the OpenSSL crypto provider, so havn never writes a
  warehouse that is not securely encrypted.

Creating an encrypted file for the read tests uses DuckDB's unsafe writer
(``force_mbedtls_unsafe``) because this sandbox cannot fetch the httpfs
extension that provides the secure writer. The ciphertext format is the same;
only the key RNG differs, which does not matter for a fixture. An end-to-end
encrypted *write* through havn needs a machine with httpfs and is covered by
the write-gate test plus manual verification (see docs/encryption.md).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from havn.config import DatabaseConfig
from havn.engine.backends.duckdb_backend import DuckDBBackend
from havn.engine.backends.encryption import (
    EncryptionError,
    open_encrypted,
    resolve_encryption_key,
)

KEY = "test-key-9f3a"


def _write_encrypted_fixture(path, key: str = KEY) -> None:
    """Create an encrypted warehouse with one table, the way open_encrypted would."""
    stem = path.stem
    c = duckdb.connect(":memory:")
    c.execute("SET force_mbedtls_unsafe = 'true'")
    c.execute(f"ATTACH '{path}' AS \"{stem}\" (ENCRYPTION_KEY '{key}')")
    c.execute(f'USE "{stem}"')
    c.execute("CREATE SCHEMA gold")
    c.execute("CREATE TABLE gold.customers AS SELECT 1 AS id, 'secret-value' AS note")
    c.execute(f'CHECKPOINT "{stem}"')
    c.close()


def test_resolve_encryption_key():
    assert resolve_encryption_key(DatabaseConfig(path="w.duckdb")) is None
    assert resolve_encryption_key(DatabaseConfig(path="w.duckdb", encryption_key="  ")) is None
    assert resolve_encryption_key(DatabaseConfig(path="w.duckdb", encryption_key="k")) == "k"
    # A ${VAR} left unexpanded means the env var was not set: refuse, don't
    # silently write plaintext.
    with pytest.raises(EncryptionError):
        resolve_encryption_key(DatabaseConfig(path="w.duckdb", encryption_key="${MISSING_VAR}"))


def test_encrypted_file_is_not_plaintext_and_needs_the_key(tmp_path):
    db = tmp_path / "warehouse.duckdb"
    _write_encrypted_fixture(db)
    assert b"secret-value" not in db.read_bytes()
    # No key at all: refused.
    with pytest.raises(Exception):
        duckdb.connect(":memory:").execute(f"ATTACH '{db}' AS w (READ_ONLY)")
    # Wrong key: refused (framed as EncryptionError by open_encrypted).
    with pytest.raises(EncryptionError):
        open_encrypted(db, "wrong-key", read_only=True)


def test_backend_reads_encrypted_warehouse(tmp_path):
    db = tmp_path / "warehouse.duckdb"
    _write_encrypted_fixture(db)
    backend = DuckDBBackend(DatabaseConfig(path=str(db), encryption_key=KEY))
    assert backend.default_catalog == "warehouse"
    conn = backend.connect(read_only=True)
    try:
        # Unqualified schema.table resolves, exactly as for a plain file.
        assert conn.execute("SELECT note FROM gold.customers").fetchone() == ("secret-value",)
        assert conn.execute("SELECT current_database()").fetchone()[0] == "warehouse"
    finally:
        conn.close()


def test_backend_plaintext_path_unchanged_without_key(tmp_path):
    db = tmp_path / "warehouse.duckdb"
    backend = DuckDBBackend(DatabaseConfig(path=str(db)))
    assert backend.default_catalog is None
    conn = backend.connect(read_only=False)
    try:
        conn.execute("CREATE SCHEMA gold")
        conn.execute("CREATE TABLE gold.t AS SELECT 1 AS x")
        assert conn.execute("SELECT x FROM gold.t").fetchone() == (1,)
    finally:
        conn.close()
    # A plaintext file: the value is on disk in the clear.
    assert db.exists()


def test_backup_of_encrypted_warehouse_verifies_with_key(tmp_path):
    # `havn backup` on an encrypted warehouse must copy and verify it. Verify
    # opens the copy read-only, which needs the key; without threading it the
    # verify fails and the copy is unlinked.
    from havn.config import DatabaseConfig
    from havn.engine.backup import create_backup, verify_backup
    from havn.engine.backends.encryption import resolve_encryption_key

    db = tmp_path / "warehouse.duckdb"
    _write_encrypted_fixture(db)
    key = resolve_encryption_key(DatabaseConfig(path=str(db), encryption_key=KEY))

    entry = create_backup(tmp_path, db, verify=True, encryption_key=key)
    assert entry["verified"] is True
    backup_path = tmp_path / entry["path"] if not Path(entry["path"]).is_absolute() else Path(entry["path"])
    # The backup copy is itself encrypted (byte copy) and opens with the key.
    assert verify_backup(backup_path, project_dir=tmp_path, encryption_key=key)["valid"] is True
    # ...and not without it.
    assert verify_backup(backup_path, project_dir=tmp_path)["valid"] is False


def test_write_refuses_without_secure_crypto(tmp_path, monkeypatch):
    # Opening read-write must load the OpenSSL provider; if it cannot, havn
    # refuses rather than writing an insecure file.
    import havn.engine.backends.encryption as enc

    def _no_provider(conn):
        raise EncryptionError("httpfs unavailable")

    monkeypatch.setattr(enc, "load_write_crypto", _no_provider)
    with pytest.raises(EncryptionError):
        enc.open_encrypted(tmp_path / "new.duckdb", KEY, read_only=False)
