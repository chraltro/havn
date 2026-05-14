"""Model discovery, DAG building, and change detection."""

from __future__ import annotations

import hashlib
from graphlib import TopologicalSorter
from pathlib import Path

import duckdb

from havn.engine.sql_analysis import (
    extract_table_refs,
    parse_assertion_specs,
    parse_assertions,
    parse_column_docs,
    parse_config,
    parse_depends,
    parse_description,
    parse_grain,
    parse_owner,
    parse_source_freshness,
    strip_config_comments,
)
from havn.engine.utils import validate_identifier

from .models import SQLModel


def discover_models(transform_dir: Path) -> list[SQLModel]:
    """Discover all SQL models in the transform directory.

    Convention: folder names map to schemas.
    transform/bronze/customers.sql -> schema=bronze, name=customers
    """
    models = []
    if not transform_dir.exists():
        return models

    for sql_file in sorted(transform_dir.rglob("*.sql")):
        sql = sql_file.read_text()
        config = parse_config(sql)
        depends = parse_depends(sql)
        description = parse_description(sql)
        column_docs = parse_column_docs(sql)
        assertions = parse_assertions(sql)
        assertion_specs = parse_assertion_specs(sql)
        grain = parse_grain(sql)
        owner = parse_owner(sql)
        source_freshness = parse_source_freshness(sql)
        query = strip_config_comments(sql)
        if not depends:
            folder_schema_tmp = sql_file.relative_to(transform_dir).parent.name or "public"
            own_schema_tmp = config.get("schema", folder_schema_tmp)
            own_name_tmp = sql_file.stem
            depends = extract_table_refs(query, exclude=f"{own_schema_tmp}.{own_name_tmp}")

        # Schema from folder name (convention) or config override
        rel = sql_file.relative_to(transform_dir)
        folder_schema = rel.parent.name if rel.parent.name else "public"
        schema = config.get("schema", folder_schema)
        name = sql_file.stem
        # Validate identifiers at discovery time to prevent SQL injection downstream
        validate_identifier(schema, f"schema for {sql_file.name}")
        validate_identifier(name, f"model name for {sql_file.name}")
        materialized = config.get("materialized", "view")
        unique_key = config.get("unique_key")
        incremental_strategy = config.get("incremental_strategy", "delete+insert")
        incremental_filter = config.get("incremental_filter")
        partition_by = config.get("partition_by")
        watermark = config.get("watermark")

        models.append(
            SQLModel(
                path=sql_file,
                name=name,
                schema=schema,
                full_name=f"{schema}.{name}",
                sql=sql,
                query=query,
                materialized=materialized,
                depends_on=depends,
                description=description,
                column_docs=column_docs,
                assertions=assertions,
                assertion_specs=assertion_specs,
                unique_key=unique_key,
                incremental_strategy=incremental_strategy,
                incremental_filter=incremental_filter,
                partition_by=partition_by,
                watermark=watermark,
                grain=grain,
                owner=owner,
                source_freshness=source_freshness,
            )
        )

    return models


def build_dag(models: list[SQLModel]) -> list[SQLModel]:
    """Sort models in dependency order using topological sort."""
    model_map = {m.full_name: m for m in models}
    sorter: TopologicalSorter[str] = TopologicalSorter()

    for m in models:
        # Filter dependencies to only those that are known models
        # (landing.* tables won't be in the model list — that's fine)
        known_deps = [d for d in m.depends_on if d in model_map]
        sorter.add(m.full_name, *known_deps)

    ordered = list(sorter.static_order())
    return [model_map[name] for name in ordered if name in model_map]


def build_dag_tiers(models: list[SQLModel]) -> list[list[SQLModel]]:
    """Build DAG and return models grouped by execution tier.

    Models within the same tier have no dependencies on each other
    and can execute in parallel.
    """
    model_map = {m.full_name: m for m in models}
    sorter: TopologicalSorter[str] = TopologicalSorter()

    for m in models:
        known_deps = [d for d in m.depends_on if d in model_map]
        sorter.add(m.full_name, *known_deps)

    sorter.prepare()
    tiers: list[list[SQLModel]] = []

    while sorter.is_active():
        ready = sorted(sorter.get_ready())
        tier = [model_map[name] for name in ready if name in model_map]
        if tier:
            tiers.append(tier)
        for name in ready:
            sorter.done(name)

    return tiers


def _compute_upstream_hash(model: SQLModel, model_map: dict[str, SQLModel]) -> str:
    """Compute a combined hash of all upstream model content and upstream hashes.

    Includes both content_hash and upstream_hash of each dependency so that
    changes propagate transitively through the full DAG (not just one level).
    Models must be processed in topological order so that upstream_hash is
    already set on dependencies before it is read here.
    """
    if not model.depends_on:
        return ""
    upstream_hashes = []
    for dep in sorted(model.depends_on):
        if dep in model_map:
            dep_model = model_map[dep]
            upstream_hashes.append(dep_model.content_hash)
            upstream_hashes.append(dep_model.upstream_hash)
    return hashlib.sha256("".join(upstream_hashes).encode()).hexdigest()[:16]


def _has_changed(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
) -> bool:
    """Check if a model has changed since last run."""
    result = conn.execute(
        "SELECT content_hash, upstream_hash FROM _havn.model_state WHERE model_path = ?",
        [model.full_name],
    ).fetchone()
    if result is None:
        return True
    old_content_hash, old_upstream_hash = result
    return old_content_hash != model.content_hash or old_upstream_hash != model.upstream_hash


def _update_state(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    duration_ms: int,
    row_count: int,
) -> None:
    """Update the model state after a successful run.

    INSERT OR REPLACE on DuckDB is atomic against the model_path PK;
    DuckLake strips the PK at table creation so we fall back to
    DELETE-then-INSERT inside an explicit transaction.
    """
    from havn.engine.database import _is_ducklake_connection

    params = [
        model.full_name,
        model.content_hash,
        model.upstream_hash,
        model.materialized,
        duration_ms,
        row_count,
    ]
    if _is_ducklake_connection(conn):
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(
                "DELETE FROM _havn.model_state WHERE model_path = ?",
                [model.full_name],
            )
            conn.execute(
                """
                INSERT INTO _havn.model_state
                    (model_path, content_hash, upstream_hash, materialized_as, last_run_at, run_duration_ms, row_count)
                VALUES (?, ?, ?, ?, current_timestamp, ?, ?)
                """,
                params,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    else:
        conn.execute(
            """
            INSERT OR REPLACE INTO _havn.model_state
                (model_path, content_hash, upstream_hash, materialized_as, last_run_at, run_duration_ms, row_count)
            VALUES (?, ?, ?, ?, current_timestamp, ?, ?)
            """,
            params,
        )
