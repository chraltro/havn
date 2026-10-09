"""CLI: ``havn perf``, the performance advisor.

``havn perf`` shows the slowest models, recent regressions and open advice.
``havn perf gold.orders`` shows one model: its build history, the captured
plan with real operator timings, regressions with their plan diff, and the
advice that applies to it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Optional

import typer

from havn.cli import _load_config, _resolve_project, _warehouse_exists, app, console


def _ms(ms: Any) -> str:
    if ms is None:
        return ""
    ms = float(ms)
    if ms >= 60_000:
        return f"{ms / 60_000:.1f} min"
    if ms >= 1000:
        return f"{ms / 1000:.2f} s"
    return f"{ms:.0f} ms"


def _rows(n: Any) -> str:
    if n is None:
        return ""
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{int(n)}"


def _bytes(n: Any) -> str:
    if not n:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


_SEV_STYLE = {"high": "red", "medium": "yellow", "low": "cyan"}


@app.command("perf")
def perf_cmd(
    model: Annotated[Optional[str], typer.Argument(help="A model to show in detail, e.g. gold.orders")] = None,
    days: Annotated[int, typer.Option("--days", "-d", help="Look-back window in days")] = 7,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Rows per table")] = 15,
    regressions: Annotated[bool, typer.Option("--regressions", help="Only show regressions")] = False,
    advice: Annotated[bool, typer.Option("--advice", help="Only show advice")] = False,
    all_advice: Annotated[bool, typer.Option("--all", help="Include dismissed and snoozed advice")] = False,
    runs: Annotated[bool, typer.Option("--runs", help="List recent pipeline runs")] = False,
    critical_path: Annotated[bool, typer.Option("--critical-path", help="Critical path of a run (--run, default the latest)")] = False,
    run: Annotated[Optional[str], typer.Option("--run", help="Pipeline run id (prefix is enough)")] = None,
    dismiss: Annotated[Optional[str], typer.Option("--dismiss", help="Dismiss this advice rule for MODEL")] = None,
    snooze: Annotated[Optional[float], typer.Option("--snooze", help="With --dismiss: snooze for this many days instead")] = None,
    reopen: Annotated[Optional[str], typer.Option("--reopen", help="Reopen a dismissed or snoozed rule for MODEL")] = None,
    show_plan: Annotated[bool, typer.Option("--plan/--no-plan", help="Show the captured plan in model detail")] = True,
    as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    env: Annotated[Optional[str], typer.Option("--env", "-e", help="Environment to use")] = None,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """Performance advisor: slow models, regressions, advice, plans, critical path.

    Every model build records its wall time, rows and, when plans are
    captured (performance.capture_plans), DuckDB's own profile of the build
    statement. Nothing is re-run to produce this.
    """
    from havn.engine.database import open_warehouse

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir, env)
    if not _warehouse_exists(config, project_dir):
        console.print("[yellow]No warehouse yet. Run [bold]havn transform[/bold] first.[/yellow]")
        return

    writing = bool(dismiss or reopen)
    if writing and not model:
        console.print("[red]--dismiss / --reopen need a MODEL argument[/red]")
        raise typer.Exit(2)

    conn = open_warehouse(config, project_dir, read_only=not writing)
    try:
        if writing:
            _set_state(conn, model, dismiss, snooze, reopen)
            return
        if runs:
            _show_runs(conn, limit, as_json)
        elif critical_path:
            _show_critical_path(conn, project_dir, run, as_json)
        elif model:
            _show_model(conn, project_dir, config, model.lower(), limit, show_plan, as_json)
        else:
            _show_summary(conn, project_dir, config, days, limit, regressions, advice, all_advice, as_json)
    finally:
        conn.close()


def _set_state(conn, model: str, dismiss: str | None, snooze: float | None, reopen: str | None) -> None:
    from havn.engine.perf import set_advice_state

    try:
        if reopen:
            res = set_advice_state(conn, model, reopen, "open")
            console.print(f"[green]Reopened[/green] {reopen} for {res['model']}")
        elif snooze:
            res = set_advice_state(conn, model, dismiss, "snoozed", days=snooze)
            console.print(f"[green]Snoozed[/green] {dismiss} for {res['model']} until {res['until'][:16]}")
        else:
            res = set_advice_state(conn, model, dismiss, "dismissed")
            console.print(f"[green]Dismissed[/green] {dismiss} for {res['model']}")
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2)


def _advice_items(conn, project_dir, config, model=None, include_dismissed=False):
    from havn.engine.perf import compute_advice

    return compute_advice(
        conn, project_dir, config.performance.advice,
        model=model, include_dismissed=include_dismissed, exposures=list(config.exposures),
    )


def _show_summary(conn, project_dir, config, days, limit, only_reg, only_advice, all_advice, as_json) -> None:
    from rich.table import Table

    from havn.engine.perf import list_regressions, slowest_models

    want_slow = not (only_reg or only_advice)
    want_reg = not only_advice
    want_adv = not only_reg
    slow = slowest_models(conn, days=days, limit=limit) if want_slow else []
    regs = list_regressions(conn, days=days, limit=limit) if want_reg else []
    items = _advice_items(conn, project_dir, config, include_dismissed=all_advice) if want_adv else []

    if as_json:
        console.print_json(json.dumps({
            "slowest": slow, "regressions": regs, "advice": [i.to_dict() for i in items],
        }, default=str))
        return

    if want_slow:
        if not slow:
            console.print(f"[dim]No builds recorded in the last {days} day(s).[/dim]")
        else:
            t = Table(title=f"Slowest models, last {days} day(s) (by median build time)")
            t.add_column("Model", style="bold")
            t.add_column("Type", style="dim")
            t.add_column("Builds", justify="right")
            t.add_column("Median", justify="right")
            t.add_column("Last", justify="right")
            t.add_column("Max", justify="right")
            t.add_column("Rows", justify="right")
            t.add_column("Peak mem", justify="right")
            t.add_column("Spilled", justify="right")
            for s in slow:
                t.add_row(
                    s["model_path"], s.get("materialized") or "", str(s["builds"]),
                    _ms(s["median_ms"]), _ms(s["last_ms"]), _ms(s["max_ms"]),
                    _rows(s.get("last_rows")), _bytes(s.get("peak_memory_bytes")),
                    _bytes(s.get("spill_bytes")),
                )
            console.print(t)

    if want_reg:
        console.print()
        if not regs:
            console.print(f"[green]No regressions in the last {days} day(s).[/green]")
        else:
            console.print(f"[bold]Regressions[/bold] ({len(regs)})")
            for r in regs:
                console.print(f"  [yellow]slower[/yellow]  {r['message']}  [dim]{str(r['detected_at'])[:16]}[/dim]")

    if want_adv:
        console.print()
        if not items:
            console.print("[green]No performance advice.[/green]")
        else:
            console.print(f"[bold]Advice[/bold] ({len(items)})")
            for i in items:
                sev = _SEV_STYLE.get(i.severity, "white")
                state = f" [dim]({i.status})[/dim]" if i.status != "open" else ""
                console.print(f"  [{sev}]{i.severity:<6}[/{sev}] [bold]{i.model}[/bold]  {i.title}{state}")
                console.print(f"         [dim]{i.rule}  ·  havn perf {i.model} for the evidence[/dim]")
    console.print()
    console.print("[dim]havn perf <model> for one model's plan and history; --critical-path for the last run.[/dim]")


def _show_model(conn, project_dir, config, model, limit, show_plan, as_json) -> None:
    from rich.table import Table

    from havn.engine.perf import get_build, list_regressions, model_history

    history = model_history(conn, model, limit=limit, status=None)
    if not history:
        console.print(f"[yellow]No builds recorded for {model}.[/yellow]")
        return
    plan_row = next((h for h in history if h.get("plan_captured") and h.get("status") == "success"), None)
    plan = (get_build(conn, plan_row["id"]) or {}).get("plan") if plan_row else None
    regs = list_regressions(conn, days=365, model=model, limit=10)
    items = _advice_items(conn, project_dir, config, model=model, include_dismissed=True)

    if as_json:
        console.print_json(json.dumps({
            "model": model, "history": history, "plan": plan, "regressions": regs,
            "advice": [i.to_dict() for i in items],
        }, default=str))
        return

    ok = [h for h in history if h.get("status") == "success"]
    durations = sorted(float(h["duration_ms"] or 0) for h in ok)
    med = durations[len(durations) // 2] if durations else 0
    last = history[0]
    console.print(f"[bold]{model}[/bold]  [dim]{last.get('materialized') or ''}[/dim]")
    console.print(
        f"  median {_ms(med)} over {len(ok)} build(s), last {_ms(last.get('duration_ms'))}"
        f" ({last.get('status')}), {_rows(last.get('rows_out'))} rows"
        + (f", peak memory {_bytes(last.get('peak_memory_bytes'))}" if last.get("peak_memory_bytes") else "")
        + (f", spilled {_bytes(last.get('spill_bytes'))}" if last.get("spill_bytes") else "")
    )

    t = Table(title="Recent builds")
    t.add_column("Finished")
    t.add_column("Status")
    t.add_column("Duration", justify="right")
    t.add_column("Rows out", justify="right")
    t.add_column("Rows in", justify="right")
    t.add_column("Scanned", justify="right")
    t.add_column("Plan")
    for h in history[:limit]:
        st = h.get("status")
        t.add_row(
            str(h.get("finished_at") or "")[:19].replace("T", " "),
            f"[green]{st}[/green]" if st == "success" else f"[red]{st}[/red]",
            _ms(h.get("duration_ms")), _rows(h.get("rows_out")), _rows(h.get("rows_in")),
            _rows(h.get("rows_scanned")), "yes" if h.get("plan_captured") else "",
        )
    console.print(t)

    if plan:
        tops = plan_row.get("top_operators") or []
        if tops:
            console.print("[bold]Most expensive operators[/bold] (latest captured plan)")
            for op in tops:
                where = f" on {op['table']}" if op.get("table") else ""
                console.print(
                    f"  {op['operator']}{where}: {_ms(op['time_ms'])}"
                    + (f" ({op['pct']}%)" if op.get("pct") is not None else "")
                    + (f", {_rows(op['rows'])} rows" if op.get("rows") is not None else "")
                )
        if show_plan:
            console.print(_plan_tree(plan))
    else:
        console.print("[dim]No plan captured yet (performance.capture_plans).[/dim]")

    if regs:
        console.print("[bold]Regressions[/bold]")
        for r in regs:
            console.print(f"  [yellow]slower[/yellow]  {r['message']}  [dim]{str(r['detected_at'])[:16]}[/dim]")
            diff = r.get("plan_diff") or {}
            for line in (diff.get("summary") or [])[1:4]:
                console.print(f"           [dim]{line}[/dim]")
    if items:
        console.print("[bold]Advice[/bold]")
        for i in items:
            sev = _SEV_STYLE.get(i.severity, "white")
            state = f" [dim]({i.status})[/dim]" if i.status != "open" else ""
            console.print(f"  [{sev}]{i.severity}[/{sev}]  {i.title}{state}  [dim]{i.rule}[/dim]")
            console.print(f"    {i.explanation}")
            for line in i.suggestion.splitlines():
                console.print(f"    [green]{line}[/green]")
        console.print(f"[dim]havn perf {model} --dismiss <rule> (or add --snooze 7) to hide one.[/dim]")


def _plan_tree(plan: dict):
    from rich.tree import Tree

    def label(node: dict) -> str:
        parts = [f"[bold]{node.get('operator')}[/bold]"]
        if node.get("table"):
            parts.append(f"[cyan]{node['table']}[/cyan]")
        if node.get("actual_rows") is not None:
            parts.append(f"{_rows(node['actual_rows'])} rows")
        if node.get("rows_scanned"):
            parts.append(f"[dim]scanned {_rows(node['rows_scanned'])}[/dim]")
        t = node.get("actual_time_ms")
        if t is not None:
            color = "red" if t >= 1000 else "yellow" if t >= 100 else "green"
            parts.append(f"[{color}]{_ms(t)}[/{color}]")
        return "  ".join(parts)

    def add(tree, node):
        branch = tree.add(label(node))
        for child in node.get("children") or []:
            add(branch, child)

    root = Tree(label(plan))
    for child in plan.get("children") or []:
        add(root, child)
    return root


def _show_runs(conn, limit, as_json) -> None:
    from rich.table import Table

    from havn.engine.perf import recent_runs

    rows = recent_runs(conn, limit=limit)
    if as_json:
        console.print_json(json.dumps(rows, default=str))
        return
    if not rows:
        console.print("[dim]No runs recorded yet.[/dim]")
        return
    t = Table(title="Recent pipeline runs")
    t.add_column("Run")
    t.add_column("Started")
    t.add_column("Models", justify="right")
    t.add_column("Wall", justify="right")
    t.add_column("Busy", justify="right")
    t.add_column("Failures", justify="right")
    for r in rows:
        t.add_row(
            r["pipeline_run_id"][:8], str(r["started_at"])[:19].replace("T", " "), str(r["builds"]),
            _ms(r["wall_ms"]), _ms(r["busy_ms"]), str(r["failures"] or 0),
        )
    console.print(t)
    console.print("[dim]havn perf --critical-path --run <id> for what set a run's length.[/dim]")


def _show_critical_path(conn, project_dir, run, as_json) -> None:
    from havn.engine.perf import critical_path, recent_runs, run_builds
    from havn.engine.transform.discovery import discover_all_models

    runs = recent_runs(conn, limit=200)
    if not runs:
        console.print("[dim]No runs recorded yet.[/dim]")
        return
    if run:
        match = [r for r in runs if r["pipeline_run_id"].startswith(run)]
        if not match:
            console.print(f"[red]No recorded run starts with {run}[/red]")
            raise typer.Exit(1)
        run_id = match[0]["pipeline_run_id"]
    else:
        run_id = runs[0]["pipeline_run_id"]
    deps = {m.full_name: list(m.depends_on) for m in discover_all_models(project_dir)}
    cp = critical_path(run_builds(conn, run_id), deps)
    cp["pipeline_run_id"] = run_id
    if as_json:
        console.print_json(json.dumps(cp, default=str))
        return
    console.print(
        f"[bold]Run {run_id[:8]}[/bold]: {_ms(cp['wall_ms'])} wall clock, {_ms(cp['busy_ms'])} of builds "
        f"across {len(cp['models'])} model(s) in {cp['tiers']} tier(s)"
        + (f", {cp['parallelism']}x parallel" if cp.get("parallelism") else "")
    )
    console.print("[bold]Critical path[/bold] (what the run actually waited for)")
    for step in cp["path"]:
        wait = f"  [dim]waited {_ms(step['wait_ms'])}[/dim]" if step["wait_ms"] else ""
        console.print(
            f"  tier {step['tier']}  [bold]{step['model']}[/bold]  {_ms(step['duration_ms'])}"
            + (f" ({step['share_pct']}% of the run)" if step.get("share_pct") is not None else "")
            + wait
        )
    console.print(
        f"  [dim]{_ms(cp['path_ms'])} building + {_ms(cp['wait_ms'])} waiting on this chain[/dim]"
    )
    chain = " -> ".join(c["model"] for c in cp["longest_chain"])
    console.print(
        f"[bold]Floor[/bold]: the longest dependency chain takes {_ms(cp['longest_chain_ms'])} "
        f"({chain}); more workers cannot make this run shorter than that."
    )
