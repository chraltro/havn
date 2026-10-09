"""Editor intelligence endpoints: shadow bind and CTE enumeration.

``POST /api/bind`` resolves an unsaved buffer through the DuckDB binder and
returns diagnostics positioned in the file, plus the inferred output schema
and the schema of each upstream relation. The editor calls it on a debounce,
so everything here is read-permission and read-connection scoped.

The buffer is arbitrary SQL from anyone with read permission, so it goes
through :func:`validate_read_only_query` before it goes anywhere near a
binder. A buffer that fails the validator is never bound: the response is
HTTP 200 with ``ok: false`` and one ``source: "bind"`` error carrying the
validator's reason and a null line, which is the shape the editor already
renders as a whole-file diagnostic. Multi-statement buffers, mutations,
``COPY``/``ATTACH`` and the file-access functions are all rejected there,
and the bind pass itself (an isolated, locked-down in-memory shadow) is the
second line of defence rather than the only one.

``POST /api/sql/ctes`` enumerates the CTEs in a buffer and builds a runnable
preview query for each one by slicing the original text, so the preview keeps
the author's formatting instead of a sqlglot round trip.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from havn.server.deps import (
    _discover_models_cached,
    _get_project_dir,
    _require_permission,
)

logger = logging.getLogger("havn.server")

router = APIRouter()


# --- Pydantic models ---


class BindRequest(BaseModel):
    path: str | None = Field(None, max_length=1000)
    content: str = Field(..., max_length=1_000_000)


class CteRequest(BaseModel):
    content: str = Field(..., max_length=1_000_000)
    line: int | None = None


# --- Helpers ---


def _bind_connection():
    """A read-only connection for the bind request.

    Everything this route does with the warehouse is a catalog read: name
    validation, the base-table column types the shadow is seeded from, and the
    persisted column fallbacks. The bind pass itself runs in its own in-memory
    database, so a read-permission endpoint has no reason to hold a writable
    handle on the warehouse.
    """
    from havn.server.deps import _get_read_pool

    return _get_read_pool().connection()


def _known_tables(project_models) -> set[str]:
    """Names the buffer may legitimately reference besides the catalog.

    validate_models is called with the buffer as the only model, so without
    this every sibling model, seed and declared source reads as a missing
    table. Failures here are not worth failing a bind over.
    """
    names = {m.full_name for m in project_models}
    try:
        from havn.engine.seeds import discover_seeds

        for seed in discover_seeds(_get_project_dir() / "seeds"):
            names.add(seed["full_name"])
    except Exception:
        logger.debug("Could not list seeds for the bind request")
    try:
        from havn.server.deps import _get_config

        for source in _get_config().sources:
            for table in source.tables:
                names.add(f"{source.schema}.{table.name}")
    except Exception:
        logger.debug("Could not list sources for the bind request")
    return names


def _errors_payload(items) -> list[dict]:
    return [
        {
            "severity": severity,
            "message": message,
            "line": line,
            "col": col,
            "end_line": end_line,
            "end_col": end_col,
            "source": source,
        }
        for severity, message, line, col, end_line, end_col, source in items
    ]


# --- Bind endpoint ---


@router.post("/api/bind")
def bind_endpoint(request: Request, req: BindRequest) -> dict:
    """Shadow-bind an unsaved buffer and report diagnostics plus schemas."""
    _require_permission(request, "read")

    from havn.engine.transform.bind import (
        ancestor_closure,
        as_validation_message,
        bind_models,
        model_from_buffer,
        read_only_rejection,
    )
    from havn.engine.transform.columns import describe_object, load_model_columns

    project_dir = _get_project_dir()
    transform_dir = project_dir / "transform"

    if req.path:
        # By path parts, not by string prefix: `/proj-backup` starts with
        # `/proj`, so a prefix test let a sibling directory through.
        candidate = (project_dir / req.path).resolve()
        try:
            candidate.relative_to(project_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=400, detail="Path outside project directory")
    try:
        buffer_model = model_from_buffer(
            req.content, path=req.path, transform_dir=transform_dir
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # The buffer is caller-supplied SQL on a read-permission endpoint, so it
    # is validated before it reaches any binder and is never executed when it
    # fails. The answer is 200 with ok: false and one bind-source error, the
    # shape the editor already renders as a whole-file diagnostic.
    rejection = read_only_rejection(req.content)
    if rejection is not None:
        return {
            "model": buffer_model.full_name if req.path else None,
            "ok": False,
            "errors": _errors_payload(
                [("error", rejection, None, None, None, None, "bind")]
            ),
            "columns": [],
            "upstream": {},
            "duration_ms": 0,
        }

    rows: list[tuple] = []

    # Name-level validation of the buffer alone, so a typo'd table name is
    # reported even when the bind pass is unavailable.
    project_models = [
        m for m in _discover_models_cached(transform_dir)
        if m.full_name != buffer_model.full_name
    ]
    chain = ancestor_closure(
        project_models + [buffer_model], [buffer_model.full_name]
    )

    with _bind_connection() as cur:
        from havn.engine.transform import validate_models

        try:
            name_errors = validate_models(
                cur, [buffer_model], known_tables=_known_tables(project_models)
            )
        except Exception as e:
            logger.debug("Buffer validation failed: %s", e)
            name_errors = []
        for err in name_errors:
            rows.append(
                (err.severity, err.message, err.line, None, None, None, "validate")
            )

        result = bind_models(cur, chain, project_dir=project_dir)
        for warning in result.warnings:
            rows.append(("warning", warning.message, None, None, None, None, "bind"))
        for err in result.errors.get(buffer_model.full_name, []):
            rows.append((
                "error",
                as_validation_message(err),
                err.line,
                err.col,
                err.end_line,
                err.end_col,
                "bind",
            ))
        # An upstream that failed to bind is the buffer's problem too, but it
        # belongs to another file, so it is a warning without a position here.
        for name, errs in result.errors.items():
            if name == buffer_model.full_name:
                continue
            for err in errs:
                if err.kind == "upstream":
                    continue
                rows.append((
                    "warning",
                    f"{name}: {as_validation_message(err)}",
                    None, None, None, None,
                    "bind",
                ))

        columns = [
            {"name": n, "type": t}
            for n, t in result.schemas.get(buffer_model.full_name, [])
        ]
        upstream: dict[str, list[dict]] = {}
        for dep in buffer_model.depends_on:
            key = dep.lower()
            bound = result.schemas.get(dep)
            if bound:
                upstream[key] = [{"name": n, "type": t} for n, t in bound]
                continue
            # Not in the bound chain (a landing table, a seed, or a model that
            # failed): fall back to what the last successful build recorded,
            # then to whatever the catalog holds right now.
            persisted = load_model_columns(cur, dep)
            if persisted:
                upstream[key] = persisted
                continue
            described = describe_object(cur, dep)
            if described:
                upstream[key] = [{"name": n, "type": t} for n, t in described]

    errors = _errors_payload(rows)
    return {
        # A pathless scratch buffer has no model identity to report.
        "model": buffer_model.full_name if req.path else None,
        "ok": not any(e["severity"] == "error" for e in errors),
        "errors": errors,
        "columns": columns,
        "upstream": upstream,
        "duration_ms": result.duration_ms,
    }


# --- Persisted column schemas ---


@router.get("/api/models/{model_name:path}/columns")
def model_columns_endpoint(request: Request, model_name: str) -> dict:
    """Columns and types recorded for a model at its last successful build."""
    _require_permission(request, "read")
    from havn.engine.transform.columns import load_model_columns
    from havn.server.deps import _get_read_pool

    with _get_read_pool().connection() as conn:
        columns = load_model_columns(conn, model_name)
    return {"model": model_name, "columns": columns}


# --- CTE enumeration ---


@router.post("/api/sql/ctes")
def list_ctes_endpoint(request: Request, req: CteRequest) -> dict:
    """Enumerate the CTEs in a buffer and build a preview query for each."""
    _require_permission(request, "read")
    from havn.engine.sql_analysis import strip_config_comments
    from havn.engine.transform.ctes import CteParseError, enumerate_ctes

    # Directive lines are blanked in place, so line numbers still match the
    # buffer the editor is showing. Character offsets do not (a blanked line
    # is shorter than the original); this route only uses lines. Anything that
    # needs offsets must go through the per-line map in engine/rename.py.
    query = strip_config_comments(req.content)
    try:
        ctes, active = enumerate_ctes(query, line=req.line)
    except CteParseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "ctes": [
            {
                "name": c.name,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "preview_sql": c.preview_sql,
            }
            for c in ctes
        ],
        "active": active,
    }
