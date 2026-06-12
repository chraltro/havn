# MCP Server

`havn mcp` starts a [Model Context Protocol](https://modelcontextprotocol.io)
server over stdio, exposing your warehouse to AI agents — Claude Code,
Codex CLI, or any other MCP client. The agent can explore tables, run
read-only queries, inspect the transform DAG and lineage, query
semantic-layer metrics, and (optionally) build models.

No extra dependencies: the protocol implementation ships with havn.

## Setup

```bash
# Claude Code
claude mcp add havn -- havn mcp -p /path/to/your/project

# Any other MCP client: configure a stdio server with
#   command: havn
#   args: ["mcp", "-p", "/path/to/your/project"]
```

Options:

| Flag | Meaning |
|------|---------|
| `-p, --project` | Project directory (default: current dir) |
| `-e, --env` | Environment to use (`dev`, `prod`, ...) |
| `--read-only` | Disable the `run_transform` tool entirely |

## Tools

| Tool | Description |
|------|-------------|
| `query` | Read-only SQL (mutations and file-access functions are rejected) |
| `list_tables` | Tables and views, optionally filtered by schema |
| `describe_table` | Columns, types, and row count |
| `list_models` | Transform models with materialization and dependencies |
| `get_model` | A model's SQL source, config, and dependencies |
| `model_lineage` | Direct + transitive upstreams and downstreams |
| `run_history` | Recent pipeline runs from `_havn.run_log` |
| `list_metrics` | Semantic-layer metrics from `metrics/*.yml` |
| `query_metric` | Query a metric by dimensions / grain / time range |
| `run_transform` | Build models (omitted with `--read-only`) |

## How it coexists with `havn serve`

DuckDB allows one writing process at a time. When `havn serve` is running,
it holds the warehouse lock — the MCP server detects this and routes read
queries through the server's HTTP API instead of opening the file directly
(the same strategy the CLI uses). `run_transform` refuses to run while the
lock is held and tells the agent why.

## Safety model

The MCP server has the same trust level as your local CLI:

- Every `query` / `query_metric` / introspection call goes through the same
  read-only SQL validation as the web API: single statement, no mutation
  verbs, no `read_csv` / `read_parquet` / HTTP functions.
- Reads use a read-only DuckDB connection — even a validator bypass could
  not write.
- `run_transform` is the only mutating tool. It builds your own transform
  models (with Pipeline Rewind snapshots when enabled, so agent-triggered
  builds can be rolled back like any other run). Start with `--read-only`
  if the agent shouldn't build at all.
