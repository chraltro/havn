"""CLI: ``havn mcp`` — start the MCP (Model Context Protocol) stdio server."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from havn.cli import _resolve_project, app


@app.command()
def mcp(
    env: Annotated[Optional[str], typer.Option("--env", "-e", help="Environment to use")] = None,
    read_only: Annotated[bool, typer.Option("--read-only", help="Disable the run_transform tool")] = False,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """Start an MCP stdio server exposing the warehouse to AI agents.

    Register it with an MCP client, e.g. for Claude Code:

        claude mcp add havn -- havn mcp -p /path/to/project

    Tools: query, list_tables, describe_table, list_models, get_model,
    model_lineage, run_history, list_metrics, query_metric, run_transform.
    """
    import logging
    import sys

    # stdout carries JSON-RPC frames; all logging must go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

    project_dir = _resolve_project(project_dir)

    from havn.mcp.server import MCPServer

    MCPServer(project_dir, env=env, read_only=read_only).serve()
