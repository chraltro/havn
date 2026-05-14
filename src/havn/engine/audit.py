"""Audit logging for tracking user actions.

Writes structured audit entries to _havn.audit_log in DuckDB.
"""

from __future__ import annotations

import logging

import duckdb

logger = logging.getLogger("havn.audit")

# Valid audit actions
VALID_ACTIONS = frozenset({
    "query",
    "transform",
    "ingest",
    "export",
    "file_edit",
    "file_delete",
    "login",
    "config_change",
    "auth_failed",
    "permission_denied",
    "connector_sync",
    "connector_setup",
    "masking_policy_create",
    "masking_policy_update",
    "masking_policy_delete",
    "user_create",
    "user_update",
    "user_delete",
    "token_revoke",
    "snapshot_restore",
    "secret_change",
})


def ensure_audit_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the audit_log table if it doesn't exist."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS _havn")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.audit_log (
            id INTEGER PRIMARY KEY DEFAULT nextval('_havn.audit_log_seq'),
            "user" VARCHAR NOT NULL,
            action VARCHAR NOT NULL,
            resource VARCHAR,
            detail VARCHAR,
            ip_address VARCHAR,
            "timestamp" TIMESTAMP DEFAULT current_timestamp
        )
    """)


def _ensure_sequence(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the audit_log sequence if it doesn't exist."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS _havn")
    try:
        conn.execute("CREATE SEQUENCE IF NOT EXISTS _havn.audit_log_seq START 1")
    except duckdb.CatalogException:
        pass
    except Exception as e:
        logger.debug("CREATE SEQUENCE audit_log_seq failed: %s", e)


def log_audit(
    conn: duckdb.DuckDBPyConnection,
    user: str,
    action: str,
    resource: str,
    detail: str | None = None,
    ip_address: str | None = None,
) -> None:
    """Write an audit log entry.

    Parameters
    ----------
    conn : DuckDB connection
    user : Username or "anonymous"
    action : One of VALID_ACTIONS
    resource : The resource being acted upon (e.g. SQL query, file path, stream name)
    detail : Optional additional detail
    ip_address : Client IP address
    """
    if action not in VALID_ACTIONS:
        logger.warning("Unknown audit action: %s", action)

    _ensure_sequence(conn)
    ensure_audit_table(conn)
    conn.execute(
        """
        INSERT INTO _havn.audit_log ("user", action, resource, detail, ip_address)
        VALUES (?, ?, ?, ?, ?)
        """,
        [user, action, resource, detail, ip_address],
    )


def query_audit_log(
    conn: duckdb.DuckDBPyConnection,
    limit: int = 100,
    user: str | None = None,
    action: str | None = None,
    resource: str | None = None,
) -> list[dict]:
    """Query audit log entries with optional filters.

    Parameters
    ----------
    conn : DuckDB connection
    limit : Maximum entries to return (default 100)
    user : Filter by username
    action : Filter by action type
    resource : Filter by resource (substring match)

    Returns
    -------
    List of audit log entries as dicts.
    """
    _ensure_sequence(conn)
    ensure_audit_table(conn)

    conditions = []
    params: list = []

    if user is not None:
        conditions.append('"user" = ?')
        params.append(user)
    if action is not None:
        conditions.append("action = ?")
        params.append(action)
    if resource is not None:
        conditions.append("resource ILIKE ?")
        params.append(f"%{resource}%")

    where = ""
    if conditions:
        where = "WHERE " + " AND ".join(conditions)

    params.append(max(1, min(limit, 1000)))

    rows = conn.execute(
        f"""
        SELECT id, "user", action, resource, detail, ip_address, "timestamp"
        FROM _havn.audit_log
        {where}
        ORDER BY "timestamp" DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

    return [
        {
            "id": r[0],
            "user": r[1],
            "action": r[2],
            "resource": r[3],
            "detail": r[4],
            "ip_address": r[5],
            "timestamp": str(r[6]) if r[6] else None,
        }
        for r in rows
    ]
