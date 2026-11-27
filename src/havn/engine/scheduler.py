"""Scheduler: an in-process polling loop for streams, jobs and reports.

``SchedulerThread`` wakes every 30 seconds and runs, synchronously and one
at a time:

- streams in project.yml with a cron ``schedule`` (through the shared
  stream runner, ``engine.stream_runner``, as ``havn stream`` does);
- orchestration jobs (``orchestration/*.yml``) with cron or interval
  (``every 2h``) schedules;
- scheduled dashboard reports.

A cron schedule fires when one of its fire times fell between the previous
poll and this one, so a schedule that came due while an earlier run held
the loop still runs once that run ends. Several missed fires of the same
schedule collapse into one run. The clock starts when the thread starts:
time when no scheduler was running is never caught up (the minute the
thread starts in counts as covered).

There is no queue and no persistence apart from the interval-job
timestamps restored from ``_havn.job_runs``. Also provides ``FileWatcher``
for auto-rebuild on SQL changes.
"""

from __future__ import annotations

import datetime
import logging
import threading
import time
from pathlib import Path

from rich.console import Console

console = Console()
logger = logging.getLogger("havn.scheduler")


def _run_stream_task(project_dir_str: str, stream_name: str, trigger: str = "scheduled") -> dict:
    """Run one stream through the shared stream runner; return its result dict.

    Never raises for a failing stream: the result's ``status`` is
    ``"success"`` or ``"error"``.
    """
    from havn.config import load_project
    from havn.engine.stream_runner import StreamNotFound, run_stream

    project_dir = Path(project_dir_str)
    config = load_project(project_dir)
    try:
        result = run_stream(config, project_dir, stream_name, trigger=trigger)
    except StreamNotFound:
        return {"stream": stream_name, "status": "error", "error": f"Stream '{stream_name}' not found"}
    return result.to_dict()


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


def get_scheduled_streams(project_dir: Path) -> list[dict]:
    """Return info about all streams that have cron schedules."""
    from havn.config import load_project

    config = load_project(project_dir)
    scheduled = []
    for name, stream in config.streams.items():
        if stream.schedule:
            scheduled.append({
                "name": name,
                "description": stream.description,
                "schedule": stream.schedule,
                "steps": [{"action": s.action, "targets": s.targets} for s in stream.steps],
            })
    return scheduled


class SchedulerThread(threading.Thread):
    """Background thread that runs scheduled streams, jobs and reports."""

    POLL_SECONDS = 30

    def __init__(self, project_dir: Path):
        super().__init__(daemon=True, name="havn-scheduler")
        self.project_dir = project_dir
        self._stop_event = threading.Event()
        self._schedules: list[dict] = []
        # Interval-job keys -> epoch seconds of their last fire.
        self._last_run: dict[str, float] = {}
        # Cron keys -> the time up to which that schedule has been
        # evaluated. A fire in (clock, now] is due.
        self._cron_clock: dict[str, datetime.datetime] = {}
        # When the scheduler's clock starts; set by run() (or the first
        # evaluation when _should_run is called on a thread never started).
        self._started_at: datetime.datetime | None = None
        # False until the first poll finishes; see _clock_origin.
        self._cron_clock_settled = False

    def stop(self) -> None:
        self._stop_event.set()

    def _clock_origin(self, now: datetime.datetime) -> datetime.datetime:
        """Where a schedule seen for the first time starts counting from.

        Schedules present when the scheduler starts count from the start of
        the minute it started in; one added to project.yml later counts from
        the start of the minute it first appears in. Either way, nothing
        before is caught up.
        """
        base = now if self._started_at is None or self._cron_clock_settled else self._started_at
        return base.replace(second=0, microsecond=0) - datetime.timedelta(microseconds=1)

    def _should_run(self, name: str, cron_expr: str, now=None) -> bool:
        """True when ``cron_expr`` had a fire time since ``name`` was last evaluated.

        Each call advances ``name``'s clock to ``now``, so a fire is reported
        once, and several fires missed while the loop was busy (or asleep)
        are reported as one. Uses POSIX cron weekdays (0=Sunday). An invalid
        expression never fires. ``now`` is injectable for tests.
        """
        from havn.engine.cron import next_cron_fire

        now = now or datetime.datetime.now()
        last = self._cron_clock.get(name)
        if last is None:
            last = self._clock_origin(now)
        # Only move forward: when the wall clock steps back (DST ends), the
        # repeated hour must not fire everything in it a second time.
        self._cron_clock[name] = max(last, now)
        nxt = next_cron_fire(cron_expr, last)
        return nxt is not None and nxt <= now

    def run(self) -> None:
        self._started_at = datetime.datetime.now()
        logger.info("Scheduler started for %s", self.project_dir)
        self._restore_interval_clocks()

        while not self._stop_event.is_set():
            self._tick()
            # Sleep until next check
            self._stop_event.wait(self.POLL_SECONDS)

        logger.info("Scheduler stopped")

    def _restore_interval_clocks(self) -> None:
        """Restore last-fire timestamps for interval job schedules.

        So that "every 2 weeks" doesn't re-fire immediately after a restart.
        """
        from havn.config import load_project

        try:
            from havn.engine.database import open_warehouse
            from havn.engine.orchestration import (
                discover_jobs,
                ensure_job_runs_table,
                is_interval_schedule,
            )

            config_init = load_project(self.project_dir)
            conn_init = open_warehouse(config_init, self.project_dir)
            try:
                ensure_job_runs_table(conn_init)
                for job in discover_jobs(self.project_dir):
                    schedules = job.schedules or ([job.cron] if job.cron else [])
                    for sched in schedules:
                        if not is_interval_schedule(sched):
                            continue
                        key = f"job:{job.name}:interval:{sched}"
                        row = conn_init.execute(
                            "SELECT MAX(started_at) FROM _havn.job_runs WHERE job_name = ?",
                            [job.name],
                        ).fetchone()
                        if row and row[0] is not None:
                            self._last_run[key] = row[0].timestamp()
                            logger.debug("Restored interval last-run for %s: %s", key, row[0])
            finally:
                conn_init.close()
        except Exception as exc:
            logger.debug("Could not restore interval timestamps: %s", exc)

    def _tick(self, now: datetime.datetime | None = None) -> None:
        """One poll: run every stream, job and report that is due.

        ``now`` is injectable for tests; it is the time due-ness is judged
        at, evaluated once per item just before that item is checked (so a
        long run inside this tick is not lost to the next item's clock).
        """
        from havn.config import load_project

        def _now() -> datetime.datetime:
            return now or datetime.datetime.now()

        if self._started_at is None:
            self._started_at = _now()
        try:
            config = load_project(self.project_dir)
            for name, stream in config.streams.items():
                if not stream.schedule:
                    continue
                if self._should_run(name, stream.schedule, now=_now()):
                    self._run_scheduled_stream(name)

            self._tick_jobs(config, _now)

            # --- Scheduled dashboard reports (_havn.reports) ---
            try:
                from havn.engine.reports import run_due_reports_for_project

                for delivery in run_due_reports_for_project(self.project_dir):
                    colour = "green" if delivery["status"] in ("sent", "skipped") else "red"
                    console.print(
                        f"[bold {colour}]Scheduler:[/bold {colour}] Report "
                        f"{delivery['report_id']} {delivery['status']}"
                    )
            except Exception as e:
                logger.warning("Scheduled report check failed: %s", e)
        except Exception as e:
            logger.error("Scheduler error: %s", e)
        finally:
            # Every schedule present at the first poll has its clock now;
            # any that appears later starts counting from when it appears.
            self._cron_clock_settled = True

    def _run_scheduled_stream(self, name: str) -> None:
        logger.info("Scheduler triggering stream: %s", name)
        console.print(f"[bold blue]Scheduler:[/bold blue] Running stream '{name}'")
        try:
            result = _run_stream_task(str(self.project_dir), name)
        except Exception as e:
            console.print(f"[bold red]Scheduler:[/bold red] Stream '{name}' failed: {e}")
            return
        if result.get("status") == "success":
            console.print(f"[bold green]Scheduler:[/bold green] Stream '{name}' completed")
        else:
            console.print(
                f"[bold red]Scheduler:[/bold red] Stream '{name}' failed: "
                f"{result.get('error') or 'see the log above'}"
            )

    def _tick_jobs(self, config, now_fn) -> None:
        """Run orchestration jobs whose cron or interval schedule is due."""
        try:
            from havn.engine.database import ensure_meta_table, open_warehouse
            from havn.engine.orchestration import (
                discover_jobs,
                ensure_job_runs_table,
                execute_job,
                is_interval_schedule,
                parse_interval,
                resolve_execution_plan,
            )
            from havn.engine.transform.discovery import build_dag, discover_all_models

            for job in discover_jobs(self.project_dir):
                if not job.enabled:
                    continue
                schedules = job.schedules or ([job.cron] if job.cron else [])
                if not schedules:
                    continue
                # Evaluate every cron schedule (each call advances that
                # schedule's clock, so a job with several schedules due in
                # the same window still fires once), then intervals.
                now = now_fn()
                triggering = None
                for sched in schedules:
                    if is_interval_schedule(sched):
                        continue
                    if self._should_run(f"job:{job.name}:{sched}", sched, now=now) and triggering is None:
                        triggering = sched
                if triggering is None:
                    for sched in schedules:
                        if not is_interval_schedule(sched):
                            continue
                        delta = parse_interval(sched)
                        if delta is None:
                            continue
                        key = f"job:{job.name}:interval:{sched}"
                        now_ts = now.timestamp()
                        last = self._last_run.get(key)
                        if last is None or (now_ts - last) >= delta.total_seconds():
                            triggering = sched
                            self._last_run[key] = now_ts
                            break
                if not triggering:
                    continue

                logger.info("Scheduler triggering job: %s", job.name)
                console.print(f"[bold blue]Scheduler:[/bold blue] Running job '{job.name}'")
                jconn = None
                try:
                    jconn = open_warehouse(config, self.project_dir)
                    ensure_meta_table(jconn)
                    ensure_job_runs_table(jconn)
                    models = discover_all_models(self.project_dir)
                    dag = build_dag(models)
                    plan = resolve_execution_plan(
                        job.targets or [job.target],
                        dag,
                        self.project_dir,
                        conn=jconn,
                        resolve=job.resolve,
                        exclude=job.exclude or None,
                    )
                    result = execute_job(job, plan, jconn, self.project_dir, trigger="scheduled")
                    if result.status == "success":
                        console.print(f"[bold green]Scheduler:[/bold green] Job '{job.name}' completed")
                    else:
                        console.print(
                            f"[bold red]Scheduler:[/bold red] Job '{job.name}' {result.status}"
                            + (f": {result.error}" if result.error else "")
                        )
                except Exception as e:
                    console.print(f"[bold red]Scheduler:[/bold red] Job '{job.name}' failed: {e}")
                finally:
                    if jconn is not None:
                        try:
                            jconn.close()
                        except Exception:
                            pass
        except Exception as e:
            logger.debug("Orchestration scheduler check failed: %s", e)


class FileWatcher(threading.Thread):
    """Watches transform/, ingest/, and macros/ for changes, triggers rebuilds.

    The ``on_macro_change`` callback is invoked (with *project_dir*) whenever
    a ``.py`` or ``.sql`` file inside ``macros/`` is saved.  The server layer
    wires this to ``reregister_macros_on_shared_conns`` so live connections
    pick up edits without a restart.
    """

    def __init__(self, project_dir: Path, on_change=None, on_macro_change=None, route_globs: list[str] | None = None):
        super().__init__(daemon=True, name="havn-watcher")
        self.project_dir = project_dir
        self.on_change = on_change
        self.on_macro_change = on_macro_change
        # When set, only transform-side changes whose relative path matches
        # one of these globs trigger a rebuild — and only the matching
        # model is rebuilt, not the whole DAG.
        self.route_globs = route_globs or []
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

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

                    label = f"Running transform (target: {targets[0]})..." if targets else "Running transform..."
                    console.print(f"[bold yellow]Watcher:[/bold yellow] {label}")
                    try:
                        from havn.config import load_project
                        from havn.engine.database import open_warehouse
                        from havn.engine.transform import run_transform

                        config = load_project(project_dir)
                        conn = open_warehouse(config, project_dir)
                        try:
                            run_transform(conn, project_dir / "transform", targets=targets)
                        finally:
                            conn.close()
                        console.print("[bold green]Watcher:[/bold green] Transform completed")
                    except Exception as e:
                        console.print(f"[bold red]Watcher:[/bold red] Transform failed: {e}")

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
                self._stop_event.wait(1)
        finally:
            observer.stop()
            observer.join()
