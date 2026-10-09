"""Data governance: column masking, row-level security, lineage-aware policies.

Every surface that hands warehouse data to a person goes through
:func:`govern_query` (or :func:`run_governed`), which applies the viewer's
masking and row policies -- explicit ones and the ones inherited through
column lineage -- before the query runs, and refuses what it cannot govern.

Submodules:

* ``viewer``    -- the identity policies are evaluated against
* ``catalog``   -- relations, views, macros, and policy inheritance
* ``lineage``   -- per-column lineage, recorded for every model build
* ``rewrite``   -- the query rewriter and its plan-based verification
* ``statements``-- governed execution of whole statements (writes included),
                   for governed Python and the SQL statement API
"""

from __future__ import annotations

from .viewer import SYSTEM, Viewer, viewer_from_user

__all__ = [
    "SYSTEM",
    "Viewer",
    "viewer_from_user",
    "GovernanceError",
    "GovernedQuery",
    "govern_query",
    "run_governed",
    "governed_relation_sql",
    "is_governed",
    "viewer_policies",
    "get_snapshot",
    "describe_governance",
]


def __getattr__(name: str):
    # Lazy: discovery imports governance.lineage on every build, and the
    # rewriter pulls in sqlglot's optimizer.
    if name in {"GovernanceError", "GovernedQuery", "govern_query", "run_governed",
                "governed_relation_sql", "is_governed", "viewer_policies"}:
        from . import rewrite

        return getattr(rewrite, name)
    if name == "get_snapshot":
        from .catalog import get_snapshot

        return get_snapshot
    if name == "describe_governance":
        from .catalog import describe, get_snapshot

        def describe_governance(conn, project_dir=None):
            return describe(get_snapshot(conn, project_dir))

        return describe_governance
    raise AttributeError(name)
