# SQL API

Programmatic access to the warehouse over HTTP, in a shape familiar to
Databricks and Snowflake users.

## POST /v1/sql — execute a statement

```bash
curl -X POST http://localhost:3000/v1/sql \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer $HAVN_TOKEN' \
  -d '{"sql": "SELECT * FROM gold.orders LIMIT 10"}'
```

Fast queries return inline:

```json
{
  "statement_id": "...",
  "status": "succeeded",
  "columns": ["order_id", "customer_id", "total"],
  "rows": [[1, 42, 9.99], [2, 17, 19.99]],
  "row_count": 2
}
```

Slow queries (default threshold 10 seconds) return `202`:

```json
{"statement_id": "c7f...", "status": "running"}
```

Override the sync window with `"wait_seconds": 30`.

## GET /v1/sql/{statement_id}

Status + metadata (no rows inline).

## GET /v1/sql/{statement_id}/result

Full result body. Accept-header negotiation:

| Accept                                  | Response body              |
|-----------------------------------------|----------------------------|
| `application/json` (default)            | Envelope with `columns` + `rows` |
| `application/x-ndjson`                  | One JSON row per line      |
| `application/vnd.apache.arrow.stream`   | Arrow IPC stream (binary)  |

## DELETE /v1/sql/{statement_id}

Cancels via the Resource Manager, which calls `conn.interrupt()`.

## Auth

Uses the same token system as the web UI. Requires the `execute`
permission for `POST` / `DELETE`, `read` for `GET`.

## Resource governance

Every statement acquires a slot from the `query` category. Runaway
queries hit the category's timeout (set per user role via
`engine/query_governor.py`) and are interrupted automatically.

## Notes

- Results are cached in memory for 1 hour (`RESULT_TTL_SECONDS`) and
  garbage-collected on each subsequent `POST`.
- Rows returned inline are capped at 10,000 (`MAX_INLINE_ROWS`). Streaming
  paths (`/result` with NDJSON or Arrow) have no cap.
- DDL / DML statements (no result set) succeed with `row_count: 0` and
  empty `columns`.
