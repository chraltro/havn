from havn import macro


@macro
def normalize_country(code: str) -> str:
    """Normalize country codes to ISO 3166-1 alpha-2."""
    if not code:
        return "UNKNOWN"
    code = code.strip().upper()
    aliases = {
        "USA": "US", "UNITED STATES": "US", "AMERICA": "US",
        "UK": "GB", "UNITED KINGDOM": "GB", "ENGLAND": "GB",
        "DEUTSCHLAND": "DE", "GERMANY": "DE",
        "NORGE": "NO", "NORWAY": "NO",
        "SVERIGE": "SE", "SWEDEN": "SE",
        "DANMARK": "DK", "DENMARK": "DK",
    }
    return aliases.get(code, code)


@macro
def revenue_tier(amount: float) -> str:
    """Classify revenue into tiers."""
    if amount >= 10000:
        return "enterprise"
    elif amount >= 1000:
        return "mid-market"
    elif amount >= 100:
        return "smb"
    return "micro"


@macro
def churn_label(days_since_last: int) -> str:
    """Label churn risk based on days since last activity."""
    if days_since_last > 90:
        return "churned"
    elif days_since_last > 60:
        return "high-risk"
    elif days_since_last > 30:
        return "at-risk"
    return "active"


@macro
def fiscal_quarter(d: str) -> int:
    """Return fiscal quarter (FY starts April)."""
    from datetime import datetime
    month = datetime.fromisoformat(d).month
    return ((month - 4) % 12) // 3 + 1
