"""CLI: ``havn flight`` — start the Arrow Flight SQL server."""

from __future__ import annotations

import os

import typer

app = typer.Typer(help="Arrow Flight SQL server (port 50051 by default)")


@app.callback(invoke_without_command=True)
def flight(
    host: str = typer.Option("0.0.0.0", help="Bind host"),
    port: int = typer.Option(50051, help="Bind port"),
    token: str | None = typer.Option(
        None,
        "--token",
        help="Require this token for Bearer/Basic auth. "
             "Defaults to $HAVN_FLIGHT_TOKEN; unset disables auth.",
    ),
) -> None:
    """Start the Flight SQL server against the active project warehouse."""
    from rich.console import Console

    from havn.server.flight import serve_flight

    console = Console()
    effective_token = token or os.environ.get("HAVN_FLIGHT_TOKEN")
    if effective_token is None:
        console.print("[yellow]Warning:[/yellow] no token set — Flight server is unauthenticated")
    else:
        console.print("[green]Flight server auth:[/green] enabled")

    console.print(f"[bold]Listening on grpc://{host}:{port}[/bold]")
    serve_flight(host=host, port=port, token=effective_token)
