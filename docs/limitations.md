# What havn Supports

This page lists, feature by feature, what havn does today and what it does not.
It is updated with each release, so if something here looks out of date against
the version you are running, it is a bug in this page. Missing something you
need, or found a gap we have not written down? Open an issue at
[github.com/chraltro/havn/issues](https://github.com/chraltro/havn/issues).

Status words mean exactly this:

| Word | Meaning |
|---|---|
| **Supported** | Works today, covered by tests, safe to build on. |
| **Partial** | Works with a stated limitation. Read the note before you rely on it. |
| **Not supported** | Does not work and is not being built right now. The note says why. |
| **Planned** | Designed and queued, but not in a release yet. |

## SQL features

havn hands your SQL to DuckDB unchanged, so anything DuckDB can run, havn can
build. What varies is how well havn's own analysis (dependency extraction for
the DAG, and column-level lineage) understands the construct. Dependency
extraction is what decides build order, and it handles every construct below.

| Feature | Status | Notes |
|---|---|---|
| CTEs | Supported | Build order and column lineage both correct through CTE chains. |
| Window functions | Supported | Including `QUALIFY`. |
| `UNION` / `UNION ALL` | Supported | Builds correctly. Column lineage only reports the first branch, see the lineage table below. |
| `SELECT * EXCLUDE (...)` | Supported | Builds correctly. Excluded columns are still reported by column lineage. |
| `SELECT * REPLACE (...)` | Supported | Builds correctly. The replacement expression is invisible to column lineage. |
| Struct and list columns | Supported | Builds correctly. `s.field` confuses column lineage, `s['field']` does not. |
| `PIVOT` / `UNPIVOT` | Supported | Builds correctly and the DAG sees the source table. Column lineage returns nothing. |
| `QUALIFY` | Supported | Builds correctly, column lineage correct. |
| `ASOF JOIN` | Supported | Builds correctly, dependencies detected. |
| Python macros (`@macro`) | Supported | Registered as DuckDB scalar UDFs, callable anywhere in a model. |
| Table macros (`@table_macro`) | Supported | Called with `FROM`. Output columns come from the declared `schema=`. |
| `CREATE MACRO` SQL macros | Supported | `.sql` files in `macros/` are registered alongside the Python ones. |
| Jinja templating | Not supported | Deliberate. Models are plain SQL; reusable logic goes in a macro instead. |

## Materializations

Set with `@config materialized=...` at the top of a model.

| Materialization | Status | Notes |
|---|---|---|
| `view` | Supported | |
| `table` | Supported | Rebuilt when the model's content hash or any upstream hash changes. |
| `incremental`, `delete+insert` | Supported | Default strategy. Needs `unique_key`. |
| `incremental`, `merge` | Supported | Needs `unique_key`. Updates every matched row rather than only changed ones. |
| `incremental`, `append` | Supported | No `unique_key` needed and no dedup. |
| `on_schema_change` policy | Planned | Today a column added upstream is added to the target automatically, but a removed column and a retyped column are not handled: the build either diverges silently or fails partway. The `fail` / `ignore` / `append_new_columns` / `sync_all_columns` policy is designed and queued. |
| `ephemeral` | Planned | Will inline the model into its consumers as a CTE rather than materializing it. |
| `snapshot` (SCD2 history) | Planned | Will add `valid_from` / `valid_to` / `is_current` tracking with timestamp and check strategies. Note `havn snapshot` today is a different feature: it saves a whole-project state you can rewind to. |
| `microbatch` | Not supported | Deferred. The batch loop, per-batch state and backfill CLI are all missing, and no user has asked for them yet. Incremental models with an `incremental_filter` cover the common case. |

## Validation

`havn validate` checks project structure, config and the DAG before you build.

| Check | Status | Notes |
|---|---|---|
| DAG construction, cycle detection | Supported | Circular dependencies are reported by name. |
| Table and column name checking | Partial | Columns are checked against the warehouse catalog. Any upstream that has not been built yet is skipped, so on a fresh warehouse a bad column name can pass validation and fail at build time. |
| Type checking | Planned | Nothing checks types today. The design delegates to the DuckDB binder against a shadow catalog, which resolves types and catches bind errors (wrong arity, unknown function, missing column, ambiguous reference, set-operation mismatch) without reading a row. It will not catch value-domain failures such as `CAST('abc' AS INTEGER)`; those still fail at run time. |
| Column-level lineage | Partial | See the per-construct table below. |
| Contracts | Partial | See [Testing](#testing). |
| Unknown `@config` keys | Partial | Unrecognised keys are ignored rather than rejected, so a typo such as `materialised=table` silently builds a view. |

### Column lineage, per construct

Column lineage is what powers the lineage view and impact analysis. It is
accurate for the common shapes and wrong in specific ones. Until the rewrite
lands, treat a lineage result as a strong hint, not a guarantee.

| Construct | Status | Notes |
|---|---|---|
| CTE chain | Supported | |
| Window function | Supported | |
| `QUALIFY` | Supported | |
| `unnest` | Supported | |
| Correlated subquery | Supported | Over-broad: reports more sources than strictly contribute. |
| Struct bracket `s['field']` | Partial | Correct, but resolves to the struct column rather than the field. |
| Nested subquery in `FROM` | Partial | The subquery alias is reported as if it were a source table. |
| `UNION ALL` | Partial | Only the first branch is traced; later branches are dropped. |
| `SELECT *` over a join | Partial | A column name present in both sides collapses to a single source. |
| `SELECT * EXCLUDE (x)` | Partial | `x` is still reported as an output column. |
| `SELECT * REPLACE (expr AS x)` | Partial | The replacement expression is never inspected. |
| Struct dot `s.field` | Partial | `s` is reported as a source table. |
| `PIVOT` | Not supported | Returns an empty result. Precise per-column lineage through a pivot is not planned; mapping output columns to the pivoted source is. |

## Running models

| Feature | Status | Notes |
|---|---|---|
| `havn transform` (whole project) | Supported | Content-hash change detection rebuilds only what changed, `--force` rebuilds everything. |
| Targets on `havn transform` | Partial | Takes exact model names only (`gold.orders` or `orders`). Graph operators are not accepted here yet, so upstream and downstream are not pulled in. |
| Job selectors in `orchestration/*.yml` | Supported | Full selector grammar: `+model`, `model+`, `+model+`, `schema.*`, and ingest/export script paths, with `resolve: upstream`. |
| Glob selectors (`gold.fct_*`) | Planned | Only whole-schema `schema.*` matches today. |
| Selector methods (`tag:`, `path:`, `state:modified`) | Planned | The underlying change detection exists; the selector syntax does not. |
| Environments | Supported | `havn env use <name>` switches the database path and connection overrides declared in `project.yml`. |
| Defer to another environment | Planned | Will attach a prod warehouse read-only so a dev run reads models it has not built. DuckDB's file lock means it cannot attach while another process holds that file open for writing; that constraint is not removable. |
| Scheduler | Supported | Cron schedules in job files, run by `havn schedule`, plus `havn watch` for rebuild on file change. |
| Parallel execution | Supported | Independent models run concurrently by default; `--sequential` turns it off. |

## Editor (web UI)

The Monaco editor in `havn serve`.

| Feature | Status | Notes |
|---|---|---|
| Autocomplete | Supported | Schemas, tables, columns and macros. |
| Hover | Partial | Shows macro signatures and docstrings, and table and column metadata. Inferred column types arrive with type checking. |
| Format and whole-model preview | Supported | |
| Live error markers | Planned | Bind and lint diagnostics inline as you type, from the same pass as type checking. |
| Go to definition on a model reference | Planned | |
| Preview a single CTE | Planned | Whole-model preview works today. |
| Rename a column across downstream models | Not supported | Deliberately withheld. Today's lineage does not see columns referenced only in `WHERE`, `JOIN`, `GROUP BY` or `ORDER BY`, so an automated rename would silently skip them and report success. It waits on the lineage rewrite. |

## Reuse

| Feature | Status | Notes |
|---|---|---|
| Project macros (`macros/`) | Supported | Python and SQL macros, auto-registered. |
| Built-in macro library | Supported | Shipped with havn, and a project macro of the same name overrides it. |
| Shared macro packs (pip-installable) | Planned | The lightweight way to share logic between projects. |
| Packages (shared models across projects) | Not supported | Deferred. There is no package config, no fetch step and no multi-root model discovery, and a half-built version that works in the CLI but not in the DAG view would be worse than none. Macro packs cover the realistic case first. |

## Testing

| Feature | Status | Notes |
|---|---|---|
| `@assert` data-quality assertions | Supported | One expression per line in a model, evaluated against the built model. |
| Contracts (`contracts/*.yml`) | Partial | Assertions, freshness windows, severity, notification and escalation all work. They run strictly after the model is built, and a contract on a model that does not exist yet reports a missing table. |
| Contract column and type declarations | Planned | Contracts have no column, type or nullability surface today. It arrives with type checking, along with a type-equivalence policy so a harmless widening does not fail the build. |
| Unit tests (fixed input rows, expected output) | Planned | Nothing exists today. The design runs the model against in-memory fixtures with no warehouse involved. |
| Anomaly detection | Supported | Statistical checks over run history, configured under `quality.anomaly_detection` and surfaced in the web UI. |
| `havn diff` | Supported | Row-level diff of what a change would do, before you build it. |
