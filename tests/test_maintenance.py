"""Tests for the DuckLake maintenance scheduler."""

from __future__ import annotations

import threading
import time

import duckdb
import pytest

from havn.engine.streaming.maintenance import MaintenanceConfig, MaintenanceScheduler


def test_scheduler_noop_on_duckdb_backend(tmp_path):
    """Starting on a DuckDB backend must not launch a thread."""
    path = str(tmp_path / "wh.duckdb")

    def factory():
        return duckdb.connect(path)

    sched = MaintenanceScheduler(
        connection_factory=factory,
        backend_name="duckdb",
        config=MaintenanceConfig(flush_interval_s=1, merge_interval_s=1, checkpoint_interval_s=1),
    )
    sched.start()
    assert sched._thread is None  # no thread launched
    sched.stop()


def test_scheduler_flush_handles_missing_function_gracefully(tmp_path):
    """A backend named 'ducklake' without the function loaded shouldn't crash."""
    path = str(tmp_path / "wh.duckdb")

    def factory():
        # Plain DuckDB — ducklake_flush doesn't exist, which should log at
        # debug and swallow rather than propagate.
        return duckdb.connect(path)

    sched = MaintenanceScheduler(
        connection_factory=factory,
        backend_name="ducklake",
    )
    # Call flush directly rather than through the thread loop.
    sched.flush()  # must not raise
    sched.merge()  # must not raise
    sched.checkpoint_and_expire()  # must not raise


def test_scheduler_thread_lifecycle(tmp_path):
    """Start + stop round-trip cleanly."""
    path = str(tmp_path / "wh.duckdb")

    def factory():
        return duckdb.connect(path)

    # Very large intervals so the loop sleeps quietly.
    sched = MaintenanceScheduler(
        connection_factory=factory,
        backend_name="ducklake",
        config=MaintenanceConfig(
            flush_interval_s=3600,
            merge_interval_s=3600,
            checkpoint_interval_s=3600,
        ),
    )
    sched.start()
    time.sleep(0.05)
    assert sched._thread is not None and sched._thread.is_alive()
    sched.stop()
    time.sleep(0.05)
    assert sched._thread is None
