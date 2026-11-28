"""Versioned migrations for havn's own metadata schema (``_havn``).

The single source of truth for every ``_havn`` table havn creates. The list
``MIGRATIONS`` is ordered and append-only; each entry runs once per warehouse,
inside its own transaction, and is recorded in ``_havn.schema_migrations``
only when it commits.

When migrations run
-------------------
- Opening a warehouse for writing (both backends'
  ``connect(read_only=False)`` and ``database.connect(read_only=False)``)
  checks the version and applies what is pending, before the caller can
  open a transaction. On an up-to-date warehouse that is four tiny queries
  (about 2 ms), independent of how many tables the warehouse holds.
- Every ``ensure_*_table`` helper calls :func:`ensure_migrated`, so a
  connection opened some other way (a plain ``duckdb.connect`` in a test or
  a script) is migrated on first use, as the lazy helpers did before. Once
  this process has seen a database file at the current version that call
  is one ``duckdb_databases()`` query (roughly 0.3 to 1 ms). An in-memory
  database is never remembered as settled, so there it reads the applied
  versions every time. If that first use happens inside the caller's open
  transaction the migration joins it (DuckDB has no nested transactions),
  and is kept or lost with it.
- Read-only connections never migrate. Code reading through them keeps
  handling an absent table, as it always has.

One process-wide lock serializes the work, so threads sharing a connection
(the server's per-thread cursors) or opening the same file at once apply each
migration exactly once: the second thread re-reads the applied versions after
taking the lock and finds nothing to do.

DuckDB 2 rules the code here keeps to: a failed statement aborts the
surrounding transaction, so the one statement that can fail by design (reading
a missing ``schema_migrations``) only runs outside a transaction; and a
half-read result blocks the connection, so every query is read with
``fetchall()``.

A warehouse written by a newer havn (its highest applied version is above
``CURRENT_VERSION``) is left alone: nothing is applied and a warning is
logged once per process. Migrations never drop, rename or retype a column
(they add tables and columns, and may rewrite rows, as migration 2 does), so
an older havn keeps working with the tables it knows; see docs/upgrading.md.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Callable

import duckdb

from . import _0001_baseline, _0002_lowercase_model_keys

logger = logging.getLogger("havn.migrations")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: Callable[[duckdb.DuckDBPyConnection, "MigrationContext"], None]


@dataclass(frozen=True)
class MigrationContext:
    """What a migration may need to know about the connection it runs on."""

    is_lake: bool


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline", _0001_baseline.up),
    Migration(2, "lowercase_model_keys", _0002_lowercase_model_keys.up),
)

CURRENT_VERSION = MIGRATIONS[-1].version


class MigrationError(RuntimeError):
    """A metadata migration failed; the warehouse is left at the previous version."""

    def __init__(
        self,
        migration: Migration,
        database: str,
        cause: BaseException,
        *,
        in_caller_transaction: bool = False,
    ):
        self.migration = migration
        self.database = database
        self.cause = cause
        self.in_caller_transaction = in_caller_transaction
        if in_caller_transaction:
            outcome = (
                "It ran inside an open transaction of the caller, which must now be "
                "rolled back: nothing done in that transaction is kept, including "
                "this migration and any applied in it before; migrations committed "
                "earlier are kept."
            )
        else:
            outcome = (
                "That migration was rolled back and not recorded; earlier ones are kept."
            )
        super().__init__(
            f"Upgrading havn's metadata schema failed at migration "
            f"{migration.version} ({migration.name}) on {database}: {cause}. "
            f"{outcome} Restore a backup or report this error with the message above."
        )


@dataclass(frozen=True)
class MigrationStatus:
    """Where a warehouse's metadata schema stands relative to this havn."""

    version: int                # highest applied version, 0 when none
    latest: int                 # CURRENT_VERSION
    pending: tuple[Migration, ...]
    newer_than_code: bool       # written by a havn that knows later migrations

    @property
    def up_to_date(self) -> bool:
        return not self.pending and not self.newer_than_code


_lock = threading.RLock()
# Databases (by normalized path) this process has seen at CURRENT_VERSION, or
# found to be newer than the code. ensure_migrated skips them.
_settled: set[str] = set()
_warned_newer: set[str] = set()


def _normalize(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _database_info(conn: duckdb.DuckDBPyConnection) -> tuple[str | None, bool]:
    """``(normalized path or None for in-memory, read_only)`` of the current database."""
    rows = conn.execute(
        "SELECT path, readonly FROM duckdb_databases() WHERE database_name = current_database()"
    ).fetchall()
    if not rows:
        return None, False
    path, readonly = rows[0]
    return (_normalize(str(path)) if path else None), bool(readonly)


def _in_transaction(conn: duckdb.DuckDBPyConnection) -> bool:
    """True when the connection has an explicit transaction open.

    Under autocommit every statement gets a new transaction id, while inside
    an explicit transaction two reads return the same one. It cannot be found
    out by trying BEGIN and catching the error: a failed statement aborts the
    caller's transaction in DuckDB. (current_transaction_id, not txid_current:
    the latter hits an internal error on DuckLake.)
    """
    first = conn.execute("SELECT current_transaction_id()").fetchall()[0][0]
    second = conn.execute("SELECT current_transaction_id()").fetchall()[0][0]
    return first == second


def _read_applied(conn: duckdb.DuckDBPyConnection) -> set[int] | None:
    """Versions in ``_havn.schema_migrations``, or None when the table is absent.

    Outside a transaction the table is simply read, and a missing table shows
    up as a CatalogException: that is the cheap path every connect takes.
    Inside a caller's transaction a failed statement would abort it, so the
    catalog is asked first; duckdb_tables() lists every table in the
    database, which costs tens of milliseconds in a warehouse with hundreds
    of tables, so it is kept off the common path.
    """
    query = "SELECT version FROM _havn.schema_migrations"
    if _in_transaction(conn):
        row = conn.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE database_name = current_database() "
            "AND schema_name = '_havn' AND table_name = 'schema_migrations'"
        ).fetchall()
        if not row[0][0]:
            return None
        return {int(r[0]) for r in conn.execute(query).fetchall()}
    try:
        rows = conn.execute(query).fetchall()
    except duckdb.CatalogException:
        return None
    return {int(r[0]) for r in rows}


def _has_metadata_tables(conn: duckdb.DuckDBPyConnection) -> bool:
    """True when ``_havn`` holds any table besides ``schema_migrations``."""
    row = conn.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE database_name = current_database() "
        "AND schema_name = '_havn' AND table_name <> 'schema_migrations'"
    ).fetchall()
    return bool(row[0][0])


def applied_versions(conn: duckdb.DuckDBPyConnection) -> set[int]:
    """Versions recorded in ``_havn.schema_migrations`` (empty when the table is absent)."""
    return _read_applied(conn) or set()


def migration_status(conn: duckdb.DuckDBPyConnection) -> MigrationStatus:
    """Read-only view of the warehouse's schema version. Safe on read-only connections."""
    applied = applied_versions(conn)
    pending = tuple(m for m in MIGRATIONS if m.version not in applied)
    version = max(applied) if applied else 0
    return MigrationStatus(
        version=version,
        latest=CURRENT_VERSION,
        pending=pending,
        newer_than_code=version > CURRENT_VERSION,
    )


def forget_database(path: str | os.PathLike) -> None:
    """Make the next :func:`ensure_migrated` on ``path`` check the warehouse again.

    Called when a connection to the file is (re)opened, because the file may
    have been replaced since this process last looked (a restored backup).
    """
    with _lock:
        _settled.discard(_normalize(str(path)))


def ensure_migrated(conn: duckdb.DuckDBPyConnection) -> None:
    """Bring the connection's database to the current version, once per process.

    A no-op on read-only connections and on databases already seen at the
    current version. Raises :class:`MigrationError` when a migration fails.
    """
    path, read_only = _database_info(conn)
    if read_only:
        return
    if path is not None and path in _settled:
        return
    with _lock:
        if path is not None and path in _settled:
            return
        _apply_locked(conn, path)


def on_writable_connect(conn: duckdb.DuckDBPyConnection) -> None:
    """Hook for the backends: re-check and migrate a freshly opened writable connection."""
    path, read_only = _database_info(conn)
    if read_only:
        return
    with _lock:
        if path is not None:
            _settled.discard(path)
        _apply_locked(conn, path)


def apply_migrations(conn: duckdb.DuckDBPyConnection) -> list[Migration]:
    """Apply every pending migration now and return the ones applied.

    Unlike :func:`ensure_migrated` this always reads the applied versions.
    Raises ``duckdb.InvalidInputException`` on a read-only connection.
    """
    path, read_only = _database_info(conn)
    if read_only:
        raise duckdb.InvalidInputException(
            "Cannot apply metadata migrations on a read-only connection"
        )
    with _lock:
        return _apply_locked(conn, path)


def _apply_locked(conn: duckdb.DuckDBPyConnection, path: str | None) -> list[Migration]:
    from havn.engine.database import _is_ducklake_connection

    label = path or "an in-memory database"
    applied = _read_applied(conn)
    if applied is None:
        # No primary key, so the same DDL works on DuckLake; the lock and the
        # re-read under it are what keep versions unique.
        conn.execute("CREATE SCHEMA IF NOT EXISTS _havn")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _havn.schema_migrations ("
            "version INTEGER NOT NULL, name VARCHAR NOT NULL, "
            "applied_at TIMESTAMP DEFAULT current_timestamp, havn_version VARCHAR)"
        )
        applied = set()
    # Creating the schema of a brand-new warehouse is not news; upgrading an
    # existing one is, so only that is logged at INFO.
    fresh = not applied and not _has_metadata_tables(conn)
    highest = max(applied) if applied else 0
    if highest > CURRENT_VERSION:
        if path is not None:
            _settled.add(path)
        if (path or "") not in _warned_newer:
            _warned_newer.add(path or "")
            logger.warning(
                "The metadata schema of %s is at version %d, newer than this havn "
                "knows (%d): it was last opened by a newer havn. No migrations were "
                "applied. Older releases keep working with the tables they know, "
                "but upgrading havn again is the safe path.",
                label, highest, CURRENT_VERSION,
            )
        return []

    pending = [m for m in MIGRATIONS if m.version not in applied]
    if not pending:
        if path is not None:
            _settled.add(path)
        return []

    ctx = MigrationContext(is_lake=_is_ducklake_connection(conn))
    version = _havn_version()
    done: list[Migration] = []
    for migration in pending:
        own_transaction = _begin(conn)
        try:
            migration.up(conn, ctx)
            conn.execute(
                "INSERT INTO _havn.schema_migrations (version, name, applied_at, havn_version) "
                "VALUES (?, ?, current_timestamp, ?)",
                [migration.version, migration.name, version],
            )
            if own_transaction:
                conn.execute("COMMIT")
        except Exception as e:
            if own_transaction:
                try:
                    conn.execute("ROLLBACK")
                except duckdb.Error:
                    logger.debug("ROLLBACK after a failed migration failed", exc_info=True)
            raise MigrationError(
                migration, label, e, in_caller_transaction=not own_transaction
            ) from e
        done.append(migration)
        logger.log(
            logging.DEBUG if fresh else logging.INFO,
            "Applied metadata migration %d (%s) to %s", migration.version, migration.name, label,
        )
        if not own_transaction:
            # The caller's transaction holds the change and may still roll it
            # back, so do not remember the database as settled.
            path = None
    if path is not None:
        _settled.add(path)
    return done


def _begin(conn: duckdb.DuckDBPyConnection) -> bool:
    """Open a transaction; False when the caller already has one open.

    A migration started from inside a caller's transaction (a lazy ensure_*
    call in the middle of a write) becomes part of it, since DuckDB has no
    nested transactions or savepoints.
    """
    if _in_transaction(conn):
        return False
    conn.execute("BEGIN TRANSACTION")
    return True


def _havn_version() -> str | None:
    try:
        from havn import __version__

        return str(__version__)
    except Exception:
        return None


__all__ = [
    "CURRENT_VERSION",
    "MIGRATIONS",
    "Migration",
    "MigrationContext",
    "MigrationError",
    "MigrationStatus",
    "applied_versions",
    "apply_migrations",
    "ensure_migrated",
    "forget_database",
    "migration_status",
    "on_writable_connect",
]
