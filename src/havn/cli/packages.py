"""Packages commands: install, list and remove shared model/macro packages."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from havn.cli import _load_config, _resolve_project, app, console

packages_app = typer.Typer(
    name="packages",
    help="Install and inspect shared model and macro packages.",
    no_args_is_help=False,
)
app.add_typer(packages_app)


def _counts(root) -> tuple[int, int]:
    """Models and macros a package contributes, for the listing."""
    from havn.engine.macros import _discover_all_python_macros, _discover_sql_macros
    from havn.engine.transform import discover_package_models

    try:
        models = len(discover_package_models(root))
    except Exception:
        models = 0
    scalars, tables = _discover_all_python_macros(root.macros_dir, package=root.name)
    macros = len(scalars) + len(tables) + len(_discover_sql_macros(root.macros_dir))
    return models, macros


@packages_app.callback(invoke_without_command=True)
def list_packages(
    ctx: typer.Context,
    project_dir: Annotated[
        Optional[Path], typer.Option("--project", "-p", help="Project directory")
    ] = None,
) -> None:
    """List installed packages."""
    if ctx.invoked_subcommand is not None:
        return
    from rich.table import Table

    from havn.engine.packages import package_roots, read_lock

    project_dir = _resolve_project(project_dir)
    lock = read_lock(project_dir)
    roots = package_roots(project_dir)
    if not roots:
        if lock:
            console.print(
                "[yellow]Packages are locked but not installed. "
                "Run [bold]havn packages install[/bold].[/yellow]"
            )
        else:
            console.print(
                "[dim]No packages installed. Add a packages: block to "
                "project.yml, then run havn packages install.[/dim]"
            )
        return

    table = Table(title="Installed Packages")
    table.add_column("Name", style="bold")
    table.add_column("Source")
    table.add_column("Rev")
    table.add_column("Commit")
    table.add_column("Version")
    table.add_column("Models", justify="right")
    table.add_column("Macros", justify="right")

    for root in roots:
        entry = lock.get(root.name)
        source = entry.source if entry else "?"
        rev = (entry.rev or entry.path) if entry else ""
        commit = (entry.commit[:12] if entry and entry.commit else "")
        models, macros = _counts(root)
        table.add_row(
            root.name,
            source,
            rev,
            commit,
            root.manifest.version,
            str(models),
            str(macros),
        )
    console.print(table)


@packages_app.command("install")
def install(
    upgrade: Annotated[
        bool,
        typer.Option(
            "--upgrade",
            help="Re-resolve each rev instead of reusing the locked commit.",
        ),
    ] = False,
    project_dir: Annotated[
        Optional[Path], typer.Option("--project", "-p", help="Project directory")
    ] = None,
) -> None:
    """Install packages declared in project.yml into havn_packages/."""
    from havn.engine.packages import install_packages

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir)
    if not config.packages:
        console.print("[dim]No packages: block in project.yml; nothing to install.[/dim]")
        return

    results = install_packages(project_dir, config, upgrade=upgrade)
    failed = 0
    for result in results:
        if result.status == "error":
            failed += 1
            console.print(f"  [red]fail[/red]  {result.name}: {result.message}")
            continue
        detail = result.commit[:12] if result.commit else result.ref
        label = "[dim]unchanged[/dim]" if result.status == "unchanged" else "[green]ok[/green]"
        console.print(f"  {label}  {result.name} [dim]{detail}[/dim]")
        for warning in result.warnings:
            console.print(f"        [yellow]{warning}[/yellow]")

    console.print(
        f"[dim]{len(results) - failed}/{len(results)} package(s) installed; "
        "lock written to havn_packages.lock[/dim]"
    )
    if failed:
        raise typer.Exit(1)


@packages_app.command("remove")
def remove(
    name: Annotated[str, typer.Argument(help="Package name")],
    project_dir: Annotated[
        Optional[Path], typer.Option("--project", "-p", help="Project directory")
    ] = None,
) -> None:
    """Delete an installed package and drop it from the lock file."""
    from havn.engine.packages import remove_package

    project_dir = _resolve_project(project_dir)
    try:
        removed = remove_package(project_dir, name)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if not removed:
        console.print(f"[yellow]Package '{name}' is not installed.[/yellow]")
        raise typer.Exit(1)
    console.print(f"[green]Removed[/green] {name}")
    console.print(
        "[dim]Its packages: entry in project.yml is untouched; remove it there "
        "too or the next install brings it back.[/dim]"
    )
