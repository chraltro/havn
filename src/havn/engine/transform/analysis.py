"""Column lineage, SQL validation, impact analysis, and freshness monitoring."""

from __future__ import annotations

import logging

import duckdb

from havn.engine.sql_analysis import extract_column_lineage as _extract_column_lineage_impl

from .models import SQLModel, ValidationError

logger = logging.getLogger("havn.transform")


def extract_column_lineage(
    model: SQLModel,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Extract column-level lineage from a SQL model using sqlglot AST parsing.

    Returns a mapping of output_column -> list of {source_table, source_column}.
    Delegates to the shared sql_analysis module for AST-based lineage tracing.
    """
    return _extract_column_lineage_impl(
        query=model.query,
        depends_on=model.depends_on,
        conn=conn,
    )


def validate_models(
    conn: duckdb.DuckDBPyConnection | None,
    models: list[SQLModel],
    known_tables: set[str] | None = None,
    source_columns: dict[str, set[str]] | None = None,
    landing_schemas: set[str] | None = None,
    deny_rules: list | None = None,
) -> list[ValidationError]:
    """Validate all models without executing them.

    Checks:
    - SQL parses correctly (sqlglot)
    - Referenced tables exist (in DAG, DuckDB catalog, sources.yml, or seeds)
    - Column references exist in upstream tables (when resolvable)
    - Ambiguous column references (column in multiple upstream tables without qualifier)
    - All depends_on references resolve to DAG models or existing catalog objects
    - Incremental models have a unique_key when using merge/delete+insert strategy
    - No model writes to a landing schema (would overwrite raw data)

    Args:
        conn: DuckDB connection for catalog lookups.
        models: List of SQL models to validate.
        known_tables: Additional known table names (e.g. from seeds, sources).
        source_columns: Column sets declared in sources.yml, keyed by table name.
        landing_schemas: Schema names reserved for raw/landing data.
    """
    import sqlglot
    from sqlglot import exp

    model_names = {m.full_name for m in models}
    errors: list[ValidationError] = []

    # Build catalog of known tables (existing in DuckDB + model names + extra)
    all_known_tables: set[str] = set(model_names)
    if known_tables:
        all_known_tables.update(t.lower() for t in known_tables)
    if conn:
        try:
            rows = conn.execute(
                "SELECT table_schema || '.' || table_name FROM information_schema.tables"
            ).fetchall()
            all_known_tables.update(r[0].lower() for r in rows)
        except Exception as e:
            logger.debug("Could not get table columns from catalog: %s", e)

    # Build column catalog: table -> set of columns
    column_catalog: dict[str, set[str]] = {}
    if source_columns:
        for table_name, cols in source_columns.items():
            column_catalog.setdefault(table_name.lower(), set()).update(
                c.lower() for c in cols
            )
    if conn:
        try:
            rows = conn.execute(
                "SELECT table_schema || '.' || table_name, column_name "
                "FROM information_schema.columns"
            ).fetchall()
            for table_fqn, col_name in rows:
                table_fqn = table_fqn.lower()
                column_catalog.setdefault(table_fqn, set()).add(col_name.lower())
        except Exception as e:
            logger.debug("Could not describe table columns: %s", e)

    for model in models:
        # 1. Parse check
        try:
            parsed = sqlglot.parse_one(model.query, read="duckdb")
        except sqlglot.errors.ParseError as e:
            errors.append(ValidationError(
                model=model.full_name,
                severity="error",
                message=f"SQL parse error: {e}",
            ))
            continue

        # CTEs look like exp.Table refs in the AST; collect their names so we
        # can skip catalog/column lookups against them.
        cte_names: set[str] = set()
        cte_columns: dict[str, set[str]] = {}
        for cte in parsed.find_all(exp.CTE):
            cte_alias = (cte.alias or "").lower()
            if not cte_alias:
                continue
            cte_names.add(cte_alias)
            cols: set[str] = set()
            inner = cte.this
            if isinstance(inner, exp.Select):
                star_seen = False
                for proj in inner.expressions:
                    if isinstance(proj, exp.Alias):
                        cols.add(proj.alias.lower())
                    elif isinstance(proj, exp.Column):
                        if proj.name == "*" or isinstance(proj.this, exp.Star):
                            star_seen = True
                        elif proj.name:
                            cols.add(proj.name.lower())
                    elif isinstance(proj, exp.Star):
                        star_seen = True
                # If the CTE selects *, we cannot enumerate its columns
                # statically — leave the column set empty (we'll skip
                # validation rather than flag false positives).
                if star_seen:
                    cols = set()
            cte_columns[cte_alias] = cols

        def _is_cte_ref(table: exp.Table) -> bool:
            return (table.name or "").lower() in cte_names and not (table.db or "")

        # 2. Check referenced tables exist
        for table in parsed.find_all(exp.Table):
            if _is_cte_ref(table):
                continue
            db_name = table.db or ""
            table_name = table.name or ""
            if db_name and table_name:
                fqn = f"{db_name}.{table_name}".lower()
                from havn.engine.sql_analysis import SKIP_SCHEMAS
                if fqn not in all_known_tables and db_name.lower() not in SKIP_SCHEMAS:
                    errors.append(ValidationError(
                        model=model.full_name,
                        severity="error",
                        message=f"Referenced table '{fqn}' does not exist",
                    ))

        # 3. Check column references
        # Build alias map for this model (excluding CTE references)
        alias_map: dict[str, str] = {}
        # Aliases that point to CTEs: e.g. `FROM flows f` -> {"f": "flows"}.
        cte_alias_map: dict[str, str] = {}
        for table in parsed.find_all(exp.Table):
            if _is_cte_ref(table):
                cte_name = (table.name or "").lower()
                cte_alias_map[cte_name] = cte_name
                alias = (table.alias or "").lower()
                if alias:
                    cte_alias_map[alias] = cte_name
                continue
            db_name = table.db or ""
            table_name = table.name or ""
            alias = table.alias or ""
            if db_name and table_name:
                fqn = f"{db_name}.{table_name}".lower()
                if alias:
                    alias_map[alias.lower()] = fqn
                alias_map[fqn] = fqn

        for col in parsed.find_all(exp.Column):
            col_name = col.name.lower() if col.name else ""
            table_ref = col.table.lower() if col.table else ""

            # Skip "*" tokens — sqlglot represents `b.*` as Column(name="*", table="b").
            if col_name == "*":
                continue

            if table_ref and col_name:
                # CTE-qualified (or aliased CTE-qualified) column.
                cte_target = cte_alias_map.get(table_ref)
                if cte_target:
                    cte_cols = cte_columns.get(cte_target, set())
                    # Only error if we have any CTE outputs recorded and the
                    # column genuinely isn't there. If we couldn't infer the
                    # CTE's columns (e.g. SELECT * inside the CTE), skip.
                    if cte_cols and col_name not in cte_cols:
                        errors.append(ValidationError(
                            model=model.full_name,
                            severity="error",
                            message=f"Column '{col_name}' not found in CTE '{cte_target}'",
                        ))
                    continue

                resolved_table = alias_map.get(table_ref, table_ref)
                if resolved_table in column_catalog:
                    if col_name not in column_catalog[resolved_table]:
                        errors.append(ValidationError(
                            model=model.full_name,
                            severity="error",
                            message=f"Column '{col_name}' not found in table '{resolved_table}'",
                        ))
            elif col_name and not table_ref:
                # Unqualified column — check for ambiguity across upstream sources.
                found_in: list[str] = []
                for dep in model.depends_on:
                    if dep in column_catalog and col_name in column_catalog[dep]:
                        found_in.append(dep)
                if len(found_in) > 1:
                    errors.append(ValidationError(
                        model=model.full_name,
                        severity="warning",
                        message=f"Ambiguous column '{col_name}' found in multiple tables: {', '.join(found_in)}",
                    ))

    # --- Additional pre-build validations ---

    # Default landing schemas if not provided
    _landing = {s.lower() for s in landing_schemas} if landing_schemas else {"landing"}

    for model in models:
        # 4. Check all depends_on references resolve
        for dep in model.depends_on:
            dep_lower = dep.lower()
            if dep_lower not in all_known_tables:
                errors.append(ValidationError(
                    model=model.full_name,
                    severity="error",
                    message=f"Dependency '{dep}' not found in DAG or database catalog",
                ))

        # 5. Incremental models should have unique_key for merge/delete+insert
        if model.materialized == "incremental":
            if model.incremental_strategy in ("merge", "delete+insert") and not model.unique_key:
                errors.append(ValidationError(
                    model=model.full_name,
                    severity="warning",
                    message=(
                        f"Incremental model uses '{model.incremental_strategy}' strategy "
                        "but has no unique_key set — this may cause duplicate rows"
                    ),
                ))

        # 6. Model must not write to a landing schema
        if model.schema.lower() in _landing:
            errors.append(ValidationError(
                model=model.full_name,
                severity="error",
                message=(
                    f"Model writes to landing schema '{model.schema}' — "
                    "transforms must not overwrite raw data"
                ),
            ))

    # 7. Deny-list policies: refuse models in forbidden schemas that
    #    reference forbidden columns. Catches PII leaks at compile time.
    if deny_rules:
        for model in models:
            try:
                parsed = sqlglot.parse_one(model.query, read="duckdb")
            except sqlglot.errors.ParseError:
                continue  # Already reported above
            schema_lower = model.schema.lower()
            referenced_columns: set[str] = set()
            for col in parsed.find_all(exp.Column):
                if col.name:
                    referenced_columns.add(col.name.lower())
            for rule in deny_rules:
                forbidden_schemas = {s.lower() for s in (rule.forbid_in_schemas or [])}
                if schema_lower not in forbidden_schemas:
                    continue
                col = (rule.column or "").lower()
                if not col:
                    continue
                if col in referenced_columns:
                    reason = f"  ({rule.reason})" if rule.reason else ""
                    errors.append(ValidationError(
                        model=model.full_name,
                        severity="error",
                        message=(
                            f"Policy violation: column '{rule.column}' is forbidden "
                            f"in schema '{model.schema}'.{reason}"
                        ),
                    ))

    return errors


def impact_analysis(
    models: list[SQLModel],
    target: str,
    column: str | None = None,
    conn: duckdb.DuckDBPyConnection | None = None,
) -> dict:
    """Analyze downstream impact of changing a model or column.

    Args:
        models: All discovered models
        target: Model name (e.g. "silver.customers")
        column: Optional column name to trace
        conn: Optional connection for column-level lineage resolution

    Returns:
        Dict with downstream_models, affected_columns, impact_chain
    """
    model_map = {m.full_name: m for m in models}

    # Build reverse dependency graph: model -> list of models that depend on it
    reverse_deps: dict[str, list[str]] = {}
    for m in models:
        for dep in m.depends_on:
            reverse_deps.setdefault(dep, []).append(m.full_name)

    # BFS to find all downstream models
    downstream: list[str] = []
    visited: set[str] = set()
    queue = [target]

    while queue:
        current = queue.pop(0)
        for child in reverse_deps.get(current, []):
            if child not in visited:
                visited.add(child)
                downstream.append(child)
                queue.append(child)

    # Build impact chain (model -> its direct dependents)
    impact_chain: dict[str, list[str]] = {}
    chain_visited: set[str] = set()
    chain_queue = [target]
    while chain_queue:
        current = chain_queue.pop(0)
        if current in chain_visited:
            continue
        chain_visited.add(current)
        children = reverse_deps.get(current, [])
        if children:
            impact_chain[current] = children
            chain_queue.extend(children)

    result: dict = {
        "target": target,
        "downstream_models": downstream,
        "impact_chain": impact_chain,
    }

    # Column-level impact if a column is specified
    if column and conn:
        affected_columns: list[dict[str, str]] = []
        for ds_name in downstream:
            ds_model = model_map.get(ds_name)
            if not ds_model:
                continue
            lineage = extract_column_lineage(ds_model, conn)
            for out_col, sources in lineage.items():
                for src in sources:
                    if src["source_table"] == target and src["source_column"] == column:
                        affected_columns.append({
                            "model": ds_name,
                            "column": out_col,
                        })
        result["column"] = column
        result["affected_columns"] = affected_columns

    return result


def check_freshness(
    conn: duckdb.DuckDBPyConnection,
    max_age_hours: float = 24.0,
    *,
    include_sources: bool = False,
    source_min_rows: int = 0,
    transform_dir = None,
) -> list[dict]:
    """Check freshness of all models. Returns stale models.

    A model is stale if it hasn't been run within ``max_age_hours``.
    When ``include_sources=True`` the source-side row counts and
    max-on-column timestamps from each model's ``@source_freshness``
    contracts are joined into the result. ``source_min_rows > 0``
    additionally flips a model to stale if any source has fewer than
    that many rows (the "0 rows ≠ fresh" guarantee).
    """
    try:
        rows = conn.execute(
            """
            SELECT model_path, last_run_at, run_duration_ms, row_count,
                   EXTRACT(EPOCH FROM (current_timestamp - last_run_at)) / 3600 AS hours_since
            FROM _havn.model_state
            ORDER BY last_run_at ASC
            """
        ).fetchall()
    except Exception as e:
        logger.warning("Failed to check freshness: %s", e)
        return []

    # Build a model_path -> source_specs lookup if sources are requested.
    source_specs_by_model: dict[str, list[dict]] = {}
    if include_sources and transform_dir is not None:
        try:
            from .discovery import discover_models

            for m in discover_models(transform_dir):
                if m.source_freshness:
                    source_specs_by_model[m.full_name] = m.source_freshness
        except Exception as e:
            logger.debug("Couldn't load model source specs: %s", e)

    results = []
    for model_path, last_run, duration_ms, row_count, hours_since in rows:
        entry: dict = {
            "model": model_path,
            "last_run_at": str(last_run) if last_run else None,
            "hours_since_run": round(hours_since, 1) if hours_since is not None else None,
            "is_stale": hours_since is not None and hours_since > max_age_hours,
            "row_count": row_count,
        }
        if include_sources:
            specs = source_specs_by_model.get(model_path, [])
            sources_out: list[dict] = []
            for spec in specs:
                src = {
                    "table": spec["table"],
                    "on": spec.get("on"),
                    "row_count": None,
                    "max_loaded_at": None,
                    "age_seconds": None,
                    "is_stale": False,
                    "error": None,
                }
                try:
                    cnt = conn.execute(f"SELECT COUNT(*) FROM {spec['table']}").fetchone()
                    src["row_count"] = int(cnt[0]) if cnt else 0
                    if spec.get("on"):
                        on = spec["on"]
                        # Cast MAX() to VARCHAR in SQL: returning a bare
                        # TIMESTAMP/TIMESTAMPTZ across the DuckDB→Python
                        # boundary needs pytz on some installs and crashes
                        # if it's missing. The string form is enough for
                        # human-readable surfacing; age comes from EXTRACT.
                        row = conn.execute(
                            f"SELECT CAST(MAX({on}) AS VARCHAR), "
                            f"EXTRACT(EPOCH FROM (current_timestamp - MAX({on}))) "
                            f"FROM {spec['table']}"
                        ).fetchone()
                        if row and row[0] is not None:
                            src["max_loaded_at"] = row[0]
                            src["age_seconds"] = float(row[1]) if row[1] is not None else None
                    max_age = int(spec.get("max_age_seconds") or 0)
                    if src["age_seconds"] is not None and max_age > 0:
                        src["is_stale"] = src["age_seconds"] > max_age
                    if source_min_rows > 0 and (src["row_count"] or 0) < source_min_rows:
                        src["is_stale"] = True
                except Exception as e:
                    src["error"] = str(e)
                    src["is_stale"] = True
                sources_out.append(src)
                # Roll up into the model's overall freshness verdict so
                # CI / alerts can read a single is_stale flag.
                if src["is_stale"]:
                    entry["is_stale"] = True
            entry["sources"] = sources_out
        results.append(entry)

    return results
