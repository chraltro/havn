"""Arrow Flight SQL server.

A ``FlightServerBase`` subclass that executes tickets on DuckDB via the havn
backend and streams the result as an Arrow IPC stream.

Auth is a single bearer token validated by a ``ServerMiddleware``. Basic and
Bearer schemes are both accepted on the ``authorization`` header; a token
of ``None`` disables auth (dev / local only).

Port defaults to 50051. Start with ``havn flight`` or co-start alongside
``havn serve``.
"""

from __future__ import annotations

import base64
import logging
import secrets
from typing import Callable

logger = logging.getLogger("havn.flight")


def _import_pyarrow():
    import pyarrow as pa
    import pyarrow.flight as flight

    return pa, flight


def _build_bearer_middleware_factory(token: str | None):
    _, flight = _import_pyarrow()

    class BearerMiddleware(flight.ServerMiddleware):
        """No-op middleware — authentication is done in the factory."""

    class BearerMiddlewareFactory(flight.ServerMiddlewareFactory):
        def start_call(self, info, headers):
            if token is None:
                return BearerMiddleware()

            auth_header = None
            for k, v in headers.items():
                if k.lower() == "authorization" and v:
                    auth_header = v[0] if isinstance(v, list) else v
                    break
            if not auth_header:
                raise flight.FlightUnauthenticatedError("missing authorization")

            lowered = auth_header.lower()
            if lowered.startswith("bearer "):
                presented = auth_header.split(" ", 1)[1]
            elif lowered.startswith("basic "):
                try:
                    decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
                    presented = decoded.split(":", 1)[1]
                except Exception as e:
                    raise flight.FlightUnauthenticatedError("invalid basic auth") from e
            else:
                raise flight.FlightUnauthenticatedError("unsupported auth scheme")

            if not secrets.compare_digest(presented, token):
                raise flight.FlightUnauthenticatedError("invalid token")
            return BearerMiddleware()

    return BearerMiddlewareFactory()


def create_flight_server(
    *,
    location: str = "grpc://0.0.0.0:50051",
    token: str | None = None,
    backend_factory: Callable | None = None,
):
    """Build (but don't serve) a Flight SQL server.

    ``backend_factory`` returns a WarehouseBackend; defaults to the havn
    server's configured backend. Callers in tests can inject a fake backend.
    """
    pa, flight = _import_pyarrow()

    # Hoisted from the `backend_factory is None` branch so that callers who
    # inject their own factory (notably the test suite) still see `cursor_for`
    # in the closure that ``do_get`` reads from.
    from havn.engine.write_queue import cursor_for  # noqa: F401

    if backend_factory is None:
        from havn.server.deps import _get_backend

        def backend_factory():
            return _get_backend()

    class HavnFlight(flight.FlightServerBase):
        def do_get(self, context, ticket):  # noqa: D401
            from havn.engine.resource_manager import current_task, get_resource_manager

            sql = (
                ticket.ticket.decode("utf-8")
                if isinstance(ticket.ticket, (bytes, bytearray))
                else str(ticket.ticket)
            )
            manager = get_resource_manager()
            backend = backend_factory()
            conn = backend.connect(read_only=True)
            try:
                with manager.acquire_sync("query", f"flight:{sql[:40]}", conn=conn):
                    task = current_task()
                    if task is not None:
                        manager.register_cancel(task.task_id, conn.interrupt)
                    cur = cursor_for(conn)
                    try:
                        cur.execute(sql)
                        table = (
                            cur.to_arrow_table()
                            if hasattr(cur, "to_arrow_table")
                            else cur.fetch_arrow_table()
                        )
                    finally:
                        try:
                            cur.close()
                        except Exception:
                            pass
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

    middleware = (
        {"auth": _build_bearer_middleware_factory(token)} if token is not None else {}
    )
    return HavnFlight(location=location, middleware=middleware)


def serve_flight(
    *,
    host: str = "0.0.0.0",
    port: int = 50051,
    token: str | None = None,
) -> None:
    """Blocking entrypoint used by the ``havn flight`` CLI command."""
    server = create_flight_server(
        location=f"grpc://{host}:{port}",
        token=token,
    )
    logger.info("Arrow Flight SQL server listening on grpc://%s:%s", host, port)
    server.serve()
