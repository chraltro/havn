# Configuration

All havn project settings live in `project.yml` at the project root. This page documents every configuration option, including database resources, pipeline streams, connectors, environments, alerts, Sentinel, and Rewind.

## Web UI Experience

### Settings Panel

1. Go to the **Configure** tab and click **Settings**
2. The Settings panel lets you manage:
   - **Secrets** -- View, add, and delete `.env` variables (keys are shown, values are masked)
   - **Users** -- Create, update, and delete users with role assignments (when auth is enabled)
   - **Alerts** -- Configure Slack and webhook notification channels
   - **Resource Limits** -- View and update database memory_limit and threads settings
   - **Theme** -- Toggle between light and dark modes

### Environment Switcher

The web UI shows the active environment and lets you switch between configured environments via the API. See [Environments](environments).

## Minimal Configuration

```yaml
name: my-project
database:
  path: warehouse.duckdb
```

## Full Reference

### Project Name

```yaml
name: my-project
```

Human-readable project name. Used in logging, documentation, and the web UI header.

### Database

```yaml
database:
  path: warehouse.duckdb
  threads: 4
  memory_limit: "2GB"
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `path` | string | required | Path to the DuckDB file, relative to the project root |
| `threads` | int | DuckDB default | Number of threads for query parallelism |
| `memory_limit` | string | DuckDB default | Maximum memory per query (e.g., "2GB", "512MB") |

The `threads` and `memory_limit` settings are applied when connecting to DuckDB and control resource usage for transforms and queries.

### Connections

Define external database connections. Values support environment variable expansion via `${VAR}` syntax, resolved from `.env`:

```yaml
connections:
  prod_postgres:
    type: postgres
    host: ${DB_HOST}
    port: 5432
    database: ${DB_NAME}
    user: ${DB_USER}
    password: ${DB_PASSWORD}

  analytics_mysql:
    type: mysql
    host: localhost
    database: analytics
    user: reader
    password: ${MYSQL_PASSWORD}
```

Connection parameters vary by type. See [Connectors](connectors) for available types.

### Streams

Define data pipelines with ordered steps:

```yaml
streams:
  full-refresh:
    description: "Full pipeline rebuild"
    schedule: "0 6 * * *"
    retries: 2
    retry_delay: 10
    webhook_url: "https://hooks.slack.com/services/..."
    steps:
      - seed: [all]
      - ingest: [all]
      - transform: [all]
      - export: [all]

  quick-transform:
    description: "Just rebuild models"
    steps:
      - transform: [all]
```

Stream options:

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `description` | string | `""` | Human-readable description |
| `schedule` | string | `null` | Cron expression (5-field) |
| `retries` | int | `0` | Retry attempts per failed step |
| `retry_delay` | int | `5` | Seconds between retries |
| `webhook_url` | string | `null` | URL for completion notifications |

Step actions: `ingest`, `seed`, `transform`, `export`. Each takes a list of targets or `[all]`.

### Lint

Configure SQLFluff SQL linting:

```yaml
lint:
  dialect: duckdb
  rules:
    - L001
    - L002
    - L003
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `dialect` | string | `duckdb` | SQL dialect for SQLFluff |
| `rules` | list | all rules | SQLFluff rules to enable |

Lint configuration can also be managed via API:

```bash
GET /api/lint/config     # View current config
PUT /api/lint/config     # Update config
DELETE /api/lint/config  # Reset to defaults
```

### Sources

Declare external data sources with metadata and freshness SLAs:

```yaml
sources:
  - name: production_db
    schema: landing
    description: "Production PostgreSQL database"
    connection: prod_postgres
    freshness_hours: 24
    tables:
      - name: customers
        description: "Customer records"
        loaded_at_column: updated_at
        columns:
          - name: customer_id
            description: "Primary key"
          - name: email
            description: "Customer email address"
      - name: orders
        description: "Order records"
```

See [Sources](sources) for details.

### Exposures

Declare downstream consumers of your data:

```yaml
exposures:
  - name: sales_dashboard
    description: "Executive sales dashboard"
    owner: analytics-team
    type: dashboard
    url: "https://dashboard.internal/sales"
    depends_on:
      - gold.daily_revenue
      - gold.customer_summary
```

Exposures appear in the DAG visualization and documentation. They represent systems outside havn that consume your data (dashboards, APIs, reports).

### Environments

Define environment-specific overrides:

```yaml
environments:
  dev:
    database:
      path: dev_warehouse.duckdb
  prod:
    database:
      path: prod_warehouse.duckdb
  test:
    database:
      path: ":memory:"
```

Switch environments with `--env`:

```bash
havn transform --env prod
havn serve --env dev
```

See [Environments](environments) for details.

### Alerts

Configure alerting for pipeline events:

```yaml
alerts:
  channels:
    - slack
    - webhook
  slack_webhook_url: ${SLACK_WEBHOOK_URL}
  webhook_url: "https://alerts.example.com/havn"
  on_success: true
  on_failure: true
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `channels` | list | `[]` | Alert channels: `slack`, `webhook`, `log` |
| `slack_webhook_url` | string | `null` | Slack incoming webhook URL |
| `webhook_url` | string | `null` | Custom webhook URL for alerts |
| `on_success` | bool | `false` | Send alerts on pipeline success |
| `on_failure` | bool | `true` | Send alerts on pipeline failure |

### Sentinel (Schema Monitoring)

Configure Schema Sentinel for upstream schema change detection:

```yaml
sentinel:
  enabled: true
  on_change: warn
  track_ordering: false
  rename_inference: true
  auto_fix: false
  select_star_warning: true
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enabled` | bool | `true` | Enable schema monitoring |
| `on_change` | string | `warn` | Action on schema change: `warn`, `error`, `ignore` |
| `track_ordering` | bool | `false` | Treat column reordering as breaking |
| `rename_inference` | bool | `true` | Suggest renames for deleted+added column pairs |
| `auto_fix` | bool | `false` | Auto-apply obvious rename fixes |
| `select_star_warning` | bool | `true` | Flag `SELECT *` as high-risk for schema changes |

See [Sentinel](sentinel) for full documentation.

### Rewind (Pipeline Snapshots)

Configure automatic pipeline snapshots and time travel:

```yaml
rewind:
  enabled: true
  retention: 30
  max_storage: 10GB
  dedup: true
  exclude: [temp_table, scratch]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enabled` | bool | `true` | Enable automatic snapshots |
| `retention` | int | `30` | Days to keep snapshots |
| `max_storage` | string | unlimited | Maximum snapshot storage (e.g., "10GB") |
| `dedup` | bool | `true` | Skip snapshots when data is identical to previous |
| `exclude` | list | `[]` | Tables to exclude from snapshots |

See [Versioning](versioning) for full documentation.

### Connectors (CDC)

Configure CDC-enabled connectors for incremental data extraction:

```yaml
connectors:
  prod_users:
    type: postgres
    connection: prod_postgres
    target_schema: landing
    tables:
      - name: users
        cdc_mode: high_watermark
        cdc_column: updated_at
      - name: roles
        cdc_mode: full_refresh
    schedule: "*/30 * * * *"
```

See [CDC](cdc) for details.

## Environment Variable Expansion

Any value in `project.yml` can reference environment variables using `${VAR}` syntax:

```yaml
connections:
  prod:
    type: postgres
    host: ${DB_HOST}
    password: ${DB_PASSWORD}
```

Variables are resolved from the `.env` file at the project root:

```
DB_HOST=db.production.internal
DB_PASSWORD=s3cure_p@ssw0rd
```

The `.env` file is included in `.gitignore` by default and should never be committed.

## Managing Secrets

### Via CLI

```bash
havn secrets list                    # List secret keys
havn secrets set DB_PASSWORD value   # Set a secret
havn secrets delete DB_PASSWORD      # Remove a secret
```

### Via Web UI

Go to **Configure** > **Settings** > **Secrets** to view and manage secrets. Keys are shown but values are always masked.

### Via API

```bash
# List secrets (keys with masked values)
curl http://localhost:3000/api/secrets \
  -H "Authorization: Bearer <admin-token>"

# Set a secret
curl -X POST http://localhost:3000/api/secrets \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"key": "DB_PASSWORD", "value": "new_password"}'

# Delete a secret
curl -X DELETE http://localhost:3000/api/secrets/DB_PASSWORD \
  -H "Authorization: Bearer <admin-token>"
```

## Database Configuration via API

```bash
# View current database config
GET /api/config/database

# Update database config
PUT /api/config/database
Content-Type: application/json

{"threads": 8, "memory_limit": "4GB"}
```

## Related Pages

- [Getting Started](getting-started) -- Project setup walkthrough
- [Pipelines](pipelines) -- Stream configuration details
- [Environments](environments) -- Multi-environment support
- [Connectors](connectors) -- Connection types and parameters
- [Scheduler](scheduler) -- Cron scheduling reference
- [Sentinel](sentinel) -- Schema monitoring configuration
- [Versioning](versioning) -- Rewind and snapshot configuration
