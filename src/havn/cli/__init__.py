"""CLI interface for the data platform.

Split into modules by command group for maintainability.
The Typer app and shared helpers live here; each module registers its commands.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

# Force UTF-8 for stdout/stderr so non-ASCII data (Norwegian, German, Asian
# scripts, emoji) renders correctly on Windows consoles where the default is
# cp1252. This must run before Console() is instantiated, otherwise rich
# captures the wrong encoding.
if sys.platform == "win32":
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass

def _version_callback(value: bool) -> None:
    if value:
        from havn import __version__
        print(f"havn {__version__}")
        raise typer.Exit()


app = typer.Typer(
    name="havn",
    help="havn — self-hosted data platform. Data in safe waters.",
    no_args_is_help=True,
)
console = Console()


@app.callback(invoke_without_command=True)
def _main(
    version: bool = typer.Option(False, "--version", "-V", callback=_version_callback, is_eager=True, help="Show version and exit."),
) -> None:
    """havn — self-hosted data platform. Data in safe waters."""

# Global environment override, set by --env on commands that support it.
_active_env: str | None = None


def _resolve_project(project_dir: Path | None = None) -> Path:
    project_dir = project_dir or Path.cwd()
    if not (project_dir / "project.yml").exists():
        console.print(f"[red]No project.yml found in {project_dir}[/red]")
        console.print("Run [bold]havn init[/bold] to create a new project.")
        raise typer.Exit(1)
    return project_dir


def _load_config(project_dir: Path, env: str | None = None):
    """Load project config with optional environment override."""
    from havn.config import load_project
    return load_project(project_dir, env=env or _active_env)


def _warehouse_exists(config, project_dir: Path) -> bool:
    """Return True if the configured warehouse has been initialized.

    Backend-aware replacement for checking `db_path.exists()`.
    """
    from havn.engine.backends import create_backend
    return create_backend(config.database, project_dir=project_dir).exists()


# Import submodules so they register their commands on `app`.
# Order doesn't matter for registration, but keep alphabetical for clarity.
from havn.cli import admin  # noqa: E402, F401
from havn.cli import connectors  # noqa: E402, F401
from havn.cli import diff  # noqa: E402, F401
from havn.cli import env  # noqa: E402, F401
from havn.cli.flight import app as flight_app  # noqa: E402
from havn.cli import jobs  # noqa: E402, F401
from havn.cli import macros  # noqa: E402, F401
from havn.cli import migrate  # noqa: E402, F401
from havn.cli import models  # noqa: E402, F401
from havn.cli import pipeline  # noqa: E402, F401
from havn.cli import pr  # noqa: E402, F401
from havn.cli import project  # noqa: E402, F401
from havn.cli import quality  # noqa: E402, F401
from havn.cli import query  # noqa: E402, F401
from havn.cli import shell  # noqa: E402, F401
from havn.cli import masking  # noqa: E402, F401
from havn.cli import rewind  # noqa: E402, F401
from havn.cli import sentinel  # noqa: E402, F401
from havn.cli import streaming  # noqa: E402, F401
from havn.cli import version  # noqa: E402, F401

app.add_typer(flight_app, name="flight")
