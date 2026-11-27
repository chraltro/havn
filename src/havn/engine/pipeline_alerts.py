"""Pipeline success/failure alerts driven by the ``alerts:`` block of project.yml.

Shared by the stream runner (``havn stream`` and scheduled streams) and by
orchestration jobs, so an unattended run alerts the same way a manual one
does. Delivery never raises: an unreachable Slack hook must not turn a
successful run into a failed one, or hide the real error of a failed one.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("havn.alerts")


def alerts_configured(alerts) -> bool:
    """True when ``alerts`` names somewhere to deliver to.

    A URL (Slack or generic webhook) or an explicit ``channels`` list,
    which is how the URL-less ``log`` channel is enabled.
    """
    if alerts is None:
        return False
    return bool(
        getattr(alerts, "slack_webhook_url", None)
        or getattr(alerts, "webhook_url", None)
        or getattr(alerts, "channels", None)
    )


def send_pipeline_alert(
    alerts,
    target: str,
    *,
    ok: bool,
    duration_s: float,
    conn=None,
    error: str | None = None,
    models_built: int = 0,
) -> list[dict]:
    """Send a ``pipeline_success`` or ``pipeline_failure`` alert for ``target``.

    ``alerts`` is the project's ``AlertsConfig``; ``on_failure`` and
    ``on_success`` decide whether anything is sent. Returns the per-channel
    results, or ``[]`` when nothing was sent or delivery itself blew up.
    """
    if not alerts_configured(alerts):
        return []
    if ok and not getattr(alerts, "on_success", True):
        return []
    if not ok and not getattr(alerts, "on_failure", True):
        return []
    try:
        from havn.engine.alerts import (
            AlertConfig,
            alert_pipeline_failure,
            alert_pipeline_success,
        )

        cfg = AlertConfig(
            slack_webhook_url=alerts.slack_webhook_url,
            webhook_url=alerts.webhook_url,
            # A copy: send_alert appends auto-detected channels to the list.
            channels=list(alerts.channels or []),
        )
        if ok:
            return alert_pipeline_success(target, duration_s, cfg, conn, models_built=models_built)
        return alert_pipeline_failure(target, duration_s, error or "failed", cfg, conn)
    except Exception as exc:  # never fail a run over its notification
        logger.warning("Pipeline alert for %s could not be sent: %s", target, exc)
        return []
