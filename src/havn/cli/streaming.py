"""CLI commands for the API poll consumer (havn poll ...)."""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.table import Table

from havn.cli import _load_config, _resolve_project, _warehouse_exists, app, console

logger = logging.getLogger("havn.cli.streaming")

poll_app = typer.Typer(name="poll", help="Manage API poll consumers.", no_args_is_help=True)
app.add_typer(poll_app)

_PIDFILE_DIR = ".havn/streaming"


def _pidfile_path(project_dir: Path, connector: str) -> Path:
    return project_dir / _PIDFILE_DIR / f"{connector}.pid"


def _read_pid(pidfile: Path) -> int | None:
    try:
        return int(pidfile.read_text().strip())
    except Exception:
        return None


def _load_connector_config(project_dir: Path, connector: str, env: str | None) -> dict:
    """Return the polling config dict for *connector* from project.yml.

    ``ConnectionConfig`` stores its parameters in ``.params``; we return that
    dict directly so the consumer receives the same flat structure the rest of
    the connector framework expects.
    """
    config = _load_config(project_dir, env)
    connections = config.connections or {}
    if connector not in connections:
        console.print(
            f"[red]Connector '{connector}' not found in project.yml connections.[/red]"
        )
        raise typer.Exit(1)
    conn_cfg = connections[connector]
    # ConnectionConfig wraps params in a .params dict
    if hasattr(conn_cfg, "params"):
        return conn_cfg.params
    if hasattr(conn_cfg, "model_dump"):
        return conn_cfg.model_dump()
    return dict(conn_cfg) if conn_cfg else {}


@poll_app.command("once")
def poll_once(
    connector: Annotated[str, typer.Option("--connector", "-c", help="Connector name")],
    env: Annotated[Optional[str], typer.Option("--env", "-e", help="Environment")] = None,
    project_dir: Annotated[
        Optional[Path], typer.Option("--project", "-p", help="Project directory")
    ] = None,
) -> None:
    """Run a single poll against a connector and exit.

    Useful for testing connector config and for cron-based scheduling where
    havn is not expected to stay running between polls.
    """
    from havn.engine.streaming.api_poll import APIPollConsumer

    project_dir = _resolve_project(project_dir)
    connector_cfg = _load_connector_config(project_dir, connector, env)

    consumer = APIPollConsumer(connector, connector_cfg, project_dir)
    console.print(f"[bold]Polling[/bold] {connector}...")
    result = consumer.poll_once()

    if result.error:
        console.print(f"[red]Error:[/red] {result.error}")
        raise typer.Exit(1)

    console.print(
        f"  rows inserted: {result.rows_inserted}  "
        f"watermark: {result.new_watermark or '-'}  "
        f"({result.duration_ms}ms)"
    )


@poll_app.command("start")
def poll_start(
    connector: Annotated[str, typer.Option("--connector", "-c", help="Connector name")],
    interval: Annotated[
        int, typer.Option("--interval", help="Poll interval in seconds")
    ] = 60,
    foreground: Annotated[
        bool,
        typer.Option("--foreground/--background", help="Block until Ctrl-C (default: foreground)"),
    ] = True,
    env: Annotated[Optional[str], typer.Option("--env", "-e", help="Environment")] = None,
    project_dir: Annotated[
        Optional[Path], typer.Option("--project", "-p", help="Project directory")
    ] = None,
) -> None:
    """Start polling a connector.

    In foreground mode (default) the command blocks until Ctrl-C.
    In background mode (--background) a pidfile is written to
    ``.havn/streaming/{connector}.pid`` and the process detaches.
    """
    from havn.engine.streaming.api_poll import APIPollConsumer

    project_dir = _resolve_project(project_dir)
    connector_cfg = _load_connector_config(project_dir, connector, env)

    # Respect poll_interval_seconds from connector config when not explicitly set
    # (the CLI --interval flag always wins).
    cfg_interval = int(connector_cfg.get("poll_interval_seconds", interval))
    effective_interval = interval if interval != 60 else cfg_interval

    if not foreground:
        _start_background(project_dir, connector, effective_interval, env)
        return

    consumer = APIPollConsumer(connector, connector_cfg, project_dir)
    stop_event = threading.Event()

    def _handle_sigint(*_: object) -> None:
        console.print("\n[dim]Stopping poll loop...[/dim]")
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_sigint)

    console.print(
        f"[bold]Polling[/bold] {connector} every {effective_interval}s "
        "(Ctrl-C to stop)"
    )
    consumer.run(effective_interval, stop_event)
    console.print("[dim]Poller stopped.[/dim]")


def _start_background(
    project_dir: Path, connector: str, interval: int, env: str | None
) -> None:
    """Fork (Unix) or subprocess-spawn a background poll loop."""
    pidfile = _pidfile_path(project_dir, connector)
    pidfile.parent.mkdir(parents=True, exist_ok=True)

    existing_pid = _read_pid(pidfile)
    if existing_pid is not None:
        # Check if process is actually alive
        try:
            os.kill(existing_pid, 0)
            console.print(
                f"[yellow]Poller for '{connector}' already running "
                f"(pid {existing_pid})[/yellow]"
            )
            return
        except OSError:
            pass  # Process dead — remove stale pidfile

    # On Windows os.fork() is not available; use subprocess instead.
    import sys

    cmd = [
        sys.executable, "-m", "havn",
        "poll", "start",
        "--connector", connector,
        "--interval", str(interval),
        "--foreground",
        "--project", str(project_dir),
    ]
    if env:
        cmd += ["--env", env]

    import subprocess

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pidfile.write_text(str(proc.pid))
    console.print(
        f"[green]Poller for '{connector}' started[/green] "
        f"(pid {proc.pid}, pidfile: {pidfile})"
    )


@poll_app.command("stop")
def poll_stop(
    connector: Annotated[str, typer.Option("--connector", "-c", help="Connector name")],
    project_dir: Annotated[
        Optional[Path], typer.Option("--project", "-p", help="Project directory")
    ] = None,
) -> None:
    """Stop a background poll process by its pidfile."""
    project_dir = _resolve_project(project_dir)
    pidfile = _pidfile_path(project_dir, connector)
    pid = _read_pid(pidfile)

    if pid is None:
        console.print(f"[yellow]No pidfile found for '{connector}'.[/yellow]")
        raise typer.Exit(1)

    try:
        os.kill(pid, signal.SIGTERM)
        pidfile.unlink(missing_ok=True)
        console.print(f"[green]Sent SIGTERM to pid {pid} ({connector})[/green]")
    except OSError as exc:
        console.print(f"[red]Failed to stop pid {pid}: {exc}[/red]")
        pidfile.unlink(missing_ok=True)
        raise typer.Exit(1)


@poll_app.command("status")
def poll_status(
    project_dir: Annotated[
        Optional[Path], typer.Option("--project", "-p", help="Project directory")
    ] = None,
) -> None:
    """List active pollers and their CDC state from _havn.cdc_state."""
    from havn.config import load_project
    from havn.engine.cdc import get_cdc_status
    from havn.engine.database import open_warehouse

    project_dir = _resolve_project(project_dir)
    config = load_project(project_dir)

    if not _warehouse_exists(config, project_dir):
        console.print("[yellow]No warehouse database found.[/yellow]")
        return

    conn = open_warehouse(config, project_dir)
    try:
        entries = get_cdc_status(conn)
    finally:
        conn.close()

    pidfile_dir = project_dir / _PIDFILE_DIR

    tbl = Table(title="API Poll Status")
    tbl.add_column("Connector", style="bold")
    tbl.add_column("Mode")
    tbl.add_column("Watermark")
    tbl.add_column("Last Sync")
    tbl.add_column("Rows Synced", justify="right")
    tbl.add_column("Running")

    if not entries:
        console.print("[yellow]No CDC state recorded yet.[/yellow]")
        return

    for e in entries:
        connector = e["connector"]
        pidfile = pidfile_dir / f"{connector}.pid"
        pid = _read_pid(pidfile)
        running = "-"
        if pid is not None:
            try:
                os.kill(pid, 0)
                running = f"[green]yes (pid {pid})[/green]"
            except OSError:
                running = "[dim]stale[/dim]"

        tbl.add_row(
            connector,
            e["cdc_mode"],
            e["watermark"] or "-",
            e["last_sync_at"][:19] if e["last_sync_at"] else "-",
            str(e["rows_synced"]),
            running,
        )

    console.print(tbl)
