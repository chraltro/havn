# Telemetry: Prometheus, OpenTelemetry, OpenLineage

havn can export what it does to the tools you already run. Every exporter is
**off** until you turn it on under `telemetry:` in `project.yml`, and none of
them can fail a build: delivery problems are logged and dropped.

```yaml
telemetry:
  prometheus:
    enabled: true
    token: ${HAVN_METRICS_TOKEN}      # optional scrape token (keep it in .env)
    allow_unauthenticated: localhost  # false | localhost | true
    include_models: true              # per-model series
  opentelemetry:
    enabled: true
    endpoint: http://localhost:4318/v1/traces
    headers: {}
    service_name: havn
    trace_api: true                   # one span per API request
  openlineage:
    enabled: true
    transport: http                   # http | file
    url: http://localhost:5000        # Marquez
    endpoint: api/v1/lineage
    api_key: ${OPENLINEAGE_API_KEY}
    namespace: analytics              # job namespace (default havn://<project name>)
    column_lineage: true
```

## Prometheus: `GET /metrics`

Text exposition format, served by `havn serve`. No extra package is needed.

```yaml
# prometheus.yml
scrape_configs:
  - job_name: havn
    scrape_interval: 30s
    static_configs:
      - targets: ["havn-host:3000"]
    authorization:
      credentials: <the telemetry.prometheus.token>
```

**Process series** (since the server started):
`havn_query_duration_seconds` (histogram), `havn_transform_duration_seconds`
(histogram, `status="success"|"error"`), `havn_queries_total`,
`havn_rows_processed_total`, `havn_streaming_events_total`,
`havn_active_tasks`, `havn_warehouse_size_bytes`, resource budgets.

**Warehouse series** (read from `_havn` on each scrape, survive restarts):

| Metric | Type | Labels |
|---|---|---|
| `havn_runs_total` | counter | run_type, status |
| `havn_last_run_timestamp_seconds` | gauge | run_type |
| `havn_model_last_build_duration_seconds` | gauge | model, schema, materialized |
| `havn_model_rows` | gauge | model, schema |
| `havn_model_freshness_age_seconds` | gauge | model, schema |
| `havn_model_last_build_success` | gauge (1/0) | model, schema, status |
| `havn_model_build_failures_total` | counter | model, schema |
| `havn_assertion_failures_total` | counter | model, severity |
| `havn_assertions_failing` | gauge | model |
| `havn_model_peak_memory_bytes`, `havn_model_spill_bytes` | gauge | model |
| `havn_perf_regressions_total` | counter | model |
| `havn_job_runs_total` | counter | job, status |
| `havn_job_last_run_success` | gauge (1/0) | job, status, trigger |
| `havn_job_last_run_timestamp_seconds`, `havn_job_last_run_duration_seconds` | gauge | job |
| `havn_write_queue_depth`, `havn_write_queue_capacity`, `havn_read_pool_idle`, `havn_resource_tasks_active` | gauge | |

Set `include_models: false` for projects with thousands of models.

**Who may scrape**, in order: anyone when `allow_unauthenticated: true`; a
scraper on 127.0.0.1 / ::1 when it is `localhost`; whoever sends
`Authorization: Bearer <token>`; and, when havn runs with `--auth`, any user
token with read permission. With auth off and no token configured, `/metrics`
is as open as the rest of the server.

Setting the `HAVN_METRICS_TOKEN` environment variable alone still turns the
endpoint on with that token, as it did before the endpoint was configurable.

## OpenTelemetry traces

```bash
pip install 'havn[otel]'
```

One trace per pipeline run: a `havn transform` (or `havn job <name>`) span,
a span per step (`step transform`, or each job step), and a `model <name>`
span per build with `havn.rows`, `havn.duration_ms`, `havn.rows_scanned`,
`havn.peak_memory_bytes`, `havn.plan_captured` and an error status when the
build failed. Parallel workers parent their spans to the run correctly.

With `trace_api: true` each API request gets a server span named after its
route (`GET /api/perf/models/{model}`); an incoming W3C `traceparent` header
continues the caller's trace.

Spans go over OTLP/HTTP through a batch processor to `endpoint`. havn uses
its own tracer provider, so an application embedding havn keeps its global one.
Without the packages installed, or with `enabled: false`, all of this is a
no-op.

## OpenLineage

Each model build emits a `START` event before it runs and a `COMPLETE` or
`FAIL` event after, following the OpenLineage 2-0-2 spec, with no SDK
required:

- `job`: the model (`gold.orders`) in your namespace, with `sql` and `jobType`
  facets;
- `run`: a fresh run id per build, a `parent` facet pointing at the pipeline
  run (so Marquez groups a run's models), `processing_engine`, and
  `errorMessage` on `FAIL`;
- `inputs`: the model's upstreams;
- `outputs`: the model with a `schema` facet, an `outputStatistics` row count,
  and a `columnLineage` facet from havn's own column lineage.

Datasets are named `schema.table` in the namespace
`duckdb://<absolute path of the warehouse>` (override with
`dataset_namespace`).

`transport: http` POSTs JSON to `<url>/<endpoint>` with an optional bearer
`api_key`. `transport: file` appends one JSON event per line to `path`
(relative to the project). Delivery runs on a background thread; the end of a
run waits up to `timeout_s` + 1 second for queued events, so a CLI run does
not exit before they are sent.

See also the [performance advisor](performance.md).
