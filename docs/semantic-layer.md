# Semantic Layer

Define a metric once in YAML, query it consistently everywhere — the CLI,
the API, dashboards, and AI agents via [MCP](mcp.md). No more five slightly
different definitions of "revenue" scattered across saved queries.

## Defining metrics

Metrics live in YAML files under `metrics/` in your project root:

```yaml
# metrics/revenue.yml
metrics:
  - name: revenue
    description: Total order revenue
    model: gold.orders            # table or view to aggregate over
    measure: SUM(amount)          # any SQL aggregate expression
    dimensions: [region, status]  # columns that may be grouped by
    time_dimension: order_date    # column used for time bucketing
    filters:
      - status != 'cancelled'     # always-applied WHERE predicates
```

| Key | Required | Meaning |
|-----|----------|---------|
| `name` | yes | Metric identifier (letters, digits, underscores) |
| `model` | yes | Schema-qualified table or view, e.g. `gold.orders` |
| `measure` | yes | SQL aggregate expression, e.g. `SUM(amount)`, `COUNT(DISTINCT user_id)` |
| `description` | no | Shown in listings |
| `dimensions` | no | Whitelist of columns callers may group by |
| `time_dimension` | no | Column used for `--grain` / `--start` / `--end` |
| `filters` | no | Predicates ANDed into every query |

One file can declare many metrics; many files are merged. Invalid
definitions are reported but don't break loading of the valid ones.

## Querying metrics

```bash
havn metrics                       # list all metrics
havn metrics sql revenue --by region --grain month     # show compiled SQL
havn metrics query revenue --by region --grain month   # run it
havn metrics query revenue --start 2026-01-01 --end 2026-02-01 --json
```

`havn metrics query` compiles the metric to a single `SELECT` and runs it
read-only — routed through a running `havn serve` when one holds the
warehouse lock, exactly like `havn query`.

The compiled SQL for the example above:

```sql
SELECT
    DATE_TRUNC('month', order_date) AS month,
    region,
    SUM(amount) AS revenue
FROM gold.orders
WHERE (status != 'cancelled')
GROUP BY 1, 2
ORDER BY 1, 2
```

## API

| Endpoint | Description |
|----------|-------------|
| `GET /api/semantic/metrics` | List metric definitions + load errors |
| `POST /api/semantic/compile` | Compile a metric query to SQL without running it |
| `POST /api/semantic/query` | Compile and execute |

`POST /api/semantic/query` body:

```json
{
  "metric": "revenue",
  "dimensions": ["region"],
  "grain": "month",
  "start": "2026-01-01",
  "end": "2026-07-01",
  "limit": 1000
}
```

Responses include the compiled `sql` alongside `columns` / `rows`, so it's
easy to inspect what actually ran. Masking policies apply to API metric
queries the same way they apply to `/api/query`.

## Safety

- Requested dimensions must be declared on the metric — anything else is
  rejected before SQL is generated.
- Grains are restricted to `hour`, `day`, `week`, `month`, `quarter`, `year`.
- `start` / `end` values are escaped string literals cast to `TIMESTAMP`.
- The compiled statement is run through the same read-only validation as
  `/api/query` (no mutations, no file-access functions), and through the
  masking rewriter when queried via the API.
