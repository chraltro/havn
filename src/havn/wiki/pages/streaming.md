# Streaming ingestion

havn handles three shapes of streaming input without the operational weight
of Kafka or Debezium:

| Shape     | Entry point                                 | Use it for                    |
|-----------|---------------------------------------------|-------------------------------|
| Webhook   | `POST /api/ingest/webhook/{source}`         | External services that POST   |
| CDC (WAL) | `engine/streaming/cdc_logical.py`           | Postgres sources, sub-second  |
| Poll      | Existing `engine/cdc.py` high-watermark mode | REST APIs without webhooks   |

## Webhook path

Every POST lands in a single staging table (`_havn.webhook_staging`), and a
background worker drains it every ~15 seconds into `landing.<source>`. This
lets havn batch inserts (which matters for DuckLake inlining) rather than
one DuckDB write per request.

```bash
curl -X POST http://localhost:3000/api/ingest/webhook/orders \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer $HAVN_TOKEN' \
  -d '{"id": 42, "total": 19.99}'
# → 202 {"status": "staged", "source": "orders"}
```

Support endpoints:

- `GET  /api/streaming/webhook/status` — backlog + worker stats.
- `POST /api/streaming/webhook/flush`  — manual flush, returns rows flushed.

## Logical-replication CDC

Vendored `pypgoutput` decodes Postgres WAL events. Configure a consumer per
source:

```python
from havn.engine.streaming.cdc_logical import (
    LogicalCDCConfig,
    build_consumer,
)

cfg = LogicalCDCConfig(
    dsn="postgres://repl:secret@db.internal/prod",
    slot_name="havn_prod",
    publication="havn_pub",
    tables=["public.orders", "public.customers"],
    flush_interval=10.0,
    flush_rows=50,
)
consumer = build_consumer(cfg, connection_factory=lambda: backend.connect())
consumer.start()
```

Events are buffered per table; the consumer flushes every 10 seconds or at
50 rows, whichever comes first. The `pypgoutput` vendor is a placeholder in
core — follow `src/havn/vendor/pypgoutput/__init__.py` for the drop-in
steps.

## DuckLake maintenance

When the backend is DuckLake the server's lifespan starts a
`MaintenanceScheduler` that periodically:

- Flushes inlined rows to Parquet (every 10 minutes).
- Merges adjacent small files (every 30 minutes).
- Checkpoints the catalog and expires snapshots older than 7 days (daily).

On a DuckDB-backed warehouse the scheduler is a no-op.

## Observability

Every flush increments `havn_streaming_events_total{source, status}` and
`havn_rows_processed_total{category="streaming"}` — scrape them from
`/metrics`.
