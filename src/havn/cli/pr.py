"""CLI commands for pull requests."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from havn.cli import _load_config, _resolve_project, app, console

pr_app = typer.Typer(
    name="pr",
    help="Pull request commands",
    no_args_is_help=False,
)
app.add_typer(pr_app)


@pr_app.callback(invoke_without_command=True)
def list_command(
    ctx: typer.Context,
    status: Optional[str] = typer.Option(None, "--status", "-s", help="Filter by status (open/merged/closed)"),
    project_dir: Optional[Path] = typer.Option(None, "--project", "-p"),
) -> None:
    """List pull requests."""
    if ctx.invoked_subcommand is not None:
        return
    from havn.engine.pr import list_prs

    project_dir = _resolve_project(project_dir)
    prs = list_prs(project_dir, status=status)
    if not prs:
        console.print("[dim]No pull requests found.[/dim]")
        return

    table = Table(title="Pull Requests")
    table.add_column("ID", style="bold")
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Author")
    table.add_column("Branch")
    table.add_column("Approvers")
    table.add_column("Created")
    for pr in prs:
        status_color = {
            "open": "green",
            "merged": "magenta",
            "closed": "dim",
        }.get(pr.status, "white")
        approvers = ", ".join(pr.approvers) if pr.approvers else "-"
        if pr.change_requesters:
            approvers = f"[red]CHANGES[/red] ({', '.join(pr.change_requesters)})"
        table.add_row(
            pr.id,
            pr.title,
            f"[{status_color}]{pr.status}[/{status_color}]",
            pr.author,
            f"{pr.head_ref} -> {pr.base_ref}",
            approvers,
            pr.created_at[:10] if pr.created_at else "-",
        )
    console.print(table)


@pr_app.command()
def create(
    branch: str = typer.Option(..., "--branch", "-b", help="Head branch (the PR's source)"),
    title: str = typer.Option(..., "--title", "-t", help="PR title"),
    description: str = typer.Option("", "--description", "-d", help="PR description"),
    base: str = typer.Option("main", "--base", help="Base branch to merge into"),
    author: str = typer.Option("local", "--author", "-a", help="PR author"),
    no_approval: bool = typer.Option(False, "--no-approval", help="Skip approval requirement"),
    project_dir: Optional[Path] = typer.Option(None, "--project", "-p"),
) -> None:
    """Create a new pull request."""
    from havn.engine.pr import create_pr

    project_dir = _resolve_project(project_dir)
    try:
        pr = create_pr(
            project_dir,
            title=title,
            description=description,
            base_ref=base,
            head_ref=branch,
            author=author,
            require_approval=not no_approval,
        )
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    console.print(f"[green]Created {pr.id}[/green]: {pr.title}")
    console.print(f"  {pr.head_ref} -> {pr.base_ref}")
    console.print(f"  File: .havn/prs/{pr.id}.json (commit and push to share)")


@pr_app.command()
def show(
    pr_id: str = typer.Argument(..., help="PR id"),
    project_dir: Optional[Path] = typer.Option(None, "--project", "-p"),
) -> None:
    """Show details of a pull request."""
    from havn.engine.database import connect, ensure_meta_table
    from havn.engine.pr import get_latest_build, get_pr

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir)
    pr = get_pr(project_dir, pr_id)
    if pr is None:
        console.print(f"[red]PR '{pr_id}' not found[/red]")
        raise typer.Exit(1)

    console.print(f"\n[bold]{pr.id}[/bold] — {pr.title}")
    console.print(f"Status: {pr.status}")
    console.print(f"Author: {pr.author}")
    console.print(f"Branch: {pr.head_ref} -> {pr.base_ref}")
    if pr.description:
        console.print(f"\n{pr.description}")
    if pr.approvers:
        console.print(f"\n[green]Approved by:[/green] {', '.join(pr.approvers)}")
    if pr.change_requesters:
        console.print(f"[red]Changes requested by:[/red] {', '.join(pr.change_requesters)}")

    # Latest build
    db_path = project_dir / config.database.path
    if db_path.exists():
        conn = connect(db_path)
        try:
            ensure_meta_table(conn)
            build = get_latest_build(conn, pr.id)
        finally:
            conn.close()
        if build:
            console.print(f"\n[bold]Latest build:[/bold] {build['status']} ({build.get('duration_ms', 0)}ms)")
            if build.get("data_diff"):
                mods = [k for k, v in build["data_diff"].items() if v.get("status") == "modified"]
                adds = [k for k, v in build["data_diff"].items() if v.get("status") == "added"]
                removes = [k for k, v in build["data_diff"].items() if v.get("status") == "removed"]
                if adds:
                    console.print(f"  [green]+ new:[/green] {', '.join(adds)}")
                if removes:
                    console.print(f"  [red]- removed:[/red] {', '.join(removes)}")
                if mods:
                    console.print(f"  [yellow]~ modified:[/yellow] {', '.join(mods)}")
            if build.get("error"):
                console.print(f"  [red]error:[/red] {build['error']}")

    if pr.comments:
        console.print("\n[bold]Comments:[/bold]")
        for c in pr.comments:
            prefix = "[cyan][AI][/cyan]" if c.comment_type == "ai_review" else "[dim][review][/dim]"
            console.print(f"  {prefix} [bold]{c.author}[/bold] ({c.created_at[:19]}): {c.body}")


@pr_app.command()
def comment(
    pr_id: str = typer.Argument(...),
    body: str = typer.Argument(..., help="Comment body"),
    author: str = typer.Option("local", "--author", "-a"),
    project_dir: Optional[Path] = typer.Option(None, "--project", "-p"),
) -> None:
    """Add a comment to a PR."""
    from havn.engine.pr import add_comment

    project_dir = _resolve_project(project_dir)
    try:
        c = add_comment(project_dir, pr_id, author, body)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Comment added[/green]: {c.id}")


@pr_app.command()
def approve(
    pr_id: str = typer.Argument(...),
    reviewer: str = typer.Option("local", "--reviewer", "-r"),
    project_dir: Optional[Path] = typer.Option(None, "--project", "-p"),
) -> None:
    """Approve a PR."""
    from havn.engine.pr import approve_pr

    project_dir = _resolve_project(project_dir)
    try:
        pr = approve_pr(project_dir, pr_id, reviewer)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Approved[/green] {pr.id} (approvers: {', '.join(pr.approvers)})")


@pr_app.command("request-changes")
def request_changes_command(
    pr_id: str = typer.Argument(...),
    reviewer: str = typer.Option("local", "--reviewer", "-r"),
    reason: str = typer.Option("", "--reason"),
    project_dir: Optional[Path] = typer.Option(None, "--project", "-p"),
) -> None:
    """Request changes on a PR."""
    from havn.engine.pr import request_changes

    project_dir = _resolve_project(project_dir)
    try:
        pr = request_changes(project_dir, pr_id, reviewer, reason=reason)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"[yellow]Changes requested[/yellow] on {pr.id}")


@pr_app.command()
def build(
    pr_id: str = typer.Argument(...),
    project_dir: Optional[Path] = typer.Option(None, "--project", "-p"),
) -> None:
    """Build a PR branch in an isolated worktree and diff against main."""
    from havn.engine.database import connect, ensure_meta_table
    from havn.engine.pr import build_pr

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir)
    db_path = project_dir / config.database.path
    conn = connect(db_path, project_dir=project_dir)
    ensure_meta_table(conn)
    try:
        console.print(f"[bold]Building PR {pr_id}...[/bold]")
        record = build_pr(project_dir, pr_id, conn)
    finally:
        conn.close()

    status_color = "green" if record["status"] == "success" else "red"
    console.print(
        f"[{status_color}]{record['status']}[/{status_color}] — {record.get('duration_ms', 0)}ms"
    )
    if record.get("error"):
        console.print(f"[red]Error: {record['error']}[/red]")
        raise typer.Exit(1)
    diff = record.get("data_diff") or {}
    if diff:
        console.print("\n[bold]Data diff:[/bold]")
        for fqn, d in diff.items():
            status = d.get("status", "")
            if status == "unchanged":
                continue
            delta = (d.get("pr_rows", 0) or 0) - (d.get("main_rows", 0) or 0)
            sign = "+" if delta >= 0 else ""
            console.print(
                f"  {status:<10} {fqn}: {d.get('main_rows', 0)} -> {d.get('pr_rows', 0)} "
                f"({sign}{delta})"
            )


@pr_app.command()
def review(
    pr_id: str = typer.Argument(...),
    ai: bool = typer.Option(False, "--ai", help="Print the AI review prompt to stdout"),
    project_dir: Optional[Path] = typer.Option(None, "--project", "-p"),
) -> None:
    """Print an AI-ready review prompt for a PR.

    Pipe the output to your own agent: ``havn pr review <id> --ai | claude``
    """
    from havn.engine.database import connect, ensure_meta_table
    from havn.engine.pr import build_review_prompt, get_latest_build, get_pr

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir)
    pr = get_pr(project_dir, pr_id)
    if pr is None:
        console.print(f"[red]PR '{pr_id}' not found[/red]")
        raise typer.Exit(1)

    db_path = project_dir / config.database.path
    build = None
    if db_path.exists():
        conn = connect(db_path)
        try:
            ensure_meta_table(conn)
            build = get_latest_build(conn, pr_id)
        finally:
            conn.close()

    prompt = build_review_prompt(project_dir, pr, build=build)
    if ai:
        # Plain stdout for piping to an agent
        print(prompt)
    else:
        console.print(prompt)


@pr_app.command()
def merge(
    pr_id: str = typer.Argument(...),
    user: str = typer.Option("local", "--user", "-u"),
    project_dir: Optional[Path] = typer.Option(None, "--project", "-p"),
) -> None:
    """Merge a PR into its base branch."""
    from havn.engine.database import connect, ensure_meta_table
    from havn.engine.pr import merge_pr

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir)
    db_path = project_dir / config.database.path
    conn = connect(db_path, project_dir=project_dir)
    ensure_meta_table(conn)
    try:
        result = merge_pr(project_dir, pr_id, user, conn)
    finally:
        conn.close()
    if not result["success"]:
        console.print(f"[red]Cannot merge: {result['error']}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Merged {pr_id}[/green]")
    console.print(f"  Commit: {result.get('merge_commit', '?')[:8]}")
    console.print(f"  {result.get('head_ref')} -> {result.get('base_ref')}")


@pr_app.command()
def close(
    pr_id: str = typer.Argument(...),
    user: str = typer.Option("local", "--user", "-u"),
    project_dir: Optional[Path] = typer.Option(None, "--project", "-p"),
) -> None:
    """Close a PR without merging."""
    from havn.engine.pr import close_pr

    project_dir = _resolve_project(project_dir)
    try:
        pr = close_pr(project_dir, pr_id, user)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"[yellow]Closed[/yellow] {pr.id}")


@pr_app.command()
def diff(
    pr_id: str = typer.Argument(...),
    project_dir: Optional[Path] = typer.Option(None, "--project", "-p"),
) -> None:
    """Show the git file diff for a PR."""
    from havn.engine.git import diff_files_between
    from havn.engine.pr import get_pr

    project_dir = _resolve_project(project_dir)
    pr = get_pr(project_dir, pr_id)
    if pr is None:
        console.print(f"[red]PR '{pr_id}' not found[/red]")
        raise typer.Exit(1)
    files = diff_files_between(project_dir, pr.base_ref, pr.head_ref)
    if not files:
        console.print("[dim]No files changed[/dim]")
        return
    console.print(f"[bold]Files changed ({len(files)}):[/bold]")
    for f in files:
        console.print(f"  {f}")
