# Scheduler

havn includes a built-in scheduler for running streams and orchestration jobs on cron schedules. It also provides a file watcher that automatically rebuilds transforms when SQL files change.

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
    overlap: queue                # still running at the next hour: run once more after it
    steps:
      - ingest: [connector_stripe]
      - transform: [all]

  weekly-report:
    description: "Weekly summary export"
    schedule: "0 9 * * 1"         # 9 AM every Monday
    timezone: Europe/Oslo         # 9 AM in Oslo, whatever the server's clock says
    steps:
      - transform: [all]
      - export: [weekly_report]
```

Jobs in `orchestration/*.yml` take `schedules:` (a list of cron expressions or intervals such as `every 2h`) and the same `overlap`, `catchup` and `timezone` keys:

```yaml
name: nightly
targets: [gold.*]
schedules: ["0 2 * * *"]
timezone: Europe/Oslo
overlap: skip
catchup: true
```

Project-wide defaults live under `scheduler:` in `project.yml`:

```yaml
scheduler:
  max_concurrent: 1      # runs at a time
  overlap: skip          # skip | queue
  catchup: false         # run once after a restart if fires were missed
  timezone: Europe/Oslo  # default: the server's local time
  history_days: 30       # retention of the schedule history
```

### Cron Syntax

havn uses standard 5-field cron expressions (Vixie cron semantics):

```
┌───────────── minute (0 - 59)
│ ┌───────────── hour (0 - 23)
│ │ ┌───────────── day of month (1 - 31)
│ │ │ ┌───────────── month (1 - 12 or JAN-DEC)
│ │ │ │ ┌───────────── day of week (0 - 7 or SUN-SAT; 0 and 7 are Sunday)
│ │ │ │ │
* * * * *
```

When both day of month and day of week are restricted, a day matches if either does. `@hourly`, `@daily`, `@weekly`, `@monthly` and `@yearly` are accepted.

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

### CLI

```bash
havn schedule
```

This checks the scheduler settings, shows every scheduled stream and job with its next run, and starts the scheduler:

```
                         Scheduled Streams
  Stream       Schedule    Next run                      Overlap  Description
  daily-etl    0 6 * * *   2026-11-28 06:00 local        skip     Daily ETL pipeline
  weekly-rep…  0 9 * * 1   2026-11-30 09:00 Europe/Oslo  skip     Weekly summary export

Starting scheduler... (at most 1 run at a time; Ctrl+C to stop)
```

The next run is shown in the schedule's own time zone (`local` is the server's local time). The scheduler runs as a foreground process. Press Ctrl+C to stop it (see [Stopping](#stopping)).

### With the Web Server

```bash
havn serve --schedule
```

runs the same scheduler inside the server process.

### Settings are checked before anything runs

An unknown time zone name, an `overlap` other than `skip` or `queue`, or a `max_concurrent` below 1 stops `havn schedule` and `havn serve --schedule` with a message naming the setting, and `havn validate` reports the same errors. A misspelt time zone never falls back to local time.

## How It Works

The scheduler is a dispatch loop and a pool of worker threads:

1. Every 30 seconds the loop re-reads `project.yml` and `orchestration/*.yml` (so schedule changes take effect without a restart), works out which schedules have a fire time since it last looked, and hands those runs to the pool. It never runs a stream or job itself, so it keeps ticking while a long run executes.
2. Workers run streams through the same code as `havn stream` (seeds, retries, Pipeline Rewind, the stream's `webhook_url`, the `alerts:` success/failure alert) and jobs as `havn jobs run` would. A stream counts as failed when a step fails, including a transform step where a model errored or an error-severity `@assert` rejected it.
3. Every fire is written to `_havn.schedule_history`, with how late the run started and how it ended.

Dashboard reports that are due are checked once per tick, also in the pool.

### Overlap

What happens when a stream or job is still running, or already waiting to run, at its next fire time:

| `overlap` | Behaviour |
|-----------|-----------|
| `skip` (default) | The fire is recorded as `skipped_overlap` and nothing more runs. |
| `queue` | One run waits and starts when the current one finishes. A further fire while one is already waiting is recorded as `skipped_overlap`. |

A run is never started twice at the same time, whatever `max_concurrent` is.

### Concurrency

`scheduler.max_concurrent` is the number of workers, and it defaults to **1**: scheduled runs execute one at a time, as they always have. Runs that come due together wait in a queue, and their lateness is recorded.

Raising it is possible, but runs that overlap in one warehouse can fail. DuckDB allows one writer process per file and resolves concurrent transactions within that process optimistically, so two runs that write the same catalog object conflict. Measured on this codebase (two streams started together in one process, as the scheduler would):

- On a fresh warehouse, the second run failed with `Catalog write-write conflict on create with "_havn"`: both runs create the metadata tables.
- Two ingest scripts replacing the same landing table failed in 4 of 10 rounds.
- Two scripts that each run `CREATE SCHEMA IF NOT EXISTS landing` on a warehouse without that schema conflicted.
- Model builds are safe from each other (havn builds one model at a time per process), and streams that write disjoint, already existing tables ran side by side without errors.

So set `max_concurrent` above 1 only when the streams and jobs that can overlap write different tables and the schemas they use already exist. A conflict shows up as a failed run in the history; stream `retries` can absorb an occasional one.

The `havn serve` web UI runs one pipeline at a time on its own, independently of the scheduler: a run started from the UI can still overlap a scheduled one.

### Missed fires and restarts

The scheduler writes a heartbeat to `_havn.scheduler_state` on every tick. When it starts, it compares that heartbeat (or a schedule's last recorded fire, whichever is later) with the current time:

- Every cron fire that passed while no scheduler was running is recorded in the history as `missed`.
- With `catchup: true` the newest of them runs once, straight away (the history marks it `catchup` and shows how late it started); the older ones stay `missed`. With `catchup: false` (the default) they are only recorded.
- On the very first start there is no heartbeat, so nothing counts as missed.
- At most the newest 100 missed fires per schedule are recorded individually; the log says how many there were.

The same applies when the process was suspended (a laptop asleep): if several fires of one schedule passed between two ticks, the newest runs and the older ones are recorded as `missed`.

Runs that were queued or running when an earlier scheduler process ended are marked `interrupted` on the next start.

Interval schedules on jobs (`every 2h`) count from the job's last run in `_havn.job_runs`, so an overdue interval job runs on the first tick after a restart whatever `catchup` says.

### Time zones

Cron expressions are evaluated in the server's local time unless `timezone` is set, on `scheduler:` for the whole project or on a stream or job. Docker images usually run in UTC, so `timezone: Europe/Oslo` is the way to get 06:00 Oslo time there. Names are IANA zone names (`Europe/Oslo`, `America/New_York`, `UTC`); `local` on a stream or job means the server's local time even when the project has a default zone. havn depends on the `tzdata` package so that zone names work on Windows and in slim containers too.

Daylight saving time changes have two awkward cases, handled like this:

- **A fire time that does not exist** (clocks go forward, so 02:30 on the last Sunday of March is skipped in Europe/Oslo) runs at the first minute after the gap, 03:00. Several fire times inside the gap (`*/15 * * * *`) collapse into that one run.
- **A fire time that occurs twice** (clocks go back, so 02:00 to 02:59 happens twice on the last Sunday of October) fires once, at its first occurrence. A schedule that fires every hour or more often therefore pauses during the repeated hour instead of running it twice.

These rules apply to configured zones and to server local time alike.

### Stopping

Stopping the scheduler (Ctrl+C on `havn schedule`, or shutting down `havn serve --schedule`):

1. No new runs are dispatched.
2. Runs that were queued but had not started are recorded as `cancelled`.
3. Runs in progress get up to 30 seconds to finish. Press Ctrl+C again to quit at once.
4. A run still going after that ends with the process: the worker threads never keep the process alive. Its history row stays `running` and becomes `interrupted` when the scheduler next starts; DuckDB rolls back the transaction it was in.

## Schedule History

```bash
havn schedule --history                     # newest 30 fires
havn schedule --history --name daily-etl    # one stream or job
havn schedule --history --status missed     # only missed fires
havn schedule --history --limit 200
```

shows one row per fire: when it was due (in the schedule's time zone), its status, how late it started and how long it ran.

| Status | Meaning |
|--------|---------|
| `queued` | Handed to a worker, not started yet |
| `running` | Started |
| `success` / `failed` | Finished |
| `skipped_overlap` | The same stream or job was still running or already waiting |
| `missed` | Passed while no scheduler was running (or while the process was suspended) |
| `interrupted` | Was queued or running when the scheduler process ended |
| `cancelled` | Was queued when the scheduler was stopped |

The table is `_havn.schedule_history` (timestamps in UTC), so it can also be queried:

```bash
havn query "SELECT schedule_name, scheduled_for, status, lateness_ms
            FROM _havn.schedule_history ORDER BY scheduled_for DESC LIMIT 20"
```

Rows older than `scheduler.history_days` (default 30) are deleted at start and hourly.

## File Watcher

The file watcher monitors `transform/` and `ingest/` for changes and automatically triggers rebuilds.

### Starting the Watcher

```bash
havn watch
```

Output:

```
Watching for changes... (Ctrl+C to stop)
  transform/  -> auto-rebuild SQL models
```

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

## Scheduler Status

### Via API

```bash
curl http://localhost:3000/api/scheduler
```

Returns the scheduled streams with their time zone, overlap policy and next run:

```json
{
  "scheduled_streams": [
    {
      "name": "daily-etl",
      "description": "Daily ETL pipeline",
      "schedule": "0 6 * * *",
      "timezone": "Europe/Oslo",
      "overlap": "skip",
      "next_run": "2026-11-28T06:00:00+01:00",
      "steps": [
        {"action": "ingest", "targets": ["all"]},
        {"action": "transform", "targets": ["all"]}
      ]
    }
  ]
}
```

```bash
curl "http://localhost:3000/api/scheduler/history?limit=50&name=daily-etl&status=failed"
```

returns `{"history": [...]}`, newest first, with `scheduled_for`, `started_at` and `finished_at` as UTC timestamps, `lateness_ms`, `duration_ms`, `status`, `trigger` (`schedule` or `catchup`) and `error`. Both endpoints need read permission.

## Deployment Considerations

### Running as a Service

For production deployments, run the scheduler as a background service:

```bash
# Using systemd (Linux)
# Create /etc/systemd/system/havn-scheduler.service

# Using nohup
nohup havn schedule > /var/log/havn-scheduler.log 2>&1 &
```

Run one scheduler per warehouse: either `havn schedule` or `havn serve --schedule`, not both.

### Monitoring

- `havn schedule --history --status failed` (or `missed`, `skipped_overlap`) shows what did not go to plan
- Check `havn history` for the step-level log of each run
- Use `havn freshness` to detect if scheduled pipelines have stopped running
- Configure webhook notifications on streams for immediate failure alerts

## Related Pages

- [Pipelines](pipelines) -- Stream configuration and execution
- [Configuration](configuration) -- Scheduler settings in project.yml
- [Connectors](connectors) -- Scheduling connector syncs
- [CLI Reference](cli-reference) -- Schedule and watch commands
