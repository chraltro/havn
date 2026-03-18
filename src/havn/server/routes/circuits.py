"""Circuit breaker status endpoint."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from havn.server.deps import DbConn, _require_permission, ensure_meta_table

logger = logging.getLogger("havn.server")

router = APIRouter()


@router.get("/api/circuits")
def get_circuits(request: Request, conn: DbConn) -> list[dict]:
    """Return the current state of all circuit breakers."""
    _require_permission(request, "read")
    ensure_meta_table(conn)

    from havn.engine.circuit_breaker import default_breaker

    # Try to load persisted state so the response includes historical data
    try:
        default_breaker.load_state(conn)
    except Exception:
        pass

    return default_breaker.get_all_states()


@router.post("/api/circuits/{name}/reset")
def reset_circuit(request: Request, name: str, conn: DbConn) -> dict:
    """Manually reset a circuit breaker to CLOSED."""
    _require_permission(request, "execute")

    from havn.engine.circuit_breaker import default_breaker

    default_breaker.reset(name)

    # Persist the reset
    try:
        default_breaker.save_state(conn)
    except Exception:
        pass

    return {"name": name, "state": "closed"}
