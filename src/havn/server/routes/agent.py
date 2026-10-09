"""Agent sidebar: WebSocket endpoint and REST API for coding agents."""

from __future__ import annotations

from havn.textio import read_project_text

import json
import logging
import re
from pathlib import Path

from fastapi import APIRouter, Request
from starlette.websockets import WebSocket, WebSocketDisconnect

from havn.server.deps import _authenticate_websocket, _require_permission

router = APIRouter()
log = logging.getLogger(__name__)

# Active agent sessions keyed by WebSocket id (object id)
_active_sessions: dict[int, dict] = {}


def _build_system_prompt(project_path: str, port: int = 3000) -> str:
    """Build a context-rich system prompt so the agent understands havn conventions."""
    base_url = f"http://localhost:{port}"
    parts = [
        "You are an assistant embedded in the havn data platform web UI.",
        "You are shown in the Agent sidebar panel within the havn web interface.",
        f"Project root: {project_path}",
        "",
        "# What is havn?",
        "havn is a self-hosted data platform (a Nordic alternative to Databricks/Snowflake).",
        "It uses DuckDB for OLAP analytics, plain SQL for transforms, and Python for",
        "ingest/export scripts. All data lives in a single warehouse.duckdb file.",
        "",
        "# The web UI you are embedded in",
        "The user is interacting with you from the havn web UI (started via `havn serve`).",
        "The UI has four main sections, each with sub-tabs:",
        "",
        "- **Develop**: Editor (Monaco code editor for SQL/Python files), Notebooks (.dpnb), DAG (dependency graph of all SQL models)",
        "- **Explore**: Query (interactive SQL runner with autocomplete), Tables (browse warehouse tables/views/columns), Data Sources (manage connectors)",
        "- **Observe**: Quality (data quality checks and profiling), Sentinel (monitoring), Diff (preview transform changes), History (pipeline run log)",
        "- **Configure**: Masking (column-level data masking), Wiki (built-in documentation), Docs (auto-generated schema docs), Settings (project settings)",
        "",
        "The user can navigate to any of these tabs. When they ask about features, refer to",
        "the correct tab/section. For example:",
        '- "How do I see the DAG?" -> Click the DAG tab under Develop',
        '- "How do I run a query?" -> Use the Query tab under Explore',
        '- "Where are my tables?" -> Tables tab under Explore',
        '- "How do I check data quality?" -> Quality tab under Observe',
        "",
        "Other UI features:",
        "- The Run button (top bar) executes pipelines. The dropdown arrow has options: Transform, Lint, Contracts, Full Refresh.",
        "- The file tree (left sidebar) shows project files. Click a file to open it in the Editor.",
        "- The output panel (bottom) shows execution logs, errors, and results.",
        "- The Overview tab shows project health, recent runs, and quick actions.",
        "",
        "# Key CLI commands (also available from the UI)",
        "- `havn serve` - start web UI",
        "- `havn transform` - build SQL models (change detection skips unchanged)",
        "- `havn transform --force` - rebuild all models",
        "- `havn jobs run <name>` - run a job (ingest -> transform -> export)",
        "- `havn query \"SELECT ...\"` - ad-hoc SQL",
        "- `havn tables` - list warehouse tables",
        "- `havn lint` / `havn lint --fix` - lint SQL files",
        "- `havn diff` - preview what would change",
        "- `havn history` - show run log",
        "- `havn connect <type>` - set up a data connector",
        "- `havn run <script>` - run a single ingest/export script",
        "",
        "# Data architecture",
        "Data flows through four DuckDB schemas:",
        "  landing (raw) -> bronze (cleaned) -> silver (business logic) -> gold (consumption-ready)",
        "",
        "SQL transforms live in transform/ subdirectories (bronze/, silver/, gold/).",
        "Python ingest scripts live in ingest/, export scripts in export/.",
        "Config is in project.yml, secrets in .env.",
        "",
        "# SQL model conventions",
        "Every .sql file in transform/ has @-prefixed directives at the top:",
        "  @config materialized=table, schema=silver",
        "  @assert row_count > 0",
        "  @assert no_nulls(customer_id)",
        "Dependencies are auto-extracted from FROM/JOIN clauses, so @depends_on is",
        "optional -- use it only when a model name is hidden behind a function or",
        "string interpolation. Other directives: @description, @col <name>: <text>.",
        "Legacy '-- config: ...' / '-- depends_on: ...' / '-- assert: ...' SQL-comment",
        "syntax still parses for back-compat. No Jinja, no templating, just plain SQL.",
        "",
        "# Python script conventions",
        "Ingest/export scripts get a `db` (DuckDB connection) pre-injected.",
        "Just write top-level code: db.execute(\"CREATE OR REPLACE TABLE landing.data AS ...\")",
        "",
        "# Connectors",
        "havn has 20+ built-in connectors: PostgreSQL, MySQL, SQLite, Stripe, Shopify,",
        "HubSpot, Google Sheets, S3/GCS, REST APIs, CSV, Parquet, webhooks, and more.",
        "Set them up via `havn connect <type>` or the Data Sources tab in the UI.",
        "",
        "CRITICAL SECURITY CONSTRAINT:",
        f"You MUST NEVER read, write, search, or access any files outside of {project_path}.",
        "- ALL file operations must target paths within the project root.",
        "- ALL Bash commands must operate within the project root.",
        "- If a user asks to access files outside the project, refuse and explain why.",
        "",
        "CRITICAL EXECUTION CONSTRAINT:",
        "NEVER run pipeline, transform, lint, or any data-modifying commands yourself.",
        "This includes: `havn transform`, `havn jobs run`, `havn lint --fix`, `havn run`,",
        "`havn query` with INSERT/UPDATE/DELETE, or any curl POST to /api/transform,",
        "/api/stream, /api/lint, /api/run endpoints.",
        "These operations use the shared DuckDB connection and will cause file locking",
        "conflicts with the running server. They can also make irreversible changes to",
        "the warehouse that the user cannot undo.",
        "Instead, TELL THE USER to run these operations from the UI (Run button, etc.).",
        "You may only READ data: `havn query \"SELECT ...\"`, `havn tables`, `havn diff`,",
        "curl GET endpoints, etc.",
        "",
        "# Searching the wiki for answers",
        "When you don't know the answer to a question about havn features, SEARCH THE WIKI.",
        "Use curl to query the local API (the server you are running inside):",
        "",
        f"  curl -s {base_url}/api/wiki/search/<keyword>",
        "",
        "This returns matching excerpts from wiki pages. To read the full page:",
        "",
        f"  curl -s {base_url}/api/wiki/<slug>",
        "",
        "Available wiki slugs: getting-started, configuration, environments, transforms,",
        "pipelines, seeds, sources, connectors, cdc, quality, contracts, lineage, auth,",
        "masking, sentinel, scheduler, notebooks, versioning, cli-reference, api-reference.",
        "",
        "ALWAYS search the wiki before saying you don't know about a feature.",
        "",
        "# Querying the project DAG and lineage",
        "When you need to understand model dependencies (e.g., before renaming a column,",
        "changing a schema, or understanding downstream impact), query the local API:",
        "",
        f"  curl -s {base_url}/api/dag",
        "",
        "This returns the full DAG with nodes and edges. Each node has:",
        "  { id, schema, name, type, materialized, depends_on, file_path }",
        "Each edge has: { from, to }",
        "",
        "For column-level lineage of a specific model:",
        "",
        f"  curl -s {base_url}/api/lineage/<schema.model>",
        "",
        "For downstream impact analysis:",
        "",
        f"  curl -s {base_url}/api/impact/<schema.model>",
        "",
        "IMPORTANT: When changing column names, table schemas, or model structure,",
        "ALWAYS check downstream dependencies first using the DAG or impact API.",
        "Fix ALL affected downstream models before reporting the change as complete.",
        "",
        "When editing files:",
        "- Follow existing naming conventions in the project",
        "- For SQL models: use -- config and -- depends_on headers",
        "- For Python scripts: the `db` connection is pre-injected",
        "- Always explain what you changed and why",
    ]

    # Include project.yml content if it exists
    project_yml = Path(project_path) / "project.yml"
    if project_yml.exists():
        try:
            content = read_project_text(project_yml)
            parts.extend(["", "# project.yml contents:", "```yaml", content, "```"])
        except Exception:
            pass

    # Include wiki index for reference knowledge
    wiki_index = Path(__file__).resolve().parent.parent.parent / "wiki" / "pages" / "index.md"
    if wiki_index.exists():
        try:
            wiki_content = read_project_text(wiki_index)
            parts.extend([
                "",
                "# havn documentation reference",
                "Below is the havn wiki index. Use this to answer questions about havn features.",
                "The wiki is also available to the user in the Wiki tab under Configure.",
                "",
                wiki_content,
            ])
        except Exception:
            pass

    return "\n".join(parts)


@router.get("/api/agents")
def list_agents(request: Request) -> list[dict]:
    """List available coding agents and their installation status."""
    _require_permission(request, "read")
    from havn.engine.agents.registry import list_available_agents

    return list_available_agents()


def register_agent_websocket(app) -> None:
    """Register the agent WebSocket endpoint on the FastAPI app."""
    try:

        @app.websocket("/ws/agent")
        async def websocket_agent(websocket: WebSocket) -> None:
            """WebSocket endpoint for agent sidebar communication.

            Protocol:
              Client sends:
                { "type": "start", "agent": "claude", "project_path": "/path" }
                { "type": "message", "message": "Add a customer table" }
                { "type": "stop" }

              Server sends:
                { "type": "ready", "agent": "claude" }
                { "type": "chunk", "chunk_type": "text"|"tool_use"|"diff", "content": "..." }
                { "type": "done" }
                { "type": "error", "message": "..." }
            """
            import havn.server.app as server_app

            # Authenticate before accepting the connection
            ws_user = _authenticate_websocket(websocket)
            if ws_user is None:
                await websocket.close(code=4001, reason="Authentication required")
                return

            # ...and authorize. This socket drives a coding agent inside the
            # project directory with the server process's privileges: it can
            # write files and run shell commands. Authentication alone would let
            # any viewer token do that, so require "write" as every other
            # mutating surface does.
            from havn.engine.auth import has_permission
            if not has_permission(ws_user.get("role", ""), "write"):
                await websocket.close(code=4003, reason="Permission denied")
                return
            # A coding agent has a shell as the server's OS user, which no
            # masking or row policy can follow; refuse it to users they apply to.
            try:
                from havn.server.deps import _get_read_pool, _is_governed

                with _get_read_pool().connection() as cur:
                    governed = _is_governed(ws_user, cur)
            except Exception:
                governed = ws_user.get("role") != "admin"
            if governed:
                await websocket.close(code=4003, reason="Not available: masking or row policies apply to you")
                return

            await websocket.accept()
            ws_id = id(websocket)
            _active_sessions[ws_id] = {"adapter": None, "streaming": False, "user": ws_user}

            try:
                while True:
                    raw = await websocket.receive_text()
                    if len(raw) > 200_000:
                        await websocket.send_json(
                            {"type": "error", "message": "Message too large"}
                        )
                        continue

                    data = json.loads(raw)
                    msg_type = data.get("type", "")

                    if msg_type == "start":
                        await _handle_start(
                            websocket, ws_id, data, server_app.PROJECT_DIR
                        )
                    elif msg_type == "message":
                        await _handle_message(websocket, ws_id, data)
                    elif msg_type == "set_mode":
                        await _handle_set_mode(websocket, ws_id, data)
                    elif msg_type == "set_model":
                        await _handle_set_model(websocket, ws_id, data)
                    elif msg_type == "stop":
                        await _handle_stop(websocket, ws_id)
                    else:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "message": f"Unknown message type: {msg_type}",
                            }
                        )

            except WebSocketDisconnect:
                log.debug("WebSocket %s disconnected", ws_id)
            except json.JSONDecodeError:
                try:
                    await websocket.send_json(
                        {"type": "error", "message": "Invalid JSON"}
                    )
                except Exception:
                    pass
            except Exception as exc:
                log.exception("WebSocket %s unhandled error: %s", ws_id, exc)
                try:
                    await websocket.send_json(
                        {"type": "error", "message": str(exc)}
                    )
                except Exception:
                    pass
            finally:
                await _cleanup_session(ws_id)

    except ImportError:
        pass  # starlette WebSocket support not available


async def _handle_start(
    websocket: WebSocket, ws_id: int, data: dict, project_dir: Path
) -> None:
    """Handle agent:start — spawn the adapter and start a session."""
    from havn.engine.agents import get_adapter

    agent_name = data.get("agent", "claude")
    # Always use server-side project dir — never trust client-provided paths.
    project_path = str(project_dir)

    # Clean up any existing session
    session = _active_sessions.get(ws_id)
    if session and session.get("adapter"):
        try:
            await session["adapter"].stop_session()
        except Exception:
            pass

    previous = _active_sessions.get(ws_id) or {}
    if previous.get("workspace") is not None:
        previous["workspace"].close()
        previous["workspace"] = None

    adapter = get_adapter(agent_name)
    if adapter is None:
        await websocket.send_json(
            {"type": "error", "message": f"Unknown agent: {agent_name}"}
        )
        return

    if not adapter.is_available():
        await websocket.send_json(
            {
                "type": "error",
                "message": f"{adapter.display_name} is not installed. "
                f"Install it and make sure the '{agent_name}' command is on PATH.",
            }
        )
        return

    # Extract port from WebSocket scope for API URL in system prompt
    server = websocket.scope.get("server")
    port = server[1] if server and len(server) > 1 else 3000
    permission_mode = data.get("mode", "auto")
    if permission_mode not in _MODES:
        permission_mode = "auto"

    # Review mode: the agent works in a copy of the project, and what it
    # changes comes back as a change set that is verified before the user
    # can apply it. The project itself is never written by the agent.
    workspace = None
    agent_path = project_path
    if permission_mode == "review":
        from havn.engine.changesets.overlay import AgentWorkspace

        workspace = AgentWorkspace(project_dir)
        workspace.sync()
        agent_path = str(workspace.root)
    system_prompt = _build_system_prompt(agent_path, port=port)
    if workspace is not None:
        system_prompt += "\n\n" + _REVIEW_PROMPT

    model = _sanitize_model(data.get("model", ""))
    log.info("Starting agent session: agent=%s, project=%s, mode=%s, model=%s",
             agent_name, project_path, permission_mode, model)
    try:
        # Review mode needs the agent's write tools: its writes land in the
        # workspace copy, not the project.
        adapter.permission_mode = "auto" if permission_mode == "review" else permission_mode
        adapter.model = model
        await adapter.start_session(agent_path, system_prompt)
        _active_sessions[ws_id] = {
            "adapter": adapter,
            "agent": agent_name,
            "mode": permission_mode,
            "model": model,
            "workspace": workspace,
            "changeset_id": None,
            "user": previous.get("user") or {},
        }
        await websocket.send_json({"type": "ready", "agent": agent_name, "mode": permission_mode})
    except Exception as exc:
        log.exception("Failed to start agent session")
        if workspace is not None:
            workspace.close()
        await websocket.send_json(
            {"type": "error", "message": f"Failed to start: {exc}"}
        )


async def _handle_message(websocket: WebSocket, ws_id: int, data: dict) -> None:
    """Handle agent:message — send message and stream response chunks."""
    session = _active_sessions.get(ws_id)
    if not session or not session.get("adapter"):
        await websocket.send_json(
            {"type": "error", "message": "No active agent session. Send 'start' first."}
        )
        return

    if session.get("streaming"):
        await websocket.send_json(
            {"type": "error", "message": "Already processing a message. Wait for it to finish."}
        )
        return

    message = data.get("message", "").strip()
    if not message:
        await websocket.send_json(
            {"type": "error", "message": "Empty message"}
        )
        return

    if len(message) > 100_000:
        await websocket.send_json(
            {"type": "error", "message": "Message too long (100K char limit)"}
        )
        return

    adapter = session["adapter"]
    session["streaming"] = True
    workspace = session.get("workspace")
    if workspace is not None:
        import asyncio

        await asyncio.to_thread(_sync_workspace, session)
    try:
        async for chunk in adapter.send_message(message):
            chunk_type = chunk.get("type", "text")
            content = chunk.get("content", "")

            if chunk_type == "done":
                await websocket.send_json({"type": "done"})
            else:
                msg = {
                    "type": "chunk",
                    "chunk_type": chunk_type,
                    "content": content,
                }
                detail = chunk.get("detail")
                if detail:
                    msg["detail"] = detail
                tool_input = chunk.get("tool_input")
                if tool_input:
                    msg["tool_input"] = tool_input
                await websocket.send_json(msg)
        if workspace is not None:
            await _collect_change_set(websocket, session, message)
    except (WebSocketDisconnect, RuntimeError):
        # Client disconnected mid-stream — stop the agent process quietly
        try:
            await adapter.stop_session()
        except Exception:
            pass
    except Exception as exc:
        log.exception("Error streaming agent response")
        try:
            await websocket.send_json(
                {"type": "error", "message": f"Agent error: {exc}"}
            )
            await websocket.send_json({"type": "done"})
        except Exception:
            pass  # client already gone
    finally:
        session["streaming"] = False


async def _handle_set_mode(websocket: WebSocket, ws_id: int, data: dict) -> None:
    """Handle set_mode: ask (read-only), auto (writes files) or review.

    Review runs the agent in a workspace copy of the project, so switching
    into or out of it restarts the agent session in the other directory.
    """
    import havn.server.app as server_app

    session = _active_sessions.get(ws_id)
    mode = data.get("mode", "auto")
    if mode not in _MODES:
        await websocket.send_json(
            {"type": "error", "message": f"Unknown mode: {mode}. Use 'ask', 'auto' or 'review'."}
        )
        return
    if session and session.get("adapter"):
        old = session.get("mode", "auto")
        if (old == "review") != (mode == "review"):
            await _handle_start(
                websocket, ws_id,
                {"agent": session.get("agent", "claude"), "mode": mode, "model": session.get("model", "")},
                server_app.PROJECT_DIR,
            )
            await websocket.send_json({"type": "mode_changed", "mode": mode, "restarted": True})
            return
        session["adapter"].permission_mode = mode
        session["mode"] = mode
    await websocket.send_json({"type": "mode_changed", "mode": mode})


async def _handle_set_model(websocket: WebSocket, ws_id: int, data: dict) -> None:
    """Handle set_model — change the model used by the agent."""
    session = _active_sessions.get(ws_id)
    model = _sanitize_model(data.get("model", ""))
    if session and session.get("adapter"):
        session["adapter"].model = model
        session["model"] = model
    await websocket.send_json({"type": "model_changed", "model": model})


# Only allow model IDs that look like real model identifiers
_MODEL_RE = re.compile(r"^[a-zA-Z0-9._:-]*$")


def _sanitize_model(value: str) -> str:
    """Validate model string — reject anything that isn't a clean identifier."""
    if not value:
        return ""
    if len(value) > 100 or not _MODEL_RE.match(value):
        return ""
    return value


async def _handle_stop(websocket: WebSocket, ws_id: int) -> None:
    """Handle agent:stop — clean up the current session."""
    session = _active_sessions.get(ws_id)
    if session and session.get("adapter"):
        try:
            await session["adapter"].stop_session()
        except Exception:
            pass
        session["adapter"] = None
    if session and session.get("workspace") is not None:
        session["workspace"].close()
        session["workspace"] = None
    await websocket.send_json({"type": "stopped"})


async def _cleanup_session(ws_id: int) -> None:
    """Clean up session on disconnect."""
    session = _active_sessions.pop(ws_id, None)
    if session and session.get("adapter"):
        try:
            await session["adapter"].stop_session()
        except Exception:
            pass
    if session and session.get("workspace") is not None:
        session["workspace"].close()


# ---------------------------------------------------------------------------
# Review mode: agent edits become verified change sets
# ---------------------------------------------------------------------------

_MODES = ("ask", "auto", "review")

_REVIEW_PROMPT = """\
# Review mode
You are working in a review copy of the project, not the project itself. Edit
model files here as you normally would (transform/**/*.sql, macros/,
tests/unit/, contracts/, metrics/). When you finish, havn collects your edits
into a change set and verifies it: parse and DAG checks, the bind pass, the
unit tests of affected models, contracts, assertions, and a data diff of
every affected model built into a scratch database. The user sees the report
and decides whether to apply it. Files outside those folders are not carried
over. The warehouse and .env are not in this copy; read data through the
havn API (curl the server) as usual. If the user tells you a check failed,
fix it in this copy: your previous edits are still here."""


async def _send_jsonable(websocket: WebSocket, payload: dict) -> None:
    """send_json with str() for values json cannot encode (dates, decimals).

    A verification report carries rows from unit tests and contracts, and a
    plain send_json raised on the first date in them.
    """
    await websocket.send_text(json.dumps(payload, default=str))


def _open_change_set(session: dict):
    """The session's change set while it can still be revised, else None."""
    import havn.server.app as server_app
    from havn.engine.changesets import ChangeSetError, get_change_set

    cs_id = session.get("changeset_id")
    if not cs_id:
        return None
    try:
        cs = get_change_set(server_app.PROJECT_DIR, cs_id)
    except ChangeSetError:
        return None
    return cs if cs.status not in ("applied", "discarded") else None


def _sync_workspace(session: dict) -> None:
    """Make the workspace match the project plus the session's open proposal."""
    from havn.engine.changesets.store import proposed_files

    cs = _open_change_set(session)
    session["workspace"].sync(proposed_files(cs) if cs else None)


async def _collect_change_set(websocket: WebSocket, session: dict, message: str) -> None:
    """Turn what the agent changed in its workspace into a verified change set."""
    import asyncio

    import havn.server.app as server_app
    from havn.engine.changesets import ChangeSetError, create_change_set, revise_change_set

    project_dir = server_app.PROJECT_DIR
    proposals, ignored = await asyncio.to_thread(session["workspace"].diff)
    if not proposals:
        if ignored:
            await websocket.send_json({"type": "changeset_ignored", "paths": ignored})
        return
    title = " ".join(message.split())[:120]
    existing = _open_change_set(session)
    try:
        if existing is not None:
            cs = revise_change_set(project_dir, existing.id, proposals, ignored=ignored)
        else:
            cs = create_change_set(
                project_dir, proposals,
                source=f"agent:{session.get('agent', 'agent')}", title=title, ignored=ignored,
            )
    except ChangeSetError as e:
        await websocket.send_json({"type": "error", "message": f"Could not record the change set: {e}"})
        return
    session["changeset_id"] = cs.id
    await _send_jsonable(websocket, {
        "type": "changeset",
        "changeset": {**cs.to_dict(project_dir, include_content=False), "status": "verifying"},
    })
    role = (session.get("user") or {}).get("role", "editor")
    # Awaited here rather than spawned: a task outliving this handler call was
    # cancelled with the request scope. The agent's reply is already done; the
    # next message waits for the report, which the agent should see anyway.
    await _verify_and_notify(websocket, cs.id, role)


async def _verify_and_notify(websocket: WebSocket, cs_id: str, role: str) -> None:
    import asyncio

    import havn.server.app as server_app

    try:
        cs = await asyncio.to_thread(verify_with_server_connection, cs_id, role)
    except Exception as e:
        log.exception("Change set verification failed")
        try:
            await websocket.send_json({"type": "error", "message": f"Verification failed: {e}"})
        except Exception:
            pass
        return
    try:
        await _send_jsonable(websocket, {
            "type": "changeset",
            "changeset": cs.to_dict(server_app.PROJECT_DIR, include_content=False),
        })
    except Exception as e:
        # The sidebar closed; the report is stored and listed in the UI.
        log.debug("Could not send the change set report: %s", e)


def verify_with_server_connection(cs_id: str, role: str):
    """Verify on a read cursor from the server's pool (None without a warehouse)."""
    from havn.engine.changesets.service import verify_and_store
    from havn.server.deps import _get_backend, _get_config, _get_project_dir, _get_read_pool

    project_dir = _get_project_dir()
    if not _get_backend().exists():
        return verify_and_store(project_dir, cs_id, conn=None, config=_get_config(), role=role)
    with _get_read_pool().connection() as cursor:
        return verify_and_store(project_dir, cs_id, conn=cursor, config=_get_config(), role=role)
