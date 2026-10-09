"""CLI: ``havn ask`` (questions over the semantic layer) and ``havn changes``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, List, Optional

import typer
from rich.table import Table

from havn.cli import _load_config, _resolve_project, app, console

_ProjectOpt = Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")]
_EnvOpt = Annotated[Optional[str], typer.Option("--env", "-e", help="Environment to use")]

_HISTORY_FILE = ("ask", "history.json")
_MAX_SHOWN_ROWS = 50


def _history_path(project_dir: Path) -> Path:
    return project_dir / ".havn" / _HISTORY_FILE[0] / _HISTORY_FILE[1]


def _load_history(project_dir: Path) -> list[dict]:
    from havn.textio import read_project_text

    path = _history_path(project_dir)
    if not path.is_file():
        return []
    try:
        data = json.loads(read_project_text(path))
        return [t for t in data if isinstance(t, dict) and t.get("question")][-10:]
    except Exception:
        return []


def _save_history(project_dir: Path, history: list[dict]) -> None:
    path = _history_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history[-10:], indent=1), encoding="utf-8")


def _run_ask(project_dir: Path, env: str | None, body: dict) -> dict:
    """Ask through a running `havn serve`, or locally when there is none."""
    from havn.engine.ai.config import AIConfigError
    from havn.engine.ai.service import ServerAskError, call_server, local_context

    try:
        result = call_server(project_dir, "POST", "/api/ask", body)
    except ServerAskError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    if result is not None:
        return result
    from havn.engine.ai.ask import ask

    try:
        with local_context(project_dir, env=env) as ctx:
            return ask(
                body["question"], ctx, history=body.get("history"),
                exploratory=body.get("exploratory", False), summarize=body.get("summarize", False),
            )
    except AIConfigError as e:
        console.print(f"[red]{e}[/red]")
        console.print("Configure the [bold]ai:[/bold] section of project.yml; see docs/ask.md.")
        raise typer.Exit(1)


def _spec_line(spec: dict) -> str:
    parts = [", ".join(spec.get("metrics") or [])]
    if spec.get("dimensions"):
        parts.append("by " + ", ".join(spec["dimensions"]))
    if spec.get("grain"):
        parts.append(f"per {spec['grain']}")
    for f in spec.get("filters") or []:
        parts.append(f"where {f['dimension']} {f['op']} {f['value']!r}")
    if spec.get("start") or spec.get("end"):
        parts.append(f"from {spec.get('start') or '...'} to {spec.get('end') or '...'}")
    for o in spec.get("order_by") or []:
        parts.append(f"order {o['field']} {o['direction']}")
    if spec.get("limit"):
        parts.append(f"limit {spec['limit']}")
    return "  ".join(parts)


def _print_rows(columns: list[str], rows: list[list], title: str | None = None) -> None:
    table = Table(title=title)
    for col in columns:
        table.add_column(str(col), max_width=40)
    for row in rows[:_MAX_SHOWN_ROWS]:
        table.add_row(*["" if v is None else str(v) for v in row])
    console.print(table)
    if len(rows) > _MAX_SHOWN_ROWS:
        console.print(f"[dim]{len(rows) - _MAX_SHOWN_ROWS} more rows (use --json for all)[/dim]")


def _print_answer(result: dict, *, show_sql: bool) -> None:
    status = result.get("status")
    if result.get("error"):
        console.print(f"[red]{result['error']}[/red]")
    if result.get("explanation"):
        console.print(result["explanation"])
    if status == "answered":
        spec = result.get("spec") or {}
        console.print(f"[bold]spec[/bold]  {_spec_line(spec)}")
        res = result.get("result") or {}
        console.print()
        _print_rows(res.get("columns", []), res.get("rows", []))
        if res.get("truncated"):
            console.print("[yellow]Result capped at ai.max_rows.[/yellow]")
        if result.get("summary"):
            console.print(f"\n{result['summary']}")
        console.print()
        for m in result.get("metrics", []):
            console.print(f"[bold]metric[/bold] {m['name']} = {m['measure']} on {m['model']}"
                          + (f"  [dim]({m['source_path']})[/dim]" if m.get("source_path") else ""))
        for lin in result.get("lineage", []):
            chain = [n["name"] for n in lin.get("nodes", [])]
            console.print(f"[bold]lineage[/bold] {' <- '.join(chain)}"
                          + (f"  [dim]sources: {', '.join(lin.get('sources', []))}[/dim]" if lin.get("sources") else ""))
        for f in result.get("freshness", []):
            if f.get("never_built"):
                console.print(f"[bold]fresh[/bold]  {f['model']}: never built")
            else:
                mark = "[yellow]stale[/yellow]" if f.get("is_stale") else "[green]fresh[/green]"
                console.print(f"[bold]fresh[/bold]  {f['model']}: built {f['last_run_at']} "
                              f"({f['hours_since_run']}h ago) {mark}")
        if show_sql and result.get("sql"):
            console.print("\n[bold]SQL[/bold]")
            console.print(result["sql"], markup=False, highlight=False)
    elif status == "clarify":
        console.print(f"[bold]?[/bold] {result.get('clarification')}")
    elif status in ("unanswerable", "exploratory"):
        console.print("[yellow]No defined metric answers this.[/yellow]")
        closest = result.get("closest_metrics") or []
        if closest:
            console.print("Closest metrics: " + ", ".join(
                c["name"] + (f" ({c['description']})" if c.get("description") else "") for c in closest
            ))
        sug = result.get("suggested_metric")
        if sug and sug.get("yaml"):
            console.print(f"\nA metric you could add as [bold]{sug['path']}[/bold]:")
            console.print(sug["yaml"], markup=False, highlight=False)
        elif sug and sug.get("errors"):
            console.print("[dim]A suggested metric was discarded: " + "; ".join(sug["errors"]) + "[/dim]")
        exp = result.get("exploratory")
        if exp:
            console.print("\n[bold yellow]Exploratory SQL (unverified, not from the semantic layer)[/bold yellow]")
            console.print(exp.get("sql") or "", markup=False, highlight=False)
            if exp.get("error"):
                console.print(f"[red]{exp['error']}[/red]")
            elif exp.get("result"):
                _print_rows(exp["result"]["columns"], exp["result"]["rows"])
        elif result.get("exploratory_available"):
            console.print("[dim]Re-run with --exploratory for an unverified SQL attempt.[/dim]")
    for w in result.get("warnings", []):
        console.print(f"[yellow]warn[/yellow]  {w}")
    sent = result.get("data_sent_to_model") or {}
    shared = [k for k in ("dimension_values", "result_rows") if sent.get(k)]
    console.print(f"[dim]{result.get('provider', '')}: sent catalog metadata"
                  + (f" and {', '.join(shared).replace('_', ' ')}" if shared else ", no row data") + "[/dim]")


@app.command("ask")
def ask_cmd(
    words: Annotated[Optional[List[str]], typer.Argument(help="The question, or eval files with --eval")] = None,
    eval_mode: Annotated[bool, typer.Option("--eval", help="Run question -> spec eval files (default tests/ask/*.yml)")] = False,
    cont: Annotated[bool, typer.Option("--continue", "-c", help="Refine the previous question ('now by month')")] = False,
    exploratory: Annotated[bool, typer.Option("--exploratory", help="When no metric fits, try unverified exploratory SQL (needs ai.exploratory_sql)")] = False,
    summarize: Annotated[bool, typer.Option("--summarize", help="Summarise the result in words (sends result rows to the model; needs ai.summarize_results)")] = False,
    save_suggestion: Annotated[bool, typer.Option("--save-suggestion", help="Write a suggested metric definition to metrics/")] = False,
    sql: Annotated[bool, typer.Option("--sql/--no-sql", help="Print the compiled SQL")] = True,
    json_output: Annotated[bool, typer.Option("--json", help="Print the full answer as JSON")] = False,
    min_accuracy: Annotated[float, typer.Option("--min-accuracy", help="With --eval: exit non-zero below this accuracy (0-1)")] = 1.0,
    env: _EnvOpt = None,
    project_dir: _ProjectOpt = None,
) -> None:
    """Ask a question; havn answers it from the metrics in metrics/*.yml.

    The model picks metrics, dimensions, grain, filters and a time range from
    the catalog; havn validates that choice, compiles it with the semantic
    layer and runs it read-only. Only catalog metadata is sent to the model.
    """
    project_dir = _resolve_project(project_dir)
    _load_config(project_dir, env)  # loads .env, validates --env
    if eval_mode:
        _run_eval(project_dir, env, words or ["tests/ask/*.yml"], json_output, min_accuracy)
        return

    question = " ".join(words or []).strip()
    if not question:
        console.print("[red]Ask a question, e.g.[/red] havn ask \"revenue by region last month\"")
        raise typer.Exit(1)
    history = _load_history(project_dir) if cont else []
    body = {
        "question": question,
        "history": history,
        "exploratory": exploratory,
        "summarize": summarize,
    }
    result = _run_ask(project_dir, env, body)
    _save_history(project_dir, history + [{"question": question, "spec": result.get("spec")}])

    if json_output:
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_answer(result, show_sql=sql)

    sug = result.get("suggested_metric")
    if save_suggestion and sug and sug.get("yaml"):
        from havn.engine.ai.service import accept_suggested_metric

        try:
            path = accept_suggested_metric(project_dir, sug["definition"], sug.get("path"))
            console.print(f"[green]Saved[/green] {path}")
        except ValueError as e:
            console.print(f"[red]Could not save the suggestion:[/red] {e}")
            raise typer.Exit(1)
    if result.get("status") == "error":
        raise typer.Exit(1)


def _run_eval(project_dir: Path, env: str | None, patterns: list[str], json_output: bool, min_accuracy: float) -> None:
    from havn.engine.ai.config import AIConfigError
    from havn.engine.ai.evaluate import expand_paths, load_cases, run_eval
    from havn.engine.ai.service import ServerAskError, call_server, local_context

    files = expand_paths(patterns, project_dir)
    if not files:
        console.print(f"[red]No eval files match {' '.join(patterns)}[/red]")
        raise typer.Exit(1)
    rel = []
    for f in files:
        try:
            rel.append(f.resolve().relative_to(project_dir.resolve()).as_posix())
        except ValueError:
            rel.append(str(f))
    try:
        report = call_server(project_dir, "POST", "/api/ask/eval", {"paths": rel})
    except ServerAskError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    if report is None:
        cases, errors = load_cases(files)
        for e in errors:
            console.print(f"[red]eval file error:[/red] {e}")
        if not cases:
            raise typer.Exit(1)
        try:
            with local_context(project_dir, env=env) as ctx:
                report = run_eval(cases, ctx)
        except AIConfigError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        report["load_errors"] = errors

    if json_output:
        print(json.dumps(report, indent=2, default=str))
    else:
        table = Table(title=f"ask eval ({report.get('provider', '')})")
        table.add_column("", width=4)
        table.add_column("Case")
        table.add_column("Question", max_width=40)
        table.add_column("Detail", max_width=60)
        for r in report["results"]:
            mark = "[green]pass[/green]" if r["passed"] else "[red]FAIL[/red]"
            detail = "; ".join(r.get("mismatches") or []) or r.get("kind", "")
            table.add_row(mark, r["name"], r["question"], detail)
        console.print(table)
        acc = report.get("accuracy")
        console.print(f"{report['passed']}/{report['total']} passed"
                      + (f" ({acc:.0%})" if acc is not None else ""))
    acc = report.get("accuracy") or 0.0
    if acc < min_accuracy:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# havn changes: change sets proposed by agents
# ---------------------------------------------------------------------------

changes_app = typer.Typer(
    help="Change sets: agent-proposed model edits, verified before they are applied.",
    no_args_is_help=False,
    invoke_without_command=True,
)
app.add_typer(changes_app, name="changes")


@changes_app.callback()
def _changes_main(ctx: typer.Context, project_dir: _ProjectOpt = None) -> None:
    """List open change sets (default), or use a subcommand."""
    if ctx.invoked_subcommand is None:
        _list_changes(_resolve_project(project_dir), show_all=False)


@changes_app.command("list")
def changes_list(
    all_: Annotated[bool, typer.Option("--all", help="Include applied and discarded")] = False,
    project_dir: _ProjectOpt = None,
) -> None:
    """List change sets."""
    _list_changes(_resolve_project(project_dir), show_all=all_)


def _list_changes(project_dir: Path, show_all: bool) -> None:
    from havn.engine.changesets import list_change_sets

    items = list_change_sets(project_dir, include_closed=show_all)
    if not items:
        console.print("[dim]No change sets.[/dim]")
        return
    table = Table(title="Change sets")
    for col in ("Id", "Status", "Source", "Title", "Files", "Updated"):
        table.add_column(col)
    colors = {"ready": "green", "failed": "red", "applied": "cyan", "discarded": "dim"}
    for cs in items:
        color = colors.get(cs.status, "yellow")
        table.add_row(cs.id, f"[{color}]{cs.status}[/{color}]", cs.source, cs.title,
                      str(len(cs.files)), cs.updated_at)
    console.print(table)


@changes_app.command("show")
def changes_show(
    change_set_id: Annotated[str, typer.Argument(help="Change set id")],
    diff: Annotated[bool, typer.Option("--diff", help="Show the file diffs")] = False,
    project_dir: _ProjectOpt = None,
) -> None:
    """Show a change set's files and verification report."""
    from havn.engine.changesets import ChangeSetError, get_change_set
    from havn.engine.changesets.service import report_text

    project_dir = _resolve_project(project_dir)
    try:
        cs = get_change_set(project_dir, change_set_id)
    except ChangeSetError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(report_text(cs), markup=False, highlight=False)
    if diff:
        import difflib

        for f in cs.files:
            lines = difflib.unified_diff(
                (f.base or "").splitlines(keepends=True),
                (f.content or "").splitlines(keepends=True),
                fromfile=f"a/{f.path}", tofile=f"b/{f.path}",
            )
            console.print("".join(lines), markup=False, highlight=False)


@changes_app.command("verify")
def changes_verify(
    change_set_id: Annotated[str, typer.Argument(help="Change set id")],
    env: _EnvOpt = None,
    project_dir: _ProjectOpt = None,
) -> None:
    """Re-run verification (through `havn serve` when it is running)."""
    from havn.engine.ai.service import ServerAskError, call_server
    from havn.engine.changesets import ChangeSet, ChangeSetError
    from havn.engine.changesets.service import report_text, verify_locally

    project_dir = _resolve_project(project_dir)
    try:
        data = call_server(project_dir, "POST", f"/api/changesets/{change_set_id}/verify", {})
        cs = ChangeSet.from_dict(data) if data is not None else verify_locally(project_dir, change_set_id, env=env)
    except (ServerAskError, ChangeSetError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(report_text(cs), markup=False, highlight=False)
    if cs.status != "ready":
        raise typer.Exit(1)


@changes_app.command("apply")
def changes_apply(
    change_set_id: Annotated[str, typer.Argument(help="Change set id")],
    force: Annotated[bool, typer.Option("--force", help="Apply even though verification failed")] = False,
    project_dir: _ProjectOpt = None,
) -> None:
    """Write a verified change set into the project."""
    from havn.engine.changesets import ChangeSetError, apply_change_set

    project_dir = _resolve_project(project_dir)
    try:
        cs = apply_change_set(project_dir, change_set_id, force=force, user="cli")
    except ChangeSetError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    for f in cs.files:
        console.print(f"[green]{f.action}[/green] {f.path}")
    console.print("Applied. Run [bold]havn transform[/bold] to build the changed models.")


@changes_app.command("discard")
def changes_discard(
    change_set_id: Annotated[str, typer.Argument(help="Change set id")],
    project_dir: _ProjectOpt = None,
) -> None:
    """Discard a change set without applying it."""
    from havn.engine.changesets import ChangeSetError, discard_change_set

    project_dir = _resolve_project(project_dir)
    try:
        discard_change_set(project_dir, change_set_id)
    except ChangeSetError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"Discarded {change_set_id}.")
