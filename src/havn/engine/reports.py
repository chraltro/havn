"""Scheduled reports: deliver a dashboard (or one widget) by email and Slack.

A report lives in ``_havn.reports`` and names a dashboard, optionally one
widget, a cron schedule (``engine/cron.py``), recipients, the formats to
attach, fixed filter/parameter values and an optional condition ("only send
when revenue < 1000"). Each run is recorded in ``_havn.report_deliveries``
and in the audit log.

Governance: a report runs as its **owner** (the user who created it), with
the owner's current role, through the shared governed read path. Demoting
the owner narrows what the next delivery contains; deleting the owner stops
the report. Filters and parameters must be ones the dashboard declares.

Scheduling: ``SchedulerThread`` (``engine/scheduler.py``) calls
:func:`run_due_reports_for_project` every poll. A report fires at most once
per cron minute; the fired minute is stored on the report row, so a restart
inside the minute does not send twice.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import secrets
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any

import duckdb

from havn.engine.governed_query import QueryIdentity

logger = logging.getLogger("havn.engine.reports")

FORMATS = ("pdf", "png", "csv")
CONDITION_OPS = {
    "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "=", "ne": "!=",
    "has_rows": "returns rows", "no_rows": "returns no rows",
}
MAX_RECIPIENTS = 50
_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")
_ENV_REF_RE = re.compile(r"^\$\{(\w+)\}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,99}$")


class ReportError(Exception):
    """A report definition is invalid or cannot run. ``status_code`` follows HTTP."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def ensure_report_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the report tables if they do not exist (lazy bootstrap)."""
    from havn.engine.database import _is_ducklake_connection, _strip_pk

    is_lake = _is_ducklake_connection(conn)
    conn.execute("CREATE SCHEMA IF NOT EXISTS _havn")
    conn.execute(_strip_pk("""
        CREATE TABLE IF NOT EXISTS _havn.reports (
            id                VARCHAR PRIMARY KEY,
            name              VARCHAR NOT NULL,
            dashboard_id      VARCHAR NOT NULL,
            widget_id         VARCHAR,
            schedule          VARCHAR,
            enabled           BOOLEAN DEFAULT TRUE,
            owner             VARCHAR NOT NULL,
            recipients        JSON DEFAULT '{}',
            formats           JSON DEFAULT '[]',
            filters           JSON DEFAULT '{}',
            parameters        JSON DEFAULT '{}',
            condition         JSON,
            subject           VARCHAR DEFAULT '',
            message           VARCHAR DEFAULT '',
            created_at        TIMESTAMP,
            updated_at        TIMESTAMP,
            last_run_at       TIMESTAMP,
            last_status       VARCHAR,
            last_error        VARCHAR,
            last_fired_minute TIMESTAMP
        )
    """, is_lake))
    conn.execute(_strip_pk("""
        CREATE TABLE IF NOT EXISTS _havn.report_deliveries (
            id            VARCHAR PRIMARY KEY,
            report_id     VARCHAR NOT NULL,
            trigger       VARCHAR NOT NULL,
            status        VARCHAR NOT NULL,
            condition_met BOOLEAN,
            channels      JSON,
            summary       JSON,
            error         VARCHAR,
            started_at    TIMESTAMP,
            finished_at   TIMESTAMP
        )
    """, is_lake))


_REPORT_COLUMNS = (
    "id, name, dashboard_id, widget_id, schedule, enabled, owner, recipients, formats, "
    "filters, parameters, condition, subject, message, created_at, updated_at, "
    "last_run_at, last_status, last_error, last_fired_minute"
)


def _j(v: Any, default: Any) -> Any:
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    return v.isoformat() if isinstance(v, (_dt.datetime, _dt.date)) else str(v)


def _now() -> _dt.datetime:
    return _dt.datetime.now().replace(microsecond=0)


def _row_to_report(r: tuple) -> dict:
    return {
        "id": r[0],
        "name": r[1],
        "dashboard_id": r[2],
        "widget_id": r[3],
        "schedule": r[4],
        "enabled": bool(r[5]),
        "owner": r[6],
        "recipients": _j(r[7], {}),
        "formats": _j(r[8], []),
        "filters": _j(r[9], {}),
        "parameters": _j(r[10], {}),
        "condition": _j(r[11], None),
        "subject": r[12] or "",
        "message": r[13] or "",
        "created_at": _iso(r[14]),
        "updated_at": _iso(r[15]),
        "last_run_at": _iso(r[16]),
        "last_status": r[17],
        "last_error": r[18],
        "last_fired_minute": _iso(r[19]),
    }


def list_reports(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    try:
        rows = conn.execute(f"SELECT {_REPORT_COLUMNS} FROM _havn.reports ORDER BY name").fetchall()
    except duckdb.CatalogException:
        return []
    return [_row_to_report(r) for r in rows]


def get_report(conn: duckdb.DuckDBPyConnection, report_id: str) -> dict | None:
    try:
        row = conn.execute(f"SELECT {_REPORT_COLUMNS} FROM _havn.reports WHERE id = ?", [report_id]).fetchone()
    except duckdb.CatalogException:
        return None
    return _row_to_report(row) if row else None


def find_report(conn: duckdb.DuckDBPyConnection, name_or_id: str) -> dict | None:
    """Look a report up by id, then by name (case-insensitive)."""
    report = get_report(conn, name_or_id)
    if report:
        return report
    try:
        row = conn.execute(
            f"SELECT {_REPORT_COLUMNS} FROM _havn.reports WHERE lower(name) = lower(?)", [name_or_id]
        ).fetchone()
    except duckdb.CatalogException:
        return None
    return _row_to_report(row) if row else None


def list_deliveries(conn: duckdb.DuckDBPyConnection, report_id: str, limit: int = 20) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT id, trigger, status, condition_met, channels, summary, error, started_at, finished_at "
            "FROM _havn.report_deliveries WHERE report_id = ? ORDER BY started_at DESC LIMIT ?",
            [report_id, max(1, min(limit, 200))],
        ).fetchall()
    except duckdb.CatalogException:
        return []
    return [
        {
            "id": r[0], "trigger": r[1], "status": r[2], "condition_met": r[3],
            "channels": _j(r[4], []), "summary": _j(r[5], {}), "error": r[6],
            "started_at": _iso(r[7]), "finished_at": _iso(r[8]),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_recipients(recipients: dict | None, allowed_domains: list[str] | None = None) -> dict:
    """Normalise ``{"email": [...], "slack": [...]}`` and check every entry."""
    recipients = recipients or {}
    if not isinstance(recipients, dict):
        raise ReportError(400, "recipients must be an object with 'email' and/or 'slack' lists")
    emails_in = recipients.get("email") or []
    slack_in = recipients.get("slack") or []
    if isinstance(emails_in, str):
        emails_in = [e for e in re.split(r"[,\s;]+", emails_in) if e]
    if isinstance(slack_in, str):
        slack_in = [slack_in]
    if not isinstance(emails_in, list) or not isinstance(slack_in, list):
        raise ReportError(400, "recipients.email and recipients.slack must be lists")
    emails: list[str] = []
    domains = [d.lower().lstrip("@") for d in (allowed_domains or []) if d]
    for e in emails_in:
        e = str(e).strip()
        if not _EMAIL_RE.match(e) or len(e) > 254:
            raise ReportError(400, f"'{e}' is not a valid email address")
        if domains and e.rsplit("@", 1)[1].lower() not in domains:
            raise ReportError(
                400, f"'{e}' is outside the allowed recipient domains ({', '.join(domains)})"
            )
        if e.lower() not in [x.lower() for x in emails]:
            emails.append(e)
    slack: list[str] = []
    for t in slack_in:
        t = str(t).strip()
        if t == "default" or _ENV_REF_RE.match(t) or re.match(r"^https?://[^\s]+$", t):
            if t not in slack:
                slack.append(t)
        else:
            raise ReportError(
                400, "Slack targets are 'default', a ${VAR} from .env, or a webhook URL"
            )
    if len(emails) + len(slack) > MAX_RECIPIENTS:
        raise ReportError(400, f"At most {MAX_RECIPIENTS} recipients per report")
    return {"email": emails, "slack": slack}


def validate_condition(condition: dict | None, widget_ids: set[str]) -> dict | None:
    if not condition:
        return None
    if not isinstance(condition, dict):
        raise ReportError(400, "condition must be an object")
    op = condition.get("op")
    if op not in CONDITION_OPS:
        raise ReportError(400, f"condition.op must be one of: {', '.join(CONDITION_OPS)}")
    wid = condition.get("widget_id")
    if wid not in widget_ids:
        raise ReportError(400, "condition.widget_id must be a query widget on the dashboard")
    out: dict = {"widget_id": wid, "op": op}
    if condition.get("column"):
        col = str(condition["column"])
        if len(col) > 200:
            raise ReportError(400, "condition.column is too long")
        out["column"] = col
    if op not in ("has_rows", "no_rows"):
        value = condition.get("value")
        try:
            out["value"] = float(value)
        except (TypeError, ValueError):
            raise ReportError(400, "condition.value must be a number")
    return out


def _validate_definition(conn: duckdb.DuckDBPyConnection, fields: dict, config) -> dict:
    from havn.engine.cron import CronError, parse_cron
    from havn.engine.dashboard_queries import FilterValueError, load_dashboard, runs_query, validate_viewer_inputs

    out = dict(fields)
    if "name" in out:
        name = (out["name"] or "").strip()
        if not _NAME_RE.match(name):
            raise ReportError(400, "name: 1-100 letters, digits, spaces, '.', '_' or '-'")
        out["name"] = name
    dashboard = load_dashboard(conn, out["dashboard_id"])
    if dashboard is None:
        raise ReportError(404, "Dashboard not found")
    query_widgets = {w["id"] for w in dashboard["widgets"] if runs_query(w)}
    all_widgets = {w["id"] for w in dashboard["widgets"]}
    if out.get("widget_id") and out["widget_id"] not in all_widgets:
        raise ReportError(400, "widget_id is not a widget on this dashboard")
    if out.get("schedule"):
        try:
            parse_cron(out["schedule"])
        except CronError as e:
            raise ReportError(400, f"schedule: {e}")
    else:
        out["schedule"] = None
    formats = out.get("formats") or []
    bad = [f for f in formats if f not in FORMATS]
    if bad:
        raise ReportError(400, f"Unknown format(s): {', '.join(bad)}. Use: {', '.join(FORMATS)}")
    out["formats"] = list(dict.fromkeys(formats))
    allowed = getattr(getattr(config, "reports", None), "allowed_recipient_domains", None)
    out["recipients"] = validate_recipients(out.get("recipients"), allowed)
    try:
        f, p, _ = validate_viewer_inputs(dashboard, out.get("filters") or {}, out.get("parameters") or {})
    except FilterValueError as e:
        raise ReportError(400, str(e))
    out["filters"] = f
    out["parameters"] = {k: v for k, v in (out.get("parameters") or {}).items() if k in p}
    out["condition"] = validate_condition(out.get("condition"), query_widgets)
    out["subject"] = (out.get("subject") or "")[:300]
    out["message"] = (out.get("message") or "")[:4000]
    return out


def create_report(conn: duckdb.DuckDBPyConnection, fields: dict, owner: str, config=None) -> dict:
    ensure_report_tables(conn)
    data = _validate_definition(conn, fields, config)
    if find_report(conn, data["name"]):
        raise ReportError(409, f"A report named '{data['name']}' already exists")
    rid = "r" + secrets.token_hex(8)
    now = _now()
    conn.execute(
        f"""
        INSERT INTO _havn.reports (id, name, dashboard_id, widget_id, schedule, enabled, owner,
            recipients, formats, filters, parameters, condition, subject, message, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [rid, data["name"], data["dashboard_id"], data.get("widget_id") or None, data["schedule"],
         bool(data.get("enabled", True)), owner, json.dumps(data["recipients"]),
         json.dumps(data["formats"]), json.dumps(data["filters"]), json.dumps(data["parameters"]),
         json.dumps(data["condition"]) if data["condition"] else None,
         data["subject"], data["message"], now, now],
    )
    return get_report(conn, rid)


def update_report(conn: duckdb.DuckDBPyConnection, report_id: str, changes: dict, config=None) -> dict:
    current = get_report(conn, report_id)
    if current is None:
        raise ReportError(404, "Report not found")
    merged = {k: current[k] for k in (
        "name", "dashboard_id", "widget_id", "schedule", "enabled", "recipients", "formats",
        "filters", "parameters", "condition", "subject", "message",
    )}
    merged.update({k: v for k, v in changes.items() if k in merged})
    data = _validate_definition(conn, merged, config)
    other = find_report(conn, data["name"])
    if other and other["id"] != report_id:
        raise ReportError(409, f"A report named '{data['name']}' already exists")
    conn.execute(
        """
        UPDATE _havn.reports SET name = ?, dashboard_id = ?, widget_id = ?, schedule = ?, enabled = ?,
            recipients = ?, formats = ?, filters = ?, parameters = ?, condition = ?, subject = ?,
            message = ?, updated_at = ?
        WHERE id = ?
        """,
        [data["name"], data["dashboard_id"], data.get("widget_id") or None, data["schedule"],
         bool(data.get("enabled", True)), json.dumps(data["recipients"]), json.dumps(data["formats"]),
         json.dumps(data["filters"]), json.dumps(data["parameters"]),
         json.dumps(data["condition"]) if data["condition"] else None,
         data["subject"], data["message"], _now(), report_id],
    )
    return get_report(conn, report_id)


def delete_report(conn: duckdb.DuckDBPyConnection, report_id: str) -> bool:
    try:
        deleted = conn.execute("DELETE FROM _havn.reports WHERE id = ? RETURNING id", [report_id]).fetchall()
        conn.execute("DELETE FROM _havn.report_deliveries WHERE report_id = ?", [report_id])
    except duckdb.CatalogException:
        return False
    return bool(deleted)


def disable_reports_for_dashboard(conn: duckdb.DuckDBPyConnection, dashboard_id: str) -> int:
    """Called when a dashboard is deleted: its reports stop, with the reason on the row."""
    try:
        rows = conn.execute(
            "UPDATE _havn.reports SET enabled = FALSE, last_status = 'failed', "
            "last_error = 'The dashboard was deleted' WHERE dashboard_id = ? RETURNING id",
            [dashboard_id],
        ).fetchall()
    except duckdb.CatalogException:
        return 0
    return len(rows)


def public_view(report: dict) -> dict:
    """A report for API responses: Slack webhook URLs reduced to their host (they are secrets)."""
    out = dict(report)
    rec = dict(out.get("recipients") or {})
    rec["slack"] = [_mask_slack_target(t) for t in rec.get("slack") or []]
    out["recipients"] = rec
    return out


def _mask_slack_target(t: str) -> str:
    if t == "default" or _ENV_REF_RE.match(t):
        return t
    m = re.match(r"^(https?://[^/]+)", t)
    return f"{m.group(1)}/…" if m else "…"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def owner_identity(conn: duckdb.DuckDBPyConnection, owner: str) -> QueryIdentity:
    """The identity a report runs as: its owner with the owner's current role.

    A report created with auth disabled is owned by ``local`` and runs as
    admin, which is what that user was; a deleted owner stops the report.
    """
    try:
        row = conn.execute("SELECT role FROM _havn.users WHERE username = ?", [owner]).fetchone()
    except duckdb.CatalogException:
        row = None
    if row:
        return QueryIdentity(username=owner, role=row[0], source="report")
    if owner == "local":
        return QueryIdentity(username="local", role="admin", source="report")
    raise ReportError(403, f"The report's owner '{owner}' no longer exists; reassign or delete the report")


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


@dataclass
class RenderedReport:
    subject: str
    html: str
    text: str
    attachments: list[tuple[str, str, bytes]] = field(default_factory=list)  # (filename, mime, content)
    inline_images: dict[str, bytes] = field(default_factory=dict)  # cid -> png
    data: dict = field(default_factory=dict)
    condition_met: bool = True


def collect_report_data(
    conn: duckdb.DuckDBPyConnection, report: dict, identity: QueryIdentity, config=None,
) -> dict:
    """Run the report's widgets as ``identity`` and return the data to render."""
    from havn.engine.dashboard_queries import (
        FilterValueError,
        dashboard_freshness,
        load_dashboard,
        run_widget_query,
        runs_query,
        validate_viewer_inputs,
    )
    from havn.engine.governed_query import GovernedQueryError
    from havn.engine.report_render import kpi_display

    dashboard = load_dashboard(conn, report["dashboard_id"])
    if dashboard is None:
        raise ReportError(404, "The report's dashboard no longer exists")
    try:
        filters, params, types = validate_viewer_inputs(dashboard, report.get("filters"), report.get("parameters"))
    except FilterValueError as e:
        raise ReportError(400, str(e))
    max_rows = int(getattr(getattr(config, "reports", None), "max_rows_per_widget", 1000) or 1000)

    selected = dashboard["widgets"]
    if report.get("widget_id"):
        selected = [w for w in selected if w["id"] == report["widget_id"]]
        if not selected:
            raise ReportError(404, "The report's widget no longer exists")
    needed = {w["id"] for w in selected}
    cond = report.get("condition") or None
    if cond:
        needed.add(cond.get("widget_id"))

    results: dict[str, dict] = {}
    for w in dashboard["widgets"]:
        if w["id"] not in needed:
            continue
        entry = {
            "id": w["id"], "title": w.get("title") or "", "widget_type": w.get("widget_type"),
            "chart_type": w.get("chart_type"), "config": w.get("config") or {},
            "columns": [], "rows": [], "row_count": 0, "truncated": False, "error": None,
        }
        if runs_query(w):
            try:
                r = run_widget_query(
                    conn, w["sql_query"], identity, filters=filters, parameters=params,
                    filter_types=types, row_cap=max_rows,
                )
                entry.update({k: r[k] for k in ("columns", "rows", "row_count", "truncated")})
            except (GovernedQueryError, FilterValueError) as e:
                entry["error"] = str(e)
        if w.get("widget_type") == "text" and not entry["config"].get("content") and w.get("sql_query"):
            entry["config"] = {**entry["config"], "content": w["sql_query"]}
        if w.get("widget_type") == "kpi" and not entry["error"]:
            entry["kpi"] = kpi_display(entry)
        results[w["id"]] = entry

    return {
        "title": report.get("subject") or report.get("name") or dashboard["name"],
        "report_name": report.get("name"),
        "dashboard_name": dashboard["name"],
        "dashboard_id": dashboard["id"],
        "message": report.get("message") or "",
        "generated_at": _now().isoformat(),
        "freshness": dashboard_freshness(conn, [w for w in dashboard["widgets"] if w["id"] in needed]),
        "filters": filters,
        "widgets": [results[w["id"]] for w in selected if w["id"] in results],
        "_all_results": results,
        "run_as": {"username": identity.username, "role": identity.role},
    }


def evaluate_condition(condition: dict | None, data: dict) -> tuple[bool, str | None]:
    """(met, description). No condition means always send."""
    if not condition:
        return True, None
    from havn.engine.dashboard_queries import kpi_value

    w = data["_all_results"].get(condition["widget_id"])
    if w is None:
        return False, "the condition's widget no longer exists"
    title = w.get("title") or "the widget"
    if w.get("error"):
        return False, f"{title} failed: {w['error']}"
    op = condition["op"]
    if op == "has_rows":
        return w["row_count"] > 0, f"{title} returned {w['row_count']} row(s)"
    if op == "no_rows":
        return w["row_count"] == 0, f"{title} returned no rows"
    col, value = kpi_value(w, condition.get("column"))
    try:
        actual = float(value)
    except (TypeError, ValueError):
        return False, f"{title} has no numeric value to compare"
    target = float(condition["value"])
    met = {
        "gt": actual > target, "gte": actual >= target, "lt": actual < target,
        "lte": actual <= target, "eq": actual == target, "ne": actual != target,
    }[op]
    from havn.engine.report_render import format_number

    return met, f"{col} is {format_number(actual)} ({CONDITION_OPS[op]} {format_number(target)})"


def _dashboard_link(conn: duckdb.DuckDBPyConnection, dashboard_id: str, base_url: str | None) -> str | None:
    """A link for recipients: the dashboard's signed-in share link, when there is one."""
    if not base_url:
        return None
    try:
        row = conn.execute(
            "SELECT id FROM _havn.dashboard_shares WHERE dashboard_id = ? AND mode = 'signed_in' "
            "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY created_at DESC LIMIT 1",
            [dashboard_id, _now()],
        ).fetchone()
    except duckdb.CatalogException:
        row = None
    return f"{base_url.rstrip('/')}/p/{row[0]}" if row else None


def render_report(conn: duckdb.DuckDBPyConnection, report: dict, data: dict, config=None) -> RenderedReport:
    """Turn collected data into the email/Slack payloads and attachments."""
    from havn.engine.report_render import (
        charts_available,
        render_dashboard_png,
        render_html,
        render_pdf,
        render_text,
        render_widget_png,
        safe_filename,
        widget_csv,
    )

    rcfg = getattr(config, "reports", None)
    charts = charts_available(getattr(rcfg, "charts", "auto") if rcfg else "auto")
    met, description = evaluate_condition(report.get("condition"), data)
    data["condition"] = {"met": met, "description": description} if description else None
    data["link_url"] = _dashboard_link(conn, report["dashboard_id"], getattr(rcfg, "base_url", None))
    formats = report.get("formats") or []
    notes: list[str] = []

    inline: dict[str, bytes] = {}
    cids: dict[str, str] = {}
    if charts:
        for w in data["widgets"]:
            png = render_widget_png(w)
            if png:
                cid = f"chart-{safe_filename(w['id'])}"
                cids[w["id"]] = cid
                inline[cid] = png

    attachments: list[tuple[str, str, bytes]] = []
    base = safe_filename(report.get("name") or data["dashboard_name"], "report")
    if "pdf" in formats:
        attachments.append((f"{base}.pdf", "application/pdf", render_pdf(data, use_charts=charts)))
        if not charts:
            notes.append("The PDF is text-only: install havn[reports] (matplotlib) on the server for charts.")
    if "png" in formats:
        png = render_dashboard_png(data) if charts else None
        if png:
            attachments.append((f"{base}.png", "image/png", png))
        else:
            notes.append(
                "No PNG snapshot: install havn[reports] (matplotlib) on the server."
                if not charts else "No PNG snapshot: nothing in this report can be drawn as a chart."
            )
    if "csv" in formats:
        for w in data["widgets"]:
            if w.get("columns") and not w.get("error"):
                attachments.append((
                    f"{base}-{safe_filename(w.get('title') or w['id'])}.csv", "text/csv", widget_csv(w),
                ))
    data["notes"] = notes
    if attachments:
        data["attachments_note"] = f"{len(attachments)} attachment{'s' if len(attachments) != 1 else ''}"
    subject = report.get("subject") or f"{report.get('name') or data['dashboard_name']}"
    if description and report.get("condition"):
        subject = f"{subject}: {description}" if met else subject
    return RenderedReport(
        subject=subject[:250],
        html=render_html(data, cids),
        text=render_text(data),
        attachments=attachments,
        inline_images=inline,
        data=data,
        condition_met=met,
    )


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def _resolve_secret(value: str | None, what: str) -> str | None:
    if value is None:
        return None
    m = re.search(r"\$\{(\w+)\}", value)
    if m:
        raise ReportError(400, f"{what} references ${{{m.group(1)}}}, which is not set in .env")
    return value


def send_email(rendered: RenderedReport, to: list[str], smtp_cfg) -> None:
    """Send the rendered report to ``to`` via the project's SMTP settings."""
    host = _resolve_secret(getattr(smtp_cfg, "host", None), "reports.smtp.host")
    if not host:
        raise ReportError(400, "Email is not configured: set reports.smtp.host in project.yml")
    sender = _resolve_secret(smtp_cfg.from_address or smtp_cfg.username, "reports.smtp.from")
    if not sender:
        raise ReportError(400, "Set reports.smtp.from in project.yml")
    username = _resolve_secret(smtp_cfg.username, "reports.smtp.username")
    password = _resolve_secret(smtp_cfg.password, "reports.smtp.password")

    msg = EmailMessage()
    msg["Subject"] = rendered.subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="havn.local")
    msg.set_content(rendered.text)
    msg.add_alternative(rendered.html, subtype="html")
    html_part = msg.get_payload()[-1]
    for cid, png in rendered.inline_images.items():
        html_part.add_related(png, maintype="image", subtype="png", cid=f"<{cid}>", filename=f"{cid}.png")
    for filename, mime, content in rendered.attachments:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)

    timeout = getattr(smtp_cfg, "timeout", 30) or 30
    if smtp_cfg.ssl:
        client = smtplib.SMTP_SSL(host, smtp_cfg.port or 465, timeout=timeout, context=ssl.create_default_context())
    else:
        client = smtplib.SMTP(host, smtp_cfg.port or 587, timeout=timeout)
    try:
        client.ehlo()
        if not smtp_cfg.ssl and smtp_cfg.starttls:
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        if username and password:
            client.login(username, password)
        client.send_message(msg)
    finally:
        try:
            client.quit()
        except Exception:
            pass


def slack_payload(rendered: RenderedReport) -> dict:
    """Slack Block Kit message: title, condition, KPI fields, top rows, link."""
    from havn.engine.report_render import format_cell

    data = rendered.data
    blocks: list[dict] = [{"type": "header", "text": {"type": "plain_text", "text": rendered.subject[:150]}}]
    cond = data.get("condition")
    if cond and cond.get("description"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f":bell: Sent because {cond['description']}"}})
    kpis = [w["kpi"] for w in data["widgets"] if w.get("kpi")]
    if kpis:
        blocks.append({
            "type": "section",
            "fields": [{"type": "mrkdwn", "text": f"*{k['label']}*\n{k['display']}"} for k in kpis[:10]],
        })
    for w in data["widgets"]:
        if w.get("kpi") or w.get("widget_type") in ("text", "image", "divider"):
            continue
        if w.get("error"):
            body = f"_could not load: {w['error'][:200]}_"
        else:
            cols = w.get("columns") or []
            lines = [" | ".join(str(c) for c in cols)]
            lines += [" | ".join(format_cell(v) for v in r) for r in (w.get("rows") or [])[:8]]
            more = w.get("row_count", 0) - 8
            body = "```" + "\n".join(lines)[:2500] + "```" + (f"\n…and {more} more rows" if more > 0 else "")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{w.get('title') or 'Untitled'}*\n{body}"}})
        if len(blocks) > 45:
            break
    fresh = (data.get("freshness") or {}).get("as_of")
    context = f"havn report · generated {data.get('generated_at', '')[:16].replace('T', ' ')}"
    if fresh:
        context += f" · data as of {fresh[:16].replace('T', ' ')}"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": context}]})
    if data.get("link_url"):
        blocks.append({
            "type": "actions",
            "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Open dashboard"}, "url": data["link_url"]}],
        })
    return {"text": rendered.subject, "blocks": blocks}


def resolve_slack_target(target: str, config) -> str:
    """Turn a stored Slack target into a webhook URL."""
    import os

    if target == "default":
        url = getattr(getattr(config, "reports", None), "slack_webhook_url", None) or getattr(
            getattr(config, "alerts", None), "slack_webhook_url", None
        )
        if not url:
            raise ReportError(400, "No default Slack webhook: set reports.slack_webhook_url or alerts.slack_webhook_url")
        return _resolve_secret(url, "reports.slack_webhook_url")
    m = _ENV_REF_RE.match(target)
    if m:
        url = os.environ.get(m.group(1))
        if not url:
            raise ReportError(400, f"${{{m.group(1)}}} is not set in .env")
        return url
    return target


def deliver(rendered: RenderedReport, report: dict, config) -> list[dict]:
    """Send to every recipient; returns one result per channel target."""
    from havn.engine.alerts import post_slack_payload

    results: list[dict] = []
    recipients = report.get("recipients") or {}
    emails = recipients.get("email") or []
    if emails:
        try:
            send_email(rendered, emails, config.reports.smtp)
            results.append({"channel": "email", "target": ", ".join(emails), "status": "sent"})
        except Exception as e:
            logger.warning("Report %s email failed: %s", report.get("name"), e)
            results.append({"channel": "email", "target": ", ".join(emails), "status": "failed", "error": str(e)[:500]})
    payload = None
    for target in recipients.get("slack") or []:
        shown = _mask_slack_target(target)
        try:
            payload = payload or slack_payload(rendered)
            post_slack_payload(resolve_slack_target(target, config), payload)
            results.append({"channel": "slack", "target": shown, "status": "sent"})
        except Exception as e:
            logger.warning("Report %s Slack delivery failed: %s", report.get("name"), e)
            results.append({"channel": "slack", "target": shown, "status": "failed", "error": str(e)[:500]})
    return results


def _record(conn, report: dict, *, trigger: str, status: str, condition_met: bool | None,
            channels: list[dict], summary: dict, error: str | None, started: _dt.datetime) -> dict:
    did = "d" + secrets.token_hex(8)
    finished = _now()
    try:
        conn.execute(
            """
            INSERT INTO _havn.report_deliveries
                (id, report_id, trigger, status, condition_met, channels, summary, error, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [did, report["id"], trigger, status, condition_met, json.dumps(channels),
             json.dumps(summary, default=str), error, started, finished],
        )
        conn.execute(
            "UPDATE _havn.reports SET last_run_at = ?, last_status = ?, last_error = ? WHERE id = ?",
            [finished, status, error, report["id"]],
        )
    except Exception:
        logger.warning("Failed to record delivery for report %s", report.get("name"), exc_info=True)
    try:
        from havn.engine.audit import log_audit

        sent_to = ", ".join(f"{c['channel']}:{c['target']}={c['status']}" for c in channels) or "none"
        log_audit(
            conn, user=report.get("owner") or "unknown", action="report_delivery",
            resource=f"report={report.get('name')}",
            detail=f"trigger={trigger} status={status} to={sent_to}" + (f" error={error[:200]}" if error else ""),
        )
    except Exception:
        logger.debug("Audit write failed for report delivery", exc_info=True)
    return {
        "id": did, "report_id": report["id"], "trigger": trigger, "status": status,
        "condition_met": condition_met, "channels": channels, "summary": summary, "error": error,
        "started_at": _iso(started), "finished_at": _iso(finished),
    }


def _summary(data: dict) -> dict:
    return {
        "kpis": [
            {"label": w["kpi"]["label"], "value": w["kpi"]["value"], "display": w["kpi"]["display"]}
            for w in data.get("widgets", []) if w.get("kpi")
        ],
        "widgets": len(data.get("widgets", [])),
        "errors": [w["title"] or w["id"] for w in data.get("widgets", []) if w.get("error")],
        "condition": (data.get("condition") or {}).get("description"),
        "run_as": data.get("run_as"),
    }


def prepare_report(conn: duckdb.DuckDBPyConnection, report: dict, config=None) -> RenderedReport:
    """Collect and render a report as its owner, without sending anything."""
    identity = owner_identity(conn, report["owner"])
    data = collect_report_data(conn, report, identity, config)
    return render_report(conn, report, data, config)


def run_report(
    conn: duckdb.DuckDBPyConnection, report: dict, config, *, trigger: str = "manual", force: bool = False,
) -> dict:
    """Collect, render and deliver one report, and record the delivery.

    ``force`` sends even when the report's condition is not met (used by
    "send now" when the user asks for it). Never raises for delivery
    problems; they are recorded on the delivery with status ``failed``.
    """
    ensure_report_tables(conn)
    started = _now()
    try:
        rendered = prepare_report(conn, report, config)
    except ReportError as e:
        return _record(conn, report, trigger=trigger, status="failed", condition_met=None,
                       channels=[], summary={}, error=str(e), started=started)
    except Exception as e:
        logger.exception("Report %s failed to render", report.get("name"))
        return _record(conn, report, trigger=trigger, status="failed", condition_met=None,
                       channels=[], summary={}, error=f"Rendering failed: {e}", started=started)
    summary = _summary(rendered.data)
    if not rendered.condition_met and not force:
        return _record(conn, report, trigger=trigger, status="skipped", condition_met=False,
                       channels=[], summary=summary,
                       error=None, started=started)
    recipients = report.get("recipients") or {}
    if not (recipients.get("email") or recipients.get("slack")):
        return _record(conn, report, trigger=trigger, status="failed", condition_met=rendered.condition_met,
                       channels=[], summary=summary, error="The report has no recipients", started=started)
    channels = deliver(rendered, report, config)
    sent = sum(1 for c in channels if c["status"] == "sent")
    status = "sent" if sent == len(channels) else ("partial" if sent else "failed")
    errors = "; ".join(f"{c['channel']}: {c['error']}" for c in channels if c.get("error")) or None
    return _record(conn, report, trigger=trigger, status=status, condition_met=rendered.condition_met,
                   channels=channels, summary=summary, error=errors, started=started)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def claim_due_reports(conn: duckdb.DuckDBPyConnection, now: _dt.datetime | None = None) -> list[dict]:
    """Reports whose cron matches ``now``'s minute and have not fired in it; marks them fired."""
    from havn.engine.cron import cron_matches

    now = now or _dt.datetime.now()
    minute = now.replace(second=0, microsecond=0)
    due = []
    for report in list_reports(conn):
        if not report["enabled"] or not report["schedule"]:
            continue
        try:
            if not cron_matches(report["schedule"], now):
                continue
        except Exception:
            continue
        last = report.get("last_fired_minute")
        if last and _dt.datetime.fromisoformat(last) >= minute:
            continue
        rows = conn.execute(
            "UPDATE _havn.reports SET last_fired_minute = ? WHERE id = ? "
            "AND (last_fired_minute IS NULL OR last_fired_minute < ?) RETURNING id",
            [minute, report["id"], minute],
        ).fetchall()
        if rows:
            due.append(report)
    return due


def run_due_reports(conn: duckdb.DuckDBPyConnection, config, now: _dt.datetime | None = None) -> list[dict]:
    """Run every report due at ``now``. Returns the delivery records."""
    out = []
    for report in claim_due_reports(conn, now):
        logger.info("Scheduler sending report: %s", report["name"])
        out.append(run_report(conn, report, config, trigger="schedule"))
    return out


def inline_images(html: str, images: dict[str, bytes]) -> str:
    """Swap ``cid:`` references for data URIs so the HTML renders outside a mail client."""
    import base64

    for cid, png in images.items():
        html = html.replace(f'src="cid:{cid}"', f'src="data:image/png;base64,{base64.b64encode(png).decode()}"')
    return html


def render_file(conn: duckdb.DuckDBPyConnection, report: dict, config, fmt: str) -> tuple[bytes, str, str]:
    """Render a report to one file for preview/download: ``(content, media type, filename)``."""
    from havn.engine.report_render import charts_available, render_dashboard_png, render_pdf, safe_filename

    fmt = (fmt or "html").lower()
    if fmt not in ("html", "pdf", "png"):
        raise ReportError(400, "format must be html, pdf or png")
    rendered = prepare_report(conn, report, config)
    base = safe_filename(report.get("name") or "report", "report")
    rcfg = getattr(config, "reports", None)
    charts = charts_available(getattr(rcfg, "charts", "auto") if rcfg else "auto")
    if fmt == "pdf":
        return render_pdf(rendered.data, use_charts=charts), "application/pdf", f"{base}.pdf"
    if fmt == "png":
        png = render_dashboard_png(rendered.data) if charts else None
        if png is None:
            raise ReportError(
                400,
                'PNG needs matplotlib on the server (pip install "havn[reports]")' if not charts
                else "Nothing in this report can be drawn as a chart",
            )
        return png, "image/png", f"{base}.png"
    html = inline_images(rendered.html, rendered.inline_images)
    return html.encode("utf-8"), "text/html; charset=utf-8", f"{base}.html"


def run_due_reports_for_project(project_dir: Path, now: _dt.datetime | None = None) -> list[dict]:
    """Scheduler entry point: open the warehouse, run due reports, close it."""
    from havn.config import load_project
    from havn.engine.backends import create_backend
    from havn.engine.database import open_warehouse

    config = load_project(project_dir)
    if not create_backend(config.database, project_dir=project_dir).exists():
        return []
    conn = open_warehouse(config, project_dir)
    try:
        try:
            conn.execute("SELECT 1 FROM _havn.reports LIMIT 1")
        except duckdb.CatalogException:
            return []
        return run_due_reports(conn, config, now)
    finally:
        conn.close()
