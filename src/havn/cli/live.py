"""Live models: ``havn live`` and its subcommands.

``havn live`` runs the live runner in the foreground with a status display,
for a project that is not being served (``havn serve`` already runs one).
``status``, ``pause``, ``resume`` and ``advance`` talk to a running server
when there is one -- it holds the warehouse open -- and to the warehouse
directly otherwise.
"""

from __future__ import annotations

from havn.textio import read_project_text

import json
import time
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.table import Table

from havn.cli import _load_config, _resolve_project, app, console

live_app = typer.Typer(
    name="live",
    help=(
        "Live models: continuous refresh from streaming ingest to gold. "
        "Run `havn live` on its own to start the runner in the foreground."
    ),
    invoke_without_command=True,
)
app.add_typer(live_app)

_EnvOpt = Annotated[Optional[str], typer.Option("--env", "-e", help="Environment to use")]
_ProjectOpt = Annotated[
    Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")
]

_STATUS_STYLE = {
    "live": "green", "behind": "yellow", "waiting": "cyan", "failing": "red", "paused": "magenta",
}


# ---------------------------------------------------------------------------
# Talking to a running server
# ---------------------------------------------------------------------------


def _server(project_dir: Path) -> tuple[str, int] | None:
    """(host, port) of a live ``havn serve`` for this project, or None."""
    from havn.cli.query import _pid_alive

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
    return info.get("host", "127.0.0.1"), int(info.get("port", 3000))


def _foreground_runner_pid(project_dir: Path) -> int | None:
    from havn.cli.query import _pid_alive

    path = project_dir / ".havn" / "live.json"
    if not path.exists():
        return None
    try:
        pid = int(json.loads(read_project_text(path)).get("pid"))
    except Exception:
        return None
    return pid if _pid_alive(pid) else None


def _http(server: tuple[str, int], method: str, path: str) -> dict:
    import urllib.error
    import urllib.request

    host, port = server
    req = urllib.request.Request(f"http://{host}:{port}{path}", method=method,
                                 data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail")
        except Exception:
            detail = str(e)
        console.print(f"[red]{detail}[/red]")
        raise typer.Exit(1)
    except (urllib.error.URLError, OSError) as e:
        console.print(f"[red]Could not reach the havn server at {host}:{port}: {e}[/red]")
        raise typer.Exit(1)


def _refuse_if_held(project_dir: Path) -> None:
    pid = _foreground_runner_pid(project_dir)
    if pid is not None:
        console.print(
            f"[yellow]A `havn live` runner (pid {pid}) has the warehouse open; "
            "its status display is in that terminal.[/yellow]"
        )
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_lag(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms" if seconds else "0s"
    if seconds < 120:
        return f"{seconds:.1f}s"
    return f"{seconds / 60:.1f}m"


def _fmt_ago(iso: str | None) -> str:
    if not iso:
        return "never"
    from datetime import datetime, timezone

    try:
        then = datetime.fromisoformat(iso.rstrip("Z")).replace(tzinfo=timezone.utc)
    except ValueError:
        return iso
    secs = (datetime.now(timezone.utc) - then).total_seconds()
    return f"{_fmt_lag(max(secs, 0))} ago"


def render_status(status: dict) -> Table:
    runner = status.get("runner") or {}
    title = "Live models"
    if runner.get("running"):
        title += f"  [green]runner up[/green] · {runner.get('refreshes', 0)} refreshes · {runner.get('cycles', 0)} cycles"
    else:
        title += "  [dim]runner not running[/dim]"
    table = Table(title=title, title_justify="left", expand=False)
    table.add_column("Model", style="bold")
    table.add_column("Kind", style="dim")
    table.add_column("Status")
    table.add_column("Lag", justify="right")
    table.add_column("Events/s", justify="right")
    table.add_column("Last refresh")
    table.add_column("Refreshes", justify="right")
    table.add_column("Detail", overflow="fold", max_width=60)
    for m in status.get("models", []):
        st = m.get("status", "live")
        style = _STATUS_STYLE.get(st, "white")
        detail = ""
        if st == "failing":
            detail = (m.get("last_error") or "")[:200]
            if m.get("next_retry_at"):
                detail += f" (retry {m['next_retry_at']})"
        elif st == "waiting":
            detail = f"waiting on {m.get('waiting_on')}"
        elif m.get("refreshing"):
            detail = "refreshing..."
        kind = m.get("materialized", "")
        if m.get("strategy"):
            kind += f"/{m['strategy']}"
        if m.get("cdc"):
            kind += " cdc"
        table.add_row(
            m["model"], kind, f"[{style}]{st}[/{style}]", _fmt_lag(m.get("lag_seconds")),
            f"{m.get('events_per_second', 0):g}", _fmt_ago(m.get("last_refresh_at")),
            str(m.get("refreshes", 0)), detail,
        )
    if not status.get("models"):
        table.add_row("[dim]no live models (add live=true to a model's @config)[/dim]", "", "", "", "", "", "", "")
    return table


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@live_app.callback()
def live_main(
    ctx: typer.Context,
    once: Annotated[bool, typer.Option("--once", help="Run one refresh cycle and exit")] = False,
    env: _EnvOpt = None,
    project_dir: _ProjectOpt = None,
) -> None:
    """Run the live runner in the foreground with a status display (Ctrl-C to stop)."""
    if ctx.invoked_subcommand is not None:
        return
    from havn.engine.backends import create_backend
    from havn.engine.database import ensure_meta_table
    from havn.engine.live.runner import LiveRunner
    from havn.engine.live.settings import LiveSettings
    from havn.engine.write_queue import WriteQueue

    project_dir = _resolve_project(project_dir)
    if _server(project_dir) is not None:
        console.print(
            "[yellow]havn serve is running for this project and already runs the live "
            "runner. See Observe > Live in the web UI, or `havn live status`.[/yellow]"
        )
        raise typer.Exit(1)
    _refuse_if_held(project_dir)
    config = _load_config(project_dir, env)
    try:
        settings = LiveSettings.from_raw(config.live)
    except ValueError as e:
        console.print(f"[red]live: in project.yml: {e}[/red]")
        raise typer.Exit(1)

    backend = create_backend(config.database, project_dir=project_dir)
    try:
        wq = WriteQueue(backend)
    except Exception as e:
        console.print(f"[red]Could not open the warehouse for writing: {e}[/red]")
        raise typer.Exit(1)
    ensure_meta_table(wq.conn)
    runner = LiveRunner(project_dir, wq, settings=settings, alerts=config.alerts)

    if once:
        try:
            runner._load_models(force=True)
            runner._call(runner._load_states)
            results = runner.run_cycle()
            runner._flush_logs(force=True)
            for r in results:
                color = {"built": "green", "error": "red", "assertion_failed": "red"}.get(r.status, "dim")
                extra = f" ({r.events} events, {r.duration_ms}ms)" if r.status == "built" else ""
                if r.error:
                    extra = f": {r.error}"
                console.print(f"  [{color}]{r.status}[/{color}]  {r.model}{extra}")
            console.print(render_status(runner.status()))
            failed = any(r.status in ("error", "assertion_failed") for r in results)
        finally:
            wq.close()
        raise typer.Exit(1 if failed else 0)

    marker = project_dir / ".havn" / "live.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    import os

    marker.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    from rich.live import Live

    runner.start()
    if not runner.graph.order:
        console.print("[yellow]No live models yet; add live=true to a model's @config. "
                      "Watching transform/ for changes.[/yellow]")
    try:
        with Live(render_status(runner.status()), console=console, refresh_per_second=2) as display:
            while True:
                time.sleep(1.0)
                try:
                    display.update(render_status(runner.status()))
                except Exception as e:  # keep the display up through a bad read
                    console.print(f"[dim]status read failed: {e}[/dim]")
    except KeyboardInterrupt:
        console.print("[dim]Stopping the live runner...[/dim]")
    finally:
        runner.stop()
        wq.close()
        try:
            marker.unlink()
        except OSError:
            pass


@live_app.command("status")
def live_status_cmd(
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON")] = False,
    env: _EnvOpt = None,
    project_dir: _ProjectOpt = None,
) -> None:
    """Show live models: status, lag, events per second, last refresh."""
    project_dir = _resolve_project(project_dir)
    server = _server(project_dir)
    if server is not None:
        status = _http(server, "GET", "/api/live/status")
    else:
        _refuse_if_held(project_dir)
        from havn.engine.database import open_warehouse
        from havn.engine.live.settings import LiveSettings
        from havn.engine.live.status import live_status
        from havn.engine.transform.discovery import discover_all_models

        config = _load_config(project_dir, env)
        models = discover_all_models(project_dir)
        try:
            conn = open_warehouse(config, project_dir, read_only=True)
        except Exception as e:
            console.print(f"[red]Could not open the warehouse: {e}[/red]")
            raise typer.Exit(1)
        try:
            status = live_status(conn, models, settings=LiveSettings.from_raw(config.live))
        finally:
            conn.close()
    if json_output:
        console.print_json(json.dumps(status, default=str))
        return
    console.print(render_status(status))
    sources = status.get("sources") or []
    if sources:
        table = Table(title="Sources", title_justify="left")
        table.add_column("Source", style="bold")
        table.add_column("Kind", style="dim")
        table.add_column("Watermark", justify="right")
        table.add_column("Rows", justify="right")
        table.add_column("Events/s", justify="right")
        table.add_column("Last advance")
        for s in sources:
            table.add_row(s["source"], s["kind"], str(s["watermark"]), str(s["rows_total"]),
                          f"{s['events_per_second']:g}", _fmt_ago(s.get("advanced_at")))
        console.print(table)


def _set_paused(model: str, paused: bool, env: str | None, project_dir: Path | None) -> None:
    project_dir = _resolve_project(project_dir)
    verb = "pause" if paused else "resume"
    server = _server(project_dir)
    if server is not None:
        _http(server, "POST", f"/api/live/models/{model}/{verb}")
    else:
        _refuse_if_held(project_dir)
        from havn.engine.database import open_warehouse
        from havn.engine.live.graph import LiveGraph
        from havn.engine.live.state import set_paused
        from havn.engine.transform.discovery import discover_all_models

        if model.lower() not in LiveGraph.build(discover_all_models(project_dir)).order:
            console.print(f"[red]{model} is not a live model[/red]")
            raise typer.Exit(1)
        conn = open_warehouse(_load_config(project_dir, env), project_dir)
        try:
            set_paused(conn, model.lower(), paused)
        finally:
            conn.close()
    console.print(f"[green]{model.lower()} {'paused' if paused else 'resumed'}[/green]")


@live_app.command("pause")
def live_pause(
    model: Annotated[str, typer.Argument(help="Live model, e.g. silver.orders")],
    env: _EnvOpt = None,
    project_dir: _ProjectOpt = None,
) -> None:
    """Stop refreshing a live model (its downstream live models wait)."""
    _set_paused(model, True, env, project_dir)


@live_app.command("resume")
def live_resume(
    model: Annotated[str, typer.Argument(help="Live model, e.g. silver.orders")],
    env: _EnvOpt = None,
    project_dir: _ProjectOpt = None,
) -> None:
    """Resume a paused (or failing) live model; it catches up on everything queued."""
    _set_paused(model, False, env, project_dir)


@live_app.command("advance")
def live_advance(
    source: Annotated[str, typer.Argument(help="Landing table, e.g. landing.orders")],
    env: _EnvOpt = None,
    project_dir: _ProjectOpt = None,
) -> None:
    """Stamp rows committed to a landing table and announce them to live models."""
    project_dir = _resolve_project(project_dir)
    server = _server(project_dir)
    if server is not None:
        result = _http(server, "POST", f"/api/live/sources/{source}/advance")
        rows = result.get("rows", 0)
    else:
        _refuse_if_held(project_dir)
        from havn.engine.database import open_warehouse
        from havn.engine.live.sources import advance_source

        conn = open_warehouse(_load_config(project_dir, env), project_dir)
        try:
            adv = advance_source(conn, source)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        finally:
            conn.close()
        rows = adv.rows if adv else 0
    if rows:
        console.print(f"[green]{source.lower()}[/green] advanced by {rows} row(s)")
    else:
        console.print(f"[dim]{source.lower()}: nothing new to advance[/dim]")
