"""DAG visualization and full DAG endpoints."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Request

from havn.server.deps import (
    _discover_models_cached,
    _get_config,
    _get_project_dir,
    _require_permission,
    build_dag,
    connect,
)

router = APIRouter()


def _scan_ingest_targets(project_dir: Path) -> dict[str, list[str]]:
    """Scan ingest scripts for tables they create (schema.table patterns)."""
    ingest_dir = project_dir / "ingest"
    if not ingest_dir.is_dir():
        return {}

    pattern = re.compile(
        r"(?:CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW)\s+|INTO\s+)"
        r"(\w+\.\w+)",
        re.IGNORECASE,
    )

    targets: dict[str, list[str]] = {}
    files = sorted(
        list(ingest_dir.glob("*.py")) + list(ingest_dir.glob("*.dpnb")),
        key=lambda p: p.name,
    )
    for script_file in files:
        if script_file.name.startswith("_"):
            continue
        try:
            text = script_file.read_text()
        except Exception:
            continue
        for match in pattern.finditer(text):
            table_ref = match.group(1).lower()
            rel_path = str(script_file.relative_to(project_dir))
            if table_ref not in targets:
                targets[table_ref] = []
            if rel_path not in targets[table_ref]:
                targets[table_ref].append(rel_path)

    return targets


def _scan_import_sources(project_dir: Path) -> dict[str, str]:
    """Query run_log for the most recent successful import per table."""
    from havn.server.deps import _get_db_path
    db_path = _get_db_path()
    conn = connect(db_path)
    try:
        result = conn.execute(
            """
            SELECT DISTINCT ON (target) target, log_output
            FROM _havn.run_log
            WHERE run_type = 'import' AND status = 'success'
            ORDER BY target, started_at DESC
        """
        ).fetchall()
        return {
            row[0]: row[1] if row[1] else row[0].split(".")[-1] for row in result
        }
    except Exception:
        return {}
    finally:
        conn.close()


@router.get("/api/dag")
def get_dag(request: Request) -> dict:
    """Get the model DAG for visualization."""
    _require_permission(request, "read")
    project_dir = _get_project_dir()
    transform_dir = project_dir / "transform"
    models = _discover_models_cached(transform_dir)
    ordered = build_dag(models)

    nodes = []
    edges = []
    model_set = {m.full_name for m in models}

    ingest_targets = _scan_ingest_targets(project_dir)
    import_sources = _scan_import_sources(project_dir)

    external_deps: set[str] = set()
    for m in models:
        for dep in m.depends_on:
            if dep not in model_set:
                external_deps.add(dep)

    added_scripts: set[str] = set()
    for dep in sorted(external_deps):
        for script_path in ingest_targets.get(dep, []):
            script_id = f"script:{script_path}"
            if script_id not in added_scripts:
                added_scripts.add(script_id)
                nodes.append(
                    {
                        "id": script_id,
                        "label": Path(script_path).name,
                        "schema": "ingest",
                        "type": "ingest",
                        "path": script_path,
                    }
                )
            edges.append({"source": script_id, "target": dep})

    added_imports: set[str] = set()
    for dep in sorted(external_deps):
        if dep in import_sources and dep not in ingest_targets:
            source_file = import_sources[dep]
            import_id = f"import:{dep}"
            if import_id not in added_imports:
                added_imports.add(import_id)
                nodes.append(
                    {
                        "id": import_id,
                        "label": source_file,
                        "schema": "import",
                        "type": "import",
                        "source_file": source_file,
                    }
                )
            edges.append({"source": import_id, "target": dep})

    for dep in sorted(external_deps):
        schema = dep.split(".")[0] if "." in dep else "source"
        nodes.append(
            {
                "id": dep,
                "label": dep,
                "schema": schema,
                "type": "source",
            }
        )

    for m in ordered:
        nodes.append(
            {
                "id": m.full_name,
                "label": m.path.name,
                "schema": m.schema,
                "type": m.materialized,
                "path": str(m.path.relative_to(project_dir)),
            }
        )

    for m in models:
        for dep in m.depends_on:
            edges.append({"source": dep, "target": m.full_name})

    return {"nodes": nodes, "edges": edges}


def _scan_export_sources(project_dir: Path, model_fqns: set[str]) -> dict[str, list[str]]:
    """Scan export scripts for references to specific model FQNs.

    Returns a mapping of ``export/script.py`` -> list of FQNs it references.
    Used by the orchestration DAG picker to wire exports as downstream
    consumers of the models they read from.
    """
    export_dir = project_dir / "export"
    if not export_dir.is_dir():
        return {}
    result: dict[str, list[str]] = {}
    files = sorted(
        list(export_dir.glob("*.py")) + list(export_dir.glob("*.dpnb")),
        key=lambda p: p.name,
    )
    for script_file in files:
        if script_file.name.startswith("_"):
            continue
        try:
            text = script_file.read_text()
        except Exception:
            continue
        refs: list[str] = []
        for fqn in model_fqns:
            if fqn in text:
                refs.append(fqn)
        rel = f"export/{script_file.name}"
        result[rel] = refs
    return result


@router.get("/api/dag/orchestration")
def get_orchestration_dag(request: Request) -> dict:
    """DAG used by the Orchestration job picker.

    Returns ingest scripts (as sources feeding ``landing.*`` tables), every
    transform model grouped by schema, and export scripts (as sinks) with the
    models they reference. Also includes per-model ``last_status`` from
    ``_havn.model_state`` so the picker can show a health indicator.
    """
    _require_permission(request, "read")
    project_dir = _get_project_dir()
    transform_dir = project_dir / "transform"
    models = _discover_models_cached(transform_dir)
    ordered = build_dag(models)

    model_set = {m.full_name for m in models}
    ingest_targets = _scan_ingest_targets(project_dir)
    export_refs = _scan_export_sources(project_dir, model_set)

    # Fetch last_run info from _havn.model_state for status badges
    last_state: dict[str, dict] = {}
    try:
        from havn.server.deps import _get_db_path
        db_path = _get_db_path()
        if db_path.exists():
            conn = connect(db_path, read_only=True)
            try:
                rows = conn.execute(
                    "SELECT model_path, last_run_at, row_count, run_duration_ms "
                    "FROM _havn.model_state"
                ).fetchall()
                for r in rows:
                    last_state[r[0]] = {
                        "last_run_at": str(r[1]) if r[1] else None,
                        "row_count": r[2],
                        "duration_ms": r[3],
                    }
            except Exception:
                pass
            finally:
                conn.close()
    except Exception:
        pass

    nodes: list[dict] = []
    edges: list[dict] = []

    # Ingest scripts — one node per physical script
    seen_ingest: set[str] = set()
    for _dep, scripts in ingest_targets.items():
        for script_path in scripts:
            if script_path in seen_ingest:
                continue
            seen_ingest.add(script_path)
            nodes.append({
                "id": script_path,
                "kind": "ingest",
                "label": Path(script_path).name,
                "path": script_path,
                "schema": "ingest",
            })
    # Also include ingest scripts that don't reference any landing table
    # (e.g. side-effect scripts) — enumerate the directory
    ingest_dir = project_dir / "ingest"
    if ingest_dir.is_dir():
        for script_file in sorted(list(ingest_dir.glob("*.py")) + list(ingest_dir.glob("*.dpnb"))):
            if script_file.name.startswith("_"):
                continue
            rel = f"ingest/{script_file.name}"
            if rel not in seen_ingest:
                seen_ingest.add(rel)
                nodes.append({
                    "id": rel,
                    "kind": "ingest",
                    "label": script_file.name,
                    "path": rel,
                    "schema": "ingest",
                })

    # Transform models
    for m in ordered:
        state = last_state.get(m.full_name, {})
        nodes.append({
            "id": m.full_name,
            "kind": "transform",
            "label": m.name,
            "schema": m.schema,
            "materialized": m.materialized,
            "depends_on": list(m.depends_on or []),
            "path": str(m.path.relative_to(project_dir)) if m.path else None,
            "row_count": state.get("row_count"),
            "last_run_at": state.get("last_run_at"),
            "duration_ms": state.get("duration_ms"),
        })

    # Export scripts — reference which models they consume
    for export_path, refs in export_refs.items():
        nodes.append({
            "id": export_path,
            "kind": "export",
            "label": Path(export_path).name,
            "path": export_path,
            "schema": "export",
            "depends_on": refs,
        })

    # Edges: ingest -> landing (via dep chain), model -> model, model -> export
    for dep, scripts in ingest_targets.items():
        for script_path in scripts:
            edges.append({"source": script_path, "target": dep, "kind": "ingest"})
    for m in models:
        for dep in m.depends_on or []:
            edges.append({"source": dep, "target": m.full_name, "kind": "transform"})
    for export_path, refs in export_refs.items():
        for ref in refs:
            edges.append({"source": ref, "target": export_path, "kind": "export"})

    # Sorted schema list for the picker's column layout
    schemas_in_use: list[str] = []
    for m in ordered:
        if m.schema not in schemas_in_use:
            schemas_in_use.append(m.schema)

    return {
        "nodes": nodes,
        "edges": edges,
        "schemas": schemas_in_use,
    }


@router.get("/api/dag/full")
def get_full_dag(request: Request) -> dict:
    """Get the full DAG including seeds, sources, and exposures."""
    _require_permission(request, "read")
    project_dir = _get_project_dir()
    transform_dir = project_dir / "transform"
    models = _discover_models_cached(transform_dir)
    ordered = build_dag(models)
    config = _get_config()

    nodes = []
    edges = []
    model_set = {m.full_name for m in models}

    source_tables: set[str] = set()
    for src in config.sources:
        for tbl in src.tables:
            full_name = f"{src.schema}.{tbl.name}"
            source_tables.add(full_name)
            nodes.append(
                {
                    "id": full_name,
                    "label": tbl.name,
                    "schema": src.schema,
                    "type": "source",
                    "description": tbl.description or src.description,
                }
            )

    from havn.engine.seeds import discover_seeds

    seeds_dir = project_dir / "seeds"
    seeds = discover_seeds(seeds_dir)
    seed_set: set[str] = set()
    for s in seeds:
        seed_set.add(s["full_name"])
        nodes.append(
            {
                "id": s["full_name"],
                "label": s["name"],
                "schema": s["schema"],
                "type": "seed",
            }
        )

    ingest_targets = _scan_ingest_targets(project_dir)
    external_deps: set[str] = set()
    for m in models:
        for dep in m.depends_on:
            if dep not in model_set and dep not in source_tables and dep not in seed_set:
                external_deps.add(dep)

    for dep in sorted(external_deps):
        for script_path in ingest_targets.get(dep, []):
            script_id = f"script:{script_path}"
            nodes.append(
                {
                    "id": script_id,
                    "label": Path(script_path).name,
                    "schema": "ingest",
                    "type": "ingest",
                    "path": script_path,
                }
            )
            edges.append({"source": script_id, "target": dep})

        if dep not in source_tables and dep not in seed_set:
            schema = dep.split(".")[0] if "." in dep else "source"
            nodes.append(
                {
                    "id": dep,
                    "label": dep,
                    "schema": schema,
                    "type": "source",
                }
            )

    for m in ordered:
        nodes.append(
            {
                "id": m.full_name,
                "label": m.path.name,
                "schema": m.schema,
                "type": m.materialized,
                "path": str(m.path.relative_to(project_dir)),
            }
        )

    for m in models:
        for dep in m.depends_on:
            edges.append({"source": dep, "target": m.full_name})

    for exp in config.exposures:
        exp_id = f"exposure:{exp.name}"
        nodes.append(
            {
                "id": exp_id,
                "label": exp.name,
                "schema": "exposure",
                "type": "exposure",
                "description": exp.description,
                "owner": exp.owner,
            }
        )
        for dep in exp.depends_on:
            edges.append({"source": dep, "target": exp_id})

    return {"nodes": nodes, "edges": edges}
