"""One build at a time per model, within this process.

Batch transforms, job runs and the live runner can all decide to build the
same model at the same moment. Every build goes through ``execute_model``,
which holds this lock for the model while it writes, so they take turns.

The lock is re-entrant: the live runner takes it around a whole refresh (the
build, its assertions and the commit) and ``execute_model`` takes it again
inside on the same thread. Across processes there is no lock to take; a
second process cannot open a DuckDB file that is open for writing, and on
DuckLake two writers racing on one live model collide on its consumed
watermark row and one of them fails instead of applying the batch twice.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()


def _lock_for(model: str) -> threading.RLock:
    with _guard:
        lock = _locks.get(model)
        if lock is None:
            lock = _locks[model] = threading.RLock()
        return lock


@contextmanager
def model_lock(model: str, *, timeout: float | None = None) -> Iterator[bool]:
    """Hold ``model``'s build lock. Yields False (holding nothing) on timeout.

    ``timeout=None`` waits as long as it takes, which is what a build wants;
    the live runner passes ``0`` so a model that a batch run is building is
    retried on the next cycle instead of stalling the write queue.
    """
    lock = _lock_for(model)
    acquired = lock.acquire() if timeout is None else lock.acquire(timeout=timeout)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def is_locked(model: str) -> bool:
    """Whether another thread holds ``model``'s lock right now (best effort)."""
    lock = _lock_for(model)
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True
