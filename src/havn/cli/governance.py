"""Governance CLI: havn pii (classifications) and havn rls (row policies)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.table import Table

from havn.cli import _load_config, _resolve_project, _warehouse_exists, app, console


def _open(project_dir: Optional[Path], env: Optional[str]):
    from havn.engine.database import ensure_meta_table, open_warehouse

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir, env)
    if not _warehouse_exists(config, project_dir):
        console.print("[yellow]No warehouse database found.[/yellow]")
        raise typer.Exit(1)
    conn = open_warehouse(config, project_dir)
    ensure_meta_table(conn)
    return conn, project_dir, config


@app.command("pii")
def pii(
    relation: Annotated[Optional[str], typer.Argument(help="Only this schema.table")] = None,
    env: Annotated[Optional[str], typer.Option("--env", "-e", help="Environment to use")] = None,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory")] = None,
) -> None:
    """Show PII classifications, including those inherited through lineage.

    A column is classified by a masking policy, an @pii tag in its model, or
    by being derived from a classified column (unless the model says
    @declassify). Inherited masking applies at query time like explicit policies.
    """
    from havn.engine.governance.report import governance_warnings, pii_report

    conn, project_dir, config = _open(project_dir, env)
    try:
        report = pii_report(conn, project_dir)
        warnings = governance_warnings(conn, project_dir, config)
    finally:
        conn.close()
    table = Table(title="Classified columns")
    for col in ("Relation", "Column", "Source", "From", "Masking"):
        table.add_column(col)
    rows = 0
    for entry in report["classifications"]:
        if relation and entry["relation"] != relation.lower():
            continue
        for c in entry["columns"]:
            rows += 1
            masking = c.get("method") or "[red]none[/red]"
            table.add_row(entry["relation"], c["column"], c["source"], ", ".join(c.get("from") or []), masking)
    if rows:
        console.print(table)
    else:
        console.print("[dim]No classified columns.[/dim]")
    if report.get("declassified"):
        console.print("\n[bold]Declassified[/bold]")
        for d in report["declassified"]:
            console.print(f"  {d['relation']}.{d['column']}: {d['reason'] or '(no reason given)'}")
    inherited = [p for p in report["row_policies"] if p.get("inherited_from")]
    if inherited:
        console.print("\n[bold]Inherited row policies[/bold]")
        for p in inherited:
            what = "shows its subjects no rows" if p["deny"] else p["filter_sql"]
            console.print(f"  {p['relation']} (from {p['inherited_from']}): {what}")
    for w in warnings:
        console.print(f"[yellow]warning[/yellow] {w.model}: {w.message}")


@app.command("rls")
def rls(
    action: Annotated[str, typer.Argument(help="Action: list, add, remove")],
    table_name: Annotated[Optional[str], typer.Option("--table", "-t", help="schema.table")] = None,
    filter_sql: Annotated[Optional[str], typer.Option("--filter", "-f", help="SQL boolean filter")] = None,
    roles: Annotated[Optional[str], typer.Option("--roles", help="Comma list of roles it applies to")] = None,
    users: Annotated[Optional[str], typer.Option("--users", help="Comma list of users it applies to")] = None,
    exempt: Annotated[Optional[str], typer.Option("--exempt", help="Exempt roles (default: admin)")] = None,
    name: Annotated[Optional[str], typer.Option("--name", help="Policy name")] = None,
    policy_id: Annotated[Optional[str], typer.Option("--id", help="Policy ID (for remove)")] = None,
    env: Annotated[Optional[str], typer.Option("--env", "-e", help="Environment to use")] = None,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory")] = None,
) -> None:
    """Manage row-level security policies.

    Examples:
        havn rls list
        havn rls add -t silver.customers -f "region = havn_attr('region')" --roles viewer,editor
        havn rls remove --id <policy-id>
    """
    from havn.engine.row_policies import (
        create_row_policy,
        delete_row_policy,
        ensure_row_policy_table,
        load_row_policies,
    )

    def split(v: Optional[str]) -> list[str] | None:
        return [x.strip() for x in v.split(",") if x.strip()] if v else None

    conn, _project_dir, _config = _open(project_dir, env)
    try:
        ensure_row_policy_table(conn)
        if action == "list":
            policies = load_row_policies(conn)
            if not policies:
                console.print("[dim]No row policies.[/dim]")
                return
            t = Table(title="Row policies")
            for col in ("ID", "Table", "Filter", "Applies to", "Exempt", "Enabled"):
                t.add_column(col)
            for p in policies:
                applies = ", ".join(p["applies_to_roles"] + p["applies_to_users"]) or "everyone"
                t.add_row(p["id"][:12], f"{p['schema_name']}.{p['table_name']}", p["filter_sql"],
                          applies, ", ".join(p["exempted_roles"] + p["exempted_users"]),
                          "yes" if p["enabled"] else "no")
            console.print(t)
        elif action == "add":
            if not table_name or "." not in table_name or not filter_sql:
                console.print("[red]add needs --table schema.table and --filter[/red]")
                raise typer.Exit(1)
            schema, _, rel = table_name.partition(".")
            try:
                p = create_row_policy(
                    conn, schema_name=schema, table_name=rel, filter_sql=filter_sql, name=name,
                    applies_to_roles=split(roles), applies_to_users=split(users),
                    exempted_roles=split(exempt) if exempt is not None else None,
                    created_by="cli",
                )
            except ValueError as e:
                console.print(f"[red]{e}[/red]")
                raise typer.Exit(1)
            console.print(f"[green]Row policy created[/green] {p['id']}")
        elif action == "remove":
            if not policy_id:
                console.print("[red]remove needs --id[/red]")
                raise typer.Exit(1)
            matches = [p["id"] for p in load_row_policies(conn) if p["id"].startswith(policy_id)]
            if len(matches) != 1 or not delete_row_policy(conn, matches[0]):
                console.print(f"[red]No single row policy matches {policy_id}[/red]")
                raise typer.Exit(1)
            console.print("[green]Row policy removed[/green]")
        else:
            console.print(f"[red]Unknown action {action}: use list, add or remove[/red]")
            raise typer.Exit(1)
    finally:
        conn.close()
