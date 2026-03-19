# SQL Transforms

SQL transforms are the core of havn's data pipeline. Every `.sql` file in the `transform/` directory is a model that produces a table or view in DuckDB. Models are parsed, ordered by dependency, and executed automatically with change detection and parallel execution.

## Web UI Experience

### Editing Transforms in the Develop Tab

1. Go to the **Develop** tab and click **Editor**
2. The **file tree** on the left shows your project structure -- expand `transform/` to see all models organized by schema (bronze, silver, gold)
3. Click any `.sql` file to open it in the **Monaco editor** with DuckDB syntax highlighting, autocomplete, and formatting
4. Changes are automatically saved when you edit
5. Click **Run** (or use the Run menu) to execute transforms and see results in the **Output Panel** below the editor

### DAG Visualization

1. In the **Develop** tab, click **DAG** to see an interactive dependency graph of all your SQL models
2. Each node represents a model, colored by schema (bronze, silver, gold)
3. **Hover** over any node to highlight its upstream and downstream dependencies
4. The DAG also shows **seeds**, **sources**, **ingest scripts**, and **exposures** in the full view
5. Use the DAG to understand data flow and debug circular dependencies

### Running Transforms from the UI

Use the **Run menu** dropdown in the toolbar:

- **Transform** -- Runs `havn transform` (with change detection)
- **Transform (Force)** -- Runs `havn transform --force` (rebuilds everything)
- **Lint** -- Checks SQL style
- **Check** -- Validates models, assertions, and contracts

The **Output Panel** shows real-time streaming results as models build, with status indicators (skip, done, fail) and timing for each model.

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

Every SQL model file starts with metadata comments followed by a SELECT statement:

```sql
-- config: materialized=table, schema=silver
-- depends_on: bronze.customers, bronze.orders
-- description: Customer dimension with order counts
-- col: customer_id: Unique customer identifier
-- col: order_count: Total number of orders
-- assert: row_count > 0
-- assert: unique(customer_id)
-- assert: no_nulls(customer_id)

SELECT
    c.customer_id,
    c.name,
    c.email,
    COUNT(o.order_id) AS order_count,
    SUM(o.total_amount) AS lifetime_value
FROM bronze.customers c
LEFT JOIN bronze.orders o ON c.customer_id = o.customer_id
GROUP BY 1, 2, 3
```

### Config Comments

#### `-- config:`

Sets materialization and schema:

```sql
-- config: materialized=table, schema=gold
```

| Option | Values | Default | Description |
|--------|--------|---------|-------------|
| `materialized` | `table`, `view`, `incremental` | `table` | How the model is stored |
| `schema` | any valid name | folder name | Override the target schema |

#### `-- depends_on:`

Declares upstream dependencies for DAG ordering:

```sql
-- depends_on: bronze.customers, bronze.orders
```

Dependencies are used to determine execution order. If omitted, havn auto-detects table references from the SQL using AST parsing (via sqlglot), but explicit declaration is recommended for clarity.

#### `-- description:`

Documents the model:

```sql
-- description: Customer dimension table with lifetime metrics
```

Descriptions appear in the auto-generated documentation, the Tables browser, and the DAG hover tooltips.

#### `-- col:`

Documents individual columns:

```sql
-- col: customer_id: Unique customer identifier
-- col: lifetime_value: Sum of all order amounts
```

Column descriptions flow into the documentation generator and the Tables panel column view.

#### `-- assert:`

Defines data quality assertions evaluated after the model builds:

```sql
-- assert: row_count > 0
-- assert: unique(customer_id)
-- assert: no_nulls(email)
-- assert: accepted_values(status, ['active', 'inactive'])
-- assert: "total_amount >= 0"
```

See [Quality](quality) for the full assertion reference.

## Materialization Types

### Table (Default)

```sql
-- config: materialized=table
```

Creates a persistent table using `CREATE OR REPLACE TABLE ... AS SELECT ...`. Data is stored on disk and queries are fast.

### View

```sql
-- config: materialized=view
```

Creates a view using `CREATE OR REPLACE VIEW ... AS SELECT ...`. The query runs on read, so data is always current but queries may be slower for complex logic.

### Incremental

```sql
-- config: materialized=incremental
```

Appends new rows to an existing table instead of replacing it. On first run, creates the table. On subsequent runs, inserts only new rows. Use this for large, append-heavy tables where a full rebuild would be too expensive.

## Change Detection

havn uses SHA256 hashing to detect when a model's SQL has changed. On each `havn transform` run:

1. The SQL content is normalized (whitespace-insensitive)
2. A SHA256 hash is computed from the normalized SQL
3. The hash is compared against the stored hash in `_dp_internal.model_state`
4. If the hash matches and upstream models haven't changed, the model is **skipped**
5. If the hash differs or any upstream dependency was rebuilt, the model is **rebuilt**

This means most `havn transform` runs only rebuild what has actually changed, making iterative development fast.

## DAG Ordering

Models are automatically sorted in topological order based on their `-- depends_on:` declarations. This ensures upstream tables exist before downstream models try to read from them.

```
bronze.customers ---+
                    +--> silver.dim_customer --> gold.customer_summary
bronze.orders ------+
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

Builds only the specified models (and their upstream dependencies if needed).

### Parallel Execution

Parallel execution is enabled by default. Models at the same level in the DAG (no dependencies between them) execute concurrently. To control the number of workers:

```bash
havn transform --workers 8
```

To disable parallel execution and run models sequentially:

```bash
havn transform --sequential
```

### Skip Pre-Transform Validation

```bash
havn transform --skip-check
```

Skips the pre-transform validation step (SQL syntax, dependency checks) for faster execution when you are confident your models are valid.

### Environment Override

```bash
havn transform --env prod
```

Uses the database path and settings from the `prod` environment. See [Environments](environments).

## Auto-Profiling

After each model builds, havn automatically computes column-level statistics:

- **Row count** -- Total number of rows
- **Column count** -- Number of columns
- **Null percentages** -- Percentage of NULL values per column
- **Distinct counts** -- Number of distinct values per column

These profiles are stored in `_dp_internal.model_profiles` and are visible in the Quality panel and Tables browser.

## Plain SQL -- No Templating

havn uses plain SQL with no Jinja, no macros, and no templating language. This means:

- SQL files work directly in any DuckDB client
- No learning curve beyond standard SQL
- Full DuckDB syntax support (window functions, CTEs, UNNEST, etc.)
- Easy to test and debug

If you need dynamic behavior, use Python ingest/export scripts or parameterize via environment variables in `project.yml`.

## Validation

Check your models for errors without running them:

```bash
havn check
```

This validates:
- SQL syntax (via sqlglot AST parsing)
- `-- depends_on:` references exist in the DAG, seeds, or sources
- Column references against known upstream table schemas
- Inline assertions against live data (if warehouse exists)
- YAML contracts from `contracts/`

## Promoting Queries to Models

Convert ad-hoc SQL queries into proper transform models:

```bash
# From a SQL string
havn promote "SELECT * FROM bronze.data WHERE active = true" --name active_data --schema silver

# From a notebook cell
havn promote notebooks/explore.dpnb --name my_model --schema silver

# From a SQL file
havn promote query.sql --name my_model --schema gold
```

This auto-detects dependencies, creates the `.sql` file with proper config comments, and validates the model fits in the DAG.

## API Reference

### Run Transforms

```bash
POST /api/transform
Content-Type: application/json

{"targets": null, "force": false}
```

### List Models

```bash
GET /api/models
```

Returns all models with metadata: name, schema, materialization, dependencies, content hash, and file path.

### Create a Model

```bash
POST /api/models/create
Content-Type: application/json

{"name": "my_model", "schema_name": "silver", "materialized": "table", "sql": "SELECT 1"}
```

### Validate Models

```bash
POST /api/check
```

Runs model validation, inline assertions, and YAML contracts.

### Diff Models

```bash
POST /api/diff
Content-Type: application/json

{"targets": null, "target_schema": null, "full": false}
```

Compares SQL output against materialized tables to show what would change on rebuild.

## Related Pages

- [Pipelines](pipelines) -- Run transforms as part of multi-step streams
- [Quality](quality) -- Data quality assertions and profiling
- [Lineage](lineage) -- Column-level lineage and impact analysis
- [Seeds](seeds) -- Load CSV reference data
- [Versioning](versioning) -- Snapshot and restore model data
- [CLI Reference](cli-reference) -- Full command reference for `havn transform`
