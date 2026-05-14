"""Scheduler: Huey + SqliteHuey for cron-based stream execution.

Runs streams on cron schedules defined in project.yml.
Also supports file-watching for auto-rebuild on SQL changes.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from huey import SqliteHuey, crontab
from rich.console import Console

console = Console()
logger = logging.getLogger("havn.scheduler")

# Global huey instance — initialized by start_scheduler()
_huey: SqliteHuey | None = None
_project_dir: Path | None = None


def _get_huey(project_dir: Path) -> SqliteHuey:
    """Get or create the Huey instance backed by SQLite in the project dir."""
    global _huey
    if _huey is None:
        db_path = project_dir / ".havn_scheduler.db"
        _huey = SqliteHuey(filename=str(db_path), immediate=False)
    return _huey


def _run_stream_task(project_dir_str: str, stream_name: str) -> dict:
    """Execute a stream. Called by Huey as a task."""
    from havn.config import load_project
    from havn.engine.database import open_warehouse
    from havn.engine.runner import run_scripts_in_dir
    from havn.engine.transform import run_transform

    project_dir = Path(project_dir_str)
    config = load_project(project_dir)
    stream_config = config.streams.get(stream_name)
    if not stream_config:
        return {"error": f"Stream '{stream_name}' not found"}

    conn = open_warehouse(config, project_dir)
    step_results = []

    try:
        for step in stream_config.steps:
            if step.action == "ingest":
                results = run_scripts_in_dir(conn, project_dir / "ingest", "ingest", step.targets)
                step_results.append({"action": "ingest", "results": [r["status"] for r in results]})
            elif step.action == "transform":
                targets = step.targets if step.targets != ["all"] else None
                results = run_transform(conn, project_dir / "transform", targets=targets)
                step_results.append({"action": "transform", "results": results})
            elif step.action == "export":
                results = run_scripts_in_dir(conn, project_dir / "export", "export", step.targets)
                step_results.append({"action": "export", "results": [r["status"] for r in results]})

        logger.info("Stream '%s' completed: %s", stream_name, step_results)
        return {"stream": stream_name, "status": "success", "steps": step_results}
    except Exception as e:
        logger.error("Stream '%s' failed: %s", stream_name, e)
        return {"stream": stream_name, "status": "error", "error": str(e)}
    finally:
        conn.close()


def _parse_cron(cron_expr: str) -> dict:
    """Parse a cron expression '* * * * *' into Huey crontab kwargs.

    Format: minute hour day month day_of_week
    """
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron expression: '{cron_expr}' (expected 5 fields)")

    kwargs = {}
    fields = ["minute", "hour", "day", "month", "day_of_week"]
    for field_name, value in zip(fields, parts):
        if value != "*":
            kwargs[field_name] = value
    return kwargs


def _cron_field_matches(pattern: str, current: int) -> bool:
    """Return True if `current` matches a single cron field `pattern`.

    Supports lists (1,5,30), ranges (1-5), steps (*/5, 0-29/5), and the
    star wildcard. Each comma-separated alternative is evaluated independently
    and the result is the logical OR.
    """
    if pattern == "*":
        return True
    try:
        for token in pattern.split(","):
            token = token.strip()
            if not token:
                continue
            step = 1
            if "/" in token:
                head, step_str = token.split("/", 1)
                step = int(step_str)
                if step <= 0:
                    continue
            else:
                head = token
            if head == "*" or head == "":
                lo, hi = None, None
            elif "-" in head:
                lo_s, hi_s = head.split("-", 1)
                lo, hi = int(lo_s), int(hi_s)
            else:
                value = int(head)
                if step == 1:
                    if current == value:
                        return True
                    continue
                lo, hi = value, value
            if lo is None:
                if current % step == 0:
                    return True
                continue
            if lo <= current <= hi and (current - lo) % step == 0:
                return True
    except (ValueError, ZeroDivisionError):
        return False
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
    """Background thread that runs scheduled streams via a simple cron loop."""

    def __init__(self, project_dir: Path):
        super().__init__(daemon=True, name="havn-scheduler")
        self.project_dir = project_dir
        self._stop_event = threading.Event()
        self._schedules: list[dict] = []
        self._last_run: dict[str, float] = {}

    def stop(self) -> None:
        self._stop_event.set()

    def _should_run(self, name: str, cron_expr: str) -> bool:
        """Check if a cron expression matches the current minute.

        Uses POSIX cron weekday convention: 0=Sunday, 1=Monday, ..., 6=Saturday.
        """
        import datetime

        now = datetime.datetime.now()
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            return False

        # POSIX cron weekday: 0=Sun..6=Sat. Python's weekday(): 0=Mon..6=Sun.
        posix_weekday = (now.weekday() + 1) % 7
        checks = [
            (parts[0], now.minute),
            (parts[1], now.hour),
            (parts[2], now.day),
            (parts[3], now.month),
            (parts[4], posix_weekday),
        ]

        for pattern, current in checks:
            if not _cron_field_matches(pattern, current):
                return False

        # Don't run more than once per minute
        last = self._last_run.get(name, 0)
        minute_key = now.replace(second=0, microsecond=0).timestamp()
        if last >= minute_key:
            return False

        return True

    def run(self) -> None:
        from havn.config import load_project

        logger.info("Scheduler started for %s", self.project_dir)

        # Restore last-fire timestamps for interval schedules from the database
        # so that "every 2 weeks" doesn't re-fire immediately after restart.
        try:
            from havn.engine.database import open_warehouse as _open_warehouse
            from havn.engine.orchestration import (
                discover_jobs as _discover_jobs,
                ensure_job_runs_table as _ensure_runs,
                is_interval_schedule as _is_interval,
            )

            config_init = load_project(self.project_dir)
            conn_init = _open_warehouse(config_init, self.project_dir)
            try:
                _ensure_runs(conn_init)
                for job in _discover_jobs(self.project_dir):
                    schedules = job.schedules or ([job.cron] if job.cron else [])
                    for sched in schedules:
                        if not _is_interval(sched):
                            continue
                        key = f"job:{job.name}:interval:{sched}"
                        row = conn_init.execute(
                            "SELECT MAX(started_at) FROM _havn.job_runs WHERE job_name = ?",
                            [job.name],
                        ).fetchone()
                        if row and row[0] is not None:
                            self._last_run[key] = row[0].timestamp()
                            logger.debug(
                                "Restored interval last-run for %s: %s", key, row[0]
                            )
            finally:
                conn_init.close()
        except Exception as exc:
            logger.debug("Could not restore interval timestamps: %s", exc)

        while not self._stop_event.is_set():
            try:
                config = load_project(self.project_dir)
                for name, stream in config.streams.items():
                    if not stream.schedule:
                        continue
                    if self._should_run(name, stream.schedule):
                        import datetime

                        minute_key = datetime.datetime.now().replace(
                            second=0, microsecond=0
                        ).timestamp()
                        self._last_run[name] = minute_key
                        logger.info("Scheduler triggering stream: %s", name)
                        console.print(f"[bold blue]Scheduler:[/bold blue] Running stream '{name}'")
                        try:
                            _run_stream_task(str(self.project_dir), name)
                            console.print(f"[bold green]Scheduler:[/bold green] Stream '{name}' completed")
                        except Exception as e:
                            console.print(f"[bold red]Scheduler:[/bold red] Stream '{name}' failed: {e}")

                # --- Orchestration jobs ---
                try:
                    from havn.engine.database import open_warehouse
                    from havn.engine.database import ensure_meta_table as _emt
                    from havn.engine.orchestration import (
                        discover_jobs,
                        ensure_job_runs_table,
                        execute_job,
                        resolve_execution_plan,
                    )
                    from havn.engine.transform.discovery import (
                        build_dag,
                        discover_models,
                    )

                    from havn.engine.orchestration import (
                        is_interval_schedule,
                        parse_interval,
                    )
                    orch_jobs = discover_jobs(self.project_dir)
                    for job in orch_jobs:
                        if not job.enabled:
                            continue
                        schedules = job.schedules or ([job.cron] if job.cron else [])
                        if not schedules:
                            continue
                        # Trigger if ANY schedule matches
                        triggering = None
                        for sched in schedules:
                            if is_interval_schedule(sched):
                                delta = parse_interval(sched)
                                if delta is None:
                                    continue
                                key = f"job:{job.name}:interval:{sched}"
                                import time as _t
                                now_ts = _t.time()
                                last = self._last_run.get(key)
                                if last is None or (now_ts - last) >= delta.total_seconds():
                                    triggering = sched
                                    self._last_run[key] = now_ts
                                    break
                            else:
                                if self._should_run(f"job:{job.name}:{sched}", sched):
                                    triggering = sched
                                    break
                        if triggering:
                            import datetime

                            mk = datetime.datetime.now().replace(
                                second=0, microsecond=0
                            ).timestamp()
                            self._last_run[f"job:{job.name}"] = mk
                            logger.info("Scheduler triggering job: %s", job.name)
                            console.print(
                                f"[bold blue]Scheduler:[/bold blue] Running job '{job.name}'"
                            )
                            jconn = None
                            try:
                                jconn = open_warehouse(config, self.project_dir)
                                _emt(jconn)
                                ensure_job_runs_table(jconn)
                                models = discover_models(self.project_dir / "transform")
                                dag = build_dag(models)
                                plan = resolve_execution_plan(
                                    job.targets or [job.target],
                                    dag,
                                    self.project_dir,
                                    conn=jconn,
                                    resolve=job.resolve,
                                )
                                execute_job(
                                    job, plan, jconn, self.project_dir, trigger="scheduled"
                                )
                                console.print(
                                    f"[bold green]Scheduler:[/bold green] Job '{job.name}' completed"
                                )
                            except Exception as e:
                                console.print(
                                    f"[bold red]Scheduler:[/bold red] Job '{job.name}' failed: {e}"
                                )
                            finally:
                                if jconn is not None:
                                    try:
                                        jconn.close()
                                    except Exception:
                                        pass
                except Exception as e:
                    logger.debug("Orchestration scheduler check failed: %s", e)
            except Exception as e:
                logger.error("Scheduler error: %s", e)

            # Sleep until next check (poll every 30 seconds)
            self._stop_event.wait(30)

        logger.info("Scheduler stopped")


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
