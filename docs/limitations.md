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
| `on_schema_change` policy | Supported | `append_new_columns` (default), `ignore`, `fail`, `sync_all_columns`, set with `@config on_schema_change=`. Staging and target are compared on name and type in both directions before any write, so a policy that refuses the change leaves the target untouched. No option backfills old rows for a newly added column, and only top-level columns are tracked: a field inside a `STRUCT`, `MAP` or `LIST` is invisible to the comparison. |
| `ephemeral` | Supported | Inlined into every consumer as a `__havn_`-prefixed CTE rather than materialized; chains and the model's own CTEs are hoisted in dependency order, and aliases are preserved. Reported as `inlined`. `@assert`, `@grain`, `{this}`, `@watermark` and `incremental_filter` are rejected on an ephemeral model, because each of them needs a target table that never exists. |
| `snapshot` (SCD2 history) | Supported | `@config materialized=snapshot, unique_key=...` with `strategy=check` (optionally `check_cols=`) or `strategy=timestamp, updated_at=`, and `hard_deletes=ignore|invalidate|new_record`. Writes `valid_from`, `valid_to`, `is_current`, `row_hash` (plus `is_deleted` under `new_record`), renameable project-wide under `snapshots.meta_columns`. Replays are no-ops; a duplicate `unique_key` in the query is refused before any write; `--force` re-runs the merge and never drops history, so dropping the table is the reset. New source columns are appended with NULL history, removed or retyped columns are an error, and `incremental_filter` / `@watermark` are rejected. Not to be confused with `havn snapshot` / `havn rewind`, which save whole-warehouse restore points. |
| `microbatch` | Not supported | Deferred. The batch loop, per-batch state and backfill CLI are all missing, and no user has asked for them yet. Incremental models with an `incremental_filter` cover the common case. |

## Validation

`havn validate` checks project structure, config and the DAG before you build.

| Check | Status | Notes |
|---|---|---|
| DAG construction, cycle detection | Supported | Circular dependencies are reported by name. |
| Table and column name checking | Supported | The bind pass resolves every model against a shadow catalog, so columns on upstreams that have never been built are checked too. `--no-bind` falls back to the name-level check, which skips unbuilt upstreams. |
| Type resolution and bind errors | Supported | `havn validate` hands each model's SQL to the DuckDB binder against a shadow catalog. Resolves output types and catches wrong arity, unknown functions, operator overload failures, missing columns, missing struct keys, ambiguous references, aggregation without GROUP BY and set-operation arity mismatches, without reading a row. Caveat: it does not catch value conversions. `CAST(some_varchar AS INTEGER)` binds clean and fails at run time on the first row that is not a number, as does comparing a numeric column to a string constant. A clean bind is not a guarantee that the build will succeed. See [Data Quality](quality.md#validation-and-type-resolution). |
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
| Selectors on `havn transform` | Supported | The full grammar, positionally or with `--select/-s`, plus `--exclude/-x`. `havn ls` dry-runs a selector without building. |
| Job selectors in `orchestration/*.yml` | Supported | The same grammar, plus ingest/export script paths, a job-level `exclude:`, and `resolve: upstream`. |
| Graph operators (`+x`, `x+`, `+x+`, `n+x`, `x+n`, `@x`) | Supported | `n+`/`+n` bound the walk to N hops; `@x` is x, its downstream, and every upstream of those. |
| Glob selectors (`gold.fct_*`, `*.customers`) | Supported | fnmatch anywhere in the name, in either half. |
| Selector methods (`tag:`, `path:`, `config.<key>:`, `state:modified`) | Supported | `state:modified` reads the same content and upstream hashes change detection uses. Comma intersects, e.g. `tag:daily,gold.*`. |
| Selectors on the API and MCP | Supported | `POST /api/transform` takes `targets` and `exclude`; `GET /api/models?select=` filters the listing; the MCP `run_transform` and `list_models` tools take `select`. |
| `result:` and `source_status:` selectors | Not supported | These need a stored result set from the previous run, which havn does not keep per model beyond its status. |
| Environments | Supported | `havn env use <name>` switches the database path and connection overrides declared in `project.yml`. |
| Defer to another environment | Planned | Will attach a prod warehouse read-only so a dev run reads models it has not built. DuckDB's file lock means it cannot attach while another process holds that file open for writing; that constraint is not removable. |
| Scheduler | Supported | Cron schedules in job files, run by `havn schedule`, plus `havn watch` for rebuild on file change. |
| Parallel execution | Supported | Independent models run concurrently by default; `--sequential` turns it off. |

## Editor (web UI)

The Monaco editor in `havn serve`.

| Feature | Status | Notes |
|---|---|---|
| Autocomplete | Supported | Schemas, tables, columns and macros. |
| Hover | Supported | Shows inferred column types for output columns and `alias.column` references, plus macro signatures and docstrings and table and column metadata. |
| Format and whole-model preview | Supported | |
| Live error markers | Supported | Bind diagnostics 400 ms after a keystroke, lint diagnostics on save and after 1.5 s idle, under separate marker owners. Only for `.sql` files under `transform/`. |
| Go to definition on a model reference | Supported | `F12` or Ctrl/Cmd+Click on `schema.model`. Resolves through the model's own path, so `@config schema=` overrides are followed. Column-level definition is still planned. |
| Preview a single CTE | Supported | A `Preview` code lens above each CTE, or Ctrl/Cmd+Shift+Enter at the cursor. Capped at 100 rows, like the whole-model preview. |
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
| Unit tests (fixed input rows, expected output) | Supported | `tests/unit/*.yml` declares mock upstream rows and the expected output; `havn test` runs each case on an in-memory DuckDB with the project's macros, reading nothing from the warehouse. An unmocked upstream is an error, not a fallback. Incremental models are tested as a full refresh: `incremental_filter` and the merge strategy are not exercised. |
| Anomaly detection | Supported | Statistical checks over run history, configured under `quality.anomaly_detection` and surfaced in the web UI. |
| `havn diff` | Supported | Row-level diff of what a change would do, before you build it. |
