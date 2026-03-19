# Pipelines

Pipelines in havn are called **streams**. A stream is an ordered sequence of steps (ingest, seed, transform, export) defined in `project.yml`. Streams provide a single command to run your entire data pipeline or any subset of it, with real-time streaming output, retry logic, and webhook notifications.

## Web UI Experience

### Running Streams from the UI

1. In the **Develop** tab, use the **Run menu** dropdown in the toolbar
2. Select a stream name (e.g., "full-refresh") to start it
3. The **Output Panel** shows real-time streaming results as each step executes
4. Each step displays numbered progress messages with status indicators:
   - **start** -- Step is beginning execution
   - **done** -- Step completed successfully (with row count and duration)
   - **skip** -- Step was skipped (no changes detected)
   - **fail** -- Step failed (with error message)

### Streaming Pipeline Output (SSE)

The web UI uses Server-Sent Events (SSE) to stream pipeline execution in real time:

```bash
GET /api/stream/{stream_name}/events
```

Each event includes the step type, target, status, row count, duration, and any error messages. The UI renders these as a numbered list with color-coded status badges.

### Cancelling a Running Pipeline

Click the **Cancel** button in the Output Panel toolbar, or via API:

```bash
POST /api/stream/cancel
```

### Overview Tab Pipeline Health

The **Overview** tab displays:

- **Pipeline Health** -- Recent pipeline runs with status, affected table/file, row counts, and duration
- **Failed Runs Detail** -- Click the stats card to expand a list of recent failures with error messages

## Defining Streams

Streams are configured in `project.yml` under the `streams:` key:

```yaml
streams:
  full-refresh:
    description: "Full pipeline rebuild"
    steps:
      - seed: [all]
      - ingest: [all]
      - transform: [all]
      - export: [all]

  daily-etl:
    description: "Daily incremental ETL"
    schedule: "0 6 * * *"
    steps:
      - ingest: [all]
      - transform: [all]

  export-only:
    description: "Re-export without rebuilding"
    steps:
      - export: [all]
```

## Stream Steps

Each step specifies an **action** and a list of **targets**:

### Ingest

Runs Python scripts (`.py`) and notebooks (`.dpnb`) from the `ingest/` directory:

```yaml
- ingest: [all]                    # Run all ingest scripts
- ingest: [customers, orders]     # Run specific scripts
```

Scripts prefixed with `_` (e.g., `_helpers.py`) are skipped. The `db` DuckDB connection is pre-injected into each script.

### Seed

Loads CSV files from the `seeds/` directory into DuckDB tables:

```yaml
- seed: [all]                     # Load all seeds
```

Seeds use change detection -- only modified CSVs are reloaded. See [Seeds](seeds).

### Transform

Builds SQL models from the `transform/` directory in dependency order:

```yaml
- transform: [all]               # Build all models
- transform: [gold.summary]      # Build specific models
```

Uses SHA256 change detection to skip unchanged models. See [Transforms](transforms).

### Export

Runs Python scripts from the `export/` directory:

```yaml
- export: [all]                   # Run all export scripts
- export: [daily_report]         # Run specific scripts
```

## Running Streams

### Basic Execution

```bash
havn stream full-refresh
```

Executes each step in order. If any ingest step fails, the pipeline stops immediately to preserve data integrity.

### Force Rebuild

```bash
havn stream full-refresh --force
```

Forces all transform models to rebuild regardless of change detection.

### With Environment

```bash
havn stream daily-etl --env prod
```

Uses the production database and environment settings.

## Error Handling

Streams have built-in error handling:

- **Ingest failures stop the pipeline** -- If an ingest script fails, subsequent steps (transform, export) are not executed. This prevents building models on incomplete data.
- **Transform failures are reported** -- Failed models are logged but other independent models continue (parallel execution).
- **Export failures are logged** -- Export errors do not affect upstream data.

### Retry Support

Streams support automatic retries for transient failures:

```yaml
streams:
  daily-etl:
    description: "Daily ETL with retries"
    retries: 3
    retry_delay: 10
    steps:
      - ingest: [all]
      - transform: [all]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `retries` | int | `0` | Number of retry attempts per failed step |
| `retry_delay` | int | `5` | Seconds to wait between retries |

### Webhook Notifications

Get notified when a stream completes or fails:

```yaml
streams:
  daily-etl:
    webhook_url: "https://hooks.slack.com/services/T.../B.../..."
    steps:
      - ingest: [all]
      - transform: [all]
```

The webhook receives a JSON POST with:

```json
{
  "stream": "daily-etl",
  "status": "success",
  "duration_seconds": 12.3,
  "timestamp": "2025-01-15T06:00:00"
}
```

## Pipeline Integration with Rewind

When Rewind is enabled, every pipeline run automatically creates **snapshots** of your data. This means you can:

- Browse historical pipeline runs in the DAG Rewind timeline
- Compare row counts and schema changes across runs
- Restore any model to a previous pipeline run's state

See [Versioning](versioning) for Rewind details.

## Pipeline Integration with Sentinel

When Sentinel is enabled, schema checks can run before pipeline execution to detect breaking upstream changes. Configure this behavior:

```yaml
sentinel:
  enabled: true
  on_change: warn    # warn | error | ignore
```

- `warn` -- Log warnings but continue the pipeline
- `error` -- Halt the pipeline if breaking schema changes are detected
- `ignore` -- Skip schema checks entirely

See [Sentinel](sentinel) for details.

## Scheduling

Streams can be scheduled with cron expressions:

```yaml
streams:
  daily-etl:
    schedule: "0 6 * * *"    # 6 AM daily
    steps:
      - ingest: [all]
      - transform: [all]
```

Start the scheduler:

```bash
havn schedule
```

See [Scheduler](scheduler) for the full cron reference.

## Python Ingest Scripts

Ingest scripts are plain Python files. A DuckDB connection is pre-injected as `db`:

```python
# ingest/customers.py
import requests

response = requests.get("https://api.example.com/customers")
data = response.json()

db.execute("CREATE SCHEMA IF NOT EXISTS landing")
db.execute("CREATE OR REPLACE TABLE landing.customers AS SELECT * FROM ?", [data])
```

### Legacy Format

The older `def run(db)` function format is still supported for backward compatibility:

```python
def run(db):
    db.execute("CREATE SCHEMA IF NOT EXISTS landing")
    db.execute("CREATE OR REPLACE TABLE landing.data AS SELECT 1")
```

### Script Execution

- Scripts run as top-level code with `db` available in the namespace
- `stdout` and `stderr` are captured and logged
- Scripts prefixed with `_` are skipped
- Script output is masked to prevent leaking secrets from `.env`

## Running Individual Steps

You can run steps independently without a stream:

```bash
havn run ingest/customers.py       # Run a single script
havn run ingest/earthquakes.dpnb   # Run a notebook
havn seed                          # Load all seeds
havn transform                     # Build all models
havn run export/daily_report.py    # Run a single export
```

## Pipeline Monitoring

### Run History

```bash
havn history
```

Shows all pipeline runs with type, target, status, duration, and row counts.

### History in the Web UI

Go to the **Observe** tab and click **History** to see all recent pipeline runs sorted by timestamp. Each run shows:
- Run type (ingest, seed, transform, export)
- Affected target (table name or file)
- Status (success or failure with error message)
- Duration and rows affected
- Timestamp

### Freshness

```bash
havn freshness --hours 24
```

Checks which models were last built more than 24 hours ago.

### Project Status

```bash
havn status
```

Shows project health: git info, warehouse stats, and last run status.

## API Reference

### Run a Stream

```bash
POST /api/stream/{stream_name}
```

Optional query param: `?force=true`

### Stream Events (SSE)

```bash
GET /api/stream/{stream_name}/events
```

Real-time Server-Sent Events for pipeline execution progress.

### Cancel a Stream

```bash
POST /api/stream/cancel
```

### List Streams

```bash
GET /api/streams
```

Returns all configured streams with their steps and schedules.

### Run a Script

```bash
POST /api/run
Content-Type: application/json

{"script_path": "ingest/customers.py"}
```

### Run History

```bash
GET /api/history?limit=50
```

### Scheduler Status

```bash
GET /api/scheduler
```

## Related Pages

- [Transforms](transforms) -- SQL model details
- [Configuration](configuration) -- Full `project.yml` reference
- [Scheduler](scheduler) -- Cron scheduling details
- [Connectors](connectors) -- Automated data ingestion
- [Versioning](versioning) -- Pipeline snapshots and Rewind
- [Sentinel](sentinel) -- Schema change detection before pipeline runs
- [CLI Reference](cli-reference) -- All pipeline-related commands
