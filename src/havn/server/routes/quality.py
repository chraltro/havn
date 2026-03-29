"""Data quality endpoints: profiles, assertions, freshness, alerts, and contracts."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from havn.server.deps import (
    DbConn,
    DbConnReadOnly,
    DbConnReadOnlyOptional,
    _get_config,
    _get_project_dir,
    _require_permission,
    ensure_meta_table,
)

router = APIRouter()


# --- Pydantic models ---


class TestAlertRequest(BaseModel):
    channel: str = Field(..., pattern=r"^(slack|webhook|log)$")
    slack_webhook_url: str | None = None
    webhook_url: str | None = None


class AnomalyConfigUpdate(BaseModel):
    enabled: bool | None = None
    lookback: int | None = Field(None, ge=3, le=1000)
    threshold: float | None = Field(None, gt=0.0, le=10.0)


# --- Freshness ---


@router.get("/api/freshness")
def get_freshness(
    request: Request, conn: DbConnReadOnly, max_hours: float = 24.0
) -> list[dict]:
    """Check model freshness: which models are stale?"""
    _require_permission(request, "read")
    from havn.engine.transform import check_freshness

    ensure_meta_table(conn)
    return check_freshness(conn, max_age_hours=max_hours)


# --- Model profiles ---


@router.get("/api/profiles")
def get_profiles(request: Request, conn: DbConnReadOnly) -> list[dict]:
    """Get auto-computed profile stats for all models."""
    _require_permission(request, "read")
    ensure_meta_table(conn)
    rows = conn.execute(
        "SELECT model_path, row_count, column_count, null_percentages, distinct_counts, profiled_at "
        "FROM _havn.model_profiles ORDER BY model_path"
    ).fetchall()
    return [
        {
            "model": r[0],
            "row_count": r[1],
            "column_count": r[2],
            "null_percentages": json.loads(r[3]) if r[3] else {},
            "distinct_counts": json.loads(r[4]) if r[4] else {},
            "profiled_at": str(r[5]) if r[5] else None,
        }
        for r in rows
    ]


@router.get("/api/profiles/{model_name}")
def get_profile(
    request: Request, model_name: str, conn: DbConnReadOnly
) -> dict:
    """Get profile stats for a specific model."""
    _require_permission(request, "read")
    ensure_meta_table(conn)
    row = conn.execute(
        "SELECT model_path, row_count, column_count, null_percentages, distinct_counts, profiled_at "
        "FROM _havn.model_profiles WHERE model_path = ?",
        [model_name],
    ).fetchone()
    if not row:
        raise HTTPException(
            404, f"No profile for '{model_name}'. Run havn transform first."
        )
    return {
        "model": row[0],
        "row_count": row[1],
        "column_count": row[2],
        "null_percentages": json.loads(row[3]) if row[3] else {},
        "distinct_counts": json.loads(row[4]) if row[4] else {},
        "profiled_at": str(row[5]) if row[5] else None,
    }


# --- Assertions ---


@router.get("/api/assertions")
def get_assertions(
    request: Request, conn: DbConnReadOnly, limit: int = 100
) -> list[dict]:
    """Get recent data quality assertion results."""
    _require_permission(request, "read")
    ensure_meta_table(conn)
    rows = conn.execute(
        """
        SELECT model_path, expression, passed, detail, checked_at
        FROM _havn.assertion_results
        ORDER BY checked_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    return [
        {
            "model": r[0],
            "expression": r[1],
            "passed": r[2],
            "detail": r[3],
            "checked_at": str(r[4]) if r[4] else None,
        }
        for r in rows
    ]


@router.get("/api/assertions/{model_name}")
def get_model_assertions(
    request: Request, model_name: str, conn: DbConnReadOnly
) -> list[dict]:
    """Get assertion results for a specific model."""
    _require_permission(request, "read")
    ensure_meta_table(conn)
    rows = conn.execute(
        """
        SELECT model_path, expression, passed, detail, checked_at
        FROM _havn.assertion_results
        WHERE model_path = ?
        ORDER BY checked_at DESC
        LIMIT 50
        """,
        [model_name],
    ).fetchall()
    return [
        {
            "model": r[0],
            "expression": r[1],
            "passed": r[2],
            "detail": r[3],
            "checked_at": str(r[4]) if r[4] else None,
        }
        for r in rows
    ]


# --- Alerts ---


@router.get("/api/alerts")
def get_alert_history(
    request: Request, conn: DbConnReadOnly, limit: int = 50
) -> list[dict]:
    """Get alert history."""
    _require_permission(request, "read")
    ensure_meta_table(conn)
    rows = conn.execute(
        """
        SELECT alert_type, channel, target, message, status, sent_at, error
        FROM _havn.alert_log
        ORDER BY sent_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    return [
        {
            "alert_type": r[0],
            "channel": r[1],
            "target": r[2],
            "message": r[3],
            "status": r[4],
            "sent_at": str(r[5]) if r[5] else None,
            "error": r[6],
        }
        for r in rows
    ]


@router.post("/api/alerts/test")
def test_alert(request: Request, req: TestAlertRequest) -> dict:
    """Send a test alert to verify configuration."""
    _require_permission(request, "execute")
    from havn.engine.alerts import Alert, AlertConfig, send_alert

    config = AlertConfig(
        slack_webhook_url=req.slack_webhook_url,
        webhook_url=req.webhook_url,
        channels=[req.channel],
    )
    alert = Alert(
        alert_type="test",
        target="havn_test",
        message="This is a test alert from havn. If you see this, alerts are working!",
        details={"source": "havn alerts test"},
    )
    results = send_alert(alert, config)
    if results and results[0].get("status") == "sent":
        return {"status": "sent", "channel": req.channel}
    error = (
        results[0].get("error", "Unknown error")
        if results
        else "No channels configured"
    )
    raise HTTPException(400, f"Alert test failed: {error}")


# --- Data Contracts ---


@router.get("/api/contracts")
def list_contracts(request: Request) -> list[dict]:
    """List all discovered contracts."""
    _require_permission(request, "read")
    from havn.engine.contracts import discover_contracts

    contracts_dir = _get_project_dir() / "contracts"
    contracts = discover_contracts(contracts_dir)
    return [
        {
            "name": c.name,
            "model": c.model,
            "description": c.description,
            "severity": c.severity,
            "assertions": c.assertions,
            "notify": c.notify,
            "escalate_after": c.escalate_after,
            "path": str(c.path) if c.path else None,
        }
        for c in contracts
    ]


@router.post("/api/contracts/run")
def run_contracts_endpoint(request: Request, conn: DbConn) -> dict:
    """Run all data contracts and return results."""
    _require_permission(request, "read")
    from havn.engine.contracts import run_contracts

    contracts_dir = _get_project_dir() / "contracts"
    results = run_contracts(conn, contracts_dir)
    return {
        "total": len(results),
        "passed": sum(1 for r in results if r.passed),
        "failed": sum(1 for r in results if not r.passed),
        "results": [
            {
                "contract_name": r.contract_name,
                "model": r.model,
                "passed": r.passed,
                "severity": r.severity,
                "duration_ms": r.duration_ms,
                "error": r.error,
                "assertions": r.results,
                "consecutive_failures": r.consecutive_failures,
            }
            for r in results
        ],
    }


@router.get("/api/contracts/history")
def get_contracts_history(
    request: Request, conn: DbConnReadOnly
) -> list[dict]:
    """Get recent contract evaluation history."""
    _require_permission(request, "read")
    from havn.engine.contracts import get_contract_history

    return get_contract_history(conn, limit=100)


@router.get("/api/contracts/{model_name}/history")
def get_contract_model_history(
    request: Request, model_name: str, conn: DbConnReadOnly
) -> list[dict]:
    """Get contract evaluation history for a specific model with trends."""
    _require_permission(request, "read")
    from havn.engine.contracts import get_contract_model_history

    return get_contract_model_history(conn, model_name, limit=50)


# --- Anomaly Detection ---


@router.get("/api/anomalies")
def get_anomalies(
    request: Request,
    conn: DbConnReadOnly,
    limit: int = 100,
    model: str | None = None,
) -> list[dict]:
    """List recent anomalies, optionally filtered by model."""
    _require_permission(request, "read")
    ensure_meta_table(conn)
    if model:
        rows = conn.execute(
            """
            SELECT model_name, metric, current_value, mean_value, stddev_value,
                   z_score, direction, message, detected_at
            FROM _havn.anomaly_log
            WHERE model_name = ?
            ORDER BY detected_at DESC
            LIMIT ?
            """,
            [model, limit],
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT model_name, metric, current_value, mean_value, stddev_value,
                   z_score, direction, message, detected_at
            FROM _havn.anomaly_log
            ORDER BY detected_at DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
    return [
        {
            "model": r[0],
            "metric": r[1],
            "current_value": r[2],
            "mean_value": r[3],
            "stddev_value": r[4],
            "z_score": r[5],
            "direction": r[6],
            "message": r[7],
            "detected_at": str(r[8]) if r[8] else None,
        }
        for r in rows
    ]


@router.get("/api/anomalies/config")
def get_anomaly_config(request: Request) -> dict:
    """Get current anomaly detection configuration."""
    _require_permission(request, "read")
    config = _get_config()
    ad = config.quality.anomaly_detection
    return {
        "enabled": ad.enabled,
        "lookback": ad.lookback,
        "threshold": ad.threshold,
        "notify": ad.notify,
    }


@router.put("/api/anomalies/config")
def update_anomaly_config(request: Request, req: AnomalyConfigUpdate) -> dict:
    """Update anomaly detection settings in project.yml."""
    _require_permission(request, "write")
    import yaml

    project_dir = _get_project_dir()
    config_path = project_dir / "project.yml"
    if not config_path.exists():
        raise HTTPException(404, "project.yml not found")

    raw = yaml.safe_load(config_path.read_text()) or {}
    quality = raw.setdefault("quality", {})
    anomaly = quality.setdefault("anomaly_detection", {})

    if req.enabled is not None:
        anomaly["enabled"] = req.enabled
    if req.lookback is not None:
        anomaly["lookback"] = req.lookback
    if req.threshold is not None:
        anomaly["threshold"] = req.threshold

    config_path.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))

    return {
        "status": "updated",
        "enabled": anomaly.get("enabled", True),
        "lookback": anomaly.get("lookback", 30),
        "threshold": anomaly.get("threshold", 2.0),
    }


@router.get("/api/anomalies/{model_name}")
def get_model_anomalies(
    request: Request, model_name: str, conn: DbConnReadOnly, limit: int = 50
) -> list[dict]:
    """Get anomalies for a specific model."""
    _require_permission(request, "read")
    ensure_meta_table(conn)
    rows = conn.execute(
        """
        SELECT model_name, metric, current_value, mean_value, stddev_value,
               z_score, direction, message, detected_at
        FROM _havn.anomaly_log
        WHERE model_name = ?
        ORDER BY detected_at DESC
        LIMIT ?
        """,
        [model_name, limit],
    ).fetchall()
    return [
        {
            "model": r[0],
            "metric": r[1],
            "current_value": r[2],
            "mean_value": r[3],
            "stddev_value": r[4],
            "z_score": r[5],
            "direction": r[6],
            "message": r[7],
            "detected_at": str(r[8]) if r[8] else None,
        }
        for r in rows
    ]


@router.get("/api/anomalies/{model_name}/history")
def get_model_profile_history(
    request: Request, model_name: str, conn: DbConnReadOnly, limit: int = 30
) -> list[dict]:
    """Get profile history for a model (for sparkline/trend visualization)."""
    _require_permission(request, "read")
    ensure_meta_table(conn)
    rows = conn.execute(
        """
        SELECT row_count, null_percentages, distinct_counts, profiled_at
        FROM _havn.profile_history
        WHERE model_path = ?
        ORDER BY profiled_at ASC
        LIMIT ?
        """,
        [model_name, limit],
    ).fetchall()
    return [
        {
            "row_count": r[0],
            "null_percentages": json.loads(r[1]) if r[1] else {},
            "distinct_counts": json.loads(r[2]) if r[2] else {},
            "profiled_at": str(r[3]) if r[3] else None,
        }
        for r in rows
    ]
