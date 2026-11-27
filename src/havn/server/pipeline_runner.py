"""The web UI's pipeline runner: one DAG executor behind every run it starts.

``POST /api/stream/{name}/start``, ``POST /api/pipeline/start`` and
``POST /api/transform/start`` (``server/routes/pipeline.py``) each describe a
run as a :class:`RunSpec` and hand it to :func:`run_pipeline` on a background
thread. The run puts every selected seed, ingest script, model and export
script into one ``graphlib.TopologicalSorter`` and starts each node as soon as
its dependencies are done, on a worker pool of cursors off the server's
shared connection. Progress goes to the browser as events through ``emit``.

Models are built by :func:`havn.engine.transform.orchestration.build_one_model`,
the same function ``havn transform`` uses, so a run from the UI gets the
change detection, ``@source_freshness`` gate, assertions and blocking,
profiling, performance capture and Pipeline Rewind snapshots that a CLI run
gets. The model set comes from ``discover_all_models`` (installed packages
included), so upstream hashes agree with what the CLI stored.

The event contract with the browser (``frontend/src/PipelineContext.tsx``)
is fixed; see ``_start_payload``, ``_end_payload`` and ``_complete_payload``.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Any

from havn.engine.write_queue import cursor_for

logger = logging.getLogger("havn.server")

Emit = Callable[[str, dict], None]

# Statuses ``build_one_model`` returns, as the browser is told about them.
# The frontend knows "built", "skipped", "inlined", "error" and
# "assertion_failed", and shows anything else as a green "Built ...". So a
# model held back by a stale error-severity source is reported "skipped"
# (``run_log`` records it as skipped too), and a model a ``policies.deny``
# rule refused is reported "error" (``run_log`` records it as an error).
# "skipped_upstream_blocked" has always been sent as is and still is.
_EVENT_STATUS = {"source_stale": "skipped", "policy_denied": "error"}

# Node statuses (before the mapping above) that fail the run. A transform
# fails on what ``havn transform`` exits 1 for, plus a policy denial, which
# run_log records as an error and the run history therefore shows as failed.
_FAILED = frozenset({"error", "assertion_failed", "policy_denied"})
_SKIPPED = frozenset({"skipped", "skipped_upstream_blocked", "source_stale"})


@dataclass
class RunSpec:
    """What one UI run does, and how it reports itself.

    ``seed`` / ``ingest`` / ``transform`` / ``export`` select the nodes: None
    leaves that kind out of the run, ``["all"]`` takes everything, anything
    else names scripts by file stem or models by graph selector (see
    :func:`havn.engine.selectors.select_models`).
    """

    operation: str                      # "stream", "pipeline" or "transform"
    label: str
    stream: str = ""                    # "stream" field of start/complete events
    force: bool = False
    user: dict = field(default_factory=dict)
    seed: bool = False
    ingest: list[str] | None = None
    transform: list[str] | None = None
    export: list[str] | None = None
    steps: int = 1                      # "steps" field of the start event
    retries: int = 0
    retry_delay: float = 0.0
    gate: bool = True                   # run the pre-build validation gate
    job: tuple[str, str, str] | None = None   # job_runs (job_name, job_file, target)
    audit_resource: str | None = None
    audit_detail: str = ""
    rewind_trigger: str = "ui"
    webhook_url: str | None = None
    alert: bool = False                 # send the project's pipeline alert


@dataclass
class _Node:
    kind: str                           # "seed", "ingest", "transform", "export"
    name: str
    path: Path | None = None
    model: Any = None


@dataclass
class _Tally:
    has_error: bool = False
    cancelled: bool = False
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    assertion_failed: int = 0
    built: int = 0
    models_run: list[str] = field(default_factory=list)


# --- events ---------------------------------------------------------------


def _start_payload(spec: RunSpec, run_id: str, total: int) -> dict:
    if spec.operation == "transform":
        return {"operation": "transform", "label": spec.label, "pipeline_run_id": run_id}
    return {
        "operation": spec.operation, "label": spec.label, "stream": spec.stream,
        "steps": spec.steps, "total": total, "pipeline_run_id": run_id,
    }


def _end_payload(spec: RunSpec, node: _Node, result: dict, num: int) -> dict:
    status = _EVENT_STATUS.get(result.get("status", "error"), result.get("status", "error"))
    if spec.operation == "transform":
        return {
            "name": node.name, "action": node.kind, "status": status,
            "duration_ms": result.get("duration_ms", 0),
            "row_count": result.get("row_count", 0), "num": num,
        }
    return {
        "name": node.name, "action": node.kind, "status": status,
        "duration_ms": result.get("duration_ms", 0),
        "row_count": result.get("row_count", 0),
        "rows_affected": result.get("rows_affected", 0),
        "error": result.get("error"),
        "materialized": node.model.materialized if node.kind == "transform" else None,
        "num": num,
    }


def _complete_payload(
    spec: RunSpec, run_id: str, status: str, duration_s: float, error: str | None = None,
) -> dict:
    if spec.operation == "transform":
        data = {
            "operation": "transform", "status": status,
            "duration_seconds": duration_s, "pipeline_run_id": run_id,
        }
        if error is not None:
            data["error"] = error
        return data
    return {
        "stream": spec.stream, "status": status,
        "duration_seconds": duration_s, "pipeline_run_id": run_id,
    }


# --- planning ---------------------------------------------------------------


def _scripts(directory: Path, targets: list[str] | None) -> list[Path]:
    """Scripts in ``directory`` a step with ``targets`` runs, by name.

    Same rule as ``runner.run_scripts_in_dir``: ``.py`` and ``.dpnb`` files,
    ``_``-prefixed ones skipped, targets matched on the stem.
    """
    if targets is None or not directory.is_dir():
        return []
    scripts = [
        s for s in [*directory.glob("*.py"), *directory.glob("*.dpnb")]
        if not s.name.startswith("_")
    ]
    if targets and "all" not in targets:
        wanted = {t.removesuffix(".py").removesuffix(".dpnb") for t in targets}
        scripts = [s for s in scripts if s.stem in wanted]
    return sorted(scripts, key=lambda p: p.name)


@dataclass
class _Plan:
    nodes: dict[str, _Node]
    deps: dict[str, set[str]]
    models: list                        # the selected models, in DAG order
    model_map: dict                     # the whole project, upstream hashes set


def _plan(spec: RunSpec, project_dir: Path, config: Any, conn: Any) -> _Plan:
    from havn.engine.transform.discovery import discover_all_models
    from havn.engine.transform.orchestration import _hash_full_dag

    nodes: dict[str, _Node] = {}
    deps: dict[str, set[str]] = {}

    seed_ids: set[str] = set()
    if spec.seed:
        from havn.engine.seeds import discover_seeds

        for seed in discover_seeds(project_dir / "seeds"):
            nid = f"seed:{seed['full_name']}"
            nodes[nid] = _Node("seed", seed["full_name"], path=seed["path"])
            deps[nid] = set()
            seed_ids.add(nid)

    ingest_ids: set[str] = set()
    landing_to_ingest: dict[str, str] = {}
    for script in _scripts(project_dir / "ingest", spec.ingest):
        nid = f"ingest:{script.name}"
        nodes[nid] = _Node("ingest", script.name, path=script)
        deps[nid] = set()
        ingest_ids.add(nid)
        # A model reading landing.<stem> waits for that script alone.
        landing_to_ingest[f"landing.{script.stem}"] = nid

    models: list = []
    model_map: dict = {}
    if spec.transform is not None:
        all_models = discover_all_models(project_dir, config)
        if all_models:
            chosen = _select(spec.transform, all_models, conn, project_dir)
            models, model_map = _hash_full_dag(
                [m for m in all_models if m.full_name in chosen], all_models,
            )

    selected = {m.full_name for m in models}
    nearest = _nearest_selected(model_map, selected)
    for model in models:
        nid = f"transform:{model.full_name}"
        nodes[nid] = _Node("transform", model.full_name, model=model)
        wait: set[str] = set()
        for dep in model.depends_on:
            if dep in selected:
                wait.add(f"transform:{dep}")
            elif dep in model_map:
                # An unselected model in between still orders two selected ones.
                wait.update(f"transform:{d}" for d in nearest(dep))
            elif f"seed:{dep}" in nodes:
                wait.add(f"seed:{dep}")
            elif dep.startswith("landing."):
                mapped = landing_to_ingest.get(dep)
                if mapped:
                    wait.add(mapped)
                else:
                    wait.update(ingest_ids)
        deps[nid] = wait

    # Exports read what the run built: every model, or with no transform in
    # the run, whatever it loaded.
    transform_ids = {f"transform:{m.full_name}" for m in models}
    export_wait = transform_ids or (ingest_ids | seed_ids)
    for script in _scripts(project_dir / "export", spec.export):
        nid = f"export:{script.name}"
        nodes[nid] = _Node("export", script.name, path=script)
        deps[nid] = set(export_wait)

    return _Plan(nodes, deps, models, model_map)


def _select(selectors: list[str], all_models: list, conn: Any, project_dir: Path) -> set[str]:
    if not selectors or "all" in selectors:
        return {m.full_name for m in all_models}
    from havn.engine.selectors import select_models

    selection = select_models(selectors, all_models, conn=conn, project_dir=project_dir)
    for warning in selection.warnings:
        logger.info("Pipeline selection: %s", warning)
    return set(selection.selected)


def _nearest_selected(model_map: dict, selected: set[str]) -> Callable[[str], set[str]]:
    """``nearest(name)``: the selected models closest above ``name``."""
    memo: dict[str, set[str]] = {}

    def nearest(name: str) -> set[str]:
        if name in memo:
            return memo[name]
        memo[name] = set()  # cycle guard; build_dag rejects real cycles
        found: set[str] = set()
        for dep in getattr(model_map.get(name), "depends_on", ()):
            if dep in selected:
                found.add(dep)
            elif dep in model_map:
                found |= nearest(dep)
        memo[name] = found
        return found

    return nearest


# --- the run ----------------------------------------------------------------


def run_pipeline(
    spec: RunSpec,
    project_dir: Path,
    emit: Emit,
    cancel: threading.Event,
) -> str:
    """Run ``spec`` to completion, emitting events. Returns the final status.

    Always ends with exactly one ``complete`` event. The caller marks the
    operation finished afterwards.
    """
    from havn.server.deps import _get_config, _get_shared_conn

    run_id = str(uuid.uuid4())
    started = time.perf_counter()
    tally = _Tally()
    status = "failed"

    if spec.operation == "transform":
        emit("start", _start_payload(spec, run_id, 0))

    try:
        config = _get_config()
        conn = _get_shared_conn()
        main = cursor_for(conn)
        try:
            plan = _plan(spec, project_dir, config, main)
            _record_job_run(conn, spec, run_id, len(plan.nodes))
            if spec.operation != "transform":
                emit("start", _start_payload(spec, run_id, len(plan.nodes)))
            if not plan.nodes:
                status = "success"
                emit("complete", _complete_payload(spec, run_id, status, 0))
                return status
            status = _execute(spec, plan, project_dir, config, conn, main, run_id, emit, cancel, tally)
        finally:
            main.close()
    except _GateFailed:
        tally.has_error = True
        status = "failed"
        emit("complete", _complete_payload(spec, run_id, status, 0))
        return status
    except Exception as e:
        logger.error("Pipeline run failed: %s", e, exc_info=True)
        tally.has_error = True
        status = "failed"
        duration_s = round(time.perf_counter() - started, 1)
        error = str(e) if spec.operation == "transform" else None
        emit("complete", _complete_payload(spec, run_id, status, duration_s, error))
        return status
    finally:
        _finish_job_run(spec, run_id, tally, started)
    return status


class _GateFailed(Exception):
    """The pre-build gate refused the run before anything was built."""


def _execute(
    spec: RunSpec,
    plan: _Plan,
    project_dir: Path,
    config: Any,
    conn: Any,
    main: Any,
    run_id: str,
    emit: Emit,
    cancel: threading.Event,
    tally: _Tally,
) -> str:
    from havn.engine.database import ensure_meta_table, log_run
    from havn.engine.instrumentation import instrument_run
    from havn.engine.transform.discovery import blocked_models
    from havn.engine.transform.orchestration import _evaluate_deny_rules, detect_run_anomalies
    from havn.server.deps import _get_backend, _get_db_resource_limits
    from havn.server.prebuild_gate import run_prebuild_gate

    started = time.perf_counter()
    nodes = plan.nodes
    models = plan.models
    ensure_meta_table(main)

    # Pre-build validation. With ingest scripts or seeds in the run, the
    # tables they load may not exist yet, so the gate waits until every one
    # of them has finished and runs just before the first transform is
    # submitted; no transform starts before that (see submit_ready).
    loaders_left = sum(1 for n in nodes.values() if n.kind in ("ingest", "seed"))
    gate_pending = spec.gate and bool(models) and loaders_left > 0
    gate_failed = False
    if spec.gate and models and not loaders_left:
        if not run_prebuild_gate(conn, models, emit, project_dir=project_dir):
            raise _GateFailed()

    # Every schema a worker writes to exists before the first worker starts:
    # two workers racing CREATE SCHEMA IF NOT EXISTS hit DuckDB's catalog
    # write-write conflict.
    schemas = set() if spec.operation == "transform" else {"landing", "bronze", "silver", "gold"}
    schemas |= {m.schema for m in models if m.schema}
    if any(n.kind == "seed" for n in nodes.values()):
        schemas.add("seeds")
    for schema in sorted(schemas):
        main.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

    # State shared by the workers' build_one_model calls (see its docstring
    # for why no lock is needed). A rejected parent outside this selection
    # still blocks its children, as in `havn transform`.
    selected = {m.full_name for m in models}
    results: dict[str, str] = {}
    blocked: set[str] = (blocked_models(main) - selected) if models else set()
    run_profiles: dict[str, object] = {}
    denied = _evaluate_deny_rules(models, project_dir) if models else {}
    for name, reason in denied.items():
        results[name] = "policy_denied"
        blocked.add(name)
        try:
            log_run(main, "transform", name, "error", 0, 0,
                    error=f"policy_denied: {reason}", pipeline_run_id=run_id)
        except Exception:
            pass

    rewind_id = _start_rewind(config, project_dir, spec) if models else None

    _, threads_cfg = _get_db_resource_limits()
    # DuckLake's catalog cannot tolerate concurrent in-process writes: several
    # cursors creating tables against one attached catalog corrupt its
    # metadata. One worker there; transforms still get full memory and
    # threads through per-query SET memory_limit / SET threads.
    max_workers = 1 if _get_backend().name == "ducklake" else (threads_cfg or 4)

    result_q: queue.Queue = queue.Queue()
    numbers: dict[str, int] = {}
    next_num = [1]

    def run_once(nid: str, cur: Any) -> dict:
        node = nodes[nid]
        if node.kind in ("ingest", "export"):
            from havn.engine.runner import run_script

            res = run_script(cur, node.path, node.kind, pipeline_run_id=run_id,
                             force=spec.force, run_as=spec.user)
            out = {
                "status": res["status"], "duration_ms": res.get("duration_ms", 0),
                "error": res.get("error"), "orphaned": bool(res.get("orphaned")),
            }
            if node.kind == "ingest":
                out["rows_affected"] = res.get("rows_affected", 0)
            return out
        if node.kind == "seed":
            from havn.engine.seeds import load_seed

            try:
                res = load_seed(cur, node.path, "seeds", spec.force)
            except Exception as e:
                log_run(cur, "seed", node.name, "error", error=str(e), pipeline_run_id=run_id)
                return {"status": "error", "error": str(e)}
            return {"status": res["status"], "duration_ms": res["duration_ms"],
                    "row_count": res["row_count"]}
        # transform
        from havn.engine.transform.orchestration import build_one_model

        model = node.model
        outcome = build_one_model(
            cur, model, plan.model_map, results, blocked,
            force=spec.force, project_dir=project_dir,
            rewind_config=config.rewind if rewind_id else None, run_id=rewind_id,
            pipeline_run_id=run_id, run_profiles=run_profiles, echo=False,
        )
        error = outcome.error
        if outcome.status == "policy_denied":
            error = f"policy_denied: {denied.get(model.full_name, '')}".rstrip(": ")
        return {"status": outcome.status, "duration_ms": outcome.duration_ms,
                "row_count": outcome.row_count, "error": error}

    def retryable(nid: str, res: dict) -> bool:
        # A script still running after its timeout owns its cursor; running it
        # again would race it. Assertion failures, stale sources and denials
        # come out the same on a second try.
        return res.get("status") == "error" and not res.get("orphaned")

    def exec_node(nid: str) -> None:
        node = nodes[nid]
        cur = cursor_for(conn)
        start_data = {"name": node.name, "action": node.kind, "num": numbers.get(nid, 0)}
        if node.kind == "transform":
            start_data["materialized"] = node.model.materialized
        result_q.put(("__start__", start_data))
        try:
            attempt = 0
            while True:
                try:
                    res = run_once(nid, cur)
                except Exception as e:
                    logger.error("Node %s failed: %s", nid, e, exc_info=True)
                    if node.kind == "transform":
                        try:
                            log_run(cur, "transform", node.name, "error",
                                    error=str(e), pipeline_run_id=run_id)
                        except Exception:
                            pass
                    res = {"status": "error", "error": str(e)}
                if attempt >= spec.retries or not retryable(nid, res) or cancel.is_set():
                    break
                attempt += 1
                logger.warning("Pipeline node %s failed; retry %d/%d after %ss",
                               node.name, attempt, spec.retries, spec.retry_delay)
                if cancel.wait(spec.retry_delay):
                    break
                if node.kind == "transform":
                    # Let build_one_model take the model again.
                    results.pop(node.name, None)
                    blocked.discard(node.name)
            result_q.put((nid, res))
        finally:
            cur.close()

    sorter = TopologicalSorter(plan.deps)
    sorter.prepare()
    executor = ThreadPoolExecutor(max_workers=max_workers)
    active = 0
    pending: list[str] = []

    def maybe_run_gate() -> bool:
        nonlocal gate_pending, gate_failed
        if gate_pending:
            gate_pending = False
            gate_failed = not run_prebuild_gate(conn, models, emit, project_dir=project_dir)
        return not gate_failed

    def submit_ready() -> None:
        nonlocal active
        i = 0
        while i < len(pending) and active < max_workers:
            if nodes[pending[i]].kind == "transform":
                if gate_pending and loaders_left:
                    i += 1  # held back until the loaders are done
                    continue
                if not maybe_run_gate():
                    return  # the loop below sees gate_failed and stops
            nid = pending.pop(i)
            if nid not in numbers:
                numbers[nid] = next_num[0]
                next_num[0] += 1
            try:
                executor.submit(exec_node, nid)
            except RuntimeError:
                return  # interpreter shutting down
            active += 1

    with instrument_run(
        main, project_dir=project_dir, pipeline_run_id=run_id,
        kind=spec.operation, name=spec.stream or None,
    ):
        pending.extend(sorter.get_ready())
        submit_ready()
        while sorter.is_active() or active > 0:
            if gate_failed and active == 0:
                tally.has_error = True
                executor.shutdown(wait=False, cancel_futures=True)
                break
            if cancel.is_set():
                tally.cancelled = True
                executor.shutdown(wait=False, cancel_futures=True)
                break
            try:
                nid, res = result_q.get(timeout=2)
            except queue.Empty:
                continue
            if nid == "__start__":
                emit("model_start", res)
                continue

            active -= 1
            node = nodes[nid]
            if nid not in numbers:
                numbers[nid] = next_num[0]
                next_num[0] += 1
            emit("model_end", _end_payload(spec, node, res, numbers[nid]))

            raw = res.get("status", "error")
            if node.kind in ("ingest", "seed"):
                loaders_left -= 1
            if node.kind == "transform":
                tally.models_run.append(node.name)
                if raw == "assertion_failed":
                    tally.assertion_failed += 1
                elif raw == "built":
                    tally.built += 1
            if raw in _FAILED:
                tally.has_error = True
                tally.failed += 1
            elif raw in _SKIPPED:
                tally.skipped += 1
            else:
                tally.completed += 1

            sorter.done(nid)
            try:
                pending.extend(sorter.get_ready())
            except Exception:
                pass
            submit_ready()

        try:
            executor.shutdown(wait=False)
        except Exception:
            pass

        if not tally.cancelled:
            # Workers are done; nothing writes run_profiles any more.
            detect_run_anomalies(main, run_profiles, project_dir)

    duration_s = round(time.perf_counter() - started, 1)
    status = "cancelled" if tally.cancelled else ("failed" if tally.has_error else "success")

    if rewind_id:
        _finish_rewind(config, project_dir, rewind_id, tally)

    if spec.audit_resource:
        try:
            from havn.engine.audit import log_audit

            log_audit(main, user=spec.user.get("username", "anonymous"),
                      action="transform", resource=spec.audit_resource,
                      detail=f"{spec.audit_detail}: {status} in {duration_s}s")
        except Exception:
            pass

    emit("complete", _complete_payload(spec, run_id, status, duration_s))
    _notify(spec, config, main, status, duration_s, tally)
    return status


# --- bookkeeping --------------------------------------------------------------


def _record_job_run(conn: Any, spec: RunSpec, run_id: str, total: int) -> None:
    """A job_runs row, so the run appears in Job Results."""
    if spec.job is None:
        return
    try:
        from havn.engine.orchestration import ensure_job_runs_table

        cur = cursor_for(conn)
        try:
            ensure_job_runs_table(cur)
            job_name, job_file, target = spec.job
            cur.execute(
                "INSERT INTO _havn.job_runs (id, job_name, job_file, target, status, steps_total, trigger) "
                "VALUES (?, ?, ?, ?, 'running', ?, ?)",
                [run_id, job_name, job_file, target, total, "pipeline"],
            )
        finally:
            cur.close()
    except Exception:
        logger.debug("Failed to insert job_run for %s", spec.label, exc_info=True)


def _finish_job_run(spec: RunSpec, run_id: str, tally: _Tally, started: float) -> None:
    if spec.job is None:
        return
    try:
        from havn.engine.orchestration import ensure_job_runs_table
        from havn.server.deps import _get_shared_conn

        cur = cursor_for(_get_shared_conn())
        try:
            ensure_job_runs_table(cur)
            status = "failure" if tally.has_error else ("cancelled" if tally.cancelled else "success")
            cur.execute(
                "UPDATE _havn.job_runs SET status = ?, finished_at = current_timestamp, "
                "duration_ms = ?, steps_completed = ?, steps_failed = ?, steps_skipped = ? "
                "WHERE id = ?",
                [status, int((time.perf_counter() - started) * 1000),
                 tally.completed, tally.failed, tally.skipped, run_id],
            )
        finally:
            cur.close()
    except Exception:
        logger.debug("Failed to update job_run for %s", spec.label, exc_info=True)


def _start_rewind(config: Any, project_dir: Path, spec: RunSpec) -> str | None:
    """A Pipeline Rewind run for this run's snapshots, as the CLI starts one."""
    if not config.rewind.enabled:
        return None
    try:
        from havn.engine.snapshots import start_run

        return start_run(project_dir, trigger=spec.rewind_trigger)
    except Exception as e:
        logger.warning("Could not start a Rewind run: %s", e)
        return None


def _finish_rewind(config: Any, project_dir: Path, rewind_id: str, tally: _Tally) -> None:
    try:
        from havn.engine.snapshots import RewindConfig, finish_run, run_gc

        # As `havn transform`: failed on an error, partial when only
        # assertions failed (or the run was cut short), else success.
        hard = tally.failed - tally.assertion_failed
        status = (
            "failed" if hard
            else "partial" if (tally.assertion_failed or tally.cancelled)
            else "success"
        )
        finish_run(project_dir, rewind_id, status, tally.models_run)
        rw = config.rewind
        run_gc(project_dir, RewindConfig(
            enabled=rw.enabled, retention=rw.retention, max_storage=rw.max_storage,
            dedup=rw.dedup, exclude=rw.exclude,
        ))
    except Exception as e:
        logger.warning("Rewind bookkeeping failed: %s", e)


def _notify(spec: RunSpec, config: Any, conn: Any, status: str, duration_s: float, tally: _Tally) -> None:
    """The stream's webhook and the project's pipeline alert, as `havn stream` sends them."""
    if status == "cancelled":
        return
    ok = status == "success"
    if spec.webhook_url:
        from havn.engine.stream_runner import send_stream_webhook

        err = send_stream_webhook(spec.webhook_url, spec.stream, "success" if ok else "failed", duration_s)
        if err:
            logger.warning("Webhook failed for stream %s: %s", spec.stream, err)
    if spec.alert:
        try:
            from havn.engine.pipeline_alerts import send_pipeline_alert

            send_pipeline_alert(
                config.alerts, spec.stream, ok=ok, duration_s=duration_s, conn=conn,
                error=None if ok else f"{tally.failed} step(s) failed",
                models_built=tally.built,
            )
        except Exception as e:
            logger.debug("Pipeline alert failed: %s", e)
