"""Dependency-free MCP (Model Context Protocol) stdio server.

Implements the MCP JSON-RPC protocol directly over newline-delimited JSON
on stdin/stdout — no SDK dependency. The server is intentionally small:
``handle_message`` is a pure dict-in/dict-out dispatcher (easy to test),
and ``serve`` wires it to stdio.

All warehouse access is read-only except the ``run_transform`` tool, which
can be disabled entirely with ``read_only=True`` (``havn mcp --read-only``).
Read queries route through a running ``havn serve`` when one holds the
warehouse lock, falling back to a direct read-only connection otherwise —
the same strategy the CLI uses.

stdout carries protocol frames only; anything else (logs, warnings) must go
to stderr or it corrupts the stream.
"""

from __future__ import annotations

from havn.textio import read_project_text

import contextlib
import io
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable

from havn import __version__
from havn.engine.sql_safety import ReadOnlyQueryError, validate_read_only_query

logger = logging.getLogger("havn.mcp")

PROTOCOL_VERSION = "2025-06-18"
_SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")

_DEFAULT_QUERY_ROWS = 100
_MAX_QUERY_ROWS = 10_000

# JSON-RPC error codes
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


class ToolError(Exception):
    """A tool failed in a way the agent should see (returned as isError)."""


class MCPServer:
    """MCP stdio server bound to a single havn project."""

    def __init__(self, project_dir: Path, env: str | None = None, read_only: bool = False) -> None:
        self.project_dir = Path(project_dir)
        self.env = env
        self.read_only = read_only
        self._tools = self._build_tools()

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def serve(self, stdin=None, stdout=None) -> None:
        """Read newline-delimited JSON-RPC messages until EOF."""
        stdin = stdin or sys.stdin
        if stdout is None:
            stdout = _claim_protocol_stdout()
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self._write(stdout, _error_response(None, _PARSE_ERROR, "Parse error"))
                continue
            # Engine code prints progress (the transform engine's module-level
            # Rich Console, print() in user macros). Rich resolves sys.stdout
            # on every write when built without file=, so swapping sys.stdout
            # for the call keeps those lines off the protocol stream.
            with contextlib.redirect_stdout(sys.stderr):
                response = self.handle_message(msg)
            if response is not None:
                self._write(stdout, response)

    @staticmethod
    def _write(stdout, message: dict) -> None:
        stdout.write(json.dumps(message, default=str) + "\n")
        stdout.flush()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def handle_message(self, msg: Any) -> dict | None:
        """Handle one JSON-RPC message. Returns the response dict, or None
        for notifications (which must not be answered)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error_response(None, _INVALID_REQUEST, "Invalid request")

        method = msg.get("method")
        msg_id = msg.get("id")
        is_notification = "id" not in msg

        if not isinstance(method, str):
            # A response message from the client (has id but no method) — ignore.
            return None if is_notification or "result" in msg or "error" in msg else _error_response(
                msg_id, _INVALID_REQUEST, "Missing method"
            )

        if method.startswith("notifications/"):
            return None

        try:
            params = msg.get("params") or {}
            if method == "initialize":
                result = self._handle_initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [t["def"] for t in self._tools.values()]}
            elif method == "tools/call":
                result = self._handle_tools_call(params)
            else:
                if is_notification:
                    return None
                return _error_response(msg_id, _METHOD_NOT_FOUND, f"Method not found: {method}")
        except _InvalidParams as e:
            return _error_response(msg_id, _INVALID_PARAMS, str(e))
        except Exception as e:  # never crash the loop on a single message
            logger.exception("MCP handler error for %s", method)
            return _error_response(msg_id, _INTERNAL_ERROR, str(e))

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _handle_initialize(self, params: dict) -> dict:
        client_version = str(params.get("protocolVersion", ""))
        version = client_version if client_version in _SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "havn", "version": __version__},
        }

    def _handle_tools_call(self, params: dict) -> dict:
        name = params.get("name")
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise _InvalidParams(f"Unknown tool: {name!r}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise _InvalidParams("arguments must be an object")
        try:
            payload = tool["handler"](arguments)
        except (ToolError, ReadOnlyQueryError, ValueError) as e:
            return {
                "content": [{"type": "text", "text": f"Error: {e}"}],
                "isError": True,
            }
        text = payload if isinstance(payload, str) else json.dumps(payload, default=str, indent=2)
        return {"content": [{"type": "text", "text": text}]}

    # ------------------------------------------------------------------
    # Warehouse access
    # ------------------------------------------------------------------

    def _config(self):
        from havn.config import load_project

        return load_project(self.project_dir, env=self.env)

    def _server_info(self) -> tuple[str, int] | None:
        """Return (host, port) of a live ``havn serve`` for this project."""
        info_path = self.project_dir / ".havn" / "serve.json"
        if not info_path.exists():
            return None
        try:
            info = json.loads(read_project_text(info_path))
            host = info.get("host", "127.0.0.1")
            port = int(info.get("port", 3000))
            pid = info.get("pid")
        except Exception:
            return None
        if pid is not None:
            from havn.cli.query import _pid_alive

            if not _pid_alive(int(pid)):
                return None
        return host, port

    def _query_via_server(self, sql: str, max_rows: int) -> dict | None:
        """Run sql through a running ``havn serve``, or return None if there
        is no live server (caller falls back to a direct connection)."""
        server = self._server_info()
        if server is None:
            return None
        host, port = server
        import urllib.error
        import urllib.request

        body = json.dumps({"sql": sql, "limit": min(max_rows, 50_000)}).encode("utf-8")
        req = urllib.request.Request(
            f"http://{host}:{port}/api/query",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                detail = str(e)
            raise ToolError(f"query failed via havn serve at {host}:{port}: {detail}")
        except (urllib.error.URLError, ConnectionError, OSError):
            return None
        return {
            "columns": data.get("columns", []),
            "rows": data.get("rows", []),
            "row_count": len(data.get("rows", [])),
            "truncated": bool(data.get("truncated", False)),
        }

    def _execute_readonly(self, sql: str, max_rows: int = _DEFAULT_QUERY_ROWS) -> dict:
        """Validate and run a read-only query, via server or direct connection."""
        validate_read_only_query(sql)
        max_rows = max(1, min(int(max_rows), _MAX_QUERY_ROWS))

        via_server = self._query_via_server(sql, max_rows)
        if via_server is not None:
            return via_server

        from havn.engine.backends import create_backend
        from havn.engine.database import open_warehouse

        config = self._config()
        backend = create_backend(config.database, project_dir=self.project_dir)
        if not backend.exists():
            raise ToolError("No warehouse database found. Run a pipeline first.")

        conn = open_warehouse(config, self.project_dir, read_only=True)
        try:
            cur = conn.execute(sql)
            if cur.description is None:
                return {"columns": [], "rows": [], "row_count": 0, "truncated": False}
            columns = [d[0] for d in cur.description]
            rows = cur.fetchmany(max_rows + 1)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(str(e))
        finally:
            conn.close()
        truncated = len(rows) > max_rows
        rows = [list(r) for r in rows[:max_rows]]
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def _build_tools(self) -> dict[str, dict]:
        tools: list[tuple[str, str, dict, Callable[[dict], Any]]] = [
            (
                "query",
                "Run a read-only SQL query against the havn warehouse (DuckDB "
                "dialect). Mutations and file-access functions are rejected.",
                {
                    "type": "object",
                    "properties": {
                        "sql": {"type": "string", "description": "SELECT statement to run"},
                        "limit": {
                            "type": "integer",
                            "description": f"Max rows to return (default {_DEFAULT_QUERY_ROWS}, max {_MAX_QUERY_ROWS})",
                        },
                    },
                    "required": ["sql"],
                },
                self._tool_query,
            ),
            (
                "list_tables",
                "List tables and views in the warehouse, optionally filtered by schema.",
                {
                    "type": "object",
                    "properties": {
                        "schema": {"type": "string", "description": "Schema name to filter by"},
                    },
                },
                self._tool_list_tables,
            ),
            (
                "describe_table",
                "Show columns, types, and row count for a table or view.",
                {
                    "type": "object",
                    "properties": {
                        "table": {"type": "string", "description": "Qualified name, e.g. gold.orders"},
                    },
                    "required": ["table"],
                },
                self._tool_describe_table,
            ),
            (
                "list_models",
                "List SQL transform models with materialization, schema, tags and "
                "dependencies. Pass a graph selector to narrow the list.",
                {
                    "type": "object",
                    "properties": {
                        "select": {
                            "type": "string",
                            "description": (
                                "Optional graph selector, e.g. 'tag:daily', "
                                "'+gold.orders', 'gold.fct_*', 'path:transform/gold/'"
                            ),
                        },
                    },
                },
                self._tool_list_models,
            ),
            (
                "get_model",
                "Get a transform model's SQL source, config, and dependencies.",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Model name, e.g. silver.customers"},
                    },
                    "required": ["name"],
                },
                self._tool_get_model,
            ),
            (
                "model_lineage",
                "Show a model's upstream dependencies and downstream dependents "
                "(direct and transitive).",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Model name, e.g. silver.customers"},
                    },
                    "required": ["name"],
                },
                self._tool_model_lineage,
            ),
            (
                "bind_model",
                "Resolve a model's SQL through the DuckDB binder without "
                "building it: returns the inferred output columns and any bind "
                "errors with line numbers. Catches wrong arity, unknown "
                "functions, operator overload failures and missing columns, "
                "including on upstream models that have never been built. Does "
                "not catch value conversions such as CAST of a non-numeric "
                "string, which only fail at run time.",
                {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Model name, e.g. silver.customers",
                        },
                        "sql": {
                            "type": "string",
                            "description": (
                                "SQL to bind instead of a saved model, including "
                                "any @config lines. Pair with name to bind it in "
                                "that model's place."
                            ),
                        },
                    },
                },
                self._tool_bind_model,
            ),
            (
                "run_history",
                "Show recent pipeline run history (ingest, transform, export).",
                {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Number of entries (default 20)"},
                    },
                },
                self._tool_run_history,
            ),
            (
                "list_metrics",
                "List semantic-layer metrics declared in metrics/*.yml.",
                {"type": "object", "properties": {}},
                self._tool_list_metrics,
            ),
            (
                "query_metric",
                "Query a semantic-layer metric: group by declared dimensions, "
                "bucket by time grain, and filter by time range.",
                {
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string", "description": "Metric name"},
                        "dimensions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Dimensions to group by (must be declared on the metric)",
                        },
                        "grain": {
                            "type": "string",
                            "description": "Time grain: hour, day, week, month, quarter, year",
                        },
                        "start": {"type": "string", "description": "Inclusive lower time bound"},
                        "end": {"type": "string", "description": "Exclusive upper time bound"},
                        "limit": {"type": "integer", "description": "Max rows"},
                    },
                    "required": ["metric"],
                },
                self._tool_query_metric,
            ),
            (
                "run_unit_tests",
                "Run model unit tests from tests/unit/*.yml. Each test runs "
                "the model against declared fixture rows on a throwaway "
                "in-memory database, so it reads nothing from the warehouse.",
                {
                    "type": "object",
                    "properties": {
                        "model": {
                            "type": "string",
                            "description": "Only run tests for this model, e.g. silver.customers",
                        },
                    },
                },
                self._tool_run_unit_tests,
            ),
            (
                "ask",
                "Answer a business question from the semantic layer. havn's "
                "configured model maps the question onto metrics/*.yml (metric, "
                "dimensions, grain, filters, time range), compiles it and runs it "
                "read-only. Returns the result, the structured spec, the compiled "
                "SQL, lineage and freshness, or says plainly that no metric "
                "answers it and suggests one. Pass history to refine a previous "
                "answer ('now by month').",
                {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "The question"},
                        "history": {
                            "type": "array",
                            "description": "Earlier turns: [{question, spec}] from previous ask results",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "question": {"type": "string"},
                                    "spec": {"type": "object"},
                                },
                                "required": ["question"],
                            },
                        },
                        "exploratory": {
                            "type": "boolean",
                            "description": "When no metric fits, try unverified exploratory SQL "
                            "(only if the project allows it)",
                        },
                    },
                    "required": ["question"],
                },
                self._tool_ask,
            ),
            (
                "submit_change_set",
                "Propose edits to model files as a change set and get the "
                "verification report back. Nothing is written to the project: "
                "havn checks the change (parse and DAG, bind pass, affected unit "
                "tests, contracts, assertions) and builds every affected model "
                "into a scratch database to diff it against the real tables. "
                "Iterate by passing change_set_id with the full new file set "
                "until the report is ok; the user applies it from the havn UI. "
                "Paths are relative to the project: transform/**/*.sql, macros/, "
                "tests/unit/, contracts/, metrics/.",
                {
                    "type": "object",
                    "properties": {
                        "files": {
                            "type": "array",
                            "description": "Full proposed contents: [{path, content}]; "
                            "content null or delete: true removes the file",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "content": {"type": ["string", "null"]},
                                    "delete": {"type": "boolean"},
                                },
                                "required": ["path"],
                            },
                        },
                        "title": {"type": "string", "description": "One line saying what the change does"},
                        "change_set_id": {
                            "type": "string",
                            "description": "Revise this open change set instead of creating a new one",
                        },
                    },
                    "required": ["files"],
                },
                self._tool_submit_change_set,
            ),
            (
                "get_change_set",
                "Show a change set's status and verification report.",
                {
                    "type": "object",
                    "properties": {"change_set_id": {"type": "string"}},
                    "required": ["change_set_id"],
                },
                self._tool_get_change_set,
            ),
        ]
        if not self.read_only:
            tools.append(
                (
                    "run_transform",
                    "Build SQL transform models (the DAG). Only changed models are "
                    "rebuilt unless force is set. Fails if `havn serve` holds the "
                    "warehouse lock.",
                    {
                        "type": "object",
                        "properties": {
                            "select": {
                                "type": "string",
                                "description": (
                                    "Graph selector for what to build. 'gold.orders' is "
                                    "that model alone; '+gold.orders' adds its upstreams, "
                                    "'gold.orders+' its downstream, '@x' both directions, "
                                    "'gold.fct_*' a wildcard, 'tag:daily' a tag, "
                                    "'path:transform/gold/' a path, "
                                    "'config.materialized:incremental' a config key, "
                                    "'state:modified+' what changed plus downstream, and "
                                    "a comma intersects. Omit to build everything."
                                ),
                            },
                            "exclude": {
                                "type": "string",
                                "description": "Selector whose matches are removed from the selection",
                            },
                            "force": {"type": "boolean", "description": "Rebuild even if unchanged"},
                        },
                    },
                    self._tool_run_transform,
                )
            )
        return {
            name: {
                "def": {"name": name, "description": desc, "inputSchema": schema},
                "handler": handler,
            }
            for name, desc, schema, handler in tools
        }

    # --- tool handlers -------------------------------------------------

    def _tool_query(self, args: dict) -> dict:
        sql = args.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise _InvalidParams("sql is required")
        limit = args.get("limit") or _DEFAULT_QUERY_ROWS
        return self._execute_readonly(sql, max_rows=limit)

    def _tool_list_tables(self, args: dict) -> dict:
        from havn.engine.utils import validate_identifier

        schema = args.get("schema")
        schema_filter = ""
        if schema:
            validate_identifier(str(schema), "schema")
            schema_filter = f" AND table_schema = '{schema}'"
        sql = (
            "SELECT table_schema, table_name, table_type "
            "FROM information_schema.tables "
            "WHERE table_catalog = current_database() "
            "AND table_schema NOT IN ('information_schema', '_havn')"
            f"{schema_filter} "
            "ORDER BY table_schema, table_name"
        )
        result = self._execute_readonly(sql, max_rows=_MAX_QUERY_ROWS)
        return {
            "tables": [
                {"schema": r[0], "name": r[1], "type": r[2]} for r in result["rows"]
            ]
        }

    def _tool_describe_table(self, args: dict) -> dict:
        from havn.engine.utils import validate_identifier

        table = str(args.get("table") or "")
        parts = table.split(".")
        if len(parts) != 2:
            raise _InvalidParams("table must be schema-qualified, e.g. gold.orders")
        schema, name = parts
        validate_identifier(schema, "schema")
        validate_identifier(name, "table")

        cols = self._execute_readonly(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_catalog = current_database() "
            f"AND table_schema = '{schema}' AND table_name = '{name}' "
            "ORDER BY ordinal_position",
            max_rows=_MAX_QUERY_ROWS,
        )
        if not cols["rows"]:
            raise ToolError(f"Table not found: {table}")
        count = self._execute_readonly(f"SELECT COUNT(*) FROM {schema}.{name}", max_rows=1)
        return {
            "table": table,
            "row_count": count["rows"][0][0] if count["rows"] else None,
            "columns": [
                {"name": r[0], "type": r[1], "nullable": r[2] != "NO"}
                for r in cols["rows"]
            ],
        }

    def _models(self):
        from havn.engine.transform import discover_all_models

        return discover_all_models(self.project_dir)

    def _tool_list_models(self, args: dict) -> dict:
        models = self._models()
        select = args.get("select")
        if select:
            from havn.engine.selectors import select_models

            chosen = set(
                select_models(
                    [str(select)], models, project_dir=self.project_dir
                ).selected
            )
            models = [m for m in models if m.full_name in chosen]
        return {
            "models": [
                {
                    "name": m.full_name,
                    "materialized": m.materialized,
                    "language": getattr(m, "language", "sql"),
                    "depends_on": m.depends_on,
                    "description": m.description,
                    "tags": list(getattr(m, "tags", []) or []),
                    # None for the project's own models; the package name for
                    # a model that came from havn_packages/, which an agent
                    # needs before it suggests editing the file.
                    "package": getattr(m, "package", None),
                    "path": m.path.relative_to(self.project_dir).as_posix(),
                }
                for m in models
            ]
        }

    def _find_model(self, name: str):
        models = self._models()
        for m in models:
            if m.full_name == name.lower() or m.name == name.lower():
                return m
        available = ", ".join(m.full_name for m in models) or "none"
        raise ToolError(f"Unknown model {name!r} (available: {available})")

    def _tool_get_model(self, args: dict) -> dict:
        m = self._find_model(str(args.get("name") or ""))
        return {
            "name": m.full_name,
            "path": m.path.relative_to(self.project_dir).as_posix(),
            "materialized": m.materialized,
            "language": getattr(m, "language", "sql"),
            "depends_on": m.depends_on,
            "description": m.description,
            "assertions": m.assertions,
            "sql": m.sql,
        }

    def _tool_model_lineage(self, args: dict) -> dict:
        m = self._find_model(str(args.get("name") or ""))
        models = self._models()
        by_name = {x.full_name: x for x in models}

        def transitive(start: str, edges: dict[str, list[str]]) -> list[str]:
            seen: list[str] = []
            stack = list(edges.get(start, []))
            while stack:
                node = stack.pop()
                if node in seen or node == start:
                    continue
                seen.append(node)
                stack.extend(edges.get(node, []))
            return sorted(seen)

        upstream_edges = {x.full_name: list(x.depends_on) for x in models}
        downstream_edges: dict[str, list[str]] = {x.full_name: [] for x in models}
        for x in models:
            for dep in x.depends_on:
                if dep in downstream_edges:
                    downstream_edges[dep].append(x.full_name)

        return {
            "model": m.full_name,
            "upstream_direct": sorted(m.depends_on),
            "upstream_all": transitive(m.full_name, upstream_edges),
            "downstream_direct": sorted(downstream_edges.get(m.full_name, [])),
            "downstream_all": transitive(m.full_name, downstream_edges),
            # depends_on can reference non-model tables (e.g. landing.*);
            # flag which upstreams are managed models vs raw tables.
            "upstream_external": sorted(
                d for d in m.depends_on if d not in by_name
            ),
        }

    def _tool_bind_model(self, args: dict) -> dict:
        """Bind one model (saved or supplied as SQL) and report its schema."""
        from havn.engine.database import open_warehouse
        from havn.engine.transform.bind import (
            ancestor_closure,
            as_validation_message,
            bind_models,
            model_from_buffer,
            read_only_rejection,
        )

        name = str(args.get("name") or "").strip()
        sql = args.get("sql")
        if not name and not sql:
            raise _InvalidParams("bind_model needs a name or sql")

        models = list(self._models())
        if sql:
            # Agent-supplied SQL, not a file the user wrote: validate it
            # before any binder sees it, and never bind a buffer that fails.
            rejection = read_only_rejection(str(sql))
            if rejection is not None:
                return {
                    "model": name or None,
                    "ok": False,
                    "available": True,
                    "errors": [
                        {
                            "message": rejection,
                            "line": None,
                            "col": None,
                            "kind": "bind",
                        }
                    ],
                    "columns": [],
                }
            path = None
            if name:
                existing = self._find_model(name)
                path = existing.path
                models = [m for m in models if m.full_name != existing.full_name]
            target = model_from_buffer(
                str(sql), path=path, transform_dir=self.project_dir / "transform"
            )
            models.append(target)
        else:
            target = self._find_model(name)

        chain = ancestor_closure(models, [target.full_name])

        # The bind pass builds its shadow in its own in-memory database and
        # uses this connection only to read base table column types, so a
        # read-only handle is enough. A project whose warehouse does not exist
        # yet cannot be opened read-only at all; fall back so binding still
        # works before the first build.
        config = self._config()
        try:
            conn = open_warehouse(config, self.project_dir, read_only=True)
        except Exception:
            try:
                conn = open_warehouse(config, self.project_dir)
            except Exception as e:
                raise ToolError(
                    "Could not open the warehouse for binding "
                    f"(a running `havn serve` holds the lock): {e}"
                )
        try:
            result = bind_models(conn, chain, project_dir=self.project_dir)
        except Exception as e:
            raise ToolError(f"bind failed: {e}")
        finally:
            conn.close()

        if not result.available:
            return {
                "model": target.full_name,
                "ok": False,
                "available": False,
                "errors": [w.message for w in result.warnings],
                "columns": [],
            }

        own = result.errors.get(target.full_name, [])
        return {
            "model": target.full_name,
            "ok": not own,
            "available": True,
            "errors": [
                {
                    "message": as_validation_message(e),
                    "line": e.line,
                    "col": e.col,
                    "kind": e.kind,
                }
                for e in own
            ],
            "columns": [
                {"name": n, "type": t}
                for n, t in result.schemas.get(target.full_name, [])
            ],
            "upstream_errors": {
                other: [as_validation_message(e) for e in errs]
                for other, errs in result.errors.items()
                if other != target.full_name
            },
            "duration_ms": result.duration_ms,
        }

    def _tool_run_history(self, args: dict) -> dict:
        limit = max(1, min(int(args.get("limit") or 20), 500))
        result = self._execute_readonly(
            "SELECT run_type, target, status, started_at, duration_ms, rows_affected, error "
            "FROM _havn.run_log "
            f"ORDER BY started_at DESC LIMIT {limit}",
            max_rows=limit,
        )
        keys = ["run_type", "target", "status", "started_at", "duration_ms", "rows_affected", "error"]
        return {"runs": [dict(zip(keys, row)) for row in result["rows"]]}

    def _tool_run_unit_tests(self, args: dict) -> dict:
        from havn.engine.unit_tests import (
            CATALOG_SQL,
            catalog_from_rows,
            run_unit_tests,
        )

        model = args.get("model")
        if model is not None and not isinstance(model, str):
            raise _InvalidParams("model must be a string")

        # Column types for mocks that don't declare them. A warehouse that
        # isn't there, or is locked by `havn serve`, just means no catalog —
        # the tests themselves run entirely in memory either way.
        catalog: dict = {}
        try:
            rows = self._execute_readonly(CATALOG_SQL, max_rows=_MAX_QUERY_ROWS)["rows"]
            catalog = catalog_from_rows(rows)
        except Exception as e:
            logger.debug("No catalog available for unit tests: %s", e)

        result = run_unit_tests(self.project_dir, model=model, catalog=catalog)
        return result.to_dict()

    def _tool_list_metrics(self, args: dict) -> dict:
        from havn.engine.semantic import load_metrics

        metrics, errors = load_metrics(self.project_dir)
        return {
            "metrics": [m.to_dict() for m in metrics.values()],
            "errors": errors,
        }

    def _tool_query_metric(self, args: dict) -> dict:
        from havn.engine.semantic import SemanticError, compile_metric, get_metric

        name = str(args.get("metric") or "")
        try:
            metric = get_metric(self.project_dir, name)
            limit = args.get("limit")
            sql = compile_metric(
                metric,
                dimensions=args.get("dimensions"),
                grain=args.get("grain"),
                start=args.get("start"),
                end=args.get("end"),
                limit=int(limit) if limit is not None else None,
            )
        except SemanticError as e:
            raise ToolError(str(e))
        result = self._execute_readonly(sql, max_rows=int(args.get("limit") or 1000))
        result["metric"] = name
        result["sql"] = sql
        return result

    def _tool_ask(self, args: dict) -> dict:
        from havn.engine.ai.config import AIConfigError
        from havn.engine.ai.service import ServerAskError, call_server, local_context

        question = args.get("question")
        if not isinstance(question, str) or not question.strip():
            raise _InvalidParams("question is required")
        history = args.get("history") or []
        if not isinstance(history, list):
            raise _InvalidParams("history must be a list")
        body = {
            "question": question,
            "history": [h for h in history if isinstance(h, dict) and h.get("question")][:10],
            "exploratory": bool(args.get("exploratory", False)),
        }
        try:
            via_server = call_server(self.project_dir, "POST", "/api/ask", body)
        except ServerAskError as e:
            raise ToolError(str(e))
        if via_server is not None:
            return _trim_ask(via_server)

        from havn.engine.ai.ask import ask

        try:
            with local_context(self.project_dir, env=self.env) as ctx:
                result = ask(
                    question, ctx, history=body["history"], exploratory=body["exploratory"],
                )
        except AIConfigError as e:
            raise ToolError(str(e))
        return _trim_ask(result)

    def _tool_submit_change_set(self, args: dict) -> dict:
        from havn.engine.ai.service import ServerAskError, call_server
        from havn.engine.changesets import (
            ChangeSet,
            ChangeSetError,
            create_change_set,
            revise_change_set,
        )
        from havn.engine.changesets.service import report_text, verify_locally

        files = args.get("files")
        if not isinstance(files, list) or not files:
            raise _InvalidParams("files must be a non-empty list of {path, content}")
        proposals = []
        for f in files:
            if not isinstance(f, dict) or not isinstance(f.get("path"), str):
                raise _InvalidParams("each file needs a path")
            delete = bool(f.get("delete")) or f.get("content") is None
            proposals.append({"path": f["path"], "content": None if delete else str(f["content"])})
        title = str(args.get("title") or "")
        cs_id = args.get("change_set_id")
        if cs_id is not None and not isinstance(cs_id, str):
            raise _InvalidParams("change_set_id must be a string")

        body = {
            "files": [
                {"path": p["path"], "content": p["content"], "delete": p["content"] is None}
                for p in proposals
            ],
            "title": title,
            "source": "mcp",
            "change_set_id": cs_id,
        }
        try:
            via_server = call_server(self.project_dir, "POST", "/api/changesets", body)
        except ServerAskError as e:
            raise ToolError(str(e))
        try:
            if via_server is not None:
                cs = ChangeSet.from_dict(via_server)
            else:
                if cs_id:
                    cs = revise_change_set(self.project_dir, cs_id, proposals, title=title or None)
                else:
                    cs = create_change_set(self.project_dir, proposals, source="mcp", title=title)
                cs = verify_locally(self.project_dir, cs.id, env=self.env)
        except ChangeSetError as e:
            raise ToolError(str(e))
        return _change_set_payload(cs, report_text(cs))

    def _tool_get_change_set(self, args: dict) -> dict:
        from havn.engine.changesets import ChangeSetError, get_change_set
        from havn.engine.changesets.service import report_text

        cs_id = args.get("change_set_id")
        if not isinstance(cs_id, str) or not cs_id:
            raise _InvalidParams("change_set_id is required")
        try:
            cs = get_change_set(self.project_dir, cs_id)
        except ChangeSetError as e:
            raise ToolError(str(e))
        return _change_set_payload(cs, report_text(cs))

    def _tool_run_transform(self, args: dict) -> dict:
        if self._server_info() is not None:
            raise ToolError(
                "The warehouse is locked by a running `havn serve`. "
                "Run transforms through the web UI, or stop the server first."
            )

        from havn.engine.database import open_warehouse
        from havn.engine.transform import run_transform

        config = self._config()
        select = args.get("select")
        exclude = args.get("exclude")
        force = bool(args.get("force", False))
        targets = [str(select)] if select else None
        exclusions = [str(exclude)] if exclude else None

        run_id = None
        if config.rewind.enabled:
            from havn.engine.snapshots import start_run

            run_id = start_run(self.project_dir, trigger="mcp")

        conn = open_warehouse(config, self.project_dir)
        try:
            results = run_transform(
                conn,
                self.project_dir / "transform",
                targets=targets,
                exclude=exclusions,
                force=force,
                project_dir=self.project_dir,
                db_config=config.database,
                rewind_config=config.rewind if run_id else None,
                run_id=run_id,
            )
        except Exception as e:
            raise ToolError(f"transform failed: {e}")
        finally:
            conn.close()

        if run_id and config.rewind.enabled:
            try:
                from havn.engine.snapshots import finish_run

                errors = sum(1 for s in results.values() if s == "error")
                status = "failed" if errors else "success"
                finish_run(self.project_dir, run_id, status, list(results.keys()))
            except Exception as e:
                logger.warning("rewind finish_run failed: %s", e)

        summary = {
            "built": sum(1 for s in results.values() if s == "built"),
            "skipped": sum(1 for s in results.values() if str(s).startswith("skipped")),
            "inlined": sum(1 for s in results.values() if s == "inlined"),
            "errors": sum(1 for s in results.values() if s == "error"),
        }
        return {"summary": summary, "results": results}


def _claim_protocol_stdout():
    """Reserve the process's real stdout for JSON-RPC frames.

    Returns a writer on a duplicate of file descriptor 1 and points fd 1 at
    stderr, so output that bypasses ``sys.stdout`` (C extensions, child
    processes) cannot interleave with protocol frames either. Falls back to
    ``sys.stdout`` when it has no usable descriptor (e.g. under a test runner
    that replaced it); the per-message ``redirect_stdout`` still applies.
    """
    try:
        sys.stdout.flush()
        fd = sys.stdout.fileno()
        protocol = os.fdopen(os.dup(fd), "w", encoding="utf-8", newline="\n")
        os.dup2(sys.stderr.fileno(), fd)
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        return sys.stdout
    return protocol


class _InvalidParams(ValueError):
    """Raised by handlers when tool/method params are malformed."""


def _trim_ask(result: dict, max_rows: int = 200) -> dict:
    """An ask answer sized for an agent's context: rows capped, rest intact."""
    out = dict(result)
    res = out.get("result")
    if isinstance(res, dict) and len(res.get("rows") or []) > max_rows:
        res = dict(res)
        res["rows"] = res["rows"][:max_rows]
        res["rows_shown"] = max_rows
        out["result"] = res
    return out


def _change_set_payload(cs, summary: str) -> dict:
    """What an agent needs to iterate: the verdict, the failures, the diff."""
    data = cs.to_dict(include_content=False)
    report = data.get("report") or {}
    return {
        "change_set_id": cs.id,
        "revision": cs.revision,
        "status": cs.status,
        "ok": bool(report.get("ok")),
        "summary": summary,
        "failures": [
            c for c in report.get("checks", []) if c.get("status") in ("fail", "warn")
        ],
        "diffs": report.get("diffs", []),
        "files": [f"{f['action']} {f['path']}" for f in data["files"]],
        "ignored": data.get("ignored", []),
        "next": (
            "Verified. Tell the user it is ready to apply from the havn UI (Agent sidebar, Changes)."
            if report.get("ok")
            else "Fix the failures and call submit_change_set again with change_set_id."
        ),
    }


def _error_response(msg_id: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": code, "message": message},
    }
