"""havn - Self-hosted data platform. Data in safe waters."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

# The user-facing decorators (``from havn import macro, table_macro, model``)
# and ``__version__`` are resolved on first access (PEP 562) rather than at
# import time: the macros module pulls in duckdb and the version lookup pulls
# in importlib.metadata, together most of a cold ``import havn``. Every CLI
# invocation imports this package, so that cost was paid even by ``--help``.
_LAZY_ATTRS = {
    "macro": "havn.engine.macros",
    "table_macro": "havn.engine.macros",
    "model": "havn.engine.model_decorator",
}

__all__ = ["macro", "table_macro", "model", "setup_logging"]


def _read_version() -> str:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version

    try:
        return _pkg_version("havn")
    except PackageNotFoundError:
        # Editable install before metadata is generated, or running from a
        # source checkout without `pip install -e .` first.
        return "0.0.0+unknown"


def __getattr__(name: str) -> object:
    if name == "__version__":
        value: object = _read_version()
    elif name in _LAZY_ATTRS:
        import importlib

        value = getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    # Cache on the module so __getattr__ runs once per name.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_ATTRS) | {"__version__"})


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
