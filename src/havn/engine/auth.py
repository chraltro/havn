"""Authentication and user management.

Simple token-based auth with role-based permissions.
Users stored in DuckDB _havn schema.
Roles: admin (full), editor (run + query), viewer (read-only).
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import timedelta
from pathlib import Path

import duckdb

from havn.engine.database import connect

logger = logging.getLogger("havn.auth")

# Default token lifetime: 30 days
TOKEN_LIFETIME = timedelta(days=30)


def _hash_token(token: str) -> str:
    """Hash a bearer token with SHA-256 for storage. Tokens are already
    high-entropy so a fast hash without salt is sufficient."""
    return hashlib.sha256(token.encode()).hexdigest()


def _hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """Hash a password with PBKDF2. Returns (hash_hex, salt_hex)."""
    if salt is None:
        salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return key.hex(), salt.hex()


def _verify_password(password: str, stored_hash: str, stored_salt: str) -> bool:
    """Verify password against stored hash."""
    key = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(stored_salt), 100_000
    )
    return key.hex() == stored_hash


def ensure_auth_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Create auth tables if they don't exist."""
    from havn.engine.database import _is_ducklake_connection, _strip_pk

    is_lake = _is_ducklake_connection(conn)
    conn.execute("CREATE SCHEMA IF NOT EXISTS _havn")
    conn.execute(_strip_pk("""
        CREATE TABLE IF NOT EXISTS _havn.users (
            username     VARCHAR PRIMARY KEY,
            password_hash VARCHAR NOT NULL,
            password_salt VARCHAR NOT NULL,
            role         VARCHAR NOT NULL DEFAULT 'viewer',
            display_name VARCHAR,
            created_at   TIMESTAMP DEFAULT current_timestamp,
            last_login   TIMESTAMP
        )
    """, is_lake))
    conn.execute(_strip_pk("""
        CREATE TABLE IF NOT EXISTS _havn.tokens (
            token        VARCHAR PRIMARY KEY,
            username     VARCHAR NOT NULL,
            created_at   TIMESTAMP DEFAULT current_timestamp,
            expires_at   TIMESTAMP
        )
    """, is_lake))
    _ensure_attributes_column(conn)


def _ensure_attributes_column(conn: duckdb.DuckDBPyConnection) -> None:
    """Add ``_havn.users.attributes`` to a users table that predates it.

    Attributes are admin-managed key/value pairs (``{"region": "north"}``)
    that row policies read through ``havn_attr('region')``. Checked before
    altering, because ensure_auth_tables runs on every token validation and
    an ALTER is a catalog write even when the column is already there.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM duckdb_columns() WHERE schema_name = '_havn' "
            "AND table_name = 'users' AND column_name = 'attributes'"
        ).fetchone()
        if row is None:
            conn.execute("ALTER TABLE _havn.users ADD COLUMN IF NOT EXISTS attributes JSON")
    except duckdb.Error:
        logger.debug("Could not add _havn.users.attributes", exc_info=True)


def _decode_attributes(raw) -> dict:
    """``attributes`` as stored (JSON text, or already decoded) -> a plain dict."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        import json

        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# Attribute keys are referenced from row-policy SQL as havn_attr('<key>'), so
# they are kept to identifier-like names. Values are strings, numbers,
# booleans or flat lists of those.
_ATTR_KEY_MAX = 64
_ATTR_VALUE_MAX = 1000


def normalize_attributes(attributes: dict) -> dict:
    """Validate and normalise a user-attribute mapping, or raise ValueError."""
    import re

    if not isinstance(attributes, dict):
        raise ValueError("attributes must be an object of key/value pairs")
    if len(attributes) > 100:
        raise ValueError("at most 100 attributes per user")
    out: dict = {}
    for key, value in attributes.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)                 or len(key) > _ATTR_KEY_MAX:
            raise ValueError(
                f"Invalid attribute name {key!r}: use letters, digits and underscores"
            )
        out[key.lower()] = _normalize_attr_value(key, value)
    return out


def _normalize_attr_value(key: str, value):
    scalar = (str, int, float, bool)
    if value is None:
        return None
    if isinstance(value, scalar):
        if isinstance(value, str) and len(value) > _ATTR_VALUE_MAX:
            raise ValueError(f"Attribute {key!r} is longer than {_ATTR_VALUE_MAX} characters")
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > 500:
            raise ValueError(f"Attribute {key!r} has more than 500 values")
        items = []
        for item in value:
            if not isinstance(item, scalar):
                raise ValueError(f"Attribute {key!r} may only list strings and numbers")
            items.append(item)
        return items
    raise ValueError(f"Attribute {key!r} must be a string, number, boolean or list")


def get_user_attributes(conn: duckdb.DuckDBPyConnection, username: str) -> dict | None:
    """A user's attributes, or None when the user does not exist."""
    ensure_auth_tables(conn)
    row = conn.execute(
        "SELECT attributes FROM _havn.users WHERE username = ?", [username]
    ).fetchone()
    if row is None:
        return None
    return _decode_attributes(row[0])


def set_user_attributes(
    conn: duckdb.DuckDBPyConnection, username: str, attributes: dict
) -> dict | None:
    """Replace a user's attributes. Returns the stored mapping, or None if no such user."""
    import json

    ensure_auth_tables(conn)
    clean = normalize_attributes(attributes)
    existing = conn.execute(
        "SELECT username FROM _havn.users WHERE username = ?", [username]
    ).fetchone()
    if not existing:
        return None
    conn.execute(
        "UPDATE _havn.users SET attributes = ? WHERE username = ?",
        [json.dumps(clean), username],
    )
    return clean


def create_user(
    conn: duckdb.DuckDBPyConnection,
    username: str,
    password: str,
    role: str = "viewer",
    display_name: str | None = None,
) -> dict:
    """Create a new user."""
    ensure_auth_tables(conn)
    if role not in ("admin", "editor", "viewer"):
        raise ValueError(f"Invalid role: {role}. Must be admin, editor, or viewer.")

    # Check if user exists
    existing = conn.execute(
        "SELECT username FROM _havn.users WHERE username = ?", [username]
    ).fetchone()
    if existing:
        raise ValueError(f"User '{username}' already exists")

    pw_hash, pw_salt = _hash_password(password)
    conn.execute(
        """
        INSERT INTO _havn.users (username, password_hash, password_salt, role, display_name)
        VALUES (?, ?, ?, ?, ?)
        """,
        [username, pw_hash, pw_salt, role, display_name or username],
    )
    return {"username": username, "role": role, "display_name": display_name or username}


def authenticate(conn: duckdb.DuckDBPyConnection, username: str, password: str) -> str | None:
    """Authenticate user, return token or None."""
    ensure_auth_tables(conn)
    row = conn.execute(
        "SELECT password_hash, password_salt FROM _havn.users WHERE username = ?",
        [username],
    ).fetchone()
    if not row:
        # Perform a dummy hash to prevent timing-based username enumeration
        _hash_password(password, os.urandom(32))
        return None
    if not _verify_password(password, row[0], row[1]):
        return None

    # Generate token with expiration — store hash, return plaintext
    token = secrets.token_urlsafe(32)
    token_hash = _hash_token(token)
    conn.execute(
        "INSERT INTO _havn.tokens (token, username, expires_at) VALUES (?, ?, current_timestamp + ?)",
        [token_hash, username, TOKEN_LIFETIME],
    )
    conn.execute(
        "UPDATE _havn.users SET last_login = current_timestamp WHERE username = ?",
        [username],
    )
    logger.info("User '%s' authenticated successfully", username)
    return token


def validate_token(conn: duckdb.DuckDBPyConnection, token: str) -> dict | None:
    """Validate a token and return user info, or None.

    Rejects expired tokens (where expires_at is set and in the past).
    """
    ensure_auth_tables(conn)
    token_hash = _hash_token(token)
    row = conn.execute(
        """
        SELECT t.username, u.role, u.display_name, u.attributes
        FROM _havn.tokens t
        JOIN _havn.users u ON t.username = u.username
        WHERE t.token = ?
          AND (t.expires_at IS NULL OR t.expires_at > current_timestamp)
        """,
        [token_hash],
    ).fetchone()
    if not row:
        # Clean up expired tokens opportunistically
        conn.execute(
            "DELETE FROM _havn.tokens WHERE expires_at IS NOT NULL AND expires_at <= current_timestamp"
        )
        return None
    return {
        "username": row[0],
        "role": row[1],
        "display_name": row[2],
        "attributes": _decode_attributes(row[3]),
    }


def list_users(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """List all users (no passwords)."""
    ensure_auth_tables(conn)
    rows = conn.execute(
        """
        SELECT username, role, display_name, created_at, last_login, attributes
        FROM _havn.users
        ORDER BY created_at
        """
    ).fetchall()
    return [
        {
            "username": r[0],
            "role": r[1],
            "display_name": r[2],
            "created_at": str(r[3]) if r[3] else None,
            "last_login": str(r[4]) if r[4] else None,
            "attributes": _decode_attributes(r[5]),
        }
        for r in rows
    ]


def update_user(
    conn: duckdb.DuckDBPyConnection,
    username: str,
    role: str | None = None,
    password: str | None = None,
    display_name: str | None = None,
) -> bool:
    """Update user fields. Returns True if found."""
    ensure_auth_tables(conn)
    existing = conn.execute(
        "SELECT username FROM _havn.users WHERE username = ?", [username]
    ).fetchone()
    if not existing:
        return False

    if role:
        if role not in ("admin", "editor", "viewer"):
            raise ValueError(f"Invalid role: {role}")
        conn.execute(
            "UPDATE _havn.users SET role = ? WHERE username = ?",
            [role, username],
        )
    if password:
        pw_hash, pw_salt = _hash_password(password)
        conn.execute(
            "UPDATE _havn.users SET password_hash = ?, password_salt = ? WHERE username = ?",
            [pw_hash, pw_salt, username],
        )
    if display_name:
        conn.execute(
            "UPDATE _havn.users SET display_name = ? WHERE username = ?",
            [display_name, username],
        )
    return True


def delete_user(conn: duckdb.DuckDBPyConnection, username: str) -> bool:
    """Delete a user and their tokens."""
    ensure_auth_tables(conn)
    existing = conn.execute(
        "SELECT username FROM _havn.users WHERE username = ?", [username]
    ).fetchone()
    if not existing:
        return False
    conn.execute("DELETE FROM _havn.tokens WHERE username = ?", [username])
    conn.execute("DELETE FROM _havn.users WHERE username = ?", [username])
    return True


def revoke_tokens(conn: duckdb.DuckDBPyConnection, username: str) -> int:
    """Revoke all tokens for a user."""
    ensure_auth_tables(conn)
    before = conn.execute(
        "SELECT COUNT(*) FROM _havn.tokens WHERE username = ?", [username]
    ).fetchone()[0]
    conn.execute("DELETE FROM _havn.tokens WHERE username = ?", [username])
    return before


def has_any_users(conn: duckdb.DuckDBPyConnection) -> bool:
    """Check if any users exist (for initial setup)."""
    ensure_auth_tables(conn)
    row = conn.execute("SELECT COUNT(*) FROM _havn.users").fetchone()
    return row[0] > 0


ROLE_PERMISSIONS = {
    "admin": {"read", "write", "execute", "manage_users", "manage_secrets"},
    "editor": {"read", "write", "execute"},
    "viewer": {"read"},
}


def has_permission(role: str, permission: str) -> bool:
    """Check if a role has a specific permission."""
    return permission in ROLE_PERMISSIONS.get(role, set())
