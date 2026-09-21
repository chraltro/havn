"""CLI: ``havn test`` — run model unit tests from ``tests/unit/*.yml``."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from havn.cli import _load_config, _resolve_project, _warehouse_exists, app, console


@app.command("test")
def test_cmd(
    model: Annotated[Optional[str], typer.Option("--model", "-m", help="Only run tests for this model")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show the differing rows for failing tests")] = False,
    env: Annotated[Optional[str], typer.Option("--env", "-e", help="Environment to use")] = None,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """Run model unit tests: fixed input rows in, expected rows out.

    Each test runs the model against its declared fixtures on a throwaway
    in-memory database. Nothing is read from or written to the warehouse,
    so a test can neither pass nor fail because of what is currently built.
    """
    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir, env)
    ok = run_and_report(project_dir, config, model=model, verbose=verbose)
    if not ok:
        raise typer.Exit(1)


def run_and_report(
    project_dir: Path,
    config,
    *,
    model: str | None = None,
    verbose: bool = False,
    quiet_when_empty: bool = False,
    conn=None,
) -> bool:
    """Run the project's unit tests and print a report. Returns True if clean.

    Shared with ``havn check``, which runs the same suite after validation.
    """
    from rich.table import Table

    from havn.engine.unit_tests import run_unit_tests

    if conn is not None:
        from havn.engine.unit_tests import catalog_from_connection

        catalog = catalog_from_connection(conn)
    else:
        catalog = _warehouse_catalog(project_dir, config)
    result = run_unit_tests(project_dir, model=model, catalog=catalog)

    for err in result.load_errors:
        console.print(f"  [red]error[/red]  definition: {err}")

    if not result.results:
        if not quiet_when_empty and not result.load_errors:
            target = f" for {model}" if model else ""
            console.print(f"[yellow]No unit tests found{target}.[/yellow]")
            console.print(
                "Add [bold]tests/unit/<name>.yml[/bold] with a [bold]model:[/bold] and "
                "[bold]tests:[/bold] block. See the unit test docs."
            )
        return not result.load_errors

    table = Table(title=None)
    table.add_column("", width=4)
    table.add_column("Model", style="cyan")
    table.add_column("Test", style="bold")
    table.add_column("Detail", max_width=60)
    table.add_column("Time", justify="right")
    for res in result.results:
        if res.status == "pass":
            icon = "[green]pass[/green]"
        elif res.status == "fail":
            icon = "[red]FAIL[/red]"
        else:
            icon = "[red]err[/red] "
        table.add_row(icon, res.model, res.name, res.message, f"{res.duration_ms}ms")
    console.print(table)

    for res in result.results:
        for warning in res.warnings:
            console.print(f"  [yellow]warn[/yellow]  {res.model} / {res.name}: {warning}")

    if verbose:
        for res in result.results:
            if res.status != "fail":
                continue
            console.print()
            console.print(f"[bold]{res.model} / {res.name}[/bold]")
            _print_rows("expected, not produced", res.missing_rows, res.missing_count, "red")
            _print_rows("produced, not expected", res.unexpected_rows, res.unexpected_count, "yellow")

    summary = f"{result.passed} passed"
    if result.failed:
        summary += f", {result.failed} failed"
    if result.errored:
        summary += f", {result.errored} errored"
    color = "green" if result.ok else "red"
    console.print(f"[{color}]{summary}[/{color}] in {result.duration_ms}ms")
    if not result.ok and not verbose:
        console.print("[dim]Run with -v to see the differing rows.[/dim]")
    return result.ok


def _print_rows(label: str, rows: list[dict], total: int, color: str) -> None:
    from rich.table import Table

    if not rows:
        return
    table = Table(title=f"{label} ({total})", title_style=color, title_justify="left")
    for column in rows[0]:
        table.add_column(str(column))
    for row in rows:
        table.add_row(*["NULL" if v is None else str(v) for v in row.values()])
    console.print(table)
    if total > len(rows):
        console.print(f"  [dim]... and {total - len(rows)} more[/dim]")


def _warehouse_catalog(project_dir: Path, config) -> dict:
    """Snapshot the warehouse column types, if a warehouse is reachable.

    The catalog only supplies types for mocks that don't declare them and
    powers the narrow-mock warning; a missing or locked warehouse is normal
    and must not fail the run.
    """
    from havn.engine.database import open_warehouse
    from havn.engine.unit_tests import catalog_from_connection

    if not _warehouse_exists(config, project_dir):
        return {}
    conn = None
    try:
        conn = open_warehouse(config, project_dir, read_only=True)
        return catalog_from_connection(conn)
    except Exception:
        return {}
    finally:
        if conn is not None:
            conn.close()
