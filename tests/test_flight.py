"""Tests for the Arrow Flight SQL server."""

from __future__ import annotations

import threading
import time

import pytest


pa = pytest.importorskip("pyarrow")
flight = pytest.importorskip("pyarrow.flight")


class _StubBackend:
    """Tiny backend that hands out a fresh in-memory DuckDB per call."""

    name = "duckdb"

    def connect(self, read_only: bool = False):
        import duckdb

        c = duckdb.connect(":memory:")
        c.execute("CREATE TABLE t(n INT); INSERT INTO t VALUES (1),(2),(3)")
        return c


@pytest.fixture()
def running_server():
    from havn.engine.resource_manager import reset_resource_manager
    from havn.server.flight import create_flight_server

    reset_resource_manager()
    server = create_flight_server(
        location="grpc://127.0.0.1:0",
        token=None,
        backend_factory=lambda: _StubBackend(),
    )
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    # Wait briefly for the server to bind.
    time.sleep(0.2)
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_do_get_returns_arrow_table(running_server):
    port = running_server.port
    client = flight.FlightClient(f"grpc://127.0.0.1:{port}")
    ticket = flight.Ticket(b"SELECT * FROM t ORDER BY n")
    reader = client.do_get(ticket)
    table = reader.read_all()
    assert table.num_rows == 3
    assert table.column("n").to_pylist() == [1, 2, 3]


def test_unauthenticated_rejected_when_token_set():
    from havn.engine.resource_manager import reset_resource_manager
    from havn.server.flight import create_flight_server

    reset_resource_manager()
    server = create_flight_server(
        location="grpc://127.0.0.1:0",
        token="s3cret",
        backend_factory=lambda: _StubBackend(),
    )
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    time.sleep(0.2)
    try:
        port = server.port
        client = flight.FlightClient(f"grpc://127.0.0.1:{port}")
        with pytest.raises(flight.FlightUnauthenticatedError):
            client.do_get(flight.Ticket(b"SELECT 1")).read_all()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_authenticated_with_bearer(running_server):
    """With token=None the server accepts anything; confirm shape of auth headers work."""
    port = running_server.port
    client = flight.FlightClient(f"grpc://127.0.0.1:{port}")
    options = flight.FlightCallOptions(headers=[(b"authorization", b"Bearer anything")])
    reader = client.do_get(flight.Ticket(b"SELECT 1 AS n"), options)
    table = reader.read_all()
    assert table.num_rows == 1
