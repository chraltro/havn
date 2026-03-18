"""Tests for the circuit breaker with exponential backoff."""

import threading
import time

import duckdb
import pytest

from havn.engine.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)
from havn.engine.database import ensure_circuit_state_table


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _failing_fn():
    raise RuntimeError("boom")


def _succeeding_fn():
    return "ok"


# ---------------------------------------------------------------------------
# CLOSED -> OPEN transition after N failures
# ---------------------------------------------------------------------------


class TestClosedToOpen:
    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60)
        for _ in range(3):
            with pytest.raises(RuntimeError):
                cb.execute("test", _failing_fn)
        assert cb.get_state("test") == CircuitState.OPEN

    def test_stays_closed_below_threshold(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.execute("test", _failing_fn)
        assert cb.get_state("test") == CircuitState.CLOSED

    def test_open_circuit_raises_circuit_open_error(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.execute("test", _failing_fn)
        with pytest.raises(CircuitOpenError) as exc_info:
            cb.execute("test", _failing_fn)
        assert "test" in str(exc_info.value)


# ---------------------------------------------------------------------------
# OPEN -> HALF_OPEN after recovery timeout
# ---------------------------------------------------------------------------


class TestOpenToHalfOpen:
    def test_transitions_to_half_open_after_timeout(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.1)
        # Open the circuit
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.execute("test", _failing_fn)
        assert cb.get_state("test") == CircuitState.OPEN

        # Wait for recovery
        time.sleep(0.15)
        assert cb.get_state("test") == CircuitState.HALF_OPEN

    def test_half_open_allows_one_call(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.1)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.execute("test", _failing_fn)

        time.sleep(0.15)
        # Should allow the probe call through
        result = cb.execute("test", _succeeding_fn)
        assert result == "ok"


# ---------------------------------------------------------------------------
# HALF_OPEN -> CLOSED on success
# ---------------------------------------------------------------------------


class TestHalfOpenToClosed:
    def test_closes_on_successful_probe(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.1)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.execute("test", _failing_fn)

        time.sleep(0.15)
        cb.execute("test", _succeeding_fn)
        assert cb.get_state("test") == CircuitState.CLOSED

    def test_reopens_on_failed_probe(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.1)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.execute("test", _failing_fn)

        time.sleep(0.15)
        assert cb.get_state("test") == CircuitState.HALF_OPEN
        with pytest.raises(RuntimeError):
            cb.execute("test", _failing_fn)
        assert cb.get_state("test") == CircuitState.OPEN


# ---------------------------------------------------------------------------
# Exponential backoff timing
# ---------------------------------------------------------------------------


class TestExponentialBackoff:
    def test_backoff_increases_exponentially(self):
        cb = CircuitBreaker(backoff_base=2.0, max_backoff=60.0)
        delays = [cb.backoff_delay(i) for i in range(5)]
        # Each delay (minus jitter) should roughly double
        # delay = min(2 * 2^attempt + jitter, 60)
        # attempt 0: ~2*1 + jitter = ~2-3
        # attempt 1: ~2*2 + jitter = ~4-5
        # attempt 2: ~2*4 + jitter = ~8-9
        for i in range(1, len(delays)):
            # The base component doubles, so delay[i] should be > delay[i-1]
            # (modulo jitter, which is at most 1.0)
            assert delays[i] > delays[i - 1] * 0.9  # allow for jitter

    def test_backoff_capped_at_max(self):
        cb = CircuitBreaker(backoff_base=2.0, max_backoff=10.0)
        delay = cb.backoff_delay(20)  # 2 * 2^20 would be huge
        assert delay <= 10.0

    def test_backoff_includes_jitter(self):
        """Multiple calls should produce different values (probabilistic)."""
        cb = CircuitBreaker(backoff_base=1.0)
        delays = {cb.backoff_delay(0) for _ in range(20)}
        # With jitter = random(0,1), we expect variation
        assert len(delays) > 1


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_failures_open_circuit(self):
        cb = CircuitBreaker(failure_threshold=5, recovery_timeout=60)
        errors = []

        def _fail():
            try:
                cb.execute("concurrent", _failing_fn)
            except (RuntimeError, CircuitOpenError):
                pass
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_fail) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Unexpected errors: {errors}"
        # After 20 failures with threshold=5, circuit must be open
        assert cb.get_state("concurrent") == CircuitState.OPEN

    def test_concurrent_mixed_operations(self):
        cb = CircuitBreaker(failure_threshold=100, recovery_timeout=60)
        results = []

        def _succeed():
            try:
                r = cb.execute("mixed", _succeeding_fn)
                results.append(r)
            except Exception:
                pass

        threads = [threading.Thread(target=_succeed) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 50
        assert all(r == "ok" for r in results)
        assert cb.get_state("mixed") == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# Per-name isolation
# ---------------------------------------------------------------------------


class TestPerNameIsolation:
    def test_separate_circuits_per_name(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60)
        # Fail circuit "a" twice
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.execute("a", _failing_fn)
        assert cb.get_state("a") == CircuitState.OPEN
        # Circuit "b" should still be closed
        assert cb.get_state("b") == CircuitState.CLOSED
        result = cb.execute("b", _succeeding_fn)
        assert result == "ok"


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


class TestReset:
    def test_manual_reset(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.execute("test", _failing_fn)
        assert cb.get_state("test") == CircuitState.OPEN
        cb.reset("test")
        assert cb.get_state("test") == CircuitState.CLOSED
        # Should be able to call again
        result = cb.execute("test", _succeeding_fn)
        assert result == "ok"


# ---------------------------------------------------------------------------
# get_all_states
# ---------------------------------------------------------------------------


class TestGetAllStates:
    def test_returns_all_circuits(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60)
        cb.execute("alpha", _succeeding_fn)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.execute("beta", _failing_fn)
        states = cb.get_all_states()
        names = {s["name"] for s in states}
        assert names == {"alpha", "beta"}
        state_map = {s["name"]: s for s in states}
        assert state_map["alpha"]["state"] == "closed"
        assert state_map["beta"]["state"] == "open"
        assert state_map["beta"]["failure_count"] == 2


# ---------------------------------------------------------------------------
# Persistence (save/load with DuckDB)
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_save_and_load(self, tmp_path):
        db_path = tmp_path / "cb_test.duckdb"
        conn = duckdb.connect(str(db_path))
        ensure_circuit_state_table(conn)

        cb1 = CircuitBreaker(failure_threshold=2, recovery_timeout=60)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb1.execute("script_a", _failing_fn)
        cb1.execute("script_b", _succeeding_fn)
        cb1.save_state(conn)

        # Load into a fresh breaker
        cb2 = CircuitBreaker(failure_threshold=2, recovery_timeout=60)
        cb2.load_state(conn)

        assert cb2.get_state("script_a") == CircuitState.OPEN
        assert cb2.get_state("script_b") == CircuitState.CLOSED

        conn.close()
