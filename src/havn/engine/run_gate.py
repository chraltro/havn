"""One process-wide gate between runs that write to a project's warehouse.

Inside one process (``havn serve --schedule``) the web UI and the scheduler's
worker pool can both start runs against the same warehouse. Two runs that
create tables, load landing data or write ``_havn`` metadata at the same time
hit DuckDB write-write conflicts, so they must not overlap. The UI already
ran one operation at a time and the scheduler's pool is sized by
``scheduler.max_concurrent``; this gate is what makes the two know about each
other.

Two kinds of holder share one gate per project:

- ``exclusive`` (the web UI and the server's blocking run endpoints): only
  when nobody else holds the gate. :meth:`RunGate.try_exclusive` never
  waits; a busy gate returns a :class:`GateBusy` saying who holds it, so the
  UI can answer "already running" at once.
- ``shared`` (scheduled streams and jobs): any number at once, so
  ``scheduler.max_concurrent`` > 1 still lets scheduled runs overlap each
  other, as the user opted into. :meth:`RunGate.acquire_shared` waits while
  an exclusive holder is in, so a scheduled run that fires during a UI run
  starts as soon as the UI run ends.

A scheduled run waiting for the gate also keeps new exclusive holders out:
otherwise a user starting one UI run after another could hold a due
schedule back indefinitely.

The CLI (``havn stream``, ``havn jobs run``) runs in its own process, where
DuckDB's file lock already keeps other processes out; it does not take the
gate.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

EXCLUSIVE = "exclusive"
SHARED = "shared"


@dataclass(frozen=True)
class GateBusy:
    """Why the gate could not be taken: who holds it (or waits for it)."""

    kind: str               # EXCLUSIVE or SHARED: the kind of the current holders
    labels: tuple[str, ...]  # what each holder is running ("stream 'nightly'")
    waiting: bool = False   # True: free, but a scheduled run is about to take it

    @property
    def message(self) -> str:
        what = ", ".join(self.labels) or "a run"
        if self.waiting:
            return f"A scheduled run is about to start ({what})."
        if self.kind == SHARED:
            return f"A scheduled run is in progress ({what})."
        return f"An operation is already running ({what})."


@dataclass(eq=False)
class GateTicket:
    """Proof of holding the gate; pass it to :meth:`RunGate.release`."""

    gate: RunGate
    kind: str
    label: str
    released: bool = field(default=False)

    def release(self) -> None:
        self.gate.release(self)


class RunGate:
    """The gate of one project. Get it with :func:`gate_for`."""

    def __init__(self, key: str):
        self.key = key
        self._cond = threading.Condition(threading.Lock())
        self._holders: list[GateTicket] = []
        self._waiting: list[str] = []

    # -- state ---------------------------------------------------------------

    def _kind(self) -> str | None:
        return self._holders[0].kind if self._holders else None

    def busy(self) -> GateBusy | None:
        """Who holds the gate right now, or None when it is free."""
        with self._cond:
            return self._busy_locked()

    def _busy_locked(self) -> GateBusy | None:
        if self._holders:
            return GateBusy(self._kind(), tuple(t.label for t in self._holders))
        if self._waiting:
            return GateBusy(SHARED, tuple(self._waiting), waiting=True)
        return None

    # -- acquiring -----------------------------------------------------------

    def try_exclusive(self, label: str) -> GateTicket | GateBusy:
        """Take the gate alone, now, or say why not. Never waits."""
        with self._cond:
            busy = self._busy_locked()
            if busy is not None:
                return busy
            ticket = GateTicket(self, EXCLUSIVE, label)
            self._holders.append(ticket)
            return ticket

    def acquire_shared(
        self,
        label: str,
        *,
        should_stop: Callable[[], bool] | None = None,
        poll: float = 0.5,
    ) -> GateTicket | None:
        """Take the gate alongside other shared holders; wait out an exclusive one.

        ``should_stop`` is checked while waiting; when it returns True the
        wait ends and None is returned (the scheduler stopping).
        """
        with self._cond:
            self._waiting.append(label)
            try:
                while self._kind() == EXCLUSIVE:
                    if should_stop is not None and should_stop():
                        return None
                    self._cond.wait(poll)
                ticket = GateTicket(self, SHARED, label)
                self._holders.append(ticket)
                return ticket
            finally:
                self._waiting.remove(label)
                self._cond.notify_all()

    def release(self, ticket: GateTicket) -> None:
        """Give the gate back. Releasing twice is harmless."""
        with self._cond:
            if ticket.released:
                return
            ticket.released = True
            try:
                self._holders.remove(ticket)
            except ValueError:
                pass
            self._cond.notify_all()

    def wait_free(self, timeout: float | None = None) -> bool:
        """Block until nobody holds or waits for the gate. For tests."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self._holders or self._waiting:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._cond.wait(remaining)
        return True


_gates: dict[str, RunGate] = {}
_gates_lock = threading.Lock()


def _key(project_dir: Path | str) -> str:
    try:
        return os.path.normcase(str(Path(project_dir).resolve()))
    except OSError:
        return os.path.normcase(str(project_dir))


def gate_for(project_dir: Path | str) -> RunGate:
    """The gate of the project at ``project_dir`` (one per process).

    Keyed by project rather than by warehouse file: the server and the
    scheduler may resolve different environments of one project, and a
    project-wide gate is the stricter of the two.
    """
    key = _key(project_dir)
    with _gates_lock:
        gate = _gates.get(key)
        if gate is None:
            gate = _gates[key] = RunGate(key)
        return gate
