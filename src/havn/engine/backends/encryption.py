"""At-rest encryption for the DuckDB warehouse file.

DuckDB can encrypt a database file with AES (``ATTACH ... (ENCRYPTION_KEY
...)``). havn exposes it through ``database.encryption_key`` in project.yml,
written as ``${HAVN_ENCRYPTION_KEY}`` so the key lives in the environment, not
the repo.

Two facts about DuckDB decide the shape of this module:

1. An encrypted file is opened by attaching it to an in-memory root
   (``connect(':memory:')`` then ``ATTACH '<file>' AS "<stem>" (ENCRYPTION_KEY
   ...)`` then ``USE "<stem>"``). A plain ``connect('<file>')`` names its
   default catalog after the file stem, so attaching ``AS "<stem>"`` makes
   ``current_database()`` identical to the unencrypted case and the rest of
   the engine is unaffected.
2. *Writing* an encrypted file needs the OpenSSL crypto provider, which ships
   in the ``httpfs`` extension. The built-in provider is read-only. So reads
   and backup verification work anywhere, but creating or updating an
   encrypted warehouse requires ``httpfs`` to be installed/loadable. When it
   is not, havn refuses rather than writing an insecure or plaintext file.
"""

from __future__ import annotations

from pathlib import Path

import duckdb


class EncryptionError(Exception):
    """At-rest encryption is configured but cannot be applied safely."""


def resolve_encryption_key(config) -> str | None:
    """The at-rest key for ``config``, or None when encryption is off.

    ``config`` is a DatabaseConfig. A value left as a literal ``${VAR}`` means
    the referenced environment variable was not set; that is a misconfiguration
    (the user asked for encryption and the key is missing), so it raises rather
    than silently falling back to an unencrypted file.
    """
    key = getattr(config, "encryption_key", None)
    if not key:
        return None
    key = str(key).strip()
    if not key:
        return None
    if key.startswith("${") and key.endswith("}"):
        var = key[2:-1]
        raise EncryptionError(
            f"database.encryption_key references ${{{var}}}, but that environment "
            f"variable is not set. Set it (e.g. in .env) or remove the key."
        )
    return key


def catalog_name(path: str | Path) -> str:
    """The catalog name DuckDB gives the warehouse file (its stem)."""
    return Path(path).stem


def load_write_crypto(conn: duckdb.DuckDBPyConnection) -> None:
    """Ensure the OpenSSL crypto provider is loaded, for writing encrypted files.

    Raises :class:`EncryptionError` if ``httpfs`` cannot be loaded, so a write
    never silently falls back to the built-in read-only (or insecure) provider.
    """
    try:
        conn.execute("INSTALL httpfs")
        conn.execute("LOAD httpfs")
    except Exception as e:  # duckdb.IOException and friends
        raise EncryptionError(
            "Writing an encrypted warehouse needs DuckDB's OpenSSL crypto "
            "provider, which ships in the 'httpfs' extension, and it could not "
            "be loaded (no network to fetch it, or it is unavailable on this "
            f"platform): {e}. Install it once where the machine has network "
            "(`duckdb -c \"INSTALL httpfs\"`); it is then cached. havn will not "
            "write a warehouse that is not securely encrypted."
        ) from e


def open_encrypted(
    path: str | Path,
    key: str,
    *,
    read_only: bool,
) -> duckdb.DuckDBPyConnection:
    """Open an encrypted warehouse file as the default catalog.

    Reads use the built-in crypto provider and need nothing extra. Writes load
    the OpenSSL provider first and raise :class:`EncryptionError` if it is
    unavailable.
    """
    path = Path(path)
    stem = catalog_name(path)
    conn = duckdb.connect(":memory:")
    if not read_only:
        load_write_crypto(conn)
        path.parent.mkdir(parents=True, exist_ok=True)
    key_lit = key.replace("'", "''")
    opts = [f"ENCRYPTION_KEY '{key_lit}'"]
    if read_only:
        opts.append("READ_ONLY")
    try:
        conn.execute(f"ATTACH '{str(path)}' AS \"{stem}\" ({', '.join(opts)})")
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        # A wrong key surfaces here; keep DuckDB's message but frame it.
        raise EncryptionError(
            f"Could not open encrypted warehouse {path} (wrong key, or the "
            f"file is not encrypted): {e}"
        ) from e
    conn.execute(f'USE "{stem}"')
    from havn.engine.write_queue import tag_default_catalog

    tag_default_catalog(conn, stem)
    return conn
