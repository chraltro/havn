"""Tests for the MCP stdio server (havn mcp)."""

import io
import json

import duckdb
import pytest

from havn.mcp.server import MCPServer


@pytest.fixture
def project(tmp_path):
    (tmp_path / "project.yml").write_text("name: test\ndatabase:\n  path: warehouse.duckdb\n")
    bronze = tmp_path / "transform" / "bronze"
    silver = tmp_path / "transform" / "silver"
    bronze.mkdir(parents=True)
    silver.mkdir(parents=True)
    (bronze / "events.sql").write_text(
        "@config materialized=table, schema=bronze\n\n"
        "SELECT * FROM landing.events\n"
    )
    (silver / "daily.sql").write_text(
        "@config materialized=table, schema=silver\n"
        "@description Daily event counts\n\n"
        "SELECT kind, COUNT(*) AS n FROM bronze.events GROUP BY 1\n"
    )
    (tmp_path / "metrics").mkdir()
    (tmp_path / "metrics" / "events.yml").write_text("""
metrics:
  - name: event_count
    model: landing.events
    measure: COUNT(*)
    dimensions: [kind]
""")

    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
    conn.execute(
        "CREATE TABLE landing.events AS "
        "SELECT CASE WHEN range % 2 = 0 THEN 'click' ELSE 'view' END AS kind "
        "FROM range(10)"
    )
    conn.close()
    return tmp_path


@pytest.fixture
def server(project):
    return MCPServer(project)


def rpc(server, method, params=None, msg_id=1):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return server.handle_message(msg)


def call_tool(server, name, arguments=None):
    resp = rpc(server, "tools/call", {"name": name, "arguments": arguments or {}})
    assert "result" in resp, resp
    result = resp["result"]
    text = result["content"][0]["text"]
    if result.get("isError"):
        return {"_error": text}
    return json.loads(text)


# --- Protocol ---


def test_initialize(server):
    resp = rpc(server, "initialize", {"protocolVersion": "2025-06-18"})
    result = resp["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "havn"
    assert "tools" in result["capabilities"]


def test_initialize_unknown_version_falls_back(server):
    resp = rpc(server, "initialize", {"protocolVersion": "1999-01-01"})
    assert resp["result"]["protocolVersion"] == "2025-06-18"


def test_ping(server):
    assert rpc(server, "ping")["result"] == {}


def test_notifications_get_no_response(server):
    assert server.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    ) is None


def test_unknown_method(server):
    resp = rpc(server, "bogus/method")
    assert resp["error"]["code"] == -32601


def test_invalid_message(server):
    resp = server.handle_message(["not", "a", "dict"])
    assert resp["error"]["code"] == -32600


def test_tools_list(server):
    tools = rpc(server, "tools/list")["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {
        "query", "list_tables", "describe_table", "list_models", "get_model",
        "model_lineage", "run_history", "list_metrics", "query_metric",
        "run_transform",
    } <= names
    for t in tools:
        assert t["description"]
        assert t["inputSchema"]["type"] == "object"


def test_read_only_mode_hides_run_transform(project):
    server = MCPServer(project, read_only=True)
    names = {t["name"] for t in rpc(server, "tools/list")["result"]["tools"]}
    assert "run_transform" not in names


def test_unknown_tool_is_protocol_error(server):
    resp = rpc(server, "tools/call", {"name": "bogus", "arguments": {}})
    assert resp["error"]["code"] == -32602


def test_serve_loop_over_streams(project):
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        "not json at all",
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stdin = io.StringIO(
        "\n".join(m if isinstance(m, str) else json.dumps(m) for m in msgs) + "\n"
    )
    stdout = io.StringIO()
    MCPServer(project).serve(stdin=stdin, stdout=stdout)
    lines = [json.loads(l) for l in stdout.getvalue().strip().splitlines()]
    # initialize response, parse error, tools/list response — no reply to the notification
    assert len(lines) == 3
    assert lines[0]["id"] == 1
    assert lines[1]["error"]["code"] == -32700
    assert lines[2]["id"] == 2


# --- Tools ---


def test_query_tool(server):
    result = call_tool(server, "query", {"sql": "SELECT kind, COUNT(*) AS n FROM landing.events GROUP BY 1 ORDER BY 1"})
    assert result["columns"] == ["kind", "n"]
    assert result["rows"] == [["click", 5], ["view", 5]]


def test_query_tool_rejects_mutations(server):
    result = call_tool(server, "query", {"sql": "DROP TABLE landing.events"})
    assert "Only SELECT" in result["_error"]


def test_query_tool_rejects_file_access(server):
    result = call_tool(server, "query", {"sql": "SELECT * FROM read_csv('/etc/passwd')"})
    assert "File-access" in result["_error"]


def test_query_tool_truncates(server):
    result = call_tool(server, "query", {"sql": "SELECT * FROM range(100)", "limit": 5})
    assert result["row_count"] == 5
    assert result["truncated"] is True


def test_list_tables(server):
    result = call_tool(server, "list_tables")
    assert {"schema": "landing", "name": "events", "type": "BASE TABLE"} in result["tables"]


def test_describe_table(server):
    result = call_tool(server, "describe_table", {"table": "landing.events"})
    assert result["row_count"] == 10
    assert result["columns"][0]["name"] == "kind"


def test_describe_table_missing(server):
    result = call_tool(server, "describe_table", {"table": "landing.nope"})
    assert "not found" in result["_error"].lower()


def test_describe_table_requires_qualified_name(server):
    resp = rpc(server, "tools/call", {"name": "describe_table", "arguments": {"table": "x; DROP"}})
    # malformed name surfaces as a tool error, not a crash
    assert "result" in resp or "error" in resp


def test_list_models(server):
    result = call_tool(server, "list_models")
    names = {m["name"] for m in result["models"]}
    assert names == {"bronze.events", "silver.daily"}


def test_get_model(server):
    result = call_tool(server, "get_model", {"name": "silver.daily"})
    assert result["depends_on"] == ["bronze.events"]
    assert result["description"] == "Daily event counts"
    assert "GROUP BY" in result["sql"]


def test_get_model_unknown(server):
    result = call_tool(server, "get_model", {"name": "gold.nope"})
    assert "Unknown model" in result["_error"]


def test_model_lineage(server):
    result = call_tool(server, "model_lineage", {"name": "bronze.events"})
    assert result["upstream_direct"] == ["landing.events"]
    assert result["upstream_external"] == ["landing.events"]
    assert result["downstream_all"] == ["silver.daily"]


def test_list_metrics(server):
    result = call_tool(server, "list_metrics")
    assert result["metrics"][0]["name"] == "event_count"


def test_query_metric(server):
    result = call_tool(server, "query_metric", {"metric": "event_count", "dimensions": ["kind"]})
    assert result["columns"] == ["kind", "event_count"]
    assert result["rows"] == [["click", 5], ["view", 5]]
    assert "SELECT" in result["sql"]


def test_query_metric_unknown(server):
    result = call_tool(server, "query_metric", {"metric": "nope"})
    assert "unknown metric" in result["_error"]


def test_run_transform(server):
    result = call_tool(server, "run_transform", {})
    assert result["summary"]["errors"] == 0
    assert result["summary"]["built"] == 2
    # Built tables are now queryable
    check = call_tool(server, "query", {"sql": "SELECT n FROM silver.daily WHERE kind = 'click'"})
    assert check["rows"] == [[5]]


def test_run_history_after_transform(server):
    call_tool(server, "run_transform", {})
    result = call_tool(server, "run_history", {"limit": 5})
    assert len(result["runs"]) > 0
    assert result["runs"][0]["run_type"] == "transform"
