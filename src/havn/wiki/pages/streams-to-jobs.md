# Migrating from Streams to Jobs

havn's `streams:` section in `project.yml` is the original way to define pipelines. It still works and is not being removed. Orchestration jobs (YAML files in `orchestration/`) are the recommended approach for new pipelines because they support model selectors, multiple schedules, and a visual editor in the web UI.

This page shows how to convert an existing stream to a job.

## Side-by-side Comparison

### Before: stream in project.yml

```yaml
# project.yml
streams:
  daily-etl:
    description: "Daily ETL with retries"
    schedule: "0 6 * * *"
    retries: 3
    retry_delay: 10
    steps:
      - ingest: [all]
      - transform: [all]
      - export: [all]
```

### After: job in orchestration/

```yaml
# orchestration/daily-etl.yml
name: daily-etl
description: "Daily ETL with retries"
schedules:
  - "0 6 * * *"
retry: 3
retry_delay: 10
targets:
  - ingest/*
  - +gold.*
  - export/*
```

The most important difference is how targets are expressed. Streams use fixed step lists (`ingest: [all]`, `transform: [all]`). Jobs use selectors that resolve against the DAG at runtime, so adding a new model or script is picked up automatically.

## Field Mapping

| Stream field | Job field | Notes |
|---|---|---|
| `description` | `description` | Same |
| `schedule` | `schedules` | Jobs accept a list; add more schedules freely |
| `retries` | `retry` | Renamed |
| `retry_delay` | `retry_delay` | Same |
| `steps: [{ingest: [all]}]` | `targets: [ingest/*]` | Selector syntax |
| `steps: [{transform: [all]}]` | `targets: [+gold.*]` | Or `silver.*`, etc. |
| `steps: [{export: [all]}]` | `targets: [export/*]` | Selector syntax |
| `webhook_url` | `notify` | See below |

## Migrating Webhook Notifications

Stream `webhook_url` has no direct equivalent in jobs yet. Configure Slack/webhook alerts in `project.yml` under the `alerts:` key instead -- those apply globally to all pipeline runs:

```yaml
# project.yml
alerts:
  slack_webhook_url: "https://hooks.slack.com/services/..."
  on_failure: true
  on_success: false
```

## Running the Job

After creating the job file, verify it resolves correctly:

```bash
havn jobs preview daily-etl
```

Then run it once manually to confirm:

```bash
havn jobs run daily-etl
```

Once satisfied, you can remove the corresponding stream from `project.yml`.

## Keeping Both

There is no requirement to migrate. Streams and jobs coexist -- you can run `havn stream daily-etl` and `havn jobs run other-job` in the same project. Migrate at your own pace, or keep streams for simple single-schedule pipelines and use jobs for anything more complex.

## What Streams Still Do Better

For simple one-step pipelines with a single cron schedule, a stream definition in `project.yml` remains concise. Jobs add overhead (a separate file) that is not always worth it for trivial cases.

## Related Pages

- [Orchestration Jobs](orchestration-jobs) -- Full job reference
- [Pipelines](pipelines) -- Stream reference
- [Configuration](configuration) -- Full project.yml reference
