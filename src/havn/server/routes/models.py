"""SQL model management, transform pipeline, lineage, and documentation endpoints."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, Field

from havn.server.deps import (
    DbConn,
    DbConnReadOnly,
    DbConnReadOnlyOptional,
    _discover_models_cached,
    _get_config,
    _get_project_dir,
    _require_permission,
    _serialize,
    build_dag,
    ensure_meta_table,
    run_transform,
)

logger = logging.getLogger("havn.server")

router = APIRouter()


# --- Pydantic models ---


class TransformRequest(BaseModel):
    # Graph selectors, same grammar as `havn transform`. A plain
    # ``schema.name`` still means exactly that model, which is what the UI's
    # "run model" button sends.
    targets: list[str] | None = Field(default=None, max_length=500)
    exclude: list[str] | None = Field(default=None, max_length=500)
    force: bool = False
    # Explicit event-time backfill range for microbatch models, mirroring
    # `havn transform --event-time-start/--event-time-end`. UTC.
    event_time_start: str | None = Field(default=None, max_length=64)
    event_time_end: str | None = Field(default=None, max_length=64)
    # Tri-state, like `havn transform --defer/--no-defer`: null defers when
    # the active environment declares a target, true insists on it, false
    # builds against this warehouse alone.
    defer: bool | None = None
    defer_snapshot: bool = False


class DiffRequest(BaseModel):
    targets: list[str] | None = Field(default=None)
    target_schema: str | None = Field(default=None, max_length=100)
    full: bool = False
    mode: str = Field(default="all", pattern=r"^(single|changed|all)$")


class CreateModelRequest(BaseModel):
    name: str = Field(
        ..., min_length=1, max_length=200, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$"
    )
    schema_name: str = Field(
        default="bronze", pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$"
    )
    materialized: str = Field(default="table", pattern=r"^(table|view)$")
    sql: str = Field(default="", max_length=1_000_000)


# --- Model list ---


@router.get("/api/models")
def list_models(
    request: Request,
    select: str | None = Query(
        default=None,
        max_length=1000,
        description=(
            "Graph selector filtering the list, e.g. 'tag:daily', '+gold.orders', "
            "'gold.fct_*'. Omit to list everything."
        ),
    ),
) -> list[dict]:
    """List SQL transformation models, optionally filtered by a graph selector."""
    _require_permission(request, "read")
    project_dir = _get_project_dir()
    transform_dir = project_dir / "transform"
    models = _discover_models_cached(transform_dir)
    if select:
        from havn.engine.selectors import select_models as _select_models

        # No connection is passed: ``state:`` needs write-ish access to the
        # warehouse and this is a read-only listing endpoint.
        chosen = set(
            _select_models([select], models, project_dir=project_dir).selected
        )
        models = [m for m in models if m.full_name in chosen]
    return [
        {
            "name": m.name,
            "schema": m.schema,
            "full_name": m.full_name,
            "materialized": m.materialized,
            "depends_on": m.depends_on,
            "path": str(m.path.relative_to(project_dir)),
            "content_hash": m.content_hash,
            "tags": list(getattr(m, "tags", []) or []),
        }
        for m in models
    ]


# --- Transform ---


@router.post("/api/transform")
def run_transform_endpoint(
    request: Request,
    conn: DbConn,
    req: TransformRequest = Body(default_factory=TransformRequest),
) -> dict:
    """Run the SQL transformation pipeline.

    ``targets`` and ``exclude`` are graph selectors (``+x``, ``x+``, ``@x``,
    ``gold.fct_*``, ``tag:daily``, ``state:modified`` and so on). Body is
    optional: POSTing with no body runs all models without --force.
    """
    _require_permission(request, "execute")
    logger.info(
        "Transform requested: targets=%s exclude=%s force=%s",
        req.targets, req.exclude, req.force,
    )
    from havn.engine.transform import BatchRange, parse_event_time

    batch_range = None
    if req.event_time_start or req.event_time_end:
        try:
            batch_range = BatchRange(
                start=parse_event_time(req.event_time_start, "event_time_start")
                if req.event_time_start else None,
                end=parse_event_time(req.event_time_end, "event_time_end")
                if req.event_time_end else None,
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    # Defer is resolved here rather than inside run_transform because it is a
    # project-config question (which environment, which file), and the
    # config is a server dependency.
    from havn.engine.defer import DeferError, resolve_defer

    try:
        defer_spec = resolve_defer(
            _get_config(), _get_project_dir(),
            enabled=req.defer, snapshot=req.defer_snapshot,
        )
    except DeferError as e:
        raise HTTPException(400, str(e)) from e

    # Resolve the selection here so a mistyped selector is an error rather
    # than an empty success. `run_transform` prints its warnings to the
    # server's console and returns {}, which reached the caller as 200 with
    # no results: indistinguishable from "everything was already up to date".
    warnings = _selection_warnings(conn, req)
    if warnings is not None and warnings["selected"] == 0:
        reasons = "; ".join(warnings["warnings"])
        raise HTTPException(
            400,
            f"No models matched: {reasons}" if reasons
            else "No models matched the given selectors.",
        )

    try:
        results = run_transform(
            conn,
            _get_project_dir() / "transform",
            targets=req.targets,
            exclude=req.exclude,
            force=req.force,
            batch_range=batch_range,
            defer=defer_spec,
        )
    except Exception as e:
        logger.exception("Transform failed")
        raise HTTPException(400, f"Transform failed: {e}")

    body: dict = {"results": results}
    if warnings and warnings["warnings"]:
        # Some selectors matched and some did not. The run went ahead, and the
        # caller is told which of its selectors did nothing.
        body["warnings"] = warnings["warnings"]
    return body


def _selection_warnings(conn, req: TransformRequest) -> dict | None:
    """Resolve this request's selectors, or None when it selects everything.

    Returns ``{"selected": <count>, "warnings": [...]}``. A failure to resolve
    is reported as no information rather than as an error: the run itself is
    the authority, and this check exists only to turn a silent empty result
    into a 400.
    """
    if not req.targets and not req.exclude:
        return None
    if req.targets and list(req.targets) == ["all"]:
        return None
    from havn.engine.selectors import select_models

    project_dir = _get_project_dir()
    try:
        models = _discover_models_cached(project_dir / "transform")
        selection = select_models(
            req.targets or ["all"],
            models,
            conn=conn,
            project_dir=project_dir,
            exclude=list(req.exclude) if req.exclude else None,
        )
    except Exception as e:
        logger.debug("Could not pre-resolve the transform selection: %s", e)
        return None
    return {
        "selected": len(selection.selected),
        "warnings": list(selection.warnings),
    }


# --- Diff ---


@router.post("/api/diff")
def run_diff_endpoint(request: Request, req: DiffRequest, conn: DbConn) -> list[dict]:
    """Diff models: compare SQL output against materialized tables."""
    _require_permission(request, "read")
    from havn.engine.diff import diff_models

    config = _get_config()
    try:
        ensure_meta_table(conn)
        results = diff_models(
            conn,
            _get_project_dir() / "transform",
            targets=req.targets,
            target_schema=req.target_schema,
            project_config=config,
            full=req.full,
            mode=req.mode,
        )
        return [
            {
                "model": r.model,
                "added": r.added,
                "removed": r.removed,
                "modified": r.modified,
                "total_before": r.total_before,
                "total_after": r.total_after,
                "is_new": r.is_new,
                "error": r.error,
                "schema_changes": [
                    {
                        "column": sc.column,
                        "change_type": sc.change_type,
                        "old_type": sc.old_type,
                        "new_type": sc.new_type,
                    }
                    for sc in r.schema_changes
                ],
                "sample_added": r.sample_added,
                "sample_removed": r.sample_removed,
                "sample_modified": r.sample_modified,
                "skipped": r.skipped,
            }
            for r in results
        ]
    except Exception as e:
        logger.exception("Diff failed")
        raise HTTPException(400, f"Diff failed: {e}")


# --- Lineage ---


@router.get("/api/lineage/{model_name}")
def get_lineage(
    request: Request, model_name: str, conn: DbConnReadOnlyOptional = None
) -> dict:
    """Get column-level lineage for a model (AST-based via sqlglot)."""
    _require_permission(request, "read")
    from havn.engine.transform import extract_column_lineage

    transform_dir = _get_project_dir() / "transform"
    models = _discover_models_cached(transform_dir)
    model_map = {m.full_name: m for m in models}

    target = model_map.get(model_name)
    if not target:
        matches = [m for m in models if m.name == model_name]
        if matches:
            target = matches[0]
        else:
            raise HTTPException(404, f"Model '{model_name}' not found")

    lineage = extract_column_lineage(target, conn)
    return {
        "model": target.full_name,
        "columns": lineage,
        "depends_on": target.depends_on,
    }


@router.get("/api/lineage")
def get_all_lineage(
    request: Request, conn: DbConnReadOnlyOptional = None
) -> list[dict]:
    """Get column-level lineage for all models."""
    _require_permission(request, "read")
    from havn.engine.sql_analysis import fetch_column_catalog
    from havn.engine.transform import extract_column_lineage

    transform_dir = _get_project_dir() / "transform"
    models = _discover_models_cached(transform_dir)

    # Read the column catalog once for the whole project rather than once per
    # model (and, before that, once per dependency of every model).
    catalog = fetch_column_catalog(conn) if conn else None

    results = []
    for model in models:
        lineage = extract_column_lineage(model, conn, column_catalog=catalog)
        results.append(
            {
                "model": model.full_name,
                "columns": lineage,
                "depends_on": model.depends_on,
            }
        )
    return results


# --- Impact analysis ---


@router.get("/api/impact/{model_name}")
def get_impact(
    request: Request,
    model_name: str,
    conn: DbConnReadOnlyOptional = None,
    column: str | None = None,
) -> dict:
    """Analyze downstream impact of changing a model or column."""
    _require_permission(request, "read")
    from havn.engine.transform import discover_all_models, impact_analysis

    models = discover_all_models(_get_project_dir())
    model_map = {m.full_name: m for m in models}

    if model_name not in model_map:
        matches = [m for m in models if m.name == model_name]
        if matches:
            model_name = matches[0].full_name
        else:
            raise HTTPException(404, f"Model '{model_name}' not found")

    return impact_analysis(models, model_name, column=column, conn=conn)


# --- Explain ---


@router.get("/api/models/{model_name:path}/explain")
def get_explain(
    request: Request,
    model_name: str,
    conn: DbConn,
    analyze: bool = False,
) -> dict:
    """Return DuckDB's parsed query plan for a model's SQL.

    With ``?analyze=true`` the query actually executes and per-node
    timings are surfaced (so this is treated as ``execute`` permission).
    """
    _require_permission(request, "execute" if analyze else "read")
    from havn.engine.explain import (
        enrich_plan_dict,
        explain_analyze_query,
        explain_query,
        plan_to_dict,
    )
    from havn.engine.transform import discover_all_models

    models = discover_all_models(_get_project_dir())

    target = next((m for m in models if m.full_name == model_name), None) or next(
        (m for m in models if m.name == model_name), None
    )
    if target is None:
        raise HTTPException(404, f"Model '{model_name}' not found")

    if analyze:
        plan, raw_text = explain_analyze_query(conn, target.query)
    else:
        plan, raw_text = explain_query(conn, target.query)

    plan_dict = plan_to_dict(plan)
    if analyze:
        plan_dict = enrich_plan_dict(plan_dict)
    plan_dict["_raw_text"] = raw_text
    plan_dict["model"] = target.full_name
    plan_dict["analyze"] = analyze
    return plan_dict


# --- Docs ---


@router.get("/api/docs/markdown")
def get_docs_markdown(request: Request, conn: DbConnReadOnly) -> dict:
    """Generate markdown documentation."""
    _require_permission(request, "read")
    from havn.engine.docs import generate_docs

    config = _get_config()
    md = generate_docs(
        conn,
        _get_project_dir() / "transform",
        sources=config.sources,
        exposures=config.exposures,
    )
    return {"markdown": md}


@router.get("/api/docs/structured")
def get_docs_structured(request: Request, conn: DbConnReadOnly) -> dict:
    """Generate structured documentation for two-pane UI."""
    _require_permission(request, "read")
    from havn.engine.docs import generate_structured_docs

    return generate_structured_docs(conn, _get_project_dir() / "transform")


# --- Model notebook view ---


@router.get("/api/models/{model_name:path}/notebook-view")
def get_model_notebook_view(
    request: Request, model_name: str, conn: DbConnReadOnlyOptional = None
) -> dict:
    """Get a notebook-style view for a SQL model."""
    _require_permission(request, "read")
    from havn.engine.transform import extract_column_lineage

    transform_dir = _get_project_dir() / "transform"
    models = _discover_models_cached(transform_dir)
    model_map = {m.full_name: m for m in models}

    target = model_map.get(model_name)
    if not target:
        matches = [m for m in models if m.name == model_name]
        if matches:
            target = matches[0]
        else:
            raise HTTPException(404, f"Model '{model_name}' not found")

    sql_source = target.path.read_text()
    rel_path = str(target.path.relative_to(_get_project_dir()))

    sample_data = None
    if conn:
        try:
            quoted = f'"{target.schema}"."{target.name}"'
            result = conn.execute(f"SELECT * FROM {quoted} LIMIT 50")
            columns = [desc[0] for desc in result.description]
            rows = result.fetchall()
            sample_data = {
                "columns": columns,
                "rows": [[_serialize(v) for v in row] for row in rows],
            }
        except Exception:
            sample_data = None

    lineage = None
    try:
        lineage = extract_column_lineage(target, conn)
    except Exception:
        pass

    upstream = target.depends_on
    downstream = [
        m.full_name for m in models if target.full_name in m.depends_on
    ]

    return {
        "model": target.full_name,
        "path": rel_path,
        "sql_source": sql_source,
        "materialized": target.materialized,
        "schema": target.schema,
        "sample_data": sample_data,
        "lineage": lineage,
        "upstream": upstream,
        "downstream": downstream,
    }


# --- Model workbench ---


@router.get("/api/models/workbench")
def get_model_workbench(
    request: Request,
    path: str = Query(..., min_length=1, max_length=1000),
    conn: DbConnReadOnlyOptional = None,
) -> dict:
    """Everything the editor's workbench shows for one SQL model, in one call.

    ``path`` is the project-relative file path the editor has open. It is only
    compared against discovered models, never read from disk directly.
    """
    _require_permission(request, "read")
    from havn.engine.transform import failing_rows_sql
    from havn.engine.transform.columns import load_model_columns

    project_dir = _get_project_dir()
    models = _discover_models_cached(project_dir / "transform")

    def rel(m) -> str | None:
        try:
            return str(m.path.relative_to(project_dir))
        except ValueError:
            return None

    wanted = path.replace("\\", "/").lstrip("./")
    target = next((m for m in models if rel(m) == wanted), None)
    if target is None:
        raise HTTPException(404, f"No model is defined in '{path}'")

    by_name = {m.full_name: m for m in models}
    children: dict[str, list[str]] = {}
    for m in models:
        for dep in m.depends_on:
            children.setdefault(dep, []).append(m.full_name)

    # Transitive downstream, breadth-first so nearer models come first.
    downstream_all: list[str] = []
    seen = {target.full_name}
    queue = list(children.get(target.full_name, []))
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        downstream_all.append(name)
        queue.extend(children.get(name, []))

    upstream = [
        {"name": d, "path": rel(by_name[d]) if d in by_name else None}
        for d in target.depends_on
    ]
    downstream = [
        {"name": d, "path": rel(by_name[d])}
        for d in sorted(set(children.get(target.full_name, [])))
    ]

    specs = list(target.assertion_specs) or [(e, "error") for e in target.assertions]
    if target.grain:
        specs.append((f"grain({', '.join(target.grain)})", "error"))

    columns: list[dict] = []
    state = {
        "built": False,
        "last_run_at": None,
        "row_count": None,
        "run_duration_ms": None,
        "changed_since_build": None,
    }
    latest: dict[str, dict] = {}
    runs: list[dict] = []

    if conn is not None:
        ensure_meta_table(conn)
        row = conn.execute(
            "SELECT content_hash, last_run_at, row_count, run_duration_ms "
            "FROM _havn.model_state WHERE model_path = ?",
            [target.full_name],
        ).fetchone()
        if row:
            state = {
                "built": True,
                "last_run_at": str(row[1]) if row[1] else None,
                "row_count": row[2],
                "run_duration_ms": row[3],
                "changed_since_build": row[0] != target.content_hash,
            }

        for r in conn.execute(
            """
            SELECT expression, passed, detail, checked_at
            FROM _havn.assertion_results
            WHERE model_path = ?
            QUALIFY row_number() OVER (PARTITION BY expression ORDER BY checked_at DESC) = 1
            """,
            [target.full_name],
        ).fetchall():
            latest[r[0]] = {
                "passed": r[1],
                "detail": r[2],
                "checked_at": str(r[3]) if r[3] else None,
            }

        runs = [
            {
                "status": r[0],
                "started_at": str(r[1]) if r[1] else None,
                "duration_ms": r[2],
                "rows_affected": r[3],
                "error": r[4],
            }
            for r in conn.execute(
                """
                SELECT status, started_at, duration_ms, rows_affected, error
                FROM _havn.run_log
                WHERE target = ? AND run_type = 'transform'
                ORDER BY started_at DESC
                LIMIT 10
                """,
                [target.full_name],
            ).fetchall()
        ]

        columns = load_model_columns(conn, target.full_name)
        if not columns:
            try:
                columns = [
                    {"name": r[0], "type": r[1]}
                    for r in conn.execute(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_catalog = current_database() "
                        "AND table_schema = ? AND table_name = ? "
                        "ORDER BY ordinal_position",
                        [target.schema, target.name],
                    ).fetchall()
                ]
            except Exception:
                columns = []

    # Documented columns the warehouse has not seen yet still get a row, so
    # the Columns tab reflects the file and not only the last build.
    known = {c["name"].lower() for c in columns}
    docs = {k.lower(): v for k, v in target.column_docs.items()}
    col_rows = [
        {"name": c["name"], "type": c["type"], "description": docs.get(c["name"].lower(), "")}
        for c in columns
    ]
    col_rows += [
        {"name": name, "type": None, "description": text}
        for name, text in target.column_docs.items()
        if name.lower() not in known
    ]

    checks = []
    for expr, severity in specs:
        result = latest.get(expr, {})
        checks.append(
            {
                "expression": expr,
                "severity": severity,
                "passed": result.get("passed"),
                "detail": result.get("detail"),
                "checked_at": result.get("checked_at"),
                "failing_sql": failing_rows_sql(target, expr),
            }
        )

    return {
        "model": target.full_name,
        "path": rel(target),
        "schema": target.schema,
        "name": target.name,
        "materialized": target.materialized,
        "description": target.description,
        "owner": target.owner,
        "tags": list(target.tags),
        "upstream": upstream,
        "downstream": downstream,
        "downstream_all": downstream_all,
        "columns": col_rows,
        "checks": checks,
        "runs": runs,
        "state": state,
    }


# --- Create new model ---


@router.post("/api/models/create")
def create_model_endpoint(request: Request, req: CreateModelRequest) -> dict:
    """Create a new SQL model file."""
    _require_permission(request, "write")
    project_dir = _get_project_dir()
    transform_dir = project_dir / "transform"
    schema_dir = transform_dir / req.schema_name

    if not schema_dir.resolve().is_relative_to(transform_dir.resolve()):
        raise HTTPException(400, "Invalid schema name")

    schema_dir.mkdir(parents=True, exist_ok=True)

    model_path = schema_dir / f"{req.name}.sql"
    if model_path.exists():
        raise HTTPException(
            409, f"Model '{req.schema_name}.{req.name}' already exists"
        )

    sql_content = (
        req.sql
        or f"@config materialized={req.materialized}, schema={req.schema_name}\n\nSELECT 1 AS placeholder\n"
    )
    # Prepend @config only if the user-supplied SQL doesn't already have a
    # config directive (either the new @config or the legacy "-- config:").
    has_config = sql_content.lstrip().startswith("@config") or sql_content.startswith("-- config:")
    if not has_config:
        sql_content = f"@config materialized={req.materialized}, schema={req.schema_name}\n\n{sql_content}"

    model_path.write_text(sql_content)

    return {
        "status": "created",
        "path": str(model_path.relative_to(project_dir)),
        "full_name": f"{req.schema_name}.{req.name}",
    }


# --- Pre-build validation ---


@router.post("/api/validate")
def run_validate(request: Request, conn_opt: DbConnReadOnlyOptional = None) -> dict:
    """Validate SQL models: dependencies, incremental config, schema conflicts.

    Returns validation errors and warnings as JSON. Does not execute any SQL.
    """
    _require_permission(request, "read")
    from havn.engine.seeds import discover_seeds
    from havn.engine.transform import discover_all_models, validate_models

    project_dir = _get_project_dir()
    models = discover_all_models(project_dir)
    config = _get_config()

    known_tables: set[str] = set()
    seeds = discover_seeds(project_dir / "seeds")
    for s in seeds:
        known_tables.add(s["full_name"])
    for src in config.sources:
        for t in src.tables:
            known_tables.add(f"{src.schema}.{t.name}")

    source_columns: dict[str, set[str]] = {}
    for src in config.sources:
        for t in src.tables:
            full = f"{src.schema}.{t.name}"
            source_columns[full] = {c.name for c in t.columns}

    landing_schemas: set[str] = {"landing"}
    for src in config.sources:
        landing_schemas.add(src.schema.lower())

    errors = validate_models(
        conn_opt, models,
        known_tables=known_tables,
        source_columns=source_columns,
        landing_schemas=landing_schemas,
    )

    error_count = sum(1 for e in errors if e.severity == "error")

    return {
        "models_checked": len(models),
        "errors": [
            {"model": e.model, "severity": e.severity, "message": e.message}
            for e in errors
        ],
        "passed": error_count == 0,
    }


# --- Compile-time validation ---


@router.post("/api/check")
def run_check(request: Request, conn_opt: DbConnReadOnlyOptional = None) -> dict:
    """Validate SQL models, run inline assertions, and run YAML contracts."""
    _require_permission(request, "read")
    from havn.engine.seeds import discover_seeds
    from havn.engine.transform import discover_all_models, run_assertions, validate_models

    project_dir = _get_project_dir()
    models = discover_all_models(project_dir)
    config = _get_config()

    known_tables: set[str] = set()
    seeds = discover_seeds(project_dir / "seeds")
    for s in seeds:
        known_tables.add(s["full_name"])
    for src in config.sources:
        for t in src.tables:
            known_tables.add(f"{src.schema}.{t.name}")

    source_columns: dict[str, set[str]] = {}
    for src in config.sources:
        for t in src.tables:
            full = f"{src.schema}.{t.name}"
            source_columns[full] = {c.name for c in t.columns}

    conn = conn_opt
    errors = validate_models(
        conn, models, known_tables=known_tables, source_columns=source_columns
    )

    # Run inline assertions (-- assert: comments) against live data
    assertion_results: list[dict] = []
    if conn:
        for model in models:
            if model.assertions:
                try:
                    results = run_assertions(conn, model)
                    for ar in results:
                        assertion_results.append({
                            "model": model.full_name,
                            "expression": ar.expression,
                            "passed": ar.passed,
                            "detail": ar.detail,
                        })
                except Exception as e:
                    assertion_results.append({
                        "model": model.full_name,
                        "expression": "(all)",
                        "passed": False,
                        "detail": str(e),
                    })

    # Run YAML contracts from contracts/ directory
    contract_results: list[dict] = []
    contracts_dir = project_dir / "contracts"
    if conn and contracts_dir.exists():
        from havn.engine.contracts import discover_contracts, evaluate_contract

        contracts = discover_contracts(contracts_dir)
        for contract in contracts:
            try:
                cr = evaluate_contract(conn, contract)
                contract_results.append({
                    "contract_name": cr.contract_name,
                    "model": cr.model,
                    "passed": cr.passed,
                    "severity": cr.severity,
                    "duration_ms": cr.duration_ms,
                    "error": cr.error,
                    "assertions": cr.results,
                })
            except Exception as e:
                contract_results.append({
                    "contract_name": contract.name,
                    "model": contract.model,
                    "passed": False,
                    "severity": contract.severity,
                    "duration_ms": 0,
                    "error": str(e),
                    "assertions": [],
                })

    validation_passed = not any(e.severity == "error" for e in errors)
    assertions_passed = all(ar["passed"] for ar in assertion_results)
    contracts_passed = all(cr["passed"] for cr in contract_results)

    return {
        "models_checked": len(models),
        "errors": [
            {"model": e.model, "severity": e.severity, "message": e.message}
            for e in errors
        ],
        "assertions": assertion_results,
        "contracts": contract_results,
        "passed": validation_passed and assertions_passed and contracts_passed,
    }
