"""Scheduled reports: definitions, governance, rendering, scheduling and delivery.

Delivery is tested end to end against local servers: a minimal SMTP server
on a socket (no smtpd/aiosmtpd) and an HTTP server standing in for Slack.
"""

from __future__ import annotations

import datetime as dt
import email
import json
import socket
import threading
from email import policy
from http.server import BaseHTTPRequestHandler, HTTPServer

import duckdb
import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Local SMTP and Slack stand-ins
# ---------------------------------------------------------------------------


class FakeSMTP:
    """Just enough SMTP (EHLO/MAIL/RCPT/DATA/QUIT) to receive messages."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.messages: list[dict] = []
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn):
        f = conn.makefile("rb")

        def send(line):
            conn.sendall((line + "\r\n").encode())

        send("220 fake ESMTP")
        mail_from, rcpts = None, []
        try:
            while True:
                line = f.readline()
                if not line:
                    break
                cmd = line.decode().strip()
                upper = cmd.upper()
                if upper.startswith(("EHLO", "HELO")):
                    send("250-fake")
                    send("250 8BITMIME")
                elif upper.startswith("MAIL FROM"):
                    mail_from = cmd.split(":", 1)[1].strip()
                    send("250 OK")
                elif upper.startswith("RCPT TO"):
                    rcpts.append(cmd.split(":", 1)[1].strip().strip("<>"))
                    send("250 OK")
                elif upper == "DATA":
                    send("354 go ahead")
                    data = bytearray()
                    while True:
                        chunk = f.readline()
                        if chunk in (b".\r\n", b".\n", b""):
                            break
                        if chunk.startswith(b".."):
                            chunk = chunk[1:]
                        data += chunk
                    self.messages.append({
                        "from": mail_from, "rcpts": rcpts,
                        "message": email.message_from_bytes(bytes(data), policy=policy.default),
                    })
                    rcpts = []
                    send("250 queued")
                elif upper == "QUIT":
                    send("221 bye")
                    break
                else:
                    send("250 OK")
        finally:
            conn.close()

    def close(self):
        self._stop = True
        self.sock.close()


class FakeSlack:
    def __init__(self, status=200):
        received = self.received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                received.append(json.loads(body))
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def smtp():
    s = FakeSMTP()
    yield s
    s.close()


@pytest.fixture
def slack():
    s = FakeSlack()
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Project / client
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path, smtp, slack, monkeypatch):
    (tmp_path / ".env").write_text(
        f"SMTP_PASSWORD=hunter2\nTEAM_SLACK={slack.url}\n", encoding="utf-8"
    )
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("TEAM_SLACK", raising=False)
    (tmp_path / "project.yml").write_text(
        "name: test\n"
        "database:\n  path: warehouse.duckdb\n"
        "reports:\n"
        "  base_url: https://havn.example.com\n"
        f"  slack_webhook_url: {slack.url}\n"
        "  allowed_recipient_domains: [example.com]\n"
        "  smtp:\n"
        "    host: 127.0.0.1\n"
        f"    port: {smtp.port}\n"
        "    from: havn <reports@example.com>\n"
        "    starttls: false\n",
        encoding="utf-8",
    )
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA landing")
    conn.execute(
        "CREATE TABLE landing.orders AS SELECT i AS id, 'c' || i || '@example.com' AS email, "
        "CASE WHEN i % 2 = 0 THEN 'north' ELSE 'south' END AS region, i * 10.0 AS amount "
        "FROM range(1, 11) t(i)"
    )
    from havn.engine.auth import authenticate, create_user
    from havn.engine.masking import create_policy, ensure_masking_table

    tokens = {}
    for name, role in (("ada", "admin"), ("eve", "editor"), ("ed2", "editor"), ("vic", "viewer")):
        create_user(conn, name, "pw-" + name, role)
        tokens[name] = authenticate(conn, name, "pw-" + name)
    ensure_masking_table(conn)
    create_policy(conn, schema_name="landing", table_name="orders", column_name="email",
                  method="redact", exempted_roles=["admin"])
    conn.close()
    (tmp_path / "tokens.json").write_text(json.dumps(tokens), encoding="utf-8")
    return tmp_path


@pytest.fixture
def client(project):
    import havn.server.app as server_app
    from havn.server.deps import _clear_config_cache, invalidate_token_cache, reset_shared_conn

    reset_shared_conn()
    invalidate_token_cache()
    _clear_config_cache()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = True
    c = TestClient(server_app.app)
    yield c
    server_app.AUTH_ENABLED = False
    reset_shared_conn()


def _h(project, who):
    tokens = json.loads((project / "tokens.json").read_text(encoding="utf-8"))
    return {"Authorization": f"Bearer {tokens[who]}"}


@pytest.fixture
def dashboard(client, project):
    admin = _h(project, "ada")
    did = client.post("/api/dashboards", json={"name": "Sales"}, headers=admin).json()["id"]
    client.put(f"/api/dashboards/{did}", headers=admin, json={
        "filters": [{"id": "f1", "label": "Region", "type": "dropdown", "column": "region"}],
    })
    kpi = client.post(f"/api/dashboards/{did}/widgets", headers=admin, json={
        "widget_type": "kpi", "title": "Revenue", "config": {"prefix": "$"},
        "sql_query": "SELECT SUM(amount) AS revenue FROM landing.orders",
        "position": {"x": 1, "y": 1, "w": 6, "h": 3},
    }).json()["id"]
    chart = client.post(f"/api/dashboards/{did}/widgets", headers=admin, json={
        "widget_type": "chart", "chart_type": "bar", "title": "By region",
        "sql_query": "SELECT region, SUM(amount) AS amount FROM landing.orders GROUP BY 1 ORDER BY 1",
        "position": {"x": 7, "y": 1, "w": 6, "h": 4},
    }).json()["id"]
    table = client.post(f"/api/dashboards/{did}/widgets", headers=admin, json={
        "widget_type": "table", "title": "Orders",
        "sql_query": "SELECT id, email, region, amount FROM landing.orders ORDER BY id",
        "position": {"x": 1, "y": 5, "w": 12, "h": 6},
    }).json()["id"]
    return {"id": did, "kpi": kpi, "chart": chart, "table": table}


def _create(client, project, dashboard, who="eve", **kw):
    body = {
        "name": "Daily sales", "dashboard_id": dashboard["id"], "schedule": "0 7 * * *",
        "recipients": {"email": ["boss@example.com"], "slack": ["default"]},
        "formats": ["pdf", "png", "csv"], **kw,
    }
    r = client.post("/api/reports", json=body, headers=_h(project, who))
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Definitions and permissions
# ---------------------------------------------------------------------------


def test_create_validates(client, project, dashboard):
    h = _h(project, "eve")
    base = {"name": "x", "dashboard_id": dashboard["id"]}
    assert client.post("/api/reports", json={**base, "schedule": "not cron"}, headers=h).status_code == 400
    assert client.post("/api/reports", json={**base, "recipients": {"email": ["a@elsewhere.org"]}},
                       headers=h).status_code == 400
    assert client.post("/api/reports", json={**base, "recipients": {"email": ["nope"]}}, headers=h).status_code == 400
    assert client.post("/api/reports", json={**base, "recipients": {"slack": ["file:///etc"]}},
                       headers=h).status_code == 400
    assert client.post("/api/reports", json={**base, "formats": ["docx"]}, headers=h).status_code == 400
    assert client.post("/api/reports", json={**base, "filters": {"email": "x"}}, headers=h).status_code == 400
    assert client.post("/api/reports", json={**base, "condition": {"widget_id": "zz", "op": "gt", "value": 1}},
                       headers=h).status_code == 400
    assert client.post("/api/reports", json={**base, "dashboard_id": "missing"}, headers=h).status_code == 404
    assert client.post("/api/reports", json=base, headers=_h(project, "vic")).status_code == 403


def test_only_owner_or_admin_changes_a_report(client, project, dashboard):
    rep = _create(client, project, dashboard)
    assert rep["owner"] == "eve" and rep["next_run_at"]
    assert client.put(f"/api/reports/{rep['id']}", json={"enabled": False}, headers=_h(project, "ed2")).status_code == 403
    assert client.post(f"/api/reports/{rep['id']}/send", headers=_h(project, "ed2")).status_code == 403
    assert client.post(f"/api/reports/{rep['id']}/preview", headers=_h(project, "ed2")).status_code == 403
    r = client.put(f"/api/reports/{rep['id']}", json={"enabled": False}, headers=_h(project, "ada"))
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["owner"] == "eve"
    assert r.json()["next_run_at"] is None
    assert client.delete(f"/api/reports/{rep['id']}", headers=_h(project, "eve")).status_code == 200


def test_slack_urls_are_masked_and_survive_round_trip(client, project, dashboard, slack):
    rep = _create(client, project, dashboard, recipients={"slack": [slack.url, "${TEAM_SLACK}"]})
    shown = rep["recipients"]["slack"]
    assert slack.url not in json.dumps(rep) and shown[0].endswith("/…") and shown[1] == "${TEAM_SLACK}"
    # The UI sends back what it was shown; the stored URL is kept.
    r = client.put(f"/api/reports/{rep['id']}", json={"recipients": {"slack": shown}}, headers=_h(project, "eve"))
    assert r.status_code == 200
    d = client.post(f"/api/reports/{rep['id']}/send", headers=_h(project, "eve")).json()
    assert d["status"] == "sent" and len(slack.received) == 2


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def test_send_now_delivers_email_and_slack(client, project, dashboard, smtp, slack):
    rep = _create(client, project, dashboard)
    d = client.post(f"/api/reports/{rep['id']}/send", headers=_h(project, "eve")).json()
    assert d["status"] == "sent", d
    assert {c["channel"] for c in d["channels"]} == {"email", "slack"}

    msg = smtp.messages[0]
    assert msg["rcpts"] == ["boss@example.com"]
    m = msg["message"]
    assert m["Subject"] == "Daily sales"
    html = m.get_body(preferencelist=("html",)).get_content()
    assert "$550" in html  # KPI value inline
    names = sorted(p.get_filename() for p in m.iter_attachments())
    assert "Daily_sales.pdf" in names
    assert any(n.endswith(".csv") for n in names)
    pdf = next(p for p in m.iter_attachments() if p.get_filename().endswith(".pdf")).get_content()
    assert pdf.startswith(b"%PDF")
    csvs = [p.get_content() for p in m.iter_attachments() if p.get_filename().endswith(".csv")]
    assert any("north" in (c if isinstance(c, str) else c.decode("utf-8-sig")) for c in csvs)

    payload = slack.received[0]
    flat = json.dumps(payload)
    assert "Revenue" in flat and "$550" in flat

    detail = client.get(f"/api/reports/{rep['id']}", headers=_h(project, "eve")).json()
    assert detail["last_status"] == "sent" and detail["deliveries"][0]["trigger"] == "manual"
    log = client.get("/api/audit?action=report_delivery", headers=_h(project, "ada")).json()
    assert log and "status=sent" in log[0]["detail"]


def test_report_runs_as_owner_masking_applies(client, project, dashboard, smtp):
    as_editor = _create(client, project, dashboard, name="editor copy", formats=["csv"],
                        recipients={"email": ["boss@example.com"]})
    as_admin = _create(client, project, dashboard, who="ada", name="admin copy", formats=["csv"],
                       recipients={"email": ["boss@example.com"]})
    # An admin pressing send on the editor's report still gets the editor's view.
    client.post(f"/api/reports/{as_editor['id']}/send", headers=_h(project, "ada"))
    client.post(f"/api/reports/{as_admin['id']}/send", headers=_h(project, "ada"))

    def body(i):
        m = smtp.messages[i]["message"]
        parts = [m.get_body(preferencelist=("html",)).get_content()]
        parts += [p.get_content() for p in m.iter_attachments()]
        return "".join(p if isinstance(p, str) else p.decode("utf-8-sig") for p in parts)

    assert "c1@example.com" not in body(0)
    assert "c1@example.com" in body(1)


def test_owner_deleted_stops_report(client, project, dashboard, smtp):
    rep = _create(client, project, dashboard, recipients={"email": ["boss@example.com"]})
    client.delete("/api/users/eve", headers=_h(project, "ada"))
    d = client.post(f"/api/reports/{rep['id']}/send", headers=_h(project, "ada")).json()
    assert d["status"] == "failed" and "no longer exists" in d["error"]
    assert smtp.messages == []


def test_condition_skips_and_force_sends(client, project, dashboard, smtp):
    rep = _create(client, project, dashboard, recipients={"email": ["boss@example.com"]},
                  condition={"widget_id": dashboard["kpi"], "op": "lt", "value": 100})
    d = client.post(f"/api/reports/{rep['id']}/send", headers=_h(project, "eve")).json()
    assert d["status"] == "skipped" and d["condition_met"] is False
    assert smtp.messages == []
    d = client.post(f"/api/reports/{rep['id']}/send?force=true", headers=_h(project, "eve")).json()
    assert d["status"] == "sent"

    met = _create(client, project, dashboard, name="alert", recipients={"email": ["boss@example.com"]},
                  condition={"widget_id": dashboard["kpi"], "op": "gt", "value": 100})
    d = client.post(f"/api/reports/{met['id']}/send", headers=_h(project, "eve")).json()
    assert d["status"] == "sent" and d["condition_met"] is True
    subject = smtp.messages[-1]["message"]["Subject"]
    assert "revenue is 550" in subject


def test_report_filters_apply(client, project, dashboard, smtp):
    rep = _create(client, project, dashboard, formats=["csv"], widget_id=dashboard["table"],
                  filters={"region": "north"}, recipients={"email": ["boss@example.com"]})
    client.post(f"/api/reports/{rep['id']}/send", headers=_h(project, "eve"))
    m = smtp.messages[0]["message"]
    csvs = [p.get_content() for p in m.iter_attachments()]
    text = "".join(c if isinstance(c, str) else c.decode("utf-8-sig") for c in csvs)
    assert "north" in text and "south" not in text
    assert len(csvs) == 1  # single-widget report


def test_failed_channel_is_partial(client, project, dashboard, smtp):
    bad = FakeSlack(status=500)
    try:
        rep = _create(client, project, dashboard, recipients={"email": ["boss@example.com"], "slack": [bad.url]})
        d = client.post(f"/api/reports/{rep['id']}/send", headers=_h(project, "eve")).json()
    finally:
        bad.close()
    assert d["status"] == "partial"
    assert any(c["status"] == "failed" for c in d["channels"])


def test_missing_smtp_secret_is_reported(client, project, dashboard):
    yml = project / "project.yml"
    yml.write_text(yml.read_text(encoding="utf-8") + "    username: bot\n    password: ${NOT_SET_ANYWHERE}\n",
                   encoding="utf-8")
    from havn.server.deps import _clear_config_cache

    _clear_config_cache()
    rep = _create(client, project, dashboard, recipients={"email": ["boss@example.com"]})
    d = client.post(f"/api/reports/{rep['id']}/send", headers=_h(project, "eve")).json()
    assert d["status"] == "failed" and "NOT_SET_ANYWHERE" in d["error"]


def test_preview_does_not_send(client, project, dashboard, smtp):
    rep = _create(client, project, dashboard)
    p = client.post(f"/api/reports/{rep['id']}/preview", headers=_h(project, "eve")).json()
    assert "<html" in p["html"] and "cid:" not in p["html"]
    assert {a["filename"] for a in p["attachments"]} >= {"Daily_sales.pdf"}
    assert smtp.messages == []


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def test_scheduler_fires_once_per_minute(client, project, dashboard, smtp):
    rep = _create(client, project, dashboard, schedule="30 6 * * *", recipients={"email": ["boss@example.com"]})
    client.post("/api/reports", json={
        "name": "disabled", "dashboard_id": dashboard["id"], "schedule": "30 6 * * *", "enabled": False,
        "recipients": {"email": ["boss@example.com"]},
    }, headers=_h(project, "eve"))
    import havn.server.deps as deps
    from havn.config import load_project
    from havn.engine.reports import run_due_reports
    from havn.engine.write_queue import cursor_for

    cfg = load_project(project)
    cur = cursor_for(deps._get_shared_conn())
    try:
        assert run_due_reports(cur, cfg, dt.datetime(2026, 10, 9, 6, 29)) == []
        fired = run_due_reports(cur, cfg, dt.datetime(2026, 10, 9, 6, 30, 5))
        again = run_due_reports(cur, cfg, dt.datetime(2026, 10, 9, 6, 30, 40))
        next_day = run_due_reports(cur, cfg, dt.datetime(2026, 10, 10, 6, 30, 1))
    finally:
        cur.close()
    assert [d["report_id"] for d in fired] == [rep["id"]]
    assert fired[0]["status"] == "sent" and fired[0]["trigger"] == "schedule"
    assert again == []
    assert len(next_day) == 1
    assert len(smtp.messages) == 2


def test_scheduler_thread_hook(project, dashboard, client, smtp, monkeypatch):
    """SchedulerThread's loop calls the report hook (smoke test of the wiring)."""
    import havn.engine.reports as reports
    from havn.engine.scheduler import SchedulerThread

    called = threading.Event()

    def fake(project_dir, now=None):
        called.set()
        return []

    monkeypatch.setattr(reports, "run_due_reports_for_project", fake)
    t = SchedulerThread(project)
    t.start()
    try:
        assert called.wait(10)
    finally:
        t.stop()
        t.join(5)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _data():
    return {
        "title": "T <script>", "dashboard_name": "D", "generated_at": "2026-10-09T07:00:00",
        "freshness": {"as_of": "2026-10-09T06:00:00"},
        "widgets": [
            {"id": "k", "title": "Rev", "widget_type": "kpi", "config": {}, "columns": ["v"], "rows": [[1234567]],
             "row_count": 1, "kpi": {"label": "Rev", "display": "1.2M", "value": 1234567, "delta_pct": None}},
            {"id": "c", "title": "Chart", "widget_type": "chart", "chart_type": "line", "config": {},
             "columns": ["d", "a", "b"], "rows": [["x", 1, 2], ["y", 3, 4], ["z", 2, 5]], "row_count": 3},
            {"id": "t", "title": "Tbl", "widget_type": "table", "config": {},
             "columns": ["name"], "rows": [["<b>x</b>"]], "row_count": 1},
        ],
    }


def test_html_escapes_values():
    from havn.engine.report_render import render_html

    html = render_html(_data())
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<b>x</b>" not in html and "1.2M" in html


def test_pdf_without_matplotlib(monkeypatch):
    import havn.engine.report_render as rr

    monkeypatch.setattr(rr, "charts_available", lambda setting="auto": False)
    pdf = rr.render_pdf(_data())
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF") and b"Rev: 1.2M" in pdf


def test_charts_when_matplotlib_present():
    from havn.engine.report_render import charts_available, render_dashboard_png, render_pdf, render_widget_png

    if not charts_available():
        pytest.skip("matplotlib not installed (or not importable)")

    data = _data()
    assert render_widget_png(data["widgets"][1]).startswith(b"\x89PNG")
    assert render_widget_png(data["widgets"][2]) is None  # tables are not charts
    assert render_dashboard_png(data).startswith(b"\x89PNG")
    assert render_pdf(data).startswith(b"%PDF")


def test_charts_off_degrades_gracefully(client, project, dashboard, smtp):
    yml = project / "project.yml"
    yml.write_text(yml.read_text(encoding="utf-8").replace("reports:\n", "reports:\n  charts: off\n"),
                   encoding="utf-8")
    from havn.server.deps import _clear_config_cache

    _clear_config_cache()
    rep = _create(client, project, dashboard, recipients={"email": ["boss@example.com"]})
    p = client.post(f"/api/reports/{rep['id']}/preview", headers=_h(project, "eve")).json()
    assert "data:image/png" not in p["html"]
    assert any("No PNG snapshot" in n for n in p["notes"])
    names = {a["filename"] for a in p["attachments"]}
    assert "Daily_sales.pdf" in names and "Daily_sales.png" not in names


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_list_preview_send(project, dashboard, client, smtp, tmp_path):
    rep = _create(client, project, dashboard, recipients={"email": ["boss@example.com"]})
    from havn.server.deps import reset_shared_conn

    reset_shared_conn()  # release the warehouse so the CLI can open it
    from typer.testing import CliRunner

    from havn.cli import app

    runner = CliRunner()
    r = runner.invoke(app, ["reports", "list", "--project", str(project)])
    assert r.exit_code == 0, r.output
    assert "Daily sales" in r.output
    out = tmp_path / "preview.html"
    r = runner.invoke(app, ["reports", "preview", "Daily sales", "--out", str(out), "--project", str(project)])
    assert r.exit_code == 0, r.output
    assert out.exists() and "<html" in out.read_text(encoding="utf-8")
    pdf = tmp_path / "preview.pdf"
    r = runner.invoke(app, ["reports", "preview", rep["id"], "--format", "pdf", "--out", str(pdf), "--project", str(project)])
    assert r.exit_code == 0, r.output
    assert pdf.read_bytes().startswith(b"%PDF")
    assert smtp.messages == []
    r = runner.invoke(app, ["reports", "send", "Daily sales", "--project", str(project)])
    assert r.exit_code == 0, r.output
    assert len(smtp.messages) == 1
    r = runner.invoke(app, ["reports", "send", "nope", "--project", str(project)])
    assert r.exit_code != 0
