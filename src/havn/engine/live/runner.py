"""The live runner: listen for advances, coalesce them, refresh in DAG order.

One daemon thread per process. It wakes on a :class:`SourceAdvanced` event
(or on its poll timer), waits a moment for the burst to finish, then walks
the live models in topological order and refreshes each one that is behind.
A chain bronze -> silver -> gold therefore moves in one cycle: bronze's
refresh publishes its watermark, and silver, next in line, sees it.

Writes
    Every refresh is submitted to the executor -- the server's write queue,
    or the one ``havn live`` creates -- and runs on a cursor of its single
    write connection, so live refreshes queue behind (and in front of) every
    other write instead of opening a second writer. A refresh is one
    transaction: data, consumed watermarks, the model's own watermark and its
    ``model_state`` commit together or not at all.

Coexisting with batch runs
    The refresh holds the model's build lock (``transform/locks.py``) for
    the whole transaction and asks for it without waiting: if a batch run or
    a job is building the model, the runner leaves it and looks again next
    cycle. Batch builds of a live model do the same watermark bookkeeping,
    so whichever gets there first applies the pending batch and the other
    finds nothing to do.

Failures
    A refresh that errors, or whose error-severity assertion fails, is
    rolled back -- the bad batch never becomes visible downstream -- and the
    model is marked failing with exponential backoff (``backoff_base``
    doubling up to ``backoff_max``). An alert goes out on the first failure
    and when it recovers. Live models downstream of a failing or paused one
    wait; sources upstream keep landing, and the model catches up on
    everything that queued once it recovers.

Logging
    A refresh every second would bury ``run_log``. Each live model gets one
    aggregated ``run_type='live'`` row per ``log_interval`` (refresh count,
    events applied, average and maximum lag); failures and pause/resume are
    logged as they happen.

Assertions and profiling
    ``@assert`` runs inside the refresh transaction, against the table as it
    will be after the commit, on every refresh by default
    (``assertion_interval: 0``), so a failing check stops the batch from
    landing. A large model can space them out with ``assertion_interval``;
    a batch run always checks. Profiling, a full scan, runs at most every
    ``profile_interval``.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Protocol

import duckdb

from havn.engine.transform.models import SQLModel

from . import events
from .graph import LiveGraph
from .settings import LiveSettings
from .state import (
    ModelLiveState,
    consumed_watermarks,
    ensure_live_tables,
    load_states,
    prune_advances,
    save_state,
    source_watermarks,
    utcnow,
)

logger = logging.getLogger("havn.live")


class Executor(Protocol):
    """What the runner needs from the write queue."""

    def submit(self, func: Callable, *args: Any, **kwargs: Any) -> Future: ...


@dataclass
class RefreshResult:
    model: str
    status: str            # built | idle | busy | error | assertion_failed | skipped
    duration_ms: int = 0
    events: int = 0
    lag_ms: int | None = None
    error: str | None = None
    reason: str = ""


@dataclass
class _Accum:
    refreshes: int = 0
    events: int = 0
    duration_ms: int = 0
    lag_sum_ms: int = 0
    lag_n: int = 0
    lag_max_ms: int = 0


@dataclass
class _UIEvent:
    id: int
    type: str
    data: dict = field(default_factory=dict)


def _sources_signature(project_dir: Path) -> tuple:
    """Cheap fingerprint of every file discovery reads."""
    entries = []
    for base in ("transform", "havn_packages"):
        root = project_dir / base
        if not root.exists():
            continue
        for path in root.rglob("*.sql"):
            try:
                st = path.stat()
            except OSError:
                continue
            entries.append((str(path), st.st_mtime_ns, st.st_size))
    for name in ("project.yml",):
        p = project_dir / name
        if p.exists():
            st = p.stat()
            entries.append((str(p), st.st_mtime_ns, st.st_size))
    return tuple(sorted(entries))


class LiveRunner:
    """Refresh live models as their sources advance. See the module docstring."""

    def __init__(
        self,
        project_dir: Path,
        executor: Executor,
        *,
        settings: LiveSettings | None = None,
        alerts: object | None = None,
        models_loader: Callable[[], list[SQLModel]] | None = None,
    ) -> None:
        self.project_dir = Path(project_dir)
        self.executor = executor
        self.settings = settings or LiveSettings()
        self.alerts = alerts
        self._models_loader = models_loader
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._unsubscribe: Callable[[], None] | None = None

        self._models: list[SQLModel] = []
        self._model_map: dict[str, SQLModel] = {}
        self._graph = LiveGraph(models={})
        self._signature: tuple | None = None
        self._next_sig_check = 0.0
        self.discovery_error: str | None = None

        self._states: dict[str, ModelLiveState] = {}
        self._refreshing: str | None = None
        self._pending_since: float | None = None
        self._last_event_at = 0.0
        self._last_cycle_start = 0.0
        self._last_cycle_wall: datetime | None = None
        self._next_poll = 0.0
        self._next_prune = 0.0
        self._last_log_flush = time.monotonic()
        self._accum: dict[str, _Accum] = {}
        self._force: set[str] = set()

        self.started_at: datetime | None = None
        self.cycles = 0
        self.refreshes = 0
        self.events_seen = 0
        self.last_cycle_at: datetime | None = None
        self.last_cycle_ms = 0

        self._ui_events: deque[_UIEvent] = deque(maxlen=500)
        self._ui_seq = 0
        self._ui_cond = threading.Condition()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.started_at = utcnow()
        self._load_models(force=True)
        self._call(self._load_states)
        self._unsubscribe = events.subscribe(self._on_advance)
        # Catch up on anything that landed while no runner was up.
        with self._lock:
            self._pending_since = time.monotonic()
            self._last_event_at = 0.0
        self._thread = threading.Thread(target=self._run, name="havn-live", daemon=True)
        self._thread.start()
        self._emit("runner", {"running": True})

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        with self._lock:
            self._wake.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        try:
            self._flush_logs(force=True)
        except Exception as e:
            logger.debug("final live log flush skipped: %s", e)
        self._emit("runner", {"running": False})

    def wake(self) -> None:
        """Run a cycle as soon as the rate limit allows."""
        with self._lock:
            if self._pending_since is None:
                self._pending_since = time.monotonic()
            self._last_event_at = 0.0
            self._wake.notify_all()

    # ------------------------------------------------------------------
    # Controls (from the API / CLI)
    # ------------------------------------------------------------------

    def pause(self, model: str) -> dict:
        model = model.lower()
        self._require_live(model)
        state = self._state(model)
        state.paused = True
        state.status = "paused"
        self._persist(state)
        self._call(self._log_event, model, "paused", None)
        self._emit("state", {"model": model, "status": "paused"})
        return state.to_dict()

    def resume(self, model: str) -> dict:
        model = model.lower()
        self._require_live(model)
        state = self._state(model)
        state.paused = False
        state.status = "active"
        state.consecutive_failures = 0
        state.next_retry_at = None
        self._persist(state)
        self._call(self._log_event, model, "resumed", None)
        self._emit("state", {"model": model, "status": "active"})
        self.wake()
        return state.to_dict()

    def refresh_now(self, model: str) -> None:
        """Retry a failing model now instead of waiting out its backoff."""
        model = model.lower()
        self._require_live(model)
        with self._lock:
            state = self._state(model)
            state.next_retry_at = None
            self._force.add(model)
        self.wake()

    def _require_live(self, model: str) -> None:
        if model not in self._graph.order:
            raise KeyError(f"{model} is not a live model")

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _on_advance(self, event: events.SourceAdvanced) -> None:
        with self._lock:
            self.events_seen += event.rows
            now = time.monotonic()
            if self._pending_since is None:
                self._pending_since = now
            self._last_event_at = now
            self._wake.notify_all()
        self._emit("advance", {
            "source": event.source, "watermark": event.watermark,
            "rows": event.rows, "kind": event.kind,
        })

    def _emit(self, type_: str, data: dict) -> None:
        with self._ui_cond:
            self._ui_seq += 1
            self._ui_events.append(_UIEvent(self._ui_seq, type_, dict(data, ts=time.time())))
            self._ui_cond.notify_all()

    def events_since(self, after: int, timeout: float = 0.0) -> list[dict]:
        """UI events with an id above ``after``, waiting up to ``timeout``."""
        deadline = time.monotonic() + timeout
        with self._ui_cond:
            while True:
                out = [
                    {"id": e.id, "type": e.type, "data": e.data}
                    for e in self._ui_events if e.id > after
                ]
                remaining = deadline - time.monotonic()
                if out or remaining <= 0 or self._stop.is_set():
                    return out
                self._ui_cond.wait(remaining)

    @property
    def last_event_id(self) -> int:
        return self._ui_seq

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _cycle_due_at(self) -> float | None:
        if self._pending_since is None:
            return None
        s = self.settings
        closes = min(self._last_event_at + s.debounce, self._pending_since + s.max_latency)
        return max(self._last_cycle_start + s.min_interval, closes)

    def _run(self) -> None:
        s = self.settings
        self._next_poll = time.monotonic() + s.poll_interval
        while not self._stop.is_set():
            run_now = False
            with self._lock:
                now = time.monotonic()
                retry_in = self._next_retry_in()
                if retry_in is not None and retry_in <= 0 and self._pending_since is None:
                    self._pending_since = now
                due = self._cycle_due_at()
                if (due is not None and due <= now) or now >= self._next_poll:
                    self._pending_since = None
                    self._last_cycle_start = now
                    self._last_cycle_wall = utcnow()
                    self._next_poll = now + s.poll_interval
                    run_now = True
                else:
                    waits = [
                        self._next_poll - now,
                        s.log_interval - (now - self._last_log_flush),
                    ]
                    if due is not None:
                        waits.append(due - now)
                    if retry_in is not None:
                        waits.append(retry_in)
                    self._wake.wait(max(min(waits), 0.01))
            if self._stop.is_set():
                break
            if run_now:
                try:
                    self.run_cycle()
                except Exception as e:  # pragma: no cover - defensive
                    logger.exception("live cycle failed: %s", e)
            self._maybe_flush_logs()

    def _next_retry_in(self) -> float | None:
        """Seconds until a failing model's backoff ends (only ones not yet tried)."""
        now = utcnow()
        since = self._last_cycle_wall
        waits = [
            (st.next_retry_at - now).total_seconds()
            for st in self._states.values()
            if st.status == "failing" and not st.paused and st.next_retry_at is not None
            and (since is None or st.next_retry_at > since)
        ]
        return min(waits) if waits else None

    # ------------------------------------------------------------------
    # One cycle
    # ------------------------------------------------------------------

    def run_cycle(self) -> list[RefreshResult]:
        """Refresh every live model that is behind, in DAG order."""
        started = time.perf_counter()
        self._load_models()
        results: list[RefreshResult] = []
        held: set[str] = set()
        with self._lock:
            order = list(self._graph.order)
            graph = self._graph
        for name in order:
            if self._stop.is_set():
                break
            state = self._state(name)
            ups = [u for u in graph.live_upstreams(name)]
            waiting_on = next((u for u in ups if u in held), None)
            if state.paused:
                held.add(name)
                continue
            if waiting_on:
                held.add(name)
                continue
            now = utcnow()
            forced = name in self._force
            if (
                state.status == "failing" and not forced
                and state.next_retry_at is not None and state.next_retry_at > now
            ):
                held.add(name)
                continue
            model = self._model_map[name]
            if (
                model.live_interval and state.last_refresh_at is not None and not forced
                and (now - state.last_refresh_at).total_seconds() < model.live_interval
            ):
                # Too soon for this model; come back when its interval is up.
                with self._lock:
                    self._pending_since = self._pending_since or time.monotonic()
                continue
            result = self._refresh(name)
            results.append(result)
            if result.status in ("error", "assertion_failed"):
                held.add(name)
            elif state.status == "failing" and result.status != "busy":
                held.add(name)
        self.cycles += 1
        self.last_cycle_at = utcnow()
        self.last_cycle_ms = int((time.perf_counter() - started) * 1000)
        if time.monotonic() >= self._next_prune:
            self._next_prune = time.monotonic() + 600
            try:
                self._call(lambda cur: prune_advances(cur))
            except Exception as e:
                logger.debug("advance prune skipped: %s", e)
        return results

    def _refresh(self, name: str) -> RefreshResult:
        with self._lock:
            self._refreshing = name
        try:
            result = self._call(self._refresh_on, name)
        except Exception as e:
            # Queue full (back-pressure) or the queue is gone: try again next
            # cycle without counting it against the model.
            from havn.engine.write_queue import WriteQueueFullError

            if isinstance(e, WriteQueueFullError):
                with self._lock:
                    self._pending_since = self._pending_since or time.monotonic()
                return RefreshResult(name, "busy", reason="write queue full")
            result = RefreshResult(name, "error", error=str(e))
            self._call(self._record_failure, name, result)
        finally:
            with self._lock:
                self._refreshing = None
                self._force.discard(name)
        if result.status in ("built", "error", "assertion_failed"):
            self._emit("refresh", {
                "model": name, "status": result.status, "duration_ms": result.duration_ms,
                "events": result.events, "lag_ms": result.lag_ms, "error": result.error,
                "reason": result.reason,
            })
        return result

    # --- Runs on the write-queue thread ---------------------------------

    def _call(self, func: Callable, *args: Any) -> Any:
        """Run ``func(cursor, *args)`` on the executor and wait for it."""
        from havn.engine.write_queue import cursor_for

        def _job(conn: duckdb.DuckDBPyConnection) -> Any:
            cur = cursor_for(conn)
            try:
                return func(cur, *args)
            finally:
                try:
                    cur.close()
                except Exception:
                    pass

        return self.executor.submit(_job).result()

    def _behind(self, cur: duckdb.DuckDBPyConnection, model: SQLModel) -> str:
        """Why ``model`` needs a refresh, or "" when it is up to date."""
        from havn.engine.transform.discovery import _has_changed, _is_blocked

        row = cur.execute(
            "SELECT table_type FROM information_schema.tables "
            "WHERE table_catalog = current_database() AND table_schema = ? AND table_name = ?",
            [model.schema, model.name],
        ).fetchone()
        wanted = "VIEW" if model.materialized == "view" else "BASE TABLE"
        if row is None or row[0] != wanted:
            # Nothing to build from until a source has committed through
            # advance_source: before that a landing table has no _havn_seq
            # and the model's SQL would not even bind.
            from .graph import tracked_sources

            srcs = tracked_sources(model, self._model_map)
            if srcs and not any(source_watermarks(cur, srcs).values()):
                return ""
            return "initial build"
        if _has_changed(cur, model):
            return "definition changed"
        if model.materialized != "incremental":
            return ""
        if _is_blocked(cur, model):
            return "blocked by a failed check"
        sources = self._graph.sources.get(model.full_name, [])
        current = source_watermarks(cur, sources)
        consumed = consumed_watermarks(cur, model.full_name)
        for s in sources:
            if current.get(s, 0) > consumed.get(s, 0):
                return "new data"
        return ""

    def _refresh_on(self, cur: duckdb.DuckDBPyConnection, name: str) -> RefreshResult:
        from havn.engine.transform.locks import model_lock
        from havn.engine.transform.orchestration import build_one_model
        from havn.engine.transform.quality import _save_assertions
        from havn.engine.utils import begin_transaction

        from .refresh import last_result

        model = self._model_map[name]
        state = self._state(name)
        with model_lock(name, timeout=0) as got:
            if not got:
                return RefreshResult(name, "busy", reason="a batch run is building it")
            reason = self._behind(cur, model)
            if not reason:
                if state.status == "failing":
                    # Nothing left to apply: a batch run got past the failure.
                    self._record_recovery(cur, name, None)
                return RefreshResult(name, "idle")
            now = utcnow()
            s = self.settings
            check = s.assertion_interval <= 0 or state.last_assertions_at is None or (
                (now - state.last_assertions_at).total_seconds() >= s.assertion_interval
            )
            prof = state.last_profile_at is None or (
                (now - state.last_profile_at).total_seconds() >= s.profile_interval
            )
            started = time.perf_counter()
            owns = begin_transaction(cur)
            try:
                outcome = build_one_model(
                    cur, model, self._model_map, {}, set(),
                    log_runs=False, assertions=check, profile=prof, echo=False,
                )
            except Exception as e:  # build_one_model catches; this is belt and braces
                outcome = None
                error = str(e)
            else:
                error = outcome.error
            status = outcome.status if outcome is not None else "error"
            committed = False
            if status == "built" and owns:
                try:
                    cur.execute("COMMIT")
                    committed = True
                except Exception as e:
                    status, error = "error", f"commit failed: {e}"
            if not committed and owns:
                try:
                    cur.execute("ROLLBACK")
                except Exception:
                    pass
            duration_ms = int((time.perf_counter() - started) * 1000)
            if status == "built":
                live = last_result(name) if model.materialized == "incremental" else None
                events_n = live.events if live else 0
                lag_ms = live.lag_ms if live else None
                if check and (model.assertions or model.grain):
                    state.last_assertions_at = now
                if prof and model.materialized != "view":
                    state.last_profile_at = now
                self._record_success(cur, name, duration_ms, events_n, lag_ms)
                return RefreshResult(name, "built", duration_ms, events_n, lag_ms, reason=reason)
            if status == "skipped":
                return RefreshResult(name, "idle", reason="nothing to build")
            if status == "assertion_failed" and outcome is not None:
                # The rollback took the stored results with it; keep them so
                # the Quality view shows what failed.
                try:
                    _save_assertions(cur, model, outcome.assertion_results)
                except Exception as e:
                    logger.debug("could not save live assertion results: %s", e)
            result = RefreshResult(name, status if status in ("assertion_failed",) else "error",
                                   duration_ms, error=error or status, reason=reason)
            self._record_failure(cur, name, result)
            return result

    def _record_success(
        self, cur: duckdb.DuckDBPyConnection, name: str, duration_ms: int, events_n: int, lag_ms: int | None
    ) -> None:
        state = self._state(name)
        was_failing = state.status == "failing"
        state.status = "active"
        state.consecutive_failures = 0
        state.next_retry_at = None
        state.last_error = None
        state.last_refresh_at = utcnow()
        state.last_duration_ms = duration_ms
        state.last_lag_ms = lag_ms
        state.refreshes += 1
        state.rows_total += events_n
        save_state(cur, state)
        self.refreshes += 1
        acc = self._accum.setdefault(name, _Accum())
        acc.refreshes += 1
        acc.events += events_n
        acc.duration_ms += duration_ms
        if lag_ms is not None:
            acc.lag_sum_ms += lag_ms
            acc.lag_n += 1
            acc.lag_max_ms = max(acc.lag_max_ms, lag_ms)
        if was_failing:
            self._record_recovery(cur, name, state)

    def _record_recovery(self, cur, name: str, state: ModelLiveState | None) -> None:
        state = state or self._state(name)
        if state.status == "failing":
            state.status = "active"
            state.consecutive_failures = 0
            state.next_retry_at = None
            state.last_error = None
            save_state(cur, state)
        self._log_event(cur, name, "recovered", None)
        self._alert(cur, "live_model_recovered", name, f"Live model `{name}` is refreshing again", {})
        self._emit("state", {"model": name, "status": "active"})

    def _record_failure(self, cur, name: str, result: RefreshResult) -> None:
        state = self._state(name)
        state.consecutive_failures += 1
        n = state.consecutive_failures
        delay = min(self.settings.backoff_max, self.settings.backoff_base * (2 ** (n - 1)))
        delay *= random.uniform(0.9, 1.1)
        state.status = "failing"
        state.last_error = (result.error or result.status)[:2000]
        state.next_retry_at = utcnow() + timedelta(seconds=delay)
        try:
            save_state(cur, state)
        except Exception as e:
            logger.warning("could not save live state for %s: %s", name, e)
        self._log_event(cur, name, "error", state.last_error, duration_ms=result.duration_ms)
        logger.warning("live model %s failed (%d in a row), retrying in %.1fs: %s",
                       name, n, delay, state.last_error)
        if n == 1:
            self._alert(
                cur, "live_model_failed", name,
                f"Live model `{name}` failed and is paused with backoff: {state.last_error}",
                {"error": state.last_error, "retry_in": f"{delay:.0f}s"},
            )
        self._emit("state", {"model": name, "status": "failing", "error": state.last_error,
                             "retry_in_s": round(delay, 1)})

    # ------------------------------------------------------------------
    # Logging, alerts, state
    # ------------------------------------------------------------------

    def _log_event(self, cur, name: str, status: str, error: str | None, duration_ms: int = 0) -> None:
        from havn.engine.database import log_run

        try:
            log_run(cur, "live", name, status if status == "error" else "success",
                    duration_ms, 0, error=error,
                    log_output=None if status == "error" else f"live: {status}")
        except Exception as e:
            logger.debug("live run_log write failed: %s", e)

    def _maybe_flush_logs(self) -> None:
        if time.monotonic() - self._last_log_flush >= self.settings.log_interval:
            try:
                self._flush_logs()
            except Exception as e:
                logger.debug("live log flush failed: %s", e)

    def _flush_logs(self, force: bool = False) -> None:
        with self._lock:
            accum, self._accum = self._accum, {}
            self._last_log_flush = time.monotonic()
        accum = {k: v for k, v in accum.items() if v.refreshes}
        if not accum:
            return

        def _write(cur) -> None:
            from havn.engine.database import log_run

            for name, a in accum.items():
                avg = (a.lag_sum_ms / a.lag_n / 1000) if a.lag_n else 0.0
                summary = (
                    f"{a.refreshes} live refresh{'es' if a.refreshes != 1 else ''}, "
                    f"{a.events} event{'s' if a.events != 1 else ''}, "
                    f"lag avg {avg:.2f}s max {a.lag_max_ms / 1000:.2f}s"
                )
                log_run(cur, "live", name, "success", a.duration_ms, a.events, log_output=summary)

        self._call(_write)

    def _alert(self, cur, alert_type: str, target: str, message: str, details: dict) -> None:
        if self.alerts is None:
            return
        if alert_type == "live_model_failed" and not getattr(self.alerts, "on_failure", True):
            return
        try:
            from havn.engine.alerts import Alert, AlertConfig, send_alert

            cfg = AlertConfig(
                slack_webhook_url=getattr(self.alerts, "slack_webhook_url", None),
                webhook_url=getattr(self.alerts, "webhook_url", None),
                channels=list(getattr(self.alerts, "channels", None) or []),
            )
            if not cfg.channels and not cfg.slack_webhook_url and not cfg.webhook_url:
                cfg.channels = ["log"]
            send_alert(Alert(alert_type, target, message, details), cfg, cur)
        except Exception as e:
            logger.warning("live alert failed: %s", e)

    def _state(self, name: str) -> ModelLiveState:
        with self._lock:
            state = self._states.get(name)
            if state is None:
                state = self._states[name] = ModelLiveState(model=name)
            return state

    def _persist(self, state: ModelLiveState) -> None:
        self._call(lambda cur: save_state(cur, state))

    def _load_states(self, cur) -> None:
        ensure_live_tables(cur)
        loaded = load_states(cur)
        with self._lock:
            self._states.update(loaded)

    # ------------------------------------------------------------------
    # Models
    # ------------------------------------------------------------------

    def _discover(self) -> list[SQLModel]:
        if self._models_loader is not None:
            return self._models_loader()
        from havn.engine.transform.discovery import discover_all_models

        return discover_all_models(self.project_dir)

    def _load_models(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now < self._next_sig_check:
            return
        self._next_sig_check = now + 2.0
        sig = _sources_signature(self.project_dir) if self._models_loader is None else None
        if not force and sig is not None and sig == self._signature:
            return
        try:
            models = self._discover()
        except Exception as e:
            self.discovery_error = str(e)
            logger.warning("live: model discovery failed, keeping the last good set: %s", e)
            return
        from havn.engine.transform.orchestration import _hash_full_dag

        try:
            _hash_full_dag(models, models)
        except Exception as e:
            self.discovery_error = str(e)
            return
        graph = LiveGraph.build(models)
        with self._lock:
            self._models = models
            self._model_map = {m.full_name: m for m in models}
            self._graph = graph
            self._signature = sig
            self.discovery_error = None

    @property
    def graph(self) -> LiveGraph:
        return self._graph

    @property
    def models(self) -> list[SQLModel]:
        return list(self._models)

    def snapshot(self) -> dict:
        """What only the runner knows, merged into ``status.live_status``."""
        with self._lock:
            models = {
                name: {
                    **self._state(name).to_dict(),
                    "refreshing": self._refreshing == name,
                }
                for name in self._graph.order
            }
            return {
                "running": self.running,
                "started_at": self.started_at.isoformat() + "Z" if self.started_at else None,
                "cycles": self.cycles,
                "refreshes": self.refreshes,
                "events_seen": self.events_seen,
                "last_cycle_at": self.last_cycle_at.isoformat() + "Z" if self.last_cycle_at else None,
                "last_cycle_ms": self.last_cycle_ms,
                "pending": self._pending_since is not None,
                "discovery_error": self.discovery_error,
                "models": models,
            }

    def status(self) -> dict:
        """The full status payload, read through the write queue's connection."""
        from .status import live_status

        snap = self.snapshot()
        return self._call(lambda cur: live_status(cur, self.models, runner=snap, settings=self.settings))
