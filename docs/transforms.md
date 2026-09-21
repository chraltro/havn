# SQL Transforms

SQL transforms are the core of havn's data pipeline. Every `.sql` file in the `transform/` directory is a model that produces a table or view in DuckDB. Models are parsed, ordered by dependency, and executed automatically.

## File Structure

Models are organized into subdirectories that correspond to schemas:

```
transform/
  bronze/           # Schema: bronze
    customers.sql
    orders.sql
  silver/           # Schema: silver
    dim_customer.sql
    fact_orders.sql
  gold/             # Schema: gold
    customer_summary.sql
```

The folder name determines the default schema. A file at `transform/silver/dim_customer.sql` produces a table at `silver.dim_customer`.

## SQL Model Format

Every SQL model file starts with directive lines followed by a SELECT statement:

```sql
@config materialized=table, schema=silver
@description Customer dimension with order counts
@col customer_id: Unique customer identifier
@col order_count: Total number of orders
@assert row_count > 0
@assert unique(customer_id)
@assert no_nulls(customer_id)

SELECT
    c.customer_id,
    c.name,
    c.email,
    COUNT(o.order_id)   AS order_count,
    SUM(o.total_amount) AS lifetime_value
FROM bronze.customers c
LEFT JOIN bronze.orders o ON c.customer_id = o.customer_id
GROUP BY 1, 2, 3
```

Notice there is no `@depends_on` line: havn parses the `FROM` and `JOIN` clauses with `sqlglot` and adds `bronze.customers` and `bronze.orders` to the DAG automatically. You only need `@depends_on` when the parser can't see a reference (e.g. a table name passed through a function or string-built dynamically).

### Directives

Every directive supports two forms -- bare and parenthesised:

```sql
@config materialized=table, schema=gold
@config(materialized=table, schema=gold)
```

Both parse identically. Pick whichever reads better in your editor; the bare form is canonical in the kit and templates.

#### `@config`

Sets materialization, schema, and per-model engine settings:

```sql
@config materialized=table, schema=gold, unique_key=customer_id, incremental_strategy=delete+insert
```

| Key                     | Values                                  | Default                                       |
|-------------------------|-----------------------------------------|-----------------------------------------------|
| `materialized`          | `table`, `view`, `incremental`, `ephemeral`, `snapshot` | `view`                        |
| `schema`                | any valid schema name                   | folder name (e.g. `bronze` for `transform/bronze/`) |
| `unique_key`            | column name                             | none (required for incremental merges)        |
| `incremental_strategy`  | `delete+insert`, `merge`, `append`, `microbatch` | `delete+insert`                      |
| `incremental_filter`    | SQL expression (e.g. `event_time >= ...`) | none                                        |
| `partition_by`          | column name                             | none                                          |
| `watermark`             | column name                             | none                                          |
| `on_schema_change`      | `append_new_columns`, `ignore`, `fail`, `sync_all_columns` | `append_new_columns`       |
| `tags`                  | comma-separated labels                  | none                                          |
| `strategy`              | `check`, `timestamp` (snapshot only)    | `check`                                       |
| `updated_at`            | column name (snapshot, `strategy=timestamp`) | none                                     |
| `check_cols`            | `all` or comma-separated columns (snapshot, `strategy=check`) | `all`                   |
| `hard_deletes`          | `ignore`, `invalidate`, `new_record` (snapshot only) | `ignore`                         |
| `event_time`            | column name (microbatch only)           | none                                          |
| `batch_size`            | `hour`, `day`, `month`, `year` (microbatch only) | none                                 |
| `begin`                 | date or timestamp, UTC (microbatch only) | none                                         |
| `lookback`              | whole number of windows (microbatch only) | `1`                                         |

`tags` labels a model for the `tag:` selector:

```sql
@config materialized=table, tags=daily,finance
```

Tags are not part of the model's content hash, so retagging a hundred models
labels them rather than rebuilding a hundred tables. Each tag must be an
identifier (letters, digits, underscore, hyphen, not starting with a digit);
`havn check` reports anything else, which catches `tags = daily finance`
written with a space instead of a comma.

Keys outside this table are rejected by `havn check`, rather than being read as nothing: `@config materialised=table` used to build a view without complaining. An unrecognised key, and an unsupported value of `materialized`, are both validation errors, with a suggestion when the name is a near miss:

```
error  gold.orders  Unknown @config key 'materialised'. Did you mean 'materialized'? Known keys: ...
```

#### `@depends_on` (optional)

Declares upstream dependencies explicitly. Auto-extraction handles most cases:

```sql
@depends_on bronze.customers, bronze.orders
```

Use this when a dependency isn't visible in plain SQL (e.g. a model name interpolated through a Python macro). When you write `@depends_on`, havn uses your list and skips auto-extraction.

#### `@description`

One-line model description, surfaces in the catalog and UI:

```sql
@description Customer dimension table with lifetime metrics
```

#### `@col`

Per-column documentation. Repeat one line per documented column:

```sql
@col customer_id: Unique customer identifier
@col lifetime_value: Sum of all settled non-reversed order amounts
```

#### `@assert`

Defines data-quality assertions evaluated after the model builds. Repeat one line per assertion:

```sql
@assert row_count > 0
@assert unique(customer_id)
@assert no_nulls(email)
@assert accepted_values(status, ['active', 'inactive'])
@assert "total_amount >= 0"
```

See [Quality](quality) for the full assertion reference.

### Legacy comment syntax (still supported)

Older projects use SQL-comment-prefixed directives. They still parse correctly, so you can mix and match while migrating:

```sql
-- config: materialized=table, schema=silver
-- depends_on: bronze.customers
-- assert: row_count > 0
```

New code should use the `@`-prefixed form.

## Change Detection

havn uses SHA256 hashing to detect when a model's SQL has changed. On each `havn transform` run:

1. The SQL content is normalized (whitespace-insensitive)
2. A SHA256 hash is computed from the normalized SQL
3. The hash is compared against the stored hash in `_havn.model_state`
4. If the hash matches **and** the combined hash of all transitive upstreams hasn't changed, the model is **skipped**
5. Otherwise the model is **rebuilt**

Most `havn transform` runs only rebuild what has actually changed, making iterative development fast.

## DAG Ordering

Models are automatically sorted in topological order based on their dependencies (auto-extracted from SQL or declared via `@depends_on`). This ensures upstream tables exist before downstream models try to read from them.

```
bronze.customers ──┐
                   ├──> silver.dim_customer ──> gold.customer_summary
bronze.orders ─────┘
```

If a circular dependency is detected, `havn transform` will fail with an error. Use `havn validate` to check for circular dependencies without running transforms.

## Running Transforms

### Build All Models

```bash
havn transform
```

Only rebuilds models whose SQL has changed or whose upstream dependencies were rebuilt.

### Force Rebuild Everything

```bash
havn transform --force
```

Ignores change detection and rebuilds all models.

### Build Specific Models

```bash
havn transform gold.customer_summary silver.dim_customer
```

Builds only the named models. See [Selecting models](#selecting-models) for
everything else the argument accepts.

### Parallel Execution

```bash
havn transform --parallel --workers 4
```

Runs independent models concurrently. Models at the same level in the DAG (no dependencies between them) execute in parallel.

### Environment Override

```bash
havn transform --env prod
```

Uses the database path and settings from the `prod` environment. See [Environments](environments).

## Selecting models

Everywhere havn takes a set of models -- the positional argument to `havn
transform`, `--select`, `havn ls`, `targets:` in a job file, `targets` on
`POST /api/transform`, `?select=` on `GET /api/models`, the MCP tools -- it
takes the same grammar. One implementation, so a selector that works in one
place works in all of them.

### Names and wildcards

| Selector | Selects |
|---|---|
| `gold.orders` | exactly that model |
| `orders` | the model named `orders`, whatever schema it lives in |
| `gold.*` | every model in the `gold` schema |
| `gold.fct_*` | every `gold` model whose name starts with `fct_` |
| `*.customers` | every `customers` model, in any schema |
| `*` | every model |

Wildcards are fnmatch patterns and work anywhere in the name, in either half.

### Graph operators

| Selector | Selects |
|---|---|
| `+gold.orders` | the model and everything it depends on, transitively |
| `gold.orders+` | the model and everything that depends on it, transitively |
| `+gold.orders+` | both directions |
| `2+gold.orders` | the model and two hops of upstream |
| `gold.orders+1` | the model and one hop of downstream |
| `@silver.customers` | the model, its downstream, and every upstream of those |

`@x` is the "make this runnable from scratch" selector: it pulls in whatever
else the downstream models need, so the whole selection can be built in one
pass against an empty warehouse.

The operators combine with wildcards and methods: `+gold.*`, `tag:daily+`,
`@path:transform/silver/`.

### Methods

| Selector | Selects |
|---|---|
| `tag:daily` | models carrying `@config tags=daily` |
| `path:transform/gold/` | models whose file lives under that path |
| `path:transform/**/fct_*.sql` | a path glob, when a prefix is not enough |
| `config.materialized:incremental` | any `@config` key, matched by value |
| `config.unique_key:customer_id` | ... including the incremental settings |
| `state:modified` | models whose SQL or upstream hash changed since the last run |
| `state:modified+` | ... plus everything downstream of them |

`state:` compares against the same `_havn.model_state` hashes that change
detection uses, so `havn transform state:modified+` builds exactly what a
plain `havn transform` would build, and `havn ls state:modified+` tells you
in advance what that is. It needs a warehouse; in a project that has never
been built, everything counts as modified.

### Combining

A comma intersects. Each piece is resolved in full, operators and all, and
only models in every piece survive:

```bash
havn transform 'tag:daily,gold.*'          # daily models in gold
havn transform 'state:modified,tag:daily'  # daily models that changed
```

Several selectors union. Repeat the positional argument, or `--select`:

```bash
havn transform gold.orders silver.customers
havn transform -s tag:daily -s tag:hourly
```

`--exclude/-x` subtracts a second selection from the first:

```bash
havn transform 'gold.*' -x tag:expensive
havn transform state:modified+ -x 'gold.experimental_*'
```

Quote anything containing `*`, or the shell expands it first.

### Dry-running a selector

`havn ls` resolves a selector and prints what it matched, without building
anything:

```bash
$ havn ls '@silver.customers'
                    4 model(s)
 model               schema   materialized   tags
 bronze.customers    bronze   table          daily
 silver.customers    silver   table          daily
 gold.fct_orders     gold     incremental    finance
 gold.dim_customer   gold     table          -
```

`havn ls --names` prints bare names one per line for piping. A selector that
matched nothing is a warning and a non-zero exit, on both `havn ls` and
`havn transform`, so a typo does not look like a project that was already up
to date. `havn transform -v` prints which selector matched what before it
starts.

## Materialization

### View (Default)

```sql
@config materialized=view
```

Creates a view using `CREATE OR REPLACE VIEW ... AS SELECT ...`. The query runs on read, so data is always current but queries may be slower for complex logic.

### Table

```sql
@config materialized=table
```

Creates a persistent table using `CREATE OR REPLACE TABLE ... AS SELECT ...`. Data is stored on disk and queries are fast.

### Incremental

```sql
@config materialized=incremental, unique_key=event_id, incremental_strategy=delete+insert
```

Builds the table incrementally: on first run, it materialises the full result; on subsequent runs, only new rows (filtered by `incremental_filter` if provided) are appended or merged. See `incremental_strategy` in the `@config` table above.

Incremental runs that use a `unique_key` (the `delete+insert` and `merge` strategies) apply all their writes in a single transaction: the schema-evolution `ALTER`s that add newly appeared columns, the `DELETE`/`UPDATE` that clears the rows being replaced, and the `INSERT` that writes the new ones. If any of them fails, the whole run is rolled back and the target keeps exactly the data it had before. A source column whose type changed underneath you (say an integer that arrived as text) now fails the run cleanly instead of leaving the model with the deleted rows missing.

#### Schema changes: `on_schema_change`

Before it writes anything, an incremental run compares its query's columns against the target table on name **and** type, in both directions. What happens next is the model's `on_schema_change` policy:

```sql
@config materialized=incremental, unique_key=event_id, on_schema_change=sync_all_columns
```

| Policy | Added column | Removed column | Retyped column |
|---|---|---|---|
| `append_new_columns` (default) | `ALTER TABLE ADD COLUMN`, then written | Error, nothing written | Error, nothing written |
| `ignore` | Not added, not written | Left in place, not written | Error unless the cast is a lossless widening inside one type family |
| `fail` | Error, nothing written | Error, nothing written | Error, nothing written |
| `sync_all_columns` | `ALTER TABLE ADD COLUMN` | `ALTER TABLE DROP COLUMN` | `ALTER TABLE ALTER COLUMN ... TYPE` |

Two of those cells used to be silent data loss, which is why the default refuses them rather than carrying on:

- A **removed column** left the target diverging without a word. Rows written from then on got `NULL` while every older row kept its stale value.
- A **retyped column** was cast back into the target's old type on the way in. A `DOUBLE` of `20.5` written into an `INTEGER` column became `21`.

The error message names the column, both types, and the policy that would accept the change. Nothing is written when a policy refuses, so the target still holds exactly the rows it held before the run. To take the new shape wholesale instead, rebuild with `havn transform --force`.

Two limits worth stating plainly, both shared with dbt:

- **No option backfills old rows.** A column added to the target is `NULL` for every row that was already there; only rows written from this run on carry a value.
- **Only top-level columns are tracked.** A field that appears, disappears or changes type inside a `STRUCT`, `MAP` or `LIST` column is invisible to the comparison, because the column's own type is what is compared.

`sync_all_columns` depends on DuckDB accepting the `ALTER`. DuckDB refuses to drop or retype a column that a constraint or an index depends on; havn reports that as an error naming the column, and the run leaves the table untouched.

The policy is folded into the model's content hash, so changing it rebuilds the model's change-detection state on the next run rather than being picked up silently on the run after.

### Ephemeral

```sql
@config materialized=ephemeral
```

Builds nothing. Every model that references an ephemeral model gets its query prepended as a named CTE, and the reference rewritten to that CTE, so DuckDB sees one query. Use it for a step that exists to be read once by the models downstream, where a view would add a name to the warehouse that nobody should query directly.

```sql
-- transform/silver/active_orders.sql
@config materialized=ephemeral

SELECT * FROM bronze.orders WHERE status <> 'cancelled'
```
```sql
-- transform/gold/revenue.sql
@config materialized=table

SELECT customer_id, SUM(amount) AS revenue FROM silver.active_orders GROUP BY 1
```

`havn transform` builds that as:

```sql
WITH __havn_silver_active_orders AS (
  SELECT * FROM bronze.orders WHERE status <> 'cancelled'
)
SELECT customer_id, SUM(amount) AS revenue
FROM __havn_silver_active_orders AS active_orders
GROUP BY 1
```

What to expect:

- The model is reported as `inlined` rather than `built` or `skipped`, and gets a `model_state` row with `materialized_as = ephemeral` so change detection has something honest to read. Editing an ephemeral model rebuilds every model downstream of it, through the same transitive upstream hash every other model uses.
- Aliases survive. `FROM silver.active_orders o` stays `o`, and an unaliased reference keeps the model's own name, so `active_orders.amount` still resolves.
- Chains work. An ephemeral model that reads another is inlined first, and an ephemeral model's own CTEs are hoisted ahead of it under the same `__havn_` prefix, so two ephemeral models can both define a CTE called `base`.
- Switching a model to `ephemeral` drops the table or view it used to materialize, so a stale copy cannot answer queries that look like they hit the model.
- Consumers may be views, tables or incrementals. For an incremental consumer the inlined query is what builds both the first full load and every staging table after it.

Two limits:

- **`@assert` is rejected on an ephemeral model.** There is no table to query after the build. `havn check` says so and points you at moving the assertion to a consumer.
- **`{this}`, `@watermark` and `incremental_filter` are rejected.** They all name a target table, and an ephemeral model has none.

The trade, which dbt makes too: a DuckDB error inside a three-level inlined query reports a line the user never wrote. Naming each CTE after the model it came from is what makes the message readable.

### Microbatch incremental models

```sql
@config materialized=incremental, incremental_strategy=microbatch, event_time=event_at, batch_size=day, begin=2024-01-01
```

An ordinary incremental model runs its query once and writes whatever comes back. A microbatch model runs it once per time window and writes each window separately. That is what makes a three-year backfill survivable: it is a thousand small transactions instead of one enormous one, each window is recorded, and a failure at window 700 leaves 699 windows committed and resumes there next time.

The model does its own filtering, exactly as in dbt. `{start}` and `{end}` are substituted per window with typed timestamp literals:

```sql
-- transform/gold/events.sql
@config materialized=incremental, incremental_strategy=microbatch, event_time=event_at, batch_size=day, begin=2024-01-01

SELECT
    event_at,
    user_id,
    COUNT(*) AS events
FROM silver.events
WHERE event_at >= {start}
  AND event_at < {end}
GROUP BY 1, 2
```

havn does not append a `WHERE` clause for you. An event-time predicate pushed into the wrong place in a query with a `GROUP BY`, a window function or a join to a dimension changes the answer, and only the model's author knows where it belongs. `havn check` warns about a microbatch model whose SQL never mentions `{start}`, because every window would then read the whole source and the last one would win.

| Key | Values | Default |
|---|---|---|
| `event_time` | column name, the one windows are cut on | required |
| `batch_size` | `hour`, `day`, `month`, `year` | required |
| `begin` | first window, `2024-01-01` or `2024-01-01 06:00:00` | required |
| `lookback` | whole number of finished windows to redo each run | `1` |

All boundaries are UTC, and all of them are naive timestamps: a window is a range compared against the `event_time` column, and mixing an aware boundary with a naive column is a comparison DuckDB refuses rather than one it guesses at. `begin` is rounded down to its window, so `begin=2024-01-15` with `batch_size=month` starts at 2024-01-01.

#### What a run does

Per window, inside its own transaction: run the query, `DELETE` that window's rows from the target on `event_time`, `INSERT` the new ones, record the window as `done`. Because the window is deleted before it is written, re-running a window is a replace rather than a duplicate, which is also how a late-arriving row lands.

Between runs, the windows live in `_havn.batch_state`:

```bash
havn query "SELECT window_start, status, \"rows\" FROM _havn.batch_state WHERE model_path = 'gold.events' ORDER BY window_start"
```

The next ordinary run starts at the first window that is not `done`, or at the window after the last one recorded, and then goes `lookback` windows further back so rows that arrived late for a finished window are picked up. `lookback=1` is the default because an hourly or daily feed almost always has stragglers; `lookback=0` is the right setting when the source is genuinely immutable once written.

#### Backfilling an explicit range

```bash
havn transform gold.events --event-time-start 2024-01-01 --event-time-end 2024-03-01
```

Windows in that range are processed regardless of what state says, and `--event-time-end` is exclusive. Either flag may be given alone: an open start falls back to the model's `begin`, an open end to now. The same two fields exist on `POST /api/transform` as `event_time_start` and `event_time_end`.

`havn transform gold.events --force` reprocesses every window from `begin`.

An explicit range also substitutes `{start}` and `{end}` inside an `incremental_filter` on models that are *not* microbatch, which is a way to scope one ordinary incremental run to a date range without editing the model. Without the flags the placeholders are left alone rather than guessed at.

#### What to expect

- **The window holding "now" is processed too**, even though it is not over. The alternative is that data written in the last hour waits for the hour to turn.
- **Columns evolve per window** under the model's `on_schema_change` policy, the same as any incremental model. A column added part-way through a backfill is `NULL` for the windows already written.
- **`incremental_filter` and `@watermark` are rejected.** The batch window is already the filter; a second one would narrow every window and leave gaps nothing refills.
- **A window count above 100,000 is refused** rather than attempted. `begin=1970-01-01` with `batch_size=hour` is close to half a million windows and is almost always a typo.
- **Failures name the window.** The error says which window of how many failed, how many are committed, and that re-running resumes there.

### Snapshot models (SCD2)

```sql
@config materialized=snapshot, unique_key=customer_id, strategy=check
```

A snapshot model keeps the history of a source table instead of its current state. Every run compares what the query returns now against what the history table already holds, and writes a new row version for anything that changed. Rows are never updated in place except to close them, so yesterday's answer to "what tier was this customer on?" stays answerable forever.

**Two features share the word "snapshot", and they are not the same thing.** *Snapshot models* (this section) keep row-level history of one table inside the warehouse: a type 2 slowly changing dimension, built by `havn transform` like any other model. `havn snapshot` and `havn rewind` (see `docs/versioning.md`) are the other thing: whole-warehouse restore points that let you put the entire project back the way it was after a bad run. One is a modeling pattern, the other is an undo button.

#### What the table looks like

The target holds the query's own columns plus four meta columns:

| Column | Type | Meaning |
|---|---|---|
| `valid_from` | `TIMESTAMP` | when this version became the truth |
| `valid_to` | `TIMESTAMP` | when it stopped, `NULL` while it is current |
| `is_current` | `BOOLEAN` | the one live version per key |
| `row_hash` | `VARCHAR` | hash of the tracked columns, used for change detection |

With `hard_deletes=new_record` a fifth column, `is_deleted`, marks tombstone rows.

Rename any of them project-wide in `project.yml`, which is what a project migrating from dbt wants:

```yaml
snapshots:
  meta_columns:
    valid_from: dbt_valid_from
    valid_to: dbt_valid_to
    is_current: dbt_is_current
    row_hash: dbt_scd_id
    is_deleted: dbt_is_deleted
  valid_to_current: "'9999-12-31'::TIMESTAMP"
```

`valid_to_current` puts a sentinel date in the open row's `valid_to` instead of `NULL`. BI tools filter `valid_to > today` more comfortably than they handle a three-valued comparison against `NULL`.

#### Strategies

`strategy=check` (the default) hashes the tracked columns and compares the hash. `check_cols` narrows what counts as a change:

```sql
@config materialized=snapshot, unique_key=customer_id, check_cols=tier,region
```

Only `tier` and `region` are watched; a new `last_seen_at` on every run does not manufacture a version. The default, `check_cols=all`, hashes every non-key column.

`strategy=timestamp` trusts the source's own change clock:

```sql
@config materialized=snapshot, unique_key=order_id, strategy=timestamp, updated_at=modified_at
```

A row is a new version when its `updated_at` is later than the stored `valid_from`, and the version is dated from `updated_at` rather than from the moment of the run. Replaying an old extract therefore lands the version where it belongs in history instead of at the top. `row_hash` is still written under this strategy, so the content of every version is on record even though the decision was made on the clock.

#### Hard deletes

`hard_deletes` decides what happens to a key that stopped appearing in the source:

- `ignore` (default): the last version stays current. Absence is treated as "no news", which is the right reading when the query is filtered or the extract is partial.
- `invalidate`: the current version is closed, with `valid_to` set to the run timestamp and `is_current` false. The key has no current row until it comes back.
- `new_record`: the current version is closed and a tombstone row is appended with the same values, `is_deleted = true` and `is_current = true`. Downstream models can then see *that* a key was deleted and when, not merely that it stopped being current.

A key that returns after a delete opens a fresh live version under every policy.

#### Running them

```bash
havn transform silver.dim_customer
```

- **Replaying is free.** A run whose source has not changed writes nothing. A to B and back to A produces three versions, because coming back is a change like any other.
- **Duplicate keys are refused before any write.** If the query returns a key more than once, the run fails, names the key, and leaves history exactly as it was. DuckDB's `UPDATE ... FROM` picks an arbitrary row when the source matches more than once, so the alternative is silently storing whichever version the scan reached first. The usual fix is a `QUALIFY row_number() OVER (PARTITION BY key ORDER BY ...) = 1` in the query.
- **`--force` re-runs the merge; it never drops history.** Forcing a rebuild must not be a way to lose years of versions by accident. If you really do want to start over, drop the table: `havn query "DROP TABLE silver.dim_customer"`, then run the model again.
- **New source columns are appended**, `NULL` for every row already in history, exactly as an incremental model does it. A removed or retyped column is an error, because a snapshot cannot rewrite what it already wrote.
- **`@assert`, `@grain` and profiling work** the same as for a `table` model; they run against the history table after the merge, so an assertion can talk about `is_current` directly.
- **`incremental_filter` and `@watermark` are rejected.** A snapshot has to read the source's current state in full: filtering the read would look exactly like a hard delete of every filtered-out row.
- **All timestamps are the warehouse's `current_timestamp`** unless `strategy=timestamp` dates them from the source.

#### Coming from dbt

The config names and values are the same ones dbt uses, so a migration is a search and replace rather than a translation table: `unique_key`, `strategy` (`check` / `timestamp`), `updated_at`, `check_cols`, `hard_deletes` (`ignore` / `invalidate` / `new_record`). Three differences worth knowing:

- Snapshots live in `transform/` with every other model rather than in their own `snapshots/` directory, and they are selected, tagged and scheduled like any model.
- The meta columns are called `valid_from`, `valid_to`, `is_current` and `row_hash` by default. `snapshots.meta_columns` in `project.yml` is the equivalent of dbt's `snapshot_meta_column_names`, and `valid_to_current` is the equivalent of `dbt_valid_to_current`.
- There is no `dbt_updated_at` column. Under `strategy=timestamp` the source's `updated_at` is `valid_from`, which is the value that column held anyway.

## Plain SQL -- No Templating

havn uses plain SQL with no Jinja, no macros, and no templating language. This means:

- SQL files work directly in any DuckDB client (just delete the `@config` line first)
- No learning curve beyond standard SQL
- Full DuckDB syntax support (window functions, CTEs, UNNEST, etc.)
- Easy to test and debug

If you need dynamic behavior, use Python ingest/export scripts or parameterize via environment variables in `project.yml`. For reusable scalar logic, register a Python macro and call it directly in SQL -- see [Macros](macros).

## Validation

Check your models for errors without running them:

```bash
havn check
```

This validates:
- SQL syntax (via sqlglot AST parsing)
- Auto-extracted and explicit `@depends_on` references resolve to known models, seeds, or sources
- Column references against known upstream table schemas
- Inline assertions against live data (if warehouse exists)
- YAML contracts from `contracts/`

## Related Pages

- [Pipelines](pipelines) -- Run transforms as part of multi-step streams
- [Quality](quality) -- Data quality assertions and profiling
- [Lineage](lineage) -- Column-level lineage and impact analysis
- [Seeds](seeds) -- Load CSV reference data
- [CLI Reference](cli-reference) -- Full command reference for `havn transform`
