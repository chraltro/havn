"""SQL transformation engine.

Parses SQL files with config comments, builds a DAG, executes in dependency order.
Handles change detection via content hashing, incremental models, data quality
assertions, auto-profiling, freshness monitoring, and parallel execution.

This package re-exports all public symbols so existing imports continue to work:
    from havn.engine.transform import run_transform, discover_models, SQLModel, ...
"""

from __future__ import annotations

# Data models
from .models import (
    AssertionResult,
    ModelResult,
    ProfileResult,
    SQLModel,
    ValidationError,
    _hash_content,
)

# Discovery and DAG
from .discovery import (
    _compute_upstream_hash,
    _has_changed,
    _update_state,
    build_dag,
    build_dag_tiers,
    discover_all_models,
    discover_models,
    discover_package_models,
)

# Data quality and profiling
from .quality import (
    _evaluate_assertion,
    _save_assertions,
    _save_profile,
    failing_rows_sql,
    profile_model,
    run_assertions,
)

# Execution
from .execution import (
    BatchRange,
    MicrobatchError,
    SchemaChangeError,
    SnapshotError,
    SnapshotSettings,
    _execute_incremental,
    _execute_microbatch,
    _execute_single_model,
    _execute_snapshot,
    compute_batch_windows,
    execute_model,
    parse_event_time,
    resolve_query,
    shift_batch,
    substitute_batch_window,
    truncate_to_batch,
    snapshot_settings_for,
    snapshot_settings_from_config,
)

# Ephemeral model inlining
from .inline import (
    EphemeralInlineError,
    cte_name_for,
    inline_ephemeral,
)

# Persisted model column schemas
from .columns import (
    load_model_columns,
    save_model_columns,
)

# Shadow bind pass
from .bind import (
    BindError,
    BindResult,
    ancestor_closure,
    bind_models,
)

# Analysis, validation, lineage, freshness
from .analysis import (
    check_freshness,
    extract_column_lineage,
    impact_analysis,
    validate_models,
)

# Orchestration
from .orchestration import (
    run_transform,
)

__all__ = [
    # Models
    "AssertionResult",
    "ModelResult",
    "ProfileResult",
    "SQLModel",
    "ValidationError",
    # Discovery
    "build_dag",
    "build_dag_tiers",
    "discover_all_models",
    "discover_models",
    "discover_package_models",
    # Quality
    "failing_rows_sql",
    "profile_model",
    "run_assertions",
    # Execution
    "SchemaChangeError",
    "execute_model",
    "BatchRange",
    "MicrobatchError",
    "_execute_microbatch",
    "compute_batch_windows",
    "parse_event_time",
    "shift_batch",
    "substitute_batch_window",
    "truncate_to_batch",
    "SnapshotError",
    "SnapshotSettings",
    "_execute_snapshot",
    "snapshot_settings_for",
    "snapshot_settings_from_config",
    "resolve_query",
    "run_transform",
    # Ephemeral inlining
    "EphemeralInlineError",
    "cte_name_for",
    "inline_ephemeral",
    # Bind
    "BindError",
    "BindResult",
    "ancestor_closure",
    "bind_models",
    "load_model_columns",
    "save_model_columns",
    # Analysis
    "check_freshness",
    "extract_column_lineage",
    "impact_analysis",
    "validate_models",
]
