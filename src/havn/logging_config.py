"""Structured logging configuration for havn.

Sets up structlog as the root logger. JSON output when
``HAVN_LOG_FORMAT=json`` is set (production / cloud), otherwise a
human-friendly console renderer with colors.

Existing ``logging.getLogger(name)`` callsites across the codebase keep
working — structlog is wired via ``logging.basicConfig`` so stdlib
loggers inherit the same processor chain.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog

_CONFIGURED = False


def configure_logging(level: str | int | None = None, force: bool = False) -> None:
    """Configure structlog + stdlib logging. Idempotent unless ``force=True``."""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    log_level = _resolve_level(level)
    use_json = os.environ.get("HAVN_LOG_FORMAT", "").lower() == "json"

    # Shared processors that run regardless of output format.
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    if use_json:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    # Structlog-native loggers.
    structlog.configure(
        processors=shared_processors
        + [
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging through structlog so existing ``logger = logging.getLogger(...)``
    # callsites emit the same structured output.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    # Quiet a few noisy third-party loggers at WARNING.
    for name in ("urllib3", "httpx", "httpcore", "watchdog", "uvicorn.access"):
        logging.getLogger(name).setLevel(max(log_level, logging.WARNING))

    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog-bound logger. Lazily configures on first use."""
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name) if name else structlog.get_logger()


def _resolve_level(level: str | int | None) -> int:
    if level is None:
        level = os.environ.get("HAVN_LOG_LEVEL", "INFO")
    if isinstance(level, int):
        return level
    return getattr(logging, str(level).upper(), logging.INFO)
