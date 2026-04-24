"""Streaming ingestion primitives.

- :mod:`havn.engine.streaming.webhook`     — webhook staging + flush worker.
- :mod:`havn.engine.streaming.cdc_logical` — Postgres logical-replication CDC.
- :mod:`havn.engine.streaming.maintenance` — DuckLake flush / compact / checkpoint.
- :mod:`havn.engine.streaming.api_poll`    — scheduled HTTP polling consumers.
"""

from __future__ import annotations

from havn.engine.streaming.api_poll import APIPollConsumer, PollResult
from havn.engine.streaming.webhook import (
    FlushWorker,
    WebhookStaging,
    append_event,
)

__all__ = [
    "APIPollConsumer",
    "FlushWorker",
    "PollResult",
    "WebhookStaging",
    "append_event",
]
