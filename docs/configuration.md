# Configuration

All havn project settings live in `project.yml` at the project root. This page documents every configuration option.

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

Human-readable project name. Used in logging and documentation.

### Database

```yaml
database:
  path: warehouse.duckdb
```

- `path` -- Path to the DuckDB database file, relative to the project root.

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
| `retries` | int | `0` | Retry attempts per failed script or model (a failed assertion is not retried) |
| `retry_delay` | int | `5` | Seconds between retries |
| `webhook_url` | string | `null` | URL for completion notifications |
| `overlap` | string | `scheduler.overlap` | `skip` or `queue`: what a fire does while this stream is still running |
| `catchup` | bool | `scheduler.catchup` | Run once after a restart if fires were missed |
| `timezone` | string | `scheduler.timezone` | IANA zone the `schedule` is evaluated in, or `local` |

Step actions: `ingest`, `seed`, `transform`, `export`. Each takes a list of targets or `[all]` (script or seed names, or graph selectors for `transform`).

How a stream runs, the same whether it is started with `havn stream`, by the scheduler or from the web UI:

- Steps run in the order they are listed; a step starts when the one before it has finished.
- Inside a `transform` step, models build in parallel as soon as their own upstream models are done. The scripts and seeds of one step run one at a time, in name order, and a failed ingest script stops the rest of its step.
- Before the first model of a `transform` step is built, the models are validated (the same checks as `havn validate`, bind pass included). An error-severity finding stops the stream before anything is built.
- A failed step (a script error, a model error, an error-severity `@assert`, a policy denial) stops the stream: the later steps are reported as skipped with the failure as the reason, and `havn stream` exits 1.
- Every script and model run is logged in `_havn.run_log` under one pipeline run id, and the run appears in the Job Results list (`_havn.job_runs`).

Unknown step actions are reported before anything runs.

### Scheduler

Settings for `havn schedule` and `havn serve --schedule`. All optional:

```yaml
scheduler:
  max_concurrent: 1          # runs at a time (worker pool size)
  overlap: skip              # skip | queue
  catchup: false             # after a restart, run once for missed fires
  timezone: Europe/Oslo      # IANA name; leave out for the server's local time
  history_days: 30           # how long _havn.schedule_history keeps rows
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `max_concurrent` | int | `1` | How many scheduled runs may execute at the same time. Raise it only for streams and jobs that write different tables; see [Scheduler](scheduler#concurrency). |
| `overlap` | string | `skip` | When a stream or job is still running (or already waiting) at its next fire: `skip` records the fire as skipped, `queue` lets one run wait. |
| `catchup` | bool | `false` | On start, fires that passed while the scheduler was down are always recorded as `missed`; with `true` the newest one also runs, once. |
| `timezone` | string | server local time | Zone that cron expressions are evaluated in. An unknown name is an error (`havn validate` and scheduler start fail); it never falls back to local time. |
| `history_days` | int | `30` | Retention of the schedule history table. |

`overlap`, `catchup` and `timezone` are defaults: a stream (above) or a job
file in `orchestration/` can set its own. `timezone: local` on a stream or
job means the server's local time even when the project default is a zone.

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

- `dialect` -- SQL dialect for SQLFluff. Default: `duckdb`.
- `rules` -- List of SQLFluff rules to enable. Default: all rules.

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

Exposures appear in the DAG visualization and documentation.

### Environments

Define environment-specific overrides:

```yaml
environments:
  dev:
    database:
      path: dev_warehouse.duckdb
    defer: prod            # read models dev has not built from prod
  prod:
    database:
      path: prod_warehouse.duckdb
  test:
    database:
      path: ":memory:"
```

`defer:` names another environment whose warehouse supplies the models this
one has not built. It is read-only and applies to `havn transform`; writes
always land in this environment. The target's file must not be open for
writing elsewhere, or the run needs `--defer-snapshot`. See
[Environments: Defer](environments#defer).

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

- `channels` -- List of alert channels: `slack`, `webhook`, `log`
- `slack_webhook_url` -- Slack incoming webhook URL
- `webhook_url` -- Custom webhook URL for alerts
- `on_success` -- Send alerts on pipeline success (default: false)
- `on_failure` -- Send alerts on pipeline failure (default: true)

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

### packages

Shared models and macros installed from git or a local directory:

```yaml
packages:
  - name: crm
    git: https://github.com/example/havn-crm.git
    rev: v1.4.0          # tag, commit or branch; required for git sources
  - name: shared
    path: ../shared-models
```

`name` must be a valid identifier and unique, because it becomes a SQL schema
prefix. A git source needs a `rev`; a branch is accepted but warned about,
since it moves. `havn packages install` writes `havn_packages.lock`, which is
committed, while `havn_packages/` is gitignored. See [Packages](packages).

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

### Managing Secrets

Via the CLI:

```bash
havn secrets list                   # show secret keys (values masked)
havn secrets set DB_PASSWORD value  # set or update a secret
havn secrets delete DB_PASSWORD     # delete a secret
```

Via the API (when auth is enabled):

```bash
curl -X POST http://localhost:3000/api/secrets \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"key": "DB_PASSWORD", "value": "new_password"}'
```

## When havn edits project.yml

Some actions change `project.yml` (or a job file in `orchestration/`) for you:
adding or removing a connector (`havn connect`, the web UI), saving anomaly
detection or database settings, changing resource allocations, editing a job
or enabling/disabling it, and `havn migrate`. These edits change only the keys
involved and leave the rest of the file as you wrote it: comments, commented-out
examples, key order, blank lines, quoting and `${VAR}` references stay. The file
is written to a temporary file next to it and then moved into place, so an
interrupted write never leaves a truncated `project.yml`.

A few things are normalised when a file is rewritten: block indentation follows
one style for the whole file (taken from the file), YAML 1.1 boolean spellings
such as `yes` or `On` are written as `true`/`false`, and `null` or `~` as an
empty value. The meaning of every value is unchanged. A comment on the same line
as a key that is removed goes with it; comment lines below it are kept. A `<<`
merge in a mapping that havn changes is written out as the keys it supplied, and
a mapping with an anchor is replaced rather than edited, so its aliases keep
their values.

Before the file is replaced, havn reads the new text back and checks that it says
exactly what was intended. If it does not, the file is written without its
comments instead (still valid, same data) and a warning is logged.

A file that is not valid YAML, repeats a key, or contains a value havn cannot
rewrite faithfully is left untouched and the edit fails with a message naming
the file (HTTP 400 in the web UI, exit code 1 on the command line).

## Related Pages

- [Getting Started](getting-started) -- Project setup walkthrough
- [Pipelines](pipelines) -- Stream configuration details
- [Environments](environments) -- Multi-environment support
- [Connectors](connectors) -- Connection types and parameters
- [Packages](packages) -- Sharing models and macros between projects
- [Scheduler](scheduler) -- Cron scheduling reference
