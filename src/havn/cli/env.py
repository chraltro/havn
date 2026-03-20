"""Environment management commands: havn env list|use|show|reset."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.table import Table

from havn.cli import _load_config, _resolve_project, app, console

ENV_FILE = ".havn-env"


def _read_env_file(project_dir: Path) -> str | None:
    """Read the active environment from .havn-env file, or None if not set."""
    env_path = project_dir / ENV_FILE
    if env_path.exists():
        content = env_path.read_text().strip()
        return content if content else None
    return None


def _write_env_file(project_dir: Path, env_name: str) -> None:
    """Write the active environment to .havn-env file."""
    env_path = project_dir / ENV_FILE
    env_path.write_text(env_name + "\n")


def _delete_env_file(project_dir: Path) -> None:
    """Delete the .havn-env file if it exists."""
    env_path = project_dir / ENV_FILE
    if env_path.exists():
        env_path.unlink()


@app.command("env")
def env(
    action: Annotated[str, typer.Argument(help="Action: list, use, show, or reset")] = "show",
    name: Annotated[Optional[str], typer.Argument(help="Environment name (for 'use' action)")] = None,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory")] = None,
) -> None:
    """Manage active environment. Removes the need to type --env on every command.

    Actions:
      list     Show all environments defined in project.yml
      use      Set the active environment (writes .havn-env file)
      show     Show the currently active environment (default action)
      reset    Clear active environment (deletes .havn-env file)
    """
    import yaml

    project_dir = _resolve_project(project_dir)

    if action == "list":
        config_path = project_dir / "project.yml"
        raw = yaml.safe_load(config_path.read_text()) or {}
        environments = raw.get("environments", {})

        if not environments:
            console.print("[dim]No environments defined in project.yml.[/dim]")
            console.print(
                "\nAdd an [bold]environments:[/bold] section to project.yml to define environments."
            )
            return

        active = _read_env_file(project_dir)

        tbl = Table(title="Environments")
        tbl.add_column("", width=2)
        tbl.add_column("Name", style="bold")
        tbl.add_column("Database")
        tbl.add_column("Connections")

        for env_name, env_raw in environments.items():
            is_active = env_name == active
            marker = "[green]*[/green]" if is_active else ""
            db_path = env_raw.get("database", {}).get("path", "")
            conns = ", ".join(env_raw.get("connections", {}).keys()) or "-"
            tbl.add_row(marker, env_name, db_path or "-", conns)

        console.print(tbl)
        if active:
            console.print(f"\n[dim]Active environment: [bold]{active}[/bold] (from .havn-env)[/dim]")
        else:
            console.print("\n[dim]No active environment set. Use [bold]havn env use <name>[/bold] to set one.[/dim]")

    elif action == "use":
        if not name:
            console.print("[red]Please specify an environment name: havn env use <name>[/red]")
            raise typer.Exit(1)

        # Validate the environment exists in project.yml
        config_path = project_dir / "project.yml"
        raw = yaml.safe_load(config_path.read_text()) or {}
        environments = raw.get("environments", {})

        if name not in environments:
            console.print(f"[red]Environment '{name}' not found in project.yml.[/red]")
            if environments:
                available = ", ".join(environments.keys())
                console.print(f"Available environments: {available}")
            else:
                console.print("No environments defined in project.yml.")
            raise typer.Exit(1)

        _write_env_file(project_dir, name)
        console.print(f"[green]Active environment set to [bold]{name}[/bold][/green]")

    elif action == "show":
        active = _read_env_file(project_dir)
        if active:
            console.print(f"Active environment: [bold]{active}[/bold]")
        else:
            console.print("Active environment: [bold]default[/bold]")
            console.print("[dim]No .havn-env file. Use [bold]havn env use <name>[/bold] to set one.[/dim]")

    elif action == "reset":
        env_path = project_dir / ENV_FILE
        if env_path.exists():
            old = _read_env_file(project_dir)
            _delete_env_file(project_dir)
            console.print(f"[green]Cleared active environment (was: {old}).[/green]")
        else:
            console.print("[dim]No active environment to clear.[/dim]")

    else:
        console.print(f"[red]Unknown action: {action}[/red]")
        console.print("Available: list, use, show, reset")
        raise typer.Exit(1)
