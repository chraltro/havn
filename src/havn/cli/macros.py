"""Macros command: list registered Python and SQL macros."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.table import Table

from havn.cli import _resolve_project, app, console


@app.command()
def macros(
    project_dir: Annotated[
        Optional[Path],
        typer.Option("--project", "-p", help="Project directory (default: current dir)"),
    ] = None,
) -> None:
    """List all registered Python and SQL macros."""
    from havn.engine.macros import list_macros

    project_dir = _resolve_project(project_dir)
    items = list_macros(project_dir)

    if not items:
        console.print("[yellow]No macros found. Create a macros/ directory with Python or SQL files.[/yellow]")
        return

    table = Table(title="Registered Macros")
    table.add_column("Name", style="bold")
    table.add_column("Kind", style="cyan")
    table.add_column("Origin")
    table.add_column("Parameters")
    table.add_column("Returns")
    table.add_column("Source")
    table.add_column("Description", max_width=50)

    _KIND_LABELS = {"scalar": "Scalar", "table": "Table", "sql": "SQL"}
    _KIND_STYLES = {"scalar": "green", "table": "magenta", "sql": "cyan"}

    stdlib_count = 0
    user_count = 0
    for m in items:
        params_str = ", ".join(
            f"{p['name']}: {p['type']}" for p in m.get("params", [])
        ) if m.get("params") else ""
        # Stdlib entries carry a synthetic source label like
        # ``<havn.stdlib.pii>``; show that as-is, otherwise the file name.
        raw_src = m.get("source_file", "") or ""
        if raw_src.startswith("<havn.stdlib"):
            source = raw_src
        else:
            source = Path(raw_src).name if raw_src else ""
        kind = m.get("kind", "")
        label = _KIND_LABELS.get(kind, kind)
        style = _KIND_STYLES.get(kind, "")
        kind_cell = f"[{style}]{label}[/{style}]" if style else label
        is_stdlib = bool(m.get("is_stdlib"))
        origin = "[blue]stdlib[/blue]" if is_stdlib else "user"
        if is_stdlib:
            stdlib_count += 1
        else:
            user_count += 1
        table.add_row(
            m["name"],
            kind_cell,
            origin,
            params_str,
            m.get("return_type", ""),
            source,
            m.get("docstring", ""),
        )

    console.print(table)
    summary = f"{len(items)} macro(s)"
    if stdlib_count or user_count:
        summary += f" — {stdlib_count} stdlib, {user_count} user"
    console.print(f"[dim]{summary}[/dim]")
