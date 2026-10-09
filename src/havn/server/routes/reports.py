"""Scheduled reports: CRUD, send now, preview and delivery history.

Editors and admins create reports; a report runs as its owner (the creator)
through the governed read path. Only the owner or an admin can change,
send, preview or delete a report, because each of those shows or sends data
as the owner.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from havn.server.deps import DbConn, _get_config, _require_permission

logger = logging.getLogger("havn.server")

router = APIRouter()


class ReportBody(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    dashboard_id: str | None = Field(default=None, max_length=100)
    widget_id: str | None = Field(default=None, max_length=100)
    schedule: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None
    recipients: dict | None = None
    formats: list[str] | None = Field(default=None, max_length=5)
    filters: dict | None = None
    parameters: dict | None = None
    condition: dict | None = None
    subject: str | None = Field(default=None, max_length=300)
    message: str | None = Field(default=None, max_length=4000)


def _err(e) -> HTTPException:
    return HTTPException(getattr(e, "status_code", 400), str(e))


def _audit(conn, request: Request, user: dict, action: str, resource: str, detail: str = "") -> None:
    try:
        from havn.engine.audit import log_audit

        log_audit(
            conn, user=user.get("username", "anonymous"), action=action, resource=resource,
            detail=detail, ip_address=request.client.host if request.client else None,
        )
    except Exception:
        logger.debug("Audit write failed for %s", action, exc_info=True)


def _decorate(conn, report: dict) -> dict:
    from havn.engine.cron import next_cron_fire
    from havn.engine.reports import public_view

    out = public_view(report)
    try:
        row = conn.execute("SELECT name FROM _havn.dashboards WHERE id = ?", [report["dashboard_id"]]).fetchone()
        out["dashboard_name"] = row[0] if row else None
    except Exception:
        out["dashboard_name"] = None
    nxt = None
    if report.get("enabled") and report.get("schedule"):
        try:
            nxt = next_cron_fire(report["schedule"])
        except Exception:
            nxt = None
    out["next_run_at"] = nxt.isoformat() if nxt else None
    return out


def _load_for_owner(request: Request, conn, report_id: str) -> tuple[dict, dict]:
    from havn.engine.reports import get_report

    user = _require_permission(request, "write")
    report = get_report(conn, report_id)
    if report is None:
        raise HTTPException(404, "Report not found")
    if user.get("role") != "admin" and report["owner"] != user.get("username"):
        raise HTTPException(403, "Only the report's owner or an admin can do this")
    return report, user


def _keep_masked_slack(new: dict | None, current: dict) -> dict | None:
    """Map Slack targets the client echoed back masked (``https://host/…``) to the stored URL."""
    if not new or "slack" not in new:
        return new
    from havn.engine.reports import _mask_slack_target

    stored = {_mask_slack_target(t): t for t in (current.get("recipients") or {}).get("slack") or []}
    out = dict(new)
    out["slack"] = [stored.get(t, t) if str(t).endswith("…") else t for t in new.get("slack") or []]
    return out


@router.get("/api/reports/capabilities")
def report_capabilities(request: Request) -> dict:
    """What delivery and rendering the server can do, for the report form."""
    _require_permission(request, "write")
    from havn.engine.report_render import charts_available

    cfg = _get_config()
    r = cfg.reports
    return {
        "charts": charts_available(r.charts),
        "email_configured": bool(r.smtp.host),
        "slack_default_configured": bool(r.slack_webhook_url or cfg.alerts.slack_webhook_url),
        "base_url": r.base_url,
        "allowed_recipient_domains": r.allowed_recipient_domains,
        "formats": ["pdf", "png", "csv"],
    }


@router.get("/api/reports")
def list_reports_endpoint(request: Request, conn: DbConn) -> list[dict]:
    _require_permission(request, "write")
    from havn.engine.reports import list_reports

    return [_decorate(conn, r) for r in list_reports(conn)]


@router.post("/api/reports")
def create_report_endpoint(request: Request, req: ReportBody, conn: DbConn) -> dict:
    user = _require_permission(request, "write")
    from havn.engine.reports import ReportError, create_report

    if not req.name or not req.dashboard_id:
        raise HTTPException(400, "name and dashboard_id are required")
    fields = req.model_dump(exclude_none=True)
    try:
        report = create_report(conn, fields, owner=user.get("username", "anonymous"), config=_get_config())
    except ReportError as e:
        raise _err(e)
    _audit(conn, request, user, "report_create", f"report={report['name']}",
           f"dashboard={report['dashboard_id']} schedule={report['schedule'] or 'manual'}")
    return _decorate(conn, report)


@router.get("/api/reports/{report_id}")
def get_report_endpoint(request: Request, report_id: str, conn: DbConn) -> dict:
    _require_permission(request, "write")
    from havn.engine.reports import get_report, list_deliveries

    report = get_report(conn, report_id)
    if report is None:
        raise HTTPException(404, "Report not found")
    out = _decorate(conn, report)
    out["deliveries"] = list_deliveries(conn, report_id)
    return out


@router.put("/api/reports/{report_id}")
def update_report_endpoint(request: Request, report_id: str, req: ReportBody, conn: DbConn) -> dict:
    report, user = _load_for_owner(request, conn, report_id)
    from havn.engine.reports import ReportError, update_report

    changes = req.model_dump(exclude_unset=True)
    if "recipients" in changes:
        changes["recipients"] = _keep_masked_slack(changes["recipients"], report)
    try:
        updated = update_report(conn, report_id, changes, config=_get_config())
    except ReportError as e:
        raise _err(e)
    _audit(conn, request, user, "report_update", f"report={updated['name']}",
           f"changed: {', '.join(sorted(changes)) or 'nothing'}")
    return _decorate(conn, updated)


@router.delete("/api/reports/{report_id}")
def delete_report_endpoint(request: Request, report_id: str, conn: DbConn) -> dict:
    report, user = _load_for_owner(request, conn, report_id)
    from havn.engine.reports import delete_report

    delete_report(conn, report_id)
    _audit(conn, request, user, "report_delete", f"report={report['name']}")
    return {"status": "deleted", "id": report_id}


@router.post("/api/reports/{report_id}/send")
def send_report_endpoint(request: Request, report_id: str, conn: DbConn, force: bool = False) -> dict:
    """Send the report now. ``force`` sends even when its condition is not met."""
    report, _user = _load_for_owner(request, conn, report_id)
    from havn.engine.reports import run_report

    return run_report(conn, report, _get_config(), trigger="manual", force=force)


@router.post("/api/reports/{report_id}/preview")
def preview_report_endpoint(request: Request, report_id: str, conn: DbConn) -> dict:
    """Render the report as its owner without sending it."""
    report, _user = _load_for_owner(request, conn, report_id)
    from havn.engine.reports import ReportError, prepare_report

    try:
        rendered = prepare_report(conn, report, _get_config())
    except ReportError as e:
        raise _err(e)
    return {
        "subject": rendered.subject,
        "html": inline_images(rendered.html, rendered.inline_images),
        "text": rendered.text,
        "condition_met": rendered.condition_met,
        "condition": rendered.data.get("condition"),
        "attachments": [{"filename": f, "type": m, "bytes": len(c)} for f, m, c in rendered.attachments],
        "notes": rendered.data.get("notes") or [],
    }


def inline_images(html: str, images: dict[str, bytes]) -> str:
    """Swap ``cid:`` references for data URIs so the HTML renders outside a mail client."""
    from havn.engine.reports import inline_images as _inline

    return _inline(html, images)


@router.get("/api/reports/{report_id}/render")
def render_report_endpoint(request: Request, report_id: str, conn: DbConn, format: str = "pdf"):
    """Download the report as it would be sent: ``pdf``, ``png`` or ``html``."""
    from fastapi.responses import Response

    report, _user = _load_for_owner(request, conn, report_id)
    from havn.engine.reports import ReportError, render_file

    try:
        content, media_type, filename = render_file(conn, report, _get_config(), format)
    except ReportError as e:
        raise _err(e)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )
