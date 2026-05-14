"""Pipeline orchestration: sequential and parallel transform runners."""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import duckdb
from rich.console import Console

from havn.engine.database import ensure_meta_table, log_run

from .discovery import (
    _compute_upstream_hash,
    _has_changed,
    _update_state,
    build_dag,
    build_dag_tiers,
    discover_models,
)
from .execution import _execute_single_model, execute_model
from .models import SQLModel
from .quality import (
    _save_assertions,
    _save_profile,
    profile_model,
    run_assertions,
)

console = Console()
logger = logging.getLogger("havn.transform")


def run_transform(
    conn: duckdb.DuckDBPyConnection,
    transform_dir: Path,
    targets: list[str] | None = None,
    force: bool = False,
    parallel: bool = False,
    max_workers: int = 4,
    db_path: str | None = None,
    project_dir: Path | None = None,
    rewind_config: object | None = None,
    run_id: str | None = None,
    pipeline_run_id: str | None = None,
    db_config: object | None = None,
) -> dict[str, str]:
    """Run the full transformation pipeline.

    Args:
        conn: DuckDB connection
        transform_dir: Path to transform/ directory
        targets: Specific models to run (None = all)
        force: Force rebuild even if unchanged
        parallel: Enable parallel execution of independent models
        max_workers: Max number of parallel workers
        db_path: Explicit database path (required for parallel mode)
        project_dir: Project root (for snapshot capture)
        rewind_config: RewindConfig from project settings
        run_id: Pipeline run ID (for snapshot tagging)
        pipeline_run_id: Shared ID grouping all model executions in this pipeline run

    Returns:
        Dict of model_name -> status ("built", "skipped", "error")
    """
    # Generate a pipeline_run_id if not provided, so all models share the same group
    if pipeline_run_id is None:
        pipeline_run_id = str(uuid.uuid4())

    ensure_meta_table(conn)
    models = discover_models(transform_dir)

    if not models:
        console.print("[yellow]No SQL models found in transform/[/yellow]")
        return {}

    # Filter to targets if specified
    if targets and targets != ["all"]:
        target_set = set(targets)
        models = [m for m in models if m.full_name in target_set or m.name in target_set]
        if not models:
            all_names = [m.full_name for m in discover_models(transform_dir)]
            console.print(f"[yellow]No models matched targets: {', '.join(targets)}[/yellow]")
            if all_names:
                console.print(f"[dim]Available models: {', '.join(all_names)}[/dim]")
            return {}

    if parallel:
        return _run_transform_parallel(
            conn, models, force, max_workers, db_path=db_path,
            project_dir=project_dir, rewind_config=rewind_config, run_id=run_id,
            pipeline_run_id=pipeline_run_id, db_config=db_config,
        )
    return _run_transform_sequential(
        conn, models, force,
        project_dir=project_dir, rewind_config=rewind_config, run_id=run_id,
        pipeline_run_id=pipeline_run_id,
    )


def _evaluate_deny_rules(
    models: list[SQLModel],
    project_dir: Path | None,
) -> dict[str, str]:
    """Return a dict of ``{model_full_name: reason}`` for every model that
    violates a project-level deny rule.

    Loaded once per pipeline run. Failure to load the project config is
    treated as "no rules" rather than aborting — denial is opt-in via
    ``policies.deny`` and should never break a previously working build
    that didn't declare any.
    """
    if project_dir is None:
        return {}
    try:
        from havn.config import load_project
        cfg = load_project(project_dir)
    except Exception as e:
        logger.debug("Could not load project config for deny rules: %s", e)
        return {}
    deny_rules = list(cfg.policies.deny) if cfg.policies and cfg.policies.deny else []
    if not deny_rules:
        return {}

    import sqlglot
    from sqlglot import exp as _exp

    out: dict[str, str] = {}
    for model in models:
        try:
            parsed = sqlglot.parse_one(model.query, read="duckdb")
        except Exception:
            continue  # parse errors surface elsewhere
        schema_lower = model.schema.lower()
        referenced: set[str] = set()
        for col in parsed.find_all(_exp.Column):
            if col.name:
                referenced.add(col.name.lower())
        for rule in deny_rules:
            forbidden = {s.lower() for s in (rule.forbid_in_schemas or [])}
            if schema_lower not in forbidden:
                continue
            col = (rule.column or "").lower()
            if col and col in referenced:
                reason = f" ({rule.reason})" if rule.reason else ""
                out[model.full_name] = (
                    f"column {rule.column!r} forbidden in schema "
                    f"{model.schema!r}{reason}"
                )
                break
    return out


def _run_transform_sequential(
    conn: duckdb.DuckDBPyConnection,
    models: list[SQLModel],
    force: bool,
    project_dir: Path | None = None,
    rewind_config: object | None = None,
    run_id: str | None = None,
    pipeline_run_id: str | None = None,
) -> dict[str, str]:
    """Run models sequentially (original behavior + assertions + profiling)."""
    ordered = build_dag(models)
    model_map = {m.full_name: m for m in ordered}
    # Collect profiles for anomaly detection at end of run
    _run_profiles: dict[str, object] = {}

    # Compute upstream hashes
    for model in ordered:
        model.upstream_hash = _compute_upstream_hash(model, model_map)

    results: dict[str, str] = {}
    # Track models that errored or had a severity=error assertion failure
    # so we can skip their descendants (matches the contract documented for
    # @severity).
    blocked: set[str] = set()

    # Apply project-level deny rules to seed the blocked set. Done here so
    # the same logic runs whether we're sequential or parallel — and so
    # forbidden models never get sent to a worker.
    for full_name, reason in _evaluate_deny_rules(ordered, project_dir).items():
        console.print(f"  [red]deny[/red]  [bold]{full_name}[/bold]: {reason}")
        results[full_name] = "policy_denied"
        blocked.add(full_name)
        try:
            log_run(
                conn, "transform", full_name, "error",
                0, 0,
                error=f"policy_denied: {reason}",
                pipeline_run_id=pipeline_run_id,
            )
        except Exception:
            pass

    for model in ordered:
        if model.full_name in results:
            # Already handled (e.g. policy-denied above).
            continue
        changed = force or _has_changed(conn, model)
        label = f"[bold]{model.full_name}[/bold] ({model.materialized})"

        # If any upstream is blocked (error / failed-error-assertion / stale
        # source), skip this model with a clear reason.
        upstream_blocked = [d for d in model.depends_on if d in blocked]
        if upstream_blocked:
            console.print(
                f"  [yellow]skip[/yellow]  {label}: upstream blocked "
                f"({', '.join(upstream_blocked)})"
            )
            results[model.full_name] = "skipped_upstream_blocked"
            blocked.add(model.full_name)
            try:
                log_run(
                    conn, "transform", model.full_name, "skipped",
                    0, 0,
                    error=f"upstream blocked: {', '.join(upstream_blocked)}",
                    pipeline_run_id=pipeline_run_id,
                )
            except Exception:
                pass
            continue

        if not changed:
            console.print(f"  [dim]skip[/dim]  {label}")
            results[model.full_name] = "skipped"
            try:
                log_run(conn, "transform", model.full_name, "skipped", 0, 0, pipeline_run_id=pipeline_run_id)
            except Exception:
                pass
            continue

        # @source_freshness pre-check: bail out early if any error-severity
        # source spec is stale. Warnings still execute.
        if model.source_freshness:
            from .quality import _save_source_freshness, check_source_freshness

            sf_results = check_source_freshness(conn, model.source_freshness)
            _save_source_freshness(conn, model, sf_results)
            blocking = [
                r for r in sf_results
                if r["is_stale"] and r.get("severity", "error") == "error"
            ]
            for r in sf_results:
                if r["is_stale"]:
                    age = (
                        f"{r['age_seconds']:.0f}s" if r.get("age_seconds") is not None else "n/a"
                    )
                    sev_color = "red" if r.get("severity", "error") == "error" else "yellow"
                    console.print(
                        f"         [{sev_color}]stale[/{sev_color}]  source: {r['table']} "
                        f"(age={age}, max={r['max_age_seconds']}s)"
                    )
            if blocking:
                results[model.full_name] = "source_stale"
                blocked.add(model.full_name)
                try:
                    log_run(
                        conn, "transform", model.full_name, "skipped",
                        0, 0,
                        error=f"source stale: {', '.join(b['table'] for b in blocking)}",
                        pipeline_run_id=pipeline_run_id,
                    )
                except Exception:
                    pass
                continue

        try:
            duration_ms, row_count = execute_model(conn, model)
            _update_state(conn, model, duration_ms, row_count)
            log_run(conn, "transform", model.full_name, "success", duration_ms, row_count, pipeline_run_id=pipeline_run_id)

            suffix = f" ({row_count:,} rows, {duration_ms}ms)" if row_count else f" ({duration_ms}ms)"
            console.print(f"  [green]done[/green]  {label}{suffix}")

            # Capture snapshot for Pipeline Rewind
            if project_dir and run_id:
                try:
                    from havn.engine.snapshots import RewindConfig, capture_snapshot
                    rw_cfg = None
                    if rewind_config is not None:
                        rw_cfg = RewindConfig(
                            enabled=getattr(rewind_config, "enabled", True),
                            retention=getattr(rewind_config, "retention", "7d"),
                            max_storage=getattr(rewind_config, "max_storage", None),
                            dedup=getattr(rewind_config, "dedup", True),
                            exclude=getattr(rewind_config, "exclude", []),
                        )
                    capture_snapshot(project_dir, conn, model.full_name, run_id, row_count, rw_cfg)
                except Exception as snap_err:
                    logger.warning("Snapshot capture failed for %s: %s", model.full_name, snap_err)

            # Run data quality assertions (and the synthesised @grain check
            # if model.grain is set — both are evaluated by run_assertions).
            if model.assertions or model.grain:
                assertion_results = run_assertions(conn, model)
                _save_assertions(conn, model, assertion_results)
                for ar in assertion_results:
                    if ar.passed:
                        console.print(f"         [green]pass[/green]  assert: {ar.expression}")
                    else:
                        sev = ar.severity or "error"
                        sev_color = "red" if sev == "error" else "yellow"
                        sev_label = "FAIL" if sev == "error" else "WARN"
                        console.print(
                            f"         [{sev_color}]{sev_label}[/{sev_color}]  "
                            f"assert: {ar.expression} ({ar.detail})"
                        )

                failed_error = [ar for ar in assertion_results if not ar.passed and (ar.severity or "error") == "error"]
                if failed_error:
                    # Severity=error assertions halt this model AND its
                    # descendants — keeping bad data from cascading downstream.
                    results[model.full_name] = "assertion_failed"
                    blocked.add(model.full_name)
                    continue

            # Auto-profile for tables
            if model.materialized in ("table", "incremental"):
                profile = profile_model(conn, model)
                _save_profile(conn, model, profile)
                _run_profiles[model.full_name] = profile
                null_alerts = [
                    col for col, pct in profile.null_percentages.items()
                    if pct > 50.0
                ]
                if null_alerts:
                    console.print(
                        f"         [yellow]warn[/yellow]  high nulls: "
                        f"{', '.join(f'{c}({profile.null_percentages[c]}%)' for c in null_alerts)}"
                    )

            results[model.full_name] = "built"

        except Exception as e:
            log_run(conn, "transform", model.full_name, "error", error=str(e), pipeline_run_id=pipeline_run_id)
            console.print(f"  [red]fail[/red]  {label}: {e}")
            results[model.full_name] = "error"
            blocked.add(model.full_name)

    # Run anomaly detection on collected profiles
    if _run_profiles:
        try:
            from havn.engine.anomaly import detect_all_anomalies, log_anomalies, alert_anomalies
            anomalies = detect_all_anomalies(conn, _run_profiles)
            if anomalies:
                log_anomalies(conn, anomalies)
                for a in anomalies:
                    console.print(
                        f"         [yellow]anomaly[/yellow]  {a.model}: {a.message} (z={a.z_score})"
                    )
                # Send alerts if configured
                try:
                    if project_dir:
                        from havn.config import load_project
                        cfg = load_project(project_dir)
                        alert_anomalies(anomalies, cfg.alerts, conn)
                except Exception as alert_err:
                    logger.debug("Anomaly alerting skipped: %s", alert_err)
        except Exception as anom_err:
            logger.debug("Anomaly detection skipped: %s", anom_err)

    return results


def _run_transform_parallel(
    conn: duckdb.DuckDBPyConnection,
    models: list[SQLModel],
    force: bool,
    max_workers: int,
    db_path: str | None = None,
    project_dir: Path | None = None,
    rewind_config: object | None = None,
    run_id: str | None = None,
    pipeline_run_id: str | None = None,
    db_config: object | None = None,
) -> dict[str, str]:
    """Run models in parallel by DAG tiers.

    Models within the same tier are independent and can execute concurrently.
    Each tier must complete before the next one starts.
    Assertion failures in a tier block the next tier.
    """
    tiers = build_dag_tiers(models)
    model_map = {m.full_name: m for m in models}

    # Compute upstream hashes
    ordered = build_dag(models)
    for model in ordered:
        model.upstream_hash = _compute_upstream_hash(model, model_map)

    # Pre-create every target schema on the main connection BEFORE any
    # parallel worker starts. Without this, two workers in the same tier
    # racing on the same schema both run `CREATE SCHEMA IF NOT EXISTS bronze`
    # and DuckDB raises "Catalog write-write conflict on create with bronze".
    # `IF NOT EXISTS` is not enough — the conflict is on the catalog write
    # itself, not on the existence check.
    schemas_to_create = {m.schema for m in models if m.schema}
    for schema in sorted(schemas_to_create):
        try:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        except Exception as e:
            logger.debug("Pre-create schema %s failed: %s", schema, e)

    # DuckLake with a local file catalog cannot be attached twice within the
    # same process — parallel workers would all try to attach the same file
    # and collide. Fall back to sequential execution for that case.
    if db_config is not None and getattr(db_config, "backend", None) == "ducklake":
        catalog = getattr(db_config, "catalog", "") or ""
        if not catalog.startswith("postgres:"):
            console.print("[dim]DuckLake file catalog: running transforms sequentially[/dim]")
            return _run_transform_sequential(
                conn, models, force,
                project_dir=project_dir, rewind_config=rewind_config, run_id=run_id,
                pipeline_run_id=pipeline_run_id,
            )

    # Resolve database path explicitly (only used when db_config is None).
    db_path_str = db_path
    if db_config is None and not db_path_str:
        # Fall back to extracting from connection
        try:
            result = conn.execute("SELECT current_setting('duckdb_database_file')").fetchone()
            db_path_str = result[0] if result and result[0] else None
        except Exception as e:
            logger.debug("Could not extract db path from connection: %s", e)
    if db_config is None and not db_path_str:
        console.print("[yellow]Cannot determine database path, falling back to sequential[/yellow]")
        return _run_transform_sequential(
            conn, models, force,
            project_dir=project_dir, rewind_config=rewind_config, run_id=run_id,
            pipeline_run_id=pipeline_run_id,
        )

    results: dict[str, str] = {}
    total_tiers = len(tiers)

    # Apply deny rules in the parallel path too. We pre-populate ``results``
    # with denied models so every tier-skip check below treats them as blocking.
    deny_results = _evaluate_deny_rules(models, project_dir)
    for full_name, reason in deny_results.items():
        console.print(f"  [red]deny[/red]  [bold]{full_name}[/bold]: {reason}")
        results[full_name] = "policy_denied"
        try:
            log_run(
                conn, "transform", full_name, "error",
                0, 0,
                error=f"policy_denied: {reason}",
                pipeline_run_id=pipeline_run_id,
            )
        except Exception:
            pass

    # Track which models have failed so we can block ONLY their actual
    # descendants, not unrelated siblings. Pre-seed with policy denials
    # since those were applied before any tier ran.
    failed_models: set[str] = {
        name for name, status in results.items()
        if status in ("error", "assertion_failed", "policy_denied")
    }

    def _is_blocked(model: SQLModel) -> str | None:
        """Return the upstream that blocks this model, or None."""
        for dep in model.depends_on:
            if dep in failed_models:
                return dep
            # Walk transitively in case a deeper ancestor failed.
            if dep in model_map:
                upstream_block = _is_blocked(model_map[dep])
                if upstream_block:
                    return upstream_block
        return None

    for tier_idx, tier in enumerate(tiers, 1):
        # Only skip models whose actual upstreams failed.
        new_tier: list[SQLModel] = []
        for model in tier:
            if model.full_name in results:
                continue  # already marked
            blocker = _is_blocked(model)
            if blocker is not None:
                console.print(
                    f"  [dim]skip[/dim]  [bold]{model.full_name}[/bold] "
                    f"(upstream failure: {blocker})"
                )
                results[model.full_name] = "skipped_upstream_blocked"
                failed_models.add(model.full_name)
                try:
                    log_run(
                        conn, "transform", model.full_name, "skipped",
                        0, 0,
                        error=f"upstream blocked: {blocker}",
                        pipeline_run_id=pipeline_run_id,
                    )
                except Exception:
                    pass
                continue
            new_tier.append(model)
        tier = new_tier
        if not tier:
            continue

        # Filter denied models out of the tier before any work or
        # parallel dispatch.
        tier = [m for m in tier if m.full_name not in results]
        if not tier:
            continue

        if len(tier) > 1:
            console.print(f"  [dim]tier {tier_idx}/{total_tiers}[/dim] ({len(tier)} models in parallel)")

        if len(tier) == 1:
            # Single model — run in the main connection
            model = tier[0]
            changed = force or _has_changed(conn, model)
            label = f"[bold]{model.full_name}[/bold] ({model.materialized})"

            if not changed:
                console.print(f"  [dim]skip[/dim]  {label}")
                results[model.full_name] = "skipped"
                try:
                    log_run(conn, "transform", model.full_name, "skipped", 0, 0, pipeline_run_id=pipeline_run_id)
                except Exception:
                    pass
                continue

            try:
                duration_ms, row_count = execute_model(conn, model)
                _update_state(conn, model, duration_ms, row_count)
                log_run(conn, "transform", model.full_name, "success", duration_ms, row_count, pipeline_run_id=pipeline_run_id)

                # Assertions (and synthesised @grain check, if any)
                if model.assertions or model.grain:
                    ar_results = run_assertions(conn, model)
                    _save_assertions(conn, model, ar_results)
                    failed_asserts = [
                        ar for ar in ar_results
                        if not ar.passed and (ar.severity or "error") == "error"
                    ]
                    if failed_asserts:
                        for ar in failed_asserts:
                            console.print(f"         [red]FAIL[/red]  assert: {ar.expression} ({ar.detail})")
                        results[model.full_name] = "assertion_failed"
                        failed_models.add(model.full_name)
                        continue

                # Profile
                if model.materialized in ("table", "incremental"):
                    profile = profile_model(conn, model)
                    _save_profile(conn, model, profile)

                suffix = f" ({row_count:,} rows, {duration_ms}ms)" if row_count else f" ({duration_ms}ms)"
                console.print(f"  [green]done[/green]  {label}{suffix}")
                results[model.full_name] = "built"

                # Capture snapshot for Pipeline Rewind
                if project_dir and run_id:
                    try:
                        from havn.engine.snapshots import RewindConfig as _RC, capture_snapshot as _cs
                        _rw = None
                        if rewind_config is not None:
                            _rw = _RC(
                                enabled=getattr(rewind_config, "enabled", True),
                                retention=getattr(rewind_config, "retention", "7d"),
                                max_storage=getattr(rewind_config, "max_storage", None),
                                dedup=getattr(rewind_config, "dedup", True),
                                exclude=getattr(rewind_config, "exclude", []),
                            )
                        _cs(project_dir, conn, model.full_name, run_id, row_count, _rw)
                    except Exception as snap_err:
                        logger.warning("Snapshot capture failed for %s: %s", model.full_name, snap_err)

            except Exception as e:
                log_run(conn, "transform", model.full_name, "error", error=str(e), pipeline_run_id=pipeline_run_id)
                console.print(f"  [red]fail[/red]  {label}: {e}")
                results[model.full_name] = "error"
                failed_models.add(model.full_name)
        else:
            # Multiple models — run in parallel with separate connections
            # Collect ALL results from all futures before reporting
            tier_results: list[tuple[str, ModelResult]] = []
            with ThreadPoolExecutor(max_workers=min(max_workers, len(tier))) as executor:
                futures = {
                    executor.submit(
                        _execute_single_model,
                        db_path_str, model, force, model_map,
                        db_config, project_dir, pipeline_run_id,
                    ): model
                    for model in tier
                }
                for future in as_completed(futures):
                    tier_results.append(future.result())

            # Report all results from this tier
            for model_name, model_result in tier_results:
                label = f"[bold]{model_name}[/bold]"
                if model_result.status == "skipped":
                    console.print(f"  [dim]skip[/dim]  {label}")
                elif model_result.status == "built":
                    suffix = ""
                    if model_result.row_count:
                        suffix = f" ({model_result.row_count:,} rows, {model_result.duration_ms}ms)"
                    else:
                        suffix = f" ({model_result.duration_ms}ms)"
                    console.print(f"  [green]done[/green]  {label}{suffix}")
                elif model_result.status == "assertion_failed":
                    console.print(f"  [red]FAIL[/red]  {label}: assertion(s) failed")
                else:
                    console.print(f"  [red]fail[/red]  {label}: {model_result.error}")

                results[model_name] = model_result.status
                if model_result.status in ("error", "assertion_failed"):
                    failed_models.add(model_name)

                # Capture snapshot for Pipeline Rewind (parallel tier)
                if project_dir and run_id and model_result.status == "built":
                    try:
                        from havn.engine.snapshots import RewindConfig as _RC, capture_snapshot as _cs
                        _rw = None
                        if rewind_config is not None:
                            _rw = _RC(
                                enabled=getattr(rewind_config, "enabled", True),
                                retention=getattr(rewind_config, "retention", "7d"),
                                max_storage=getattr(rewind_config, "max_storage", None),
                                dedup=getattr(rewind_config, "dedup", True),
                                exclude=getattr(rewind_config, "exclude", []),
                            )
                        _cs(project_dir, conn, model_name, run_id,
                            model_result.row_count, _rw)
                    except Exception as snap_err:
                        logger.warning("Snapshot capture failed for %s: %s", model_name, snap_err)

    return results
