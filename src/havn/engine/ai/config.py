"""The ``ai:`` section of project.yml.

.. code-block:: yaml

    ai:
      provider: anthropic            # anthropic | openai (any OpenAI-compatible endpoint) | agent
      # agent: claude                # with provider: agent -- claude | codex | gemini (the sidebar's CLIs)
      model: claude-sonnet-5-5
      # base_url: http://localhost:11434/v1   # Ollama, LM Studio, vLLM, ...
      # api_key_env: ANTHROPIC_API_KEY        # name of the variable in .env
      exploratory_sql: false         # allow an unverified SQL fallback
      share_dimension_values: false  # send distinct dimension values to the model
      summarize_results: false       # send result rows to the model for a summary

Without ``provider:``, Ask uses the Anthropic API when ``ANTHROPIC_API_KEY``
is set, and otherwise the first agent CLI the sidebar can use (Claude Code,
Codex, Gemini CLI), which is already signed in and needs no key.

The API key is never written in project.yml: ``api_key_env`` names the
variable (loaded from ``.env``) that holds it. Parsed here rather than in
``havn.config`` so the AI feature carries its own defaults and validation.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROVIDERS = ("anthropic", "openai", "agent")
# Agent CLIs Ask can use, in the order the automatic default tries them.
AGENT_CLIS = ("claude", "codex", "gemini")

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5-5",
    "openai": "",  # no sensible default across OpenAI-compatible servers
    "agent": "",  # the CLI's own default model
}

DEFAULT_BASE_URLS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "agent": "",
}

DEFAULT_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "agent": "",
}


def available_agent_cli() -> str | None:
    """The first agent CLI on PATH, or None."""
    import shutil

    return next((name for name in AGENT_CLIS if shutil.which(name)), None)

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0")


class AIConfigError(ValueError):
    """The ``ai:`` section is invalid or the provider is not usable."""


@dataclass
class AIConfig:
    provider: str = "anthropic"
    model: str = DEFAULT_MODELS["anthropic"]
    # provider: agent -- which CLI (claude | codex | gemini).
    agent: str = ""
    base_url: str = DEFAULT_BASE_URLS["anthropic"]
    api_key_env: str = DEFAULT_KEY_ENV["anthropic"]
    timeout: float = 60.0
    max_tokens: int = 4096
    max_rows: int = 1000
    exploratory_sql: bool = False
    share_dimension_values: bool = False
    summarize_results: bool = False

    @property
    def api_key(self) -> str | None:
        """The key from the environment (``.env`` is loaded by load_project)."""
        value = os.environ.get(self.api_key_env, "") if self.api_key_env else ""
        return value or None

    @property
    def is_local(self) -> bool:
        """True when requests go to this machine (a local model server)."""
        from urllib.parse import urlparse

        if self.provider == "agent":
            return False  # the CLI sends the prompt to its own vendor
        host = (urlparse(self.base_url).hostname or "").lower()
        return host in _LOCAL_HOSTS or host.endswith(".local")

    def public_dict(self) -> dict:
        """Settings safe to show in the UI: never the key, only whether it is set."""
        out = asdict(self)
        out["api_key_set"] = self.api_key is not None
        out["is_local"] = self.is_local
        return out


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def parse_ai_config(raw: dict | None) -> AIConfig:
    """Build an :class:`AIConfig` from the raw ``ai:`` mapping."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise AIConfigError("ai: must be a mapping")
    provider = str(raw.get("provider") or "").strip().lower()
    if not provider:
        key_env = str(raw.get("api_key_env") or DEFAULT_KEY_ENV["anthropic"])
        if os.environ.get(key_env) or raw.get("base_url") or not available_agent_cli():
            provider = "anthropic"
        else:
            provider = "agent"
    if provider in ("openai-compatible", "openai_compatible", "ollama", "local"):
        provider = "openai"
    if provider in AGENT_CLIS:  # provider: claude is shorthand for agent + claude
        raw = {**raw, "agent": raw.get("agent") or provider}
        provider = "agent"
    if provider not in PROVIDERS:
        raise AIConfigError(
            f"ai.provider: unknown provider {provider!r} (use one of: {', '.join(PROVIDERS)})"
        )
    try:
        timeout = float(raw.get("timeout", 60))
        max_tokens = int(raw.get("max_tokens", 4096))
        max_rows = int(raw.get("max_rows", 1000))
    except (TypeError, ValueError) as e:
        raise AIConfigError(f"ai: {e}")
    if timeout <= 0 or max_tokens <= 0 or max_rows <= 0:
        raise AIConfigError("ai: timeout, max_tokens and max_rows must be positive")
    agent = ""
    if provider == "agent":
        agent = str(raw.get("agent") or available_agent_cli() or "claude").strip().lower()
        if agent not in AGENT_CLIS:
            raise AIConfigError(f"ai.agent: unknown agent {agent!r} (use one of: {', '.join(AGENT_CLIS)})")
        if "timeout" not in raw:
            timeout = 180.0  # a CLI starts up and may think for a while
        base_url = ""
    else:
        base_url = str(raw.get("base_url") or DEFAULT_BASE_URLS[provider]).rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise AIConfigError(f"ai.base_url must be an http(s) URL, got {base_url!r}")
    return AIConfig(
        provider=provider,
        agent=agent,
        model=str(raw.get("model") or DEFAULT_MODELS[provider]),
        base_url=base_url,
        api_key_env=str(raw.get("api_key_env") or DEFAULT_KEY_ENV[provider]),
        timeout=timeout,
        max_tokens=max_tokens,
        max_rows=min(max_rows, 50_000),
        exploratory_sql=_as_bool(raw.get("exploratory_sql"), False),
        share_dimension_values=_as_bool(raw.get("share_dimension_values"), False),
        summarize_results=_as_bool(raw.get("summarize_results"), False),
    )


def load_ai_config(project_dir: Path | str | None = None, config: Any = None) -> AIConfig:
    """Read ``ai:`` from a loaded ProjectConfig, or load project.yml.

    Loading the project also loads ``.env`` into the environment, which is
    where the API key comes from.
    """
    if config is None:
        from havn.config import load_project

        config = load_project(Path(project_dir) if project_dir else None)
    raw = getattr(config, "_raw", None) or {}
    return parse_ai_config(raw.get("ai"))
