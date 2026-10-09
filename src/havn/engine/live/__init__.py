"""Live models: continuous refresh from streaming ingest to gold.

- :mod:`.sources`  — stamp landing commits with ``_havn_seq`` and announce them.
- :mod:`.events`   — the in-process "source advanced" bus.
- :mod:`.state`    — watermarks, consumed marks and runner state in ``_havn``.
- :mod:`.graph`    — which models are live and what each consumes.
- :mod:`.refresh`  — the watermark bookkeeping every build of a live model does.
- :mod:`.runner`   — the service that listens, coalesces and refreshes.
- :mod:`.status`   — the read side shared by the CLI, the API and freshness.

This package is imported by model discovery (for :mod:`.settings`), so the
names below are resolved lazily and importing it stays cheap.
"""

from __future__ import annotations

from typing import Any

__all__ = ["advance_source", "LiveRunner", "LiveSettings", "SourceAdvanced", "subscribe"]


def __getattr__(name: str) -> Any:
    if name == "advance_source":
        from .sources import advance_source

        return advance_source
    if name == "LiveRunner":
        from .runner import LiveRunner

        return LiveRunner
    if name == "LiveSettings":
        from .settings import LiveSettings

        return LiveSettings
    if name in ("SourceAdvanced", "subscribe"):
        from . import events

        return getattr(events, name)
    raise AttributeError(name)
