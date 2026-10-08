"""havn deploy: build a git ref in an environment, rolling back on failure."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from havn.cli import _resolve_project, app, console


@app.command()
def deploy(
    env: Annotated[str, typer.Argument(help="Environment to deploy to (from project.yml environments)")],
    ref: Annotated[str, typer.Option("--ref", "-r", help="Git branch, tag or commit to deploy")] = "main",
    plan: Annotated[bool, typer.Option("--plan", help="Show what would rebuild, change nothing")] = False,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory")] = None,
) -> None:
    """Deploy a git ref to an environment.

    Builds every model that differs between the ref and what the environment
    last built (state:modified+), using the ref's code. The affected models are
    snapshotted first; if any of them fails, all of them are put back as they
    were, build state included.

    Examples:
      havn deploy prod --plan
      havn deploy prod
      havn deploy staging --ref release-2026-09
    """
    import duckdb

    from havn.config import load_project
    from havn.engine.database import open_warehouse
    from havn.engine.deploy import DeployError, new_record, plan_deploy, run_deploy

    project = _resolve_project(project_dir)
    base = load_project(project)
    if base.environments and env not in base.environments:
        console.print(f"[red]Unknown environment '{env}'.[/red] Defined: {', '.join(base.environments)}")
        raise typer.Exit(1)
    cfg = load_project(project, env=env) if base.environments else base
    path = Path(cfg.database.path)
    path = path if path.is_absolute() else project / path

    try:
        conn = open_warehouse(cfg, project, read_only=plan) if (path.exists() or not plan) else None
    except duckdb.Error as e:
        console.print(f"[red]Could not open {path.name}:[/red] {e}")
        console.print("If `havn serve` is running against it, deploy from the web UI (Ship) instead.")
        raise typer.Exit(1)
    try:
        if plan:
            try:
                result = plan_deploy(project, ref, conn)
            except DeployError as e:
                console.print(f"[red]{e}[/red]")
                raise typer.Exit(1)
            if not result["models"]:
                console.print(f"[green]{env} is up to date with {ref}[/green] ({result['commit'][:7]})")
                return
            console.print(f"Deploying {ref} ({result['commit'][:7]}) to [bold]{env}[/bold] would rebuild:")
            for m in result["models"]:
                console.print(f"  {m}")
            return

        rec = run_deploy(project, new_record(env, ref, deployed_by="cli"), conn, db_path=str(path))
    finally:
        if conn is not None:
            conn.close()

    status = rec["status"]
    commit = (rec.get("commit") or "")[:7]
    if status == "success":
        console.print(f"[green]Deployed {ref} ({commit}) to {env}:[/green] {len(rec['models'])} model(s) built")
    elif status == "up_to_date":
        console.print(f"[green]{env} is already up to date with {ref}[/green] ({commit})")
    elif status == "rolled_back":
        console.print(f"[red]Rolled back.[/red] {env} is as it was before the deploy.")
        for model, f in (rec.get("failed") or {}).items():
            console.print(f"  [red]{model}[/red] {f['status']}" + (f": {f['error']}" if f.get("error") else ""))
        if rec.get("error"):
            console.print(f"  {rec['error']}")
        raise typer.Exit(1)
    else:
        console.print(f"[red]Deploy failed:[/red] {rec.get('error')}")
        raise typer.Exit(1)
