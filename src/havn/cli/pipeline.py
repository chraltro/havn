"""Pipeline commands: run, ingest, export, seed, transform, stream, lint, watch, schedule."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Optional

import typer
from rich.markup import escape
from rich.table import Table

from havn.cli import _load_config, _resolve_project, _warehouse_exists, app, console

if TYPE_CHECKING:
    from havn.engine.selectors import SelectionResult

logger = logging.getLogger("havn.cli")


@app.command()
def run(
    script: Annotated[str, typer.Argument(help="Script path (e.g. ingest/customers.py)")],
    env: Annotated[Optional[str], typer.Option("--env", "-e", help="Environment to use")] = None,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Re-run even if `# @havn: schedule=once` and a prior success exists")] = False,
) -> None:
    """Run a single ingest or export script (.py or .dpnb notebook)."""
    from havn.engine.database import open_warehouse
    from havn.engine.runner import run_script

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir, env)
    script_path = project_dir / script

    if not script_path.exists():
        console.print(f"[red]Script not found: {script_path}[/red]")
        raise typer.Exit(1)

    if script_path.is_dir():
        console.print(f"[red]Expected a script file, got a directory: {script_path}[/red]")
        console.print("Hint: use [bold]havn jobs run[/bold] to run a job, or specify a file like [bold]havn run ingest/script.py[/bold]")
        raise typer.Exit(1)

    # Determine script type from immediate parent directory
    parent_name = script_path.parent.name
    if parent_name == "ingest":
        script_type = "ingest"
    elif parent_name == "export":
        script_type = "export"
    else:
        script_type = "script"
    console.print(f"[bold]Running {script_type}:[/bold]")

    conn = open_warehouse(config, project_dir)
    try:
        result = run_script(conn, script_path, script_type, force=force)
        if result["status"] == "error":
            raise typer.Exit(1)
    finally:
        conn.close()


@app.command()
def ingest(
    targets: Annotated[Optional[list[str]], typer.Argument(help="Specific scripts to run (without extension), or omit for all")] = None,
    env: Annotated[Optional[str], typer.Option("--env", "-e", help="Environment to use")] = None,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Re-run scripts marked `# @havn: schedule=once`")] = False,
) -> None:
    """Run all ingest scripts (or specific ones) from ingest/ directory."""
    from havn.engine.database import open_warehouse
    from havn.engine.runner import run_scripts_in_dir

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir, env)
    ingest_dir = project_dir / "ingest"

    if not ingest_dir.exists():
        console.print("[yellow]No ingest/ directory found.[/yellow]")
        raise typer.Exit(1)

    console.print("[bold]Ingest:[/bold]")
    conn = open_warehouse(config, project_dir)
    try:
        results = run_scripts_in_dir(conn, ingest_dir, "ingest", targets, force=force)
        errors = sum(1 for r in results if r["status"] == "error")
        ok = len(results) - errors
        console.print(f"\n  {ok} succeeded, {errors} failed")
        if errors:
            raise typer.Exit(1)
    finally:
        conn.close()


@app.command()
def export(
    targets: Annotated[Optional[list[str]], typer.Argument(help="Specific scripts to run (without extension), or omit for all")] = None,
    env: Annotated[Optional[str], typer.Option("--env", "-e", help="Environment to use")] = None,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Re-run scripts marked `# @havn: schedule=once`")] = False,
) -> None:
    """Run all export scripts (or specific ones) from export/ directory."""
    from havn.engine.database import open_warehouse
    from havn.engine.runner import run_scripts_in_dir

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir, env)
    export_dir = project_dir / "export"

    if not export_dir.exists():
        console.print("[yellow]No export/ directory found.[/yellow]")
        raise typer.Exit(1)

    console.print("[bold]Export:[/bold]")
    conn = open_warehouse(config, project_dir)
    try:
        results = run_scripts_in_dir(conn, export_dir, "export", targets, force=force)
        errors = sum(1 for r in results if r["status"] == "error")
        ok = len(results) - errors
        console.print(f"\n  {ok} succeeded, {errors} failed")
        if errors:
            raise typer.Exit(1)
    finally:
        conn.close()


@app.command()
def seed(
    force: Annotated[bool, typer.Option("--force", "-f", help="Force reload all seeds")] = False,
    schema: Annotated[str, typer.Option("--schema", "-s", help="Target schema for seeds")] = "seeds",
    env: Annotated[Optional[str], typer.Option("--env", "-e", help="Environment to use")] = None,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """Load CSV files from seeds/ directory into DuckDB tables.

    Seeds are change-detected: only modified CSVs are reloaded.
    Use --force to reload everything.
    """
    from havn.engine.database import open_warehouse
    from havn.engine.seeds import run_seeds

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir, env)
    seeds_dir = project_dir / "seeds"

    if not seeds_dir.exists():
        console.print("[yellow]No seeds/ directory found.[/yellow]")
        console.print("Create a seeds/ directory with CSV files to load as seed data.")
        return

    env_label = f" [dim](env={config.active_environment})[/dim]" if config.active_environment else ""
    console.print(f"[bold]Loading seeds{env_label}:[/bold]")

    conn = open_warehouse(config, project_dir)
    try:
        results = run_seeds(conn, seeds_dir, schema=schema, force=force)
        if results:
            built = sum(1 for s in results.values() if s == "built")
            skipped = sum(1 for s in results.values() if s == "skipped")
            errors = sum(1 for s in results.values() if s == "error")
            console.print(f"\n  {built} loaded, {skipped} skipped, {errors} errors")
            if errors:
                raise typer.Exit(1)
    finally:
        conn.close()


def _report_selection(selection: SelectionResult, *, verbose: bool) -> None:
    """Print what a set of selectors resolved to.

    Always loud about a selector that matched nothing, since a typo in a
    selector otherwise looks exactly like a project where everything is
    already up to date. Quiet about the rest unless ``-v`` asked.
    """
    for warning in selection.warnings:
        console.print(f"[yellow]warning: {warning}[/yellow]")

    if not verbose:
        return
    for selector, names in selection.matched.items():
        rendered = ", ".join(names) if names else "(nothing)"
        console.print(f"  [dim]{selector} -> {rendered}[/dim]")
    console.print(f"  [dim]{len(selection.selected)} model(s) selected[/dim]")


@app.command(name="ls")
def ls(
    targets: Annotated[Optional[list[str]], typer.Argument(help="Graph selectors (default: every model)")] = None,
    select: Annotated[Optional[list[str]], typer.Option("--select", "-s", help="Graph selector, repeatable")] = None,
    exclude: Annotated[Optional[list[str]], typer.Option("--exclude", "-x", help="Graph selector whose matches are removed")] = None,
    names_only: Annotated[bool, typer.Option("--names", "-n", help="Print bare model names, one per line, for piping")] = False,
    env: Annotated[Optional[str], typer.Option("--env", "-e", help="Environment to use (e.g. dev, prod)")] = None,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """List the models a selector resolves to, without building anything.

    A dry run for the selector grammar that `havn transform` takes, so an
    `@`, a `state:modified+` or a wildcard can be checked before it drives a
    build. Prints schema, materialization and tags for each match.
    """
    from havn.engine.database import open_warehouse
    from havn.engine.selectors import select_models
    from havn.engine.transform import discover_all_models

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir, env)

    selectors = list(targets or []) + list(select or [])
    exclusions = list(exclude or [])

    models = discover_all_models(project_dir, config)
    by_name = {m.full_name: m for m in models}

    # Only `state:` selectors need the warehouse; opening it otherwise would
    # make `havn ls` fail in a project that has never been built.
    conn = None
    if any("state:" in s for s in selectors + exclusions):
        if _warehouse_exists(config, project_dir):
            conn = open_warehouse(config, project_dir)
        else:
            # Nothing has ever been built, so every model is modified. That is
            # what `havn transform state:modified` answers on the same project,
            # because it opens (and thereby creates) the warehouse and finds no
            # model_state. `ls` must not create a file as a side effect of
            # listing, so it reads an empty in-memory database instead, which
            # gives the same answer. Passing conn=None would select nothing and
            # exit 1, contradicting the line printed right here.
            import duckdb

            conn = duckdb.connect()
            console.print(
                "[yellow]No warehouse yet; state: selectors match every model.[/yellow]"
            )

    try:
        selection = select_models(
            selectors or ["all"],
            models,
            conn=conn,
            project_dir=project_dir,
            exclude=exclusions or None,
        )
    finally:
        if conn is not None:
            conn.close()

    for warning in selection.warnings:
        console.print(f"[yellow]warning: {warning}[/yellow]")

    if not selection.selected:
        console.print("[dim]No models selected.[/dim]")
        raise typer.Exit(1)

    if names_only:
        for name in selection.selected:
            print(name)
        return

    # The package column only earns its width when a package is installed.
    has_packages = any(getattr(by_name[n], "package", None) for n in selection.selected)
    # Likewise the language column, once a Python model is in the selection.
    has_python = any(getattr(by_name[n], "is_python", False) for n in selection.selected)
    table = Table(title=f"{len(selection.selected)} model(s)")
    table.add_column("model", style="bold")
    table.add_column("schema")
    table.add_column("materialized")
    if has_python:
        table.add_column("language")
    if has_packages:
        table.add_column("package")
    table.add_column("tags")
    for name in selection.selected:
        model = by_name[name]
        row = [name, model.schema, model.materialized]
        if has_python:
            row.append(model.language)
        if has_packages:
            row.append(getattr(model, "package", None) or "[dim]-[/dim]")
        row.append(", ".join(getattr(model, "tags", []) or []) or "[dim]-[/dim]")
        table.add_row(*row)
    console.print(table)


@app.command()
def transform(
    targets: Annotated[Optional[list[str]], typer.Argument(help="Graph selectors picking what to build (default: everything)")] = None,
    select: Annotated[Optional[list[str]], typer.Option("--select", "-s", help="Graph selector, repeatable; same grammar as the positional argument")] = None,
    exclude: Annotated[Optional[list[str]], typer.Option("--exclude", "-x", help="Graph selector whose matches are removed from the selection")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Force rebuild all models")] = False,
    sequential: Annotated[bool, typer.Option("--sequential", help="Disable parallel execution; run models one at a time")] = False,
    workers: Annotated[int, typer.Option("--workers", "-w", help="Max parallel workers")] = 4,
    env: Annotated[Optional[str], typer.Option("--env", "-e", help="Environment to use (e.g. dev, prod)")] = None,
    skip_check: Annotated[bool, typer.Option("--skip-check", help="Skip pre-transform validation")] = False,
    event_time_start: Annotated[Optional[str], typer.Option("--event-time-start", help="Backfill microbatch models from this event time (UTC), e.g. 2024-01-01")] = None,
    event_time_end: Annotated[Optional[str], typer.Option("--event-time-end", help="Backfill microbatch models up to this event time (UTC), exclusive")] = None,
    defer: Annotated[Optional[bool], typer.Option("--defer/--no-defer", help="Read models this warehouse has not built from the environment's defer target (default: on when one is configured)")] = None,
    defer_snapshot: Annotated[bool, typer.Option("--defer-snapshot", help="Defer to a consistent copy of the target, for when it is locked by a running job")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Print the resolved selection and which selector matched what")] = False,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """Parse SQL models, resolve DAG, execute in dependency order.

    Targets are dbt-style graph selectors:

      havn transform                        every model
      havn transform gold.orders            exactly that model
      havn transform +gold.orders           it and everything it depends on
      havn transform gold.orders+           it and everything downstream
      havn transform 1+gold.orders          one hop of upstream only
      havn transform @silver.customers      it, its downstream, and their upstream
      havn transform 'gold.fct_*'           fnmatch wildcards
      havn transform tag:daily              models tagged daily
      havn transform path:transform/gold/   models under a path
      havn transform config.materialized:incremental
      havn transform state:modified+        what changed, plus downstream
      havn transform 'tag:daily,gold.*'     comma intersects
      havn transform -s tag:daily -x gold.experimental

    Microbatch models normally resume from their own recorded window state.
    To backfill an explicit range instead:

      havn transform gold.events --event-time-start 2024-01-01 --event-time-end 2024-03-01

    When the active environment declares `defer: <other env>`, models this
    warehouse has not built are read from that environment's warehouse and
    everything still writes here. Its file must not be open for writing
    anywhere else; if it is, --defer-snapshot reads a consistent copy instead.

    Supports incremental models, data quality assertions, auto-profiling,
    and parallel execution of independent models.
    """
    from havn.engine.database import open_warehouse
    from havn.engine.transform import BatchRange, parse_event_time, run_transform

    batch_range = None
    if event_time_start or event_time_end:
        try:
            batch_range = BatchRange(
                start=parse_event_time(event_time_start, "--event-time-start")
                if event_time_start else None,
                end=parse_event_time(event_time_end, "--event-time-end")
                if event_time_end else None,
            )
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from e
        if batch_range.start and batch_range.end and batch_range.start >= batch_range.end:
            console.print(
                "[red]--event-time-start must be before --event-time-end.[/red]"
            )
            raise typer.Exit(1)

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir, env)
    transform_dir = project_dir / "transform"

    from havn.engine.defer import DeferError, resolve_defer

    try:
        defer_spec = resolve_defer(
            config, project_dir,
            enabled=defer, snapshot=defer_snapshot, verbose=verbose,
        )
    except DeferError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    selectors = list(targets or []) + list(select or [])
    exclusions = list(exclude or [])

    parallel = not sequential
    mode = "parallel" if parallel else "sequential"
    console.print(f"[bold]Transform[/bold] [dim]({mode})[/dim]:")

    conn = open_warehouse(config, project_dir)

    # Resolve the selection up front so the user is told what will run (and
    # which selector matched nothing) before any of the work starts.
    resolved: Optional[list[str]] = None
    if selectors or exclusions:
        from havn.engine.selectors import select_models
        from havn.engine.transform import discover_all_models

        all_models = discover_all_models(project_dir, config)
        selection = select_models(
            selectors or ["all"],
            all_models,
            conn=conn,
            project_dir=project_dir,
            exclude=exclusions or None,
        )
        _report_selection(selection, verbose=verbose)
        if not selection.selected:
            console.print("[red]Nothing selected; nothing to build.[/red]")
            conn.close()
            raise typer.Exit(1)
        resolved = selection.selected

    # Register Python SQL macros (macros/ directory) so they're available in transforms
    macros_dir = project_dir / "macros"
    if macros_dir.is_dir():
        from havn.engine.macros import register_macros
        n_macros = register_macros(conn, project_dir)
        if n_macros:
            console.print(f"  [dim]{n_macros} macro(s) registered[/dim]")

    # Schema Sentinel: check for upstream schema changes before executing
    if config.sentinel.enabled:
        try:
            from havn.engine.sentinel import (
                SentinelConfig as _SC,
                get_source_names_from_models,
                run_sentinel_check,
            )
            source_names = get_source_names_from_models(project_dir)
            # Filter to sources that actually exist in the warehouse
            existing_sources = []
            for sn in source_names:
                parts = sn.split(".")
                if len(parts) == 2:
                    try:
                        exists = conn.execute(
                            "SELECT COUNT(*) FROM information_schema.tables "
                            "WHERE table_catalog = current_database() "
                            "AND table_schema = ? AND table_name = ?",
                            [parts[0], parts[1]],
                        ).fetchone()[0]
                        if exists:
                            existing_sources.append(sn)
                    except Exception:
                        pass

            if existing_sources:
                sc = _SC(
                    enabled=config.sentinel.enabled,
                    on_change=config.sentinel.on_change,
                    track_ordering=config.sentinel.track_ordering,
                    rename_inference=config.sentinel.rename_inference,
                    auto_fix=config.sentinel.auto_fix,
                    select_star_warning=config.sentinel.select_star_warning,
                )
                diffs = run_sentinel_check(project_dir, conn, existing_sources, config=sc)
                if diffs:
                    breaking = [d for d in diffs if d.has_breaking]
                    for diff in diffs:
                        n_changes = len(diff.changes)
                        severity_label = "[red]BREAKING[/red]" if diff.has_breaking else "[yellow]WARNING[/yellow]"
                        console.print(
                            f"  {severity_label}  Schema change in [bold]{diff.source_name}[/bold]: "
                            f"{n_changes} change(s)"
                        )
                        for ch in diff.changes:
                            sev = {"breaking": "[red]", "warning": "[yellow]", "info": "[dim]"}
                            sev_close = {"breaking": "[/red]", "warning": "[/yellow]", "info": "[/dim]"}
                            s = sev.get(ch.severity, "")
                            sc_ = sev_close.get(ch.severity, "")
                            detail = ""
                            if ch.old_value and ch.new_value:
                                detail = f" ({ch.old_value} -> {ch.new_value})"
                            elif ch.old_value:
                                detail = f" (was: {ch.old_value})"
                            elif ch.new_value:
                                detail = f" (type: {ch.new_value})"
                            console.print(f"           {s}{ch.change_type}{sc_}: {ch.column_name}{detail}")

                    if breaking and config.sentinel.on_change == "pause":
                        console.print(
                            "\n  [red]Pipeline paused due to breaking schema changes.[/red]"
                        )
                        console.print(
                            "  Run [bold]havn sentinel[/bold] for details, or set "
                            "[bold]sentinel.on_change: continue[/bold] in project.yml to skip."
                        )
                        conn.close()
                        raise typer.Exit(1)
        except typer.Exit:
            raise
        except Exception as e:
            logger.warning("Schema Sentinel check failed: %s", e)

    # Start a Pipeline Rewind run if enabled
    run_id = None
    if config.rewind.enabled:
        from havn.engine.snapshots import finish_run, run_gc, start_run
        run_id = start_run(project_dir, trigger="manual")

    try:
        results = run_transform(
            conn, transform_dir, targets=resolved, force=force,
            parallel=parallel, max_workers=workers,
            db_config=config.database,
            project_dir=project_dir, rewind_config=config.rewind, run_id=run_id,
            batch_range=batch_range,
            defer=defer_spec,
        )
        if not results:
            return
        built = sum(1 for s in results.values() if s == "built")
        skipped = sum(1 for s in results.values() if s == "skipped" or str(s).startswith("skipped_"))
        errors = sum(1 for s in results.values() if s == "error")
        assertions_failed = sum(1 for s in results.values() if s == "assertion_failed")
        policy_denied = sum(1 for s in results.values() if s == "policy_denied")
        source_stale = sum(1 for s in results.values() if s == "source_stale")
        inlined = sum(1 for s in results.values() if s == "inlined")
        console.print()
        parts = [f"{built} built", f"{skipped} skipped", f"{errors} errors"]
        if inlined:
            parts.append(f"{inlined} inlined")
        if assertions_failed:
            parts.append(f"{assertions_failed} assertion failures")
        if policy_denied:
            parts.append(f"{policy_denied} policy denials")
        if source_stale:
            parts.append(f"{source_stale} source-stale skips")
        console.print(f"  {', '.join(parts)}")

        # Finish the Pipeline Rewind run
        if run_id and config.rewind.enabled:
            status = "failed" if errors else ("partial" if assertions_failed else "success")
            models_run = list(results.keys())
            finish_run(project_dir, run_id, status, models_run)
            # Run garbage collection
            try:
                from havn.engine.snapshots import RewindConfig as _RC
                rw_cfg = _RC(
                    enabled=config.rewind.enabled,
                    retention=config.rewind.retention,
                    max_storage=config.rewind.max_storage,
                    dedup=config.rewind.dedup,
                    exclude=config.rewind.exclude,
                )
                deleted = run_gc(project_dir, rw_cfg)
                if deleted:
                    console.print(f"  [dim]rewind: {deleted} expired snapshot(s) cleaned up[/dim]")
            except Exception as gc_err:
                logger.warning("Snapshot GC failed: %s", gc_err)

        # Send alerts if configured. Through the shared helper, so a project
        # with only `channels: [log]` (no Slack or webhook URL) alerts here
        # the same way streams and jobs do, and delivery cannot fail the run.
        failed = bool(errors or assertions_failed)
        if failed or built > 0:
            from havn.engine.pipeline_alerts import send_pipeline_alert

            send_pipeline_alert(
                config.alerts, "transform", ok=not failed, duration_s=0, conn=conn,
                error=f"{errors} errors, {assertions_failed} assertion failures",
                models_built=built,
            )

        if errors or assertions_failed:
            raise typer.Exit(1)
    finally:
        conn.close()


@app.command()
def stream(
    name: Annotated[str, typer.Argument(help="Stream name from project.yml")],
    force: Annotated[bool, typer.Option("--force", "-f", help="Force rebuild all models")] = False,
    env: Annotated[Optional[str], typer.Option("--env", "-e", help="Environment to use")] = None,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """Run a full stream: ingest -> transform -> export as defined in project.yml."""
    from havn.engine.stream_runner import run_stream

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir, env)

    if name not in config.streams:
        console.print(f"[red]Stream '{name}' not found in project.yml[/red]")
        available = ", ".join(config.streams.keys()) or "(none)"
        console.print(f"Available streams: {available}")
        raise typer.Exit(1)

    stream_config = config.streams[name]
    console.print(f"[bold]Stream: {name}[/bold]")
    if stream_config.description:
        console.print(f"  {stream_config.description}")
    if stream_config.retries:
        console.print(f"  [dim]retries: {stream_config.retries}, delay: {stream_config.retry_delay}s[/dim]")
    console.print()

    result = run_stream(config, project_dir, name, force=force, on_event=_print_stream_event)
    if not result.ok:
        raise typer.Exit(1)


_STEP_HEADINGS = {"ingest": "Ingest", "transform": "Transform", "export": "Export", "seed": "Seeds"}


def _print_stream_event(event: str, data: dict) -> None:
    """Render the stream runner's progress events as `havn stream` output."""
    if event == "macros":
        console.print(f"  [dim]{data['count']} macro(s) registered[/dim]")
        console.print()
    elif event == "step_start":
        heading = _STEP_HEADINGS.get(data["action"], data["action"])
        console.print(f"[bold]{heading}:[/bold]")
    elif event == "step_end":
        if data["ok"]:
            console.print()
        elif data.get("error"):
            console.print(f"[red]{data['error']}[/red]")
    elif event == "retry":
        what = f"{data['action']} {data['name']}" if data.get("name") else data["action"]
        console.print(
            f"[yellow]Retrying {what} (attempt {data['attempt']}/{data['retries']}) "
            f"after {data['delay']}s...[/yellow]"
        )
    elif event == "validation":
        colour = "red" if data.get("severity") == "error" else "yellow"
        line = f":{data['line']}" if data.get("line") is not None else ""
        console.print(f"  [{colour}]{data.get('severity')}[/{colour}]  {data.get('model')}{line}: "
                      f"{escape(str(data.get('message')))}")
    elif event == "node_end":
        # Scripts and models print their own lines as they run; seeds, and
        # anything held back by a failure above it, are reported here.
        if data.get("held"):
            console.print(f"  [yellow]skip[/yellow]  [bold]{data['name']}[/bold]: {escape(str(data['error']))}")
        elif data.get("kind") == "seed":
            label = f"[bold]{data['name']}[/bold]"
            if data["status"] == "skipped":
                console.print(f"  [dim]skip[/dim]  {label}")
            elif data["status"] == "error":
                console.print(f"  [red]fail[/red]  {label}: {escape(str(data.get('error')))}")
            else:
                console.print(f"  [green]done[/green]  {label} "
                              f"({data.get('row_count', 0):,} rows, {data.get('duration_ms', 0)}ms)")
    elif event == "finished":
        if data["ok"]:
            console.print(f"[green]Stream completed successfully in {data['duration_s']}s.[/green]")
        else:
            console.print(f"[red]Stream failed after {data['duration_s']}s.[/red]")
    elif event == "gc":
        console.print(f"  [dim]rewind: {data['deleted']} expired snapshot(s) cleaned up[/dim]")
    elif event == "webhook":
        if data.get("error"):
            console.print(f"[yellow]Webhook failed: {data['error']}[/yellow]")
        else:
            console.print(f"[dim]Webhook sent to {data['url']}[/dim]")


@app.command()
def lint(
    fix: Annotated[bool, typer.Option("--fix", help="Auto-fix violations")] = False,
    style: Annotated[bool, typer.Option("--style", help="Include layout/naming/capitalisation rules (off by default; correctness-only)")] = False,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """Lint SQL files in the transform directory with SQLFluff.

    By default, only correctness rules run (ambiguity, references, unused
    CTEs, NULL-equality, etc.). Pass ``--style`` for the full SQLFluff rule
    set including layout, naming, and capitalisation. A project-level
    ``.sqlfluff`` file overrides both.
    """
    from havn.config import load_project
    from havn.lint.linter import LintRefused
    from havn.lint.linter import lint as run_lint
    from havn.lint.linter import print_violations

    project_dir = _resolve_project(project_dir)
    config = load_project(project_dir)
    transform_dir = project_dir / "transform"

    action = "Fixing" if fix else "Linting"
    mode = "style+correctness" if style else "correctness-only"
    console.print(f"[bold]{action} SQL files ({mode})...[/bold]")

    try:
        count, violations, fixed = run_lint(
            transform_dir,
            fix=fix,
            dialect=config.lint.dialect,
            rules=config.lint.rules or None,
            style=style,
        )
    except LintRefused as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    print_violations(violations)

    if fix:
        if fixed > 0:
            console.print(f"[green]{fixed} fixed.[/green]")
        if count > 0:
            console.print(f"[yellow]{count} violation(s) remain (unfixable by SQLFluff).[/yellow]")
            raise typer.Exit(1)
        if fixed == 0 and count == 0:
            console.print("[green]All clean — no violations found.[/green]")
    elif count > 0:
        console.print(f"\n[red]{count} violation(s) found.[/red] Run [bold]havn lint --fix[/bold] to auto-fix.")
        raise typer.Exit(1)
    else:
        console.print("[green]All clean — no violations found.[/green]")


@app.command()
def watch(
    route: Annotated[Optional[list[str]], typer.Option("--route", help="Glob to filter watched files; only matching paths trigger a rebuild, and only that model is rebuilt. Repeatable.")] = None,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """Watch for file changes and auto-rebuild transforms.

    By default, edits to any file under ``transform/`` rebuild the full
    DAG. ``--route 'transform/gold/route_b_*.sql'`` (repeatable) narrows
    the watcher: only matching paths trigger a rebuild, and only the
    matching model is rebuilt — useful when iterating on one model
    without paying for the full pipeline on every save.
    """
    from havn.engine.scheduler import FileWatcher

    project_dir = _resolve_project(project_dir)
    console.print("[bold]Watching for changes...[/bold] (Ctrl+C to stop)")
    console.print(f"  transform/  -> auto-rebuild SQL models")
    if route:
        for r in route:
            console.print(f"  route filter: {r}")

    watcher = FileWatcher(project_dir, route_globs=list(route) if route else None)
    watcher.start()

    try:
        watcher.join()
    except KeyboardInterrupt:
        watcher.stop()
        console.print("\n[dim]Watcher stopped.[/dim]")


@app.command()
def schedule(
    history: Annotated[bool, typer.Option("--history", help="Show recorded schedule fires (runs, skips, missed fires) instead of starting the scheduler")] = False,
    name: Annotated[Optional[str], typer.Option("--name", "-n", help="With --history: only this stream or job")] = None,
    status: Annotated[Optional[str], typer.Option("--status", help="With --history: only this status (success, failed, skipped_overlap, missed, ...)")] = None,
    limit: Annotated[int, typer.Option("--limit", help="With --history: number of rows")] = 30,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """Show scheduled streams and jobs and start the scheduler.

    With --history, list what the scheduler recorded instead: one row per
    fire, with how late it started and how it ended.
    """
    from havn.config import load_project
    from havn.engine.scheduler import (
        SchedulerConfigError,
        SchedulerThread,
        collect_schedules,
        format_in_zone,
        next_run,
    )

    project_dir = _resolve_project(project_dir)
    if history:
        _print_schedule_history(project_dir, name=name, status=status, limit=limit)
        return

    config = load_project(project_dir)
    # Streams in project.yml and orchestration/*.yml jobs. A project that
    # schedules only jobs used to be told it had nothing scheduled.
    specs, errors, warnings = collect_schedules(project_dir, config)
    for w in warnings:
        console.print(f"[yellow]Warning:[/yellow] {w}")
    if errors:
        console.print("[red]The scheduler cannot start:[/red]")
        for e in errors:
            console.print(f"  [red]-[/red] {e}")
        raise typer.Exit(1)

    streams = [s for s in specs if s.kind == "stream"]
    jobs = [s for s in specs if s.kind == "job"]
    if not streams and not jobs:
        console.print("[yellow]No scheduled streams or jobs found[/yellow]")
        console.print("Add a schedule to a stream in project.yml:")
        console.print('  schedule: "0 6 * * *"  # 6am daily')
        console.print("or to a job in orchestration/<name>.yml:")
        console.print('  schedules: ["0 6 * * *"]')
        return

    for title, label, group in (("Scheduled Streams", "Stream", streams), ("Scheduled Jobs", "Job", jobs)):
        if not group:
            continue
        table = Table(title=title)
        table.add_column(label, style="bold")
        table.add_column("Schedule")
        table.add_column("Next run")
        table.add_column("Overlap")
        table.add_column("Description")
        for spec in group:
            nxt = next_run(spec)
            if nxt is None and spec.interval_expressions:
                nxt_text = "interval, counted from the last run"
            else:
                nxt_text = format_in_zone(nxt, spec.timezone)
            overlap = spec.overlap + (", catchup" if spec.catchup else "")
            table.add_row(spec.name, ", ".join(spec.expressions), nxt_text, overlap, spec.description)
        console.print(table)
        console.print()

    workers = config.scheduler.max_concurrent
    console.print(
        f"[bold]Starting scheduler...[/bold] (at most {workers} run{'s' if workers != 1 else ''} "
        "at a time; Ctrl+C to stop)"
    )
    scheduler = SchedulerThread(project_dir)
    try:
        scheduler.start()
    except SchedulerConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    try:
        scheduler.join()
    except KeyboardInterrupt:
        scheduler.stop()
        grace = int(getattr(scheduler, "shutdown_grace", 0) or 0)
        console.print(
            f"\n[dim]Stopping: no new runs start; waiting up to {grace}s for running ones "
            "(Ctrl+C again to quit now)...[/dim]"
        )
        try:
            scheduler.join()
        except KeyboardInterrupt:
            pass
        console.print("[dim]Scheduler stopped.[/dim]")


def _print_schedule_history(project_dir: Path, *, name: str | None, status: str | None, limit: int) -> None:
    """`havn schedule --history`: the newest rows of _havn.schedule_history."""
    import datetime as _dt

    from havn.config import load_project
    from havn.engine.database import open_warehouse
    from havn.engine.schedule_history import read_history
    from havn.engine.scheduler import format_in_zone

    config = load_project(project_dir)
    if not _warehouse_exists(config, project_dir):
        console.print("[yellow]No warehouse database found. Nothing has been scheduled yet.[/yellow]")
        return
    conn = open_warehouse(config, project_dir, read_only=True)
    try:
        rows = read_history(conn, limit=limit, name=name, status=status)
    finally:
        conn.close()
    if not rows:
        console.print("[yellow]No schedule history recorded yet.[/yellow]")
        return

    def _instant(text):
        if not text:
            return None
        return _dt.datetime.fromisoformat(text.rstrip("Z")).replace(tzinfo=_dt.timezone.utc)

    def _dur(ms):
        if ms is None:
            return "-"
        secs = ms / 1000
        if secs < 60:
            return f"{secs:.1f}s"
        if secs < 3600:
            return f"{secs / 60:.1f}m"
        return f"{secs / 3600:.1f}h"

    colours = {"success": "green", "failed": "red", "missed": "yellow", "skipped_overlap": "yellow",
               "interrupted": "red", "cancelled": "dim", "running": "blue", "queued": "blue"}
    table = Table(title="Schedule history (newest first)")
    table.add_column("Scheduled for")
    table.add_column("Kind")
    table.add_column("Name", style="bold")
    table.add_column("Status")
    table.add_column("Late", justify="right")
    table.add_column("Duration", justify="right")
    table.add_column("Note")
    for r in rows:
        colour = colours.get(r["status"], "white")
        note = r["error"] or ""
        if r["trigger"] == "catchup":
            note = f"catch-up run. {note}".strip()
        table.add_row(
            format_in_zone(_instant(r["scheduled_for"]), r["timezone"]),
            r["kind"],
            r["name"],
            f"[{colour}]{r['status']}[/{colour}]",
            _dur(r["lateness_ms"]),
            _dur(r["duration_ms"]),
            note[:80],
        )
    console.print(table)


# --- cdc ---


@app.command()
def cdc(
    action: Annotated[str, typer.Argument(help="Action: status, reset")] = "status",
    connector: Annotated[Optional[str], typer.Option("--connector", "-c", help="Connector name")] = None,
    table_name: Annotated[Optional[str], typer.Option("--table", "-t", help="Table name (for reset)")] = None,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory")] = None,
) -> None:
    """View and manage CDC (Change Data Capture) state.

    Examples:
      havn cdc status                         # Show all CDC state
      havn cdc status --connector prod_users  # Show state for one connector
      havn cdc reset --connector prod_users   # Reset all watermarks for a connector
      havn cdc reset --connector prod --table users  # Reset one table
    """
    from havn.config import load_project
    from havn.engine.cdc import get_cdc_status, reset_watermark
    from havn.engine.database import open_warehouse

    project_dir = _resolve_project(project_dir)
    config = load_project(project_dir)
    if not _warehouse_exists(config, project_dir):
        console.print("[yellow]No warehouse database found. Run a pipeline first.[/yellow]")
        raise typer.Exit(1)

    conn = open_warehouse(config, project_dir)
    try:
        if action == "status":
            entries = get_cdc_status(conn, connector)
            if not entries:
                console.print("[yellow]No CDC state tracked yet.[/yellow]")
                return
            tbl = Table(title="CDC State")
            tbl.add_column("Connector", style="bold")
            tbl.add_column("Table")
            tbl.add_column("Mode")
            tbl.add_column("Watermark")
            tbl.add_column("Last Sync")
            tbl.add_column("Rows", justify="right")
            for e in entries:
                tbl.add_row(
                    e["connector"],
                    e["table"],
                    e["cdc_mode"],
                    e["watermark"] or "-",
                    e["last_sync_at"][:19] if e["last_sync_at"] else "-",
                    str(e["rows_synced"]),
                )
            console.print(tbl)

        elif action == "reset":
            if not connector:
                console.print("[red]--connector is required for reset[/red]")
                raise typer.Exit(1)
            reset_watermark(conn, connector, table_name)
            target = f"{connector}.{table_name}" if table_name else connector
            console.print(f"[green]CDC state reset for {target}[/green]")

        else:
            console.print(f"[red]Unknown action: {action}[/red]")
            console.print("Available: status, reset")
            raise typer.Exit(1)
    finally:
        conn.close()
