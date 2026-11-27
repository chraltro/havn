"""One scheduler per warehouse, enforced with an OS-level file lock.

Two schedulers on one warehouse (``havn schedule`` in a terminal and
``havn serve --schedule``, or two ``havn schedule`` processes) would both
dispatch every fire, and each one's startup marks the other's live history
rows ``interrupted``. :class:`SchedulerLock` is held for the scheduler's
whole lifetime: an exclusive, non-blocking lock on a file under
``.havn/scheduler/``, keyed by the warehouse the scheduler builds (two
environments of one project have two warehouses and may each have their
own scheduler).

The lock is the operating system's (``msvcrt.locking`` on Windows,
``fcntl.flock`` elsewhere), so it is released when the process ends, also
on a crash or ``kill -9``: a stale lock file never blocks the next start.
It also excludes a second scheduler inside the same process, since each
acquire opens its own file handle. Who holds it (pid, host, command, start
time) is written to a separate ``.json`` file next to it, because a locked
region cannot be read by other processes on Windows.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import socket
import sys
import threading
from pathlib import Path


class SchedulerLockedError(RuntimeError):
    """Another scheduler already runs against this warehouse."""


def warehouse_key(config, project_dir: Path) -> str:
    """A stable name for the warehouse ``config`` resolves to."""
    db = getattr(config, "database", None)
    backend = getattr(db, "backend", "duckdb") or "duckdb"
    raw = str(getattr(db, "path", "") or "warehouse.duckdb")
    if backend == "duckdb" and raw != ":memory:":
        p = Path(raw)
        if not p.is_absolute():
            p = Path(project_dir) / p
        try:
            p = p.resolve()
        except OSError:
            pass
        ident = os.path.normcase(str(p))
        stem = p.stem
    else:
        ident = f"{backend}:{os.path.normcase(str(Path(project_dir).resolve()))}:{raw}"
        stem = backend
    digest = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:12]
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem)[:40] or "warehouse"
    return f"{safe}-{digest}"


class SchedulerLock:
    """The lock for one warehouse. ``acquire()`` raises when it is held."""

    def __init__(self, project_dir: Path, key: str, *, warehouse: str = ""):
        self.dir = Path(project_dir) / ".havn" / "scheduler"
        self.path = self.dir / f"{key}.lock"
        self.info_path = self.dir / f"{key}.json"
        self.warehouse = warehouse
        self._fh = None
        self._mutex = threading.Lock()

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> None:
        with self._mutex:
            if self._fh is not None:
                return
            self.dir.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+b")  # noqa: SIM115 - held for the lifetime
            try:
                _lock(fh)
            except OSError:
                fh.close()
                raise SchedulerLockedError(self._held_message()) from None
            self._fh = fh
            try:
                self.info_path.write_text(json.dumps({
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "command": " ".join(os.path.basename(a) if i == 0 else a
                                        for i, a in enumerate(sys.argv)) or "python",
                    "started_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                    "warehouse": self.warehouse,
                }), encoding="utf-8")
            except OSError:
                pass

    def release(self) -> None:
        with self._mutex:
            fh, self._fh = self._fh, None
            if fh is None:
                return
            try:
                self.info_path.unlink()
            except OSError:
                pass
            try:
                _unlock(fh)
            except OSError:
                pass
            fh.close()

    def holder(self) -> dict | None:
        """What the info file says about the current holder, if anything."""
        try:
            return json.loads(self.info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _held_message(self) -> str:
        info = self.holder() or {}
        where = f" on {self.warehouse}" if self.warehouse else ""
        if info.get("pid"):
            who = f"process {info['pid']}"
            if info.get("host") and info["host"] != socket.gethostname():
                who += f" on {info['host']}"
            if info.get("command"):
                who += f" ({info['command']})"
            if info.get("started_at"):
                who += f", started {info['started_at']}"
        else:
            who = "another process"
        return (
            f"Another scheduler is already running{where}: {who}. "
            "Only one scheduler may run per warehouse; stop the other one first "
            f"(lock file: {self.path})."
        )


if sys.platform == "win32":
    import msvcrt

    def _lock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
