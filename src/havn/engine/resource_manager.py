"""ResourceManager: per-category resource governance for DuckDB workloads.

Every heavy DuckDB operation (transform, query, streaming, system) acquires a
slot from the ``ResourceManager`` before running. Each category has a memory
budget, a thread budget, and a max-concurrent count enforced by a semaphore.
The manager records live task info so the UI can display active work and
operators can cancel runaway jobs.

Four categories:

- ``transform`` — SQL model execution.
- ``query``     — ad-hoc queries, dashboards, notebooks, SQL API.
- ``streaming`` — CDC, webhook flush, polling.
- ``system``    — sentinel, diff, quality, backup, compaction.

Budgets are hints, not hard limits — DuckDB honours ``SET memory_limit`` and
``SET threads`` per connection but will still evict spill to disk rather
than OOM. The semaphore is the real backpressure: tasks queue rather than
overload the warehouse.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

import duckdb

from havn.engine.observability import (
    ACTIVE_TASKS,
    QUERIES_TOTAL,
    QUERY_DURATION,
    RESOURCE_BUDGET_MEMORY,
    RESOURCE_BUDGET_THREADS,
)

logger = logging.getLogger("havn.resource_manager")

CATEGORIES = ("transform", "query", "streaming", "system")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class CategoryBudget:
    """Budget for a single resource category."""

    memory_gb: float
    threads: int
    max_concurrent: int

    @property
    def memory_bytes(self) -> int:
        return int(self.memory_gb * 1024 * 1024 * 1024)


DEFAULT_BUDGETS: dict[str, CategoryBudget] = {
    "transform": CategoryBudget(memory_gb=4.0, threads=4, max_concurrent=2),
    "query":     CategoryBudget(memory_gb=2.0, threads=2, max_concurrent=8),
    "streaming": CategoryBudget(memory_gb=1.0, threads=2, max_concurrent=4),
    "system":    CategoryBudget(memory_gb=1.0, threads=2, max_concurrent=2),
}


@dataclass
class TaskInfo:
    """Live record of one in-flight task."""

    task_id: str
    category: str
    label: str
    started_at: float
    finished_at: float | None = None
    status: str = "running"           # running | completed | failed | cancelled
    rows_processed: int = 0
    error: str | None = None

    @property
    def duration_ms(self) -> int:
        end = self.finished_at or time.time()
        return int((end - self.started_at) * 1000)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["duration_ms"] = self.duration_ms
        return d


_current_task: contextvars.ContextVar[TaskInfo | None] = contextvars.ContextVar(
    "havn_current_task", default=None
)


# ---------------------------------------------------------------------------
# ResourceManager
# ---------------------------------------------------------------------------


class ResourceManager:
    """Singleton-per-process budget keeper and task registry."""

    def __init__(self, budgets: dict[str, CategoryBudget] | None = None) -> None:
        self._budgets: dict[str, CategoryBudget] = dict(budgets or DEFAULT_BUDGETS)
        for name in CATEGORIES:
            self._budgets.setdefault(name, DEFAULT_BUDGETS[name])

        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._sync_semaphores: dict[str, threading.Semaphore] = {}
        for name, b in self._budgets.items():
            self._sync_semaphores[name] = threading.Semaphore(b.max_concurrent)

        self._active: dict[str, TaskInfo] = {}
        self._recent: deque[TaskInfo] = deque(maxlen=50)
        self._lock = threading.Lock()
        self._cancel_callbacks: dict[str, list[callable]] = {}
        self._publish_budget_metrics()

    # --- Budget management -------------------------------------------------

    def budgets(self) -> dict[str, CategoryBudget]:
        return dict(self._budgets)

    def update_budget(self, category: str, budget: CategoryBudget) -> None:
        """Replace a category's budget. Semaphore is rebuilt if max_concurrent changes.

        Existing tasks keep their original slot; the rebuild only affects new
        acquisitions.
        """
        if category not in CATEGORIES:
            raise ValueError(f"unknown category: {category}")
        old = self._budgets.get(category)
        self._budgets[category] = budget
        if old is None or old.max_concurrent != budget.max_concurrent:
            self._sync_semaphores[category] = threading.Semaphore(budget.max_concurrent)
            # Drop the async semaphore; it'll be recreated lazily on next acquire.
            self._semaphores.pop(category, None)
        self._publish_budget_metrics()

    def _publish_budget_metrics(self) -> None:
        for name, b in self._budgets.items():
            RESOURCE_BUDGET_MEMORY.labels(category=name).set(b.memory_bytes)
            RESOURCE_BUDGET_THREADS.labels(category=name).set(b.threads)

    # --- Task lifecycle ----------------------------------------------------

    @contextlib.contextmanager
    def acquire_sync(
        self,
        category: str,
        label: str,
        *,
        conn: duckdb.DuckDBPyConnection | None = None,
        apply_limits: bool = True,
    ):
        """Blocking acquire. Use from threads / synchronous code paths."""
        if category not in CATEGORIES:
            raise ValueError(f"unknown category: {category}")
        sem = self._sync_semaphores[category]
        sem.acquire()
        task = self._start_task(category, label)
        token = _current_task.set(task)
        if conn is not None and apply_limits:
            _apply_connection_limits(conn, self._budgets[category])
        try:
            yield task
            self._finish_task(task, "completed")
        except duckdb.InterruptException:
            self._finish_task(task, "cancelled")
            raise
        except Exception as e:
            task.error = str(e)[:500]
            self._finish_task(task, "failed")
            raise
        finally:
            _current_task.reset(token)
            sem.release()

    @contextlib.asynccontextmanager
    async def acquire(
        self,
        category: str,
        label: str,
        *,
        conn: duckdb.DuckDBPyConnection | None = None,
        apply_limits: bool = True,
    ):
        """Async acquire. Use from FastAPI handlers and coroutines."""
        if category not in CATEGORIES:
            raise ValueError(f"unknown category: {category}")
        sem = self._get_async_sem(category)
        await sem.acquire()
        task = self._start_task(category, label)
        token = _current_task.set(task)
        if conn is not None and apply_limits:
            _apply_connection_limits(conn, self._budgets[category])
        try:
            yield task
            self._finish_task(task, "completed")
        except duckdb.InterruptException:
            self._finish_task(task, "cancelled")
            raise
        except Exception as e:
            task.error = str(e)[:500]
            self._finish_task(task, "failed")
            raise
        finally:
            _current_task.reset(token)
            sem.release()

    def _get_async_sem(self, category: str) -> asyncio.Semaphore:
        sem = self._semaphores.get(category)
        if sem is None:
            sem = asyncio.Semaphore(self._budgets[category].max_concurrent)
            self._semaphores[category] = sem
        return sem

    def _start_task(self, category: str, label: str) -> TaskInfo:
        task = TaskInfo(
            task_id=str(uuid.uuid4()),
            category=category,
            label=label,
            started_at=time.time(),
        )
        with self._lock:
            self._active[task.task_id] = task
        ACTIVE_TASKS.labels(category=category).inc()
        return task

    def _finish_task(self, task: TaskInfo, status: str) -> None:
        task.finished_at = time.time()
        task.status = status
        with self._lock:
            self._active.pop(task.task_id, None)
            self._recent.appendleft(task)
        ACTIVE_TASKS.labels(category=task.category).dec()
        QUERIES_TOTAL.labels(category=task.category, status=status).inc()
        QUERY_DURATION.labels(category=task.category, status=status).observe(
            task.duration_ms / 1000.0
        )

    # --- Cancellation ------------------------------------------------------

    def register_cancel(self, task_id: str, callback) -> None:
        """Attach a cancellation callback to an active task (e.g. conn.interrupt)."""
        with self._lock:
            self._cancel_callbacks.setdefault(task_id, []).append(callback)

    def cancel(self, task_id: str) -> bool:
        """Fire cancel callbacks for a task. Returns True if the task existed."""
        with self._lock:
            task = self._active.get(task_id)
            callbacks = self._cancel_callbacks.pop(task_id, [])
        if not task:
            return False
        task.status = "cancelling"
        for cb in callbacks:
            try:
                cb()
            except Exception as e:
                logger.warning("cancel callback failed for %s: %s", task_id, e)
        return True

    # --- Snapshots for the UI / SSE ----------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Current state: categories, budgets, active tasks, recent tasks."""
        with self._lock:
            active = [t.to_dict() for t in self._active.values()]
            recent = [t.to_dict() for t in list(self._recent)[:20]]

        categories = []
        for name in CATEGORIES:
            b = self._budgets[name]
            n_active = sum(1 for t in active if t["category"] == name)
            categories.append(
                {
                    "name": name,
                    "memory_gb": b.memory_gb,
                    "threads": b.threads,
                    "max_concurrent": b.max_concurrent,
                    "active": n_active,
                    "utilization": round(n_active / b.max_concurrent, 3) if b.max_concurrent else 0,
                }
            )

        return {
            "categories": categories,
            "active": active,
            "recent": recent,
            "total_memory_gb": sum(b.memory_gb for b in self._budgets.values()),
            "total_active": len(active),
        }

    def active_tasks(self) -> Iterable[TaskInfo]:
        with self._lock:
            return list(self._active.values())


# ---------------------------------------------------------------------------
# Connection settings
# ---------------------------------------------------------------------------


def _apply_connection_limits(conn: duckdb.DuckDBPyConnection, budget: CategoryBudget) -> None:
    """Apply per-category memory / thread limits to a DuckDB connection."""
    try:
        conn.execute(f"SET memory_limit = '{int(budget.memory_gb * 1024)}MB'")
    except Exception as e:
        logger.debug("memory_limit set failed: %s", e)
    try:
        conn.execute(f"SET threads = {max(1, budget.threads)}")
    except Exception as e:
        logger.debug("threads set failed: %s", e)


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------


_manager: ResourceManager | None = None
_manager_lock = threading.Lock()


def get_resource_manager() -> ResourceManager:
    """Return the process-wide ResourceManager, creating it on first use."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = ResourceManager(_load_budgets_from_config())
    return _manager


def reset_resource_manager() -> None:
    """Test helper — drop the singleton."""
    global _manager
    with _manager_lock:
        _manager = None


def _load_budgets_from_config() -> dict[str, CategoryBudget]:
    """Read budgets from project.yml if present; otherwise defaults."""
    try:
        from havn.server.deps import _get_config
        cfg = _get_config()
    except Exception:
        return dict(DEFAULT_BUDGETS)

    raw = getattr(cfg, "resources", None) or {}
    if not isinstance(raw, dict):
        return dict(DEFAULT_BUDGETS)

    out: dict[str, CategoryBudget] = {}
    for name in CATEGORIES:
        base = DEFAULT_BUDGETS[name]
        entry = raw.get(name) or {}
        try:
            out[name] = CategoryBudget(
                memory_gb=float(entry.get("memory_gb", base.memory_gb)),
                threads=int(entry.get("threads", base.threads)),
                max_concurrent=int(entry.get("max_concurrent", base.max_concurrent)),
            )
        except (TypeError, ValueError):
            out[name] = base
    return out


def current_task() -> TaskInfo | None:
    """Return the TaskInfo for the current async context, if any."""
    return _current_task.get()
