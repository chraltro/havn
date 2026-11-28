"""Authentication and user management.

Simple token-based auth with role-based permissions.
Users stored in DuckDB _havn schema.
Roles: admin (full), editor (run + query), viewer (read-only).
"""

from __future__ import annotations

import hashlib
import hmac
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

# Shortest password accepted when one is set (``havn users create``,
# ``POST /api/users``, ``PUT /api/users/{username}``, first-run setup). Not
# applied at login, so users created under the older 4-character rule can still
# sign in.
MIN_PASSWORD_LENGTH = 8


def _hash_token(token: str) -> str:
    """Hash a bearer token with SHA-256 for storage. Tokens are already
    high-entropy so a fast hash without salt is sufficient."""
    return hashlib.sha256(token.encode()).hexdigest()


# PBKDF2-HMAC-SHA256 work factor for new and changed passwords (OWASP 2023
# guidance). Stored per user in ``_havn.users.password_iterations`` so it can be
# raised later: a login with an older count re-hashes at this value. The test
# suite lowers it in ``tests/conftest.py``; production code never changes it.
PBKDF2_ITERATIONS = 600_000

# Rows written before the count was stored (``password_iterations`` IS NULL)
# were hashed with this fixed value.
LEGACY_PBKDF2_ITERATIONS = 100_000


def _pbkdf2(password: str, salt: bytes, iterations: int) -> bytes:
    """The one place a password is stretched (tests count calls through it)."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)


def _hash_password(
    password: str, salt: bytes | None = None, iterations: int | None = None
) -> tuple[str, str]:
    """Hash a password with PBKDF2. Returns (hash_hex, salt_hex).

    ``iterations`` defaults to the current ``PBKDF2_ITERATIONS``.
    """
    if salt is None:
        salt = os.urandom(32)
    if iterations is None:
        iterations = PBKDF2_ITERATIONS
    return _pbkdf2(password, salt, iterations).hex(), salt.hex()


def _verify_password(
    password: str,
    stored_hash: str,
    stored_salt: str,
    iterations: int | None = None,
) -> bool:
    """Verify a password against its stored hash in constant time.

    ``iterations`` is the count stored with the hash; None means a legacy row
    hashed at ``LEGACY_PBKDF2_ITERATIONS``.
    """
    if iterations is None:
        iterations = LEGACY_PBKDF2_ITERATIONS
    key = _pbkdf2(password, bytes.fromhex(stored_salt), iterations)
    return hmac.compare_digest(key.hex(), stored_hash)


def ensure_auth_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Make sure ``_havn.users`` and ``_havn.tokens`` exist with every column.

    The tables are defined in :mod:`havn.engine.migrations` (baseline). This
    runs on every token validation, so it must stay cheap: once the process
    has seen the warehouse file at the current version it is one
    ``duckdb_databases()`` query (well under a millisecond to about one).
    """
    from havn.engine.database import ensure_meta_table

    ensure_meta_table(conn)


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

    iterations = PBKDF2_ITERATIONS
    pw_hash, pw_salt = _hash_password(password, iterations=iterations)
    conn.execute(
        """
        INSERT INTO _havn.users
            (username, password_hash, password_salt, password_iterations, role, display_name)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [username, pw_hash, pw_salt, iterations, role, display_name or username],
    )
    return {"username": username, "role": role, "display_name": display_name or username}


def authenticate(conn: duckdb.DuckDBPyConnection, username: str, password: str) -> str | None:
    """Authenticate user, return token or None.

    Every failed attempt costs at least ``PBKDF2_ITERATIONS`` of hashing,
    whether the username exists or not, so response time does not reveal
    which usernames exist. A successful login whose stored hash uses fewer
    iterations than the current default is re-hashed at the default.
    """
    ensure_auth_tables(conn)
    row = conn.execute(
        "SELECT password_hash, password_salt, password_iterations "
        "FROM _havn.users WHERE username = ?",
        [username],
    ).fetchone()
    target = PBKDF2_ITERATIONS
    if not row:
        # Dummy hash at the current default: an unknown username costs the
        # same as a wrong password for an up-to-date user.
        _pbkdf2(password, os.urandom(32), target)
        return None
    stored_iterations = row[2] if row[2] is not None else LEGACY_PBKDF2_ITERATIONS
    if not _verify_password(password, row[0], row[1], stored_iterations):
        if stored_iterations < target:
            # A row hashed at an older, cheaper count: make up the difference
            # so a wrong password costs what an unknown username costs.
            _pbkdf2(password, os.urandom(32), target - stored_iterations)
        return None
    verified_hash = row[0]
    # The hashes the stored row may hold for the login to stand: the one just
    # verified, or the upgrade written from it below. Anything else means the
    # password was changed (or the user deleted) while this login was hashing.
    current_hashes = [verified_hash, verified_hash]
    if stored_iterations < target:
        upgraded = _rehash_password(conn, username, password, target, verified_hash)
        if upgraded is not None:
            current_hashes[1] = upgraded

    token = _issue_token(conn, username, current_hashes)
    if token is None:
        logger.info("Login for '%s' refused: the password changed during sign-in", username)
        return None
    logger.info("User '%s' authenticated successfully", username)
    return token


def _issue_token(
    conn: duckdb.DuckDBPyConnection, username: str, current_hashes: list[str]
) -> str | None:
    """Insert a session token if the user's stored hash is one of ``current_hashes``.

    The check is a conditional write to the user's row (``last_login``), so a
    concurrent password change, which writes the same row, either commits
    first (the check finds a different hash and no token is issued) or
    conflicts with this transaction. The check and the token insert share one
    transaction, opened here unless the caller's connection is already inside
    one, which is then joined (as ``engine/transform/execution.py`` does) and
    left for its owner to commit.
    """
    from havn.engine.utils import begin_transaction

    token = secrets.token_urlsafe(32)
    owns = begin_transaction(conn)
    try:
        updated = conn.execute(
            "UPDATE _havn.users SET last_login = current_timestamp "
            "WHERE username = ? AND password_hash IN (?, ?)",
            [username, *current_hashes],
        ).fetchone()
        if not updated or not updated[0]:
            if owns:
                conn.execute("ROLLBACK")
            return None
        # Store the hash, return the plaintext.
        conn.execute(
            "INSERT INTO _havn.tokens (token, username, expires_at) "
            "VALUES (?, ?, current_timestamp + ?)",
            [_hash_token(token), username, TOKEN_LIFETIME],
        )
        if owns:
            conn.execute("COMMIT")
    except BaseException:
        if owns:
            try:
                conn.execute("ROLLBACK")
            except duckdb.Error:
                pass
        raise
    return token


def _rehash_password(
    conn: duckdb.DuckDBPyConnection,
    username: str,
    password: str,
    iterations: int,
    verified_hash: str,
) -> str | None:
    """Re-hash a just-verified password at ``iterations`` (same password, so
    sessions are kept). Returns the new hash if it was stored.

    Written only while the row still holds ``verified_hash``: hashing takes
    long enough for an admin to change the password meanwhile, and an
    unconditional write would put the old password back. A failure here must
    not fail the login.
    """
    try:
        pw_hash, pw_salt = _hash_password(password, iterations=iterations)
        updated = conn.execute(
            "UPDATE _havn.users SET password_hash = ?, password_salt = ?, "
            "password_iterations = ? WHERE username = ? AND password_hash = ?",
            [pw_hash, pw_salt, iterations, username, verified_hash],
        ).fetchone()
    except duckdb.Error:
        logger.warning("Could not upgrade password hash for '%s'", username, exc_info=True)
        return None
    if not updated or not updated[0]:
        logger.info("Password hash for '%s' changed during sign-in; not upgraded", username)
        return None
    logger.info("Upgraded password hash for '%s' to %d iterations", username, iterations)
    return pw_hash


def validate_token(conn: duckdb.DuckDBPyConnection, token: str) -> dict | None:
    """Validate a token and return user info, or None.

    Rejects expired tokens (where expires_at is set and in the past).
    """
    return validate_token_with_expiry(conn, token)[0]


def validate_token_with_expiry(
    conn: duckdb.DuckDBPyConnection, token: str
) -> tuple[dict | None, float | None]:
    """``validate_token`` plus the seconds the token has left.

    The second item is None for a token without an expiry or an invalid
    token. It is measured by the database against the same clock the
    ``expires_at`` comparison uses, so a caller that caches the result can
    stop serving it on time without comparing wall clocks or time zones.
    """
    ensure_auth_tables(conn)
    token_hash = _hash_token(token)
    row = conn.execute(
        """
        SELECT t.username, u.role, u.display_name, u.attributes,
               epoch(CAST(t.expires_at AS TIMESTAMPTZ)) - epoch(current_timestamp)
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
        return None, None
    user = {
        "username": row[0],
        "role": row[1],
        "display_name": row[2],
        "attributes": _decode_attributes(row[3]),
    }
    remaining = float(row[4]) if row[4] is not None else None
    return user, remaining


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
    *,
    keep_token: str | None = None,
) -> bool:
    """Update user fields. Returns True if found.

    Changing the password revokes every session token the user holds, so a
    token issued before a reset (perhaps a stolen one) stops working.
    ``keep_token`` (a plaintext token of this user) is spared: the API passes
    the caller's own token when an admin changes their own password, so the
    session that made the change stays signed in and every other one ends.
    """
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
        iterations = PBKDF2_ITERATIONS
        pw_hash, pw_salt = _hash_password(password, iterations=iterations)
        # One transaction (joined if the caller has one open), so a login
        # cannot slip a token in between the revocation and the new hash:
        # _issue_token writes this same row, so it either commits before
        # (and its token is deleted here) or sees the new hash. Sessions
        # still go first, for a caller whose outer transaction keeps going
        # after a failed hash update: the user is then signed out with the
        # old password still valid, never the reverse.
        from havn.engine.utils import begin_transaction

        owns = begin_transaction(conn)
        try:
            if keep_token:
                conn.execute(
                    "DELETE FROM _havn.tokens WHERE username = ? AND token <> ?",
                    [username, _hash_token(keep_token)],
                )
            else:
                conn.execute("DELETE FROM _havn.tokens WHERE username = ?", [username])
            conn.execute(
                "UPDATE _havn.users SET password_hash = ?, password_salt = ?, "
                "password_iterations = ? WHERE username = ?",
                [pw_hash, pw_salt, iterations, username],
            )
            if owns:
                conn.execute("COMMIT")
        except BaseException:
            if owns:
                try:
                    conn.execute("ROLLBACK")
                except duckdb.Error:
                    pass
            raise
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
