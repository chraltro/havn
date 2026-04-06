# Orchestration Jobs

Orchestration jobs are YAML files that define what to run and when. They replace the `streams:` section in `project.yml` for new projects and add capabilities that streams lack: dbt-style model selectors, multiple simultaneous schedules, tags, and a visual DAG picker in the web UI.

Streams in `project.yml` continue to work unchanged. Jobs are the recommended approach for new pipelines.

## Creating a Job

Jobs live in the `orchestration/` directory at your project root. Each `.yml` file is one job.

```yaml
# orchestration/daily-refresh.yml
name: daily-refresh
description: "Full daily pipeline"
targets:
  - +gold.orders
  - +gold.customers
schedules:
  - "0 6 * * *"
  - "0 18 * * *"
tags:
  - production
  - daily
retry: 2
retry_delay: 30
timeout_minutes: 60
enabled: true
```

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `targets` | list | One or more model selectors or script paths |

All other fields are optional.

### Full Field Reference

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | filename stem | Display name for the job |
| `description` | string | `""` | Human-readable description |
| `targets` | list | required | What to run (see Selectors below) |
| `schedules` | list | `[]` | When to run (cron or interval strings) |
| `tags` | list | `[]` | Labels for filtering in the UI |
| `retry` | int | `0` | Number of retry attempts on failure |
| `retry_delay` | int | `10` | Seconds between retries |
| `timeout_minutes` | int | `60` | Maximum runtime before the job is cancelled |
| `enabled` | bool | `true` | Set to `false` to pause without deleting |
| `resolve` | string | `"upstream"` | Legacy field; prefer explicit selectors |

## Target Selectors

Targets use a dbt-style syntax to select models and scripts.

| Selector | Selects |
|----------|---------|
| `silver.orders` | Exactly the `silver.orders` model |
| `+silver.orders` | `silver.orders` and all its upstream models |
| `silver.orders+` | `silver.orders` and all downstream models |
| `+silver.orders+` | `silver.orders`, all upstream, and all downstream |
| `silver.*` | All models in the `silver` schema |
| `ingest/orders.py` | A specific ingest script |
| `export/daily_report.py` | A specific export script |

Upstream resolution automatically includes ingest scripts that feed the selected models. Downstream resolution includes export scripts that consume them.

### Examples

```yaml
# Rebuild one model and everything it depends on
targets:
  - +gold.revenue_summary

# Rebuild two separate model families
targets:
  - +gold.orders
  - +gold.customers

# Run all models in the silver layer
targets:
  - silver.*

# A mix of scripts and models
targets:
  - ingest/stripe.py
  - +silver.payments
  - export/daily_report.py
```

## Schedules

Jobs support two schedule formats. Both can appear in the same `schedules` list, and a job fires whenever any schedule matches.

### 5-field Cron

Standard cron syntax: `minute hour day-of-month month day-of-week`.

```yaml
schedules:
  - "0 6 * * *"       # 6 AM daily
  - "0 9 * * 1"       # 9 AM every Monday
  - "*/30 * * * *"    # Every 30 minutes
  - "0 6,18 * * *"    # 6 AM and 6 PM
```

### Interval Strings

Human-readable intervals for periods that 5-field cron cannot express cleanly.

```yaml
schedules:
  - "every 30 minutes"
  - "every 2 hours"
  - "every 3 days"
  - "every 2 weeks"
  - "every 1 month"
```

Supported units: `minute`, `hour`, `day`, `week`, `month`, `year` (singular or plural). Month is treated as 30 days; year as 365 days.

### Multiple Schedules

A job with multiple schedules fires on the earliest upcoming trigger:

```yaml
# Fires at 6 AM and at 6 PM each day
name: twice-daily
targets:
  - +gold.orders
schedules:
  - "0 6 * * *"
  - "0 18 * * *"
```

### No Schedule

Omit `schedules` entirely for a manual-only job. Run it with `havn jobs run <name>` or via the web UI.

## CLI Commands

```bash
havn jobs                    # List all jobs with status and next run time
havn jobs run <name>         # Trigger a job immediately
havn jobs preview <name>     # Show the resolved execution plan without running
havn jobs history            # Show recent job runs
havn jobs history --job <name>  # Filter to one job
havn jobs enable <name>      # Set enabled: true
havn jobs disable <name>     # Set enabled: false
```

### Previewing a Job

`havn jobs preview` resolves the job's selectors against the current DAG and shows the ordered steps that would execute:

```
daily-refresh -- 12 steps (3 ingest, 7 transform, 2 export)
Estimated: 45.2s

  1. ING  ingest/stripe.py
  2. ING  ingest/postgres.py
  3. TRF  bronze.raw_orders
  4. TRF  silver.orders
  ...
 12. EXP  export/daily_report.py
```

## Web UI

Orchestration jobs are managed in **Develop -> Orchestration**. The tab has two sub-tabs:

- **Plan Jobs** -- The job list with inline creation, editing, tag filtering, a sparkline of the last 10 runs, and a clone button per row.
- **Job Results** -- A timeline of recent runs with step-by-step output. This tab also shows output from the pipeline **Run** button in the toolbar (not just orchestration jobs), so all pipeline execution is visible in one place.

### Creating a Job in the UI

Click the **+** button at the top of the job list. A new row appears inline (no modal). Fill in the name, select targets using the DAG picker, choose a schedule with the cron wizard, add tags, then save.

### Streaming Output (SSE)

When a job is run from the web UI, its output streams to the Job Results tab in real time via Server-Sent Events (SSE). Each step reports its status as it executes (start, done, skip, fail), with row counts, duration, and error messages. You do not need to wait for the job to finish to see progress.

### Expandable Step Details

In the Job Results tab, each step in a completed run can be expanded to show its full output (`log_output`). Click a step row to toggle the detail view and inspect captured stdout/stderr, error tracebacks, or row-level diagnostics.

### DAG Picker

Click the target field to open the DAG picker. The picker shows your full pipeline graph -- ingest scripts on the left, transform models in the middle, export scripts on the right.

- Click a node to toggle it as an explicit target.
- Click the left arrow (upstream) on a node to add the `+` prefix and include all upstream dependencies automatically.
- Click the right arrow (downstream) to add the `+` suffix and include downstream models and export scripts.
- When an upstream or downstream selector is active, the DAG picker highlights the affected nodes so you can see exactly which models and scripts will be included in the job.

### Cron Wizard

Click the schedule field to open the cron wizard. It has 8 tabs:

| Tab | What it does |
|-----|-------------|
| Common | Pre-built schedules (hourly, daily, weekly, etc.) |
| Interval | Human-readable interval strings (`every N unit`) |
| Minutes | Per-field: Every / Every N / Specific values / Range |
| Hours | Same modes for the hours field |
| Day of Month | Same modes for day-of-month |
| Month | Same modes for the month field |
| Day of Week | Same modes for the weekday field |
| Advanced | Raw cron expression input |

As you adjust settings, the wizard shows the **next 5 run times** and a plain-language description.

## Job Storage

Each job is a file at `orchestration/<name>.yml`. Jobs are plain text, can be committed to git, and take effect immediately after saving -- no restart required.

Run history is stored in `_havn.job_runs` inside `warehouse.duckdb`.

## Related Pages

- [Pipelines](pipelines) -- The older `streams:` approach (still supported)
- [Scheduler](scheduler) -- Cron scheduling and the file watcher
- [Transforms](transforms) -- SQL model reference
- [Migration from Streams to Jobs](streams-to-jobs) -- Step-by-step migration guide
