"""Model Context Protocol (MCP) server for havn.

Exposes the warehouse, transform DAG, run history, and semantic layer as
MCP tools over stdio, so AI agents (Claude Code, Codex CLI, any MCP client)
can work against a havn project. Start it with ``havn mcp``.
"""

from havn.mcp.server import MCPServer, ToolError

__all__ = ["MCPServer", "ToolError"]
