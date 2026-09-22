# Data Quality

havn provides a comprehensive data quality framework with three complementary systems: inline assertions in SQL models, automatic profiling, and freshness monitoring. For standalone quality rules, see [Contracts](contracts).

All three check the data that is currently in the warehouse. To check the SQL itself against fixed input rows, with no warehouse involved, see [Unit Tests](unit-tests).

## Inline Assertions

Add `@assert` directives to SQL model files. Assertions are evaluated after each model builds during `havn transform`:

```sql
@config materialized=table, schema=gold
@assert row_count > 0
@assert unique(customer_id)
@assert no_nulls(customer_id)
@assert no_nulls(email)
@assert accepted_values(status, ['active', 'inactive', 'suspended'])
@assert "lifetime_value >= 0"

SELECT
    customer_id,
    email,
    status,
    SUM(order_total) AS lifetime_value
FROM silver.customers
GROUP BY 1, 2, 3
```

(Legacy `-- assert: ...` comment syntax still parses for back-compat.)

### Available Assertion Types

#### `row_count > N`

Checks that the table has more than N rows:

```sql
@assert row_count > 0
@assert row_count > 100
@assert row_count >= 1000
```

#### `unique(column)`

Checks that a column contains no duplicate values:

```sql
@assert unique(customer_id)
@assert unique(email)
```

#### `no_nulls(column)`

Checks that a column contains no NULL values:

```sql
@assert no_nulls(customer_id)
@assert no_nulls(email)
```

#### `accepted_values(column, [values])`

Checks that all values in a column are within the allowed set:

```sql
@assert accepted_values(status, ['active', 'inactive', 'suspended'])
@assert accepted_values(country_code, ['US', 'CA', 'GB', 'DE'])
```

#### Custom SQL Expressions

Any boolean SQL expression can be used as an assertion. Wrap complex expressions in quotes:

```sql
@assert "AVG(amount) > 0"
@assert "MAX(created_at) > CURRENT_DATE - INTERVAL '7 days'"
@assert "COUNT(DISTINCT region) > 1"
```

Custom expressions are evaluated as `SELECT (<expression>) FROM <table>` and must return a single truthy value.

### Assertion Behavior

- Assertions run **after** a model is built (after `CREATE OR REPLACE TABLE`)
- If an assertion **fails**, the model status is set to `assertion_failed`
- The model data is **not rolled back** -- the table exists but is flagged
- All assertions for a model are evaluated (not short-circuited)
- Results are stored in `_havn.assertion_results`

### Assertion Debugging

When an assertion fails, havn provides diagnostic details to help you find the problem:

- **`unique(col)` failure** -- shows top 10 duplicated values with counts
- **`no_nulls(col)` failure** -- shows null count, percentage, and sample rows with NULLs
- **`accepted_values(col, [...])` failure** -- shows unexpected values with counts
- **`row_count` failure** -- shows actual count vs threshold

To re-run assertions with full diagnostics on demand:

```bash
curl http://localhost:3000/api/quality/assertion-debug/gold.customer_summary
```

In the web UI, click **"Re-run with diagnostics"** on any failed assertion in the Observe → Quality panel to see detailed debugging output.

### Viewing Assertion Results

```bash
havn assertions
```

Shows recent assertion results with pass/fail status and details.

Via API:

```bash
# All assertions
curl http://localhost:3000/api/assertions

# For a specific model
curl http://localhost:3000/api/assertions/gold.customer_summary
```

## Data Profiling

havn automatically computes column-level statistics for every model after it builds. Profiles include:

- **Row count** -- Total number of rows
- **Column count** -- Number of columns
- **Null percentages** -- Percentage of NULL values per column
- **Distinct counts** -- Number of distinct values per column

### Viewing Profiles

```bash
# Summary of all models
havn profile

# Detailed profile for one model
havn profile gold.earthquake_summary
```

The detailed view shows per-column statistics:

```
gold.earthquake_summary  (1,234 rows, 8 columns)
  Profiled at: 2025-01-15 06:00:12

  Column         Null %   Distinct   Status
  region             0%        42   ok
  magnitude          0%       156   ok
  location          12%       891   has nulls
  depth              0%       734   ok
```

### Profile via API

```bash
# All profiles
curl http://localhost:3000/api/profiles

# Specific model
curl http://localhost:3000/api/profiles/gold.earthquake_summary
```

### Table-Level Profiling

The table browser in the web UI provides interactive profiling for any table:

```bash
curl http://localhost:3000/api/tables/gold/earthquake_summary/profile
```

Returns detailed statistics including min/max values, averages (for numeric columns), and sample values.

## Freshness Monitoring

Freshness monitoring detects models that have not been rebuilt within a specified time window.

### Check Model Freshness

```bash
havn freshness --hours 24
```

Shows all models with their last run time and whether they are stale (not rebuilt within 24 hours).

### Check Source Freshness

```bash
havn freshness --sources
```

Checks source freshness against SLAs declared in `project.yml`. See [Sources](sources).

### Freshness Alerts

Send alerts for stale models:

```bash
havn freshness --hours 24 --alert
```

This sends notifications via Slack or webhook (requires alert configuration in `project.yml`).

### Freshness via API

```bash
curl "http://localhost:3000/api/freshness?max_hours=24"
```

## Validation and type resolution

`havn validate` runs a **bind pass** over your models: every model's SQL is
handed to DuckDB's binder against a throwaway in-memory catalog, which
resolves names and types without reading a single row.

```bash
havn validate              # bind pass on, once a warehouse exists
havn validate --no-bind    # structure and DAG checks only
havn validate --bind       # ask for it explicitly
```

The pass creates each model as a view in dependency order inside the shadow
catalog, using the SQL in your file. Nothing is written to the warehouse, and
nothing is built. Three consequences are worth knowing:

- **Unbuilt upstreams are typed.** A model that has never run still resolves,
  because the shadow has its definition. On a fresh warehouse a bad column on
  an unbuilt upstream is caught, where the name-level check used to skip it.
- **Stale tables do not win.** If a model's built table has an old shape, the
  fresh definition in the shadow shadows it. Validation reflects the file, not
  the last build.
- **Macros and extensions are present.** Your Python `@macro` UDFs and any
  loaded extensions resolve inside the shadow, so they are not reported as
  unknown functions.

### How the shadow is isolated

The shadow is a private `:memory:` DuckDB database of its own, opened for the
call and closed at the end of it. It is never attached to the warehouse, and
no model SQL ever runs against a warehouse connection. The base objects your
models read (landing tables, seeds, sources, models outside the bound chain)
are recreated in it as **empty** tables with the real column types, read from
`information_schema`; an empty table binds exactly like a populated one, so no
warehouse row is ever copied in. The warehouse connection is only a catalog
source, and a read-only one is enough.

Before a single line of model SQL runs, the shadow loads its extensions,
registers your macros, and then turns `enable_external_access` off and locks
its configuration. From that point `read_csv`, `read_text`, `glob`, a bare
`FROM '<path>'`, `COPY ... TO`, `ATTACH`, `INSTALL`/`LOAD` and any attempt to
re-enable the switch all fail inside the shadow. That matters because
`POST /api/bind` takes an unsaved buffer from anyone with **read** permission:
the buffer is validated as a read-only single statement first, by the same
check `/api/query` uses, and is never bound at all if it fails. So a bind
request cannot write to the warehouse, cannot read or write a server file, and
cannot be a way to run SQL.

The cost is that a model which legitimately reads a file, such as
`read_parquet('data/x.parquet')`, cannot be bound. Those are reported as a
warning naming the model -- *file functions are not available in the bind
pass; this model is skipped* -- and the model and anything downstream of it are
skipped rather than reported as errors.

Findings are reported as **bind errors**, with a line number:

```
  error gold.summary:4: bind error: Referenced column "no_such_column" not found in FROM clause!
```

### What the bind pass catches

| Problem | Example |
|---|---|
| Wrong number of arguments | `date_trunc(event_ts)` |
| Unknown function | `no_such_fn(x)` |
| Operator overload failure | `customer + 1` where `customer` is `VARCHAR` |
| Missing column | `SELECT no_such_column FROM silver.orders` |
| Missing column on an upstream that was never built | same, with `silver.orders` unbuilt |
| Missing struct key | `payload.zzz` where `payload` is `STRUCT(a INTEGER)` |
| Ambiguous reference | `SELECT customer FROM a, b` with `customer` in both |
| Aggregation without GROUP BY | `SELECT customer, SUM(amount) FROM ...` |
| Set-operation arity mismatch | `SELECT a UNION ALL SELECT a, b` |

It also gives you the model's inferred output columns and their types, which
is what the editor's hover and the `/api/bind` endpoint return.

### What it does not catch

The binder decides whether an expression *can* be evaluated, not whether the
data in it *will* evaluate. Value conversions bind clean and fail at run time:

```sql
-- binds fine, returns INTEGER; fails on the first row that is not a number
SELECT CAST(customer AS INTEGER) AS id FROM landing.orders
```

The same applies to comparing a numeric column against a string constant and
to joining on columns whose types are convertible but whose values are not.
These are the cases a bind pass cannot reach on any engine: the value is not
known until the query runs. A clean bind is not a guarantee that the build
will succeed.

### Where the bind pass runs

- `havn validate`, as above.
- The pre-build gate in the web UI. A pipeline that would build a model with a
  bind error stops before building anything. When the run also includes ingest
  steps, the gate runs after ingest and before the first transform, so the
  landing tables it needs are already there.
- `POST /api/bind`, which the editor calls on a debounce for live markers.
  Read permission, and the buffer goes through the read-only validator first;
  a rejected buffer comes back as HTTP 200 with `"ok": false` and one error
  carrying the reason.
- The `bind_model` MCP tool, for agents editing SQL. SQL supplied to the tool
  goes through the same validator; binding a model by name does not, because
  that is the project's own file.

`havn validate --bind` skips the read-only validator: that is a local, trusted
user binding files they wrote, which may legitimately read a parquet file. It
still gets the isolated, locked-down shadow.

## Combined Validation

The `havn check` command runs all quality checks in one pass:

```bash
havn check
```

This executes:

1. **Model validation** -- SQL syntax, dependency resolution, column references
2. **Inline assertions** -- `@assert` directives against live data
3. **YAML contracts** -- Rules from `contracts/` directory
4. **Unit tests** -- Fixtures from `tests/unit/`, run in memory (`--no-unit-tests` to skip)

### CI/CD Integration

Use `havn check` in your CI/CD pipeline:

```bash
havn check --env test
```

Exit code 1 if any validation or assertion fails, making it suitable for automated quality gates.

## Alerts

havn supports alerting for pipeline events:

### Configuration

```yaml
alerts:
  channels:
    - slack
    - webhook
  slack_webhook_url: ${SLACK_WEBHOOK_URL}
  webhook_url: "https://alerts.internal/havn"
  on_success: false
  on_failure: true
```

### Alert Types

- **Pipeline failure** -- Sent when a transform or stream fails
- **Assertion failure** -- Sent when data quality assertions fail
- **Stale data** -- Sent when models exceed freshness thresholds

### Test Alerts

```bash
curl -X POST http://localhost:3000/api/alerts/test \
  -H "Content-Type: application/json" \
  -d '{"channel": "slack", "slack_webhook_url": "https://hooks.slack.com/..."}'
```

### Alert History

```bash
curl http://localhost:3000/api/alerts
```

## Anomaly Detection

havn tracks profile statistics over time and automatically detects anomalies using Z-score analysis. When the current run's metrics deviate significantly from the historical baseline, an anomaly is flagged.

### How It Works

After each transform run, havn compares the current profile (row_count, null percentages, distinct counts) against the last N runs (default 30). If the Z-score exceeds the threshold (default 2.0), an anomaly is logged.

### Configuration

```yaml
quality:
  anomaly_detection:
    enabled: true
    lookback: 30          # Number of historical runs to compare
    threshold: 2.0        # Z-score threshold for flagging
    notify: [slack]       # Alert channels
```

### Viewing Anomalies

```bash
# Recent anomalies
curl http://localhost:3000/api/anomalies

# Anomalies for a specific model
curl http://localhost:3000/api/anomalies/gold.customer_summary
```

In the web UI, anomalies appear in the Observe → Quality panel with severity coloring, showing the current value vs expected range.

### Edge Cases

- Fewer than 3 historical profiles → skipped (not enough baseline data)
- Zero standard deviation → skipped (constant value, no deviation possible)
- First run → skipped (no baseline)

## Related Pages

- [Contracts](contracts) -- Standalone YAML data quality rules
- [Unit Tests](unit-tests) -- Fixed input rows in, expected rows out, no warehouse
- [Transforms](transforms) -- Adding assertions to SQL models
- [Sources](sources) -- Source freshness SLAs
- [Lineage](lineage) -- Understanding data dependencies
- [CLI Reference](cli-reference) -- Quality-related commands
