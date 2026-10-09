# Live models

A live model refreshes within seconds of new data landing from a streaming
source (webhook, Postgres CDC, API poll), instead of waiting for the next
`havn transform` or job run. A chain `bronze -> silver -> gold` of live models
moves in one refresh cycle, in DAG order.

```sql
@config materialized=incremental, live=true, incremental_strategy=merge,
        unique_key=order_id, incremental_filter=WHERE _havn_seq > {watermark}

SELECT order_id, amount, region, _havn_seq FROM landing.orders
```

## Opting in

| `@config` key    | Meaning |
|------------------|---------|
| `live=true`      | Refresh this model whenever an input advances. |
| `live_interval=` | Least time between two refreshes of this model (`10s`, `5m`). Default: as often as the runner cycles. |

A live model must be `materialized=incremental` (`merge`, `delete+insert` or
`append`; not `microbatch`) or a `view`. Views are always live: they read
their inputs at query time and are never refreshed, only created. A live
`table`, `snapshot` or `ephemeral` is a validation error, as is a live `append`
model without an `incremental_filter` (it would append its whole input on
every refresh). `havn validate` reports all of these.

`live` is not part of the change-detection hash: turning a model live does not
rebuild it.

## Sources, sequences and watermarks

Every landing table a live model reads incrementally carries `_havn_seq
BIGINT`, assigned by havn when a batch is committed, increasing in commit
order. The built-in streaming ingest stamps and announces every commit:

- webhook flush (`POST /api/ingest/webhook/{source}` -> `landing.<source>`),
- Postgres logical-replication CDC (`engine/streaming/cdc_logical.py`),
- API poll consumers (`havn poll`, `/api/streaming/pollers/...`),
- connector high-watermark syncs (`engine/cdc.py`).

Any other writer (an ingest script, an external tool) announces its rows the
same way:

```python
db.execute("INSERT INTO landing.orders (order_id, amount) SELECT ...")
from havn.engine.live import advance_source
advance_source(db, "landing.orders")
```

or `havn live advance landing.orders`, or `POST /api/live/sources/landing.orders/advance`.

Once a landing table has `_havn_seq`, insert into it with a column list or
`INSERT ... BY NAME`; a positional `INSERT ... VALUES` no longer lines up.

Rows committed but not yet stamped have a NULL sequence, so they are invisible
to `WHERE _havn_seq > {watermark}` until stamped. Nothing is read twice.

### Placeholders

- `{watermark}`: the sequence this model has already applied from its single
  live input. A model with several live inputs names one:
  `{watermark:landing.orders}`.
- `{this}` keeps working as before.

Placeholders may appear in `incremental_filter` and in the query body (useful
for "recompute the groups that changed", see below). On the first build the
watermark is 0 and the filter still applies.

### What "inputs" are

Walking a live model's dependencies: a raw table (`landing.*`) is a tracked
input; a live incremental model is a tracked input (it publishes its own
watermark); views and ephemerals are looked through. Any other model (a batch
table, a snapshot, a non-live incremental) is read but does not trigger a
refresh; `havn validate` warns about it.

A live model publishes its own watermark after each refresh. If it passes
`_havn_seq` through, the watermark is the highest sequence in its table, so a
downstream `WHERE _havn_seq > {watermark}` reads exactly the rows that
changed. Without the column the watermark is a refresh counter: downstream
still refreshes, but has to find the change itself.

Aggregates downstream recompute the affected keys:

```sql
@config materialized=incremental, live=true, incremental_strategy=delete+insert, unique_key=region
SELECT region, COUNT(*) AS orders, SUM(amount) AS revenue
FROM silver.orders
WHERE region IN (SELECT region FROM silver.orders WHERE _havn_seq > {watermark})
GROUP BY region
```

## CDC: inserts, updates and deletes

An incremental `merge` or `delete+insert` model with a `unique_key` applies
change events instead of rows when it sets:

| key            | meaning |
|----------------|---------|
| `cdc_op=`      | column holding the operation: `I`/`U`/`D` (also `insert`/`update`/`delete`, Debezium `c`/`r`/`d`). Anything starting with `d` is a delete. |
| `cdc_seq=`     | column that orders changes to one key: an LSN, a sequence, an ISO timestamp. |
| `cdc_deletes=` | `hard` (default) or `soft`. |

The CDC landing convention (written by the logical-replication consumer) is
`op VARCHAR, lsn BIGINT, received_at TIMESTAMP, payload JSON, _havn_seq BIGINT`;
a delete's `payload` is the old key.

```sql
@config materialized=incremental, live=true, incremental_strategy=merge, unique_key=id,
        cdc_op=op, cdc_seq=lsn, incremental_filter=WHERE _havn_seq > {watermark}
SELECT CAST(payload->>'id' AS INTEGER) AS id, payload->>'status' AS status, op, lsn, _havn_seq
FROM landing.orders
```

Each run keeps, per key, the event with the highest `cdc_seq` in the batch
(a delete wins a tie), drops events not newer than what the table already
holds for that key, and replaces each remaining key. So duplicates, replays
and late older events are no-ops.

- `hard`: a deleted key's row is removed and `(key, cdc_seq)` is kept in
  `_havn.cdc_tombstones__<schema>__<name>`, so an older insert replayed after
  the delete cannot bring the row back. A later, newer insert does.
- `soft`: the row stays with `_havn_deleted = true`. Use this when a live
  model downstream must see deletes (a hard delete removes the row, so the
  next model never reads it); filter `WHERE NOT _havn_deleted` there.

## The runner

`havn serve` starts the live runner when the project has a live model (unless
`live.enabled: false`). Without a server, `havn live` runs it in the
foreground with a status table (`havn live --once` runs one cycle and exits).

It wakes on each advance, waits for the burst to settle, then refreshes every
live model that is behind, in DAG order:

- **Coalescing.** A batch closes after `debounce` seconds without a new
  advance, or `max_latency` seconds after it opened; cycles never start closer
  than `min_interval` apart. A thousand small commits become a handful of
  refreshes. It also re-reads source watermarks every `poll_interval`, so a
  missed event or another process's write is picked up.
- **One transaction per refresh.** The model's data, the watermarks it
  consumed, its own published watermark and `model_state` commit together.
- **Single writer.** Refreshes run through the write queue (the server's, or
  the one `havn live` opens), on a cursor of its write connection.
- **Batch runs coexist.** Every build of a live model, by `havn transform`, a
  job or the runner, does the same watermark bookkeeping under a per-model
  build lock. Whoever gets there first applies the pending batch; the other
  finds nothing left. The runner never waits for the lock: if a batch run is
  building the model it looks again next cycle. Across processes (DuckLake
  with a Postgres catalog) two builders collide on the consumed-watermark row
  and one fails rather than applying twice.
- **Failures.** A refresh that errors, or whose error-severity `@assert`
  fails, is rolled back, so the bad batch never becomes visible. The model is
  marked failing and retried with exponential backoff (`backoff_base`
  doubling up to `backoff_max`). An alert (`live_model_failed`) goes out on the
  first failure and `live_model_recovered` when it refreshes again. Live models
  downstream wait; sources keep landing, and the model catches up on
  everything queued once it recovers. `havn live resume` or *Retry now* skips
  the backoff.
- **Assertions** run inside the refresh transaction, on every refresh by
  default (`assertion_interval: 0`). For a large model, space them out with
  `assertion_interval`; a batch run always checks. **Profiling** (a full scan)
  runs at most every `profile_interval`. Anomaly detection runs on batch runs
  only.
- **Logging.** Refreshes do not write one `run_log` row each. Every
  `log_interval` each live model gets one `run_type='live'` row with the
  refresh count, events applied and average/maximum lag. Failures, pause,
  resume and recovery are logged as they happen.

Non-live models downstream of live ones are not touched by the runner; they
refresh on batch runs as before (a table rebuilds when its live input has been
refreshed since its own last build).

## Lag and freshness

Lag is end-to-end: how long ago the oldest source data a model has not yet
applied arrived in landing; zero when it is caught up. It is carried across
hops, so gold's lag counts from when the landing row arrived, not from when
silver refreshed. `check_freshness` (and `havn check`) judges a live model by
lag: caught up is fresh no matter when it last ran; lag above `live.max_lag`
is stale.

## Visibility and control

- **Observe > Live**: each live model's status (live, behind, waiting,
  failing, paused), lag, events/s, last refresh, refresh count, the last error
  and retry countdown; sources with their watermarks; a live activity feed
  (SSE). Pause, resume, retry now, start/stop runner.
- **DAG**: live models show their lag or state on the node.
- **CLI**: `havn live status [--json]`, `havn live pause|resume MODEL`,
  `havn live advance SOURCE`. They go through a running server when there is
  one.
- **API**: `GET /api/live/status`, `GET /api/live/events` (SSE),
  `POST /api/live/start|stop`, `POST /api/live/models/{model}/pause|resume|refresh`,
  `POST /api/live/sources/{source}/advance`.

## Settings

```yaml
live:
  enabled: true            # start the runner in havn serve
  min_interval: 1s         # least time between two refresh cycles
  debounce: 200ms          # quiet time that closes a batch early
  max_latency: 5s          # longest a batch is held open
  poll_interval: 5s        # re-read source watermarks this often
  log_interval: 60s        # one aggregated run_log row per model per interval
  backoff_base: 2s         # first retry delay after a failure
  backoff_max: 300s        # longest retry delay
  assertion_interval: 0    # 0 = check @assert on every refresh
  profile_interval: 300s   # profile a live model at most this often
  max_lag: 300s            # freshness: stale above this lag
```

## State

All in the warehouse (`_havn`): `live_sources` (watermark per source and live
model), `live_advances` (one row per commit, pruned once consumed),
`live_consumed` (per model and input), `live_state` (pause, failures, counters).

## Limits

- Full-refresh loads (a source table replaced wholesale) are not live
  sources: they have no ordering a watermark could follow.
- The in-process event bus wakes the runner instantly; writes from another
  process are seen at the next `poll_interval`.
- Turning an existing `append` model live re-reads its whole input on the
  first refresh (it has no consumed watermark yet). `merge` and `delete+insert`
  absorb that; for `append`, drop the table first.
- Hard CDC tombstones are kept indefinitely (one row per deleted key).
- The Postgres logical-replication consumer needs the vendored `pypgoutput`
  (see `src/havn/vendor/pypgoutput`).
