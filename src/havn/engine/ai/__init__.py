"""AI analyst: questions answered through the semantic layer.

``havn ask`` (and the Ask panel and the ``ask`` MCP tool) turn a question into
a structured query spec over the metrics in ``metrics/*.yml``. The language
model only ever picks from that catalog: havn validates the spec, compiles it
with the semantic layer and runs it through the governed read path. See
``docs/ask.md``.
"""

from __future__ import annotations

from havn.engine.ai.config import AIConfig, load_ai_config
from havn.engine.ai.spec import QuerySpec, SpecError

__all__ = ["AIConfig", "QuerySpec", "SpecError", "load_ai_config"]
