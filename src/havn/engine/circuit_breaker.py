"""Circuit breaker with exponential backoff for script/connector execution.

Prevents repeated execution of failing scripts by tracking failures and
opening the circuit after a configurable threshold. Supports three states:

- CLOSED: Normal operation, calls pass through.
- OPEN: Failures exceeded threshold, calls are skipped until recovery timeout.
- HALF_OPEN: Recovery timeout elapsed, next call is a probe — success closes,
  failure re-opens.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger("havn.circuit_breaker")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is attempted on an open circuit."""

    def __init__(self, name: str, opens_at: float):
        self.name = name
        self.opens_at = opens_at
        remaining = max(0, opens_at - time.time())
        super().__init__(
            f"Circuit '{name}' is OPEN. Recovery in {remaining:.0f}s."
        )


class _CircuitEntry:
    """Per-name circuit state."""

    __slots__ = (
        "state",
        "failure_count",
        "last_failure_at",
        "opens_at",
        "consecutive_successes",
    )

    def __init__(self) -> None:
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_at: float = 0.0
        self.opens_at: float = 0.0
        self.consecutive_successes: int = 0


class CircuitBreaker:
    """Thread-safe, per-name circuit breaker with exponential backoff.

    Args:
        failure_threshold: Number of failures before the circuit opens.
        recovery_timeout: Seconds before an open circuit transitions to half-open.
        backoff_base: Base delay (seconds) for exponential backoff.
        max_backoff: Maximum backoff delay in seconds.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
        backoff_base: float = 2.0,
        max_backoff: float = 60.0,
        failure_window_seconds: float = 600.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.backoff_base = backoff_base
        self.max_backoff = max_backoff
        self.failure_window_seconds = failure_window_seconds
        self._circuits: dict[str, _CircuitEntry] = {}
        self._open_attempts: dict[str, int] = {}
        self._lock = threading.Lock()

    def _get_entry(self, name: str) -> _CircuitEntry:
        if name not in self._circuits:
            self._circuits[name] = _CircuitEntry()
        return self._circuits[name]

    def get_state(self, name: str) -> CircuitState:
        """Return the current effective state for *name*."""
        with self._lock:
            entry = self._get_entry(name)
            return self._effective_state(entry)

    def _effective_state(self, entry: _CircuitEntry) -> CircuitState:
        """Compute effective state, promoting OPEN → HALF_OPEN when timeout elapses."""
        if entry.state == CircuitState.OPEN and time.time() >= entry.opens_at:
            entry.state = CircuitState.HALF_OPEN
        return entry.state

    def backoff_delay(self, attempt: int) -> float:
        """Calculate exponential backoff with jitter for a given attempt number."""
        delay = self.backoff_base * (2 ** attempt) + random.random()
        return min(delay, self.max_backoff)

    def execute(self, name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Execute *fn* through the circuit breaker named *name*.

        Raises ``CircuitOpenError`` if the circuit is open.
        On success in HALF_OPEN state the circuit closes.
        On failure the failure count increments; if it hits the threshold the
        circuit opens.
        """
        with self._lock:
            entry = self._get_entry(name)
            state = self._effective_state(entry)

            if state == CircuitState.OPEN:
                raise CircuitOpenError(name, entry.opens_at)

            # HALF_OPEN or CLOSED — allow the call

        # Execute outside the lock to avoid holding it during I/O
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self._record_failure(name)
            raise

        self._record_success(name)
        return result

    def _record_failure(self, name: str) -> None:
        now = time.time()
        with self._lock:
            entry = self._get_entry(name)
            # Decay: forget failures older than the window so a script that
            # fails once a week never silently accumulates into a permanent
            # open circuit.
            if (
                entry.last_failure_at
                and (now - entry.last_failure_at) > self.failure_window_seconds
                and entry.state == CircuitState.CLOSED
            ):
                entry.failure_count = 0
            entry.failure_count += 1
            entry.last_failure_at = now
            entry.consecutive_successes = 0

            if entry.state == CircuitState.HALF_OPEN:
                attempts = self._open_attempts.get(name, 0) + 1
                self._open_attempts[name] = attempts
                entry.state = CircuitState.OPEN
                base = self.recovery_timeout * (self.backoff_base ** (attempts - 1))
                jitter = random.random() * min(base, 1.0)
                entry.opens_at = now + min(base + jitter, self.max_backoff)
                logger.warning(
                    "Circuit '%s' probe failed. Re-opened (failures=%d, attempt=%d)",
                    name,
                    entry.failure_count,
                    attempts,
                )
            elif entry.failure_count >= self.failure_threshold:
                attempts = self._open_attempts.get(name, 0) + 1
                self._open_attempts[name] = attempts
                entry.state = CircuitState.OPEN
                entry.opens_at = now + self.recovery_timeout
                logger.warning(
                    "Circuit '%s' opened after %d failures. Recovery at +%ds.",
                    name,
                    entry.failure_count,
                    int(entry.opens_at - now),
                )

    def _record_success(self, name: str) -> None:
        with self._lock:
            entry = self._get_entry(name)
            entry.consecutive_successes += 1

            if entry.state == CircuitState.HALF_OPEN:
                entry.state = CircuitState.CLOSED
                entry.failure_count = 0
                entry.last_failure_at = 0.0
                entry.opens_at = 0.0
                self._open_attempts.pop(name, None)
                logger.info("Circuit '%s' closed after successful probe.", name)

    def reset(self, name: str) -> None:
        """Manually reset a circuit to CLOSED."""
        with self._lock:
            entry = self._get_entry(name)
            entry.state = CircuitState.CLOSED
            entry.failure_count = 0
            entry.last_failure_at = 0.0
            entry.opens_at = 0.0
            entry.consecutive_successes = 0
            self._open_attempts.pop(name, None)

    def get_all_states(self) -> list[dict[str, Any]]:
        """Return a snapshot of all circuit states."""
        with self._lock:
            result = []
            for name, entry in self._circuits.items():
                state = self._effective_state(entry)
                result.append(
                    {
                        "name": name,
                        "state": state.value,
                        "failure_count": entry.failure_count,
                        "last_failure_at": entry.last_failure_at or None,
                        "opens_at": entry.opens_at or None,
                    }
                )
            return result

    # ------------------------------------------------------------------
    # Persistence helpers — save/load from DuckDB
    # ------------------------------------------------------------------

    def save_state(self, conn: Any) -> None:
        """Persist all circuit states to ``_havn.circuit_state``."""
        from havn.engine.database import ensure_circuit_state_table

        ensure_circuit_state_table(conn)
        with self._lock:
            for name, entry in self._circuits.items():
                state = self._effective_state(entry)
                conn.execute(
                    "DELETE FROM _havn.circuit_state WHERE name = ?", [name]
                )
                conn.execute(
                    """
                    INSERT INTO _havn.circuit_state
                        (name, state, failure_count, last_failure_at, opens_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        name,
                        state.value,
                        entry.failure_count,
                        entry.last_failure_at or None,
                        entry.opens_at or None,
                    ],
                )

    def load_state(self, conn: Any) -> None:
        """Restore circuit states from ``_havn.circuit_state``."""
        from havn.engine.database import ensure_circuit_state_table

        ensure_circuit_state_table(conn)
        try:
            rows = conn.execute(
                "SELECT name, state, failure_count, last_failure_at, opens_at "
                "FROM _havn.circuit_state"
            ).fetchall()
        except Exception:
            return

        with self._lock:
            for name, state_str, fc, lfa, oa in rows:
                entry = self._get_entry(name)
                entry.state = CircuitState(state_str)
                entry.failure_count = fc
                entry.last_failure_at = lfa or 0.0
                entry.opens_at = oa or 0.0


# ---------------------------------------------------------------------------
# Module-level default instance (shared across the process)
# ---------------------------------------------------------------------------

default_breaker = CircuitBreaker()
