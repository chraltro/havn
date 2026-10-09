"""Model discovery, DAG building, and change detection."""

from __future__ import annotations

from havn.textio import read_project_text

import hashlib
import logging
from dataclasses import replace
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import TYPE_CHECKING

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
    parse_sql,
    strip_config_comments,
)
from havn.engine.utils import validate_identifier

from .columns import save_model_columns
from .models import SQLModel

if TYPE_CHECKING:
    from havn.config import ProjectConfig
    from havn.engine.packages import PackageRoot

logger = logging.getLogger(__name__)


class DuplicateModelError(ValueError):
    """Two SQL files produce the same ``schema.name``.

    Raised rather than reported, because the project has no single answer for
    what that name means: ``build_dag`` keys models by full name, so one of
    the two files would be dropped and which one won depended on filename
    order. Discovery already raises for a model it cannot use (an invalid
    schema or model identifier), so this follows the same path.
    """


def discover_models(transform_dir: Path) -> list[SQLModel]:
    """Discover all SQL models in the transform directory.

    Convention: folder names map to schemas.
    transform/bronze/customers.sql -> schema=bronze, name=customers

    Raises:
        DuplicateModelError: two files resolve to the same ``schema.name``.
        ValueError: a file's schema or model name is not a safe identifier.
    """
    models = []
    if not transform_dir.exists():
        return models

    # full_name -> the file that claimed it first
    claimed: dict[str, Path] = {}

    for sql_file in sorted(transform_dir.rglob("*.sql")):
        sql = read_project_text(sql_file)
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
        # Parsed once here and handed to the model below, so validation, the
        # deny-rule check and column lineage reuse this tree instead of
        # parsing the same SQL again.
        ast = parse_sql(query)
        folder_schema_tmp = sql_file.relative_to(transform_dir).parent.name or "public"
        own_schema_tmp = config.get("schema", folder_schema_tmp)
        own_name_tmp = sql_file.stem
        auto_refs = extract_table_refs(
            query, exclude=f"{own_schema_tmp}.{own_name_tmp}", ast=ast
        )
        if depends:
            merged = list(depends)
            seen = set(depends)
            for ref in auto_refs:
                if ref not in seen:
                    merged.append(ref)
                    seen.add(ref)
            depends = merged
        else:
            depends = auto_refs

        # Schema from folder name (convention) or config override
        rel = sql_file.relative_to(transform_dir)
        folder_schema = rel.parent.name if rel.parent.name else "public"
        schema = config.get("schema", folder_schema)
        name = sql_file.stem
        # Validate identifiers at discovery time to prevent SQL injection downstream
        validate_identifier(schema, f"schema for {sql_file.name}")
        validate_identifier(name, f"model name for {sql_file.name}")

        full_name = f"{schema}.{name}"
        previous = claimed.get(full_name)
        if previous is not None:
            raise DuplicateModelError(
                f"Duplicate model '{full_name}': both "
                f"{previous} and {sql_file} produce it. "
                "Rename one of the files, or point one at another schema "
                "with @config schema=."
            )
        claimed[full_name] = sql_file

        materialized = config.get("materialized", "view")
        unique_key = config.get("unique_key")
        incremental_strategy = config.get("incremental_strategy", "delete+insert")
        incremental_filter = config.get("incremental_filter")
        partition_by = config.get("partition_by")
        watermark = config.get("watermark")
        on_schema_change = config.get("on_schema_change", "append_new_columns")
        tags = [t.strip() for t in config.get("tags", "").split(",") if t.strip()]
        # Snapshot (SCD2) settings. Read for every model so `validate_models`
        # can complain about them on a model that is not a snapshot; execution
        # only looks at them when materialized=snapshot.
        strategy = config.get("strategy", "check")
        updated_at = config.get("updated_at")
        check_cols = config.get("check_cols")
        hard_deletes = config.get("hard_deletes", "ignore")
        # Microbatch settings. `lookback` is kept as an int here so the model
        # carries a usable value; a non-numeric one is reported by
        # `validate_models` rather than crashing discovery.
        event_time = config.get("event_time")
        batch_size = config.get("batch_size")
        begin = config.get("begin")
        raw_lookback = config.get("lookback")
        try:
            lookback = int(raw_lookback) if raw_lookback is not None else 1
        except ValueError:
            lookback = 1

        model = SQLModel(
            path=sql_file,
            name=name,
            schema=schema,
            full_name=full_name,
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
            on_schema_change=on_schema_change,
            strategy=strategy,
            updated_at=updated_at,
            check_cols=check_cols,
            hard_deletes=hard_deletes,
            event_time=event_time,
            batch_size=batch_size,
            begin=begin,
            lookback=lookback,
            grain=grain,
            owner=owner,
            source_freshness=source_freshness,
            tags=tags,
        )
        if ast is not None:
            model.ast = ast
        models.append(model)

    return models


def discover_package_models(root: PackageRoot) -> list[SQLModel]:
    """Discover one installed package's models, namespaced into the project.

    A package's models are written as if the package were the whole project:
    ``transform/silver/customers.sql`` says ``schema=silver`` and its siblings
    say ``FROM silver.customers``. Both halves are rewritten here, once, at
    discovery time:

    - the schema becomes ``<pkg>_<schema>`` (or whatever the package's
      ``havn_package.yml`` maps it to), so a package can never quietly take a
      name the project was already using;
    - references to the package's *own* models are rewritten to match, so the
      package author never writes the prefix and the project never has to
      care that it exists.

    References to anything else -- ``landing.*``, a table the package expects
    the host project to provide -- are left exactly as written.
    """
    from havn.engine.sql_rewrite import SQLRewriteError, find_table_refs, rewrite_table_refs

    raw = discover_models(root.transform_dir)
    if not raw:
        return []

    mapping: dict[str, str] = {}
    for m in raw:
        target_schema = root.schema_for(m.schema)
        validate_identifier(target_schema, f"schema for package '{root.name}'")
        mapping[f"{m.schema}.{m.name}".lower()] = f"{target_schema}.{m.name}"

    models: list[SQLModel] = []
    for m in raw:
        query = m.query
        try:
            refs = find_table_refs(query)
        except SQLRewriteError:
            # Unparseable SQL cannot be rewritten. Leave it alone: the model
            # will fail its own build with a real error, which is more useful
            # than a rewrite error standing in front of it.
            refs = []
        if any(ref in mapping for ref in refs):
            try:
                query = rewrite_table_refs(query, mapping)
            except SQLRewriteError as exc:
                raise ValueError(
                    f"Package '{root.name}': could not rewrite references in "
                    f"{m.path}: {exc}"
                ) from exc

        target_schema = root.schema_for(m.schema)
        models.append(
            replace(
                m,
                schema=target_schema,
                full_name=f"{target_schema}.{m.name}",
                query=query,
                depends_on=[mapping.get(d.lower(), d) for d in m.depends_on],
                package=root.name,
            )
        )
    return models


def discover_all_models(
    project_dir: Path,
    config: ProjectConfig | None = None,
) -> list[SQLModel]:
    """Every model the project builds: its own, plus each installed package's.

    This is what every caller that runs or lists the DAG should use.
    :func:`discover_models` stays the single-directory primitive underneath
    it, for callers that genuinely mean one directory.

    Packages come from ``havn_packages.lock``, so a project without one pays
    a single ``stat`` and gets byte-identical output to ``discover_models``.

    Raises:
        DuplicateModelError: a package model lands on a name the project (or
            an earlier package) already uses. That only happens once a
            package's manifest has overridden the ``<pkg>_`` schema prefix,
            and it is the same error a project would get from two of its own
            files claiming one name.
    """
    from havn.engine.packages import package_roots

    project_dir = Path(project_dir)
    models = discover_models(project_dir / "transform")

    roots = package_roots(project_dir)
    if config is not None:
        installed = {r.name for r in roots}
        for declared in getattr(config, "packages", []) or []:
            if declared.name not in installed:
                logger.warning(
                    "Package '%s' is declared in project.yml but not installed; "
                    "run 'havn packages install'",
                    declared.name,
                )
    if not roots:
        return models

    claimed: dict[str, Path] = {m.full_name: m.path for m in models}
    for root in roots:
        for model in discover_package_models(root):
            previous = claimed.get(model.full_name)
            if previous is not None:
                raise DuplicateModelError(
                    f"Duplicate model '{model.full_name}': both {previous} and "
                    f"{model.path} (package '{root.name}') produce it. "
                    f"Remove the schema override in {root.name}'s "
                    "havn_package.yml, or rename the project model."
                )
            claimed[model.full_name] = model.path
            models.append(model)
    return models


class CircularDependencyError(ValueError):
    """The model DAG contains a dependency cycle.

    Raised instead of letting graphlib's CycleError escape as a bare traceback
    through the CLI, the API, and the scheduler.
    """


def _format_cycle(err: CycleError, model_map: dict[str, SQLModel]) -> str:
    """Turn a graphlib CycleError into a message naming the model files."""
    cycle = err.args[1] if len(err.args) > 1 else []
    parts = []
    for name in cycle:
        model = model_map.get(name)
        parts.append(f"{name} ({model.path})" if model else name)
    chain = " -> ".join(parts) if parts else "unknown"
    return (
        "Circular dependency between models: "
        + chain
        + ". Break the cycle by removing one of the references "
        "(check @depends_on lines as well as FROM/JOIN clauses)."
    )


def build_dag(models: list[SQLModel]) -> list[SQLModel]:
    """Sort models in dependency order using topological sort."""
    model_map = {m.full_name: m for m in models}
    sorter: TopologicalSorter[str] = TopologicalSorter()

    for m in models:
        # Filter dependencies to only those that are known models
        # (landing.* tables won't be in the model list — that's fine)
        known_deps = [d for d in m.depends_on if d in model_map]
        sorter.add(m.full_name, *known_deps)

    try:
        ordered = list(sorter.static_order())
    except CycleError as e:
        raise CircularDependencyError(_format_cycle(e, model_map)) from e
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

    try:
        sorter.prepare()
    except CycleError as e:
        raise CircularDependencyError(_format_cycle(e, model_map)) from e
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


def _needs_build(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
) -> bool:
    """Whether a transform run should build ``model`` rather than skip it.

    Wider than ``_has_changed``, which only says whether the definition
    moved and backs "edited since build" in the UI and the
    ``state:modified`` selector. A model whose last build an error
    assertion rejected has an unchanged definition but must still be
    rebuilt and re-checked, or a rerun would wave it through.
    """
    # Snapshot and incremental models (microbatch included) exist to pick up
    # new source data, and their SQL does not change between runs -- so
    # judging them by their definition skipped them forever: a scheduled run
    # recorded no new history and ingested no new windows. They are built to
    # be re-run cheaply, so they always run.
    if model.materialized == "snapshot":
        return True
    if model.materialized == "incremental" and _rerun_safe(model):
        return True
    return _has_changed(conn, model) or _is_blocked(conn, model) or _inputs_newer(conn, model)


# Run types that load raw data into the warehouse outside the model DAG.
_SOURCE_LOAD_RUN_TYPES = ("ingest", "import", "connector_sync", "seed")


def _inputs_newer(conn: duckdb.DuckDBPyConnection, model: SQLModel) -> bool:
    """Whether a table model's inputs hold newer data than its last build.

    A table's SQL does not change when its inputs get new rows, so judged by
    its definition alone it stayed on old data until ``--force``:

    - an input model rebuilt in an earlier, narrower run (``havn transform
      silver.x`` refreshes silver.x but not the gold table reading it);
    - a raw source (``landing.*``) reloaded by an ingest, import, connector
      sync or seed.

    A dependency with a ``model_state`` row is an input model and counts when
    it was built after this one. Anything else is a raw source and counts
    when a successful load was logged after this model's build. That half is
    coarse on purpose: the run log says *that* raw data arrived, not which
    table, and fingerprinting every source on every run would mean a full
    scan each time. Data written into ``landing`` outside havn (another tool,
    an ad-hoc query) still needs ``--force``.

    Within one run, ``_parent_built`` already covers parents rebuilt in that
    run. Views read their inputs live and never need this, and a plain
    ``append`` incremental would duplicate rows if re-run, so only tables
    are considered.
    """
    if model.materialized != "table" or not model.depends_on:
        return False
    deps = sorted(model.depends_on)
    try:
        row = conn.execute(
            "SELECT last_run_at FROM _havn.model_state WHERE model_path = ?",
            [model.full_name],
        ).fetchone()
        if row is None or row[0] is None:
            return False  # never built: _has_changed already builds it
        built_at = row[0]
        placeholders = ", ".join("?" for _ in deps)
        # Views and ephemerals hold no data of their own: a view reads its
        # inputs live and is only rebuilt when its SQL changes, an ephemeral
        # is recorded on every run without being built. Their timestamps say
        # nothing about their data, so they are treated like the raw sources
        # they read through to. (A table reached through a view that was
        # rebuilt in a narrower run is not seen this way; --force covers it.)
        dep_rows = conn.execute(
            f"SELECT model_path, last_run_at FROM _havn.model_state "
            f"WHERE model_path IN ({placeholders}) AND materialized_as NOT IN ('ephemeral', 'view')",
            deps,
        ).fetchall()
        if any(ts is not None and ts > built_at for _, ts in dep_rows):
            return True
        if len(dep_rows) == len(deps):
            return False  # every input is a built model, and none is newer
        types = ", ".join("?" for _ in _SOURCE_LOAD_RUN_TYPES)
        hit = conn.execute(
            f"""
            SELECT 1 FROM _havn.run_log
            WHERE status = 'success' AND run_type IN ({types}) AND started_at > ?
            LIMIT 1
            """,
            [*_SOURCE_LOAD_RUN_TYPES, built_at],
        ).fetchone()
    except duckdb.CatalogException:
        return False
    return hit is not None


def _rerun_safe(model: SQLModel) -> bool:
    """Whether running an incremental model again without new data is a no-op.

    merge and delete+insert replace by key, microbatch reprocesses only open
    windows, and a filtered append reads only rows newer than the target. A
    plain append with no filter re-inserts everything it selects, so running
    it every time would duplicate rows: it keeps the changed-or-blocked rule.
    """
    strategy = (model.incremental_strategy or "delete+insert").lower()
    if strategy == "append":
        return bool(model.incremental_filter)
    return True


def _invalidate_state(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
) -> None:
    """Make the next run rebuild ``model`` and check its assertions again.

    ``_update_state`` runs as soon as the build succeeds, before assertions
    are evaluated, so a model whose severity=error assertion then fails is
    left recorded as up to date. The next run would skip it as unchanged,
    nothing would block its descendants any more, and they would build on
    the data the assertion had just rejected. Marking it blocked makes
    ``_has_changed`` rebuild it; a clean build clears the mark.
    """
    conn.execute("DELETE FROM _havn.model_blocked WHERE model_path = ?", [model.full_name])
    conn.execute("INSERT INTO _havn.model_blocked (model_path) VALUES (?)", [model.full_name])


def _clear_block(conn: duckdb.DuckDBPyConnection, model: SQLModel) -> None:
    """Accept ``model``'s current build: its checks ran and passed.

    Only a passing check clears a block. Building alone does not: the job
    runner and the stream pipeline build without (or without acting on)
    assertions, and clearing on build let a scheduled job reopen the gate.
    """
    try:
        conn.execute("DELETE FROM _havn.model_blocked WHERE model_path = ?", [model.full_name])
    except duckdb.CatalogException:
        pass


def blocked_models(conn: duckdb.DuckDBPyConnection) -> set[str]:
    """Every model whose last build an error assertion rejected."""
    try:
        return {r[0] for r in conn.execute("SELECT model_path FROM _havn.model_blocked").fetchall()}
    except duckdb.CatalogException:
        return set()


def _is_blocked(conn: duckdb.DuckDBPyConnection, model: SQLModel) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM _havn.model_blocked WHERE model_path = ? LIMIT 1",
            [model.full_name],
        ).fetchone()
    except duckdb.CatalogException:
        # A warehouse bootstrapped before the table existed has nothing blocked.
        return False
    return row is not None


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
    save_model_columns(conn, model)
