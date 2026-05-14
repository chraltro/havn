"""Tests for the Agent Sidebar: adapters, registry, REST API, and WebSocket."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest
from starlette.testclient import TestClient

from havn.engine.agents.base import AgentAdapter


# ---------------------------------------------------------------------------
# Mock adapter for testing (doesn't require any CLI installed)
# ---------------------------------------------------------------------------


class MockAdapter(AgentAdapter):
    """Test adapter that yields canned responses."""

    name = "mock"
    display_name = "Mock Agent"

    def __init__(self) -> None:
        self._started = False
        self._stopped = False
        self._project_path: str | None = None
        self._system_prompt: str | None = None

    async def start_session(
        self, project_path: str, system_prompt: str | None = None
    ) -> None:
        self._started = True
        self._project_path = project_path
        self._system_prompt = system_prompt

    async def send_message(self, message: str) -> AsyncGenerator[dict, None]:
        yield {"type": "text", "content": f"Echo: {message}"}
        yield {"type": "done", "content": ""}

    async def stop_session(self) -> None:
        self._stopped = True

    @classmethod
    def is_available(cls) -> bool:
        return True


class MockUnavailableAdapter(MockAdapter):
    name = "unavailable"
    display_name = "Unavailable Agent"

    @classmethod
    def is_available(cls) -> bool:
        return False


# ---------------------------------------------------------------------------
# Agent Registry Tests
# ---------------------------------------------------------------------------


class TestAgentRegistry:
    def test_list_available_agents(self):
        from havn.engine.agents.registry import list_available_agents

        agents = list_available_agents()
        assert isinstance(agents, list)
        assert len(agents) >= 3
        ids = [a["id"] for a in agents]
        assert "claude" in ids
        assert "codex" in ids
        assert "gemini" in ids
        for agent in agents:
            assert "id" in agent
            assert "name" in agent
            assert "available" in agent

    def test_get_adapter_known(self):
        from havn.engine.agents import get_adapter

        adapter = get_adapter("claude")
        assert adapter is not None
        assert adapter.name == "claude"

    def test_get_adapter_unknown(self):
        from havn.engine.agents import get_adapter

        adapter = get_adapter("nonexistent")
        assert adapter is None

    def test_get_adapter_returns_new_instance(self):
        from havn.engine.agents import get_adapter

        a1 = get_adapter("claude")
        a2 = get_adapter("claude")
        assert a1 is not a2


# ---------------------------------------------------------------------------
# Adapter Unit Tests
# ---------------------------------------------------------------------------


class TestClaudeAdapter:
    def test_parse_text_event(self):
        from havn.engine.agents.claude_adapter import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Hello world"},
                ]
            },
        }
        chunks = adapter._parse_event(event)
        assert len(chunks) == 1
        assert chunks[0] == {"type": "text", "content": "Hello world"}

    def test_parse_tool_use_event(self):
        from havn.engine.agents.claude_adapter import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {}},
                ]
            },
        }
        chunks = adapter._parse_event(event)
        assert len(chunks) == 1
        assert chunks[0]["type"] == "tool_use"
        assert chunks[0]["content"] == "Read"
        assert "detail" in chunks[0]

    def test_parse_result_event(self):
        from havn.engine.agents.claude_adapter import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        event = {"type": "result", "result": "Done!"}
        chunks = adapter._parse_event(event)
        assert chunks == [{"type": "text", "content": "Done!"}]

    def test_parse_error_event(self):
        from havn.engine.agents.claude_adapter import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        event = {"type": "error", "error": "Something broke"}
        chunks = adapter._parse_event(event)
        assert chunks == [{"type": "error", "content": "Something broke"}]

    def test_parse_empty_result(self):
        from havn.engine.agents.claude_adapter import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        event = {"type": "result", "result": ""}
        chunks = adapter._parse_event(event)
        assert chunks == []

    def test_parse_mixed_content(self):
        from havn.engine.agents.claude_adapter import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Let me read the file."},
                    {"type": "tool_use", "name": "Bash", "input": {}},
                    {"type": "text", "text": "Done reading."},
                ]
            },
        }
        chunks = adapter._parse_event(event)
        assert len(chunks) == 3
        assert chunks[0]["type"] == "text"
        assert chunks[1]["type"] == "tool_use"
        assert chunks[2]["type"] == "text"

    def test_parse_unknown_event_type(self):
        from havn.engine.agents.claude_adapter import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        event = {"type": "unknown_type", "data": "stuff"}
        chunks = adapter._parse_event(event)
        assert chunks == []


class TestCodexAdapter:
    def test_parse_agent_message_delta(self):
        from havn.engine.agents.codex_adapter import CodexAdapter

        adapter = CodexAdapter()
        chunks = adapter._parse_event(
            {"type": "agent.message.delta", "delta": "Hello "}
        )
        assert chunks == [{"type": "text", "content": "Hello "}]

    def test_parse_agent_message_marked_final(self):
        from havn.engine.agents.codex_adapter import CodexAdapter

        adapter = CodexAdapter()
        chunks = adapter._parse_event(
            {"type": "agent.message", "message": "Done"}
        )
        assert len(chunks) == 1
        assert chunks[0]["type"] == "text"
        assert chunks[0]["content"] == "Done"
        assert chunks[0].get("_final") is True

    def test_parse_error_event(self):
        from havn.engine.agents.codex_adapter import CodexAdapter

        adapter = CodexAdapter()
        chunks = adapter._parse_event(
            {"type": "error", "message": "401 Unauthorized"}
        )
        assert chunks == [{"type": "error", "content": "401 Unauthorized"}]

    def test_parse_turn_failed(self):
        from havn.engine.agents.codex_adapter import CodexAdapter

        adapter = CodexAdapter()
        chunks = adapter._parse_event(
            {"type": "turn.failed", "error": {"message": "Failed to refresh token"}}
        )
        assert chunks == [{"type": "error", "content": "Failed to refresh token"}]

    def test_parse_ignored_event(self):
        from havn.engine.agents.codex_adapter import CodexAdapter

        adapter = CodexAdapter()
        # thread.started / turn.started have no user-facing content
        assert adapter._parse_event({"type": "thread.started"}) == []
        assert adapter._parse_event({"type": "turn.started"}) == []
        assert adapter._parse_event({"type": "turn.completed", "usage": {}}) == []

    def test_parse_item_completed_agent_message(self):
        """Codex 0.125 emits assistant text via item.completed/agent_message."""
        from havn.engine.agents.codex_adapter import CodexAdapter

        adapter = CodexAdapter()
        chunks = adapter._parse_event(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Hi"}}
        )
        assert chunks == [{"type": "text", "content": "Hi"}]

    def test_parse_file_change_add_emits_write_tool_use(self):
        """file_change with kind=add should produce a Write tool_use so the
        sidebar editor reload hook fires."""
        from havn.engine.agents.codex_adapter import CodexAdapter

        adapter = CodexAdapter()
        chunks = adapter._parse_event(
            {
                "type": "item.completed",
                "item": {
                    "type": "file_change",
                    "changes": [{"path": "C:/foo/bar.sql", "kind": "add"}],
                    "status": "completed",
                },
            }
        )
        assert len(chunks) == 1
        assert chunks[0]["type"] == "tool_use"
        assert chunks[0]["content"] == "Write"
        assert chunks[0]["tool_input"] == {"file_path": "C:/foo/bar.sql"}

    def test_parse_file_change_update_emits_edit_tool_use(self):
        from havn.engine.agents.codex_adapter import CodexAdapter

        adapter = CodexAdapter()
        chunks = adapter._parse_event(
            {
                "type": "item.completed",
                "item": {
                    "type": "file_change",
                    "changes": [{"path": "C:/foo/bar.sql", "kind": "update"}],
                },
            }
        )
        assert len(chunks) == 1
        assert chunks[0]["content"] == "Edit"

    def test_parse_file_change_multiple_paths(self):
        from havn.engine.agents.codex_adapter import CodexAdapter

        adapter = CodexAdapter()
        chunks = adapter._parse_event(
            {
                "type": "item.completed",
                "item": {
                    "type": "file_change",
                    "changes": [
                        {"path": "a.sql", "kind": "update"},
                        {"path": "b.sql", "kind": "add"},
                    ],
                },
            }
        )
        assert len(chunks) == 2
        assert chunks[0]["content"] == "Edit"
        assert chunks[1]["content"] == "Write"

    def test_parse_item_completed_tool_call(self):
        from havn.engine.agents.codex_adapter import CodexAdapter

        adapter = CodexAdapter()
        chunks = adapter._parse_event(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "ls"},
            }
        )
        assert len(chunks) == 1
        assert chunks[0]["type"] == "tool_use"
        assert chunks[0]["content"] == "ls"

    def test_filter_benign_stderr_drops_rollout_warning(self):
        from havn.engine.agents.codex_adapter import _filter_benign_stderr

        noisy = (
            "2026-04-29T12:30:04.912646Z ERROR codex_core::session: "
            "failed to record rollout items: thread abc not found"
        )
        assert _filter_benign_stderr(noisy) == ""

    def test_filter_benign_stderr_keeps_real_errors(self):
        from havn.engine.agents.codex_adapter import _filter_benign_stderr

        real = "Error: invalid API key"
        assert _filter_benign_stderr(real) == real

    def test_filter_benign_stderr_mixed(self):
        from havn.engine.agents.codex_adapter import _filter_benign_stderr

        mixed = (
            "ERROR codex_core::session: failed to record rollout items: thread x not found\n"
            "Error: something actually broke"
        )
        assert _filter_benign_stderr(mixed) == "Error: something actually broke"

    def test_resumes_thread_on_second_message(self, monkeypatch, tmp_path):
        """Follow-up sends should use `codex exec resume <id>` to keep history."""
        import asyncio
        import json
        from havn.engine.agents import codex_adapter as mod

        captured_cmds: list[list[str]] = []

        class FakeStdout:
            def __init__(self, lines: list[bytes]):
                self._lines = list(lines)

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._lines:
                    raise StopAsyncIteration
                return self._lines.pop(0)

        class FakeStdin:
            def write(self, data: bytes) -> None:
                pass

            async def drain(self) -> None:
                pass

            def close(self) -> None:
                pass

        class FakeProcess:
            def __init__(self, thread_id: str):
                self.stdout = FakeStdout(
                    [
                        json.dumps(
                            {"type": "thread.started", "thread_id": thread_id}
                        ).encode()
                        + b"\n",
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {"type": "agent_message", "text": "ok"},
                            }
                        ).encode()
                        + b"\n",
                    ]
                )
                self.stderr = FakeStdout([])
                self.stdin = FakeStdin()
                self.returncode = 0

            async def wait(self) -> int:
                return 0

        async def fake_spawn(cmd, cwd=None, stdin=None):
            captured_cmds.append(list(cmd))
            return FakeProcess("thread-abc-123")

        monkeypatch.setattr(mod, "spawn_cli", fake_spawn)

        async def run():
            adapter = mod.CodexAdapter()
            await adapter.start_session(str(tmp_path))
            async for _ in adapter.send_message("hi"):
                pass
            async for _ in adapter.send_message("again"):
                pass
            return adapter._thread_id

        thread_id = asyncio.run(run())
        assert thread_id == "thread-abc-123"
        assert len(captured_cmds) == 2
        first, second = captured_cmds
        assert "resume" not in first
        assert second[1] == "exec"
        assert second[2] == "resume"
        assert second[3] == "thread-abc-123"


# ---------------------------------------------------------------------------
# System Prompt Tests
# ---------------------------------------------------------------------------


class TestSystemPrompt:
    def test_build_system_prompt_basic(self, tmp_path):
        from havn.server.routes.agent import _build_system_prompt

        prompt = _build_system_prompt(str(tmp_path))
        assert "havn data platform" in prompt
        assert str(tmp_path) in prompt
        assert "DuckDB" in prompt

    def test_build_system_prompt_includes_project_yml(self, tmp_path):
        from havn.server.routes.agent import _build_system_prompt

        (tmp_path / "project.yml").write_text("name: test-project\n")
        prompt = _build_system_prompt(str(tmp_path))
        assert "test-project" in prompt
        assert "project.yml contents:" in prompt

    def test_build_system_prompt_missing_project_yml(self, tmp_path):
        from havn.server.routes.agent import _build_system_prompt

        prompt = _build_system_prompt(str(tmp_path))
        assert "project.yml contents:" not in prompt


# ---------------------------------------------------------------------------
# REST API Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_project(tmp_path):
    """Create a minimal test project for agent tests."""
    (tmp_path / "project.yml").write_text(
        "name: test\ndatabase:\n  path: warehouse.duckdb\n"
        "streams:\n  test:\n    steps:\n      - transform: [all]\n"
    )
    (tmp_path / "transform" / "bronze").mkdir(parents=True)
    (tmp_path / "ingest").mkdir()
    (tmp_path / "export").mkdir()
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
    conn.close()
    return tmp_path


@pytest.fixture
def client(agent_project):
    import havn.server.app as server_app

    server_app.PROJECT_DIR = agent_project
    server_app.AUTH_ENABLED = False
    return TestClient(server_app.app)


class TestAgentsAPI:
    def test_list_agents(self, client):
        resp = client.get("/api/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        ids = [a["id"] for a in data]
        assert "claude" in ids
        assert "codex" in ids
        assert "gemini" in ids

    def test_list_agents_has_availability(self, client):
        resp = client.get("/api/agents")
        data = resp.json()
        for agent in data:
            assert "available" in agent
            assert isinstance(agent["available"], bool)


# ---------------------------------------------------------------------------
# WebSocket Integration Tests
# ---------------------------------------------------------------------------


def _patch_registry_with_mock():
    """Patch the agent registry to include our mock adapter."""
    return patch(
        "havn.server.routes.agent._handle_start",
        wraps=None,
    )


class TestAgentWebSocket:
    def test_websocket_connect_and_start(self, client):
        """Test basic WebSocket connection and agent start with mock."""
        # Patch the registry so our mock adapter is used
        from havn.engine.agents import registry

        original = registry.AGENT_REGISTRY.copy()
        registry.AGENT_REGISTRY["mock"] = MockAdapter
        try:
            with client.websocket_connect("/ws/agent") as ws:
                ws.send_json({"type": "start", "agent": "mock"})
                resp = ws.receive_json()
                assert resp["type"] == "ready"
                assert resp["agent"] == "mock"
        finally:
            registry.AGENT_REGISTRY.clear()
            registry.AGENT_REGISTRY.update(original)

    def test_websocket_send_message(self, client):
        """Test sending a message and receiving streamed chunks."""
        from havn.engine.agents import registry

        original = registry.AGENT_REGISTRY.copy()
        registry.AGENT_REGISTRY["mock"] = MockAdapter
        try:
            with client.websocket_connect("/ws/agent") as ws:
                # Start session
                ws.send_json({"type": "start", "agent": "mock"})
                resp = ws.receive_json()
                assert resp["type"] == "ready"

                # Send message
                ws.send_json({"type": "message", "message": "Hello"})

                # Receive text chunk
                resp = ws.receive_json()
                assert resp["type"] == "chunk"
                assert resp["chunk_type"] == "text"
                assert resp["content"] == "Echo: Hello"

                # Receive done
                resp = ws.receive_json()
                assert resp["type"] == "done"
        finally:
            registry.AGENT_REGISTRY.clear()
            registry.AGENT_REGISTRY.update(original)

    def test_websocket_unknown_agent(self, client):
        """Test that unknown agent names return an error."""
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json({"type": "start", "agent": "nonexistent_agent"})
            resp = ws.receive_json()
            assert resp["type"] == "error"
            assert "Unknown agent" in resp["message"]

    def test_websocket_unavailable_agent(self, client):
        """Test that unavailable agents return an error."""
        from havn.engine.agents import registry

        original = registry.AGENT_REGISTRY.copy()
        registry.AGENT_REGISTRY["unavailable"] = MockUnavailableAdapter
        try:
            with client.websocket_connect("/ws/agent") as ws:
                ws.send_json({"type": "start", "agent": "unavailable"})
                resp = ws.receive_json()
                assert resp["type"] == "error"
                assert "not installed" in resp["message"]
        finally:
            registry.AGENT_REGISTRY.clear()
            registry.AGENT_REGISTRY.update(original)

    def test_websocket_message_without_start(self, client):
        """Test sending a message before starting a session."""
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json({"type": "message", "message": "Hello"})
            resp = ws.receive_json()
            assert resp["type"] == "error"
            assert "No active agent session" in resp["message"]

    def test_websocket_empty_message(self, client):
        """Test sending an empty message."""
        from havn.engine.agents import registry

        original = registry.AGENT_REGISTRY.copy()
        registry.AGENT_REGISTRY["mock"] = MockAdapter
        try:
            with client.websocket_connect("/ws/agent") as ws:
                ws.send_json({"type": "start", "agent": "mock"})
                ws.receive_json()  # ready

                ws.send_json({"type": "message", "message": ""})
                resp = ws.receive_json()
                assert resp["type"] == "error"
                assert "Empty message" in resp["message"]
        finally:
            registry.AGENT_REGISTRY.clear()
            registry.AGENT_REGISTRY.update(original)

    def test_websocket_unknown_message_type(self, client):
        """Test sending an unknown message type."""
        with client.websocket_connect("/ws/agent") as ws:
            ws.send_json({"type": "bogus"})
            resp = ws.receive_json()
            assert resp["type"] == "error"
            assert "Unknown message type" in resp["message"]

    def test_websocket_stop(self, client):
        """Test stopping an agent session."""
        from havn.engine.agents import registry

        original = registry.AGENT_REGISTRY.copy()
        registry.AGENT_REGISTRY["mock"] = MockAdapter
        try:
            with client.websocket_connect("/ws/agent") as ws:
                ws.send_json({"type": "start", "agent": "mock"})
                ws.receive_json()  # ready

                ws.send_json({"type": "stop"})
                resp = ws.receive_json()
                assert resp["type"] == "stopped"

                # Subsequent messages should fail
                ws.send_json({"type": "message", "message": "Hello"})
                resp = ws.receive_json()
                assert resp["type"] == "error"
        finally:
            registry.AGENT_REGISTRY.clear()
            registry.AGENT_REGISTRY.update(original)

    def test_websocket_switch_agent(self, client):
        """Test switching agents mid-session by sending a new start."""
        from havn.engine.agents import registry

        original = registry.AGENT_REGISTRY.copy()
        registry.AGENT_REGISTRY["mock"] = MockAdapter
        try:
            with client.websocket_connect("/ws/agent") as ws:
                # Start with mock
                ws.send_json({"type": "start", "agent": "mock"})
                resp = ws.receive_json()
                assert resp["type"] == "ready"

                # Switch to mock again (simulates switching)
                ws.send_json({"type": "start", "agent": "mock"})
                resp = ws.receive_json()
                assert resp["type"] == "ready"
        finally:
            registry.AGENT_REGISTRY.clear()
            registry.AGENT_REGISTRY.update(original)

    def test_websocket_multiple_messages(self, client):
        """Test sending multiple messages in sequence."""
        from havn.engine.agents import registry

        original = registry.AGENT_REGISTRY.copy()
        registry.AGENT_REGISTRY["mock"] = MockAdapter
        try:
            with client.websocket_connect("/ws/agent") as ws:
                ws.send_json({"type": "start", "agent": "mock"})
                ws.receive_json()  # ready

                for msg in ["first", "second", "third"]:
                    ws.send_json({"type": "message", "message": msg})
                    chunk = ws.receive_json()
                    assert chunk["type"] == "chunk"
                    assert msg in chunk["content"]
                    done = ws.receive_json()
                    assert done["type"] == "done"
        finally:
            registry.AGENT_REGISTRY.clear()
            registry.AGENT_REGISTRY.update(original)

    def test_websocket_ignores_client_project_path(self, client, agent_project):
        """Test that client-provided project_path is ignored (security)."""
        from havn.engine.agents import registry

        started_paths = []

        class PathTrackingAdapter(MockAdapter):
            async def start_session(self, project_path, system_prompt=None):
                started_paths.append(project_path)
                await super().start_session(project_path, system_prompt)

        original = registry.AGENT_REGISTRY.copy()
        registry.AGENT_REGISTRY["mock"] = PathTrackingAdapter
        try:
            with client.websocket_connect("/ws/agent") as ws:
                ws.send_json({
                    "type": "start",
                    "agent": "mock",
                    "project_path": "/etc/shadow",  # malicious path
                })
                resp = ws.receive_json()
                assert resp["type"] == "ready"
                # Should use server PROJECT_DIR, not client-provided path
                assert started_paths[0] == str(agent_project)
                assert started_paths[0] != "/etc/shadow"
        finally:
            registry.AGENT_REGISTRY.clear()
            registry.AGENT_REGISTRY.update(original)
