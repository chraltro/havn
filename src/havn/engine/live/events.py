"""In-process notifications that a live source advanced.

Streaming ingest publishes a :class:`SourceAdvanced` after each commit; the
live runner subscribes and wakes up. The bus is a convenience for latency,
never the record of truth: the watermark itself lives in
``_havn.live_sources`` and the runner also re-reads it on a timer, so an
advance committed by another process, or one whose event was missed, is
still picked up.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

logger = logging.getLogger("havn.live")


@dataclass(frozen=True)
class SourceAdvanced:
    """``source`` now holds committed rows up to ``watermark``."""

    source: str
    watermark: int
    rows: int
    advanced_at: datetime
    kind: str = "source"  # "source" for landing tables, "model" for live models
    extra: dict = field(default_factory=dict, compare=False, hash=False)


Listener = Callable[[SourceAdvanced], None]

_listeners: list[Listener] = []
_lock = threading.Lock()


def subscribe(listener: Listener) -> Callable[[], None]:
    """Call ``listener`` on every advance; returns the unsubscribe function."""
    with _lock:
        _listeners.append(listener)

    def _unsubscribe() -> None:
        with _lock:
            try:
                _listeners.remove(listener)
            except ValueError:
                pass

    return _unsubscribe


def publish(event: SourceAdvanced) -> None:
    """Deliver ``event`` to every listener. A failing listener is logged, not raised."""
    with _lock:
        listeners = list(_listeners)
    for listener in listeners:
        try:
            listener(event)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("live event listener failed: %s", e)
