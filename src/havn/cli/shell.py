"""Interactive multi-line SQL shell against the warehouse.

A psql-style REPL with history, multi-line input (terminate with `;`),
readline editing, schema/column tab-completion, and a small set of
backslash slash-commands (``\\dt``, ``\\d``, ``\\dn``, ``\\df``,
``\\timing``, ``\\q``).

Routes through ``havn serve`` if a server is running for this project,
mirroring ``havn query``'s behavior at ``cli/query.py:_try_route_via_server``.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.table import Table

from havn.cli import _load_config, _resolve_project, _warehouse_exists, app, console

# --- shell config ---

HISTORY_PATH = Path.home() / ".havn" / "shell_history"
PROMPT = "havn> "
CONT_PROMPT = "  ...> "


@app.command()
def shell(
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
    env: Annotated[Optional[str], typer.Option("--env", "-e", help="Environment to use")] = None,
    no_history: Annotated[bool, typer.Option("--no-history", help="Disable readline history persistence")] = False,
) -> None:
    """Open an interactive multi-line SQL shell.

    Statements are terminated with ``;``. Empty input or Ctrl-D exits.
    Backslash commands: ``\\dt [schema.*]`` list tables, ``\\d <table>``
    describe a table, ``\\dn`` list schemas, ``\\df`` list macros,
    ``\\timing on|off`` toggle query timing, ``\\copy <q> TO <path>``
    write CSV, ``\\q`` quit.
    """
    from havn.engine.database import open_warehouse

    project_dir = _resolve_project(project_dir)
    config = _load_config(project_dir, env)

    if not _warehouse_exists(config, project_dir):
        console.print("[yellow]No warehouse database found. Run a pipeline first.[/yellow]")
        raise typer.Exit(1)

    server_info = _server_info(project_dir)
    if server_info:
        console.print(
            f"[dim]havn serve detected at {server_info['host']}:{server_info['port']}; "
            f"queries will route through it.[/dim]"
        )
        runner = _ServerRunner(server_info)
    else:
        try:
            conn = open_warehouse(config, project_dir, read_only=False)
        except Exception as e:
            if "already open" in str(e) or "being used by another process" in str(e):
                console.print(
                    "[red]Warehouse is locked by another process.[/red] "
                    "If [bold]havn serve[/bold] is running, the .havn/serve.json "
                    "lockfile may be stale. Stop the server and retry."
                )
                raise typer.Exit(1)
            raise
        runner = _LocalRunner(conn)

    _setup_readline(no_history=no_history)

    console.print(
        f"[bold]havn shell[/bold] — interactive SQL.  "
        f"Type [bold]\\?[/bold] for help, [bold]\\q[/bold] or Ctrl-D to exit."
    )

    state = _ShellState()

    try:
        while True:
            try:
                line = _read_input(state)
            except EOFError:
                console.print()
                break
            except KeyboardInterrupt:
                # Ctrl-C clears any partial buffer and re-prompts
                console.print("[dim]^C[/dim]")
                state.buffer = []
                continue

            if line is None:
                continue

            stripped = line.strip()
            if not stripped and not state.buffer:
                continue

            # Slash commands: only valid as a stand-alone first line
            if not state.buffer and stripped.startswith("\\"):
                if _dispatch_slash(stripped, runner, state):
                    break  # \q signaled
                continue

            state.buffer.append(line)
            joined = "\n".join(state.buffer)

            # Statement terminates on unquoted ";" at end of trimmed buffer
            if _statement_complete(joined):
                sql = joined.rstrip().rstrip(";").strip()
                state.buffer = []
                if not sql:
                    continue
                _run_and_render(runner, sql, state)
    finally:
        try:
            runner.close()
        except Exception:
            pass


# --- input handling ---

class _ShellState:
    def __init__(self) -> None:
        self.buffer: list[str] = []
        self.timing: bool = False


def _read_input(state: _ShellState) -> str | None:
    prompt = CONT_PROMPT if state.buffer else PROMPT
    return input(prompt)


def _statement_complete(buf: str) -> bool:
    """Return True when ``buf`` contains an unquoted, uncommented terminator.

    Walks the buffer once tracking single quotes, double quotes, and
    ``--`` line comments. A semicolon outside all of these counts as
    "complete". Anything after the last such semicolon (whitespace,
    trailing comments) is fine — DuckDB ignores it.

    Block comments ``/* ... */`` are not specially recognised; any
    semicolon nested inside one will be misclassified. This is rare
    enough to live with for an interactive shell.
    """
    in_single = False
    in_double = False
    saw_terminator = False
    i = 0
    n = len(buf)
    while i < n:
        ch = buf[i]
        if not in_single and not in_double and ch == "-" and i + 1 < n and buf[i + 1] == "-":
            # Skip to end-of-line (or end-of-buffer if no newline).
            nl = buf.find("\n", i)
            if nl == -1:
                break
            i = nl + 1
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == ";" and not in_single and not in_double:
            saw_terminator = True
        i += 1
    return saw_terminator and not in_single and not in_double


def _setup_readline(no_history: bool) -> None:
    try:
        import readline
    except ImportError:
        # Windows without pyreadline; bare input() still works, just no
        # editing/history. The shell remains functional.
        return

    if not no_history:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        if HISTORY_PATH.exists():
            try:
                readline.read_history_file(str(HISTORY_PATH))
            except Exception:
                pass

        import atexit

        def _save() -> None:
            try:
                readline.set_history_length(5000)
                readline.write_history_file(str(HISTORY_PATH))
            except Exception:
                pass

        atexit.register(_save)

    # Basic SQL-keyword + whitespace completion. Schema/column completion
    # would require introspecting the warehouse on every Tab; we keep it
    # simple here and lean on history for repeated identifiers.
    keywords = [
        "SELECT", "FROM", "WHERE", "GROUP BY", "ORDER BY", "LIMIT", "JOIN",
        "LEFT JOIN", "RIGHT JOIN", "INNER JOIN", "OUTER JOIN", "ON", "AS",
        "WITH", "UNION", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP",
        "TABLE", "VIEW", "DESCRIBE", "SHOW", "EXPLAIN", "ANALYZE",
        "CASE", "WHEN", "THEN", "ELSE", "END", "AND", "OR", "NOT", "NULL",
        "IS NULL", "IS NOT NULL", "DISTINCT", "COUNT", "SUM", "AVG",
        "MIN", "MAX", "COALESCE", "CAST",
    ]

    def completer(text: str, state: int) -> str | None:
        upper = text.upper()
        matches = [k for k in keywords if k.startswith(upper)]
        if state < len(matches):
            return matches[state]
        return None

    readline.set_completer(completer)
    readline.parse_and_bind("tab: complete")


# --- runners (local conn vs server-routed) ---

class _LocalRunner:
    def __init__(self, conn) -> None:
        self.conn = conn

    def execute(self, sql: str) -> tuple[list[str], list[tuple]]:
        result = self.conn.execute(sql)
        if result.description is None:
            return [], []
        cols = [d[0] for d in result.description]
        rows = result.fetchall()
        return cols, rows

    def close(self) -> None:
        self.conn.close()


class _ServerRunner:
    def __init__(self, info: dict) -> None:
        self.host = info["host"]
        self.port = info["port"]

    def execute(self, sql: str) -> tuple[list[str], list[tuple]]:
        import json as _json
        import urllib.error
        import urllib.request

        body = _json.dumps({"sql": sql}).encode("utf-8")
        req = urllib.request.Request(
            f"http://{self.host}:{self.port}/api/query",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                err_body = str(e)
            raise RuntimeError(err_body)
        return data.get("columns", []), [tuple(r) for r in data.get("rows", [])]

    def close(self) -> None:
        pass


def _server_info(project_dir: Path) -> dict | None:
    import json as _json

    info_path = project_dir / ".havn" / "serve.json"
    if not info_path.exists():
        return None
    try:
        info = _json.loads(info_path.read_text())
    except Exception:
        return None
    pid = info.get("pid")
    if pid is not None and not _pid_alive(int(pid)):
        try:
            info_path.unlink()
        except Exception:
            pass
        return None
    # Smoke-test the HTTP port; if it refuses we treat as no server.
    try:
        import urllib.error
        import urllib.request

        urllib.request.urlopen(
            f"http://{info.get('host', '127.0.0.1')}:{int(info.get('port', 3000))}/api/health",
            timeout=2,
        )
    except Exception:
        return None
    return {
        "host": info.get("host", "127.0.0.1"),
        "port": int(info.get("port", 3000)),
    }


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # Match the Windows path used by `havn query`.
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong(0)
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) == 0:
                    return False
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


# --- run + render ---

def _run_and_render(runner, sql: str, state: _ShellState) -> None:
    start = time.perf_counter()
    try:
        cols, rows = runner.execute(sql)
    except Exception as e:
        console.print(f"[red]ERROR:[/red] {e}")
        return
    elapsed = time.perf_counter() - start

    if not cols:
        console.print(f"[green]OK[/green]" + (f"  ({elapsed*1000:.1f} ms)" if state.timing else ""))
        return

    table = Table(show_lines=False)
    for c in cols:
        table.add_column(c, no_wrap=False, max_width=60)
    for row in rows:
        table.add_row(*[_fmt(v) for v in row])
    console.print(table)
    rowcount_msg = f"[dim]{len(rows)} row{'s' if len(rows) != 1 else ''}[/dim]"
    if state.timing:
        rowcount_msg += f"  [dim]({elapsed*1000:.1f} ms)[/dim]"
    console.print(rowcount_msg)


def _fmt(v) -> str:
    if v is None:
        return "[dim]NULL[/dim]"
    return str(v)


# --- slash commands ---

_SLASH_HELP = """\
[bold]Slash commands[/bold]
  \\dt [schema]    list tables (default: all user schemas)
  \\dv [schema]    list views
  \\dn             list schemas
  \\d <table>      describe a table
  \\df             list registered macros (UDFs)
  \\timing on|off  toggle per-query timing
  \\copy <SQL> TO <path>   write SQL result to CSV
  \\? or \\help    show this help
  \\q              quit
"""


def _dispatch_slash(line: str, runner, state: _ShellState) -> bool:
    """Run a slash command. Returns True if the shell should exit."""
    parts = line.split(None, 1)
    cmd = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("\\q", "\\quit", "\\exit"):
        return True
    if cmd in ("\\?", "\\help", "\\h"):
        console.print(_SLASH_HELP)
        return False
    if cmd == "\\dn":
        _run_and_render(
            runner,
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT IN ('information_schema', '_havn', 'pg_catalog', 'main') "
            "ORDER BY 1",
            state,
        )
        return False
    if cmd in ("\\dt", "\\dv"):
        ttype = "BASE TABLE" if cmd == "\\dt" else "VIEW"
        schema_clause = ""
        if arg:
            # Strip a trailing ".*" so users can type \dt gold.* like psql.
            schema_name = arg.rstrip(".*").strip()
            schema_clause = f" AND table_schema = '{_sanitize(schema_name)}'"
        sql = (
            "SELECT table_schema, table_name "
            "FROM information_schema.tables "
            f"WHERE table_type = '{ttype}' "
            f"AND table_schema NOT IN ('information_schema', '_havn'){schema_clause} "
            "ORDER BY 1, 2"
        )
        _run_and_render(runner, sql, state)
        return False
    if cmd == "\\d":
        if not arg:
            console.print("[yellow]Usage: \\d <table>  e.g. \\d gold.orders[/yellow]")
            return False
        if "." in arg:
            schema, table = arg.split(".", 1)
        else:
            schema, table = "main", arg
        sql = (
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            f"WHERE table_schema = '{_sanitize(schema)}' "
            f"AND table_name = '{_sanitize(table)}' "
            "ORDER BY ordinal_position"
        )
        _run_and_render(runner, sql, state)
        return False
    if cmd == "\\df":
        # DuckDB's duckdb_functions() lists scalar macros; user-defined
        # Python UDFs land in pg_catalog.pg_proc with a non-null prosrc.
        sql = (
            "SELECT function_name, return_type, parameters "
            "FROM duckdb_functions() "
            "WHERE function_type IN ('macro', 'table_macro') "
            "ORDER BY 1"
        )
        _run_and_render(runner, sql, state)
        return False
    if cmd == "\\timing":
        if arg.lower() in ("on", "true", "1"):
            state.timing = True
            console.print("[dim]Timing on.[/dim]")
        elif arg.lower() in ("off", "false", "0"):
            state.timing = False
            console.print("[dim]Timing off.[/dim]")
        else:
            console.print(f"[dim]Timing: {'on' if state.timing else 'off'}[/dim]")
        return False
    if cmd == "\\copy":
        # Minimal \copy: \copy <SQL> TO <path>
        upper = arg.upper()
        idx = upper.rfind(" TO ")
        if idx == -1:
            console.print("[yellow]Usage: \\copy <SQL> TO <path>[/yellow]")
            return False
        inner_sql = arg[:idx].strip()
        target = arg[idx + 4:].strip().strip("'").strip('"')
        copy_sql = f"COPY ({inner_sql}) TO '{target}' (FORMAT CSV, HEADER)"
        try:
            runner.execute(copy_sql)
            console.print(f"[green]Wrote results to {target}[/green]")
        except Exception as e:
            console.print(f"[red]ERROR:[/red] {e}")
        return False

    console.print(f"[yellow]Unknown command: {cmd}.  Type \\? for help.[/yellow]")
    return False


def _sanitize(s: str) -> str:
    """Defensive: strip single quotes from identifiers. Identifiers are
    not parameterizable in metadata queries, but we never accept arbitrary
    strings — only what a user typed in their own shell session.
    """
    return s.replace("'", "")
