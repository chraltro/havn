"""Tests for the webhook staging + flush worker."""

from __future__ import annotations

import time

import duckdb
import pytest

from havn.engine.streaming.webhook import (
    FlushWorker,
    STAGING_TABLE,
    WebhookStaging,
    append_event,
)


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "wh.duckdb"
    c = duckdb.connect(str(path))
    try:
        yield c
    finally:
        c.close()


def test_staging_table_created_idempotently(conn):
    WebhookStaging.ensure(conn)
    WebhookStaging.ensure(conn)
    rows = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema='_havn' AND table_name='webhook_staging'"
    ).fetchone()
    assert rows[0] == 1


def test_append_and_backlog(conn):
    append_event(conn, "orders", {"id": 1, "total": 9.99})
    append_event(conn, "orders", {"id": 2, "total": 19.99})
    assert WebhookStaging.backlog(conn) == 2


def test_flush_moves_rows_to_landing(tmp_path):
    path = str(tmp_path / "wh.duckdb")
    prep = duckdb.connect(path)
    try:
        append_event(prep, "payments", {"amount": 10})
        append_event(prep, "payments", {"amount": 20})
    finally:
        prep.close()

    worker = FlushWorker(connection_factory=lambda: duckdb.connect(path), flush_interval=0.1)
    flushed = worker.flush_once()
    assert flushed == 2

    verify = duckdb.connect(path)
    try:
        rows = verify.execute("SELECT count(*) FROM landing.payments").fetchone()[0]
        assert rows == 2
        backlog = verify.execute(
            f"SELECT count(*) FROM {STAGING_TABLE} WHERE flushed=false"
        ).fetchone()[0]
        assert backlog == 0
    finally:
        verify.close()


def test_flush_noop_when_empty(tmp_path):
    path = str(tmp_path / "wh.duckdb")
    # Initialize staging table.
    bootstrap = duckdb.connect(path)
    try:
        WebhookStaging.ensure(bootstrap)
    finally:
        bootstrap.close()

    worker = FlushWorker(connection_factory=lambda: duckdb.connect(path))
    assert worker.flush_once() == 0


def test_purge_flushed_drops_old_rows(tmp_path):
    path = str(tmp_path / "wh.duckdb")
    prep = duckdb.connect(path)
    try:
        append_event(prep, "ev", {"a": 1})
    finally:
        prep.close()

    worker = FlushWorker(connection_factory=lambda: duckdb.connect(path))
    worker.flush_once()
    worker.purge_flushed(older_than_seconds=0)

    verify = duckdb.connect(path)
    try:
        remaining = verify.execute(
            f"SELECT count(*) FROM {STAGING_TABLE}"
        ).fetchone()[0]
        assert remaining == 0
    finally:
        verify.close()


def test_rejects_bad_source_identifier(conn):
    import pytest as _pytest

    with _pytest.raises(ValueError):
        append_event(conn, "drop table; --", {"hi": "there"})
