"""Catalog DDL that concurrent runs in one process may both issue.

``CREATE SCHEMA IF NOT EXISTS`` is not safe to race in DuckDB: when two
transactions create the same catalog entry at the same moment, the second
fails with "Catalog write-write conflict on create" instead of finding the
entry there. With ``scheduler.max_concurrent`` above 1, two scheduled runs
starting together both create ``landing``, ``bronze`` and their models'
schemas, and both register the project's SQL macros.

:func:`catalog_ddl` runs such a step under one process-wide lock (each
statement commits on its own, so the second caller finds the entries
committed and does nothing) and retries it a few times when the conflict
comes from a writer that does not take the lock (a user's ingest script
creating a schema). Every step passed to it must be idempotent.

havn's own ``_havn`` tables are not created here: the migrations
(``engine.migrations``) create them, serialised by their own lock. The two
locks compose in one order only: a step under this lock may run a
migration (through an ``ensure_*`` helper), but a migration never takes
this lock, so they cannot deadlock. A migration in progress on another
connection creates only ``_havn`` entries, which no step here touches.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

_CATALOG_LOCK = threading.RLock()
_ATTEMPTS = 5


def is_catalog_conflict(exc: BaseException) -> bool:
    text = str(exc)
    return "write-write conflict" in text or "Catalog write-write" in text


def catalog_ddl(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run the idempotent catalog step ``fn(*args, **kwargs)`` safely; see the module doc."""
    import duckdb

    with _CATALOG_LOCK:
        for attempt in range(1, _ATTEMPTS + 1):
            try:
                return fn(*args, **kwargs)
            except duckdb.TransactionException as e:
                if attempt == _ATTEMPTS or not is_catalog_conflict(e):
                    raise
                time.sleep(0.02 * attempt)
    raise AssertionError("unreachable")
