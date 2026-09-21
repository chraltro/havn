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
| `materialized`          | `table`, `view`, `incremental`          | `view`                                        |
| `schema`                | any valid schema name                   | folder name (e.g. `bronze` for `transform/bronze/`) |
| `unique_key`            | column name                             | none (required for incremental merges)        |
| `incremental_strategy`  | `delete+insert`, `merge`, `append`      | `delete+insert`                               |
| `incremental_filter`    | SQL expression (e.g. `event_time >= ...`) | none                                        |
| `partition_by`          | column name                             | none                                          |
| `watermark`             | column name                             | none                                          |
| `tags`                  | comma-separated labels                  | none                                          |

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
