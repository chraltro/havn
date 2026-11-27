"""The pipeline engine: the one executor behind every stream, pipeline and transform run.

Callers describe a run as a :class:`RunSpec` and hand it to
:func:`run_pipeline` with a warehouse connection and an event sink:

- ``havn stream`` and scheduled streams, through ``engine.stream_runner``
  (their own connection; the CLI renders the events with Rich);
- the web UI's stream, pipeline and transform runs and the blocking
  ``POST /api/stream/{name}``, through ``server.pipeline_runner`` (the
  server's shared connection; the events become the browser's SSE events).

So every entry point gets the same planning, the same model builds
(``build_one_model``, as ``havn transform``: change detection, the
``@source_freshness`` gate, assertions and blocking, profiling, perf
capture, lineage, Pipeline Rewind snapshots), the same pre-build validation
gate, retries, failure propagation, ``run_log`` / ``job_runs`` rows,
anomaly detection, webhook and alerts.

How a run is shaped:

- A stream's steps run in their listed order (``RunSpec.ordered``). Each
  step is a phase; a phase starts when the one before it has finished, and
  a failed phase stops the stream: every node of the later steps is
  reported as skipped with the failure as its reason, as ``havn stream``
  stops before its export step after a failed load or a rejected model.
  Inside a transform step the models build in parallel as soon as their own
  parents are done; the scripts and seeds of one step run one at a time, in
  name order, and a failed ingest script stops the rest of its step (data
  integrity).
- A selective pipeline (``RunSpec.ordered=False``) puts every selected seed,
  ingest script, model and export into one DAG and starts each node as soon
  as what it reads has loaded, so a failed load holds back only the models
  that read it (plus every export).

Progress goes to ``sink(event, data)``; see :func:`run_pipeline` for the
events. Nothing here imports from ``havn.server``.
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

logger = logging.getLogger("havn.pipeline")

EventSink = Callable[[str, dict], None]

STEP_ACTIONS = ("seed", "ingest", "transform", "export")

# Node statuses (as build_one_model, run_script and load_seed return them)
# that fail the run: what `havn transform` exits 1 for, plus a policy
# denial, which run_log records as an error and the run history therefore
# shows as failed.
FAILED = frozenset({"error", "assertion_failed", "policy_denied"})
SKIPPED = frozenset({"skipped", "skipped_upstream_blocked", "source_stale"})


class RunSpecError(ValueError):
    """The run cannot be planned as described (an unknown step action)."""


@dataclass
class Step:
    """One step of a run: an action and its targets (``["all"]`` = everything).

    Targets name scripts by file stem, seeds by file stem or ``seeds.<name>``,
    and models by graph selector (see :func:`havn.engine.selectors.select_models`).
    """

    action: str
    targets: list[str] = field(default_factory=lambda: ["all"])


@dataclass
class RunSpec:
    """What one run does, and how it reports itself."""

    operation: str                      # "stream", "pipeline" or "transform"
    label: str
    steps: list[Step] = field(default_factory=list)
    name: str = ""                      # the stream's name ("pipeline" for a selective one)
    ordered: bool = True                # steps run in listed order (False: one DAG)
    force: bool = False
    user: dict | None = None            # scripts run governed for this user
    retries: int = 0                    # per failed node
    retry_delay: float = 0.0
    gate: bool = True                   # the pre-build validation gate
    job: tuple[str, str, str] | None = None   # a job_runs row: (job_name, job_file, target)
    job_trigger: str = "manual"
    audit_resource: str | None = None
    audit_detail: str = ""
    rewind_trigger: str = "manual"
    webhook_url: str | None = None
    # The project's pipeline alert: None sends nothing; "always" alerts on
    # every finished run (a stream); "when_built" alerts on failure, and on
    # success only when a model was built (`havn transform`).
    alert: str | None = None
    alert_target: str = ""
    echo: bool = False                  # model builds print their CLI lines


def stream_spec(
    name: str,
    stream_config: Any,
    *,
    force: bool = False,
    user: dict | None = None,
    trigger: str = "manual",
    echo: bool = False,
) -> RunSpec:
    """The RunSpec of stream ``name``: its steps in order, retries, webhook.

    ``trigger`` is ``"manual"`` (CLI), ``"scheduled"`` or ``"ui"``. It labels
    the Rewind run (``stream:<name>``, or ``scheduled:<name>``) and the
    ``job_runs`` row.
    """
    steps = [Step(s.action, list(s.targets or ["all"])) for s in stream_config.steps]
    return RunSpec(
        operation="stream",
        label=f"Pipeline {name}",
        steps=steps,
        name=name,
        ordered=True,
        force=force,
        user=user,
        retries=max(0, int(stream_config.retries or 0)),
        retry_delay=float(stream_config.retry_delay or 0),
        job=(f"stream ({name})", "stream", name),
        job_trigger="pipeline" if trigger == "ui" else trigger,
        rewind_trigger=f"scheduled:{name}" if trigger == "scheduled" else f"stream:{name}",
        webhook_url=stream_config.webhook_url,
        alert="always",
        alert_target=name,
        echo=echo,
    )


# --- results ----------------------------------------------------------------


@dataclass
class StepResult:
    """How one step of an ordered run went."""

    index: int
    action: str
    targets: list[str]
    ran: bool = False                   # False: not reached (an earlier step failed)
    ok: bool = True
    attempts: int = 1
    results: Any = None                 # transform/seed: {name: status}; scripts: [status]
    error: str | None = None


@dataclass
class RunResult:
    run_id: str
    status: str = "failed"              # "success", "failed" or "cancelled"
    duration_s: float = 0.0
    error: str | None = None
    crashed: bool = False               # the run itself broke off (not a node failure)
    steps: list[StepResult] = field(default_factory=list)
    nodes: list[dict] = field(default_factory=list)
    models_run: list[str] = field(default_factory=list)
    models_built: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "success"


# --- planning ---------------------------------------------------------------


@dataclass
class _Node:
    id: str
    kind: str                           # "seed", "ingest", "transform", "export"
    name: str
    path: Path | None = None
    model: Any = None

    @property
    def label(self) -> str:
        """How a failure of this node is named in a skipped node's reason."""
        return f"{self.kind}/{self.name}" if self.kind in ("ingest", "export") else self.name


@dataclass
class _Phase:
    index: int
    action: str | None                  # the step's action (None: a merged DAG)
    targets: list[str]
    nodes: dict[str, _Node]
    deps: dict[str, set[str]]
    models: list                        # the selected models, in DAG order
    model_map: dict                     # the whole project, upstream hashes set
    sequential: bool = False            # one node at a time


def _scripts(directory: Path, targets: list[str] | None) -> list[Path]:
    """Scripts in ``directory`` a step with ``targets`` runs, by name.

    ``.py`` and ``.dpnb`` files, ``_``-prefixed ones skipped, targets
    matched on the stem.
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


def _seeds(project_dir: Path, targets: list[str] | None) -> list[dict]:
    if targets is None:
        return []
    from havn.engine.seeds import discover_seeds

    seeds = discover_seeds(project_dir / "seeds")
    if targets and "all" not in targets:
        wanted = {t.removesuffix(".csv") for t in targets}
        seeds = [s for s in seeds if s["name"] in wanted or s["full_name"] in wanted]
    return seeds


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


def _plan_phase(
    index: int,
    action: str | None,
    kinds: dict[str, list[str]],
    project_dir: Path,
    config: Any,
    conn: Any,
    discovered: dict,
    *,
    ordered: bool,
) -> _Phase:
    """One DAG over the nodes ``kinds`` selects (``{"ingest": ["all"], ...}``)."""
    from havn.engine.transform.orchestration import _hash_full_dag

    prefix = f"{index}:"
    nodes: dict[str, _Node] = {}
    deps: dict[str, set[str]] = {}

    seed_ids: set[str] = set()
    for seed in _seeds(project_dir, kinds.get("seed")):
        nid = f"{prefix}seed:{seed['full_name']}"
        nodes[nid] = _Node(nid, "seed", seed["full_name"], path=seed["path"])
        deps[nid] = set()
        seed_ids.add(nid)

    ingest_ids: list[str] = []
    landing_to_ingest: dict[str, str] = {}
    for script in _scripts(project_dir / "ingest", kinds.get("ingest")):
        nid = f"{prefix}ingest:{script.name}"
        nodes[nid] = _Node(nid, "ingest", script.name, path=script)
        # In a stream step a failed ingest script stops the ones after it,
        # as `havn stream` always did.
        deps[nid] = {ingest_ids[-1]} if (ordered and ingest_ids) else set()
        ingest_ids.append(nid)
        # A model reading landing.<stem> waits for that script alone.
        landing_to_ingest[f"landing.{script.stem}"] = nid

    models: list = []
    model_map: dict = {}
    if kinds.get("transform") is not None:
        if "models" not in discovered:
            from havn.engine.transform.discovery import discover_all_models

            discovered["models"] = discover_all_models(project_dir, config)
        all_models = discovered["models"]
        if all_models:
            chosen = _select(kinds["transform"], all_models, conn, project_dir)
            models, model_map = _hash_full_dag(
                [m for m in all_models if m.full_name in chosen], all_models,
            )

    selected = {m.full_name for m in models}
    nearest = _nearest_selected(model_map, selected)
    seed_by_name = {nodes[n].name: n for n in seed_ids}
    for model in models:
        nid = f"{prefix}transform:{model.full_name}"
        nodes[nid] = _Node(nid, "transform", model.full_name, model=model)
        wait: set[str] = set()
        for dep in model.depends_on:
            if dep in selected:
                wait.add(f"{prefix}transform:{dep}")
            elif dep in model_map:
                # An unselected model in between still orders two selected
                # ones; a model reading only other models is covered by them.
                wait.update(f"{prefix}transform:{d}" for d in nearest(dep))
            elif dep in seed_by_name:
                wait.add(seed_by_name[dep])
            elif dep in landing_to_ingest:
                wait.add(landing_to_ingest[dep])
            else:
                # A table no single loader in this run is known to write
                # (raw.events, a landing table named after no script, a
                # file): any of them may write it, so any failed load holds
                # this model back.
                wait.update(ingest_ids)
        deps[nid] = wait

    # Exports read what the run built and what it loaded, so they wait on
    # every model and every loader: a failed load or model stops them.
    transform_ids = {f"{prefix}transform:{m.full_name}" for m in models}
    export_wait = transform_ids | set(ingest_ids) | seed_ids
    for script in _scripts(project_dir / "export", kinds.get("export")):
        nid = f"{prefix}export:{script.name}"
        nodes[nid] = _Node(nid, "export", script.name, path=script)
        deps[nid] = set(export_wait)

    targets = [t for ts in kinds.values() for t in ts]
    return _Phase(
        index, action, targets, nodes, deps, models, model_map,
        # Scripts and seeds of one stream step take turns: two of them
        # replacing landing tables at once conflict in DuckDB.
        sequential=ordered and action != "transform",
    )


def _plan_run(spec: RunSpec, project_dir: Path, config: Any, conn: Any) -> list[_Phase]:
    for step in spec.steps:
        if step.action not in STEP_ACTIONS:
            raise RunSpecError(
                f"{spec.operation} {spec.name or spec.label!r}: unknown step action "
                f"{step.action!r} (expected seed, ingest, transform or export)"
            )
    discovered: dict = {}
    if spec.ordered:
        return [
            _plan_phase(i, step.action, {step.action: list(step.targets or ["all"])},
                        project_dir, config, conn, discovered, ordered=True)
            for i, step in enumerate(spec.steps)
        ]
    chosen: dict[str, list[str]] = {}
    for step in spec.steps:
        chosen.setdefault(step.action, []).extend(step.targets or ["all"])
    for action, targets in chosen.items():
        if "all" in targets:
            chosen[action] = ["all"]
    return [_plan_phase(0, None, chosen, project_dir, config, conn, discovered, ordered=False)]


def default_workers(config: Any) -> int:
    """Worker threads for a run: ``database.threads`` (default 4), 1 on DuckLake.

    DuckLake's catalog cannot tolerate concurrent in-process writes: several
    cursors creating tables against one attached catalog corrupt its
    metadata. Transforms there still get full memory and threads through
    per-query settings.
    """
    db = getattr(config, "database", None)
    if getattr(db, "backend", "duckdb") == "ducklake":
        return 1
    return getattr(db, "threads", None) or 4


def send_stream_webhook(url: str, stream_name: str, status: str, duration_s: float) -> str | None:
    """POST the stream's completion to ``url``. Returns an error string, or None."""
    import json
    from datetime import datetime
    from urllib.request import Request, urlopen

    payload = json.dumps({
        "stream": stream_name,
        "status": status,
        "duration_seconds": duration_s,
        "timestamp": datetime.now().isoformat(),
    }).encode()
    try:
        req = Request(url, data=payload, headers={"Content-Type": "application/json"})
        urlopen(req, timeout=10)  # noqa: S310 - user-configured endpoint
        return None
    except Exception as e:
        return str(e)


# --- the run ----------------------------------------------------------------


@dataclass
class _Tally:
    has_error: bool = False
    cancelled: bool = False
    gate_failed: bool = False
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    assertion_failed: int = 0
    built: int = 0
    models_run: list[str] = field(default_factory=list)


def run_pipeline(
    spec: RunSpec,
    project_dir: Path,
    config: Any,
    conn: Any,
    sink: EventSink | None = None,
    *,
    run_id: str | None = None,
    cancel: threading.Event | None = None,
    sleep: Callable[[float], Any] | None = None,
    max_workers: int | None = None,
) -> RunResult:
    """Run ``spec`` to completion against ``conn`` and return how it went.

    ``conn`` is the warehouse connection; every worker builds on its own
    cursor of it (``cursor_for``), so the server passes its shared
    connection and no second connection is opened. ``cancel`` stops the run
    between nodes; ``sleep(delay)`` waits between retries (default: wait on
    ``cancel``). ``max_workers`` overrides :func:`default_workers`.

    Events, in order (``data`` fields in brackets):

    - ``run_start`` [run_id, total] once planned (not sent when planning failed);
    - ``step_start`` / ``step_end`` [index, action, targets / ok, error,
      attempts] around each step of an ordered run that is reached;
    - ``node_start`` [id, kind, name, num, materialized];
    - ``retry`` [kind, name, action, attempt, retries, delay];
    - ``validation`` [model, severity, message, line?] per gate finding;
    - ``node_end`` [id, kind, name, num, materialized, status, duration_ms,
      row_count, rows_affected, error, held]; ``status`` is the raw
      build_one_model / run_script / load_seed status, and ``held`` marks a
      node not run because of a failure above it;
    - ``gc`` [deleted] after Rewind garbage collection;
    - ``run_end`` [run_id, status, duration_s, error, crashed], exactly once;
    - ``webhook`` [url, error] and ``alert`` [results] after it.

    Never raises for a failing run; the result says how it ended.
    """
    run = _Run(
        spec, Path(project_dir), config, conn, sink,
        run_id or str(uuid.uuid4()), cancel or threading.Event(), sleep,
        max_workers or default_workers(config),
    )
    return run.run()


class _Run:
    def __init__(self, spec, project_dir, config, conn, sink, run_id, cancel, sleep, max_workers):
        self.spec: RunSpec = spec
        self.project_dir: Path = project_dir
        self.config = config
        self.conn = conn
        self.sink = sink
        self.run_id: str = run_id
        self.cancel: threading.Event = cancel
        self.sleep = sleep
        self.max_workers: int = max(1, int(max_workers))
        self.tally = _Tally()
        self.result = RunResult(run_id=run_id)
        self.started = time.perf_counter()
        self.numbers: dict[str, int] = {}
        self.next_num = 1
        self.run_profiles: dict[str, object] = {}
        self.rewind_id: str | None = None
        self.ended = False

    # -- events --------------------------------------------------------------

    def emit(self, event: str, **data) -> None:
        if self.sink is None:
            return
        try:
            self.sink(event, data)
        except Exception as exc:
            logger.debug("Pipeline event handler failed on %s: %s", event, exc)

    def _end(self, status: str, error: str | None = None, *, crashed: bool = False) -> None:
        if self.ended:
            return
        self.ended = True
        r = self.result
        r.status = status
        r.error = error
        r.crashed = crashed
        r.duration_s = round(time.perf_counter() - self.started, 1)
        r.models_run = list(dict.fromkeys(self.tally.models_run))
        r.models_built = self.tally.built
        self.emit("run_end", run_id=self.run_id, status=status, duration_s=r.duration_s,
                  error=error, crashed=crashed)

    def _wait(self, delay: float) -> bool:
        """Wait ``delay`` seconds between retries; True when cancelled meanwhile."""
        if self.sleep is not None:
            self.sleep(delay)
            return self.cancel.is_set()
        return self.cancel.wait(delay)

    def _number(self, nid: str) -> int:
        if nid not in self.numbers:
            self.numbers[nid] = self.next_num
            self.next_num += 1
        return self.numbers[nid]

    # -- the run -------------------------------------------------------------

    def run(self) -> RunResult:
        main = cursor_for(self.conn)
        job_recorded = False
        try:
            try:
                phases = _plan_run(self.spec, self.project_dir, self.config, main)
            except Exception as e:
                if not isinstance(e, RunSpecError):
                    logger.error("Pipeline run could not be planned: %s", e, exc_info=True)
                else:
                    logger.error("%s", e)
                self.tally.has_error = True
                self._end("failed", str(e), crashed=True)
                return self.result
            if self.spec.ordered:
                self.result.steps = [StepResult(p.index, p.action, p.targets) for p in phases]
            total = sum(len(p.nodes) for p in phases)
            job_recorded = self._record_job_run(main, total)
            self.emit("run_start", run_id=self.run_id, total=total)
            if not total:
                self._end("success")
                return self.result
            self._execute(phases, main)
        except Exception as e:
            logger.error("Pipeline run failed: %s", e, exc_info=True)
            self.tally.has_error = True
            self._end("failed", str(e), crashed=True)
        finally:
            if job_recorded:
                self._finish_job_run()
            try:
                main.close()
            except Exception:
                pass
        return self.result

    def _execute(self, phases: list[_Phase], main: Any) -> None:
        from havn.engine.database import ensure_meta_table
        from havn.engine.instrumentation import instrument_run

        spec = self.spec
        tally = self.tally
        ensure_meta_table(main)

        # Every schema a worker writes to exists before the first worker
        # starts: two workers racing CREATE SCHEMA IF NOT EXISTS hit DuckDB's
        # catalog write-write conflict.
        models = [m for p in phases for m in p.models]
        kinds = {n.kind for p in phases for n in p.nodes.values()}
        schemas = {"landing", "bronze", "silver", "gold"} if kinds - {"transform"} else set()
        schemas |= {m.schema for m in models if m.schema}
        if "seed" in kinds:
            schemas.add("seeds")
        for schema in sorted(schemas):
            main.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        # The same race on the one _havn table a build creates lazily (every
        # other one it writes comes from ensure_meta_table above, and the perf
        # tables from instrument_run below, both before any worker starts):
        # save_model_lineage swallows the conflict, so a model lost its row.
        if models:
            from havn.engine.governance.lineage import ensure_lineage_table

            ensure_lineage_table(main)
            self.rewind_id = self._start_rewind()

        crashed = True
        try:
            with instrument_run(
                main, project_dir=self.project_dir, pipeline_run_id=self.run_id,
                kind=spec.operation, name=spec.name or None,
            ):
                carry = ""  # the failures of an earlier step, which stop the later ones
                first_error: str | None = None
                for phase in phases:
                    if self.cancel.is_set():
                        tally.cancelled = True
                        break
                    failures, step_error = self._run_phase(phase, main, carry)
                    if tally.gate_failed or tally.cancelled:
                        break
                    if failures and not carry:
                        carry = ", ".join(sorted(set(failures)))
                        first_error = step_error
                if not tally.cancelled:
                    # Workers are done; nothing writes run_profiles any more.
                    from havn.engine.transform.orchestration import detect_run_anomalies

                    detect_run_anomalies(main, self.run_profiles, self.project_dir)
            crashed = False

            status = "cancelled" if tally.cancelled else ("failed" if tally.has_error else "success")
            error = None
            if status == "failed":
                if tally.gate_failed:
                    error = "pre-build validation failed"
                else:
                    error = first_error or f"{tally.failed} step(s) failed"

            if self.rewind_id:
                self._finish_rewind()
                self.rewind_id = None

            if spec.audit_resource:
                try:
                    from havn.engine.audit import log_audit

                    duration_s = round(time.perf_counter() - self.started, 1)
                    log_audit(main, user=(spec.user or {}).get("username", "anonymous"),
                              action="transform", resource=spec.audit_resource,
                              detail=f"{spec.audit_detail}: {status} in {duration_s}s")
                except Exception:
                    pass

            self._end(status, error)
            self._notify(main)
        finally:
            if self.rewind_id:
                self._finish_rewind(crashed=crashed)
                self.rewind_id = None

    # -- one phase -----------------------------------------------------------

    def _run_phase(self, phase: _Phase, main: Any, carry: str) -> tuple[list[str], str | None]:
        """Run one phase. Returns the labels of its failed nodes and its step error."""
        from havn.engine.database import log_run
        from havn.engine.prebuild_gate import run_prebuild_gate
        from havn.engine.transform.discovery import blocked_models
        from havn.engine.transform.orchestration import _evaluate_deny_rules

        spec = self.spec
        tally = self.tally
        nodes = phase.nodes
        models = phase.models
        step = self.result.steps[phase.index] if spec.ordered else None
        if not nodes:
            if step is not None and not carry:
                step.ran = True
                step.results = {} if phase.action in ("transform", "seed") else []
            return [], None
        if step is not None and not carry:
            step.ran = True
            self.emit("step_start", index=phase.index, action=phase.action, targets=list(phase.targets))

        workers = 1 if phase.sequential else self.max_workers

        # Pre-build validation. With ingest scripts or seeds in this phase,
        # the tables they load may not exist yet, so the gate waits until
        # every one of them has finished and runs just before the first
        # transform is submitted; no transform starts before that.
        loaders_left = sum(1 for n in nodes.values() if n.kind in ("ingest", "seed"))
        gate_pending = spec.gate and bool(models) and loaders_left > 0 and not carry
        if spec.gate and models and not loaders_left and not carry:
            if not run_prebuild_gate(self.conn, models, self._emit_validation,
                                     project_dir=self.project_dir):
                tally.gate_failed = tally.has_error = True
                if step is not None:
                    step.ok, step.error, step.results = False, "pre-build validation failed", {}
                    self.emit("step_end", index=phase.index, action=phase.action, ok=False,
                              error=step.error, attempts=1)
                return [], "pre-build validation failed"

        # State shared by the workers' build_one_model calls (see its
        # docstring for why no lock is needed). A rejected parent outside
        # this selection still blocks its children, as in `havn transform`.
        selected = {m.full_name for m in models}
        results: dict[str, str] = {}
        blocked: set[str] = (blocked_models(main) - selected) if models else set()
        denied = _evaluate_deny_rules(models, self.project_dir) if (models and not carry) else {}
        for name, reason in denied.items():
            results[name] = "policy_denied"
            blocked.add(name)
            try:
                log_run(main, "transform", name, "error", 0, 0,
                        error=f"policy_denied: {reason}", pipeline_run_id=self.run_id)
            except Exception:
                pass

        result_q: queue.Queue = queue.Queue()
        outcomes: dict[str, dict] = {}
        attempts_max = [1]

        def run_once(node: _Node, cur: Any) -> dict:
            if node.kind in ("ingest", "export"):
                from havn.engine.runner import run_script

                res = run_script(cur, node.path, node.kind, pipeline_run_id=self.run_id,
                                 force=spec.force, run_as=spec.user or None)
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
                    log_run(cur, "seed", node.name, "error", error=str(e), pipeline_run_id=self.run_id)
                    return {"status": "error", "error": str(e)}
                return {"status": res["status"], "duration_ms": res["duration_ms"],
                        "row_count": res["row_count"]}
            from havn.engine.transform.orchestration import build_one_model

            model = node.model
            outcome = build_one_model(
                cur, model, phase.model_map, results, blocked,
                force=spec.force, project_dir=self.project_dir,
                rewind_config=self.config.rewind if self.rewind_id else None,
                run_id=self.rewind_id,
                pipeline_run_id=self.run_id, run_profiles=self.run_profiles, echo=spec.echo,
            )
            error = outcome.error
            if outcome.status == "policy_denied":
                error = f"policy_denied: {denied.get(model.full_name, '')}".rstrip(": ")
            return {"status": outcome.status, "duration_ms": outcome.duration_ms,
                    "row_count": outcome.row_count, "error": error}

        def retryable(res: dict) -> bool:
            # A script still running after its timeout owns its cursor;
            # running it again would race it. Assertion failures, stale
            # sources and denials come out the same on a second try.
            return res.get("status") == "error" and not res.get("orphaned")

        def exec_node(nid: str) -> None:
            node = nodes[nid]
            cur = cursor_for(self.conn)
            result_q.put(("__start__", nid, None))
            try:
                attempt = 0
                while True:
                    try:
                        res = run_once(node, cur)
                    except Exception as e:
                        logger.error("Node %s failed: %s", nid, e, exc_info=True)
                        if node.kind == "transform":
                            try:
                                log_run(cur, "transform", node.name, "error",
                                        error=str(e), pipeline_run_id=self.run_id)
                            except Exception:
                                pass
                        res = {"status": "error", "error": str(e)}
                    if attempt >= spec.retries or not retryable(res) or self.cancel.is_set():
                        break
                    attempt += 1
                    logger.warning("Pipeline node %s failed; retry %d/%d after %ss",
                                   node.name, attempt, spec.retries, spec.retry_delay)
                    result_q.put(("__retry__", nid, attempt))
                    if self._wait(spec.retry_delay):
                        break
                    if node.kind == "transform":
                        # Let build_one_model take the model again.
                        results.pop(node.name, None)
                        blocked.discard(node.name)
                res["attempts"] = attempt + 1
                result_q.put(("__end__", nid, res))
            finally:
                cur.close()

        sorter = TopologicalSorter(phase.deps)
        sorter.prepare()
        executor = ThreadPoolExecutor(max_workers=workers)
        active = 0
        pending: list[str] = []

        # A node that failed in this run, or that is held back because
        # something above it failed, maps to the failures it carries
        # ("ingest/orders.py"). Nothing that depends on it runs. Exports
        # are leaves, so a failed export blocks nothing in its phase.
        poisoned: dict[str, str] = {}
        # A model that was not built because something above it was held
        # back without failing (a stale error-severity source, a parent
        # rejected on an earlier run) maps to its own name. Models below it
        # are held back too, also through models this run does not select,
        # which build_one_model cannot see (it checks direct parents only).
        # That does not fail the run, and exports are not held back by it.
        held: dict[str, str] = {}
        dependents: dict[str, set[str]] = {}
        for nid, waits in phase.deps.items():
            for dep in waits:
                dependents.setdefault(dep, set()).add(nid)
        failures: list[str] = []

        def end_event(node: _Node, res: dict, *, held_back: bool = False) -> None:
            self.emit(
                "node_end", id=node.id, kind=node.kind, name=node.name,
                num=self._number(node.id),
                materialized=node.model.materialized if node.kind == "transform" else None,
                status=res.get("status", "error"), duration_ms=res.get("duration_ms", 0),
                row_count=res.get("row_count", 0), rows_affected=res.get("rows_affected", 0),
                error=res.get("error"), held=held_back,
            )

        def hold_back(nid: str, reasons: list[str], *, failed: bool) -> None:
            """Report ``nid`` as not run because of ``reasons`` above it."""
            node = nodes[nid]
            reason = ", ".join(sorted({r for rs in reasons for r in rs.split(", ")}))
            error = f"upstream {'failed' if failed else 'blocked'}: {reason}"
            if failed:
                poisoned[nid] = reason
            else:
                held[nid] = node.name
            if node.kind == "transform":
                # As build_one_model does for a blocked parent: a run_log skip
                # and the blocked set, no model state.
                results[node.name] = "skipped_upstream_blocked"
                blocked.add(node.name)
                tally.models_run.append(node.name)
                try:
                    log_run(main, "transform", node.name, "skipped", 0, 0,
                            error=error, pipeline_run_id=self.run_id)
                except Exception:
                    pass
            res = {"status": "skipped_upstream_blocked", "error": error}
            outcomes[nid] = res
            end_event(node, res, held_back=True)
            tally.skipped += 1
            sorter.done(nid)

        def admit(ready) -> None:
            """Queue the nodes that became ready, holding back the poisoned ones."""
            todo = list(ready)
            while todo:
                nid = todo.pop(0)
                waits = phase.deps.get(nid, ())
                reasons = [poisoned[d] for d in waits if d in poisoned]
                if carry:
                    reasons.append(carry)
                soft = [held[d] for d in waits if d in held] if nodes[nid].kind == "transform" else []
                if reasons or soft:
                    hold_back(nid, reasons or soft, failed=bool(reasons))
                    todo.extend(sorter.get_ready())
                else:
                    pending.append(nid)

        def doomed_models() -> set[str]:
            """Models below a failure in this phase, held back or about to be."""
            doomed: set[str] = set()
            stack = list(poisoned)
            while stack:
                for nxt in dependents.get(stack.pop(), ()):
                    if nxt not in doomed:
                        doomed.add(nxt)
                        stack.append(nxt)
            return {nodes[n].name for n in doomed if nodes[n].kind == "transform"}

        def maybe_run_gate() -> bool:
            nonlocal gate_pending
            if gate_pending:
                gate_pending = False
                # Only what can still be built: a model whose loader failed
                # has nothing to validate against and will not run.
                doomed = doomed_models()
                live = [m for m in models if m.full_name not in doomed]
                if live and not run_prebuild_gate(self.conn, live, self._emit_validation,
                                                  project_dir=self.project_dir):
                    tally.gate_failed = True
            return not tally.gate_failed

        def submit_ready() -> None:
            nonlocal active
            i = 0
            while i < len(pending) and active < workers:
                if nodes[pending[i]].kind == "transform":
                    if gate_pending and loaders_left:
                        i += 1  # held back until the loaders are done
                        continue
                    if not maybe_run_gate():
                        return  # the loop below sees gate_failed and stops
                nid = pending.pop(i)
                self._number(nid)
                try:
                    executor.submit(exec_node, nid)
                except RuntimeError:
                    return  # interpreter shutting down
                active += 1

        try:
            admit(sorter.get_ready())
            submit_ready()
            while sorter.is_active() or active > 0:
                if tally.gate_failed and active == 0:
                    tally.has_error = True
                    break
                if self.cancel.is_set():
                    tally.cancelled = True
                    break
                try:
                    tag, nid, res = result_q.get(timeout=2)
                except queue.Empty:
                    continue
                node = nodes[nid]
                if tag == "__start__":
                    data = {"id": nid, "kind": node.kind, "name": node.name,
                            "num": self._number(nid),
                            "materialized": node.model.materialized if node.kind == "transform" else None}
                    self.emit("node_start", **data)
                    continue
                if tag == "__retry__":
                    attempts_max[0] = max(attempts_max[0], res + 1)
                    self.emit("retry", kind=node.kind, name=node.name, action=node.kind,
                              attempt=res, retries=spec.retries, delay=spec.retry_delay)
                    continue

                active -= 1
                outcomes[nid] = res
                end_event(node, res)

                raw = res.get("status", "error")
                if node.kind in ("ingest", "seed"):
                    loaders_left -= 1
                if node.kind == "transform":
                    tally.models_run.append(node.name)
                    if raw == "assertion_failed":
                        tally.assertion_failed += 1
                    elif raw == "built":
                        tally.built += 1
                    elif raw in ("source_stale", "skipped_upstream_blocked"):
                        held[nid] = node.name
                if raw in FAILED:
                    tally.has_error = True
                    tally.failed += 1
                    failures.append(node.label)
                    if node.kind != "export":
                        poisoned[nid] = node.label
                elif raw in SKIPPED:
                    tally.skipped += 1
                else:
                    tally.completed += 1

                sorter.done(nid)
                admit(sorter.get_ready())
                submit_ready()
        finally:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

        step_error = self._step_error(phase, outcomes) if failures else None
        if tally.gate_failed:
            step_error = "pre-build validation failed"
        if step is not None and step.ran:
            step.attempts = attempts_max[0]
            step.results = self._step_results(phase, outcomes)
            step.ok = not failures and not tally.gate_failed and not tally.cancelled
            step.error = step_error
            self.emit("step_end", index=phase.index, action=phase.action, ok=step.ok,
                      error=step.error, attempts=step.attempts)
        for nid, res in outcomes.items():
            n = nodes[nid]
            self.result.nodes.append({"phase": phase.index, "kind": n.kind, "name": n.name,
                                      "status": res.get("status"), "error": res.get("error")})
        return failures, step_error

    def _emit_validation(self, event: str, data: dict) -> None:
        self.emit(event, **data)

    @staticmethod
    def _step_results(phase: _Phase, outcomes: dict[str, dict]):
        ordered = [(phase.nodes[nid], outcomes[nid]) for nid in phase.nodes if nid in outcomes]
        if phase.action in ("ingest", "export"):
            return [res.get("status") for _, res in ordered]
        return {node.name: res.get("status") for node, res in ordered}

    @staticmethod
    def _step_error(phase: _Phase, outcomes: dict[str, dict]) -> str:
        failed = [(phase.nodes[nid], res) for nid, res in outcomes.items()
                  if res.get("status") in FAILED]
        action = phase.action
        if action == "transform" or (action is None and all(n.kind == "transform" for n, _ in failed)):
            errors = sum(1 for _, r in failed if r.get("status") == "error")
            asserts = sum(1 for _, r in failed if r.get("status") == "assertion_failed")
            denied = sum(1 for _, r in failed if r.get("status") == "policy_denied")
            msg = f"transform failed: {errors} errors, {asserts} assertion failures"
            return msg + (f", {denied} policy denials" if denied else "")
        names = ", ".join(n.name for n, _ in failed)
        return f"{action or 'pipeline'} failed: {names}"

    # -- bookkeeping ---------------------------------------------------------

    def _record_job_run(self, main: Any, total: int) -> bool:
        """A job_runs row, so the run appears in Job Results."""
        if self.spec.job is None:
            return False
        try:
            from havn.engine.orchestration import ensure_job_runs_table

            ensure_job_runs_table(main)
            job_name, job_file, target = self.spec.job
            main.execute(
                "INSERT INTO _havn.job_runs (id, job_name, job_file, target, status, steps_total, trigger) "
                "VALUES (?, ?, ?, ?, 'running', ?, ?)",
                [self.run_id, job_name, job_file, target, total, self.spec.job_trigger],
            )
            return True
        except Exception:
            logger.debug("Failed to insert job_run for %s", self.spec.label, exc_info=True)
            return False

    def _finish_job_run(self) -> None:
        tally = self.tally
        try:
            from havn.engine.orchestration import ensure_job_runs_table

            cur = cursor_for(self.conn)
            try:
                ensure_job_runs_table(cur)
                status = "failure" if tally.has_error else ("cancelled" if tally.cancelled else "success")
                cur.execute(
                    "UPDATE _havn.job_runs SET status = ?, finished_at = current_timestamp, "
                    "duration_ms = ?, steps_completed = ?, steps_failed = ?, steps_skipped = ?, "
                    "error = ? WHERE id = ?",
                    [status, int((time.perf_counter() - self.started) * 1000),
                     tally.completed, tally.failed, tally.skipped, self.result.error, self.run_id],
                )
            finally:
                cur.close()
        except Exception:
            logger.debug("Failed to update job_run for %s", self.spec.label, exc_info=True)

    def _start_rewind(self) -> str | None:
        """A Pipeline Rewind run for this run's snapshots."""
        if not self.config.rewind.enabled:
            return None
        try:
            from havn.engine.snapshots import start_run

            with rewind_lock():
                return start_run(self.project_dir, trigger=self.spec.rewind_trigger)
        except Exception as e:
            logger.warning("Could not start a Rewind run: %s", e)
            return None

    def _finish_rewind(self, *, crashed: bool = False) -> None:
        tally = self.tally
        try:
            from havn.engine.snapshots import RewindConfig, finish_run, run_gc

            # As `havn transform`: failed on an error (or when the run itself
            # broke off or the gate refused it), partial when only assertions
            # failed or the run was cancelled, else success.
            hard = tally.failed - tally.assertion_failed
            status = (
                "failed" if (hard or crashed or tally.gate_failed)
                else "partial" if (tally.assertion_failed or tally.cancelled)
                else "success"
            )
            rw = self.config.rewind
            with rewind_lock():
                finish_run(self.project_dir, self.rewind_id, status,
                           list(dict.fromkeys(tally.models_run)))
                deleted = run_gc(self.project_dir, RewindConfig(
                    enabled=rw.enabled, retention=rw.retention, max_storage=rw.max_storage,
                    dedup=rw.dedup, exclude=rw.exclude,
                ))
            if deleted:
                self.emit("gc", deleted=deleted)
        except Exception as e:
            logger.warning("Rewind bookkeeping failed: %s", e)

    def _notify(self, main: Any) -> None:
        """The stream's webhook and the project's pipeline alert."""
        spec = self.spec
        r = self.result
        tally = self.tally
        if r.status == "cancelled":
            return
        ok = r.ok
        if spec.webhook_url:
            err = send_stream_webhook(spec.webhook_url, spec.name, "success" if ok else "failed",
                                      r.duration_s)
            if err:
                logger.warning("Webhook failed for stream %s: %s", spec.name, err)
            self.emit("webhook", url=spec.webhook_url, error=err)
        if spec.alert is None:
            return
        if spec.alert == "when_built":
            # `havn transform`: a failure alert when it failed, a success
            # alert only when it built something.
            if ok and not tally.built:
                return
            errors = tally.failed - tally.assertion_failed
            error = f"{errors} errors, {tally.assertion_failed} assertion failures"
        else:
            error = r.error or f"{tally.failed} step(s) failed"
        try:
            from havn.engine.pipeline_alerts import send_pipeline_alert

            sent = send_pipeline_alert(
                self.config.alerts, spec.alert_target or spec.name, ok=ok, duration_s=r.duration_s,
                conn=main, error=None if ok else error, models_built=tally.built,
            )
            if sent:
                self.emit("alert", results=sent)
        except Exception as e:
            logger.debug("Pipeline alert failed: %s", e)


def rewind_lock():
    """The lock around Pipeline Rewind's run bookkeeping and snapshot capture.

    ``build_one_model`` takes it around every snapshot capture; the engine
    takes it around starting, finishing and garbage-collecting a Rewind run,
    for every caller (CLI, scheduler, web UI). Rewind's metadata database is
    reached through per-call cursors (``snapshots._ensure_meta_db``), so the
    lock is not what keeps the connection safe across threads; it keeps GC
    from deleting files a capture is writing.
    """
    from havn.engine.transform.orchestration import _SNAPSHOT_CAPTURE_LOCK

    return _SNAPSHOT_CAPTURE_LOCK
