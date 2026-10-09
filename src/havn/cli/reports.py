"""Report commands: list, send and preview scheduled dashboard reports."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from havn.cli import _load_config, _resolve_project, _warehouse_exists, app, console

reports_app = typer.Typer(
    name="reports",
    help="List, send and preview scheduled dashboard reports.",
    no_args_is_help=True,
)
app.add_typer(reports_app)

ProjectOpt = Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory")]
EnvOpt = Annotated[Optional[str], typer.Option("--env", "-e", help="Environment to use")]


def _server(project_dir: Path) -> tuple[str, dict] | None:
    """(base URL, headers) of a running `havn serve` for this project, or None.

    The server holds the warehouse lock, so commands go through its API
    instead. With auth enabled, set HAVN_TOKEN to an API token.
    """
    import json
    import os

    from havn.cli.query import _pid_alive
    from havn.textio import read_project_text

    info_path = project_dir / ".havn" / "serve.json"
    if not info_path.exists():
        return None
    try:
        info = json.loads(read_project_text(info_path))
    except Exception:
        return None
    pid = info.get("pid")
    if pid is not None and not _pid_alive(int(pid)):
        return None
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("HAVN_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif info.get("auth"):
        console.print("[red]The running server has auth enabled: set HAVN_TOKEN to an API token.[/red]")
        raise typer.Exit(1)
    return f"http://{info.get('host', '127.0.0.1')}:{int(info.get('port', 3000))}", headers


def _http(base: str, headers: dict, method: str, path: str) -> tuple[bytes, str]:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(base + path, data=b"{}" if method == "POST" else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310 - local server
            return resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        try:
            import json

            detail = json.loads(e.read().decode("utf-8")).get("detail")
        except Exception:
            detail = str(e)
        console.print(f"[red]{detail}[/red]")
        raise typer.Exit(1)


def _open(project_dir: Path, env: str | None):
    from havn.engine.database import open_warehouse

    config = _load_config(project_dir, env)
    if not _warehouse_exists(config, project_dir):
        console.print("[yellow]No warehouse database found.[/yellow]")
        raise typer.Exit(1)
    try:
        return config, open_warehouse(config, project_dir)
    except Exception as e:
        if "already open" in str(e) or "being used by another process" in str(e):
            console.print(
                "[red]The warehouse is locked by another process.[/red] If havn serve is running, "
                "start it from this project so .havn/serve.json points at it."
            )
            raise typer.Exit(1)
        raise


def _find(conn, name: str) -> dict:
    from havn.engine.reports import find_report

    report = find_report(conn, name)
    if report is None:
        console.print(f"[red]No report named '{name}'.[/red] See [bold]havn reports list[/bold].")
        raise typer.Exit(1)
    return report


def _find_via_server(base: str, headers: dict, name: str) -> dict:
    import json

    body, _ = _http(base, headers, "GET", "/api/reports")
    for r in json.loads(body):
        if r["id"] == name or r["name"].lower() == name.lower():
            return r
    console.print(f"[red]No report named '{name}'.[/red]")
    raise typer.Exit(1)


def _print_list(reports: list[dict]) -> None:
    from rich.table import Table

    if not reports:
        console.print("[dim]No reports yet. Create one from a dashboard's Reports page in the web UI.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Name", no_wrap=True)
    for col in ("Dashboard", "Schedule", "Owner", "Recipients", "Last run", "Status"):
        table.add_column(col)
    for r in reports:
        rec = r.get("recipients") or {}
        n_email, n_slack = len(rec.get("email") or []), len(rec.get("slack") or [])
        who = ", ".join(p for p in (f"{n_email} email" if n_email else "", f"{n_slack} Slack" if n_slack else "") if p)
        status = r.get("last_status") or "-"
        colour = {"sent": "green", "skipped": "yellow", "failed": "red", "partial": "yellow"}.get(status, "dim")
        table.add_row(
            r["name"],
            r.get("dashboard_name") or r["dashboard_id"],
            (r.get("schedule") or "manual") + ("" if r.get("enabled", True) else " (off)"),
            r.get("owner") or "",
            who or "none",
            (r.get("last_run_at") or "-")[:16].replace("T", " "),
            f"[{colour}]{status}[/{colour}]",
        )
    console.print(table)


@reports_app.command("list")
def list_cmd(project_dir: ProjectOpt = None, env: EnvOpt = None) -> None:
    """List reports with their schedule, recipients and last delivery."""
    import json

    project_dir = _resolve_project(project_dir)
    server = _server(project_dir)
    if server:
        body, _ = _http(*server, "GET", "/api/reports")
        _print_list(json.loads(body))
        return
    from havn.engine.reports import list_reports

    _config, conn = _open(project_dir, env)
    try:
        reports = list_reports(conn)
        for r in reports:
            row = conn.execute("SELECT name FROM _havn.dashboards WHERE id = ?", [r["dashboard_id"]]).fetchone()
            r["dashboard_name"] = row[0] if row else None
    finally:
        conn.close()
    _print_list(reports)


def _print_delivery(d: dict, name: str) -> None:
    status = d["status"]
    if status == "sent":
        console.print(f"[green]Sent[/green] '{name}'")
    elif status == "skipped":
        cond = (d.get("summary") or {}).get("condition") or "its condition was not met"
        console.print(f"[yellow]Skipped[/yellow] '{name}': {cond}. Use --force to send anyway.")
    else:
        console.print(f"[red]{status.capitalize()}[/red] '{name}': {d.get('error') or ''}")
    for c in d.get("channels") or []:
        mark = "[green]ok[/green]" if c["status"] == "sent" else f"[red]{c.get('error', 'failed')}[/red]"
        console.print(f"  {c['channel']} {c['target']}: {mark}")
    if status in ("failed",) and not d.get("channels"):
        raise typer.Exit(1)
    if status in ("failed", "partial"):
        raise typer.Exit(1)


@reports_app.command("send")
def send_cmd(
    name: Annotated[str, typer.Argument(help="Report name or id")],
    force: Annotated[bool, typer.Option("--force", help="Send even if the report's condition is not met")] = False,
    project_dir: ProjectOpt = None,
    env: EnvOpt = None,
) -> None:
    """Send a report now, to its configured recipients, as its owner."""
    import json

    project_dir = _resolve_project(project_dir)
    server = _server(project_dir)
    if server:
        report = _find_via_server(*server, name)
        body, _ = _http(*server, "POST", f"/api/reports/{report['id']}/send?force={'true' if force else 'false'}")
        _print_delivery(json.loads(body), report["name"])
        return
    from havn.engine.reports import run_report

    config, conn = _open(project_dir, env)
    try:
        report = _find(conn, name)
        delivery = run_report(conn, report, config, trigger="cli", force=force)
    finally:
        conn.close()
    _print_delivery(delivery, report["name"])


@reports_app.command("preview")
def preview_cmd(
    name: Annotated[str, typer.Argument(help="Report name or id")],
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="File to write (default: <name>.<format>)")] = None,
    fmt: Annotated[str, typer.Option("--format", "-f", help="html, pdf or png")] = "html",
    project_dir: ProjectOpt = None,
    env: EnvOpt = None,
) -> None:
    """Render a report to a local file without sending it."""
    project_dir = _resolve_project(project_dir)
    fmt = fmt.lower()
    if fmt not in ("html", "pdf", "png"):
        console.print("[red]--format must be html, pdf or png[/red]")
        raise typer.Exit(2)
    server = _server(project_dir)
    if server:
        report = _find_via_server(*server, name)
        content, _ = _http(*server, "GET", f"/api/reports/{report['id']}/render?format={fmt}")
    else:
        from havn.engine.reports import ReportError, render_file

        config, conn = _open(project_dir, env)
        try:
            report = _find(conn, name)
            try:
                content, _media, _filename = render_file(conn, report, config, fmt)
            except ReportError as e:
                console.print(f"[red]{e}[/red]")
                raise typer.Exit(1)
        finally:
            conn.close()
    from havn.engine.report_render import safe_filename

    target = out or Path(f"{safe_filename(report['name'], 'report')}.{fmt}")
    target.write_bytes(content)
    console.print(f"Wrote {target} ({len(content):,} bytes). Nothing was sent.")
