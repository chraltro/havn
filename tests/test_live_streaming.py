"""The streaming connectors feed live models: each commit advances its landing table.

Webhook flush, logical-replication CDC, API polling and the connector
high-watermark sync all call ``advance_source`` after they commit. These tests
drive each one against a real DuckDB file and check the landing table is
stamped, the watermark moved and the advance event went out.

The Postgres logical-replication test at the bottom needs a server with
``wal_level=logical`` and the vendored ``pypgoutput``; it is skipped unless
``HAVN_TEST_PG_DSN`` is set.
"""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

import duckdb
import pytest

from havn.engine.live import events
from havn.engine.live.state import source_watermarks
from havn.engine.transform import run_transform


@pytest.fixture
def captured():
    seen = []
    unsubscribe = events.subscribe(seen.append)
    yield seen
    unsubscribe()


def test_webhook_flush_advances_landing(tmp_path, captured):
    from havn.engine.streaming.webhook import FlushWorker, append_event

    path = str(tmp_path / "w.duckdb")
    conn = duckdb.connect(path)
    append_event(conn, "orders", {"id": 1})
    append_event(conn, "orders", {"id": 2})
    worker = FlushWorker(shared_conn=conn)
    assert worker.flush_once() == 2
    assert conn.execute("SELECT count(*), min(_havn_seq), max(_havn_seq) FROM landing.orders").fetchone() == (2, 1, 2)
    assert source_watermarks(conn) == {"landing.orders": 2}
    assert [(e.source, e.watermark, e.rows) for e in captured] == [("landing.orders", 2, 2)]
    append_event(conn, "orders", {"id": 3})
    worker.flush_once()
    assert source_watermarks(conn) == {"landing.orders": 3}


def _change(op, table, row, lsn):
    if op == "D":
        return SimpleNamespace(op=op, table_name=table, before=row, lsn=lsn)
    return SimpleNamespace(op=op, table_name=table, after=row, lsn=lsn)


def test_logical_cdc_keeps_deletes_and_lsn_and_feeds_a_cdc_model(tmp_path, captured):
    from havn.engine.streaming.cdc_logical import LogicalCDCConfig, LogicalCDCConsumer

    root = tmp_path
    (root / "project.yml").write_text("name: cdc\n", encoding="utf-8")
    model = root / "transform" / "bronze" / "orders.sql"
    model.parent.mkdir(parents=True)
    model.write_text(
        "@config materialized=incremental, live=true, incremental_strategy=merge, unique_key=id, "
        "cdc_op=op, cdc_seq=lsn, incremental_filter=WHERE _havn_seq > {watermark}\n"
        "SELECT CAST(payload->>'id' AS INTEGER) AS id, payload->>'status' AS status, op, lsn, _havn_seq\n"
        "FROM landing.orders\n",
        encoding="utf-8",
    )
    db = str(root / "warehouse.duckdb")
    consumer = LogicalCDCConsumer(
        LogicalCDCConfig(dsn="", slot_name="s", publication="p", tables=["public.orders"]),
        connection_factory=lambda: duckdb.connect(db),
    )
    consumer._handle(_change("I", "orders", {"id": 1, "status": "new"}, 100))
    consumer._handle(_change("INSERT", "public.orders", {"id": 2, "status": "new"}, 110))
    consumer._handle(_change("U", "orders", {"id": 1, "status": "paid"}, 120))
    consumer._handle(SimpleNamespace(op="T", table_name="orders"))  # truncate: ignored
    consumer._flush()
    consumer._handle(_change("D", "orders", {"id": 2}, 130))
    consumer._handle(_change("U", "orders", {"id": 1, "status": "new"}, 105))  # replayed, older
    consumer._flush()
    assert consumer.rows_flushed == 5
    conn = duckdb.connect(db)
    rows = conn.execute("SELECT op, lsn, _havn_seq FROM landing.orders ORDER BY _havn_seq").fetchall()
    assert rows == [("I", 100, 1), ("I", 110, 2), ("U", 120, 3), ("D", 130, 4), ("U", 105, 5)]
    assert [(e.source, e.watermark) for e in captured] == [("landing.orders", 3), ("landing.orders", 5)]
    run_transform(conn, root / "transform", project_dir=root)
    assert conn.execute("SELECT id, status, lsn FROM bronze.orders").fetchall() == [(1, "paid", 120)]


def test_lsn_text_form_is_ordered_numerically():
    from havn.engine.streaming.cdc_logical import _message_lsn

    a = _message_lsn(SimpleNamespace(lsn="16/B374D848"))
    b = _message_lsn(SimpleNamespace(lsn="17/00000001"))
    assert a < b
    assert _message_lsn(SimpleNamespace(transaction=SimpleNamespace(commit_lsn=42))) == 42


def test_api_poll_rows_advance_and_insert_by_name(tmp_path, captured):
    from havn.engine.live.sources import advance_source
    from havn.engine.streaming.api_poll import _upsert_rows

    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    assert _upsert_rows(conn, "landing", "events", [{"a": 1, "b": "x"}]) == 1
    advance_source(conn, "landing.events")
    # The landing table now has _havn_seq; a later poll with reordered keys
    # still lands each value in its own column.
    assert _upsert_rows(conn, "landing", "events", [{"b": "y", "a": 2}]) == 1
    advance_source(conn, "landing.events")
    assert conn.execute("SELECT a, b, _havn_seq FROM landing.events ORDER BY a").fetchall() == [
        (1, "x", 1), (2, "y", 2)
    ]


def test_api_poll_once_advances(tmp_path, captured):
    import http.server
    import json as _json

    from havn.engine.streaming.api_poll import APIPollConsumer

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = _json.dumps({"data": [{"id": 1}, {"id": 2}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        (tmp_path / "project.yml").write_text("name: p\n", encoding="utf-8")
        url = f"http://127.0.0.1:{server.server_address[1]}/items"
        result = APIPollConsumer("items", {"url": url, "json_path": "data"}, tmp_path).poll_once()
        assert result.error is None and result.rows_inserted == 2
    finally:
        server.shutdown()
    assert [(e.source, e.watermark) for e in captured] == [("landing.items", 2)]


def test_high_watermark_sync_advances(tmp_path, captured):
    from havn.engine.cdc import CDCTableConfig, sync_table_high_watermark

    src_path = tmp_path / "source.duckdb"
    src = duckdb.connect(str(src_path))
    src.execute("CREATE TABLE users (id INTEGER, updated_at TIMESTAMP)")
    src.execute("INSERT INTO users VALUES (1, '2024-01-01'), (2, '2024-01-02')")
    src.close()
    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    cfg = CDCTableConfig(name="users", cdc_mode="high_watermark", cdc_column="updated_at")
    first = sync_table_high_watermark(conn, "crm", cfg, "landing", str(src_path), source_type="duckdb")
    assert first.status == "success", first.error
    assert first.rows_synced == 2
    src = duckdb.connect(str(src_path))
    src.execute("INSERT INTO users VALUES (3, '2024-01-03')")
    src.close()
    second = sync_table_high_watermark(conn, "crm", cfg, "landing", str(src_path), source_type="duckdb")
    assert second.status == "success", second.error
    assert second.rows_synced == 1
    assert conn.execute("SELECT id, _havn_seq FROM landing.users ORDER BY id").fetchall() == [
        (1, 1), (2, 2), (3, 3)
    ]
    assert [e.watermark for e in captured] == [2, 3]


@pytest.mark.skipif(
    not os.environ.get("HAVN_TEST_PG_DSN"),
    reason="set HAVN_TEST_PG_DSN to a Postgres with wal_level=logical to run",
)
def test_postgres_logical_replication_end_to_end(tmp_path):  # pragma: no cover - needs Postgres
    """Real WAL: insert, update and delete in Postgres reach a live bronze model."""
    pytest.importorskip("psycopg")
    try:
        from havn.vendor import pypgoutput  # noqa: F401
    except ImportError:
        pytest.skip("pypgoutput is not vendored")
    import psycopg

    from havn.config import DatabaseConfig
    from havn.engine.backends import create_backend
    from havn.engine.live.runner import LiveRunner
    from havn.engine.live.settings import LiveSettings
    from havn.engine.streaming.cdc_logical import LogicalCDCConfig, build_consumer
    from havn.engine.write_queue import WriteQueue, cursor_for

    dsn = os.environ["HAVN_TEST_PG_DSN"]
    slot, pub = "havn_live_test", "havn_live_test_pub"
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("DROP TABLE IF EXISTS public.havn_live_orders")
        pg.execute("CREATE TABLE public.havn_live_orders (id INT PRIMARY KEY, status TEXT)")
        pg.execute(f"DROP PUBLICATION IF EXISTS {pub}")
        pg.execute(f"CREATE PUBLICATION {pub} FOR TABLE public.havn_live_orders")
        try:
            pg.execute(f"SELECT pg_drop_replication_slot('{slot}')")
        except Exception:
            pass

    (tmp_path / "project.yml").write_text("name: pg\n", encoding="utf-8")
    model = tmp_path / "transform" / "bronze" / "orders.sql"
    model.parent.mkdir(parents=True)
    model.write_text(
        "@config materialized=incremental, live=true, incremental_strategy=merge, unique_key=id, "
        "cdc_op=op, cdc_seq=lsn, incremental_filter=WHERE _havn_seq > {watermark}\n"
        "SELECT CAST(payload->>'id' AS INTEGER) AS id, payload->>'status' AS status, op, lsn, _havn_seq\n"
        "FROM landing.havn_live_orders\n",
        encoding="utf-8",
    )
    wq = WriteQueue(create_backend(DatabaseConfig(path="warehouse.duckdb"), project_dir=tmp_path))
    consumer = build_consumer(
        LogicalCDCConfig(dsn=dsn, slot_name=slot, publication=pub,
                         tables=["public.havn_live_orders"], flush_interval=0.5, flush_rows=1),
        connection_factory=lambda: cursor_for(wq.conn),
    )
    runner = LiveRunner(tmp_path, wq, settings=LiveSettings(min_interval=0.1, poll_interval=0.5))
    consumer.start()
    runner.start()
    try:
        with psycopg.connect(dsn, autocommit=True) as pg:
            pg.execute("INSERT INTO public.havn_live_orders VALUES (1, 'new'), (2, 'new')")
            pg.execute("UPDATE public.havn_live_orders SET status = 'paid' WHERE id = 1")
            pg.execute("DELETE FROM public.havn_live_orders WHERE id = 2")
        deadline = time.monotonic() + 60
        cur = wq.cursor()
        while time.monotonic() < deadline:
            try:
                if cur.execute("SELECT id, status FROM bronze.orders").fetchall() == [(1, "paid")]:
                    break
            except duckdb.CatalogException:
                pass
            time.sleep(0.5)
        assert cur.execute("SELECT id, status FROM bronze.orders").fetchall() == [(1, "paid")]
    finally:
        runner.stop()
        consumer.stop()
        wq.close()
