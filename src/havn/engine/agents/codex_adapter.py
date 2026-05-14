"""Codex adapter using the CLI in non-interactive mode."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncGenerator

from havn.engine.agents.base import AgentAdapter, spawn_cli


# Codex 0.125 prints these at ERROR level on stderr but they are non-fatal
# session/rollout bookkeeping warnings. The model reply is unaffected, so
# we filter them so they do not surface as user-visible errors.
_BENIGN_STDERR_FRAGMENTS = (
    "failed to record rollout items",
    "thread .* not found",  # noqa: literal substring is fine for `in` check
)


def _filter_benign_stderr(stderr: str) -> str:
    """Drop known-benign codex stderr lines, keep the rest."""
    if not stderr:
        return ""
    keep: list[str] = []
    for line in stderr.splitlines():
        if any(frag in line for frag in _BENIGN_STDERR_FRAGMENTS):
            continue
        keep.append(line)
    return "\n".join(keep).strip()


class CodexAdapter(AgentAdapter):
    """Adapter for OpenAI Codex CLI via subprocess."""

    name = "codex"
    display_name = "Codex"

    def __init__(self) -> None:
        self._project_path: str | None = None
        self._system_prompt: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self.permission_mode: str = "auto"
        self.model: str = ""
        # Captured from the first message's thread.started event so we can
        # resume the same conversation on follow-ups (codex exec is otherwise
        # stateless and would forget every prior turn).
        self._thread_id: str | None = None

    async def start_session(
        self, project_path: str, system_prompt: str | None = None
    ) -> None:
        self._project_path = project_path
        self._system_prompt = system_prompt
        self._thread_id = None

    async def send_message(self, message: str) -> AsyncGenerator[dict, None]:
        # Read prompt from stdin (`-`) so a large system prompt + message does
        # not blow past Windows' ~32 KB CreateProcess command-line limit.
        # `codex exec` is stateless, so to keep conversation history across
        # turns we capture the thread_id from the first turn and use
        # `codex exec resume <id>` for follow-ups.
        cmd = ["codex", "exec"]
        if self._thread_id:
            cmd.extend(["resume", self._thread_id])
        cmd.extend(["--json", "--skip-git-repo-check"])
        if self.permission_mode == "ask":
            cmd.extend(["--sandbox", "read-only"])
        else:
            # In auto mode the user has opted in to letting the agent edit
            # files. `--full-auto` does NOT actually grant writes in codex
            # 0.125 unless the project is registered as trusted in
            # ~/.codex/config.toml, so we use the explicit bypass flag
            # instead. The sidebar already runs locally on the user's box,
            # and Edit/Write/Bash are the whole point of auto mode.
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        if self.model:
            cmd.extend(["-m", self.model])
        cmd.append("-")  # read prompt from stdin

        # Only inject the project context on the first turn; afterwards the
        # resumed session already has it in history.
        if self._system_prompt and self._thread_id is None:
            stdin_payload = (
                f"[Project context]\n{self._system_prompt}\n\n"
                f"[User message]\n{message}"
            )
        else:
            stdin_payload = message

        if self._process is not None and self._process.returncode is None:
            try:
                self._process.kill()
                await self._process.wait()
            except Exception:
                pass
            self._process = None

        try:
            self._process = await spawn_cli(
                cmd, cwd=self._project_path, stdin=asyncio.subprocess.PIPE
            )
        except FileNotFoundError:
            yield {"type": "error", "content": "Codex CLI not found. Install it with: npm install -g @openai/codex"}
            yield {"type": "done", "content": ""}
            return
        except OSError as exc:
            # Windows raises OSError 206 (ENAMETOOLONG) when argv exceeds the
            # CreateProcess limit. We pipe the prompt via stdin to avoid this,
            # but surface a clear message in case something else trips it.
            yield {"type": "error", "content": f"Failed to launch Codex CLI: {exc}"}
            yield {"type": "done", "content": ""}
            return

        try:
            assert self._process.stdin is not None
            self._process.stdin.write(stdin_payload.encode("utf-8"))
            await self._process.stdin.drain()
            self._process.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

        saw_assistant_text = False
        saw_delta = False
        saw_auth_error = False
        async for line in self._process.stdout:  # type: ignore[union-attr]
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                yield {"type": "text", "content": text}
                saw_assistant_text = True
                continue

            # Capture thread_id from the very first event of a fresh session
            # so the next user message can resume the same conversation.
            if self._thread_id is None and event.get("type") == "thread.started":
                tid = event.get("thread_id")
                if isinstance(tid, str) and tid:
                    self._thread_id = tid

            for chunk in self._parse_event(event):
                is_final = chunk.pop("_final", False)
                # Skip the consolidated agent.message if we already streamed
                # deltas, to avoid duplicating the whole response.
                if is_final and saw_delta:
                    continue
                if chunk.get("type") == "text" and chunk.get("content"):
                    saw_assistant_text = True
                    if not is_final:
                        saw_delta = True
                if chunk.get("type") == "error":
                    msg = str(chunk.get("content", ""))
                    if "401" in msg or "Unauthorized" in msg or "refresh token" in msg:
                        saw_auth_error = True
                yield chunk

        await self._process.wait()

        if saw_auth_error:
            yield {
                "type": "error",
                "content": (
                    "Codex authentication failed (401 Unauthorized). "
                    "Run `codex login` in a terminal to sign in, then try again."
                ),
            }
        elif (not saw_assistant_text or self._process.returncode) and self._process.stderr:
            stderr = (await self._process.stderr.read()).decode(errors="replace").strip()
            stderr = _filter_benign_stderr(stderr)
            if stderr:
                yield {"type": "error", "content": stderr}

        yield {"type": "done", "content": ""}

    def _parse_event(self, event: dict) -> list[dict]:
        """Parse a Codex CLI JSONL event into sidebar chunks.

        Codex 0.125 emits:
          {"type":"thread.started", ...}
          {"type":"turn.started"}
          {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
          {"type":"item.completed","item":{"type":"command_execution",...}}
          {"type":"turn.completed","usage":{...}}
          {"type":"error","message":"..."}
          {"type":"turn.failed","error":{"message":"..."}}

        Codex 0.46 used these instead (kept for back-compat):
          {"type":"agent.message.delta","delta":"..."}
          {"type":"agent.message","message":"..."}
        """
        chunks: list[dict] = []
        etype = event.get("type", "")

        if etype == "item.completed":
            item = event.get("item", {}) or {}
            item_type = item.get("type", "")
            if item_type == "agent_message":
                text = item.get("text", "")
                if text:
                    chunks.append({"type": "text", "content": text})
            elif item_type == "file_change":
                # Codex 0.125 reports file edits as file_change items.
                # Translate each change to a tool_use chunk so the sidebar's
                # editor-reload hook (which keys off Edit/Write tool names)
                # triggers a live reload of the open file.
                changes = item.get("changes") or []
                for change in changes:
                    path = change.get("path", "")
                    kind = change.get("kind", "")
                    # `add` -> Write (new file), `update`/`delete` -> Edit
                    tool_name = "Write" if kind == "add" else "Edit"
                    short = path.replace("\\", "/").split("/")
                    detail = "/".join(short[-2:]) if len(short) > 1 else (short[0] if short else "")
                    chunks.append(
                        {
                            "type": "tool_use",
                            "content": tool_name,
                            "detail": detail,
                            "tool_input": {"file_path": path} if path else None,
                        }
                    )
            elif item_type in ("command_execution", "tool_call", "function_call"):
                name = (
                    item.get("name")
                    or item.get("command")
                    or item.get("tool")
                    or "tool"
                )
                chunks.append({"type": "tool_use", "content": str(name)[:80], "detail": ""})
        elif etype == "agent.message.delta":
            delta = event.get("delta", "")
            if delta:
                chunks.append({"type": "text", "content": delta})
        elif etype == "agent.message":
            msg = event.get("message", "")
            if msg:
                chunks.append({"type": "text", "content": msg, "_final": True})
        elif etype == "error":
            chunks.append({"type": "error", "content": event.get("message", str(event))})
        elif etype == "turn.failed":
            err = event.get("error", {}) or {}
            chunks.append({"type": "error", "content": err.get("message", "Turn failed")})
        # Ignore thread.started / turn.started / turn.completed (no user-facing content)

        return chunks

    async def stop_session(self) -> None:
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
        self._thread_id = None

    @classmethod
    def is_available(cls) -> bool:
        return shutil.which("codex") is not None
