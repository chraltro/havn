"""Streaming ingestion primitives.

- :mod:`havn.engine.streaming.webhook`     — webhook staging + flush worker.
- :mod:`havn.engine.streaming.cdc_logical` — Postgres logical-replication CDC.
- :mod:`havn.engine.streaming.maintenance` — DuckLake flush / compact / checkpoint.
"""

from havn.engine.streaming.webhook import (
    FlushWorker,
    WebhookStaging,
    append_event,
)

__all__ = ["FlushWorker", "WebhookStaging", "append_event"]
