"""CLI commands for orchestration jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from havn.cli import _load_config, _resolve_project, app, console

jobs_app = typer.Typer(
    name="jobs",
    help="Manage orchestration jobs.",
    no_args_is_help=False,
)
app.add_typer(jobs_app)


@jobs_app.callback(invoke_without_command=True)
def list_jobs(
    ctx: typer.Context,
    project_dir: Optional[Path] = typer.Option(None, "--project", "-p"),
) -> None:
    """List all orchestration jobs."""
    if ctx.invoked_subcommand is not None:
        return
    from havn.engine.database import connect, ensure_meta_table
    from havn.engine.orchestration import (
        discover_jobs,
        ensure_job_runs_table,
        get_next_run,
    )

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir)
    db_path = project_dir / config.database.path
    jobs = discover_jobs(project_dir)
    if not jobs:
        console.print("[dim]No orchestration jobs found. Create YAML files in orchestration/[/dim]")
        return

    # Get last run status from DB
    last_runs: dict[str, dict] = {}
    if db_path.exists():
        conn = connect(db_path)
        try:
            ensure_meta_table(conn)
            ensure_job_runs_table(conn)
            rows = conn.execute(
                "SELECT job_name, status, started_at, duration_ms "
                "FROM _havn.job_runs WHERE (job_name, started_at) IN "
                "(SELECT job_name, MAX(started_at) FROM _havn.job_runs GROUP BY job_name)"
            ).fetchall()
            for r in rows:
                last_runs[r[0]] = {
                    "status": r[1],
                    "started_at": str(r[2]),
                    "duration_ms": r[3],
                }
        except Exception:
            pass
        finally:
            conn.close()

    table = Table(title="Orchestration Jobs")
    table.add_column("Name", style="bold")
    table.add_column("Target")
    table.add_column("Schedule")
    table.add_column("Resolve")
    table.add_column("Enabled")
    table.add_column("Last Run")
    table.add_column("Next Run")
    for job in jobs:
        lr = last_runs.get(job.name, {})
        last_status = lr.get("status", "-")
        color = (
            "green"
            if last_status == "success"
            else "red"
            if last_status == "failure"
            else "dim"
        )
        next_run = get_next_run(job.cron) if job.enabled and job.cron else "-"
        table.add_row(
            job.name,
            job.target,
            job.cron or "-",
            job.resolve,
            "[green]on[/green]" if job.enabled else "[dim]off[/dim]",
            f"[{color}]{last_status}[/{color}]",
            str(next_run) if next_run else "-",
        )
    console.print(table)


@jobs_app.command()
def preview(
    name: str = typer.Argument(..., help="Job name or filename stem"),
    project_dir: Optional[Path] = typer.Option(None, "--project", "-p"),
) -> None:
    """Preview the resolved execution plan for a job."""
    from havn.engine.database import connect, ensure_meta_table
    from havn.engine.orchestration import _find_job, preview_plan
    from havn.engine.transform.discovery import build_dag, discover_models

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir)
    job = _find_job(project_dir, name)
    if not job:
        console.print(f"[red]Job '{name}' not found[/red]")
        raise typer.Exit(1)

    models = discover_models(project_dir / "transform")
    dag = build_dag(models)
    conn = None
    db_path = project_dir / config.database.path
    if db_path.exists():
        conn = connect(db_path)
        ensure_meta_table(conn)
    try:
        plan = preview_plan(
            job.targets or [job.target], dag, project_dir, conn=conn, resolve=job.resolve
        )
    finally:
        if conn:
            conn.close()

    console.print(f"\n[bold]{job.name}[/bold] \u2014 {job.target}")
    console.print(
        f"Steps: {plan['total_steps']} "
        f"({plan['ingest_count']} ingest, {plan['transform_count']} transform, {plan['export_count']} export)"
    )
    if plan["total_estimated_ms"]:
        console.print(f"Estimated: {plan['total_estimated_ms'] / 1000:.1f}s\n")
    for s in plan["steps"]:
        icon = {
            "ingest": "[cyan]ING[/cyan]",
            "transform": "[blue]TRF[/blue]",
            "export": "[magenta]EXP[/magenta]",
        }.get(s["type"], s["type"])
        est = f" (~{s['estimated_duration_ms']}ms)" if s["estimated_duration_ms"] else ""
        console.print(f"  {s['step']:>3}. {icon}  {s['target']}{est}")


@jobs_app.command()
def run(
    name: str = typer.Argument(..., help="Job name or filename stem"),
    project_dir: Optional[Path] = typer.Option(None, "--project", "-p"),
) -> None:
    """Trigger a job manually."""
    from havn.engine.database import connect, ensure_meta_table
    from havn.engine.orchestration import (
        _find_job,
        ensure_job_runs_table,
        execute_job,
        resolve_execution_plan,
    )
    from havn.engine.transform.discovery import build_dag, discover_models

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir)
    job = _find_job(project_dir, name)
    if not job:
        console.print(f"[red]Job '{name}' not found[/red]")
        raise typer.Exit(1)

    db_path = project_dir / config.database.path
    conn = connect(db_path, project_dir=project_dir)
    ensure_meta_table(conn)
    ensure_job_runs_table(conn)
    try:
        models = discover_models(project_dir / "transform")
        dag = build_dag(models)
        plan = resolve_execution_plan(
            job.targets or [job.target], dag, project_dir, conn=conn, resolve=job.resolve
        )
        console.print(f"[bold]Running {job.name}[/bold] \u2014 {len(plan.steps)} steps")
        result = execute_job(job, plan, conn, project_dir, trigger="manual")
        color = "green" if result.status == "success" else "red"
        console.print(
            f"[{color}]{result.status}[/{color}] \u2014 "
            f"{result.steps_completed}/{result.steps_total} steps, {result.duration_ms}ms"
        )
        if result.error:
            console.print(f"[red]Error: {result.error}[/red]")
    finally:
        conn.close()


@jobs_app.command()
def history(
    job: Optional[str] = typer.Option(None, "--job", "-j", help="Filter by job name"),
    limit: int = typer.Option(20, "--limit", "-n"),
    project_dir: Optional[Path] = typer.Option(None, "--project", "-p"),
) -> None:
    """Show recent job runs."""
    from havn.engine.database import connect, ensure_meta_table
    from havn.engine.orchestration import ensure_job_runs_table

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir)
    db_path = project_dir / config.database.path
    if not db_path.exists():
        console.print("[dim]No database found[/dim]")
        return
    conn = connect(db_path)
    ensure_meta_table(conn)
    ensure_job_runs_table(conn)
    try:
        query = (
            "SELECT job_name, status, steps_completed, steps_total, duration_ms, "
            "trigger, started_at FROM _havn.job_runs"
        )
        params: list = []
        if job:
            query += " WHERE job_name = ?"
            params.append(job)
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    if not rows:
        console.print("[dim]No job runs found[/dim]")
        return
    table = Table(title="Job Runs")
    table.add_column("Job")
    table.add_column("Status")
    table.add_column("Steps")
    table.add_column("Duration")
    table.add_column("Trigger")
    table.add_column("Started")
    for r in rows:
        color = (
            "green"
            if r[1] == "success"
            else "red"
            if r[1] == "failure"
            else "yellow"
        )
        dur = f"{r[4]}ms" if r[4] else "-"
        table.add_row(
            r[0],
            f"[{color}]{r[1]}[/{color}]",
            f"{r[2]}/{r[3]}",
            dur,
            r[5] or "-",
            str(r[6]) if r[6] else "-",
        )
    console.print(table)


@jobs_app.command()
def enable(
    name: str,
    project_dir: Optional[Path] = typer.Option(None, "--project", "-p"),
) -> None:
    """Enable a job."""
    _toggle_enabled(name, True, project_dir)


@jobs_app.command()
def disable(
    name: str,
    project_dir: Optional[Path] = typer.Option(None, "--project", "-p"),
) -> None:
    """Disable a job."""
    _toggle_enabled(name, False, project_dir)


def _toggle_enabled(name: str, enabled: bool, project_dir: Path | None) -> None:
    import yaml

    from havn.engine.orchestration import _find_job

    project_dir = _resolve_project(project_dir)
    job = _find_job(project_dir, name)
    if not job:
        console.print(f"[red]Job '{name}' not found[/red]")
        raise typer.Exit(1)
    data = yaml.safe_load(job.file_path.read_text())
    data["enabled"] = enabled
    job.file_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    console.print(f"Job '{name}' {'enabled' if enabled else 'disabled'}")
