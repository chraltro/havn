"""havn - Self-hosted data platform. Data in safe waters."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

__version__ = "0.2.5"

# Re-export decorators for user-facing macros
from havn.engine.macros import macro, table_macro  # noqa: F401


_TEXT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_TEXT_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Keys added by logging.LogRecord that we don't want in the JSON "extra" bag.
_STDLIB_LOGRECORD_KEYS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
})


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record.

    Standard fields: ts (ISO-8601 UTC), level, logger, message. Any keyword
    passed via ``logger.info("msg", extra={...})`` is merged into the top-level
    object so operators can filter on structured context (request_id, tenant,
    model, etc.) without parsing the message string.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STDLIB_LOGRECORD_KEYS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _resolve_format(explicit: str | None) -> str:
    """Pick the log format. Explicit arg wins, then HAVN_LOG_FORMAT, then text."""
    if explicit:
        return explicit.lower()
    return os.environ.get("HAVN_LOG_FORMAT", "text").lower()


def setup_logging(level: str = "INFO", format: str | None = None) -> None:
    """Configure logging for the havn platform.

    Set ``HAVN_LOG_FORMAT=json`` (or pass ``format="json"``) to emit one JSON
    object per record — useful when shipping logs to Loki/Elasticsearch or
    scraping with a log-aware collector. Default remains the colored/plain
    human-readable format.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    fmt_name = _resolve_format(format)

    if fmt_name == "json":
        formatter: logging.Formatter = _JsonFormatter()
    else:
        formatter = logging.Formatter(fmt=_TEXT_FORMAT, datefmt=_TEXT_DATEFMT)

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger("havn")
    root_logger.setLevel(log_level)
    if not root_logger.handlers:
        root_logger.addHandler(handler)
    else:
        # Swap formatter on existing handlers so repeated calls (tests, reload)
        # pick up format changes without duplicating handlers.
        for h in root_logger.handlers:
            h.setFormatter(formatter)
