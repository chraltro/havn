"""Column-level data masking engine.

Provides post-query masking of sensitive columns based on policies stored
in ``_dp_internal.masking_policies``.  Masking is applied to result rows
*after* query execution, before returning to the client.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from datetime import datetime, timedelta
from typing import Any

import duckdb


# ---------------------------------------------------------------------------
# Table bootstrap
# ---------------------------------------------------------------------------


def ensure_masking_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the masking_policies table if it doesn't exist."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS _dp_internal")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _dp_internal.masking_policies (
            id               VARCHAR PRIMARY KEY DEFAULT gen_random_uuid()::VARCHAR,
            schema_name      VARCHAR NOT NULL,
            table_name       VARCHAR NOT NULL,
            column_name      VARCHAR NOT NULL,
            method           VARCHAR NOT NULL,
            method_config    JSON,
            condition_column VARCHAR,
            condition_value  VARCHAR,
            exempted_roles   JSON DEFAULT '["admin"]',
            created_at       TIMESTAMP DEFAULT current_timestamp
        )
    """)


# ---------------------------------------------------------------------------
# Masking functions
# ---------------------------------------------------------------------------


def mask_hash(value: Any) -> str:
    """SHA-256 hash, first 8 hex chars."""
    if value is None:
        return None
    return hashlib.sha256(str(value).encode()).hexdigest()[:8]


def mask_redact(value: Any) -> str:
    """Replace with ``***``."""
    if value is None:
        return None
    return "***"


def mask_null(value: Any) -> None:
    """Replace with None."""
    return None


def mask_partial(value: Any, show_first: int = 0, show_last: int = 0) -> str:
    """Show first/last N chars, mask the rest with ``*``."""
    if value is None:
        return None
    s = str(value)
    if show_first + show_last >= len(s):
        return s
    masked_len = len(s) - show_first - show_last
    return s[:show_first] + "*" * masked_len + s[len(s) - show_last:] if show_last else s[:show_first] + "*" * masked_len


def mask_email(value: Any) -> str:
    """Mask the local part of an email, keep domain for analytics.

    ``john.doe@company.com`` -> ``***@company.com``
    """
    if value is None:
        return None
    s = str(value)
    if "@" not in s:
        return "***"
    _, domain = s.rsplit("@", 1)
    return f"***@{domain}"


def mask_phone(value: Any, show_last: int = 4) -> str:
    """Keep last N digits of a phone number, mask the rest.

    ``+1-555-123-4567`` -> ``***-***-4567``
    """
    if value is None:
        return None
    s = str(value)
    digits = re.findall(r"\d", s)
    if len(digits) <= show_last:
        return s
    visible = "".join(digits[-show_last:])
    # Reconstruct with masked prefix
    non_digit_suffix = ""
    # Walk backwards to find separator before last digits
    result_parts = []
    digit_count = 0
    for ch in reversed(s):
        if ch.isdigit():
            digit_count += 1
            if digit_count <= show_last:
                result_parts.append(ch)
            else:
                result_parts.append("*")
        else:
            result_parts.append(ch)
    return "".join(reversed(result_parts))


def mask_credit_card(value: Any, show_last: int = 4) -> str:
    """PCI-DSS compliant: show last 4 digits only.

    ``4111111111111111`` -> ``************1111``
    """
    if value is None:
        return None
    s = re.sub(r"[^0-9]", "", str(value))
    if len(s) <= show_last:
        return "*" * show_last
    return "*" * (len(s) - show_last) + s[-show_last:]


def mask_first_initial(value: Any) -> str:
    """Reduce a name to initials.

    ``John Smith`` -> ``J. S.``
    """
    if value is None:
        return None
    parts = str(value).split()
    if not parts:
        return "***"
    return " ".join(f"{p[0].upper()}." for p in parts if p)


def mask_ip_address(value: Any, keep_octets: int = 2) -> str:
    """Mask host portion of an IPv4 address.

    ``192.168.1.42`` -> ``192.168.x.x``
    """
    if value is None:
        return None
    s = str(value)
    parts = s.split(".")
    if len(parts) != 4:
        return "x.x.x.x"
    keep_octets = max(0, min(keep_octets, 3))
    masked = parts[:keep_octets] + ["x"] * (4 - keep_octets)
    return ".".join(masked)


def mask_range(value: Any, bucket_size: int = 10000) -> str:
    """Bucket a numeric value into a range for distribution-safe analytics.

    ``47382`` with bucket_size=10000 -> ``40000-50000``
    """
    if value is None:
        return None
    try:
        num = float(value)
    except (ValueError, TypeError):
        return "***"
    lower = int(num // bucket_size) * bucket_size
    upper = lower + bucket_size
    return f"{lower}-{upper}"


def mask_noise(value: Any, percentage: float = 10.0, seed_key: str = "") -> Any:
    """Add random noise within +/- percentage. Useful for analytics that need
    approximate values without revealing exact figures.

    ``47382`` with 10% -> somewhere in ``42644-52120``
    """
    if value is None:
        return None
    try:
        num = float(value)
    except (ValueError, TypeError):
        return value
    # Deterministic per-value seed so same value always gets same noise
    seed = hashlib.sha256(f"{seed_key}{value}".encode()).digest()
    rng = random.Random(int.from_bytes(seed[:4], "big"))
    factor = 1.0 + rng.uniform(-percentage / 100.0, percentage / 100.0)
    result = num * factor
    # Preserve int type if input was int-like
    if isinstance(value, int) or (isinstance(value, str) and "." not in str(value)):
        return int(round(result))
    return round(result, 2)


def mask_date_shift(value: Any, max_days: int = 30, seed_key: str = "") -> Any:
    """Shift a date/datetime by a consistent random offset.

    Preserves relative ordering within the same seed context. Useful for
    time-series analytics where intervals matter but exact dates are sensitive.
    """
    if value is None:
        return None
    # Deterministic offset per value
    seed = hashlib.sha256(f"{seed_key}{value}".encode()).digest()
    rng = random.Random(int.from_bytes(seed[:4], "big"))
    offset_days = rng.randint(-max_days, max_days)

    if isinstance(value, datetime):
        return value + timedelta(days=offset_days)
    # Try parsing common date formats
    s = str(value)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            shifted = dt + timedelta(days=offset_days)
            return shifted.strftime(fmt)
        except ValueError:
            continue
    return s


def mask_truncate(value: Any, length: int = 3) -> str:
    """Show first N characters followed by ellipsis.

    ``John Smith`` with length=3 -> ``Joh...``
    """
    if value is None:
        return None
    s = str(value)
    if len(s) <= length:
        return s
    return s[:length] + "..."


def mask_consistent_hash(value: Any, prefix: str = "", length: int = 8) -> str:
    """Deterministic pseudonym -- same input always produces same output.

    Unlike ``hash``, this is designed for JOIN-safe masking: if two tables
    both have ``user_id`` masked with consistent_hash, the masked values
    will match and JOINs still work.

    ``john@example.com`` -> ``usr_a1b2c3d4`` (with prefix="usr_")
    """
    if value is None:
        return None
    h = hashlib.sha256(str(value).encode()).hexdigest()[:length]
    return f"{prefix}{h}"


_MASKING_FNS = {
    "hash": mask_hash,
    "redact": mask_redact,
    "null": mask_null,
    "partial": mask_partial,
    "email": mask_email,
    "phone": mask_phone,
    "credit_card": mask_credit_card,
    "first_initial": mask_first_initial,
    "ip_address": mask_ip_address,
    "range": mask_range,
    "noise": mask_noise,
    "date_shift": mask_date_shift,
    "truncate": mask_truncate,
    "consistent_hash": mask_consistent_hash,
}


def apply_mask(value: Any, method: str, method_config: dict | None = None) -> Any:
    """Apply a single masking method to a value."""
    fn = _MASKING_FNS.get(method)
    if fn is None:
        return value
    cfg = method_config or {}

    # Methods with configurable parameters
    _CONFIG_KEYS: dict[str, list[str]] = {
        "partial": ["show_first", "show_last"],
        "phone": ["show_last"],
        "credit_card": ["show_last"],
        "ip_address": ["keep_octets"],
        "range": ["bucket_size"],
        "noise": ["percentage", "seed_key"],
        "date_shift": ["max_days", "seed_key"],
        "truncate": ["length"],
        "consistent_hash": ["prefix", "length"],
    }

    if method in _CONFIG_KEYS and cfg:
        kwargs = {k: cfg[k] for k in _CONFIG_KEYS[method] if k in cfg}
        return fn(value, **kwargs)
    return fn(value)


# ---------------------------------------------------------------------------
# Policy matching & application
# ---------------------------------------------------------------------------


def _load_policies(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Load all masking policies from the database."""
    ensure_masking_table(conn)
    rows = conn.execute(
        "SELECT id, schema_name, table_name, column_name, method, method_config, "
        "condition_column, condition_value, exempted_roles "
        "FROM _dp_internal.masking_policies"
    ).fetchall()
    policies = []
    for r in rows:
        config = json.loads(r[5]) if r[5] else None
        exempted = json.loads(r[8]) if r[8] else ["admin"]
        policies.append({
            "id": r[0],
            "schema_name": r[1],
            "table_name": r[2],
            "column_name": r[3],
            "method": r[4],
            "method_config": config,
            "condition_column": r[6],
            "condition_value": r[7],
            "exempted_roles": exempted,
        })
    return policies


def apply_masking(
    columns: list[str],
    rows: list[list[Any]],
    user_role: str,
    conn: duckdb.DuckDBPyConnection,
    schema: str | None = None,
    table: str | None = None,
) -> list[list[Any]]:
    """Apply masking policies to query result rows.

    Parameters
    ----------
    columns : column names from the result set
    rows : list of row lists (mutable — will be modified in place)
    user_role : the requesting user's role
    conn : DuckDB connection for loading policies
    schema / table : when known (e.g. /sample), enables exact matching.
        When None (ad-hoc /query), does best-effort column-name matching.

    Returns the (possibly modified) rows.
    """
    policies = _load_policies(conn)
    if not policies:
        return rows

    # Build a column-index map
    col_idx = {c.lower(): i for i, c in enumerate(columns)}

    # Filter to relevant policies
    matched: list[tuple[dict, int]] = []  # (policy, column_index)
    for p in policies:
        # Check role exemption
        if user_role in p["exempted_roles"]:
            continue

        col_lower = p["column_name"].lower()
        if col_lower not in col_idx:
            continue

        # If schema/table known, require exact match
        if schema is not None and table is not None:
            if p["schema_name"].lower() != schema.lower() or p["table_name"].lower() != table.lower():
                continue
        # If no schema/table (ad-hoc query), match on column name alone

        matched.append((p, col_idx[col_lower]))

    if not matched:
        return rows

    # Apply masking
    for row in rows:
        for policy, idx in matched:
            # Check condition if present
            if policy["condition_column"]:
                cond_col_lower = policy["condition_column"].lower()
                if cond_col_lower in col_idx:
                    cond_idx = col_idx[cond_col_lower]
                    if str(row[cond_idx]) != policy["condition_value"]:
                        continue
                else:
                    # Condition column not in result — skip masking
                    continue

            row[idx] = apply_mask(row[idx], policy["method"], policy["method_config"])

    return rows


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------


def create_policy(
    conn: duckdb.DuckDBPyConnection,
    *,
    schema_name: str,
    table_name: str,
    column_name: str,
    method: str,
    method_config: dict | None = None,
    condition_column: str | None = None,
    condition_value: str | None = None,
    exempted_roles: list[str] | None = None,
) -> dict:
    """Insert a new masking policy and return it."""
    if method not in _MASKING_FNS:
        raise ValueError(f"Unknown masking method: {method!r}. Must be one of {list(_MASKING_FNS)}")

    ensure_masking_table(conn)
    exempted = exempted_roles if exempted_roles is not None else ["admin"]
    config_json = json.dumps(method_config) if method_config else None
    exempted_json = json.dumps(exempted)

    row = conn.execute(
        """
        INSERT INTO _dp_internal.masking_policies
            (schema_name, table_name, column_name, method, method_config,
             condition_column, condition_value, exempted_roles)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id, created_at
        """,
        [schema_name, table_name, column_name, method, config_json,
         condition_column, condition_value, exempted_json],
    ).fetchone()

    return {
        "id": row[0],
        "schema_name": schema_name,
        "table_name": table_name,
        "column_name": column_name,
        "method": method,
        "method_config": method_config,
        "condition_column": condition_column,
        "condition_value": condition_value,
        "exempted_roles": exempted,
        "created_at": str(row[1]),
    }


def list_policies(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Return all masking policies."""
    return _load_policies(conn)


def get_policy(conn: duckdb.DuckDBPyConnection, policy_id: str) -> dict | None:
    """Return a single policy by ID, or None."""
    ensure_masking_table(conn)
    row = conn.execute(
        "SELECT id, schema_name, table_name, column_name, method, method_config, "
        "condition_column, condition_value, exempted_roles, created_at "
        "FROM _dp_internal.masking_policies WHERE id = ?",
        [policy_id],
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "schema_name": row[1],
        "table_name": row[2],
        "column_name": row[3],
        "method": row[4],
        "method_config": json.loads(row[5]) if row[5] else None,
        "condition_column": row[6],
        "condition_value": row[7],
        "exempted_roles": json.loads(row[8]) if row[8] else ["admin"],
        "created_at": str(row[9]) if row[9] else None,
    }


def update_policy(conn: duckdb.DuckDBPyConnection, policy_id: str, **updates) -> dict | None:
    """Update fields of an existing policy. Returns updated policy or None."""
    existing = get_policy(conn, policy_id)
    if not existing:
        return None

    allowed = {"schema_name", "table_name", "column_name", "method", "method_config",
               "condition_column", "condition_value", "exempted_roles"}
    sets = []
    params = []
    for key, val in updates.items():
        if key not in allowed:
            continue
        if key == "method" and val not in _MASKING_FNS:
            raise ValueError(f"Unknown masking method: {val!r}")
        if key in ("method_config", "exempted_roles"):
            val = json.dumps(val) if val is not None else None
        sets.append(f"{key} = ?")
        params.append(val)

    if not sets:
        return existing

    params.append(policy_id)
    conn.execute(
        f"UPDATE _dp_internal.masking_policies SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    return get_policy(conn, policy_id)


def delete_policy(conn: duckdb.DuckDBPyConnection, policy_id: str) -> bool:
    """Delete a policy by ID. Returns True if deleted."""
    ensure_masking_table(conn)
    before = conn.execute("SELECT COUNT(*) FROM _dp_internal.masking_policies WHERE id = ?", [policy_id]).fetchone()[0]
    if not before:
        return False
    conn.execute("DELETE FROM _dp_internal.masking_policies WHERE id = ?", [policy_id])
    return True


# ---------------------------------------------------------------------------
# Method catalog (for UI / API discovery)
# ---------------------------------------------------------------------------

MASKING_METHODS: list[dict] = [
    {
        "id": "hash",
        "name": "SHA-256 Hash",
        "description": "One-way hash, first 8 hex chars. Irreversible.",
        "category": "general",
        "example": {"input": "john@example.com", "output": "a1b2c3d4"},
        "config": [],
    },
    {
        "id": "redact",
        "name": "Full Redact",
        "description": "Replace entire value with ***.",
        "category": "general",
        "example": {"input": "John Smith", "output": "***"},
        "config": [],
    },
    {
        "id": "null",
        "name": "Nullify",
        "description": "Replace with NULL. Use when the column should be completely hidden.",
        "category": "general",
        "example": {"input": "anything", "output": "NULL"},
        "config": [],
    },
    {
        "id": "partial",
        "name": "Partial Mask",
        "description": "Show first/last N characters, mask the middle.",
        "category": "general",
        "example": {"input": "John Smith", "output": "Jo******th"},
        "config": [
            {"key": "show_first", "label": "Show first N chars", "type": "int", "default": 0},
            {"key": "show_last", "label": "Show last N chars", "type": "int", "default": 0},
        ],
    },
    {
        "id": "email",
        "name": "Email Domain Only",
        "description": "Hide the local part, keep the domain. Good for analytics by email provider.",
        "category": "pii",
        "example": {"input": "john.doe@company.com", "output": "***@company.com"},
        "config": [],
    },
    {
        "id": "phone",
        "name": "Phone (Last 4)",
        "description": "Show last N digits of a phone number for verification use cases.",
        "category": "pii",
        "example": {"input": "+1-555-123-4567", "output": "**-***-***-4567"},
        "config": [
            {"key": "show_last", "label": "Visible digits", "type": "int", "default": 4},
        ],
    },
    {
        "id": "credit_card",
        "name": "Credit Card (PCI-DSS)",
        "description": "PCI-DSS compliant: mask all but last 4 digits.",
        "category": "financial",
        "example": {"input": "4111111111111111", "output": "************1111"},
        "config": [
            {"key": "show_last", "label": "Visible digits", "type": "int", "default": 4},
        ],
    },
    {
        "id": "first_initial",
        "name": "Name Initials",
        "description": "Reduce names to initials. Semi-anonymous but still groupable.",
        "category": "pii",
        "example": {"input": "John Smith", "output": "J. S."},
        "config": [],
    },
    {
        "id": "ip_address",
        "name": "IP Address Mask",
        "description": "Mask host octets of IPv4, keep network prefix for geo-analytics.",
        "category": "pii",
        "example": {"input": "192.168.1.42", "output": "192.168.x.x"},
        "config": [
            {"key": "keep_octets", "label": "Visible octets (0-3)", "type": "int", "default": 2},
        ],
    },
    {
        "id": "range",
        "name": "Numeric Range",
        "description": "Bucket values into ranges. Preserves distribution for analytics.",
        "category": "analytics",
        "example": {"input": "47382", "output": "40000-50000"},
        "config": [
            {"key": "bucket_size", "label": "Bucket size", "type": "int", "default": 10000},
        ],
    },
    {
        "id": "noise",
        "name": "Numeric Noise",
        "description": "Add deterministic random noise within +/- percentage. Approximate but not exact.",
        "category": "analytics",
        "example": {"input": "47382", "output": "~45200"},
        "config": [
            {"key": "percentage", "label": "Noise % (+/-)", "type": "float", "default": 10.0},
            {"key": "seed_key", "label": "Seed (for consistency)", "type": "string", "default": ""},
        ],
    },
    {
        "id": "date_shift",
        "name": "Date Shift",
        "description": "Shift dates by a consistent random offset. Preserves intervals for time-series.",
        "category": "analytics",
        "example": {"input": "2024-03-15", "output": "2024-03-28"},
        "config": [
            {"key": "max_days", "label": "Max shift (days)", "type": "int", "default": 30},
            {"key": "seed_key", "label": "Seed (for consistency)", "type": "string", "default": ""},
        ],
    },
    {
        "id": "truncate",
        "name": "Truncate",
        "description": "Show first N characters with ellipsis. Simple partial visibility.",
        "category": "general",
        "example": {"input": "John Smith", "output": "Joh..."},
        "config": [
            {"key": "length", "label": "Visible chars", "type": "int", "default": 3},
        ],
    },
    {
        "id": "consistent_hash",
        "name": "Pseudonym (JOIN-safe)",
        "description": "Deterministic pseudonym: same input always maps to same output. JOINs still work across masked tables.",
        "category": "analytics",
        "example": {"input": "john@example.com", "output": "usr_a1b2c3d4"},
        "config": [
            {"key": "prefix", "label": "Prefix", "type": "string", "default": ""},
            {"key": "length", "label": "Hash length", "type": "int", "default": 8},
        ],
    },
]


def list_masking_methods() -> list[dict]:
    """Return the catalog of available masking methods with config schemas."""
    return MASKING_METHODS
