"""Query and inspection commands: query, tables, history."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.table import Table

from havn.cli import _load_config, _resolve_project, _warehouse_exists, app, console


@app.command()
def query(
    sql: Annotated[str, typer.Argument(help="SQL query to execute")],
    csv: Annotated[bool, typer.Option("--csv", help="Output as CSV")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max rows to return")] = 0,
    env: Annotated[Optional[str], typer.Option("--env", "-e", help="Environment to use")] = None,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """Run an ad-hoc SQL query against the warehouse."""
    import json as json_mod

    from havn.engine.database import open_warehouse

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir, env)

    sql = sql.strip()
    if not sql:
        console.print("[red]Empty query. Provide a SQL statement to execute.[/red]")
        raise typer.Exit(1)

    if not _warehouse_exists(config, project_dir):
        console.print("[yellow]No warehouse database found. Run a pipeline first.[/yellow]")
        raise typer.Exit(1)

    # If a havn server is running against this project, the warehouse file
    # is locked by that process (DuckDB acquires a process-level lock). Route
    # the query through the server's HTTP API so users don't have to stop
    # the server to drop into the CLI.
    if _try_route_via_server(project_dir, sql, csv, json_output, limit):
        return

    try:
        conn = open_warehouse(config, project_dir, read_only=True)
    except Exception as e:
        if "already open" in str(e) or "being used by another process" in str(e):
            console.print(
                "[red]Warehouse is locked by another process.[/red] "
                "If [bold]havn serve[/bold] is running, this CLI should auto-route "
                "through it; the lockfile at .havn/serve.json may be stale. "
                "Stop the server and retry, or remove .havn/serve.json."
            )
            raise typer.Exit(1)
        raise
    try:
        result = conn.execute(sql)
        if result.description is None:
            console.print("[yellow]Query executed successfully (no results returned).[/yellow]")
            return
        columns = [desc[0] for desc in result.description]
        rows = result.fetchall()
        if limit > 0:
            rows = rows[:limit]

        if csv:
            import io as _io
            import csv as _csv
            buf = _io.StringIO()
            writer = _csv.writer(buf)
            writer.writerow(columns)
            for row in rows:
                writer.writerow(row)
            console.print(buf.getvalue().rstrip())
        elif json_output:
            data = [dict(zip(columns, [_json_safe(v) for v in row])) for row in rows]
            console.print(json_mod.dumps(data, indent=2, default=str))
        else:
            table = Table(show_lines=len(columns) > 8)
            for col in columns:
                table.add_column(col, no_wrap=False, max_width=60)
            for row in rows:
                table.add_row(*[str(v) for v in row])
            console.print(table)
            console.print(f"[dim]{len(rows)} rows[/dim]")
    except Exception as e:
        err_msg = str(e)
        if "read-only mode" in err_msg:
            console.print("[red]Query error:[/red] havn query is read-only. Use [bold]havn run[/bold] for write operations.")
        else:
            console.print(f"[red]Query error:[/red] {e}")
        raise typer.Exit(1)
    finally:
        conn.close()


def _json_safe(v):
    """Convert DuckDB values to JSON-safe types."""
    import datetime
    if isinstance(v, (datetime.date, datetime.datetime)):
        return str(v)
    return v


def _pid_alive(pid: int) -> bool:
    """Return True if the given PID names a live process, False otherwise.

    Used to detect stale ``.havn/serve.json`` lockfiles when a `havn serve`
    process was SIGKILL'd or crashed without running its finally-block
    cleanup.
    """
    if pid <= 0:
        return False
    import os, sys
    if sys.platform == "win32":
        # On Windows, os.kill(pid, 0) raises OSError with errno EINVAL for
        # signal 0; use the OpenProcess + GetExitCodeProcess Win32 dance via
        # ctypes. STILL_ACTIVE is 259.
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong(0)
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) == 0:
                    return False
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            # If the introspection itself errors, assume alive (safer to
            # attempt HTTP than to delete a possibly-valid lockfile).
            return True
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but is owned by another user.
            return True
        except OSError:
            return False


def _try_route_via_server(project_dir: Path, sql: str, csv: bool, json_output: bool, limit: int) -> bool:
    """If a havn server is running for this project, run the query against it.

    Returns True if the request was handled (success or printed error), False
    if no server is running and the caller should fall through to the direct
    DuckDB path.
    """
    import json as _json
    info_path = project_dir / ".havn" / "serve.json"
    if not info_path.exists():
        return False
    try:
        info = _json.loads(info_path.read_text())
        host = info.get("host", "127.0.0.1")
        port = int(info.get("port", 3000))
        pid = info.get("pid")
    except Exception:
        return False

    # Stale-lockfile detection: if the recorded PID is no longer alive, the
    # server was SIGKILL'd or crashed. Remove the lockfile (so subsequent
    # invocations don't pay the HTTP-attempt cost) and fall through to the
    # direct DuckDB open path.
    if pid is not None and not _pid_alive(int(pid)):
        try:
            info_path.unlink()
        except Exception:
            pass
        return False

    try:
        import urllib.request
        import urllib.error
        body = _json.dumps({"sql": sql}).encode("utf-8")
        req = urllib.request.Request(
            f"http://{host}:{port}/api/query",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ConnectionError, OSError):
        # Connection refused even though the PID is alive. Could be a port
        # mismatch (rare) or the server hasn't finished booting. Clean up
        # the lockfile so we don't keep retrying the HTTP path on every
        # subsequent CLI invocation, and fall through to the direct path.
        try:
            info_path.unlink()
        except Exception:
            pass
        return False
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            err_body = str(e)
        console.print(f"[red]Query error:[/red] {err_body}")
        raise typer.Exit(1)

    columns = data.get("columns", [])
    rows = data.get("rows", [])
    if limit > 0:
        rows = rows[:limit]

    if csv:
        import io as _io
        import csv as _csv
        buf = _io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(row)
        console.print(buf.getvalue().rstrip())
    elif json_output:
        result = [dict(zip(columns, row)) for row in rows]
        console.print(_json.dumps(result, indent=2, default=str))
    else:
        table = Table(show_lines=len(columns) > 8)
        for col in columns:
            table.add_column(col, no_wrap=False, max_width=60)
        for row in rows:
            table.add_row(*[str(v) for v in row])
        console.print(table)
        console.print(f"[dim]{len(rows)} rows (via server at {host}:{port})[/dim]")
    return True


@app.command()
def tables(
    schema: Annotated[Optional[str], typer.Argument(help="Schema to list (all if omitted)")] = None,
    env: Annotated[Optional[str], typer.Option("--env", "-e", help="Environment to use")] = None,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """List tables and views in the warehouse."""
    from havn.engine.database import open_warehouse

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir, env)

    if not _warehouse_exists(config, project_dir):
        console.print("[yellow]No warehouse database found. Run a pipeline first.[/yellow]")
        return

    conn = open_warehouse(config, project_dir, read_only=True)
    try:
        # ``table_catalog = current_database()`` filters out the
        # DuckLake ``__ducklake_metadata_warehouse`` internal catalog,
        # which would otherwise leak its bookkeeping tables here.
        if schema:
            sql = """
                SELECT table_schema, table_name, table_type
                FROM information_schema.tables
                WHERE table_catalog = current_database()
                  AND table_schema NOT IN ('information_schema', '_havn')
                  AND table_schema = ?
                ORDER BY table_schema, table_name
            """
            result = conn.execute(sql, [schema]).fetchall()
        else:
            sql = """
                SELECT table_schema, table_name, table_type
                FROM information_schema.tables
                WHERE table_catalog = current_database()
                  AND table_schema NOT IN ('information_schema', '_havn')
                ORDER BY table_schema, table_name
            """
            result = conn.execute(sql).fetchall()
        if not result:
            console.print("[yellow]No tables found.[/yellow]")
            return

        table = Table(title="Warehouse Objects")
        table.add_column("Schema", style="cyan")
        table.add_column("Name", style="bold")
        table.add_column("Type")
        for row in result:
            type_style = "dim" if row[2] == "VIEW" else ""
            table.add_row(row[0], row[1], row[2], style=type_style)
        console.print(table)
    finally:
        conn.close()


@app.command()
def history(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Number of entries")] = 20,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """Show recent run history."""
    import duckdb

    from havn.config import load_project
    from havn.engine.database import open_warehouse

    project_dir = _resolve_project(project_dir)
    config = load_project(project_dir)

    if not _warehouse_exists(config, project_dir):
        console.print("[yellow]No warehouse database found.[/yellow]")
        return

    conn = open_warehouse(config, project_dir, read_only=True)
    try:
        try:
            result = conn.execute(
                """
                SELECT run_type, target, status, started_at, duration_ms, rows_affected, error
                FROM _havn.run_log
                ORDER BY started_at DESC
                LIMIT ?
                """,
                [limit],
            ).fetchall()
        except duckdb.CatalogException:
            console.print("[yellow]No run history yet.[/yellow]")
            return

        if not result:
            console.print("[yellow]No run history yet.[/yellow]")
            return

        table = Table(title="Run History")
        table.add_column("Type", style="cyan")
        table.add_column("Target", style="bold")
        table.add_column("Status")
        table.add_column("Time")
        table.add_column("Duration", justify="right")
        table.add_column("Rows", justify="right")
        table.add_column("Error")

        for row in result:
            status = row[2]
            if status == "success":
                status_style = "[green]"
            elif status == "skipped":
                status_style = "[dim]"
            else:
                status_style = "[red]"
            dur = f"{row[4]}ms" if row[4] else ""
            rows = str(row[5]) if row[5] else ""
            error = (row[6][:60] + "...") if row[6] and len(row[6]) > 60 else (row[6] or "")
            table.add_row(
                row[0],
                row[1],
                f"{status_style}{row[2]}[/]",
                str(row[3])[:19] if row[3] else "",
                dur,
                rows,
                error,
            )

        console.print(table)
    finally:
        conn.close()
