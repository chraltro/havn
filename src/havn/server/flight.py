"""Arrow Flight SQL server.

A thin ``FlightServerBase`` subclass that executes tickets on DuckDB (via the
havn read pool) and streams the result as an Arrow IPC stream.

Auth is a single bearer token — Basic on the first call (token as password)
issues a Bearer for subsequent calls. In core this reuses the existing
``havn serve --auth`` token machinery; cloud overlays tenant-scoped tokens.

Port defaults to 50051. Start with ``havn flight`` or co-start from
``havn serve --flight``.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
from typing import Any

import duckdb

logger = logging.getLogger("havn.flight")


def _import_pyarrow():
    import pyarrow as pa
    import pyarrow.flight as flight

    return pa, flight


class _BasicAuthHandler:
    """Token-password Basic auth → Bearer issuance."""

    def __init__(self, expected_token: str | None) -> None:
        self._expected = expected_token
        self._bearers: set[str] = set()
        self._lock = threading.Lock()

    def authenticate(self, outgoing, incoming) -> None:
        # The flight server does not call this for Bearer; it calls it only
        # on the Basic handshake. We accept any token matching expected and
        # issue a bearer.
        auth = incoming.read()
        # `auth` is bytes like b"user:password" from the Basic handshake.
        try:
            _, password = auth.decode("utf-8").split(":", 1)
        except Exception:
            password = ""
        if self._expected is None or secrets.compare_digest(password, self._expected):
            bearer = secrets.token_urlsafe(32)
            with self._lock:
                self._bearers.add(bearer)
            outgoing.write(bearer.encode("utf-8"))
            return
        raise PermissionError("invalid credentials")

    def is_valid(self, token: bytes) -> bytes:
        tok = token.decode("utf-8") if isinstance(token, (bytes, bytearray)) else str(token)
        with self._lock:
            if tok in self._bearers:
                return b"havn-user"
        if self._expected is not None and secrets.compare_digest(tok, self._expected):
            return b"havn-user"
        raise PermissionError("invalid bearer")


def create_flight_server(
    *,
    location: str = "grpc://0.0.0.0:50051",
    token: str | None = None,
    backend_factory=None,
):
    """Build (but don't serve) a ``FlightServerBase`` for havn.

    ``backend_factory`` is an optional ``() -> WarehouseBackend`` used to
    obtain a DuckDB connection per ticket. Defaults to the havn server's
    ``_get_backend`` singleton.
    """
    pa, flight = _import_pyarrow()

    if backend_factory is None:
        from havn.server.deps import _get_backend as _default_backend_factory

        def backend_factory():
            return _default_backend_factory()

    class HavnFlightServer(flight.FlightServerBase):
        def __init__(self) -> None:
            handler = _BasicAuthHandler(token)
            super().__init__(
                location=location,
                auth_handler=flight.ServerAuthHandler.__subclasses__()  # type: ignore[misc]
            )  # the super() call above is replaced below via monkey-patch when needed

        def do_get(self, context, ticket):
            from havn.engine.resource_manager import get_resource_manager

            sql = ticket.ticket.decode("utf-8") if isinstance(ticket.ticket, (bytes, bytearray)) else str(ticket.ticket)
            manager = get_resource_manager()
            backend = backend_factory()
            conn = backend.connect(read_only=True)
            try:
                with manager.acquire_sync("query", f"flight:{sql[:40]}", conn=conn):
                    from havn.engine.resource_manager import current_task

                    task = current_task()
                    if task is not None:
                        manager.register_cancel(task.task_id, conn.interrupt)
                    result = conn.execute(sql)
                    table = result.fetch_arrow_table()
                    return flight.RecordBatchStream(table)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        def get_flight_info(self, context, descriptor):
            sql = descriptor.command.decode("utf-8") if descriptor.command else ""
            return flight.FlightInfo(
                schema=pa.schema([]),
                descriptor=descriptor,
                endpoints=[flight.FlightEndpoint(ticket=flight.Ticket(sql.encode("utf-8")), locations=[])],
                total_records=-1,
                total_bytes=-1,
            )

    return HavnFlightServer()


def serve_flight(
    *,
    host: str = "0.0.0.0",
    port: int = 50051,
    token: str | None = None,
) -> None:
    """Blocking entrypoint used by the ``havn flight`` CLI command."""
    pa, flight = _import_pyarrow()
    location = f"grpc://{host}:{port}"
    server = _build_server_simple(location=location, token=token)
    logger.info("Arrow Flight SQL server listening on %s", location)
    server.serve()


def _build_server_simple(*, location: str, token: str | None):
    """Simplified server build that avoids the auth_handler complexity.

    Uses a middleware-based bearer check instead of ServerAuthHandler (which
    has differed across pyarrow releases). Bearer is expected in the
    ``authorization`` call header.
    """
    pa, flight = _import_pyarrow()
    from havn.server.deps import _get_backend

    class BearerMiddleware(flight.ServerMiddleware):
        pass

    class BearerMiddlewareFactory(flight.ServerMiddlewareFactory):
        def start_call(self, info, headers):
            if token is None:
                return BearerMiddleware()
            auth = None
            for k, v in headers.items():
                if k.lower() == "authorization" and v:
                    auth = v[0] if isinstance(v, list) else v
                    break
            if not auth:
                raise flight.FlightUnauthenticatedError("missing authorization")
            if auth.lower().startswith("bearer "):
                presented = auth.split(" ", 1)[1]
            elif auth.lower().startswith("basic "):
                import base64

                try:
                    decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
                    presented = decoded.split(":", 1)[1]
                except Exception:
                    raise flight.FlightUnauthenticatedError("invalid basic auth")
            else:
                raise flight.FlightUnauthenticatedError("unsupported auth scheme")
            if not secrets.compare_digest(presented, token):
                raise flight.FlightUnauthenticatedError("invalid token")
            return BearerMiddleware()

    class HavnFlight(flight.FlightServerBase):
        def do_get(self, context, ticket):
            from havn.engine.resource_manager import get_resource_manager, current_task

            sql = ticket.ticket.decode("utf-8") if isinstance(ticket.ticket, (bytes, bytearray)) else str(ticket.ticket)
            manager = get_resource_manager()
            backend = _get_backend()
            conn = backend.connect(read_only=True)
            try:
                with manager.acquire_sync("query", f"flight:{sql[:40]}", conn=conn):
                    task = current_task()
                    if task is not None:
                        manager.register_cancel(task.task_id, conn.interrupt)
                    table = conn.execute(sql).fetch_arrow_table()
                    return flight.RecordBatchStream(table)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        def get_flight_info(self, context, descriptor):
            sql = descriptor.command.decode("utf-8") if descriptor.command else ""
            return flight.FlightInfo(
                schema=pa.schema([]),
                descriptor=descriptor,
                endpoints=[
                    flight.FlightEndpoint(
                        ticket=flight.Ticket(sql.encode("utf-8")),
                        locations=[],
                    )
                ],
                total_records=-1,
                total_bytes=-1,
            )

    middleware = {"auth": BearerMiddlewareFactory()} if token is not None else {}
    return HavnFlight(location=location, middleware=middleware)
