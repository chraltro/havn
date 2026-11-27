# Scheduler

havn includes a built-in scheduler for running streams on cron schedules. It also provides a file watcher that automatically rebuilds transforms when SQL files change. Both can run alongside the web server.

## Web UI Experience

### Scheduler Status in Overview

The **Overview** tab shows scheduled streams and their next expected run times. If a scheduled stream has failed recently, it appears in the Failed Runs section.

### Starting Scheduler with the Server

You can start the scheduler alongside the web server using the `--schedule` flag:

```bash
havn serve --schedule
```

This runs both the web UI and the cron scheduler in the same process. Scheduled streams execute in the background and their results appear in the History panel.

### Starting File Watcher with the Server

Similarly, start the file watcher alongside the server:

```bash
havn serve --watch
```

Or combine both:

```bash
havn serve --watch --schedule
```

With `--watch`, any changes to `.sql` files in `transform/` automatically trigger a rebuild, and the results stream to the Output Panel.

### Viewing Scheduled Streams

1. Go to **Configure** > **Settings** to see all configured schedules
2. Or check the API:

```bash
curl http://localhost:3000/api/scheduler
```

## Cron Scheduling

### Defining Schedules

Add a `schedule:` field to any stream in `project.yml`:

```yaml
streams:
  daily-etl:
    description: "Daily ETL pipeline"
    schedule: "0 6 * * *"         # 6 AM every day
    steps:
      - ingest: [all]
      - transform: [all]

  hourly-sync:
    description: "Hourly connector sync"
    schedule: "0 * * * *"         # Every hour on the hour
    steps:
      - ingest: [connector_stripe]
      - transform: [all]

  weekly-report:
    description: "Weekly summary export"
    schedule: "0 9 * * 1"         # 9 AM every Monday
    steps:
      - transform: [all]
      - export: [weekly_report]
```

### Cron Syntax

havn uses standard 5-field cron expressions:

```
+-------------- minute (0 - 59)
| +------------ hour (0 - 23)
| | +---------- day of month (1 - 31)
| | | +-------- month (1 - 12)
| | | | +------ day of week (0 - 6, 0 = Monday)
| | | | |
* * * * *
```

### Cron Patterns

| Pattern | Meaning |
|---------|---------|
| `*` | Every unit |
| `*/5` | Every 5 units |
| `1,15` | At 1 and 15 |
| `1-5` | From 1 through 5 |
| `30` | At exactly 30 |

### Common Schedules

```yaml
"0 6 * * *"      # Daily at 6 AM
"0 */2 * * *"    # Every 2 hours
"*/15 * * * *"   # Every 15 minutes
"0 9 * * 1"      # Monday at 9 AM
"0 0 1 * *"      # First day of every month at midnight
"30 8 * * 1-5"   # Weekdays at 8:30 AM
"0 6,18 * * *"   # Twice daily at 6 AM and 6 PM
```

## Starting the Scheduler

### CLI (Standalone)

```bash
havn schedule
```

This shows all scheduled streams and starts the scheduler:

```
Scheduled Streams
  Stream       Schedule       Description
  daily-etl    0 6 * * *      Daily ETL pipeline
  hourly-sync  0 * * * *      Hourly connector sync

Starting scheduler... (Ctrl+C to stop)
```

The scheduler runs as a foreground process. Press Ctrl+C to stop it.

### With the Web Server

```bash
havn serve --schedule
```

Starts the web UI and scheduler together. The scheduler runs in a background thread.

### How It Works

1. Every 30 seconds the dispatch loop re-reads `project.yml` and `orchestration/*.yml` and works out which schedules have fired since it last looked
2. Due runs go to a worker pool (`scheduler.max_concurrent`, default 1 run at a time); the loop keeps ticking while they execute
3. A stream or job still running (or already waiting) at its next fire is skipped (`overlap: skip`, the default) or allowed one waiting run (`overlap: queue`)
4. Every fire is recorded in `_havn.schedule_history`; see it with `havn schedule --history`
5. Cron is evaluated in `timezone` (an IANA name such as `Europe/Oslo`) or the server's local time

Settings, missed fires after a restart (`catchup`), DST rules and shutdown are described in the scheduler reference (docs/scheduler.md).

## File Watcher

The file watcher monitors `transform/` and `ingest/` for changes and automatically triggers rebuilds.

### Starting the Watcher (Standalone)

```bash
havn watch
```

Output:

```
Watching for changes... (Ctrl+C to stop)
  transform/  -> auto-rebuild SQL models
```

### With the Web Server

```bash
havn serve --watch
```

Starts the web UI and file watcher together.

### How It Works

1. Uses the `watchdog` library to monitor filesystem events
2. Watches `transform/` and `ingest/` directories recursively
3. When a `.sql` or `.py` file is modified, it triggers an action
4. For `transform/` changes: runs `havn transform`
5. Includes 2-second debouncing to avoid multiple rebuilds for rapid saves

### Example Session

```
Watching for changes... (Ctrl+C to stop)

Watcher: transform/silver/dim_customer.sql changed
Watcher: Running transform...
  skip  bronze.customers
  done  silver.dim_customer (1,234 rows, 45ms)
  done  gold.customer_summary (892 rows, 12ms)
Watcher: Transform completed
```

## Combined Mode

For development, the most convenient setup is:

```bash
havn serve --watch --schedule
```

This gives you:
- Web UI at localhost:3000
- Automatic rebuilds when you save SQL files
- Scheduled pipeline execution in the background

## Scheduler Architecture

### SchedulerThread

The scheduler is a daemon thread running a dispatch loop, plus a pool of daemon worker threads:

- The loop wakes every 30 seconds, reloads `project.yml` and `orchestration/*.yml`, and hands due streams, jobs and the dashboard report check to the pool
- Workers run them; with the default `max_concurrent: 1` one at a time, because overlapping runs in one DuckDB warehouse can fail on write-write conflicts
- Each fire is written to `_havn.schedule_history` (status, lateness, duration), and a heartbeat to `_havn.scheduler_state`
- On start, fires that passed while no scheduler was running are recorded as `missed`; with `catchup: true` the newest runs once
- Stopping cancels queued runs and waits up to 30 seconds for running ones

A scheduled stream runs through the same code as `havn stream`: seeds, retries, Pipeline Rewind tracking, the stream's `webhook_url`, and the `alerts:` pipeline success/failure alert all apply. A stream counts as failed when a step fails, including a transform step where a model errored or an error-severity `@assert` rejected it; later steps do not run.

### FileWatcher

The file watcher runs as a separate daemon thread using the `watchdog` library. It:

- Monitors `transform/` and `ingest/` for `.sql` and `.py` changes
- Debounces rapid changes (2-second window)
- Creates a fresh database connection for each rebuild
- Reports results to the console

## Scheduler Status

### Via API

```bash
curl http://localhost:3000/api/scheduler
```

Returns:

```json
{
  "scheduled_streams": [
    {
      "name": "daily-etl",
      "description": "Daily ETL pipeline",
      "schedule": "0 6 * * *",
      "steps": [
        {"action": "ingest", "targets": ["all"]},
        {"action": "transform", "targets": ["all"]}
      ]
    }
  ]
}
```

## Deployment Considerations

### Running as a Service

For production deployments, run the scheduler as a background service:

```bash
# Using systemd (Linux)
# Create /etc/systemd/system/havn-scheduler.service

# Using nohup
nohup havn schedule > /var/log/havn-scheduler.log 2>&1 &
```

### Combined Server Deployment

For simpler deployments, run everything together:

```bash
nohup havn serve --schedule --watch --auth --env prod > /var/log/havn.log 2>&1 &
```

### Multiple Workers

The scheduler runs streams sequentially within a single thread. For parallel execution of independent streams, consider running multiple scheduler instances or using external orchestration tools.

### Monitoring

- Check `havn history` to verify scheduled runs are completing
- Use `havn freshness` to detect if scheduled pipelines have stopped running
- Configure `alerts:` in `project.yml` (or a `webhook_url` on a stream) for immediate failure alerts; scheduled streams and jobs send them too
- Use the **Overview** tab in the web UI for at-a-glance monitoring

## Related Pages

- [Pipelines](pipelines) -- Stream configuration and execution
- [Configuration](configuration) -- Schedule configuration in project.yml
- [Connectors](connectors) -- Scheduling connector syncs
- [CLI Reference](cli-reference) -- Schedule and watch commands
