# Lineage

havn provides column-level lineage tracking using AST-based SQL analysis. Lineage traces how each output column in a model maps back to its source columns in upstream tables, through CTEs, joins, subqueries, and expressions.

## How Lineage Works

havn uses sqlglot to parse SQL models into an Abstract Syntax Tree (AST) and traces column references through:

- Direct column references (`SELECT c.name FROM customers c`)
- Aliased expressions (`SELECT c.first_name || ' ' || c.last_name AS full_name`)
- CTEs (`WITH cte AS (SELECT ...) SELECT cte.col FROM cte`)
- Subqueries
- Window functions
- CASE expressions
- UNION ALL queries
- `SELECT *` expansion (when a database connection is available)

## Viewing Lineage

### CLI

```bash
havn lineage gold.earthquake_summary
```

Output:

```
Column lineage for gold.earthquake_summary:

  region <- silver.earthquake_events.region
  total_events <- (computed)
  avg_magnitude <- silver.earthquake_events.magnitude
  max_magnitude <- silver.earthquake_events.magnitude
  latest_event <- silver.earthquake_events.event_time
```

JSON output:

```bash
havn lineage gold.earthquake_summary --json
```

### API

Get lineage for a specific model:

```bash
curl http://localhost:3000/api/lineage/gold.earthquake_summary
```

Returns:

```json
{
  "model": "gold.earthquake_summary",
  "columns": {
    "region": [
      {"source_table": "silver.earthquake_events", "source_column": "region"}
    ],
    "total_events": [],
    "avg_magnitude": [
      {"source_table": "silver.earthquake_events", "source_column": "magnitude"}
    ]
  },
  "depends_on": ["silver.earthquake_events"]
}
```

Get lineage for all models:

```bash
curl http://localhost:3000/api/lineage
```

## Table-Level Dependencies

### Auto-Extracted from SQL

By default, havn parses your `FROM` and `JOIN` clauses with `sqlglot` and uses those references to build the DAG. The example model below depends on `bronze.customers` and `bronze.orders` automatically, no directive required:

```sql
@config materialized=table, schema=silver

SELECT c.customer_id, COUNT(o.order_id) AS order_count
FROM bronze.customers c
LEFT JOIN bronze.orders o USING (customer_id)
GROUP BY 1
```

### `@depends_on` Override

When the parser can't see a reference (a model name passed through a function or built up in a string), declare it explicitly:

```sql
@depends_on bronze.customers, bronze.orders
```

When `@depends_on` is present, havn uses your list and skips auto-extraction.

(Legacy `-- depends_on: ...` comment syntax still parses for back-compat.)

### DAG Visualization

The web UI displays an interactive dependency graph:

```bash
havn serve
# Navigate to the DAG tab
```

Or via API:

```bash
# Basic DAG (models only)
curl http://localhost:3000/api/dag

# Full DAG (models + seeds + sources + exposures + ingest scripts)
curl http://localhost:3000/api/dag/full
```

The DAG response includes:

- **Nodes** -- Models (table/view), sources, seeds, ingest scripts, exposures
- **Edges** -- Dependencies between nodes

## Impact Analysis

Impact analysis answers: "If I change this model or column, what downstream models are affected?"

Each affected column carries the clause the reference was found in. A hit with
`clause: "select"` comes from column lineage and names the downstream *output*
column. A hit in any other clause (`where`, `join`, `group`, `having`,
`qualify`, `order`, `window`) comes from the reference index and names the
upstream column as written, because a filter or a join key produces no output
column of its own. Repeated mentions inside one clause are reported once.

### CLI

```bash
# Model-level impact
havn impact silver.customers

# Column-level impact
havn impact silver.customers --column email
```

Output:

```
Impact analysis for silver.customers
  Column: email

  3 downstream model(s) affected:
    gold.customer_summary
    gold.email_analytics
    gold.marketing_segments

  Affected columns:
    gold.customer_summary.email
    gold.email_analytics.email_domain
    gold.marketing_segments.email  (where)

  Impact chain:
    silver.customers -> gold.customer_summary, gold.email_analytics
    gold.email_analytics -> gold.marketing_segments
```

JSON output:

```bash
havn impact silver.customers --column email --json
```

### API

```bash
# Model-level impact
curl http://localhost:3000/api/impact/silver.customers

# Column-level impact
curl "http://localhost:3000/api/impact/silver.customers?column=email"
```

Returns:

```json
{
  "model": "silver.customers",
  "column": "email",
  "downstream_models": [
    "gold.customer_summary",
    "gold.email_analytics"
  ],
  "affected_columns": [
    {"model": "gold.customer_summary", "column": "email", "clause": "select"},
    {"model": "gold.email_analytics", "column": "email_domain", "clause": "select"},
    {"model": "gold.marketing_segments", "column": "email", "clause": "where"}
  ],
  "impact_chain": {
    "silver.customers": ["gold.customer_summary", "gold.email_analytics"]
  }
}
```

## CTE Tracing

havn traces lineage through CTEs correctly. For example:

```sql
WITH customer_orders AS (
    SELECT
        c.customer_id,
        c.name,
        COUNT(o.order_id) AS order_count
    FROM bronze.customers c
    LEFT JOIN bronze.orders o ON c.customer_id = o.customer_id
    GROUP BY 1, 2
)
SELECT
    customer_id,
    name,
    order_count,
    CASE WHEN order_count > 10 THEN 'VIP' ELSE 'regular' END AS tier
FROM customer_orders
```

Lineage for `name` correctly traces through the CTE back to `bronze.customers.name`.

## Full DAG Components

The full DAG (`/api/dag/full`) includes all project components:

| Node Type | Description |
|-----------|-------------|
| `source` | External data sources declared in `sources:` |
| `seed` | CSV files from `seeds/` |
| `ingest` | Python ingest scripts from `ingest/` |
| `import` | Data imported via the import wizard |
| `table` | SQL models materialized as tables |
| `view` | SQL models materialized as views |
| `exposure` | Downstream consumers declared in `exposures:` |

Ingest scripts are linked to their target tables by scanning the script content for `CREATE TABLE` and `INSERT INTO` patterns.

## Model Notebook View

The API provides a notebook-style view for each model that combines lineage, SQL source, sample data, and upstream/downstream relationships:

```bash
curl http://localhost:3000/api/models/gold.earthquake_summary/notebook-view
```

Returns the SQL source, sample data rows, column lineage, upstream dependencies, and downstream consumers in a single response.

## What Lineage Gets Right

Every construct below is pinned by `tests/test_lineage_conformance.py`. That
suite checks each case twice: the output column set against DuckDB's own
`DESCRIBE` of the built model, and the per-column source mapping against a
written-out expectation. A construct listed as exact is asserted exactly, so
a change either keeps it working or says out loud which one it broke.

### Exact

| Construct | Note |
|---|---|
| CTE chain | Traced through every step. |
| Nested subquery in `FROM` | The subquery alias is not reported as a source table. |
| Window function | `PARTITION BY` and `ORDER BY` columns are sources of the window's output column. |
| `UNION` / `UNION ALL` | Every branch, not only the first. |
| `UNION ALL BY NAME` | Matched by name across branches. |
| `SELECT *` over a join | A name present on both sides stays two columns, named the way DuckDB names them when the model is built (`customer_id`, `customer_id_1`). |
| `SELECT * EXCLUDE (x)` | `x` is gone from the output. |
| `SELECT * REPLACE (expr AS x)` | The replacement expression is the source of `x`, not the replaced column. |
| `unnest` | The list column is the source. |
| Correlated subquery in `SELECT` | The subquery's own columns, not the whole outer scope. |
| `QUALIFY` | |
| `ASOF JOIN` | |
| `COLUMNS('regex')` | Needs a connection, see below. |
| `GROUP BY ALL` | `COUNT(*)` correctly has no column source. |
| Recursive CTE | An anchor of constants correctly has no column source. |
| `UNPIVOT` | The `NAME` and `VALUE` outputs map to every unpivoted column. |

### Approximate

| Construct | What it does |
|---|---|
| Struct dot `s.field`, bracket `s['field']` | Resolves to the struct column `s`. The field name survives in the output column name, but lineage does not point inside the struct. |
| `PIVOT` | The grouping columns are exact. Each pivoted output column maps to the `ON` and `USING` columns together; per-value lineage through a pivot is not planned. |
| `LATERAL` | Over-broad, never wrong. sqlglot gives a `LATERAL` body no scope of its own, so the correlation predicate's columns come along with the projection. The real source is always in the result. |

### Not traced by lineage at all

- **Columns used only in `WHERE`, `JOIN ... ON`, `GROUP BY`, `HAVING`, `QUALIFY` or `ORDER BY`.** They feed no output column, so by construction they have no lineage entry. Impact analysis finds them through the reference index instead and labels each hit with the clause it was found in, so a filter-only column is still reported as affected.
- **Dynamic SQL.** SQL built up as a string in a Python ingest script is invisible.

## What Lineage Needs

- **A column catalog, for `SELECT *`.** Star expansion needs to know what the star stands for. Pass a live connection, or a catalog from `fetch_column_catalog()`, or an inferred schema from the bind pass. With none of the three, an unexpandable star is reported as a single source with `"resolved": false` and a `source_column` of `*` rather than being guessed at.
- **A bound or built upstream, for types and unbuilt models.** The catalog only knows models that have been built. `havn validate` runs the bind pass, which resolves a model's schema without building it; that schema feeds lineage for the same models.
- **A live connection, for `PIVOT` and `COLUMNS('regex')`.** Only the database can enumerate the output columns of a dynamic pivot or a regex column selector, because the column names depend on the data and on the catalog. A pre-fetched catalog is not enough for these two; every other construct gives identical results from a catalog and from a connection.

## Related Pages

- [Refactoring](refactoring.md) -- renaming a column across the models that read it
- [Transforms](transforms) -- SQL model format and `@depends_on` overrides
- [Quality](quality) -- Using lineage for data quality
- [Sources](sources) -- Sources in the DAG
- [Seeds](seeds) -- Seeds in the DAG
- [API Reference](api-reference) -- Lineage and impact API endpoints
