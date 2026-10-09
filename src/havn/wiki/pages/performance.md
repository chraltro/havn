# Performance Advisor

Every model build records how long it took, how many rows went in and out,
and, unless you switch it off, the query plan DuckDB actually executed with
real per-operator timings. havn uses that history to tell you which models
are slow, which ones just got slower, which chain of models sets the length
of a run, and what to change.

```bash
havn perf                          # slowest models, regressions, advice
havn perf gold.orders              # one model: builds, plan, regressions, advice
havn perf --critical-path          # what the last run waited for
havn perf --critical-path --run 3f2a   # a specific run (id prefix)
havn perf --runs                   # recent runs with wall clock and busy time
havn perf --advice --all           # advice, including dismissed and snoozed
havn perf gold.orders --dismiss order_by_non_final
havn perf gold.orders --dismiss unused_table --snooze 14
havn perf gold.orders --reopen unused_table
havn perf --json                   # any of the above as JSON
```

In the web UI the same data is on **Observe > Performance**.

## What is recorded

One row per build in `_havn.model_perf`:

| Column | Meaning |
|---|---|
| `duration_ms` | The build itself, as `havn transform` reports it |
| `rows_before` / `rows_out` | Rows in the model before and after the build |
| `rows_in` | Size of the model's upstreams at build time (catalog estimate) |
| `rows_scanned` | Rows the build's scans actually read (from the profile) |
| `rows_produced` | Rows the build query produced (an incremental's new slice) |
| `peak_memory_bytes` | Memory DuckDB allocated for the build, or the connection's buffer high-water mark when the build raised it (DuckDB reports peaks per connection, not per query, so this is approximate) |
| `spill_bytes` | Growth of the temp-directory high-water mark during the build (a lower bound on what it spilled) |
| `cpu_time_ms`, `bytes_read`, `bytes_written` | From the profile |
| `full_refresh` | True when the build rewrote the whole table |
| `plan` | Compact operator tree with real timings and row counts |
| `top_operators` | The five operators that took the most time |

### How plans are captured

havn switches DuckDB's profiler on around the one statement that does the
work (the `CREATE TABLE AS`, or the staging `CREATE TEMP TABLE AS` of an
incremental, snapshot or microbatch model) and reads the profile straight
after. The query is **not** run a second time and no `EXPLAIN ANALYZE` is
issued; the overhead is DuckDB's per-operator timers, a few percent. Views
are not profiled: creating a view runs nothing.

```yaml
# project.yml
performance:
  enabled: true            # false: record nothing at all
  capture_plans: true      # true | sampled | false
  sample_rate: 0.25        # sampled: share of builds that are profiled
  retention_days: 90       # rows older than this are deleted
  plan_retention: 20       # only the newest N builds of each model keep their plan
```

With `capture_plans: sampled`, a model is still always profiled while it has
fewer than three captured plans, and on the build right after a regression
that had none, so the next slow build comes with evidence.

## Regressions

At the end of every run, each build is compared with the same model's last
`regression_lookback` successful builds. A build is a regression when:

- its robust z-score against the history's **median and MAD** (median absolute
  deviation) reaches `regression_threshold`. A mean and standard deviation are
  dragged around by one cold-cache outlier; the median and MAD are not;
- it is at least `regression_min_ratio` times the median, and
- at least `regression_min_delta_ms` slower in absolute terms, so a 40 ms model
  that took 90 ms does not page anyone.

Durations are then normalised by rows (rows scanned, else rows read, else rows
written). A model that took twice as long because it read twice the data grew;
it did not regress, and is not reported. A reported regression says that it is
slower per row too.

Each regression carries a **plan diff** between a representative fast build
(the captured plan closest to the median from below) and the slow one: which
operators got slower, with their times and row counts on both sides, and any
join that changed type or condition (`HASH_JOIN` became `NESTED_LOOP_JOIN`).

Regressions are stored in `_havn.perf_regressions`, printed at the end of the
run, and sent as a `perf_regression` alert through the channels in `alerts:`
(Slack, webhook, log), the same way anomalies are.

```yaml
performance:
  regression_lookback: 20
  regression_min_history: 5
  regression_threshold: 3.5
  regression_min_ratio: 1.5
  regression_min_delta_ms: 500
  alert_on_regression: true
```

## Advice

Each rule explains itself and shows the evidence it used. All of them stay
quiet below their thresholds, so a project of small models gets no advice.

| Rule | Fires when | Suggests |
|---|---|---|
| `incremental_candidate` | A `table` with at least `big_table_rows` rows that never lost a row over its recent builds and grew by at most `append_growth` per run | `materialized=incremental` with a `unique_key` (a column the profile shows as unique) and `@watermark` on a timestamp column, or microbatch |
| `join_fanout` | A join's output is `fanout_ratio` times its largest input (and at least `fanout_min_rows`), or a nested-loop join / cross product over that many rows | Check the grain, add the missing join column, aggregate first |
| `materialize_view` | A view with a join, `GROUP BY`, window or `DISTINCT` read by `view_consumers` or more models | `materialized=table` |
| `unused_table` | A materialized model with no downstream model, no exposure, no dashboard or metric referencing it and no query in the audit or slow-query log for `unused_days` | Delete it, make it a view, or declare an exposure |
| `scan_small_slice` | A scan reads `big_table_rows` or more to keep at most `scan_selectivity` of them | Compare the raw column (not a function of it), or make the model incremental / partition the upstream |
| `order_by_non_final` | A top-level `ORDER BY` without `LIMIT` in a model other models read | Remove it |
| `distinct_large` | `SELECT DISTINCT` over `big_table_rows` or more | Fix the duplicates at their source, `GROUP BY` keys, or `QUALIFY row_number()` |
| `python_udf_hot_path` | A Python `@macro` called on `udf_rows` or more rows | A SQL macro (`CREATE MACRO` in `macros/*.sql`) |

```yaml
performance:
  advice:
    enabled: true
    big_table_rows: 1000000
    min_duration_ms: 1000      # builds faster than this get no cost advice
    fanout_ratio: 10
    fanout_min_rows: 100000
    scan_selectivity: 0.05
    view_consumers: 3
    unused_days: 14
    udf_rows: 100000
    append_growth: 0.10
```

An item is dismissed for good, or snoozed for some days, per model and rule
(`havn perf <model> --dismiss <rule> [--snooze N]`, or the buttons in the UI).
The state lives in `_havn.perf_advice_state`.

`unused_table` only sees queries that went through the web UI or the SQL API
(the audit log) and slow queries; `havn query` from the CLI is not audited.

## Critical path

`havn perf --critical-path` (and the chart on the Performance page) answers
"what did this run wait for". It walks back from the model that finished last
to the upstream that finished latest, and so on. Speeding up a model that is
not on that chain does not make the run shorter. Each step shows how long it
waited after its upstream finished: in a parallel run that is the tier barrier.

It also reports the **longest dependency chain** by build time: with unlimited
workers the run could not be shorter than that.

## API

| Endpoint | |
|---|---|
| `GET /api/perf/summary?days=7` | Slowest models, trend, regressions, advice, recent runs |
| `GET /api/perf/slowest?days=&limit=` | Models by median build time |
| `GET /api/perf/trend?models=a,b&days=` | Duration history per model |
| `GET /api/perf/regressions?days=&model=` | Recorded regressions with plan diffs |
| `GET /api/perf/advice?model=&include_dismissed=` | Advice items |
| `POST /api/perf/advice/state` | `{model, rule, status: dismissed\|snoozed\|open, days}` (write permission) |
| `GET /api/perf/models/{model}` | History, latest plan, regressions, advice |
| `GET /api/perf/builds/{id}` | One build with its plan |
| `GET /api/perf/diff?fast=&slow=` | Plan diff of two builds |
| `GET /api/perf/runs` | Recent runs |
| `GET /api/perf/runs/{id}/critical-path` | Critical path and longest chain |

See also [telemetry](telemetry) for exporting build metrics to
Prometheus, OpenTelemetry and OpenLineage.
