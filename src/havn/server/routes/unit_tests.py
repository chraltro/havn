"""Model unit tests: list what's declared, and run it."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

from havn.engine.unit_tests import (
    catalog_from_connection,
    load_unit_tests,
    run_unit_tests,
)
from havn.server.deps import (
    DbConnReadOnlyOptional,
    _get_project_dir,
    _require_permission,
)

logger = logging.getLogger("havn.server")

router = APIRouter()


class UnitTestRunRequest(BaseModel):
    model: str | None = None


@router.get("/api/unit-tests")
def list_unit_tests_endpoint(request: Request) -> dict:
    """List the unit tests declared in tests/unit/, plus any load errors."""
    _require_permission(request, "read")
    tests, errors = load_unit_tests(_get_project_dir())
    return {
        "tests": [t.to_dict() for t in tests],
        "errors": errors,
    }


@router.post("/api/unit-tests/run")
def run_unit_tests_endpoint(
    request: Request,
    req: UnitTestRunRequest,
    conn: DbConnReadOnlyOptional,
) -> dict:
    """Run the unit tests, optionally filtered to a single model.

    The run itself happens on a throwaway in-memory database. The warehouse
    connection is only read for column types, so the endpoint still works
    (and still means something) before anything has been built.
    """
    _require_permission(request, "execute")

    catalog: dict = {}
    if conn is not None:
        try:
            catalog = catalog_from_connection(conn)
        except Exception as e:  # a catalog is an optimisation, never a blocker
            logger.debug("Could not snapshot catalog for unit tests: %s", e)

    result = run_unit_tests(_get_project_dir(), model=req.model, catalog=catalog)
    return result.to_dict()
