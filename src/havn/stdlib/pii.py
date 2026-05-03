"""PII masking macros — auto-registered as DuckDB scalar UDFs for every project.

Every customer-360 / compliance-adjacent project re-implements roughly
the same handful of masking helpers; we ship them here so they're a
zero-config call in SQL::

    SELECT mask_email(email), mask_phone(phone), mask_fnr(national_id)
    FROM bronze.customers

User macros in ``project_dir/macros/`` with the same name still win,
so projects with stricter formatting requirements can override.
"""

from __future__ import annotations

import hashlib

from havn import macro


@macro
def mask_email(email: str) -> str:
    """Mask the local part of an email, keep the domain.

    ``alice@example.com`` -> ``***@example.com``. Inputs without an
    ``@`` collapse to ``***``. NULL passes through.
    """
    if email is None:
        return None  # type: ignore[return-value]
    if "@" not in email:
        return "***"
    _, _, domain = email.partition("@")
    return f"***@{domain}"


@macro
def mask_phone(phone: str) -> str:
    """Mask all but the last four digits of a phone number.

    ``+47 99 88 77 66`` -> ``+47 ** ** ** 66``. Non-digit characters
    are preserved so the result keeps the shape of the original.
    """
    if phone is None:
        return None  # type: ignore[return-value]
    digits = [c for c in phone if c.isdigit()]
    if len(digits) <= 4:
        return phone
    keep = "".join(digits[-4:])
    out: list[str] = []
    seen = 0
    total = len(digits)
    for ch in phone:
        if ch.isdigit():
            if seen >= total - 4:
                out.append(ch)
            else:
                out.append("*")
            seen += 1
        else:
            out.append(ch)
    _ = keep  # used for readability; we mask in-place above
    return "".join(out)


@macro
def mask_fnr(fnr: str) -> str:
    """Mask a Norwegian ``fødselsnummer`` (or any national ID).

    Keeps the first 4 characters (date of birth) and masks the rest::

        29128512345 -> 2912*******

    Empty / NULL passes through unchanged.
    """
    if fnr is None:
        return None  # type: ignore[return-value]
    s = str(fnr)
    if len(s) <= 4:
        return "*" * len(s)
    return s[:4] + "*" * (len(s) - 4)


@macro
def mask_credit_card(cc: str) -> str:
    """Mask all but the last four digits of a credit card number.

    ``4242 4242 4242 4242`` -> ``**** **** **** 4242``. Non-digits are
    preserved.
    """
    if cc is None:
        return None  # type: ignore[return-value]
    digits = [c for c in cc if c.isdigit()]
    if len(digits) <= 4:
        return cc
    keep_from = len(digits) - 4
    out: list[str] = []
    seen = 0
    for ch in cc:
        if ch.isdigit():
            out.append(ch if seen >= keep_from else "*")
            seen += 1
        else:
            out.append(ch)
    return "".join(out)


@macro
def mask_ip(ip: str) -> str:
    """Mask the last octet of an IPv4 address.

    ``192.168.1.42`` -> ``192.168.1.0``. IPv6 collapses to ``::``.
    Anything else passes through.
    """
    if ip is None:
        return None  # type: ignore[return-value]
    if ":" in ip:
        return "::"
    parts = ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:3]) + ".0"
    return ip


@macro
def hash_consistent(value: str, salt: str) -> str:
    """SHA-256 of ``value`` + ``salt``, hex-truncated to 16 chars.

    Useful for replacing PII in an analytics-friendly way: the same
    input always yields the same opaque key, so joins across tables
    still work without exposing the original value. Same value + same
    salt = same key; rotate the salt to invalidate.

    Pass the salt explicitly — havn's macro registration doesn't
    propagate Python default values to DuckDB.
    """
    if value is None:
        return None  # type: ignore[return-value]
    return hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()[:16]
