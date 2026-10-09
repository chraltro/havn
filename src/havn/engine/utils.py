"""Shared utility functions for the havn engine layer."""

from __future__ import annotations

import re

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_identifier(value: str, label: str = "identifier") -> str:
    """Validate that a value is a safe SQL identifier.

    Only allows alphanumeric characters and underscores, starting with a letter
    or underscore. Raises ValueError if the identifier is unsafe.

    This is the single validation point for SQL identifiers used across the
    engine (transform model discovery, notebook cells, API endpoints).
    """
    if not _IDENTIFIER_RE.match(value):
        raise ValueError(f"Invalid {label}: {value!r} (must match [A-Za-z_][A-Za-z0-9_]*)")
    return value


def in_transaction(conn) -> bool:
    """Whether ``conn`` is inside an explicit ``BEGIN ... COMMIT`` right now.

    DuckDB has no direct way to ask. In autocommit mode every statement is
    its own transaction, so two ``txid_current()`` calls differ; inside an
    explicit transaction they are the same. Probing with a second ``BEGIN``
    instead is not an option: DuckDB rejects it *and aborts the outer
    transaction*, so every later statement in it fails.
    """
    try:
        first = conn.execute("SELECT txid_current()").fetchone()[0]
        second = conn.execute("SELECT txid_current()").fetchone()[0]
    except Exception:
        return False
    return first == second


def begin_transaction(conn) -> bool:
    """Open a transaction on ``conn``; return False when one was already open.

    The caller commits or rolls back only what it opened: on False it is
    running inside somebody else's transaction and must leave it alone.
    """
    if in_transaction(conn):
        return False
    conn.execute("BEGIN TRANSACTION")
    return True
