"""Language-model providers for ``havn ask``, over plain HTTP.

Two wire formats cover every model havn talks to:

- :class:`AnthropicProvider` -- the Anthropic Messages API.
- :class:`OpenAICompatibleProvider` -- ``/chat/completions``, which OpenAI and
  every common local server speaks (Ollama, LM Studio, vLLM, llama.cpp). This
  is the one that keeps everything on the machine.

Both use the standard library's ``urllib`` so the feature adds no dependency
and no SDK. A provider has one job: given a system prompt, a short message
history and a JSON schema, return one JSON object. What goes into the prompt
is decided by :mod:`havn.engine.ai.ask`, which sends catalog metadata only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any

from havn.engine.ai.config import AIConfig, AIConfigError

logger = logging.getLogger("havn.ai")

ANTHROPIC_VERSION = "2023-06-01"


class ProviderError(RuntimeError):
    """The model could not be reached, refused, or returned no usable JSON."""


class LLMProvider:
    """Base class. Subclasses implement :meth:`complete_json`."""

    name = "base"
    model = ""

    def complete_json(
        self,
        *,
        system: str,
        messages: list[dict[str, str]],
        schema: dict | None = None,
    ) -> dict:
        """Return the model's answer as a JSON object.

        ``messages`` alternate ``user`` / ``assistant`` and end with ``user``.
        """
        raise NotImplementedError

    def describe(self) -> str:
        return f"{self.name}:{self.model}"


def extract_json_object(text: str) -> dict:
    """Pull the first JSON object out of a model's text answer.

    Local models wrap JSON in prose or code fences often enough that a strict
    ``json.loads`` would fail on answers that are otherwise right.
    """
    text = (text or "").strip()
    if not text:
        raise ProviderError("the model returned an empty answer")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)
    start = text.find("{")
    if start >= 0:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    raise ProviderError(f"the model did not return a JSON object: {text[:200]!r}")


def _post_json(url: str, body: dict, headers: dict[str, str], timeout: float) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        raise _HTTPStatusError(e.code, detail[:500]) from None
    except (urllib.error.URLError, ConnectionError, OSError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        raise ProviderError(f"could not reach {url}: {reason}") from None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        raise ProviderError(f"{url} returned something that is not JSON") from None


class _HTTPStatusError(ProviderError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class AnthropicProvider(LLMProvider):
    """The Anthropic Messages API (``POST /v1/messages``)."""

    name = "anthropic"

    def __init__(self, config: AIConfig) -> None:
        if not config.api_key:
            raise AIConfigError(
                f"No API key for the Anthropic provider: set {config.api_key_env} "
                "in .env, or point ai.provider at a local OpenAI-compatible model."
            )
        self.config = config
        self.model = config.model
        self._structured = True

    def _body(self, system: str, messages: list[dict], schema: dict | None) -> dict:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.config.max_tokens,
            "system": system,
            "messages": messages,
        }
        if schema is not None and self._structured:
            body["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        return body

    def complete_json(self, *, system, messages, schema=None) -> dict:
        url = f"{self.config.base_url}/v1/messages"
        headers = {
            "x-api-key": self.config.api_key or "",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        try:
            data = _post_json(url, self._body(system, messages, schema), headers, self.config.timeout)
        except _HTTPStatusError as e:
            # A gateway or an older model that does not accept structured
            # outputs: fall back to asking for JSON in the prompt alone.
            if e.status == 400 and self._structured and schema is not None and (
                "output_config" in e.detail or "format" in e.detail or "schema" in e.detail
            ):
                logger.info("Structured output rejected (%s); retrying without it", e.detail[:120])
                self._structured = False
                data = _post_json(url, self._body(system, messages, None), headers, self.config.timeout)
            else:
                raise
        if data.get("stop_reason") == "refusal":
            raise ProviderError("the model declined to answer this question")
        text = "".join(
            block.get("text", "")
            for block in data.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        )
        return extract_json_object(text)


class OpenAICompatibleProvider(LLMProvider):
    """``POST {base_url}/chat/completions`` -- OpenAI, Ollama, LM Studio, vLLM."""

    name = "openai"

    def __init__(self, config: AIConfig) -> None:
        if not config.model:
            raise AIConfigError(
                "ai.model is required for an OpenAI-compatible provider "
                "(for Ollama, the model name you pulled, e.g. 'qwen2.5:14b')."
            )
        if not config.api_key and not config.is_local:
            raise AIConfigError(
                f"No API key for {config.base_url}: set {config.api_key_env} in .env "
                "(a local server on localhost needs none)."
            )
        self.config = config
        self.model = config.model
        self._json_mode = True

    def _body(self, system: str, messages: list[dict]) -> dict:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": 0,
            "max_tokens": self.config.max_tokens,
        }
        if self._json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def complete_json(self, *, system, messages, schema=None) -> dict:
        url = f"{self.config.base_url}/chat/completions"
        headers = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        try:
            data = _post_json(url, self._body(system, messages), headers, self.config.timeout)
        except _HTTPStatusError as e:
            if e.status == 400 and self._json_mode and "response_format" in e.detail:
                self._json_mode = False
                data = _post_json(url, self._body(system, messages), headers, self.config.timeout)
            else:
                raise
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise ProviderError("the model server returned no message") from None
        return extract_json_object(text)


def provider_from_config(config: AIConfig) -> LLMProvider:
    """The provider ``ai:`` asks for. Raises AIConfigError when unusable."""
    if config.provider == "anthropic":
        return AnthropicProvider(config)
    if config.provider == "openai":
        return OpenAICompatibleProvider(config)
    if config.provider == "agent":
        return AgentCLIProvider(config)
    raise AIConfigError(f"unknown ai.provider {config.provider!r}")


class AgentCLIProvider(LLMProvider):
    """Ask through the agent CLI the sidebar uses (Claude Code, Codex, Gemini CLI).

    The CLI is already signed in, so no API key is needed. It runs headless,
    with no tools, in an empty temporary directory: it sees only the prompt
    (catalog metadata, as with the API providers), not the project. The prompt
    goes in on stdin, never through a shell.
    """

    name = "agent"

    def __init__(self, config: AIConfig) -> None:
        import shutil

        from havn.engine.ai.config import AIConfigError

        self.agent = config.agent or "claude"
        self.model = config.model or self.agent
        self._model_flag = config.model
        self.timeout = config.timeout
        if not shutil.which(self.agent):
            raise AIConfigError(
                f"ai.agent: the {self.agent} CLI is not on PATH (it is the same CLI the agent sidebar uses)"
            )

    def describe(self) -> str:
        return f"agent:{self.agent}" + (f":{self._model_flag}" if self._model_flag else "")

    def _command(self, workdir: str) -> tuple[list[str], str | None]:
        """(argv, file the answer is written to, if not stdout)."""
        if self.agent == "claude":
            cmd = [
                "claude", "-p", "--output-format", "text",
                "--system-prompt", "You answer with exactly one JSON object and nothing else.",
                "--tools", "", "--no-session-persistence", "--strict-mcp-config",
            ]
            if self._model_flag:
                cmd += ["--model", self._model_flag]
            return cmd, None
        if self.agent == "codex":
            out = os.path.join(workdir, "answer.txt")
            cmd = ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
                   "--output-last-message", out]
            if self._model_flag:
                cmd += ["-m", self._model_flag]
            return cmd + ["-"], out
        cmd = ["gemini", "--approval-mode", "plan", "-p", "Answer the request given on stdin."]
        if self._model_flag:
            cmd += ["-m", self._model_flag]
        return cmd, None

    def complete_json(self, *, system, messages, schema=None) -> dict:
        import subprocess
        import tempfile

        from havn.engine.agents.base import resolve_cli_command

        parts = [system.strip(), ""]
        if schema:
            parts += ["Reply with one JSON object matching this JSON schema:", json.dumps(schema), ""]
        for m in messages:
            parts += [f"[{m['role']}]", m["content"], ""]
        parts.append("Reply with the JSON object only: no prose, no code fences.")
        prompt = "\n".join(parts)

        with tempfile.TemporaryDirectory(prefix="havn-ask-") as workdir:
            cmd, out_file = self._command(workdir)
            try:
                proc = subprocess.run(
                    resolve_cli_command(cmd), input=prompt, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", cwd=workdir, timeout=self.timeout,
                )
            except subprocess.TimeoutExpired:
                raise ProviderError(f"the {self.agent} CLI did not answer within {self.timeout:.0f}s") from None
            except OSError as e:
                raise ProviderError(f"could not start the {self.agent} CLI: {e}") from None
            text = proc.stdout
            if out_file and os.path.exists(out_file):
                with open(out_file, encoding="utf-8", errors="replace") as f:
                    text = f.read()
        if proc.returncode != 0 and not text.strip():
            detail = (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
            raise ProviderError(f"the {self.agent} CLI failed: {detail[0][:300]}")
        return extract_json_object(text)
