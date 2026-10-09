"""The critical path of a pipeline run: which chain of models set its length.

In a parallel run the slowest model is not necessarily what the run waited
for. What it waited for is a chain: the model that finished last, the
upstream it was waiting on, that model's slowest upstream, and so on back to
the start. Speeding up anything off that chain does not make the run any
shorter.

Two answers are computed from the builds a run recorded and the project's
DAG:

``path``
    What actually happened: walk back from the last model to finish, each
    time to the in-run upstream that finished latest. ``wait_ms`` on a step
    is the gap between that upstream finishing and this model starting:
    tier barriers in a parallel run, or simply the sequential runner being
    busy with something else.
``longest_chain``
    The longest dependency chain by build time. With unlimited workers a run
    of these models could not finish faster than its length, so it is the
    floor that more parallelism cannot get under.

Upstreams that did not build in the run (skipped as unchanged) are looked
through: their own upstreams stand in for them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def critical_path(builds: list[dict], deps: dict[str, list[str]]) -> dict:
    """Critical path and longest chain of one run.

    ``builds`` are ``model_perf`` rows of the run (``model_path``,
    ``started_at``, ``finished_at``, ``duration_ms``); ``deps`` maps every
    project model to its direct upstreams.
    """
    nodes: dict[str, dict] = {}
    for b in builds:
        start, end = _ts(b.get("started_at")), _ts(b.get("finished_at"))
        if start is None or end is None:
            continue
        name = b["model_path"]
        # A model retried in a job has two rows; the last attempt is what counted.
        if name in nodes and nodes[name]["end"] >= end:
            continue
        nodes[name] = {
            "model": name,
            "start": start,
            "end": end,
            "duration_ms": int(b.get("duration_ms") or 0),
            "status": b.get("status"),
        }
    if not nodes:
        return {"path": [], "longest_chain": [], "models": [], "wall_ms": 0, "busy_ms": 0}

    memo: dict[str, set[str]] = {}

    def run_parents(model: str, stack: frozenset = frozenset()) -> set[str]:
        """In-run upstreams of ``model``, looking through models that did not build."""
        if model in memo:
            return memo[model]
        found: set[str] = set()
        for dep in deps.get(model, []):
            if dep in stack:
                continue  # a cycle would have failed discovery; never loop here
            if dep in nodes:
                found.add(dep)
            else:
                found |= run_parents(dep, stack | {model})
        memo[model] = found
        return found

    tiers: dict[str, int] = {}

    def tier(model: str) -> int:
        if model not in tiers:
            parents = run_parents(model)
            tiers[model] = 1 + max((tier(p) for p in parents), default=-1)
        return tiers[model]

    run_start = min(n["start"] for n in nodes.values())
    run_end = max(n["end"] for n in nodes.values())
    wall_ms = int((run_end - run_start).total_seconds() * 1000)
    busy_ms = sum(n["duration_ms"] for n in nodes.values())

    # What actually happened: back from the last finisher.
    path: list[dict] = []
    current = max(nodes.values(), key=lambda n: n["end"])["model"]
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        node = nodes[current]
        parents = [p for p in run_parents(current) if p in nodes]
        pred = max(parents, key=lambda p: nodes[p]["end"]) if parents else None
        ready = nodes[pred]["end"] if pred else run_start
        path.append({
            "model": current,
            "duration_ms": node["duration_ms"],
            "started_at": node["start"].isoformat(),
            "finished_at": node["end"].isoformat(),
            "wait_ms": max(0, int((node["start"] - ready).total_seconds() * 1000)),
            "tier": tier(current),
            "share_pct": round(100.0 * node["duration_ms"] / wall_ms, 1) if wall_ms else None,
        })
        current = pred
    path.reverse()

    # The floor: longest chain by build time alone.
    best: dict[str, tuple[int, str | None]] = {}

    def longest(model: str) -> tuple[int, str | None]:
        if model not in best:
            parents = run_parents(model)
            if parents:
                p = max(parents, key=lambda x: longest(x)[0])
                best[model] = (longest(p)[0] + nodes[model]["duration_ms"], p)
            else:
                best[model] = (nodes[model]["duration_ms"], None)
        return best[model]

    tail = max(nodes, key=lambda m: longest(m)[0])
    chain: list[str] = []
    cursor: str | None = tail
    while cursor:
        chain.append(cursor)
        cursor = longest(cursor)[1]
    chain.reverse()
    on_path = {p["model"] for p in path}

    models = sorted(
        (
            {
                "model": n["model"],
                "start_offset_ms": int((n["start"] - run_start).total_seconds() * 1000),
                "duration_ms": n["duration_ms"],
                "tier": tier(n["model"]),
                "status": n["status"],
                "on_path": n["model"] in on_path,
            }
            for n in nodes.values()
        ),
        key=lambda m: (m["start_offset_ms"], m["model"]),
    )
    path_ms = sum(p["duration_ms"] for p in path)
    return {
        "started_at": run_start.isoformat(),
        "finished_at": run_end.isoformat(),
        "wall_ms": wall_ms,
        "busy_ms": busy_ms,
        "parallelism": round(busy_ms / wall_ms, 2) if wall_ms else None,
        "path": path,
        "path_ms": path_ms,
        "wait_ms": sum(p["wait_ms"] for p in path),
        "longest_chain": [
            {"model": m, "duration_ms": nodes[m]["duration_ms"], "tier": tier(m)} for m in chain
        ],
        "longest_chain_ms": longest(tail)[0],
        "tiers": 1 + max(tiers.values(), default=-1),
        "models": models,
    }
