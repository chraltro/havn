"""CLI: ``havn rename-column``, renaming a column across downstream models."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from havn.cli import _load_config, _resolve_project, _warehouse_exists, app, console


def _schemas(project_dir: Path, config, models) -> dict[str, list[tuple[str, str]]]:
    """Column names per model: what the last build recorded, else the SQL.

    The bind pass would be more accurate, but the rename only needs names and
    a bind costs a shadow catalog for every model in the project.
    """
    from havn.engine.rename import schemas_from_models
    from havn.engine.transform.columns import load_model_columns

    found = schemas_from_models(models)
    if not _warehouse_exists(config, project_dir):
        return found
    from havn.engine.database import connect

    conn = connect(project_dir / config.database.path)
    try:
        for model in models:
            persisted = load_model_columns(conn, model.full_name)
            if persisted:
                found[model.full_name] = [
                    (c["name"], c.get("type", "")) for c in persisted
                ]
    except Exception:
        pass
    finally:
        conn.close()
    return found


@app.command("rename-column")
def rename_column(
    model: Annotated[str, typer.Argument(help="Model that defines the column, e.g. silver.customers")],
    column: Annotated[str, typer.Argument(help="Current column name")],
    new_name: Annotated[str, typer.Argument(help="New column name")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the edits and write nothing")] = False,
    force: Annotated[bool, typer.Option("--force", help="Rename even though there are blockers")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask before writing")] = False,
    env: Annotated[Optional[str], typer.Option("--env", "-e", help="Environment to use")] = None,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """Rename a column in the model that defines it and everywhere it is read.

    The column is followed downstream only where it keeps its name: a model
    that re-aliases it ends the chain, and that is reported rather than
    guessed at. So is anything the index cannot see through, such as a
    downstream ``SELECT *``, which yields no identifier to rewrite.

    Nothing is written until every edit has been checked back against the
    file it belongs to, and a failure part way through puts every file back.
    """
    from rich.table import Table

    from havn.engine.rename import (
        RenameError,
        apply_rename,
        find_column_references,
        plan_rename,
    )
    from havn.engine.transform import discover_all_models

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir, env)
    # The whole project, packages included. A package model that reads the
    # renamed column is never edited -- the index reports it as a blocker --
    # but it has to be visible, or the CLI would rename the project and leave
    # the package reading a column that no longer exists.
    models = discover_all_models(project_dir, config)
    schemas = _schemas(project_dir, config, models)

    try:
        report = find_column_references(
            models, model, column, schemas=schemas, project_dir=project_dir
        )
    except RenameError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if report.sites:
        table = Table(title=f"{report.target}.{report.column}", show_header=True)
        table.add_column("file")
        table.add_column("line", justify="right")
        table.add_column("kind")
        table.add_column("clause")
        for site in report.sites:
            kind = site.kind if site.resolved or site.kind == "yaml" else f"{site.kind}?"
            table.add_row(site.path, str(site.line), kind, site.clause)
        console.print(table)
    else:
        console.print(f"[yellow]Nothing writes {model}.{column}[/yellow]")

    for blocker in report.blocked:
        console.print(f"  [yellow]blocked[/yellow]  {blocker.path}: {blocker.message}")

    try:
        edits = plan_rename(
            report, column, new_name, force=force, schemas=schemas
        )
    except RenameError as e:
        console.print(f"[red]{e}[/red]")
        if report.blocked and not force:
            console.print("Pass [bold]--force[/bold] to rename the places it can see.")
        raise typer.Exit(1)

    if not edits:
        console.print("[yellow]No edits to make.[/yellow]")
        raise typer.Exit(1)

    files = sorted({e.path for e in edits})
    console.print(
        f"\n{len(edits)} edit{'' if len(edits) == 1 else 's'} in "
        f"{len(files)} file{'' if len(files) == 1 else 's'}: "
        f"[bold]{column}[/bold] to [bold]{new_name}[/bold]"
    )

    if dry_run:
        try:
            apply_rename(project_dir, edits, dry_run=True)
        except RenameError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        console.print("[dim]--dry-run: nothing written.[/dim]")
        return

    if not yes and not typer.confirm(f"Rewrite {len(files)} file(s)?"):
        console.print("[dim]Cancelled.[/dim]")
        raise typer.Exit(1)

    try:
        apply_rename(project_dir, edits)
    except RenameError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    for path in files:
        console.print(f"  [green]rewrote[/green]  {path}")
    console.print("\nRun [bold]havn validate[/bold] to check the result.")
