"""Performance advisor: per-build plans and timings, regressions, advice.

- :mod:`.capture` profiles a build's main statement with DuckDB's profiler.
- :mod:`.store` keeps one ``_havn.model_perf`` row per build, with retention.
- :mod:`.regression` flags builds slower than their own history (median/MAD)
  and diffs the plan of a fast build against the slow one.
- :mod:`.advice` runs explainable rules over the DAG, history and plans.
- :mod:`.critical_path` finds the chain of models that set a run's length.

Builds reach all of this through :mod:`havn.engine.instrumentation`, which
``build_one_model`` and the parallel worker both call.
"""

from __future__ import annotations

from .advice import AdviceItem, RULES, compute_advice, set_advice_state
from .capture import BuildCapture, activate, compact_plan, run_build_statement, top_operators
from .critical_path import critical_path
from .regression import (
    Regression,
    alert_regressions,
    check_build,
    detect_run_regressions,
    diff_plans,
    list_regressions,
    robust_z,
)
from .store import (
    BuildRecord,
    ensure_perf_tables,
    get_build,
    latest_builds,
    model_history,
    prune,
    recent_runs,
    record_build,
    run_builds,
    slowest_models,
    trend,
)

__all__ = [
    "AdviceItem",
    "BuildCapture",
    "BuildRecord",
    "RULES",
    "Regression",
    "activate",
    "alert_regressions",
    "check_build",
    "compact_plan",
    "compute_advice",
    "critical_path",
    "detect_run_regressions",
    "diff_plans",
    "ensure_perf_tables",
    "get_build",
    "latest_builds",
    "list_regressions",
    "model_history",
    "prune",
    "recent_runs",
    "record_build",
    "robust_z",
    "run_build_statement",
    "run_builds",
    "set_advice_state",
    "slowest_models",
    "top_operators",
    "trend",
]
