from havn import macro


@macro
def mask_email(email: str) -> str:
    """Mask the local part of an email, keep domain."""
    if not email or "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    return f"***@{domain}"


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
