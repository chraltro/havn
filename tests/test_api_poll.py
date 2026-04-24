"""Tests for the API poll consumer (Phase 4c).

All tests use:
- A real DuckDB database (tmp_path fixture)
- A mini HTTP server (stdlib http.server) in a background thread to stub the API
- No external services
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import duckdb
import pytest

from havn.engine.cdc import ensure_cdc_table, get_watermark
from havn.engine.streaming.api_poll import (
    APIPollConsumer,
    PollResult,
    _append_watermark_param,
    _extract_json_path,
    _max_field_value,
)


# ---------------------------------------------------------------------------
# Mini stub HTTP server
# ---------------------------------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    """Minimal request handler driven by a shared response queue."""

    def log_message(self, *args: Any) -> None:
        pass  # Suppress access log noise

    def do_GET(self) -> None:  # noqa: N802
        server: _StubServer = self.server  # type: ignore[assignment]
        with server.lock:
            if server.responses:
                code, body, headers = server.responses.pop(0)
            else:
                code, body, headers = 200, b"[]", {}
            server.requests.append(self.path)

        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


class _StubServer(HTTPServer):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.responses: list[tuple[int, bytes, dict]] = []
        self.requests: list[str] = []
        self.lock = threading.Lock()

    def enqueue(self, body: Any, *, code: int = 200, headers: dict | None = None) -> None:
        self.responses.append((code, json.dumps(body).encode(), headers or {}))


@pytest.fixture
def stub_server():
    server = _StubServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def _server_url(server: _StubServer, path: str = "/data") -> str:
    port = server.server_address[1]
    return f"http://127.0.0.1:{port}{path}"


# ---------------------------------------------------------------------------
# Project fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "project.yml").write_text(
        "name: test\ndatabase:\n  path: warehouse.duckdb\n"
    )
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
    conn.close()
    return tmp_path


# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------


def test_extract_json_path_root_list():
    rows = _extract_json_path([{"id": 1}, {"id": 2}], "$")
    assert rows == [{"id": 1}, {"id": 2}]


def test_extract_json_path_nested():
    data = {"results": {"items": [{"id": 3}]}}
    rows = _extract_json_path(data, "results.items")
    assert rows == [{"id": 3}]


def test_extract_json_path_missing_key():
    rows = _extract_json_path({"other": []}, "results")
    assert rows == []


def test_max_field_value_strings():
    rows = [
        {"updated_at": "2024-01-03"},
        {"updated_at": "2024-01-01"},
        {"updated_at": "2024-01-02"},
    ]
    assert _max_field_value(rows, "updated_at") == "2024-01-03"


def test_max_field_value_missing_field():
    rows = [{"id": 1}, {"id": 2}]
    assert _max_field_value(rows, "updated_at") is None


def test_append_watermark_param_no_existing_qs():
    url = _append_watermark_param("http://api.example.com/data", "since", "2024-01-01")
    assert "since=2024-01-01" in url


def test_append_watermark_param_existing_qs():
    url = _append_watermark_param(
        "http://api.example.com/data?limit=100", "since", "2024-01-01"
    )
    assert "since=2024-01-01" in url
    assert "limit=100" in url


def test_append_watermark_param_replaces_existing():
    url = _append_watermark_param(
        "http://api.example.com/data?since=old", "since", "new"
    )
    assert "since=new" in url
    assert "since=old" not in url


# ---------------------------------------------------------------------------
# poll_once integration tests
# ---------------------------------------------------------------------------


def test_poll_once_inserts_rows(project: Path, stub_server: _StubServer) -> None:
    stub_server.enqueue([{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}])

    config = {"url": _server_url(stub_server), "json_path": "$"}
    consumer = APIPollConsumer("my_api", config, project)
    result = consumer.poll_once()

    assert result.error is None
    assert result.rows_inserted == 2

    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    count = conn.execute("SELECT COUNT(*) FROM landing.my_api").fetchone()[0]
    conn.close()
    assert count == 2


def test_poll_once_watermark_updated(project: Path, stub_server: _StubServer) -> None:
    stub_server.enqueue([
        {"id": 1, "updated_at": "2024-01-01"},
        {"id": 2, "updated_at": "2024-01-03"},
        {"id": 3, "updated_at": "2024-01-02"},
    ])

    config = {
        "url": _server_url(stub_server),
        "json_path": "$",
        "watermark_field": "updated_at",
    }
    consumer = APIPollConsumer("wm_api", config, project)
    result = consumer.poll_once()

    assert result.error is None
    assert result.new_watermark == "2024-01-03"

    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    wm = get_watermark(conn, "wm_api", "wm_api")
    conn.close()
    assert wm == "2024-01-03"


def test_poll_once_sends_watermark_param(project: Path, stub_server: _StubServer) -> None:
    # First poll: set the watermark
    stub_server.enqueue([{"id": 1, "updated_at": "2024-06-01"}])
    config = {
        "url": _server_url(stub_server),
        "json_path": "$",
        "watermark_field": "updated_at",
        "watermark_param": "since",
    }
    consumer = APIPollConsumer("inc_api", config, project)
    consumer.poll_once()

    # Second poll: should include since=<watermark> in request URL
    stub_server.enqueue([{"id": 2, "updated_at": "2024-06-05"}])
    consumer.poll_once()

    assert len(stub_server.requests) >= 2
    second_request = stub_server.requests[1]
    assert "since=2024-06-01" in second_request


def test_poll_once_full_refresh_no_watermark(
    project: Path, stub_server: _StubServer
) -> None:
    stub_server.enqueue({"records": [{"id": 10}]})

    config = {"url": _server_url(stub_server), "json_path": "records"}
    consumer = APIPollConsumer("full_refresh_api", config, project)
    result = consumer.poll_once()

    assert result.error is None
    assert result.rows_inserted == 1
    assert result.new_watermark is None


def test_poll_once_pagination(project: Path, stub_server: _StubServer) -> None:
    port = stub_server.server_address[1]
    page2_url = f"http://127.0.0.1:{port}/page2"

    stub_server.enqueue(
        {"items": [{"id": 1}], "next": page2_url}
    )
    stub_server.enqueue(
        {"items": [{"id": 2}], "next": None}
    )

    config = {
        "url": _server_url(stub_server),
        "json_path": "items",
        "pagination_key": "next",
    }
    consumer = APIPollConsumer("paged_api", config, project)
    result = consumer.poll_once()

    assert result.error is None
    assert result.rows_inserted == 2

    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    count = conn.execute("SELECT COUNT(*) FROM landing.paged_api").fetchone()[0]
    conn.close()
    assert count == 2


def test_poll_once_http_error_retries(project: Path, stub_server: _StubServer) -> None:
    # Two server errors then success
    stub_server.enqueue([], code=500)
    stub_server.enqueue([], code=500)
    stub_server.enqueue([{"id": 99}])

    config = {"url": _server_url(stub_server), "json_path": "$"}
    consumer = APIPollConsumer("retry_api", config, project)

    # Patch sleep to not actually wait during tests
    import havn.engine.streaming.api_poll as module
    original_sleep = time.sleep

    slept: list[float] = []

    def _fast_sleep(s: float) -> None:
        slept.append(s)

    module.time.sleep = _fast_sleep  # type: ignore[assignment]
    try:
        result = consumer.poll_once()
    finally:
        module.time.sleep = original_sleep  # type: ignore[assignment]

    assert result.error is None
    assert result.rows_inserted == 1
    assert len(slept) == 2  # Slept twice before the successful attempt


def test_poll_once_non_retryable_http_error(
    project: Path, stub_server: _StubServer
) -> None:
    stub_server.enqueue([], code=403)

    config = {"url": _server_url(stub_server), "json_path": "$"}
    consumer = APIPollConsumer("auth_api", config, project)
    result = consumer.poll_once()

    assert result.error is not None
    assert result.rows_inserted == 0


def test_poll_once_empty_response(project: Path, stub_server: _StubServer) -> None:
    stub_server.enqueue([])

    config = {"url": _server_url(stub_server), "json_path": "$"}
    consumer = APIPollConsumer("empty_api", config, project)
    result = consumer.poll_once()

    assert result.error is None
    assert result.rows_inserted == 0


# ---------------------------------------------------------------------------
# run() loop test
# ---------------------------------------------------------------------------


def test_run_loop_stops_on_event(project: Path, stub_server: _StubServer) -> None:
    # Enqueue enough responses for several polls
    for _ in range(5):
        stub_server.enqueue([{"id": 1}])

    config = {"url": _server_url(stub_server), "json_path": "$"}
    consumer = APIPollConsumer("loop_api", config, project)
    stop_event = threading.Event()

    counts: list[int] = []
    original_poll = consumer.poll_once

    def _counting_poll() -> PollResult:
        result = original_poll()
        counts.append(result.rows_inserted)
        if len(counts) >= 2:
            stop_event.set()
        return result

    consumer.poll_once = _counting_poll  # type: ignore[method-assign]

    thread = threading.Thread(
        target=consumer.run, args=(1, stop_event), daemon=True
    )
    thread.start()
    thread.join(timeout=10)

    assert not thread.is_alive(), "run() did not stop when stop_event was set"
    assert len(counts) >= 2


# ---------------------------------------------------------------------------
# CLI end-to-end test: havn poll once
# ---------------------------------------------------------------------------


def test_cli_poll_once(
    project: Path, stub_server: _StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from havn.cli import app

    stub_server.enqueue([{"id": 1, "val": "x"}])

    server_url = _server_url(stub_server)
    # project.yml connections: type + params dict (havn ConnectionConfig format)
    (project / "project.yml").write_text(
        f'name: test\ndatabase:\n  path: warehouse.duckdb\n'
        f'connections:\n  stub_conn:\n    type: rest_api\n'
        f'    url: {server_url}\n    json_path: "$"\n'
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["poll", "once", "--connector", "stub_conn", "--project", str(project)],
    )

    assert result.exit_code == 0, result.output
    assert "rows inserted: 1" in result.output

    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    count = conn.execute("SELECT COUNT(*) FROM landing.stub_conn").fetchone()[0]
    conn.close()
    assert count == 1
