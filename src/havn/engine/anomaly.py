"""Anomaly detection via statistical profiling.

Tracks profile metrics over N runs and alerts when current values deviate
significantly from historical baselines using Z-score analysis.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any

import duckdb

from havn.engine.transform.models import ProfileResult

logger = logging.getLogger("havn.anomaly")

# Default settings
DEFAULT_LOOKBACK = 30
DEFAULT_THRESHOLD = 2.0
MIN_HISTORY = 3


@dataclass
class AnomalyResult:
    """A single anomaly detection result for one metric of one model."""

    model: str
    metric: str  # "row_count", "null_percentage", "distinct_count"
    current_value: float
    mean: float
    stddev: float
    z_score: float
    is_anomaly: bool
    direction: str  # "increase" or "decrease"
    message: str  # Human-readable summary


def _mean(values: list[float]) -> float:
    """Compute arithmetic mean."""
    return sum(values) / len(values)


def _stddev(values: list[float], mean_val: float) -> float:
    """Compute population standard deviation."""
    if len(values) < 2:
        return 0.0
    variance = sum((x - mean_val) ** 2 for x in values) / len(values)
    return math.sqrt(variance)


def _format_number(val: float) -> str:
    """Format a number for human-readable display."""
    if val >= 1_000_000:
        return f"{val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"{val / 1_000:.1f}K"
    if val == int(val):
        return f"{int(val)}"
    return f"{val:.1f}"


def _pct_change(current: float, mean_val: float) -> str:
    """Format percentage change for messages."""
    if mean_val == 0:
        return "from 0"
    pct = abs((current - mean_val) / mean_val) * 100
    direction = "up" if current > mean_val else "down"
    return f"{direction} {pct:.0f}%"


def _check_metric(
    model: str,
    metric: str,
    current: float,
    history: list[float],
    threshold: float,
) -> AnomalyResult | None:
    """Check a single metric against its historical values.

    Returns an AnomalyResult if the check could be performed, or None if
    the metric should be skipped (not enough data, zero stddev, etc.).
    """
    # Filter out None values
    valid = [v for v in history if v is not None]

    if len(valid) < MIN_HISTORY:
        return None

    mean_val = _mean(valid)
    std_val = _stddev(valid, mean_val)

    if std_val == 0.0:
        return None

    z = (current - mean_val) / std_val
    is_anomaly = abs(z) > threshold
    direction = "increase" if z > 0 else "decrease"

    # Build human-readable message
    change = _pct_change(current, mean_val)
    msg = (
        f"{metric} {change} ({_format_number(current)} vs mean {_format_number(mean_val)})"
    )

    return AnomalyResult(
        model=model,
        metric=metric,
        current_value=current,
        mean=mean_val,
        stddev=std_val,
        z_score=round(z, 2),
        is_anomaly=is_anomaly,
        direction=direction,
        message=msg,
    )


def _get_profile_history(
    conn: duckdb.DuckDBPyConnection,
    model_name: str,
    lookback: int,
) -> list[dict[str, Any]]:
    """Fetch the last N profile snapshots from profile_history."""
    try:
        rows = conn.execute(
            """
            SELECT row_count, null_percentages, distinct_counts
            FROM _havn.profile_history
            WHERE model_path = ?
            ORDER BY profiled_at DESC
            LIMIT ?
            """,
            [model_name, lookback],
        ).fetchall()
    except Exception:
        return []

    result = []
    for row in rows:
        null_pcts = {}
        distinct_cts = {}
        try:
            null_pcts = json.loads(row[1]) if row[1] else {}
        except (json.JSONDecodeError, TypeError):
            pass
        try:
            distinct_cts = json.loads(row[2]) if row[2] else {}
        except (json.JSONDecodeError, TypeError):
            pass
        result.append({
            "row_count": row[0],
            "null_percentages": null_pcts,
            "distinct_counts": distinct_cts,
        })
    return result


def detect_anomalies(
    conn: duckdb.DuckDBPyConnection,
    model_name: str,
    current_profile: ProfileResult,
    lookback: int = DEFAULT_LOOKBACK,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[AnomalyResult]:
    """Check current profile against historical for anomalies.

    Args:
        conn: DuckDB connection.
        model_name: Fully qualified model name (e.g. "bronze.customers").
        current_profile: The freshly computed profile.
        lookback: Number of historical profiles to consider.
        threshold: Z-score threshold for anomaly flagging.

    Returns:
        List of AnomalyResult for metrics that were checked.
        Only includes results where sufficient data exists.
    """
    history = _get_profile_history(conn, model_name, lookback)

    if len(history) < MIN_HISTORY:
        return []

    results: list[AnomalyResult] = []

    # 1. Row count
    row_counts = [h["row_count"] for h in history if h["row_count"] is not None]
    rc = _check_metric(model_name, "row_count", float(current_profile.row_count), [float(x) for x in row_counts], threshold)
    if rc is not None:
        results.append(rc)

    # 2. Average null percentage across all columns
    if current_profile.null_percentages:
        current_avg_null = sum(current_profile.null_percentages.values()) / len(current_profile.null_percentages) if current_profile.null_percentages else 0.0
        hist_avg_nulls = []
        for h in history:
            pcts = h.get("null_percentages", {})
            if pcts:
                hist_avg_nulls.append(sum(pcts.values()) / len(pcts))
        nc = _check_metric(model_name, "null_percentage", current_avg_null, hist_avg_nulls, threshold)
        if nc is not None:
            results.append(nc)

    # 3. Average distinct count across all columns
    if current_profile.distinct_counts:
        current_avg_distinct = sum(current_profile.distinct_counts.values()) / len(current_profile.distinct_counts) if current_profile.distinct_counts else 0.0
        hist_avg_distincts = []
        for h in history:
            cts = h.get("distinct_counts", {})
            if cts:
                hist_avg_distincts.append(sum(cts.values()) / len(cts))
        dc = _check_metric(model_name, "distinct_count", current_avg_distinct, [float(x) for x in hist_avg_distincts], threshold)
        if dc is not None:
            results.append(dc)

    return results


def detect_all_anomalies(
    conn: duckdb.DuckDBPyConnection,
    profiles: dict[str, ProfileResult],
    lookback: int = DEFAULT_LOOKBACK,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[AnomalyResult]:
    """Check all models after a transform run.

    Args:
        conn: DuckDB connection.
        profiles: Dict of model_name -> ProfileResult from the current run.
        lookback: Number of historical profiles to consider.
        threshold: Z-score threshold for anomaly flagging.

    Returns:
        List of all anomalies detected across all models.
    """
    all_anomalies: list[AnomalyResult] = []
    for model_name, profile in profiles.items():
        anomalies = detect_anomalies(conn, model_name, profile, lookback, threshold)
        all_anomalies.extend(a for a in anomalies if a.is_anomaly)
    return all_anomalies


def log_anomalies(
    conn: duckdb.DuckDBPyConnection,
    anomalies: list[AnomalyResult],
) -> None:
    """Save detected anomalies to the anomaly_log table."""
    for a in anomalies:
        try:
            conn.execute(
                """
                INSERT INTO _havn.anomaly_log
                    (model_name, metric, current_value, mean_value, stddev_value,
                     z_score, direction, message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    a.model,
                    a.metric,
                    a.current_value,
                    a.mean,
                    a.stddev,
                    a.z_score,
                    a.direction,
                    a.message,
                ],
            )
        except Exception as e:
            logger.warning("Failed to log anomaly for %s: %s", a.model, e)


def alert_anomalies(
    anomalies: list[AnomalyResult],
    alert_config: Any,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> None:
    """Send alerts for detected anomalies via the alerting system."""
    if not anomalies:
        return

    from havn.engine.alerts import Alert, AlertConfig, send_alert

    # Build AlertConfig from project config
    config = AlertConfig(
        slack_webhook_url=getattr(alert_config, "slack_webhook_url", None),
        webhook_url=getattr(alert_config, "webhook_url", None),
        channels=getattr(alert_config, "channels", []),
    )

    if not config.channels and not config.slack_webhook_url and not config.webhook_url:
        return

    # Group anomalies by model for compact alerts
    by_model: dict[str, list[AnomalyResult]] = {}
    for a in anomalies:
        by_model.setdefault(a.model, []).append(a)

    for model, model_anomalies in by_model.items():
        detail_lines = [a.message for a in model_anomalies]
        alert = Alert(
            alert_type="anomaly",
            target=model,
            message=f"Anomaly detected in `{model}`: {'; '.join(detail_lines)}",
            details={
                "model": model,
                "anomaly_count": len(model_anomalies),
                "metrics": ", ".join(a.metric for a in model_anomalies),
            },
        )
        send_alert(alert, config, conn)
