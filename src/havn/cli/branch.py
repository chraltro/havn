"""Branch warehouses: havn branch status|build|diff|list|reset|clean."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.table import Table

from havn.cli import _resolve_project, app, console

branch_app = typer.Typer(
    name="branch",
    help="A warehouse per git branch: build what the branch changed, diff it against the base.",
    no_args_is_help=True,
)
app.add_typer(branch_app)

_NAME_HELP = "Git branch to act for (default: the checked-out one). CI passes the PR branch here."
_BASE_HELP = "Warehouse file to use as the base instead of the configured one (a CI artifact, a backup)."

NameOpt = Annotated[Optional[str], typer.Option("--name", "-n", help=_NAME_HELP)]
BaseOpt = Annotated[Optional[Path], typer.Option("--base", help=_BASE_HELP)]
ProjectOpt = Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")]


def _config(project_dir: Optional[Path], name: Optional[str], base: Optional[Path]):
    from havn.config import UnknownEnvironmentError, load_project

    project = _resolve_project(project_dir)
    try:
        return load_project(
            project,
            strict_env_file=True,
            branch=name,
            branch_base=str(base.resolve()) if base is not None else None,
        )
    except (UnknownEnvironmentError, ValueError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


def _require_active(config) -> None:
    if not config.branch.active:
        console.print(f"[yellow]Not on a branch warehouse:[/yellow] {config.branch.reason}.")
        if not config.branches.enabled:
            console.print(
                "Add [bold]branches: {enabled: true}[/bold] to project.yml, or pass "
                "[bold]--name <branch>[/bold]."
            )
        raise typer.Exit(1)


def _open(config, *, read_only: bool):
    """The branch warehouse, or an empty in-memory stand-in for read-only use."""
    import duckdb

    from havn.engine.branches import branch_warehouse_path
    from havn.engine.database import open_warehouse

    path = branch_warehouse_path(config)
    if read_only and (path is None or not path.exists()):
        return duckdb.connect()
    try:
        return open_warehouse(config, config.project_dir, read_only=read_only)
    except duckdb.Error as e:
        console.print(f"[red]Could not open the branch warehouse {path}:[/red] {e}")
        console.print(
            "If `havn serve` is running on this branch it holds the file; use the "
            "web UI (Ship → Data changes) or stop the server."
        )
        raise typer.Exit(1) from e


def _branch_error(e: Exception) -> None:
    console.print(f"[red]{e}[/red]")
    raise typer.Exit(1) from e


@branch_app.command("status")
def status(
    name: NameOpt = None,
    base: BaseOpt = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
    project_dir: ProjectOpt = None,
) -> None:
    """Which branch and warehouse are active, what is built locally, what is deferred."""
    from havn.engine.branches import branch_status, branch_summary
    from havn.engine.defer import DeferError

    config = _config(project_dir, name, base)
    if not config.branch.active:
        info = branch_summary(config)
        if as_json:
            print(json.dumps(info, indent=2, default=str))
            return
        console.print(f"Branch warehouse: [bold]off[/bold] [dim]({config.branch.reason})[/dim]")
        if config.branch.git_branch:
            console.print(f"Git branch: [bold]{config.branch.git_branch}[/bold]")
        console.print(f"Warehouse: {config.database.path}")
        return

    conn = _open(config, read_only=True)
    try:
        info = branch_status(conn, config)
    except DeferError as e:
        _branch_error(e)
    finally:
        conn.close()

    if as_json:
        print(json.dumps(info, indent=2, default=str))
        return

    wh = info["warehouse"] or {}
    b = info["base"]
    console.print(f"Branch: [bold]{info['branch']}[/bold] [dim]({info['source']})[/dim]")
    exists = "" if wh.get("exists") else " [dim](not created yet)[/dim]"
    console.print(f"Warehouse: [bold]{wh.get('path')}[/bold]{exists}")
    readable = "[green]readable[/green]" if b.get("readable") else f"[yellow]not readable[/yellow] [dim]({b.get('reason')})[/dim]"
    console.print(f"Base: [bold]{b['label']}[/bold] [dim]({b['path']})[/dim] {readable}")
    if b.get("built_at"):
        console.print(f"[dim]Base last built {b['built_at']}[/dim]")

    models = info.get("models") or {}
    local = models.get("local") or []
    console.print(
        f"\n{len(local)} of {models.get('total', 0)} model(s) built on this branch, "
        f"{len(models.get('deferred') or [])} read from the base."
    )
    if local:
        tbl = Table()
        tbl.add_column("Local model", style="bold")
        tbl.add_column("Built")
        tbl.add_column("Rows", justify="right")
        tbl.add_column("State")
        for m in local:
            state = "[yellow]stale[/yellow]" if m["stale"] else "[green]current[/green]"
            tbl.add_row(m["name"], (m["built_at"] or "")[:19], str(m["row_count"] or 0), state)
        console.print(tbl)
        for m in local:
            for reason in m["stale_reasons"]:
                console.print(f"  [yellow]{m['name']}[/yellow]: {reason}")
    modified = models.get("modified")
    if modified is None:
        console.print("[yellow]Could not compare with the base, so what differs is unknown.[/yellow]")
        return
    needs = models.get("needs_build") or []
    prunable = models.get("prunable") or []
    if needs:
        console.print(f"\n[yellow]{len(needs)} model(s) differ from the base and need a build:[/yellow] {', '.join(needs)}")
    if prunable:
        console.print(f"[yellow]{len(prunable)} local model(s) match the base again:[/yellow] {', '.join(prunable)}")
    if needs or prunable or info.get("stale"):
        console.print("Run [bold]havn branch build[/bold] to bring the branch warehouse up to date.")
    elif not modified:
        console.print("\n[green]Nothing on this branch differs from the base.[/green]")
    else:
        console.print("\n[green]Up to date.[/green] [dim]havn branch diff shows how the data moved.[/dim]")


@branch_app.command("build")
def build(
    name: NameOpt = None,
    base: BaseOpt = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Rebuild every planned model, changed since the last branch build or not")] = False,
    no_prune: Annotated[bool, typer.Option("--no-prune", help="Keep branch copies of models that match the base again")] = False,
    plan: Annotated[bool, typer.Option("--plan", help="Show what would be built and pruned, change nothing")] = False,
    defer_snapshot: Annotated[bool, typer.Option("--defer-snapshot", help="Read the base through a consistent copy, for when a job holds it open")] = False,
    project_dir: ProjectOpt = None,
) -> None:
    """Build the models this branch changed (state:modified+ against the base).

    Everything else is read from the base warehouse, read-only. Models whose
    branch copy no longer differs from the base are dropped from the branch
    warehouse so they read through to the base again.
    """
    from havn.engine.branches import BranchError, build_branch
    from havn.engine.defer import DeferError

    config = _config(project_dir, name, base)
    _require_active(config)
    console.print(
        f"[bold]Branch build[/bold] [dim]{config.branch.git_branch} -> {config.database.path}, "
        f"base {config.branch.base_label}[/dim]"
    )
    conn = _open(config, read_only=plan)
    try:
        result = build_branch(
            conn, config,
            force=force, prune=not no_prune, dry_run=plan, snapshot=defer_snapshot,
            on_message=lambda m: console.print(f"  [dim]{m}[/dim]"),
        )
    except (BranchError, DeferError) as e:
        _branch_error(e)
    finally:
        conn.close()

    if plan:
        if not result["plan"] and not result.get("prunable"):
            console.print("[green]Nothing differs from the base.[/green]")
        for m in result["plan"]:
            console.print(f"  build  {m}")
        for m in result.get("prunable") or []:
            console.print(f"  prune  {m}")
        return
    if result["failed"]:
        console.print("[red]Branch build failed:[/red]")
        for m, s in result["failed"].items():
            console.print(f"  [red]{m}[/red] {s}")
        raise typer.Exit(1)
    built = len(result["built"])
    skipped = len(result["plan"]) - built
    console.print(
        f"\n  {built} built, {skipped} already current, {len(result['pruned'])} pruned"
    )


@branch_app.command("diff")
def diff(
    models: Annotated[Optional[list[str]], typer.Argument(help="Only these models (default: everything the branch built or changed)")] = None,
    name: NameOpt = None,
    base: BaseOpt = None,
    markdown: Annotated[bool, typer.Option("--markdown", "--md", help="Print the pull-request comment markdown")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
    output: Annotated[Optional[Path], typer.Option("--output", "-o", help="Write the output to this file instead of stdout")] = None,
    full: Annotated[bool, typer.Option("--full", help="Every changed row, not samples")] = False,
    exit_nonzero_on_change: Annotated[bool, typer.Option("--exit-nonzero-on-change", help="Exit 2 when anything differs (for CI)")] = False,
    project_dir: ProjectOpt = None,
) -> None:
    """Data diff of the branch's models against the base: schema and rows."""
    from havn.engine.branches import BranchError, diff_branch, diff_has_changes, format_markdown
    from havn.engine.defer import DeferError

    config = _config(project_dir, name, base)
    _require_active(config)
    conn = _open(config, read_only=True)
    try:
        report = diff_branch(conn, config, models=models, full=full)
    except (BranchError, DeferError) as e:
        _branch_error(e)
    finally:
        conn.close()

    if markdown or as_json:
        text = format_markdown(report) if markdown else json.dumps(report, indent=2, default=str)
        if output is not None:
            output.write_text(text, encoding="utf-8")
            console.print(f"[green]Wrote {output}[/green]")
        else:
            print(text)
    else:
        _print_report(report)
        if output is not None:
            output.write_text(format_markdown(report), encoding="utf-8")
            console.print(f"[green]Wrote {output}[/green]")
    if exit_nonzero_on_change and diff_has_changes(report):
        raise typer.Exit(2)


def _print_report(report: dict) -> None:
    entries = report["models"]
    console.print(f"[bold]{report['branch']}[/bold] vs [bold]{report['base']}[/bold]")
    if not entries:
        console.print("[green]Nothing on this branch differs from the base.[/green]")
        return
    tbl = Table()
    # Model names never wrap or truncate: they are what people grep and copy.
    tbl.add_column("Model", style="bold", no_wrap=True, min_width=max(len(e["model"]) for e in entries))
    tbl.add_column("Status")
    tbl.add_column("Base", justify="right")
    tbl.add_column("Branch", justify="right")
    tbl.add_column("Added", justify="right", style="green")
    tbl.add_column("Removed", justify="right", style="red")
    tbl.add_column("Modified", justify="right", style="yellow")
    tbl.add_column("Schema")
    colors = {"changed": "yellow", "added": "green", "removed": "red", "error": "red", "not_built": "magenta"}
    for e in entries:
        color = colors.get(e["status"], "dim")
        schema = ", ".join(
            f"{'+' if c['change'] == 'added' else '-' if c['change'] == 'removed' else '~'}{c['column']}"
            for c in e["schema_changes"]
        ) or "-"
        tbl.add_row(
            e["model"], f"[{color}]{e['status']}[/{color}]",
            "-" if e["before"] is None else f"{e['before']:,}",
            "-" if e["after"] is None else f"{e['after']:,}",
            str(e["added"]), str(e["removed"]), str(e["modified"]), schema,
        )
    console.print(tbl)
    for e in entries:
        if e.get("error"):
            console.print(f"  [red]{e['model']}[/red]: {e['error']}")
        if e["status"] == "not_built":
            console.print(f"  [magenta]{e['model']}[/magenta] is not built yet: run [bold]havn branch build[/bold]")


@branch_app.command("list")
def list_cmd(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
    project_dir: ProjectOpt = None,
) -> None:
    """Branch warehouses on disk, and whether their git branch still exists."""
    from havn.engine.branches import list_branch_warehouses

    config = _config(project_dir, None, None)
    entries = list_branch_warehouses(config)
    if as_json:
        print(json.dumps(entries, indent=2))
        return
    if not entries:
        console.print("[dim]No branch warehouses.[/dim]")
        return
    tbl = Table(title="Branch warehouses")
    tbl.add_column("", width=2)
    tbl.add_column("Branch", style="bold")
    tbl.add_column("File")
    tbl.add_column("Size", justify="right")
    tbl.add_column("Git")
    for e in entries:
        git = {"merged": "[yellow]merged[/yellow]", "gone": "[red]deleted[/red]"}.get(e["git"], e["git"])
        tbl.add_row(
            "[green]*[/green]" if e["current"] else "",
            e["branch"] or f"[dim]{e['slug']}[/dim]",
            e["path"],
            f"{e['size_bytes'] / 1_048_576:.1f} MB",
            git,
        )
    console.print(tbl)
    if any(e["git"] in ("merged", "gone") for e in entries):
        console.print("[dim]havn branch clean removes the warehouses of merged and deleted branches.[/dim]")


@branch_app.command("reset")
def reset(
    name: NameOpt = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation")] = False,
    project_dir: ProjectOpt = None,
) -> None:
    """Delete this branch's warehouse so the next build starts from the base again."""
    from havn.engine.branches import BranchError, branch_warehouse_path, remove_branch_warehouse

    config = _config(project_dir, name, None)
    _require_active(config)
    path = branch_warehouse_path(config)
    if path is None or not path.exists():
        console.print("[dim]This branch has no warehouse yet; nothing to reset.[/dim]")
        return
    if not yes and not typer.confirm(f"Delete {config.branch.path} (branch {config.branch.git_branch})?"):
        raise typer.Exit(1)
    try:
        remove_branch_warehouse(config, path)
    except (OSError, BranchError) as e:
        _branch_error(e)
    console.print(f"[green]Deleted {config.branch.path}.[/green] The branch reads everything from the base until the next build.")


@branch_app.command("clean")
def clean(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="List what would be deleted")] = False,
    include_unknown: Annotated[bool, typer.Option("--include-unknown", help="Also delete files whose branch cannot be named")] = False,
    project_dir: ProjectOpt = None,
) -> None:
    """Delete warehouses of branches that were merged into main or deleted."""
    from havn.engine.branches import clean_branches

    config = _config(project_dir, None, None)
    result = clean_branches(config, dry_run=dry_run, include_unknown=include_unknown)
    verb = "Would delete" if dry_run else "Deleted"
    for e in result["removed"]:
        console.print(f"  {verb} {e['path']} [dim]({e['branch'] or e['slug']}, {e['git']})[/dim]")
    for path, err in result["errors"].items():
        console.print(f"  [red]Could not delete {path}:[/red] {err}")
    if not result["removed"] and not result["errors"]:
        console.print("[dim]No branch warehouses to clean.[/dim]")
    if result["errors"]:
        raise typer.Exit(1)
