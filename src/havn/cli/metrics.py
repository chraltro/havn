"""CLI: ``havn metrics`` — list, compile, and query semantic-layer metrics."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, List, Optional

import typer
from rich.table import Table

from havn.cli import _load_config, _resolve_project, _warehouse_exists, console

metrics_app = typer.Typer(
    help="Semantic layer: declarative metrics defined in metrics/*.yml.",
    no_args_is_help=False,
    invoke_without_command=True,
)

_ProjectOpt = Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")]
_EnvOpt = Annotated[Optional[str], typer.Option("--env", "-e", help="Environment to use")]


@metrics_app.callback()
def _main(
    ctx: typer.Context,
    project_dir: _ProjectOpt = None,
) -> None:
    """List declared metrics (default), or use a subcommand."""
    if ctx.invoked_subcommand is None:
        _print_metric_list(project_dir)


@metrics_app.command("list")
def list_metrics_cmd(project_dir: _ProjectOpt = None) -> None:
    """List all metrics declared in metrics/*.yml."""
    _print_metric_list(project_dir)


def _print_metric_list(project_dir: Path | None) -> None:
    from havn.engine.semantic import load_metrics

    project_dir = _resolve_project(project_dir)
    metrics, errors = load_metrics(project_dir)

    if not metrics and not errors:
        console.print("[yellow]No metrics defined.[/yellow]")
        console.print(
            "Create [bold]metrics/<name>.yml[/bold] with a [bold]metrics:[/bold] "
            "list to get started. See the semantic layer docs."
        )
        return

    if metrics:
        table = Table(title="Metrics")
        table.add_column("Name", style="bold")
        table.add_column("Model", style="cyan")
        table.add_column("Measure")
        table.add_column("Dimensions")
        table.add_column("Time")
        table.add_column("Description", max_width=40)
        for m in metrics.values():
            table.add_row(
                m.name,
                m.model,
                m.measure,
                ", ".join(m.dimensions),
                m.time_dimension or "",
                m.description,
            )
        console.print(table)

    for err in errors:
        console.print(f"[red]definition error:[/red] {err}")


def _compile(
    project_dir: Path,
    name: str,
    by: list[str],
    grain: str | None,
    start: str | None,
    end: str | None,
    limit: int | None,
) -> str:
    from havn.engine.semantic import SemanticError, compile_metric, get_metric

    try:
        metric = get_metric(project_dir, name)
        return compile_metric(
            metric, dimensions=by, grain=grain, start=start, end=end, limit=limit,
        )
    except SemanticError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


_ByOpt = Annotated[Optional[List[str]], typer.Option("--by", help="Group by this dimension (repeatable)")]
_GrainOpt = Annotated[Optional[str], typer.Option("--grain", help="Time grain: hour, day, week, month, quarter, year")]
_StartOpt = Annotated[Optional[str], typer.Option("--start", help="Inclusive lower bound on the time dimension")]
_EndOpt = Annotated[Optional[str], typer.Option("--end", help="Exclusive upper bound on the time dimension")]
_LimitOpt = Annotated[Optional[int], typer.Option("--limit", "-n", help="Max rows to return")]


@metrics_app.command("sql")
def metric_sql(
    name: Annotated[str, typer.Argument(help="Metric name")],
    by: _ByOpt = None,
    grain: _GrainOpt = None,
    start: _StartOpt = None,
    end: _EndOpt = None,
    limit: _LimitOpt = None,
    project_dir: _ProjectOpt = None,
) -> None:
    """Print the SQL a metric query compiles to, without executing it."""
    project_dir = _resolve_project(project_dir)
    sql = _compile(project_dir, name, by or [], grain, start, end, limit)
    # Plain print (not rich) so output can be piped into other tools.
    print(sql)


@metrics_app.command("query")
def metric_query(
    name: Annotated[str, typer.Argument(help="Metric name")],
    by: _ByOpt = None,
    grain: _GrainOpt = None,
    start: _StartOpt = None,
    end: _EndOpt = None,
    limit: _LimitOpt = None,
    csv: Annotated[bool, typer.Option("--csv", help="Output as CSV")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    env: _EnvOpt = None,
    project_dir: _ProjectOpt = None,
) -> None:
    """Compile a metric to SQL and run it against the warehouse."""
    from havn.cli.query import _fetch_via_server
    from havn.engine.database import open_warehouse

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir, env)
    sql = _compile(project_dir, name, by or [], grain, start, end, limit)

    # Route through a running `havn serve` (it holds the warehouse lock).
    fetched = _fetch_via_server(project_dir, sql)
    if fetched is not None:
        columns, rows, _label, _truncated = fetched
    else:
        if not _warehouse_exists(config, project_dir):
            console.print("[yellow]No warehouse database found. Run a pipeline first.[/yellow]")
            raise typer.Exit(1)
        conn = open_warehouse(config, project_dir, read_only=True)
        try:
            result = conn.execute(sql)
            columns = [d[0] for d in result.description] if result.description else []
            rows = result.fetchall()
        except Exception as e:
            console.print(f"[red]Metric query error:[/red] {e}")
            raise typer.Exit(1)
        finally:
            conn.close()

    if csv:
        import csv as _csv
        import io as _io
        buf = _io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(row)
        print(buf.getvalue().rstrip())
    elif json_output:
        import json as _json
        print(_json.dumps([dict(zip(columns, row)) for row in rows], indent=2, default=str))
    else:
        table = Table(title=name)
        for col in columns:
            table.add_column(col, max_width=60)
        for row in rows:
            table.add_row(*[str(v) for v in row])
        console.print(table)
        console.print(f"[dim]{len(rows)} rows[/dim]")
