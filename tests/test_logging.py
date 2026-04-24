"""Tests for havn.setup_logging — text and JSON formats."""

from __future__ import annotations

import io
import json
import logging
import os

import pytest

from havn import setup_logging


@pytest.fixture(autouse=True)
def _reset_havn_logger():
    logger = logging.getLogger("havn")
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    logger.handlers = []
    yield
    logger.handlers = saved_handlers
    logger.setLevel(saved_level)


def _capture(format: str | None = None) -> str:
    logger = logging.getLogger("havn.test")
    logger.handlers = []
    setup_logging(level="DEBUG", format=format)
    root = logging.getLogger("havn")
    buf = io.StringIO()
    # Swap the handler stream so assertions are deterministic.
    root.handlers[0].stream = buf
    logger.info("hello world", extra={"tenant": "acme", "rows": 42})
    return buf.getvalue().strip()


def test_text_format_default(monkeypatch):
    monkeypatch.delenv("HAVN_LOG_FORMAT", raising=False)
    out = _capture()
    assert "hello world" in out
    assert "[INFO]" in out
    assert "havn.test" in out


def test_json_format_explicit_arg():
    out = _capture(format="json")
    record = json.loads(out)
    assert record["message"] == "hello world"
    assert record["level"] == "INFO"
    assert record["logger"] == "havn.test"
    assert record["tenant"] == "acme"
    assert record["rows"] == 42
    assert record["ts"].endswith("Z")


def test_json_format_env_var(monkeypatch):
    monkeypatch.setenv("HAVN_LOG_FORMAT", "json")
    out = _capture()
    record = json.loads(out)
    assert record["message"] == "hello world"


def test_json_format_serializes_exception(monkeypatch):
    monkeypatch.setenv("HAVN_LOG_FORMAT", "json")
    logger = logging.getLogger("havn.test_exc")
    logger.handlers = []
    setup_logging(level="DEBUG")
    root = logging.getLogger("havn")
    buf = io.StringIO()
    root.handlers[0].stream = buf
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("failed thing")
    out = buf.getvalue().strip()
    record = json.loads(out)
    assert record["message"] == "failed thing"
    assert "ValueError" in record["exc_info"]


def test_json_format_handles_unserializable_extra():
    class Widget:
        def __repr__(self) -> str:
            return "<Widget>"

    logger = logging.getLogger("havn.test_unser")
    logger.handlers = []
    setup_logging(level="DEBUG", format="json")
    root = logging.getLogger("havn")
    buf = io.StringIO()
    root.handlers[0].stream = buf
    logger.info("thing", extra={"widget": Widget()})
    record = json.loads(buf.getvalue().strip())
    assert record["widget"] == "<Widget>"


def test_repeated_setup_swaps_formatter():
    setup_logging(level="INFO", format="text")
    setup_logging(level="INFO", format="json")
    root = logging.getLogger("havn")
    assert len(root.handlers) == 1
    buf = io.StringIO()
    root.handlers[0].stream = buf
    logging.getLogger("havn.test_swap").info("after swap")
    record = json.loads(buf.getvalue().strip())
    assert record["message"] == "after swap"
