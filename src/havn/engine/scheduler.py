"""Scheduler: a dispatch loop that hands due runs to a bounded worker pool.

``SchedulerThread`` wakes every 30 seconds, works out which schedules are
due and hands their runs to a pool of worker threads. It never runs a
stream or job itself, so it keeps ticking (and keeps recording what is due,
late or skipped) while a long run executes. What it schedules:

- streams in project.yml with a cron ``schedule`` (run through the shared
  stream runner, ``engine.stream_runner``, as ``havn stream`` does);
- orchestration jobs (``orchestration/*.yml``) with cron or interval
  (``every 2h``) schedules;
- scheduled dashboard reports (one check per tick, also in the pool).

Pool size is ``scheduler.max_concurrent`` (default 1, re-read every tick).
One worker means runs still execute one at a time, as before: concurrent
runs against one DuckDB warehouse can fail on write-write conflicts (two
ingest scripts replacing the same landing table; havn's own schema and
table bootstrap is serialised by ``engine.catalog_ddl``), so concurrency is
opt-in. See docs/scheduler.md.

One scheduler per warehouse: ``start()`` / ``_startup()`` take an OS-level
lock (``engine.scheduler_lock``) held until the scheduler stops. A
schedule's cron clock starts after its newest recorded fire, so a fire the
history holds is never dispatched again.

Per schedule (stream or job):

- ``overlap: skip`` (default): a fire while the same stream/job is running
  or already queued is recorded as ``skipped_overlap``.
- ``overlap: queue``: at most one run waits behind the running one; further
  fires are skipped.
- ``timezone``: cron is evaluated in that IANA zone (``engine.schedule_time``
  has the DST rules); default is the server's local time.
- ``catchup``: on start, fires that passed while no scheduler was running
  are recorded as ``missed`` (from ``_havn.schedule_history`` and the
  heartbeat in ``_havn.scheduler_state``); with ``catchup: true`` the newest
  of them runs once.

Stopping the scheduler stops dispatching, cancels runs that have not
started, and waits up to ``shutdown_grace`` seconds for running ones. The
workers are daemon threads: a run still going after that ends with the
process (its history row is marked ``interrupted`` on the next start).

Also provides ``FileWatcher`` for auto-rebuild on SQL changes.
"""

from __future__ import annotations

import datetime
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

console = Console()
logger = logging.getLogger("havn.scheduler")

UTC = datetime.timezone.utc


class SchedulerConfigError(ValueError):
    """The scheduler settings cannot be used; the message lists every problem."""


def _run_stream_task(project_dir_str: str, stream_name: str, trigger: str = "scheduled",
                     config=None) -> dict:
    """Run one stream through the shared stream runner; return its result dict.

    ``config`` is the project config the scheduler resolved (its host's
    environment and branch warehouse); None loads project.yml as is.
    Never raises for a failing stream: the result's ``status`` is
    ``"success"`` or ``"error"``.
    """
    from havn.config import load_project
    from havn.engine.stream_runner import StreamNotFound, run_stream

    project_dir = Path(project_dir_str)
    if config is None:
        config = load_project(project_dir)
    try:
        result = run_stream(config, project_dir, stream_name, trigger=trigger)
    except StreamNotFound:
        return {"stream": stream_name, "status": "error", "error": f"Stream '{stream_name}' not found"}
    return result.to_dict()


def _run_job_task(project_dir_str: str, job_name: str, trigger: str = "scheduled",
                  config=None) -> dict:
    """Run one orchestration job as a scheduled run; return ``{status, error}``.

    ``status`` is the job's own (``success``, ``failure``, ``timeout``,
    ``cancelled``) or ``error`` when the job could not be planned.
    ``config`` as for :func:`_run_stream_task`.
    """
    from havn.config import load_project
    from havn.engine.database import ensure_meta_table, open_warehouse
    from havn.engine.orchestration import (
        _find_job,
        ensure_job_runs_table,
        execute_job,
        resolve_execution_plan,
    )
    from havn.engine.transform.discovery import build_dag, discover_all_models

    project_dir = Path(project_dir_str)
    job = _find_job(project_dir, job_name)
    if job is None:
        return {"job": job_name, "status": "error", "error": f"Job '{job_name}' not found"}
    if config is None:
        config = load_project(project_dir)
    conn = open_warehouse(config, project_dir)
    try:
        ensure_meta_table(conn)
        ensure_job_runs_table(conn)
        dag = build_dag(discover_all_models(project_dir, config))
        plan = resolve_execution_plan(
            job.targets or [job.target],
            dag,
            project_dir,
            conn=conn,
            resolve=job.resolve,
            exclude=job.exclude or None,
        )
        result = execute_job(job, plan, conn, project_dir, trigger=trigger)
        return {"job": job_name, "status": result.status, "error": result.error}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _cron_field_matches(pattern: str, current: int, low: int = 0, high: int = 59) -> bool:
    """Return True if `current` matches a single cron field `pattern`.

    Kept for callers that test one field; the scheduler itself matches whole
    expressions through ``engine.cron``. Steps anchor at ``low`` (pass the
    field's range: day-of-month is 1-31, so ``*/2`` there is 1,3,5,...).
    Malformed patterns never match.
    """
    from havn.engine.cron import CronError, parse_field

    try:
        return current in parse_field(pattern, "field", low, high)
    except CronError:
        return False


# ---------------------------------------------------------------------------
# Schedules: what is scheduled, with its effective settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleSpec:
    """One scheduled stream or job with its effective scheduler settings."""

    kind: str                       # "stream" or "job"
    name: str
    expressions: tuple[str, ...]    # cron, and for jobs also "every N unit"
    overlap: str                    # "skip" or "queue"
    catchup: bool
    timezone: str | None            # IANA name; None = server local time
    description: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}"

    @property
    def cron_expressions(self) -> list[str]:
        from havn.engine.orchestration import is_interval_schedule

        return [e for e in self.expressions if not is_interval_schedule(e)]

    @property
    def interval_expressions(self) -> list[str]:
        from havn.engine.orchestration import is_interval_schedule

        return [e for e in self.expressions if is_interval_schedule(e)]

    def zone(self):
        from havn.engine.schedule_time import load_zone

        return load_zone(self.timezone)


def _resolve_settings(where: str, overlap, catchup, timezone, defaults, errors: list[str]):
    """Effective (overlap, catchup, timezone) for one schedule, or None on an error."""
    from havn.config import SCHEDULER_OVERLAP_POLICIES, parse_bool_setting
    from havn.engine.schedule_time import ScheduleTimezoneError, load_zone

    ok = True
    if overlap is None:
        eff_overlap = defaults.overlap
    else:
        eff_overlap = str(overlap).strip().lower()
        if eff_overlap not in SCHEDULER_OVERLAP_POLICIES:
            errors.append(f"{where}: overlap must be skip or queue, not {overlap!r}")
            ok = False
    if catchup is None:
        eff_catchup = defaults.catchup
    else:
        eff_catchup = parse_bool_setting(catchup)
        if eff_catchup is None:
            errors.append(f"{where}: catchup must be true or false, not {catchup!r}")
            ok = False
    if timezone is None:
        eff_tz = defaults.timezone
        own_tz = False
    else:
        eff_tz = str(timezone).strip() or None
        own_tz = True
    try:
        load_zone(eff_tz)
    except ScheduleTimezoneError as e:
        if own_tz:
            errors.append(f"{where}: timezone: {e}")
        ok = False  # the project-level default is reported once by the caller
    if eff_tz is not None and eff_tz.lower() == "local":
        eff_tz = None
    return (eff_overlap, eff_catchup, eff_tz) if ok else None


def collect_schedules(project_dir: Path, config=None, jobs=None) -> tuple[list[ScheduleSpec], list[str], list[str]]:
    """Every scheduled stream and enabled job, with errors and warnings.

    Returns ``(specs, errors, warnings)``. A stream or job with an error
    (unknown time zone, bad overlap value) is left out of ``specs``; an
    error in ``scheduler:`` itself is reported once. ``havn validate``
    prints both lists; the scheduler refuses to start on errors.
    """
    from havn.config import load_project
    from havn.engine.cron import is_valid_cron
    from havn.engine.schedule_time import ScheduleTimezoneError, load_zone

    if config is None:
        config = load_project(project_dir)
    if jobs is None:
        from havn.engine.orchestration import discover_jobs

        jobs = discover_jobs(project_dir)
    defaults = config.scheduler
    errors: list[str] = list(defaults.problems)
    warnings: list[str] = []
    try:
        load_zone(defaults.timezone)
    except ScheduleTimezoneError as e:
        errors.append(f"scheduler.timezone: {e}")

    specs: list[ScheduleSpec] = []
    for name, stream in config.streams.items():
        if not stream.schedule:
            continue
        expr = str(stream.schedule).strip()
        if not is_valid_cron(expr):
            warnings.append(f"streams.{name}.schedule: {expr!r} is not a valid cron expression; it never runs")
            continue
        settings = _resolve_settings(
            f"streams.{name}", stream.overlap, stream.catchup, stream.timezone, defaults, errors,
        )
        if settings is None:
            continue
        specs.append(ScheduleSpec("stream", name, (expr,), *settings, description=stream.description))

    for job in jobs:
        if not job.enabled:
            continue
        schedules = tuple(job.schedules or ([job.cron] if job.cron else []))
        if not schedules:
            continue
        try:
            where = f"orchestration/{job.file_path.name}"
        except Exception:
            where = f"job {job.name}"
        settings = _resolve_settings(where, job.overlap, job.catchup, job.timezone, defaults, errors)
        if settings is None:
            continue
        specs.append(ScheduleSpec("job", job.name, schedules, *settings, description=job.description))
    return specs, errors, warnings


def check_schedules(project_dir: Path, config=None) -> tuple[list[str], list[str]]:
    """``(errors, warnings)`` for the scheduler settings of this project."""
    _, errors, warnings = collect_schedules(project_dir, config)
    return errors, warnings


def next_run(spec: ScheduleSpec, now: datetime.datetime | None = None) -> datetime.datetime | None:
    """The next cron fire of ``spec`` as an aware UTC instant (None for interval-only)."""
    from havn.engine.schedule_time import next_fire

    now = now or datetime.datetime.now(UTC)
    tz = spec.zone()
    fires = [f for f in (next_fire(e, now, tz) for e in spec.cron_expressions) if f is not None]
    return min(fires) if fires else None


def format_in_zone(instant: datetime.datetime | None, timezone: str | None, *, label: bool = True) -> str:
    """``2026-11-28 06:00 Europe/Oslo`` (or ``... local``) for an instant."""
    if instant is None:
        return "-"
    from havn.engine.schedule_time import ScheduleTimezoneError, load_zone, zone_label

    try:
        tz = load_zone(timezone)
    except ScheduleTimezoneError:
        tz = None
    text = instant.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    return f"{text} {zone_label(tz)}" if label else text


def get_scheduled_streams(project_dir: Path) -> list[dict]:
    """Return info about all streams that have cron schedules.

    ``timezone`` is the zone the schedule is evaluated in (``None`` =
    server local time), ``overlap`` its overlap policy and ``next_run`` the
    next fire as an ISO timestamp with offset (``None`` when the schedule's
    settings are invalid).
    """
    from havn.config import load_project

    config = load_project(project_dir)
    specs, _, _ = collect_schedules(project_dir, config, jobs=[])
    by_name = {s.name: s for s in specs}
    scheduled = []
    for name, stream in config.streams.items():
        if stream.schedule:
            spec = by_name.get(name)
            nxt = next_run(spec) if spec else None
            scheduled.append({
                "name": name,
                "description": stream.description,
                "schedule": stream.schedule,
                "steps": [{"action": s.action, "targets": s.targets} for s in stream.steps],
                "timezone": spec.timezone if spec else stream.timezone,
                "overlap": spec.overlap if spec else None,
                "next_run": nxt.astimezone(spec.zone()).isoformat() if nxt else None,
            })
    return scheduled


# ---------------------------------------------------------------------------
# Worker pool
# ---------------------------------------------------------------------------


@dataclass
class _RunTask:
    row_id: str
    spec: ScheduleSpec
    expr: str
    scheduled_for: datetime.datetime    # aware UTC
    trigger: str = "schedule"           # "schedule" or "catchup"
    leased: bool = False                # holds the scheduler's WarehouseLease


@dataclass
class _InternalTask:
    """Scheduler housekeeping that runs in the pool (the report check)."""

    name: str
    fn: object = field(default=None)
    leased: bool = False


_RETIRE = object()  # tells one worker to exit (the pool shrank)


class _WorkerPool:
    """Daemon worker threads reading one FIFO queue; ``resize`` changes how many.

    Daemon threads never keep the process alive; ``ThreadPoolExecutor``
    workers would (they are joined at interpreter exit), so a stuck run
    could hang ``Ctrl+C`` forever.
    """

    def __init__(self, size: int, handler):
        self.size = max(1, int(size))
        self._handler = handler
        self._queue: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._closed = False
        self._started = False
        self._mutex = threading.Lock()
        self._next_index = 1

    def _spawn(self, n: int) -> None:
        for _ in range(n):
            t = threading.Thread(target=self._work, name=f"havn-scheduler-worker-{self._next_index}",
                                 daemon=True)
            self._next_index += 1
            t.start()
            self._threads.append(t)

    def submit(self, task) -> None:
        with self._mutex:
            if self._closed:
                raise RuntimeError("worker pool is closed")
            if not self._started:
                self._started = True
                self._spawn(self.size)
        self._queue.put(task)

    def resize(self, size: int) -> None:
        """Grow at once; shrink as workers finish what is queued ahead of the change."""
        size = max(1, int(size))
        with self._mutex:
            if self._closed or size == self.size:
                return
            if self._started:
                if size > self.size:
                    self._spawn(size - self.size)
                else:
                    for _ in range(self.size - size):
                        self._queue.put(_RETIRE)
            self.size = size

    def _work(self) -> None:
        while True:
            task = self._queue.get()
            if task is None or task is _RETIRE:
                return
            try:
                self._handler(task)
            except Exception:
                logger.exception("Scheduler worker failed")

    def drain(self) -> list:
        """Remove and return every task not yet picked up by a worker."""
        out = []
        while True:
            try:
                task = self._queue.get_nowait()
            except queue.Empty:
                return out
            if task is not None and task is not _RETIRE:
                out.append(task)

    def close(self) -> None:
        """Stop the workers once they finish what they are running."""
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            for _ in self._threads:
                self._queue.put(None)

    def join(self, timeout: float | None) -> list[threading.Thread]:
        """Wait up to ``timeout`` seconds in total; return workers still alive."""
        deadline = None if timeout is None else time.monotonic() + timeout
        for t in self._threads:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            t.join(remaining)
        return [t for t in self._threads if t.is_alive()]

    @property
    def threads(self) -> list[threading.Thread]:
        """The workers still running (a retired one has exited)."""
        return [t for t in self._threads if t.is_alive()]


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------


class SchedulerThread(threading.Thread):
    """Background thread that dispatches scheduled streams, jobs and reports.

    ``config_loader()`` returns the project config the scheduler works with,
    re-read on every tick: its host's resolution, so the scheduler builds the
    warehouse its host does. ``havn serve`` passes the server's own resolver
    (``--env``, the environment switched to in the UI, the branch warehouse
    of the checked-out branch); ``havn schedule --env`` resolves like the
    other run commands. Default: ``load_project(project_dir)``.

    Injectable for tests: ``clock`` returns the current time (an aware
    datetime), ``runner(task)`` runs one stream/job and returns ``(status,
    error)`` with status ``"success"`` or ``"failed"``, ``max_concurrent``
    overrides ``scheduler.max_concurrent``.
    """

    POLL_SECONDS = 30
    SHUTDOWN_GRACE_SECONDS = 30.0
    # Missed fires recorded per schedule and window; older ones are counted
    # in the log, not stored one row each.
    MISSED_ROWS_LIMIT = 100
    PURGE_EVERY = datetime.timedelta(hours=1)

    def __init__(
        self,
        project_dir: Path,
        *,
        clock=None,
        runner=None,
        max_concurrent: int | None = None,
        shutdown_grace: float | None = None,
        poll_seconds: float | None = None,
        config_loader=None,
    ):
        super().__init__(daemon=True, name="havn-scheduler")
        from havn.engine.schedule_history import HistoryStore, WarehouseLease, new_id

        self.project_dir = Path(project_dir)
        self._config_loader = config_loader
        self._clock = clock or (lambda: datetime.datetime.now(UTC))
        self._runner = runner or self._default_runner
        self._max_concurrent_override = max_concurrent
        self.shutdown_grace = self.SHUTDOWN_GRACE_SECONDS if shutdown_grace is None else shutdown_grace
        self.poll_seconds = self.POLL_SECONDS if poll_seconds is None else poll_seconds
        self.scheduler_id = new_id()

        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._stopping = False
        self._pool: _WorkerPool | None = None
        # Schedule key -> task: submitted and not started / waiting for the
        # running one (overlap: queue) / running.
        self._queued: dict[str, _RunTask] = {}
        self._deferred: dict[str, _RunTask] = {}
        self._inflight: dict[str, _RunTask] = {}
        self._internal_pending: set[str] = set()

        # Interval-job keys -> epoch seconds of their last fire.
        self._last_run: dict[str, float] = {}
        # Cron keys -> the instant up to which that schedule has been
        # evaluated. A fire in (clock, now] is due.
        self._cron_clock: dict[str, datetime.datetime] = {}
        # When the scheduler's clock starts; set by run() (or the first
        # evaluation when the thread is driven directly, as tests do).
        self._started_at: datetime.datetime | None = None
        # False until the first poll finishes; see _clock_origin.
        self._cron_clock_settled = False
        self._config = None
        self._logged_problems: set[str] = set()
        self._last_purge: datetime.datetime | None = None
        # Held from the moment a run is queued until it finishes; see
        # WarehouseLease for the DuckDB instance-teardown race it prevents.
        self._lease = WarehouseLease(self._connect)
        self._history = HistoryStore(self._lease, self.scheduler_id)
        # The warehouse's scheduler lock, held from start()/_startup() until
        # the scheduler stops.
        self._sched_lock = None

    # -- configuration -------------------------------------------------

    def _load_config(self):
        if self._config_loader is not None:
            self._config = self._config_loader()
        else:
            from havn.config import load_project

            self._config = load_project(self.project_dir)
        return self._config

    def _connect(self):
        from havn.engine.database import open_warehouse

        config = self._config or self._load_config()
        return open_warehouse(config, self.project_dir)

    def check_config(self) -> None:
        """Raise :class:`SchedulerConfigError` when the settings cannot be used."""
        config = self._load_config()
        _, errors, _ = collect_schedules(self.project_dir, config)
        if errors:
            raise SchedulerConfigError(
                "The scheduler cannot start:\n" + "\n".join(f"  - {e}" for e in errors)
            )

    @property
    def max_concurrent(self) -> int:
        if self._max_concurrent_override is not None:
            return max(1, int(self._max_concurrent_override))
        config = self._config or self._load_config()
        return max(1, config.scheduler.max_concurrent)

    def start(self) -> None:
        """Validate the settings, take the warehouse's scheduler lock, start the loop.

        Raises :class:`SchedulerConfigError` when the settings cannot be used
        or another scheduler already runs against the same warehouse.
        """
        from havn.engine.scheduler_lock import SchedulerLockedError

        self.check_config()
        try:
            self.acquire_lock()
        except SchedulerLockedError as e:
            raise SchedulerConfigError(str(e)) from e
        try:
            super().start()
        except BaseException:
            self.release_lock()
            raise

    # -- one scheduler per warehouse ---------------------------------------

    def acquire_lock(self) -> None:
        """Hold the warehouse's scheduler lock (``engine/scheduler_lock.py``).

        Raises ``SchedulerLockedError`` naming the process that holds it.
        Idempotent; released by :meth:`stop` (via ``_shutdown``).
        """
        from havn.engine.scheduler_lock import SchedulerLock, warehouse_key

        if self._sched_lock is not None and self._sched_lock.held:
            return
        config = self._config or self._load_config()
        lock = SchedulerLock(self.project_dir, warehouse_key(config, self.project_dir),
                             warehouse=str(getattr(config.database, "path", "") or ""))
        lock.acquire()
        self._sched_lock = lock

    def release_lock(self) -> None:
        if self._sched_lock is not None:
            self._sched_lock.release()
            self._sched_lock = None

    # -- time ------------------------------------------------------------

    def _now(self) -> datetime.datetime:
        from havn.engine.schedule_time import as_instant

        return as_instant(self._clock())

    def _clock_origin(self, now: datetime.datetime) -> datetime.datetime:
        """Where a schedule seen for the first time starts counting from.

        Schedules present when the scheduler starts count from the start of
        the minute it started in; one added to project.yml later counts from
        the start of the minute it first appears in. Fires before that are
        never run by the loop (restart catch-up is separate; see _startup).
        """
        from havn.engine.schedule_time import as_instant

        base = now if self._started_at is None or self._cron_clock_settled else as_instant(self._started_at)
        return as_instant(base).replace(second=0, microsecond=0) - datetime.timedelta(microseconds=1)

    # -- lifecycle -----------------------------------------------------

    def stop(self) -> None:
        """Stop dispatching. A running loop thread then shuts the pool down.

        Called on a scheduler whose thread never started (tests drive
        ``_tick`` directly), it shuts the pool down here.
        """
        self._stop_event.set()
        if not self.is_alive():
            self._shutdown()

    def run(self) -> None:
        self._started_at = self._now()
        logger.info("Scheduler started for %s", self.project_dir)
        try:
            self._startup(self._started_at)
        except Exception as e:
            logger.error("Scheduler startup bookkeeping failed: %s", e)
        while not self._stop_event.is_set():
            self._tick()
            self._stop_event.wait(self.poll_seconds)
        self._shutdown()
        logger.info("Scheduler stopped")

    def _ensure_pool(self) -> _WorkerPool:
        if self._pool is None:
            self._pool = _WorkerPool(self.max_concurrent, self._execute)
        return self._pool

    def _shutdown(self) -> None:
        """Stop the runs (see ``_shutdown_runs``), then give up the scheduler lock."""
        try:
            self._shutdown_runs()
        finally:
            self.release_lock()

    def _shutdown_runs(self) -> None:
        """Cancel runs that have not started; wait ``shutdown_grace`` for running ones."""
        from havn.engine.schedule_history import STATUS_CANCELLED

        with self._lock:
            if self._stopping and self._pool is None:
                return
            self._stopping = True
            leftovers = list(self._deferred.values())
            self._deferred.clear()
        pool = self._pool
        if pool is not None:
            for t in pool.drain():
                if isinstance(t, _RunTask):
                    leftovers.append(t)
                else:
                    self._release(t)
        with self._changed:
            for t in leftovers:
                self._queued.pop(t.spec.key, None)
            self._internal_pending.clear()
            self._changed.notify_all()
        self._history.mark_status(
            [t.row_id for t in leftovers], STATUS_CANCELLED,
            "The scheduler stopped before this run started",
        )
        for t in leftovers:
            self._release(t)
        if pool is None:
            return
        pool.close()
        alive = pool.join(self.shutdown_grace)
        if alive:
            with self._lock:
                names = ", ".join(sorted(self._inflight)) or "housekeeping"
            logger.warning(
                "Scheduler stopped with %d run(s) still in progress (%s); "
                "they end when the process exits", len(alive), names,
            )

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until nothing is queued or running. For tests and the CLI."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            while self._queued or self._deferred or self._inflight or self._internal_pending:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._changed.wait(remaining)
        return True

    @property
    def worker_threads(self) -> list[threading.Thread]:
        return self._pool.threads if self._pool is not None else []

    # -- startup: interval clocks, missed fires ---------------------------

    def _restore_interval_clocks(self, specs: list[ScheduleSpec]) -> None:
        """Restore last-fire timestamps for interval job schedules.

        So that "every 2 weeks" doesn't re-fire immediately after a restart.
        """
        interval_specs = [s for s in specs if s.kind == "job" and s.interval_expressions]
        if not interval_specs:
            return
        try:
            from havn.engine.orchestration import ensure_job_runs_table

            if not self._lease.try_acquire():
                return
            conn = self._lease.cursor()
            try:
                ensure_job_runs_table(conn)
                for spec in interval_specs:
                    row = conn.execute(
                        "SELECT MAX(started_at) FROM _havn.job_runs WHERE job_name = ?",
                        [spec.name],
                    ).fetchone()
                    if row and row[0] is not None:
                        for expr in spec.interval_expressions:
                            self._last_run[f"{spec.key}|interval|{expr}"] = row[0].timestamp()
            finally:
                conn.close()
                self._lease.release()
        except Exception as exc:
            logger.debug("Could not restore interval timestamps: %s", exc)

    def _startup(self, now: datetime.datetime | None = None) -> None:
        """Resume from the persisted history before the first tick.

        Rows an earlier process left queued/running become ``interrupted``;
        cron fires between the last heartbeat (or the schedule's last
        recorded fire, whichever is later) and the start of this minute are
        recorded as ``missed``, and for a schedule with ``catchup: true`` the
        newest one is run once. With no heartbeat and no history (the first
        start) nothing counts as missed.
        """
        from havn.engine.schedule_history import STATUS_MISSED
        from havn.engine.schedule_time import FIRES_COUNT_CAP, fires_between

        now = now or self._now()
        if self._started_at is None:
            self._started_at = now
        config = self._load_config()
        # Before touching history: marking "interrupted" rows is only right
        # when no other scheduler is alive on this warehouse.
        self.acquire_lock()
        specs, errors, _ = collect_schedules(self.project_dir, config)
        self._log_problems(errors)
        self._restore_interval_clocks(specs)
        info = self._history.startup(config.scheduler.history_days, now)
        self._last_purge = now
        if not info:
            return
        origin = self._clock_origin(now)
        last_seen = info["last_seen"]
        for spec in specs:
            self._seed_cron_clock(spec, info["last_fires"], origin)
            tz = spec.zone()
            missed: dict[datetime.datetime, str] = {}
            for expr in spec.cron_expressions:
                anchors = [a for a in (last_seen, info["last_fires"].get((spec.kind, spec.name, expr))) if a]
                if not anchors:
                    continue
                fires, total = fires_between(expr, max(anchors), origin, tz, limit=self.MISSED_ROWS_LIMIT)
                if total > len(fires):
                    logger.warning(
                        "%s %s: %s%d fires passed while the scheduler was down; recording the newest %d",
                        spec.kind, spec.name,
                        "at least " if total - len(fires) >= FIRES_COUNT_CAP else "",
                        total, len(fires),
                    )
                for f in fires:
                    missed.setdefault(f, expr)
            if not missed:
                continue
            ordered = sorted(missed.items())
            if len(ordered) > self.MISSED_ROWS_LIMIT:
                ordered = ordered[-self.MISSED_ROWS_LIMIT:]
            catch_up = ordered[-1] if spec.catchup else None
            record = ordered[:-1] if catch_up else ordered
            self._history.insert([
                self._row(spec, expr, inst, STATUS_MISSED, now,
                          error="The scheduler was not running at this time")
                for inst, expr in record
            ])
            logger.warning(
                "%s %s: %d fire(s) missed while the scheduler was down%s",
                spec.kind, spec.name, len(ordered),
                "; running the newest now (catchup: true)" if catch_up else "",
            )
            if catch_up:
                inst, expr = catch_up
                self._dispatch(spec, expr, inst, now, trigger="catchup")

    def _seed_cron_clock(self, spec: ScheduleSpec, last_fires: dict, origin: datetime.datetime) -> None:
        """Start ``spec``'s cron clocks after its newest recorded fire.

        The loop counts from the start of the minute the scheduler started
        in, so a restart in the same minute as a fire the previous process
        already recorded (and ran) would dispatch that fire again. Seeding
        each clock at the newest fire history holds for the schedule (over
        all of its expressions: two expressions firing in the same minute
        are one fire) means a recorded fire is never dispatched twice.
        """
        recorded = [
            inst for (kind, name, _expr), inst in last_fires.items()
            if kind == spec.kind and name == spec.name and inst is not None
        ]
        if not recorded:
            return
        start = max(origin, max(recorded))
        for expr in spec.cron_expressions:
            key = f"{spec.key}|{expr}"
            self._cron_clock[key] = max(self._cron_clock.get(key, start), start)

    # -- the loop --------------------------------------------------------

    def _log_problems(self, problems: list[str]) -> None:
        for p in problems:
            if p not in self._logged_problems:
                self._logged_problems.add(p)
                logger.error("Scheduler: %s (this schedule is not run)", p)

    def _tick(self, now: datetime.datetime | None = None) -> None:
        """One poll: dispatch every stream, job and report check that is due.

        ``now`` is injectable for tests (naive = local time). Runs happen in
        the worker pool, so this returns as soon as everything is handed over.
        """
        from havn.engine.schedule_time import as_instant

        now = as_instant(now) if now is not None else self._now()
        if self._started_at is None:
            self._started_at = now
        try:
            config = self._load_config()
            if self._pool is not None:
                # project.yml is re-read every tick; so is max_concurrent.
                self._pool.resize(self.max_concurrent)
            specs, errors, _ = collect_schedules(self.project_dir, config)
            self._log_problems(errors)
            for spec in specs:
                try:
                    self._evaluate(spec, now)
                except Exception as e:
                    logger.error("Scheduler: could not evaluate %s %s: %s", spec.kind, spec.name, e)
            self._submit_internal("reports", self._check_reports)
            self._history.heartbeat(now)
            if self._last_purge is None or now - self._last_purge >= self.PURGE_EVERY:
                self._history.purge(config.scheduler.history_days, now)
                self._last_purge = now
        except Exception as e:
            logger.error("Scheduler error: %s", e)
        finally:
            # Every schedule present at the first poll has its clock now;
            # any that appears later starts counting from when it appears.
            self._cron_clock_settled = True

    def _evaluate(self, spec: ScheduleSpec, now: datetime.datetime) -> None:
        """Dispatch ``spec`` if one of its expressions fired since the last poll.

        Several fires since the last poll (the process was suspended, or the
        machine slept) dispatch the newest; the older ones are recorded as
        ``missed``. Fires of different expressions at the same minute count
        once. Interval schedules are checked only when no cron one fired.
        """
        from havn.engine.schedule_history import STATUS_MISSED
        from havn.engine.schedule_time import fires_between

        tz = spec.zone()
        due: dict[datetime.datetime, str] = {}
        for expr in spec.cron_expressions:
            key = f"{spec.key}|{expr}"
            last = self._cron_clock.get(key)
            if last is None:
                last = self._clock_origin(now)
            self._cron_clock[key] = max(last, now)
            if now <= last:
                continue
            fires, _ = fires_between(expr, last, now, tz, limit=self.MISSED_ROWS_LIMIT)
            for f in fires:
                due.setdefault(f, expr)
        if not due:
            for expr in spec.interval_expressions:
                hit = self._interval_due(spec, expr, now)
                if hit is not None:
                    due[hit] = expr
                    break
        if not due:
            return
        ordered = sorted(due.items())
        *older, (latest, expr) = ordered
        if older:
            self._history.insert([
                self._row(spec, e, inst, STATUS_MISSED, now,
                          error="The scheduler did not get to this fire in time (process suspended?)")
                for inst, e in older
            ])
        self._dispatch(spec, expr, latest, now)

    def _interval_due(self, spec: ScheduleSpec, expr: str, now: datetime.datetime):
        from havn.engine.orchestration import parse_interval

        delta = parse_interval(expr)
        if delta is None:
            return None
        key = f"{spec.key}|interval|{expr}"
        now_ts = now.timestamp()
        last = self._last_run.get(key)
        if last is not None and (now_ts - last) < delta.total_seconds():
            return None
        self._last_run[key] = now_ts
        if last is None:
            return now
        return datetime.datetime.fromtimestamp(last, UTC) + delta

    # -- dispatch and execution ------------------------------------------

    def _row(self, spec: ScheduleSpec, expr: str, scheduled_for, status: str, now, *, trigger="schedule",
             error=None, row_id=None) -> dict:
        from havn.engine.schedule_history import new_id, utc_naive

        return {
            "id": row_id or new_id(),
            "schedule_kind": spec.kind,
            "schedule_name": spec.name,
            "cron_expr": expr,
            "timezone": spec.timezone,
            "scheduled_for": utc_naive(scheduled_for),
            "recorded_at": utc_naive(now),
            "status": status,
            "trigger": trigger,
            "error": error,
        }

    def _dispatch(self, spec: ScheduleSpec, expr: str, scheduled_for, now, *, trigger: str = "schedule") -> str:
        """Apply the overlap policy and hand the run to the pool.

        Returns ``"queued"``, ``"deferred"`` (overlap: queue, waiting for the
        running one), ``"skipped"`` or ``"stopped"``. The history row is
        written before the task becomes visible to a worker, so the worker's
        start/finish updates always find it.
        """
        from havn.engine.schedule_history import STATUS_QUEUED, STATUS_SKIPPED, new_id

        task = _RunTask(new_id(), spec, expr, scheduled_for, trigger)
        key = spec.key
        with self._changed:
            if self._stopping or self._stop_event.is_set():
                return "stopped"
            waiting = key in self._queued or key in self._deferred
            running = key in self._inflight
            if waiting or (running and spec.overlap == "skip"):
                reason = (
                    "A run of this schedule was already waiting" if waiting
                    else "The previous run was still running"
                )
                self._history.insert([self._row(spec, expr, scheduled_for, STATUS_SKIPPED, now,
                                                trigger=trigger, error=reason)])
                logger.info("Scheduler: %s %s skipped (%s)", spec.kind, spec.name, reason.lower())
                return "skipped"
            task.leased = self._lease.try_acquire()
            self._history.insert([self._row(spec, expr, scheduled_for, STATUS_QUEUED, now,
                                            trigger=trigger, row_id=task.row_id)])
            if running:
                self._deferred[key] = task
                self._changed.notify_all()
                return "deferred"
            self._queued[key] = task
            self._ensure_pool().submit(task)
            self._changed.notify_all()
            return "queued"

    def _submit_internal(self, name: str, fn) -> None:
        with self._changed:
            if self._stopping or name in self._internal_pending:
                return
            self._internal_pending.add(name)
            task = _InternalTask(name, fn, leased=self._lease.try_acquire())
            self._ensure_pool().submit(task)

    def _execute(self, task) -> None:
        """Worker body: run one task and record how it went."""
        from havn.engine.schedule_history import STATUS_CANCELLED, STATUS_FAILED, STATUS_SUCCESS

        if isinstance(task, _InternalTask):
            try:
                task.fn()
            except Exception as e:
                logger.warning("Scheduler %s check failed: %s", task.name, e)
            finally:
                self._release(task)
                with self._changed:
                    self._internal_pending.discard(task.name)
                    self._changed.notify_all()
            return

        key = task.spec.key
        with self._changed:
            if self._queued.get(key) is task:
                del self._queued[key]
            cancelled = self._stopping
            if not cancelled:
                self._inflight[key] = task
            self._changed.notify_all()
        if cancelled:
            self._history.mark_status([task.row_id], STATUS_CANCELLED,
                                      "The scheduler stopped before this run started")
            self._release(task)
            return

        # The project's run gate (engine/run_gate.py), shared with the other
        # scheduled runs: a run that fires while a web UI run is in progress
        # (`havn serve --schedule`) waits here until it ends, and the wait
        # counts towards its lateness.
        ticket = self._acquire_gate(task)
        if ticket is None:
            self._history.mark_status([task.row_id], STATUS_CANCELLED,
                                      "The scheduler stopped before this run started")
            self._release(task)
        else:
            try:
                started = self._now()
                lateness_ms = max(0, int((started - task.scheduled_for).total_seconds() * 1000))
                self._history.mark_started(task.row_id, started, lateness_ms)
                try:
                    status, error = self._runner(task)
                except Exception as e:
                    logger.exception("Scheduled %s %s raised", task.spec.kind, task.spec.name)
                    status, error = STATUS_FAILED, str(e)
                if status != STATUS_SUCCESS:
                    status = STATUS_FAILED
                self._history.mark_finished(task.row_id, status, self._now(), error)
            finally:
                ticket.release()
            # Released before the run stops counting as in flight, so anyone
            # waiting for idle (tests, shutdown) never races the close.
            self._release(task)

        with self._changed:
            self._inflight.pop(key, None)
            nxt = self._deferred.pop(key, None)
            if nxt is not None and not self._stopping and self._pool is not None:
                self._queued[key] = nxt
                try:
                    self._pool.submit(nxt)
                except RuntimeError:
                    self._queued.pop(key, None)
                    nxt_cancelled = nxt
                else:
                    nxt_cancelled = None
            else:
                nxt_cancelled = nxt
            self._changed.notify_all()
        if nxt_cancelled is not None:
            self._history.mark_status([nxt_cancelled.row_id], STATUS_CANCELLED,
                                      "The scheduler stopped before this run started")
            self._release(nxt_cancelled)

    def _acquire_gate(self, task: _RunTask):
        """Take the run gate for ``task`` (shared), or None when stopping meanwhile.

        Shared: scheduled runs may overlap each other up to
        ``scheduler.max_concurrent``; a web UI run holds the gate alone, and
        a scheduled run waits for it rather than being skipped.
        """
        from havn.engine.run_gate import gate_for

        gate = gate_for(self.project_dir)
        label = f"{task.spec.kind} '{task.spec.name}'"
        busy = gate.busy()
        if busy is not None and busy.kind != "shared":
            logger.info("Scheduler: %s waits for %s", label, ", ".join(busy.labels) or "a run")
        return gate.acquire_shared(label, should_stop=lambda: self._stopping)

    def _release(self, task) -> None:
        if getattr(task, "leased", False):
            task.leased = False
            self._lease.release()

    def _default_runner(self, task: _RunTask) -> tuple[str, str | None]:
        """Run a stream or job the way ``havn stream`` / ``havn jobs run`` would."""
        spec = task.spec
        label = f"{spec.kind} '{spec.name}'"
        title = label[0].upper() + label[1:]
        logger.info("Scheduler triggering %s", label)
        console.print(f"[bold blue]Scheduler:[/bold blue] Running {label}")
        config = self._config or self._load_config()
        if spec.kind == "stream":
            result = _run_stream_task(str(self.project_dir), spec.name, config=config)
        else:
            result = _run_job_task(str(self.project_dir), spec.name, config=config)
        ok = result.get("status") == "success"
        error = result.get("error")
        if ok:
            console.print(f"[bold green]Scheduler:[/bold green] {title} completed")
            return "success", None
        if spec.kind == "job" and result.get("status") not in ("error", None):
            error = f"{result.get('status')}" + (f": {error}" if error else "")
        console.print(
            f"[bold red]Scheduler:[/bold red] {title} failed: {error or 'see the log above'}"
        )
        return "failed", error

    def _check_reports(self) -> None:
        """Scheduled dashboard reports (_havn.reports) that are due."""
        from havn.engine.reports import run_due_reports_for_project

        config = self._config or self._load_config()
        for delivery in run_due_reports_for_project(self.project_dir, config=config):
            colour = "green" if delivery["status"] in ("sent", "skipped") else "red"
            console.print(
                f"[bold {colour}]Scheduler:[/bold {colour}] Report "
                f"{delivery['report_id']} {delivery['status']}"
            )


class FileWatcher(threading.Thread):
    """Watches transform/, ingest/, and macros/ for changes, triggers rebuilds.

    The ``on_macro_change`` callback is invoked (with *project_dir*) whenever
    a ``.py`` or ``.sql`` file inside ``macros/`` is saved.  The server layer
    wires this to ``reregister_macros_on_shared_conns`` so live connections
    pick up edits without a restart.

    A transform change does not rebuild at once: it is recorded as pending
    and the watcher's own loop runs the rebuild once no change has come in
    for ``debounce`` seconds, so a burst of saves builds once. The rebuild
    takes the project's run gate (``engine/run_gate.py``) exclusively and
    without waiting, like a web UI run: while a UI or scheduled run holds
    it, the rebuild stays pending (logged once) and runs when the gate is
    free. Changes that arrive meanwhile join the pending rebuild.

    ``connect()`` returns the connection to build on and is closed after the
    rebuild. ``havn serve --watch`` passes a cursor of the server's shared
    connection, so the watcher never opens a second connection in the
    server process (one with other settings is refused by DuckDB, and a
    second instance would fight the server over the file). Default: open
    the warehouse of ``config_loader()`` (``load_project``).
    """

    DEBOUNCE_SECONDS = 2.0
    POLL_SECONDS = 0.25

    def __init__(self, project_dir: Path, on_change=None, on_macro_change=None,
                 route_globs: list[str] | None = None, *, connect=None, config_loader=None,
                 debounce: float | None = None, clock=None):
        super().__init__(daemon=True, name="havn-watcher")
        self.project_dir = Path(project_dir)
        self.on_change = on_change
        self.on_macro_change = on_macro_change
        # When set, only transform-side changes whose relative path matches
        # one of these globs trigger a rebuild — and only the matching
        # model is rebuilt, not the whole DAG.
        self.route_globs = route_globs or []
        self._connect = connect
        self._config_loader = config_loader
        self.debounce = self.DEBOUNCE_SECONDS if debounce is None else debounce
        self._clock = clock or time.monotonic
        self._stop_event = threading.Event()
        self._pending_lock = threading.Lock()
        # None: nothing pending. A set: the targets to rebuild; ALL in it
        # means the whole DAG.
        self._pending: set[str] | None = None
        self._last_change = 0.0
        self._waiting_logged = False

    _ALL = "\0all"

    def stop(self) -> None:
        self._stop_event.set()

    # -- rebuilds ----------------------------------------------------------

    def request_rebuild(self, targets: list[str] | None) -> None:
        """Record a transform change; ``None`` rebuilds the whole DAG."""
        with self._pending_lock:
            if self._pending is None:
                self._pending = set()
            self._pending.update(targets if targets else [self._ALL])
            self._last_change = self._clock()

    @property
    def pending(self) -> list[str] | None:
        """The pending rebuild's targets (``[]`` = everything), or None."""
        with self._pending_lock:
            if self._pending is None:
                return None
            return [] if self._ALL in self._pending else sorted(self._pending)

    def try_rebuild(self, now: float | None = None) -> str:
        """Run the pending rebuild if it is due and the gate is free.

        Returns ``"idle"`` (nothing pending), ``"waiting"`` (a change came in
        less than ``debounce`` seconds ago), ``"busy"`` (another run holds
        the run gate; still pending) or ``"ran"``.
        """
        from havn.engine.run_gate import GateBusy, gate_for

        now = self._clock() if now is None else now
        with self._pending_lock:
            if self._pending is None:
                return "idle"
            if now - self._last_change < self.debounce:
                return "waiting"
            taken = gate_for(self.project_dir).try_exclusive("watcher rebuild")
            if isinstance(taken, GateBusy):
                if not self._waiting_logged:
                    self._waiting_logged = True
                    logger.info("Watcher: rebuild pending until the current run ends (%s)", taken.message)
                    console.print(
                        "[bold yellow]Watcher:[/bold yellow] Rebuild pending; "
                        f"waiting for the current run ({', '.join(taken.labels) or 'a run'})"
                    )
                return "busy"
            pending, self._pending = self._pending, None
            self._waiting_logged = False
        try:
            targets = None if self._ALL in pending else sorted(pending)
            self._rebuild(targets)
        finally:
            taken.release()
        return "ran"

    def _open(self):
        if self._connect is not None:
            return self._connect()
        from havn.engine.database import open_warehouse

        if self._config_loader is not None:
            config = self._config_loader()
        else:
            from havn.config import load_project

            config = load_project(self.project_dir)
        return open_warehouse(config, self.project_dir)

    def _rebuild(self, targets: list[str] | None) -> None:
        from havn.engine.transform import run_transform

        label = f"Running transform (target: {', '.join(targets)})..." if targets else "Running transform..."
        console.print(f"[bold yellow]Watcher:[/bold yellow] {label}")
        try:
            conn = self._open()
            try:
                run_transform(conn, self.project_dir / "transform", targets=targets,
                              project_dir=self.project_dir)
            finally:
                conn.close()
            console.print("[bold green]Watcher:[/bold green] Transform completed")
        except Exception as e:
            logger.error("Watcher: transform failed: %s", e)
            console.print(f"[bold red]Watcher:[/bold red] Transform failed: {e}")

    # -- the thread --------------------------------------------------------

    def run(self) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        watcher = self
        project_dir = self.project_dir
        on_macro_change = self.on_macro_change
        route_globs = list(self.route_globs)
        macro_logger = logging.getLogger("havn.macros")

        class Handler(FileSystemEventHandler):
            def __init__(self):
                self._debounce: dict[str, float] = {}

            def _debounced(self, src_path: str) -> bool:
                """Return True if this event should be processed (not a duplicate)."""
                now = time.time()
                last = self._debounce.get(src_path, 0)
                if now - last < 2:
                    return False
                self._debounce[src_path] = now
                return True

            def _handle(self, event) -> None:
                if event.is_directory:
                    return
                path = Path(event.src_path)
                if path.suffix not in (".sql", ".py"):
                    return

                if not self._debounced(event.src_path):
                    return

                rel = path.relative_to(project_dir)
                logger.info("File changed: %s", rel)
                console.print(f"[bold yellow]Watcher:[/bold yellow] {rel} changed")

                rel_str = str(rel)

                if rel_str.startswith("transform"):
                    # If route filters are configured, only react to
                    # changes matching any of them. The matched file's
                    # ``schema.name`` becomes the targeted rebuild list,
                    # so a `--route gold/route_b_*.sql` watch on a
                    # silver model edit does nothing.
                    targets: list[str] | None = None
                    if route_globs:
                        import fnmatch
                        rel_norm = rel_str.replace("\\", "/")
                        if not any(fnmatch.fnmatchcase(rel_norm, g) for g in route_globs):
                            return  # silently ignore — outside route
                        # Derive schema.name from path: transform/<schema>/<name>.sql
                        parts = path.parts
                        try:
                            tx_idx = parts.index("transform")
                            schema = parts[tx_idx + 1]
                            name = path.stem
                            targets = [f"{schema}.{name}"]
                        except (ValueError, IndexError):
                            targets = None
                    # Built by the watcher's loop, under the run gate.
                    watcher.request_rebuild(targets)

                elif rel_str.startswith("macros"):
                    macro_logger.info("Macro file changed: %s — reloading", rel)
                    console.print("[bold yellow]Watcher:[/bold yellow] Reloading macros...")
                    if on_macro_change is not None:
                        try:
                            on_macro_change(project_dir)
                            console.print(
                                "[bold green]Watcher:[/bold green] Macros reloaded"
                            )
                        except Exception as e:
                            console.print(
                                f"[bold red]Watcher:[/bold red] Macro reload failed: {e}"
                            )

            def on_modified(self, event):
                self._handle(event)

            def on_created(self, event):
                # New files in macros/ should trigger a reload just like edits.
                self._handle(event)

        observer = Observer()
        handler = Handler()

        watch_dirs = [
            project_dir / "transform",
            project_dir / "ingest",
            project_dir / "macros",
        ]
        for d in watch_dirs:
            if d.exists():
                observer.schedule(handler, str(d), recursive=True)
                logger.info("Watching: %s", d)

        observer.start()
        console.print("[bold]File watcher started[/bold]")

        try:
            while not self._stop_event.is_set():
                try:
                    self.try_rebuild()
                except Exception as e:
                    logger.error("Watcher: %s", e)
                self._stop_event.wait(self.POLL_SECONDS)
        finally:
            observer.stop()
            observer.join()
