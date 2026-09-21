"""Column lineage, SQL validation, impact analysis, and freshness monitoring."""

from __future__ import annotations

import difflib
import logging
import re

import duckdb

from havn.engine.sql_analysis import (
    CONFIG_KEYS,
    MATERIALIZATIONS,
    ON_SCHEMA_CHANGE_POLICIES,
    extract_column_lineage as _extract_column_lineage_impl,
    extract_column_references,
    fetch_column_catalog,
    parse_config,
)

from .models import SQLModel, ValidationError

logger = logging.getLogger("havn.transform")


def extract_column_lineage(
    model: SQLModel,
    conn: duckdb.DuckDBPyConnection | None = None,
    column_catalog: dict[str, list[str]] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Extract column-level lineage from a SQL model using sqlglot AST parsing.

    Returns a mapping of output_column -> list of {source_table, source_column}.
    Delegates to the shared sql_analysis module for AST-based lineage tracing.

    Tracing several models in one pass? Call
    :func:`havn.engine.sql_analysis.fetch_column_catalog` once and pass the
    result as ``column_catalog`` so the catalog is not re-read per model.
    """
    return _extract_column_lineage_impl(
        query=model.query,
        depends_on=model.depends_on,
        conn=conn,
        column_catalog=column_catalog,
        ast=model.ast,
    )


def _did_you_mean(value: str, options: set[str] | frozenset[str]) -> str:
    """A ``Did you mean 'x'?`` clause for a near miss, or "" when there is none."""
    close = difflib.get_close_matches(value.lower(), sorted(options), n=1, cutoff=0.6)
    return f" Did you mean '{close[0]}'?" if close else ""


def _validate_config_keys(models: list[SQLModel]) -> list[ValidationError]:
    """Report `@config` keys and materializations that mean nothing.

    Discovery reads a fixed set of keys off the config dict and ignores the
    rest, so `materialised=table` used to build a view without a word of
    complaint, and any key from a newer version of havn (or a plain typo)
    did the same.
    """
    errors: list[ValidationError] = []
    for model in models:
        config = parse_config(model.sql)
        for key in config:
            if key in CONFIG_KEYS:
                continue
            errors.append(ValidationError(
                model=model.full_name,
                severity="error",
                message=(
                    f"Unknown @config key '{key}'."
                    f"{_did_you_mean(key, CONFIG_KEYS)}"
                    f" Known keys: {', '.join(sorted(CONFIG_KEYS))}."
                ),
            ))

        materialized = config.get("materialized")
        if materialized and materialized not in MATERIALIZATIONS:
            errors.append(ValidationError(
                model=model.full_name,
                severity="error",
                message=(
                    f"Unknown materialization '{materialized}'."
                    f"{_did_you_mean(materialized, MATERIALIZATIONS)}"
                    f" Supported: {', '.join(sorted(MATERIALIZATIONS))}."
                ),
            ))

        policy = config.get("on_schema_change")
        if policy and policy not in ON_SCHEMA_CHANGE_POLICIES:
            errors.append(ValidationError(
                model=model.full_name,
                severity="error",
                message=(
                    f"Unknown on_schema_change policy '{policy}'."
                    f"{_did_you_mean(policy, ON_SCHEMA_CHANGE_POLICIES)}"
                    f" Supported: {', '.join(sorted(ON_SCHEMA_CHANGE_POLICIES))}."
                ),
            ))
        elif policy and config.get("materialized") != "incremental":
            errors.append(ValidationError(
                model=model.full_name,
                severity="warning",
                message=(
                    "on_schema_change only applies to incremental models; "
                    f"this model is materialized as "
                    f"'{config.get('materialized', 'view')}' and the policy is ignored"
                ),
            ))
    return errors


_TAG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def _validate_tags(models: list[SQLModel]) -> list[ValidationError]:
    """Report `@config tags=` entries that no `tag:` selector could match.

    Tags are free text on the way in, so `tags = daily finance` (a space
    instead of a comma) used to produce a single tag nobody would ever type
    at the command line. Identifier-shaped tags keep `tag:` selectors
    predictable, and a hyphen is allowed because job names use them.
    """
    errors: list[ValidationError] = []
    for model in models:
        for tag in getattr(model, "tags", []) or []:
            if not _TAG_RE.match(tag):
                errors.append(ValidationError(
                    model=model.full_name,
                    severity="error",
                    message=(
                        f"Invalid tag '{tag}' in @config tags=. Tags must be "
                        "identifiers (letters, digits, underscore, hyphen; "
                        "not starting with a digit) and separated by commas."
                    ),
                ))
    return errors


def validate_models(
    conn: duckdb.DuckDBPyConnection | None,
    models: list[SQLModel],
    known_tables: set[str] | None = None,
    source_columns: dict[str, set[str]] | None = None,
    landing_schemas: set[str] | None = None,
    deny_rules: list | None = None,
    *,
    bind: bool = False,
    project_dir=None,
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
        for table_fqn, cols in fetch_column_catalog(conn).items():
            column_catalog.setdefault(table_fqn, set()).update(
                c.lower() for c in cols
            )

    for model in models:
        # 1. Parse check. ``model.ast`` is the tree discovery already parsed.
        parsed = model.ast
        if parsed is None:
            errors.append(ValidationError(
                model=model.full_name,
                severity="error",
                message=f"SQL parse error: {model.parse_error}",
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

    errors.extend(_validate_config_keys(models))
    errors.extend(_validate_tags(models))

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

        # 6. Assertions need a table to query, and an ephemeral model never
        #    becomes one.
        if model.materialized == "ephemeral" and (model.assertions or model.grain):
            errors.append(ValidationError(
                model=model.full_name,
                severity="error",
                message=(
                    "assertions cannot run on ephemeral models; move them to a "
                    "consumer. An ephemeral model is inlined into its consumers "
                    "as a CTE and never materialized, so there is nothing to "
                    "query after the build."
                ),
            ))

        # 7. Model must not write to a landing schema
        if model.schema.lower() in _landing:
            errors.append(ValidationError(
                model=model.full_name,
                severity="error",
                message=(
                    f"Model writes to landing schema '{model.schema}' — "
                    "transforms must not overwrite raw data"
                ),
            ))

    # 8. Deny-list policies: refuse models in forbidden schemas that
    #    reference forbidden columns. Catches PII leaks at compile time.
    if deny_rules:
        for model in models:
            parsed = model.ast
            if parsed is None:
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

    # 8. Shadow bind pass: hand the SQL to the DuckDB binder.
    #
    # Everything above is name-level. The binder is what resolves types,
    # function signatures and columns on upstreams that were never built. It
    # needs a writable connection to attach its throwaway catalog, so it is
    # opt-in rather than on by default.
    if bind and conn is not None:
        errors.extend(_bind_errors(conn, models, project_dir))

    return errors


def _bind_errors(
    conn: duckdb.DuckDBPyConnection,
    models: list[SQLModel],
    project_dir=None,
) -> list[ValidationError]:
    """Run the shadow bind pass and render it as ``ValidationError`` rows."""
    from .bind import as_validation_message, bind_models

    errors: list[ValidationError] = []
    try:
        result = bind_models(conn, models, project_dir=project_dir)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("Bind pass failed: %s", e)
        return errors

    for warning in result.warnings:
        errors.append(ValidationError(
            model="",
            severity="warning",
            message=warning.message,
        ))
    for model_name, bind_errors in result.errors.items():
        for err in bind_errors:
            errors.append(ValidationError(
                model=model_name,
                severity="error",
                message=as_validation_message(err),
                line=err.line,
            ))

    errors.extend(_contract_schema_errors(conn, project_dir, result.schemas))
    return errors


def _inferred_schema(
    conn: duckdb.DuckDBPyConnection,
    model: str,
    schemas: dict[str, list[tuple[str, str]]],
) -> list[tuple[str, str]]:
    """The best (column, type) list available for ``model`` before the build.

    The bind pass is the first choice: it reflects the file as it is written
    now, not the last build. A model outside the chain that was bound falls
    back to the schema recorded at its last build, which is at least real.
    Neither available means the shape is unknown and the caller must not
    invent a finding from it.
    """
    bound = schemas.get(model)
    if bound:
        return bound
    from .columns import load_model_columns

    return [(c["name"], c["type"]) for c in load_model_columns(conn, model)]


def _contract_schema_errors(
    conn: duckdb.DuckDBPyConnection,
    project_dir,
    schemas: dict[str, list[tuple[str, str]]],
) -> list[ValidationError]:
    """Check every contract's declared columns against the inferred schema.

    This is the whole point of putting columns in a contract: a break is
    reported before the build, on a warehouse where the table may not exist
    at all, rather than after a model has already replaced good data with
    the wrong shape.
    """
    if project_dir is None:
        return []
    from pathlib import Path

    from havn.engine.contracts import check_contract_schema, discover_contracts

    contracts_dir = Path(project_dir) / "contracts"
    if not contracts_dir.exists():
        return []
    try:
        contracts = discover_contracts(contracts_dir)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("Could not read contracts for validation: %s", e)
        return []

    errors: list[ValidationError] = []
    for contract in contracts:
        for message in contract.errors:
            errors.append(ValidationError(
                model=contract.model,
                severity="error",
                message=message,
            ))
        if not contract.columns:
            continue
        inferred = _inferred_schema(conn, contract.model, schemas)
        if not inferred:
            # Never bound, never built: the shape is unknown, and a guess
            # here would be a false break on a fresh checkout.
            continue
        for finding in check_contract_schema(contract, inferred):
            if finding.severity == "info":
                # An undeclared column on a non-strict contract is the normal
                # way to cover three columns of twenty. It belongs in the
                # contract report, not in every validate run.
                continue
            errors.append(ValidationError(
                model=contract.model,
                severity=finding.severity,
                message=f"contract '{contract.name}': {finding.message}",
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
        Dict with downstream_models, affected_columns, impact_chain.

    Each entry in ``affected_columns`` carries ``model``, ``column`` and
    ``clause``. A projection hit comes from column lineage and names the
    downstream *output* column, with ``clause`` set to ``select``. A hit in
    any other clause comes from the reference index and names the upstream
    column as it is written, because a filter, a join predicate, a GROUP BY
    or an ORDER BY produces no output column of its own. Without the second
    kind, a downstream model that only filters on the column looked
    unaffected.
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
        # One catalog read for the whole downstream set, not one per model.
        catalog = fetch_column_catalog(conn)
        # The reference index attributes an unqualified column by name, so it
        # wants the column sets the catalog already gave us. Types play no
        # part in that decision, hence the empty type strings.
        reference_schema = {
            table: [(col, "") for col in cols] for table, cols in catalog.items()
        }
        target_key = target.lower()
        column_key = column.lower()
        for ds_name in downstream:
            ds_model = model_map.get(ds_name)
            if not ds_model:
                continue
            lineage = extract_column_lineage(ds_model, conn, column_catalog=catalog)
            for out_col, sources in lineage.items():
                for src in sources:
                    if src["source_table"] == target and src["source_column"] == column:
                        affected_columns.append({
                            "model": ds_name,
                            "column": out_col,
                            "clause": "select",
                        })
            affected_columns.extend(
                _non_projection_hits(ds_model, ds_name, target_key, column_key, reference_schema)
            )
        result["column"] = column
        result["affected_columns"] = affected_columns

    return result


def _non_projection_hits(
    model: SQLModel,
    model_name: str,
    target: str,
    column: str,
    reference_schema: dict[str, list[tuple[str, str]]],
) -> list[dict[str, str]]:
    """Mentions of ``target.column`` in ``model`` outside the SELECT list.

    The SELECT list is left to column lineage, which knows the output column
    name a projection lands in; repeating it here would report the same hit
    twice under two names. Everything else -- WHERE, JOIN ... ON, GROUP BY,
    HAVING, QUALIFY, ORDER BY, a window's own PARTITION BY -- has no output
    column, so the upstream column name is what the hit carries. One hit per
    clause per model: a predicate that names the column three times is still
    one reason the model is affected.
    """
    try:
        references = extract_column_references(
            model.query,
            model.depends_on,
            reference_schema,
            ast=getattr(model, "ast", None),
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("Could not index column references for %s: %s", model_name, e)
        return []

    hits: list[dict[str, str]] = []
    seen: set[str] = set()
    for ref in references:
        if ref.clause == "select":
            continue
        if ref.table != target or ref.column != column:
            continue
        if ref.clause in seen:
            continue
        seen.add(ref.clause)
        hits.append({"model": model_name, "column": column, "clause": ref.clause})
    return hits


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
