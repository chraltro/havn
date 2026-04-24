# Observability

## /metrics — Prometheus scrape

Text-exposition format. Scrape with Prometheus or VictoriaMetrics:

```yaml
scrape_configs:
  - job_name: havn
    scrape_interval: 15s
    static_configs:
      - targets: ["havn-host:3000"]
```

### Core series

| Metric                                   | Type        | Labels                 |
|------------------------------------------|-------------|------------------------|
| `havn_query_duration_seconds`            | histogram   | category, status       |
| `havn_transform_duration_seconds`        | histogram   | schema, status         |
| `havn_queries_total`                     | counter     | category, status       |
| `havn_rows_processed_total`              | counter     | category               |
| `havn_streaming_events_total`            | counter     | source, status         |
| `havn_active_tasks`                      | gauge       | category               |
| `havn_warehouse_size_bytes`              | gauge       | —                      |
| `havn_resource_budget_memory_bytes`      | gauge       | category               |
| `havn_resource_budget_threads`           | gauge       | category               |

## /health — liveness alias

Returns `{"status":"ok"}` without touching the warehouse. Use for Docker /
Kubernetes liveness probes; use `/api/health` (defined in the metrics
router) for a deeper DB-connectivity check.

## Structured logs

`HAVN_LOG_FORMAT=json` switches the root logger to newline-JSON with
`event`, `level`, `timestamp` keys. Useful when shipping logs to Loki /
Elasticsearch.

`HAVN_LOG_LEVEL` overrides the default `INFO` level.

## Legacy JSON endpoints (kept for the web UI)

- `GET /api/metrics` — aggregate JSON used by the Overview panel.
- `GET /api/metrics/models` — per-model stats.
- `GET /api/metrics/slow-queries` — recent slow-query log.
