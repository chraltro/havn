# Changelog

All notable changes to havn are documented in this file.

## [Unreleased]

The dbt v2 gap work. Every item below came out of a research pass over the
current code against dbt v2.0 (GA 2026-09-16); the plan and its evidence live
in `docs/internal/dbt-v2-gap-plan.md`.

### Validation and types

- **Bind pass.** `havn validate` now resolves every model's SQL through the
  DuckDB binder against a throwaway in-memory shadow catalog, reporting bind
  errors with line and column: wrong arity, unknown functions, operator
  overload failures, missing columns (including on upstreams that have never
  been built), missing struct keys, ambiguous references, aggregation without
  `GROUP BY`, set-operation arity mismatches. Reads no rows, writes nothing,
  about 3 ms per model. `--no-bind` opts out.
- **Validation reflects the file, not the last build.** A model's fresh SQL
  shadows its stale built table, so a rename or retype upstream is caught
  before the build rather than during it.
- **The pre-build gate no longer skips runs that include ingest.** It runs
  after ingest and before the first transform.
- **Known boundary, stated plainly:** the binder does not catch value
  conversions. `CAST(some_varchar AS INTEGER)` binds clean and fails at run
  time. dbt v2 has the same boundary.
- **Column contracts.** Contract YAML takes a `columns:` block (name, type,
  optional `nullable` and `description`) plus `strict:` and `on_widen:`.
  Declarations are checked before the build against the bind pass, so a
  break is caught on a warehouse where the table does not exist yet, and
  again after the build against the live table.
- **`havn validate --schema-drift`** (or `validation.schema_drift: warn`)
  reports a model whose output shape moved since its last build, contract or
  no contract. Off by default.
- **`havn check` rejects unknown `@config` keys and unsupported
  `materialized` values** with a did-you-mean. `materialised=table` used to
  build a view in silence.
- **Two SQL files resolving to the same `schema.name` now fail discovery**
  naming both paths, instead of one being dropped from the DAG.
- New `_havn.model_columns` table records each model's columns at build time;
  `GET /api/models/{name}/columns` reads it.

### Editor

- **Live error markers** in `transform/*.sql`: bind diagnostics 400 ms after a
  keystroke, SQLFluff warnings on save and after 1.5 s idle, with a count in
  the toolbar. Backed by the new `POST /api/bind`, which returns positioned
  diagnostics plus the inferred output and upstream schemas.
- **Hover shows inferred column types**, for the model's own output columns
  and `alias.column` references into upstream models.
- **`F12` or Ctrl/Cmd+Click on a `schema.model` reference opens the file that
  declares it**, resolved through the model's real path so `@config schema=`
  overrides are followed (the old `transform/{schema}/{name}.sql` guess is
  gone from every jump).
- **Preview a single CTE** from a `Preview` link above it or Ctrl/Cmd+Shift+
  Enter at the cursor, via the new `POST /api/sql/ctes`. Recursive CTEs are
  refused.
- **`F2` renames a column across files**, in the model that defines it and
  every downstream model that reads it, including references that only appear
  in `WHERE`, `JOIN`, `GROUP BY`, `ORDER BY`, `HAVING` or `QUALIFY`. A
  downstream `SELECT *`, `COLUMNS(...)`, `UNION BY NAME` or a YAML mention is
  reported as a blocker before anything is written. Right-click offers
  **Find column references**. Also `havn rename-column MODEL COLUMN NEW`.
- `PUT /api/files` takes a batch of files and writes all or none, with per-file
  hash checks and rollback.
- Fixed: the whole-model preview sent no row limit and shipped mid-file
  `@assert` lines to DuckDB as SQL.
- Fixed: the editor's column cache never expired, so a rebuilt model kept
  offering its old columns for the rest of the session.
- Fixed: lint violations were reported at the wrong line for every model with
  a modern `@config` header; a directive below the SQL also produced a
  spurious "unparsable section".
- `POST /api/lint/file` needs only `read` when `fix` is false.
- **Model workbench.** Opening a `transform/*.sql` file wraps the editor in a
  workbench: a lineage strip (upstream and downstream models, one click to
  open), an inspector beside the code with Preview, Checks, Columns and Runs,
  and an action bar that says whether the model is unsaved, changed since its
  last build or failing checks, and how many downstream models depend on it.
  **Build + downstream** runs the `model+` selector after saving. Failed
  `@assert` lines are highlighted in the editor with the failure inline;
  **Show rows** previews exactly the rows a failed check counted, and
  **+ add @col** starts a doc line for an undocumented column. Backed by the new
  `GET /api/models/workbench?path=`. The toolbar's Save / Run Model / Run
  buttons moved into the action bar for model files.

### Testing

- **Model unit tests.** Declare fixture rows for a model's upstreams and the
  rows it should produce in `tests/unit/*.yml`. Each test runs on a throwaway
  in-memory DuckDB with your macros registered and reads nothing from the
  warehouse, so a test cannot pass because of what happens to be built. Row
  comparison is type-drift proof and order-insensitive by default.
- `havn test` runs the suite (`--model`, `-v`), `havn check` includes it,
  `GET /api/unit-tests` and `POST /api/unit-tests/run`, a `run_unit_tests`
  MCP tool, and an Observe > Unit Tests panel. `havn init` scaffolds
  `tests/unit/` with an example.

### Modeling

- **`@config on_schema_change=append_new_columns|ignore|fail|sync_all_columns`**
  for incremental models. Staging and target are compared on name and type,
  both directions, before any write. The default now refuses a removed column
  (which used to diverge silently) and a retyped column (which used to round
  a `DOUBLE` 20.5 into an `INTEGER` 21). Schema actions are recorded in the
  run log.
- **Incremental writes are transactional.** The ALTER, DELETE, UPDATE and
  INSERT of a `delete+insert` or `merge` run commit together or not at all.
  Previously a failing INSERT left the target missing the rows it had just
  deleted.
- **`@config materialized=ephemeral`.** Never built; every consumer gets the
  query prepended as a `__havn_`-prefixed CTE with references rewritten.
  Chains and the ephemeral's own CTEs are hoisted in dependency order. Runs
  report it as `inlined`.
- **Snapshot models (SCD2).** `@config materialized=snapshot, unique_key=...`
  keeps row-level history with `valid_from`, `valid_to`, `is_current` and
  `row_hash`. `strategy=check` (optionally `check_cols=`) or
  `strategy=timestamp, updated_at=`; `hard_deletes=ignore|invalidate|
  new_record`. Config names and values match dbt's. Meta column names and a
  `valid_to_current` sentinel are configurable under `snapshots:` in
  `project.yml`. Runs are idempotent; a duplicate key in the query fails
  before any write; `--force` never drops history. This is row history, not
  the whole-warehouse restore points of `havn snapshot` / `havn rewind`.
- **Microbatch incremental strategy.** `incremental_strategy=microbatch` with
  `event_time`, `batch_size` (`hour`/`day`/`month`/`year`), `begin` and
  `lookback` cuts a run into UTC windows; the model filters on `{start}` and
  `{end}`. Each window commits in its own transaction and is recorded in
  `_havn.batch_state`, so a failure at window 17 of 30 leaves sixteen
  committed and the next run resumes. `havn transform --event-time-start/
  --event-time-end` backfills a range.

### Running

- **Graph selectors on `havn transform`:** `+x`, `x+`, `+x+`, `n+x`, `x+n`,
  `@x`, fnmatch wildcards (`gold.fct_*`), `tag:`, `path:`, `config.<key>:`,
  `state:modified`, comma for intersection, `--select/-s` and `--exclude/-x`.
  `havn ls` dry-runs a selector. Jobs gain `exclude:`. `POST /api/transform`
  and the MCP tools take the same grammar.
- **`@config tags=daily,finance`** for `tag:` selectors. Tags are not part of
  the content hash, so retagging does not rebuild.
- Fixed: targeted runs (`havn transform gold.orders`) wrote an empty upstream
  hash to `model_state`, so the next full run spuriously rebuilt the model.
- Fixed: `gold.fct_*` and any other partial wildcard silently matched no
  models in job targets.
- Fixed: `havn diff` in changed mode compared against an empty upstream hash
  and reported nearly every model as changed.
- **Defer.** `havn transform --defer` builds in the active environment while
  reading every model it has not built from the environment's `defer:`
  target (`environments.<name>.defer: <other env>` in `project.yml`). Writes
  always land locally. What is redirected is decided by the live DuckDB
  catalogs, so `landing` tables, seeds and sources fall through with no
  declaration. The limitation is DuckDB's file lock: the attach fails while
  another process holds the target open for writing, so `--defer-snapshot`
  defers to a consistent copy instead (`COPY FROM DATABASE` when the target
  is free, the newest verified backup when it is locked). `havn env show`
  and `GET /api/environment` report the target and whether it can be opened
  right now. No manifest or `--state`; havn needs the other environment's
  file.
- Model existence and column probes are scoped to the current database, so
  an attached second warehouse cannot be mistaken for the local one.

### Lineage and performance

- **Column lineage rewritten on `sqlglot.lineage`** with a conformance suite
  that uses DuckDB `DESCRIBE` as ground truth. Nested subqueries, every
  UNION branch, `SELECT * EXCLUDE/REPLACE`, `UNPIVOT` and `ASOF JOIN` are now
  exact; `PIVOT`, `LATERAL` and struct field access are documented as
  approximate. `docs/lineage.md` matches the suite.
- **Impact analysis reports downstream models that only filter, join, group
  or order on a column**, with the clause named.
- **Full-project lineage on 1000 models drops from about 20 s to about 150
  ms.** The catalog was read once per dependency; it is now read once per
  pass. Each model's SQL is also parsed once per pass instead of four times.
- New `benchmarks/bench_parse.py`.
- The `sqlglot` floor is now 26.17, the first version carrying token
  positions.

### Reuse

- **Packages.** Share models and macros between projects. Declare them under
  `packages:` in `project.yml` as `{name, git, rev}` or `{name, path}`, then
  `havn packages install`. Sources are cloned into `havn_packages/` and
  pinned by `havn_packages.lock`, which is committed; a later install
  reproduces the locked commit unless you pass `--upgrade`. A branch `rev`
  is accepted but warns, because it is not a pin.
- **Package namespacing.** A package's schemas become `<pkg>_<schema>` and
  its references to its own models are rewritten to match, so a package
  author writes plain `silver.customers` and a host project can never lose a
  name to one. Override per schema in the package's `havn_package.yml`; a
  real collision raises `DuplicateModelError`.
- **Package macros** register between the built-in library and the project,
  so project macros win, with package-scoped module names so two packages
  can both ship `macros/utils.py`.
- `havn packages install|list|remove`, a `package:` selector, `GET
  /api/packages`, `POST /api/packages/install`; package models are labelled
  in the DAG panel and the file tree, and the editor warns that the next
  install overwrites edits to an installed file. Package models are visible
  to every command and endpoint that lists or builds the DAG, including
  sentinel, Pipeline Rewind, the notebook paths, promote-to-model, unit
  tests and freshness reporting. `havn lint --fix` refuses a path inside
  `havn_packages/`.
- The DAG panel takes a graph selector: Preview highlights the matching
  nodes, Run selection builds exactly that set. The header shows the active
  environment's defer target with a green or amber dot for whether it can be
  attached right now.

### Security

The four items below were found by a review of this branch before release;
none of them shipped in a released version.

- `POST /api/bind` no longer executes caller-supplied SQL against the
  warehouse. The bind pass builds its shadow catalog in a private in-memory
  DuckDB database with no attachment to the warehouse, seeded with empty
  typed tables from `information_schema`, so a buffer cannot write to, read
  from or escape into the real catalog. The shadow turns off
  `enable_external_access` and locks its configuration before any model SQL
  runs, so `read_csv`, `read_text`, `glob`, `COPY ... TO`, `ATTACH`,
  `INSTALL` and `LOAD` all fail inside it. Buffers sent to `/api/bind` and
  to the MCP `bind_model` tool go through the shared read-only validator
  first and are never bound if they fail. The route takes a read-pool
  connection, not a writable handle.
- Package `git:` sources are restricted to `https://`, `ssh://` and
  `git@host:path`. `ext::`, `file://`, bare local paths, `git://` and
  `http://` are refused; a package on this machine belongs under `path:`.
  Installing a package imports and registers its Python macros, and the docs
  now say so.
- The in-memory connection `havn test` runs model SQL on has external access
  disabled and its configuration locked.

### Fixed before release

- F2 column rename corrupted the open file when it had unsaved changes. The
  rename is refused until the file is saved, and afterwards the buffer is
  reloaded from disk instead of being patched with disk offsets.
- F2 on a CTE's own alias could silently rename a same-named column of an
  upstream model. A rename whose target is not the model on screen now asks
  first, naming the model, the column and the number of files.
- Hover types and F2 did not resolve upstream columns when the model spelt
  a table in mixed case or quoted it.
- A bind result could overwrite a fresher column lookup with a narrower list
  from the last build, hiding a real column from completion.
- The editor kept one Monaco model per file ever opened, kept the previous
  file's error count after a switch, and claimed Monaco's own `inmemory:`
  buffers in its file opener.
- Microbatch models with an ephemeral upstream could never build: inlining
  round-tripped the SQL through sqlglot and turned `{start}` into
  `{'start': start}`. Placeholder masking now wraps every round trip of
  model SQL, inlining and defer alike.
- Catalog probes across the engine, CLI, MCP server and API describe only
  the current database. Under `--defer`, a model the target also holds was
  profiled with both warehouses' columns, `havn diff` reported columns that
  were never local, and unit-test mocks were typed from the other
  environment.
- A microbatch backfill with `--event-time-end` in the future stranded the
  model: future windows were recorded as done and later runs found no
  window to process. The range is clamped to now, future windows are never
  recorded, and the resume cursor reads only closed windows.
- `havn rename-column` left `@assert`, `@col` and `@grain` lines untouched,
  so the next build failed its own assertion. Directive lines are renamed
  too.
- `incremental_strategy=append` bypassed `on_schema_change` and inserted
  positionally, so a reordered projection wrote every value into its
  neighbour's column and reported success.
- A built snapshot could not switch `hard_deletes` to `new_record`; the
  required `is_deleted` column is now added and backfilled.
- Two concurrent transform runs in one process could interfere through the
  defer rewriter: a run started without defer had another run's redirects
  applied, and a short deferred run detached the target out from under a
  longer one. The rewriter is scoped to the run and the attach is
  refcounted; two runs deferring to different environments coexist.
- `havn rename-column` did not see installed package models from the CLI
  and, from the API, wrote edits into `havn_packages/` which the next
  install deleted. Package sites are now blockers on both paths.
- `POST /api/lint/file` let a viewer read `.sql` files in a sibling
  directory through a prefix-based containment check, and returned file
  contents on a check. Fixed, and `POST /api/bind` got the same
  containment fix.
- A `path:` package that was the project directory or an ancestor of
  `havn_packages/` copied itself recursively; a failed copy escaped as a
  traceback and left a partial checkout. Both refused or contained now, and
  install removes checkouts the lock names but `packages:` no longer
  declares.
- `havn env show` crashed on an empty `environments:` block; `havn ls
  state:modified` on a project with no warehouse listed nothing; `POST
  /api/transform` returned 200 with empty results for a mistyped selector
  (now 400 with the warnings); `havn lint --fix` prepended a blank line to
  headerless files; a batch file write naming one file two ways wrote it
  twice; `--defer-snapshot` leaked a warehouse copy in the temp directory
  when the attach failed.

### First impression

- README leads with `pip install havn`; the clone-and-npm chain moved to the
  from-source section. The wheel ships the built web UI.
- README shows a screenshot of the web UI instead of a commented-out
  placeholder.
- New docs page, **What havn Supports** (`docs/limitations.md`): per-area
  tables of supported, partial, not supported and planned.

## [0.2.27] - 2026-08-04

### Security

- **Read-only query surfaces** now block four more bypasses of the
  `sql_safety` validator, each of which let a `viewer` read arbitrary
  server-readable files through `/api/query`, the semantic layer, the
  importer preview, and the MCP `query` tool:
  - `json_execute_serialized_sql(json_serialize_sql('...'))` executes SQL
    nested inside a string literal. String literals are stripped before the
    validator's scans run, so the inner query was completely invisible to it.
  - `glob()`, `sniff_csv()`, `read_duckdb()`, the `parquet_*` metadata
    readers, `postgres_query()`/`mysql_query()`, `arrow_scan()`, and the
    spatial file readers were missing from the file-access blocklist.
  - `FORCE INSTALL` / `FORCE CHECKPOINT` put the real verb in second
    position, past the leading-keyword check. Transaction and catalog
    statements (`BEGIN`, `COMMIT`, `USE`, `COMMENT`, `ANALYZE`) are now
    rejected too.
  - A bare double-quoted data filename in table position
    (`FROM "warehouse.duckdb"`) resolves as a replacement scan relative to
    the server's working directory. 0.2.26 only caught path-shaped names.
- **Dashboard widget queries** now apply column masking. `/api/dashboards/
  {id}/widgets/{wid}/query` and `/query-batch` ran raw SQL with no masking at
  all, so any `viewer` could read unmasked PII by putting the column in a
  widget. Cached results are now keyed by role as well, so an admin's
  unmasked rows are never served to the next viewer.
- **`/ws/agent`** now requires the `write` permission. It authenticated the
  connection but never authorized it, so a `viewer` token could drive a
  coding agent inside the project directory with the server process's
  privileges (arbitrary file write and shell execution).
- **`GET /api/secrets`** no longer reveals the first and last two characters
  of every secret. For short or prefixed values that is a meaningful chunk of
  the plaintext; the masked form is now length-only.

### Fixed

- **Dependency extraction** no longer drops a real dependency when a CTE
  shares a model's name. `WITH orders AS (...) ... JOIN bronze.orders`
  resolved to no dependencies at all, which put the model in the wrong DAG
  position and left it un-rebuilt when its upstream changed.
- **`@config` parsing** no longer truncates values containing commas. A
  composite `unique_key=customer_id, event_date` parsed as
  `customer_id` alone, silently degrading an incremental `delete+insert` to a
  partial-key delete: loading a new date for an existing customer **deleted
  that customer's entire history**. `incremental_filter=WHERE x IN (1,2)`
  was truncated the same way.
- **Incremental models with NULL unique-key values** no longer duplicate on
  every run. `=` and `(k) IN (...)` evaluate to NULL rather than TRUE for a
  NULL key, so those rows were never deleted before the re-insert. Both
  `delete+insert` and `merge` now compare keys with `IS NOT DISTINCT FROM`.
- **Switching a model from `view` to `incremental`** no longer wedges it.
  The "table exists" probe counted views, so every subsequent run failed with
  `Binder Error: Can only delete from base table` until the view was dropped
  by hand.
- **Generic `@assert` expressions** are evaluated against every row instead
  of one arbitrary row. `@assert amt > 0` reported *pass* on a table
  containing a negative value. Aggregate predicates (`sum(amt) > 0`) keep
  their previous single-row semantics, and `row_count` now works inside a
  compound expression.
- **Compound assertions** are no longer silently truncated to their first
  term. `@assert row_count > 0 AND row_count > 99999` matched the `row_count`
  builtin and discarded everything after it, always reporting *pass*.
- **Adding an `@assert` or `@grain` to an already-built model** now triggers
  a rebuild. Both are stripped from the hashed query text, so change
  detection skipped the model and the new check never ran.
- **`accepted_values`** escapes embedded single quotes instead of breaking
  out of the SQL literal.
- **Dependency cycles** raise a `CircularDependencyError` naming the models
  and their files, instead of surfacing a bare `graphlib.CycleError`
  traceback through the CLI, the API, and the scheduler.
- **Pipeline run history** no longer reports failed runs as successful.
  `GET /api/history/runs` rolled up status with `MAX()` over the status
  *strings*, and `'success' > 'failed' > 'error'` alphabetically, so any run
  with at least one successful step reported success.
- **Dashboard widget timeouts** are enforced. `SET statement_timeout` is not
  a DuckDB setting, so it raised on every call, the exception was swallowed,
  and `WidgetQueryRequest.timeout` was a no-op. Widgets now use the same
  interrupt-based governor as `/api/query`.
- **`havn pr show` / `havn pr review`** no longer raise `NameError` on every
  invocation (a missing import).
- **`havn query`** against a running `havn serve` now reports server-side
  errors instead of deleting a valid `.havn/serve.json` and failing with a
  lock error. `HTTPError` subclasses `URLError`, and the broad clause came
  first.
- **Login rate limiting** is guarded by a lock. The unsynchronized
  read-modify-write let concurrent attempts exceed the cap, and the eviction
  sweep could raise `dictionary changed size during iteration` out of the
  login handler as a 500.
- **The agent sidebar** works under `havn serve --auth`. Neither its
  `/api/agents` fetch nor its `/ws/agent` connection carried the token, so
  the panel sat in a permanent 3-second reconnect loop. Its model list was
  also stale and contained a model ID that does not exist.
- **Merge and Close on a pull request** no longer throw `ReferenceError`:
  `PrDetail` used a `showConfirm` prop it was never passed, so both buttons
  did nothing.
- **`havn init`** scaffolds a project that passes its own `havn lint`. The
  sample bronze model referenced the USGS `magType` column with its original
  camel case, which the scaffolded rule set rejects.

### Changed

- `pip install -e ".[dev]"` now installs both `httpx` and `httpx2`.
  starlette 1.0 moved its `TestClient` to `httpx2` and deprecated the `httpx`
  backend; neither is a dependency of starlette itself. Declaring both clears
  the `StarletteDeprecationWarning` on every test run and keeps the suite
  working when starlette drops `httpx` support.
- `CLAUDE.md` corrected where it had drifted: `havn diff --full`, the
  location of API endpoints (`server/routes/`, not `server/app.py`), the
  scheduler implementation, and the frontend's TypeScript usage.

## [0.2.26] - 2026-07-16

### Security

- **Read-only query surfaces** now also reject DuckDB replacement-scan file
  reads written with double quotes (`SELECT * FROM "/etc/passwd"`). 0.2.25
  blocked only the single-quoted form, but DuckDB resolves a double-quoted
  path in table position as a file read too, so swapping the quote character
  bypassed the check on `/api/query`, the semantic layer, the importer
  preview, and the MCP `query` tool. Ordinary quoted identifiers
  (`FROM "gold"."my orders"`) are unaffected: only path-shaped names
  (containing a slash, backslash, or URL scheme) are rejected.

## [0.2.25] - 2026-07-16

### Security

- **Read-only query surfaces** now reject DuckDB "replacement scan" file
  reads (`SELECT * FROM '/path/file.csv'` / `FROM 'https://...'`). Previously
  the read-only validator only blocked file-access *functions*
  (`read_csv`, `read_parquet`, and so on), so a bare string path in table position
  slipped through and could read any server-readable file or reach external
  URLs. Covers `/api/query`, the semantic layer, the importer preview, and
  the MCP `query` tool (all share `havn.engine.sql_safety`).
- **`GET /v1/export/duckdb`** now requires the `execute` permission instead
  of `read`. Column masking is enforced at query time, so a raw warehouse
  download let a read-only `viewer` retrieve unmasked PII; editors and
  admins keep the data-portability path.
- **Column masking** no longer leaks when a table is referenced without its
  schema (e.g. `SELECT c.email AS x FROM customers c`). The pre-query
  rewriter now matches masking policies by table+column for schema-less
  references, and the masked-column filter/sort/join guard covers them too.
- **`POST /api/semantic/query`** now applies the same post-query masking
  backstop as `/api/query`, so a metric surfacing a masked dimension can't
  return unmasked values when the pre-query rewrite can't be applied.

### Fixed

- **Change detection** now rebuilds an incremental model when only its
  `@config` settings change (`unique_key`, `incremental_strategy`,
  `incremental_filter`, `partition_by`, `watermark`): previously the
  content hash covered only the query body, so config-only edits were
  silently skipped.
- **Parallel transforms**: a severity=`error` `@assert` (or a `@grain`
  uniqueness check) failing inside a multi-model DAG tier now reports
  `assertion_failed` and blocks downstream models, matching the sequential
  path. Previously the parallel worker reported such models as `built` and
  let bad data cascade.
- **`@watermark` incremental filter** no longer permanently drops source
  rows that tie the boundary watermark value. With a `unique_key` the filter
  is now inclusive (`>=`) so tied late-arriving rows are re-read and
  upserted; it is also NULL-safe on an empty target and works for integer
  watermark columns (the old `'1900-01-01'` sentinel forced a string
  comparison).
- **Scheduler**: cron-scheduled orchestration jobs no longer fire twice per
  matching minute (the once-per-minute dedup key was recorded under a key the
  guard never read). Scheduled streams now honor their configured
  `retries`/`retry_delay`, consistent with manual and server pipeline runs.
- **Notebook database ingest** now passes the configured connection
  parameters instead of the connection object's `__dict__`, which had nested
  the real settings under `params` and caused every value to fall back to its
  default (localhost / empty password).
- **`havn backup`** no longer leaks its write connection (and skips the WAL
  flush) when `CHECKPOINT` raises; the connection is always closed.
- **`havn backup-restore`**: the backup-restore command was renamed from
  `restore` (it was shadowed by the Pipeline Rewind `restore` command, so it
  was unreachable). The model-restore `havn restore MODEL --run ...` is
  unchanged.
- Removed a dead, unreachable duplicate `havn validate` command definition
  (the reachable one lives in the project CLI module; the `havn check`
  command covers model-level validation).

### Documentation

- **CLI reference corrected against the actual CLI.** `havn transform`
  documented a non-existent `--parallel` flag (the real flag is
  `--sequential`; parallel is the default). `havn diff` documented
  non-existent `--changed` / `--all` flags (the real "everything" flag is
  `--full`; `--exit-nonzero-on-change` was undocumented). `havn migrate`
  documented `--target` instead of `--to`.
- **`havn version` is no longer described as "show havn version."** It
  manages warehouse versions with Parquet-based time travel; the package
  version is `havn --version` / `-V`. The in-app wiki had two conflicting
  `havn version` sections; the incorrect duplicate was removed.
- **Documented `havn metrics` (list/query/sql) and `havn mcp`**, added in
  0.2.24 but missing from both CLI references.
- **README** no longer lists a `havn docs` command, which does not exist.
  Model documentation is generated from the warehouse schema and
  `@description` / `@col` directives and is browsable in the web UI.

## [0.2.24] - 2026-07-09

### Added

- **Semantic layer**: declare metrics once in `metrics/*.yml` (model,
  measure, dimensions, time dimension, filters) and query them
  consistently everywhere. `havn metrics` lists definitions,
  `havn metrics sql` shows the compiled SELECT, `havn metrics query`
  runs it (routing through a live `havn serve` like `havn query` does).
  API: `GET /api/semantic/metrics`, `POST /api/semantic/compile`,
  `POST /api/semantic/query` (masking policies apply). The compiler
  validates dimensions/grains against the declaration and escapes time
  literals, and every compiled statement passes the same read-only SQL
  validation as `/api/query`.
- **MCP server**: `havn mcp` starts a Model Context Protocol stdio
  server (no SDK dependency) so AI agents like Claude Code can work
  against the warehouse. Tools: `query` (read-only), `list_tables`,
  `describe_table`, `list_models`, `get_model`, `model_lineage`,
  `run_history`, `list_metrics`, `query_metric`, and `run_transform`
  (omitted with `--read-only`; creates Pipeline Rewind snapshots when
  enabled). Reads route through a running `havn serve` when it holds
  the warehouse lock, falling back to a direct read-only connection.
- The read-only SQL validator moved to `havn.engine.sql_safety` so the
  query API, dashboards, collaboration cells, semantic layer, and MCP
  server share one implementation (`routes/query.py` re-exports the old
  names for compatibility).

- `havn run` now accepts `--env`, matching every other pipeline command.

### Fixed

- `havn tables` and `havn history` crashed with "too many values to
  unpack" whenever a `havn serve` was running for the project: both
  unpacked the server-routed result into three variables after a fourth
  field (`truncated`) was added to `_fetch_via_server`.
- The dashboard widget cache never cached anything: writes ran on a
  read-only cursor from the read pool AND used `INTERVAL ? SECOND`,
  which is a DuckDB parser error — both failures were swallowed by a
  bare `except`. Cache writes now run on the write connection with the
  parameterizable interval form, and the cache key includes the
  widget's SQL so editing a query immediately misses stale entries
  instead of serving the old result until the TTL expires.
- `/api/query` silently dropped `offset` when no `limit` was given and
  the SQL had no LIMIT clause, re-serving page 1 to paginating clients.
  The response now also includes `row_count` (declared in the client
  types but never sent).
- Environment overrides that introduced a *new* connection mutated the
  parsed YAML in place (`.pop("type")`), stripping the connection type
  from the stored config object.
- Dashboards (web): a failed batch query left every widget spinning
  forever with the error only in the console; out-of-order batch
  responses could overwrite newer filter results (now sequence-guarded);
  drill-down fetch errors were swallowed, freezing the drill view; and
  History run-detail responses arriving after collapse/re-expand could
  attach to the wrong run.
- Wiki (web): page prose is now HTML-escaped before rendering (a raw
  `<` previously parsed as markup), and a failed page load shows a
  styled error with Retry instead of a fake "Error" page.
- Orchestration (web): interval/range/retry/timeout number inputs no
  longer snap to defaults mid-edit; values are clamped on blur and at
  save, so clearing a field can't emit `NaN` into a cron expression.
- `importer.preview_query` now enforces the platform's read-only SQL
  validation before embedding the query in its wrapper statement.

## [0.2.23] - 2026-06-12

Patch release: macro hot-reload fix.

### Fixed

- Macro hot-reload was silently broken on the DuckDB file backend:
  ``reset_macro_state()`` cleared the per-connection reload counters
  that the versioned internal UDF names (``_udf_<name>_<gen>``) rely
  on. The next registration collided with the existing
  ``_udf_<name>_0`` (DuckDB cannot replace a Python UDF), the error
  was swallowed as "already exists", and the public MACRO alias kept
  pointing at the old function. Editing ``macros/*.py`` on a running
  ``havn serve`` looked reloaded in the logs but queries kept running
  the old code. The counters now survive reloads.

### Tests

- Nightly suite repaired: hot-reload tests now exercise the production
  watcher contract (``force_reload=True``), the parallel-scheduler
  stress test asserts descendant-only blocking (siblings of a failed
  model still build), the migrate tests catch ``typer.Exit`` (newer
  typer vendors click, so the installed click's ``Exit`` is a
  different class), and an autouse fixture resets the server deps
  singletons after every test so no API test can leak its warehouse
  into the next one. Full suite: 1423 passing including slow tests.

## [0.2.22] - 2026-06-12

Query editor release: named query parameters, safer limits, and a
streaming-export fix. 1351 tests pass (up from 1345).

### Added

- Query parameters: the Query panel detects named `$name` placeholders
  and shows a Parameters row above the toolbar. Values are bound
  server-side as DuckDB prepared-statement parameters (no string
  interpolation, so values cannot inject SQL). Numbers and true/false
  are sent typed; everything else is sent as text and can be cast in
  SQL (`$day::DATE`). The `params` field is accepted by `/api/query`,
  `/api/query/explain`, `/api/query/explain-analyze`,
  `/api/query/profile`, and `/api/query/export-csv`.
- Run selection: with text selected in the editor, Run / Ctrl+Enter
  executes only the selection.
- The editor draft, parameter values, and query history survive tab
  switches and reloads. History entries record the parameter values
  used and show relative timestamps, and the dropdown gained a Clear
  button. Shortcut labels show Cmd instead of Ctrl on macOS.

### Fixed

- CSV export was completely broken ("No open result set"): FastAPI
  closes yield-dependency DB cursors before a StreamingResponse body
  runs, so the export endpoint now manages its own read cursor for the
  lifetime of the stream.
- The Explain button rendered an empty plan: the structured plan was
  passed to the viewer as raw text. The visual plan tree now renders.
- Queries ending in a `-- line comment` no longer break the auto-LIMIT:
  the client previously spliced the query into a one-line subquery
  string (commenting out the wrapper); the limit now travels in the
  request body and the server-side wrap is newline-safe.
- Running a query while the Plan tab was active left the results pane
  blank; the view now switches back to Table.
- Ctrl+Enter can no longer start a second query while one is running.

### Documentation

- API reference: documented `params`, the role-based query timeouts
  (admin 300s / editor 120s / viewer 60s; the old "30s" claim was
  outdated), and the previously undocumented explain and export-csv
  endpoints.
- Getting started: the Query panel section claimed JSON export exists
  (it does not); it now describes parameters and run-selection.

## [0.2.21] - 2026-06-09

UX/UI and accessibility release (backfilled entry; see the GitHub
release notes for full detail).

- "Run Sample Pipeline" no longer gets stuck on "Running...".
- New Model dialog follows the active theme; dialogs close on Escape;
  the Data Sources import flow is keyboard-operable.
- Native browser alerts replaced with in-app messages; run-status dots
  carry text labels for screen readers.

## [0.2.20] - 2026-06-09

Bug-fix and security release (backfilled entry; see the GitHub release
notes for full detail).

- #28: fresh `havn init` projects no longer fail on "Run All" with
  `ModuleNotFoundError: No module named 'pandas'`.
- Security: fixed an unauthenticated path-traversal in the SPA
  catch-all route and a notebook sandbox escape exposing `_havn`.
- Query Cancel button aborts the running query; DAG glow, wiki links,
  autocomplete race, and several smaller correctness fixes.

## [0.2.19] - 2026-05-15

Comprehensive stability, security, and performance sweep across the whole
codebase, plus a small agent-sidebar markdown improvement. Every change
strengthens an existing feature. 1340 tests pass (up from 1300).

### Security

- Path traversal in `/api/files/*` switched from string-prefix to
  `Path.is_relative_to`, closing the Windows sibling-directory bypass.
- Webhook receive endpoint (`POST /api/webhook/{name}`) now requires
  `HAVN_WEBHOOK_SECRET_<NAME>` or `HAVN_WEBHOOK_SECRET` (or explicit
  `HAVN_WEBHOOK_OPEN=true`). Payload capped at 5 MB; table name
  double-quoted.
- Query validator rewritten: strips strings and comments before parsing,
  walks CTE bodies via balanced-paren skipping, rejects file-access
  functions (`read_csv`, `read_parquet`, `httpfs_*`) including
  quoted-identifier variants, blocks multi-statement queries.
- `CREATE/DROP MASKING POLICY` interception now requires write permission
  (was reachable via the read-only query route).
- Dashboard filter injection closed via strict ASCII identifier regex.
- Snapshot capture / restore validates model names; all paths into SQL
  go through a safe-quoter and pass `is_relative_to` checks before any
  file write.
- `create_version` rejects table refs containing path-traversal characters
  before writing parquet.
- Importer (`read_csv`, `ATTACH`, `CREATE TABLE ... FROM _import_src`)
  validates every identifier and SQL-escapes every literal.
- Notebook ingest cells switched to `is_relative_to` and SQL literal
  escaping for filesystem paths.
- `/metrics` accepts optional `HAVN_METRICS_TOKEN` bearer auth.

### Stability

- WriteQueue worker survives `concurrent.futures.InvalidStateError` so a
  cancelled future cannot permanently stall the write path.
- WeakKeyDictionary backs the per-connection default-catalog map, so
  closed connections do not leak entries or produce wrong-catalog
  routing after `id()` recycling.
- Pipeline SSE `start_stream` race fixed: atomic check-and-set via a
  single locked `_start_operation` call.
- SSE event loop converted from a 300 ms busy poll to a
  `threading.Condition` so listeners are pushed.
- DuckDB extension installation runs once per process under a lock.
- `os.cpu_count() or 2` everywhere it was previously assumed non-None.
- Circuit breaker: time-windowed failure decay; exponential backoff
  actually applied on repeated probe failures; manual `reset` clears
  `_open_attempts` too.
- Agents (Claude, Codex, Gemini): each `send_message` kills any prior
  subprocess before spawning a new one, so a rapid second message no
  longer leaks the first process.
- Collaboration `SessionManager` got a re-entrant lock around every
  mutation; HTTP and WebSocket callers can no longer race past
  `_max_sessions`, double-evict, or corrupt `_connections`.
- Snapshot metadata DB uses one long-lived connection per project
  instead of open/close per helper call. Dead cached connections are
  detected and replaced.
- GC over expired snapshots is now O(N) (single GROUP BY) instead of
  O(N^2).

### Correctness

- Unique-key column names are validated before being interpolated into
  incremental `MERGE`/`DELETE+INSERT` SQL.
- Incremental queries are wrapped in a subquery before appending
  `WHERE`, so trailing `GROUP BY` / `ORDER BY` / `LIMIT` / `;` do not
  produce malformed SQL.
- `parse_depends` collects every `@depends_on` line, not just the first;
  explicit + auto-extracted refs are unioned in discovery.
- `row_count >= N` no longer parses as `>` followed by literal `= N`;
  regex alternatives reordered longest-first.
- Cron parser supports range+step (`0-29/5`) and comma-separated
  patterns with steps.
- `mask_partial` typing matches behavior (`str | None`) and accepts
  `show_last=0` without producing wrong-length output.
- Contracts `freshness < 5m` now means 5 minutes, matching every other
  industry tool. `s` (seconds) unit added.
- CDC `sync_table_high_watermark` reports rows actually inserted by the
  current run, not the total target row count.
- CDC `reset_watermark` returns the number of rows that were deleted.
- Lint returns a 3-tuple in the empty-files early-out path (used to be a
  2-tuple).

### Performance

- 30-second authenticated-token validation cache (`_cached_validate_token`)
  drops repeated single-thread write-queue lookups; `invalidate_token_cache`
  is called from user delete and user update so a role or password change
  takes effect immediately rather than lagging the cache TTL.
- DuckLake `status()` cached for 5 seconds; hot endpoint no longer
  triggers a Postgres round-trip per call.
- Frontend bundle split via Vite `manualChunks`: Monaco, react-vendor,
  icons, sql-formatter, dashboards, DAG, and notebooks. Main bundle
  dropped from 1308 kB to 481 kB (gzip 341 kB to 115 kB).
- TablesPanel guards against stale fetches via request-id ref.
- AgentSidebar and PipelineContext sessionStorage writes are debounced
  so per-chunk streaming does not serialize the whole log every paint.
- `_pinned_udfs` is a bounded deque (10000 entries) so long-running
  servers with many hot reloads do not accumulate dead closures.

### Frontend

- Agent sidebar markdown now renders `[label](url)` links as clickable
  anchors (open in a new tab) instead of showing the raw markdown syntax.

### Cleanups

- Removed `image_prompts.md`, `havn-dashboard-rubric.md`,
  `brand-preview.html`, and a stale benchmark result file.
- `mkdocs.yml`, `pyproject.toml`, README, CONTRIBUTING, getting-started:
  repo URL updated from `chraltro/db` to `chraltro/havn`.
- `_dp_internal` references in `.claude/rules/*.md` replaced with
  `_havn` (matches actual schema name).
- PLATFORM_REPORT.md rewritten from scratch; v0.1.0 snapshot was four
  releases out of date.
- `docs/masking.md`: documents all 14 masking methods, uses correct CLI
  command names (`havn mask add/list/remove`).
- `docs/cli-reference.md`: adds entries for `shell`, `explain`, `rewind`,
  `sentinel`, `pr`, `flight`, `streaming`, `migrate`.
- `docs/api-reference.md`: documents the decoupled SSE stream API
  (`/start` + `/events`) and the webhook auth headers.
- `docs/configuration.md`: secrets CLI now correctly documented as
  shipped (was marked "not yet available").
- `CONTRIBUTING.md`: corrected the obsolete `cli.py` / `transform.py`
  single-file references; CLI and engine are now packages.
- Vitest scope: now includes only `src/**`, so Playwright e2e specs are
  no longer collected as unit tests.
- AgentSidebar test updated for the `model` field on start messages.
- SortableTable global `tr:hover td` CSS rule scoped via class.
- `_is_read_only_connection` probes via `duckdb_databases()` instead of
  creating-then-dropping a real macro on the catalog.

### Tests

- `tests/test_sweep_fixes.py`: 37 regression tests covering query
  validation, cron parsing edge cases, depends_on accumulation, circuit
  breaker decay, webhook auth, path traversal, write queue cancelled-future
  survival, unique_key validation, snapshot/versioning identifier
  validation, mask_partial edge cases, and freshness unit semantics.
- `tests/conftest.py`: `shared_project` and `shared_client` fixtures
  available to all test files.

## [0.2.18] - 2026-05-03

A large release combining a feature roadmap (15 features across 5 phases)
with a sweep of correctness fixes uncovered by a real-world test-run
review (CTE scoping in lineage/check, diff non-determinism on aggregated
decimals, progress bar floods on non-TTY, and several API edges).

### Added

- **`havn shell`**: psql-style multi-line REPL with readline history,
  `\dt`/`\d`/`\dn`/`\df` slash commands, `\timing`, `\copy`, server-aware
  routing through `havn serve`.
- **`havn explain <model>`** with `--analyze`, `--json`, `--raw`. Surfaces
  DuckDB's plan tree using the existing `engine/explain.py` primitives.
  API counterpart at `GET /api/models/{name}/explain[?analyze=true]`.
- **`havn diff --exit-nonzero-on-change`**: exit code 2 when models have
  row/schema changes, for CI gating. Composes with `--format json`.
- **`havn init` seeds `.sqlfluff`**: relaxed default config (excludes
  RF03/AM05/ST06/LT05) so new projects don't drown in violations on
  idiomatic SQL.
- **Model directives**:
  - `@grain <cols>`: synthesises a uniqueness assertion post-build.
  - `@owner <label>`: propagates onto every assertion result for alert
    routing.
  - `@assert ..., severity=warn|error`: assertions can warn-only
    (continue) or halt downstream models on failure.
  - `@source_freshness <table>, max_age=24h, on=<col>, severity=`:
    pre-build contract; stale sources skip the model and (for error
    severity) cascade-skip downstream.
  - `@watermark <col>`: one-line incremental sugar that synthesises the
    `WHERE` clause; equivalent to writing the full `incremental_filter`
    by hand.
- **Downstream models are skipped** with status
  `skipped_upstream_blocked` when an upstream errors or fails an
  error-severity assertion.
- **`havn freshness --sources`** with `--source-min-rows N`: surfaces
  upstream row counts and max-on-column timestamps from each model's
  `@source_freshness` contracts. Resolves "fresh model on top of
  zero-row source". API counterpart accepts `?include_sources=true` and
  `?source_min_rows=N`.
- **Stdlib PII macros** (`havn.stdlib.pii`): `mask_email`, `mask_phone`,
  `mask_fnr`, `mask_credit_card`, `mask_ip`, `hash_consistent`.
  Auto-registered for every project, even those without a `macros/`
  directory. User macros with the same name shadow stdlib (warning
  logged). `havn macros` lists stdlib entries with origin tag.
- **`policies.deny` in `project.yml`**: column-level deny-list
  ("column X may not appear in schema gold"). Caught at compile time
  by `havn check` AND enforced at build time by `havn transform`
  (denied models marked `policy_denied` before any tier executes).
- **`havn watch --route <glob>`**: filter watched paths and rebuild
  only the matching model, not the whole DAG.
- **Editor "Run on save" toggle**: persists in localStorage; saves
  chain into `runSingleModel` for transform `.sql` or
  `runCurrentScript` for `ingest/export .py`.
- **TablesPanel structured docs**: per-column descriptions on hover
  and inline rail; model-level grain / owner / description block above
  the column list.

### Fixed

- **CTE scoping in `havn lineage`**: multi-source models with CTEs
  mis-attributed columns because `_extract_sources` defaulted
  unqualified columns to `depends_on[0]` and CTE references leaked
  into the table alias map. Now builds a separate `cte_alias_map`,
  threads it through, and resolves unqualified columns from the
  per-SELECT FROM/JOIN scope (with information_schema as tiebreaker).
- **CTE false positives in `havn check`**: false-positived on CTE
  columns (e.g. `flows.inflow_nok` got looked up against
  `silver.fact_transactions`) and on `b.*` star expansions (sqlglot
  represents them as `Column(name="*", table=b)`). Now builds a CTE
  name set + per-CTE column set, validates qualified CTE-column refs
  against the CTE outputs, and short-circuits `name=="*"` tokens.
- **`havn lineage` CLI now opens the warehouse** so `SELECT *` and
  unqualified columns can resolve via `information_schema`.
- **`havn diff` non-determinism**: reported +N/-N on identical content
  because `EXCEPT` is type-sensitive (temp-rebuilt columns drift on
  DECIMAL/DOUBLE precision from `SUM`s). Switched to MD5 hash of the
  per-column VARCHAR projection with a presence-prefix NULL sentinel
  (`V:` / `N`) that can't shape-collide with real data.
- **DuckDB progress bar flood on non-TTY stdout**: `enable_progress_bar`
  writes carriage-return updates that turn into thousands of newlines
  when stdout isn't a TTY. Now gated on `sys.stdout.isatty()` / `TERM`
  with a `HAVN_PROGRESS` env override.
- **`POST /api/transform` with no body**: returned 422; body is now
  optional via `Body(default_factory=...)`.
- **Unknown `/api/*` GETs returned the SPA `index.html`** with status
  200; the catch-all now 404s anything under `/api/`.
- **`havn query` truncation** at the server-side 50k row cap was
  silent; now surfaces a yellow warning to **stderr** (so CSV/JSON
  piped output stays clean) when the response's `truncated` flag is
  set.
- **`havn lint` defaults dropped RF03**
  (unqualified-reference-in-single-table) from the correctness rule
  list; it fires on idiomatic SQL and produced ~37 violations on a
  12-model project. Also pinned
  `unqualified_single_table_references=allow` in the pyproject
  sqlfluff config.
- **`run_assertions` early-returned on empty list**, so a model with
  only `@grain` (no `@assert`) never had its grain check run.
  Removed the early return; grain now always evaluates.
- **`havn check` caught `policy.deny` but `havn transform` built the
  model anyway**. Hoisted deny evaluation into `_evaluate_deny_rules()`
  called from `run_transform`; both sequential and parallel runners
  now pre-mark denied models as `policy_denied` before any tier
  executes.
- **Parallel runner blocked every later tier on ANY previous-tier
  failure**. Made blocking dependency-aware via `_is_blocked()`
  walking the actual `model.depends_on` graph; siblings of
  failed/denied models now build correctly.
- **`check_freshness` with `include_sources`** crashed on
  timestamp-with-timezone columns when pytz wasn't installed. Cast
  `MAX(<col>)` to VARCHAR in SQL so the value never crosses the
  DuckDB to Python boundary as a Python timestamp.
- **`_parse_duration`** now warns and falls back to 24h on malformed
  input (previously crashed with `ValueError` on `max_age=invalid`).
- **`parse_assertion_specs`** strips unrecognized `severity=`
  qualifiers (e.g. `severity=critical`) instead of leaving them in the
  expression where they crash `_evaluate_assertion` as bad SQL.
- **Shell statement detector rewritten** as a single forward pass:
  `SELECT 1; -- trailing comment` now correctly recognised as
  complete. Comment-swallowed semicolons and unterminated string
  literals handled correctly.

### Internal

- New `_havn.source_freshness` table; `_havn.assertion_results`
  migrated with `severity` + `owner` columns.
- `SQLModel` gains `grain`, `owner`, `source_freshness`, `watermark`,
  `assertion_specs`. `AssertionResult` gains `severity`, `owner`.
- `generate_structured_docs` surfaces grain/owner/source_freshness per
  model so the SPA can render them.
- `register_macros()` always loads `havn.stdlib.*` (even with no user
  `macros/` dir); user macros override on name collision.
- ducklake-extension tests skip with a `requires_ducklake` marker
  (probes once per session) when the extension can't be installed
  from `extensions.duckdb.org`, instead of failing with HTTP 403
  noise.

## [0.2.17] - 2026-04-29

Two regressions caught by the post-publish end-to-end re-test of 0.2.16.

### Fixed

- **`havn history` and `havn tables` now route through a running server**.
  0.2.13 added HTTP-routing for `havn query` so the warehouse-locked-by-server
  case stops being a dead-end, but `tables` and `history` were left on the
  direct DuckDB-open path. They now use the same sidecar-lockfile + HTTP
  fallback chain as `query`. End-to-end re-test confirmed: a no-op rerun
  produces a `havn history` output that includes both built and skipped
  rows from the server-side `_havn.run_log`.
- **`havn lint` no longer chokes on `@`-prefixed directives**. The linter
  stripped only legacy `--`-prefixed directive lines before handing SQL to
  SQLFluff, so canonical-syntax models produced 6 spurious `PRS`
  (parsing) violations against `@config materialized=table, schema=...`
  lines. Stripping now uses the engine's `_META_PREFIXES` set, which
  recognises both forms. End-to-end re-test confirmed: 6 PRS violations
  dropped to 4 real issues (AM04 unknown-result-columns from `SELECT *`,
  RF04 keyword `month` as identifier, AM05 unqualified joins) instead of
  being smothered by the parse failures.

## [0.2.16] - 2026-04-29

Closing the remaining loose ends from the candidate-test pass: the
history surface, lint experience, a flaky streaming test, lockfile
recovery on SIGKILL, and the welcome tour.

### Added

- **`havn lint --style`**. The default `havn lint` now runs a
  correctness-only rule set (ambiguity, references, unused CTEs,
  NULL-equality, blocked words, control flow, cast type). Layout,
  naming, and capitalisation rules are off by default and can be
  re-enabled with `--style` for a one-off cleanup pass. A project-level
  `.sqlfluff` overrides both. End-to-end against an aligned-`AS` SQL
  block: 135 violations -> 1 (the one being a real `AM04` "unknown
  number of result columns" issue, which is correctness).

### Fixed

- **Skipped transforms now appear in `_havn.run_log`** with
  `status='skipped'` and the same `pipeline_run_id` as their siblings.
  Previously a no-op pipeline run produced an empty `run_log` even
  though `_havn.job_runs` reported `steps_skipped=12`. `havn history`
  renders skipped rows in dim style so they don't crowd out the real
  events.
- **Stale `.havn/serve.json` lockfile is auto-cleaned**. `havn query`
  now checks whether the recorded PID is still alive before HTTP-routing
  and removes the lockfile if the server was SIGKILL'd or crashed.
  Falls through cleanly to the direct DuckDB-open path. Cross-platform
  (Windows uses `OpenProcess` + `GetExitCodeProcess`; POSIX uses
  `os.kill(pid, 0)`).
- **Webhook flush worker no longer races
  `test_status_reports_backlog`**. The `FlushWorker` previously ran its
  first drain immediately on start, which meant a `POST /api/ingest/
  webhook/<source>` followed by `GET /api/streaming/webhook/status`
  could observe an empty backlog if the worker drained between the two
  calls. The worker now waits one `flush_interval` before its first
  drain. Test suite is now deterministic (verified across 3 successive
  full runs).
- **First-time tour no longer pollutes the OUTPUT panel with a 404**.
  When the "Exploring Data" step pre-fills a query and there are no
  tables yet, it now seeds a friendly placeholder SQL with `run=false`
  instead of auto-running and triggering a "Warehouse not found" 404.
  In addition, `QueryPanel` suppresses the 404 from the OUTPUT log
  entirely so it never reaches users; the inline error in the Query
  panel still surfaces.
- **`previewCurrentFile` now strips `@`-prefixed directives** as well
  as legacy `--` comment headers so the preview pane renders successfully
  for SQL files written in the canonical syntax.

### Changed

- **Welcome tour trimmed from 11 steps to 6**. The tour now covers
  Welcome -> Navigation -> Project -> Transforms -> Explore -> Ready.
  DAG, Quality, Connectors, Pipelines, and Warehouse layout are
  discovered through the in-app hint system instead, which surfaces them
  contextually when relevant. The "Writing Transforms" copy now
  references `@config` and the auto-extracted dependency model.

## [0.2.15] - 2026-04-29

Documentation sweep. The user-facing kit (templates emitted by `havn init`)
already used the modern `@`-prefixed directive syntax, but README, docs/,
the in-app wiki, and a few code paths still showed the legacy
`-- config:` / `-- depends_on:` / `-- assert:` SQL-comment form. New
contributors and AI assistants reading the docs were learning the wrong
syntax for new code. No engine behaviour changes; both syntaxes still parse.

### Changed

- **README.md**: rewritten transform example to use `@config` and to call
  out that dependencies are auto-extracted from `FROM`/`JOIN` clauses, so
  `@depends_on` is optional.
- **CLAUDE.md**: SQL transform conventions section updated; the example
  block uses `@config` and the directive list documents `@config`,
  `@depends_on` (with auto-extraction note), `@description`, `@col`,
  `@assert`. Legacy syntax noted as still parsing for back-compat.
- **docs/*.md** (mkdocs site): `transforms.md` rewritten end to end with
  the new directive table including `unique_key`, `incremental_strategy`,
  `incremental_filter`, `partition_by`. `quality.md`, `contracts.md`,
  `index.md`, `lineage.md`, `macros.md`, `seeds.md`, `sources.md` updated
  inline.
- **src/havn/wiki/pages/*.md** (in-app wiki): same migration as docs/, plus
  `sentinel.md` updated to mention auto-extraction.
- **.github/copilot-instructions.md** and **PLATFORM_REPORT.md**: updated
  to document the canonical `@`-prefixed form and auto-extracted
  dependencies.
- **internal_LIMITATIONS.md**: updated the hypothetical SQL-include design
  example to use `@include` rather than the legacy comment form.
- **`havn agent` system prompt** (`server/routes/agent.py`): the
  conventions block the agent receives now uses `@config` / `@assert`,
  documents auto-extraction, and notes that legacy SQL-comment syntax
  still parses.
- **New-model scaffold** (`server/routes/models.py`): the placeholder SQL
  written by `POST /api/models` now uses `@config materialized=...,
  schema=...`. The "already has config" check accepts both `@config` and
  the legacy `-- config:` prefix so existing user templates aren't
  double-prefixed.
- **Notebook -> model promotion** (`engine/notebook/conversion.py`): the
  generated `.sql` file now emits `@config`, `@depends_on`, `@description`
  in canonical form. Tests updated accordingly.

## [0.2.14] - 2026-04-29

Hotfix on top of 0.2.13. Two of yesterday's fixes were incomplete:

### Fixed

- **Failed transforms now actually reach `_havn.run_log` from the server
  pipeline path.** 0.2.13 fixed the CLI/library code path (`engine/transform/
  execution.py` and `orchestration.py`) but missed the parallel
  `server/routes/pipeline.py` orchestrator that the UI uses. End-to-end
  re-test confirmed: a deliberately broken bronze model now produces a
  `status='error'` row in `_havn.run_log` with `pipeline_run_id` and the
  full error message attached, instead of just a `_havn.job_runs.failure`
  with empty `step_details`.
- **`havn lint` now actually excludes the noisy layout rules.** SQLFluff's
  `FluffConfig.from_kwargs(exclude_rules=...)` expects a list, not a
  comma-separated string. 0.2.13 passed a string, so SQLFluff iterated
  character by character and silently excluded nothing (LT01 / ST06 / LT05
  still fired). Fix: pass the value as a Python list. End-to-end re-test
  confirmed: an aligned-`AS` model now reports 1 real violation (`AM04
  unknown number of result columns`) instead of 3 layout nags.

## [0.2.13] - 2026-04-29

### Fixed

- **`havn serve --port N` is now strict.** When `N` is busy and the user
  passed `--port` explicitly, the server exits with code 2 and a clear
  message instead of silently rebinding to a different port. Auto-port
  selection still works when `--port` is omitted, but only as a neighbor
  search of up to 10 ports with a loud yellow warning. Background: an
  end-to-end test harness drove three concurrent servers expecting ports
  3010/3011/3012 and silently landed on a stale leftover server because
  havn rebinds without telling automation callers.
- **`havn query` (and `havn tables`, `havn history`) now route through a
  running `havn serve`.** When `havn serve` is up, the warehouse file is
  process-locked by DuckDB and a separate `havn query` invocation used to
  fail with "IO Error: Cannot open file ... already open in PID N". The
  CLI now writes a sidecar `.havn/serve.json` on serve start, and the
  query command checks that lockfile and forwards to the server's
  `/api/query` endpoint when one is running. Falls back to the direct
  DuckDB path with a clear error when no server is running.
- **Failed transform steps now appear in `_havn.run_log` with their
  `pipeline_run_id`.** Previously, when bronze layer models ran in a
  parallel tier and one of them failed, the per-step error row was
  written to `run_log` with `pipeline_run_id=NULL`, so a query like
  `SELECT * FROM _havn.run_log WHERE pipeline_run_id=?` for a failed run
  returned only the success rows -- making it look like the failure
  vanished. The parallel-tier path now passes `pipeline_run_id` through to
  `_execute_single_model` for both success and failure logs.
- **Flight SQL server: `cursor_for` import was scoped to one branch.** The
  import sat inside the `if backend_factory is None` branch, so callers
  that injected their own backend (notably the test suite) hit a
  `NameError: cannot access free variable 'cursor_for'` inside `do_get`.
  Hoisted the import to the outer scope. `tests/test_flight.py` now
  passes (was failing on main against pyarrow's flight client).
- **OUTPUT panel duration formatter no longer rounds 6-second jobs to
  `(0.0s)`.** When the SSE `model_end` event arrived before the
  per-model `model_start` had set `nodeStartTimes[name]` (or when both
  fired in the same animation frame), the wall-clock fallback returned
  ~0ms and was preferred over the server-reported `duration_ms`. The
  formatter now prefers the server-reported duration whenever present
  and renders sub-second jobs as `(123ms)`, 1-10s as `(2.34s)`, longer
  jobs as `(2.3s)`.
- **Stale "Pipeline complete" hint clears on next run.** The hint host
  now listens for a `havn_dismiss_completion_hints` window event, which
  PipelineContext fires when `startAndConnect` begins a new run. The
  toast reading "Pipeline complete. Use the Diff tab next time..." used
  to linger on screen across a fresh run-in-progress, leading users to
  think the new run had already finished.

### Changed

- **Masking policies created when auth is disabled default to
  `exempted_roles=[]`** instead of `["admin"]`. In auth-disabled mode the
  local user is auto-granted the admin role, so the legacy default made
  every policy silently inert for the only user who exists. Both the
  server route and the frontend dialog adopt the new default; existing
  policies are untouched. To restore the old behaviour explicitly, pass
  `exempted_roles=["admin"]` in the API or check the `admin` box in the
  Masking dialog before saving.
- **`havn lint` excludes the noisiest layout rules by default.**
  `layout.spacing` (LT01 -- whitespace before AS), `structure.column_order`
  (ST06 -- wildcards-then-targets), and `layout.long_lines` (LT05) are
  excluded when no project-level `.sqlfluff` file is present, so lint
  output prioritises correctness over style nags. Users who want the
  full SQLFluff defaults can drop a `.sqlfluff` next to `project.yml`.

## [0.2.12] - 2026-04-29

### Fixed

- **Codex agent sidebar overhaul.** The sidebar was effectively unusable
  with OpenAI's `codex` CLI: a fresh "Hey" reproducibly returned
  `The command line is too long.` on Windows because the ~16 KB system
  prompt + wiki index were being passed as argv elements past the
  `CreateProcess` 32 KB limit. The adapter also targeted flags that no
  longer exist in current codex builds (`--approval-mode auto-edit`,
  `--instructions`), so even a short message hit malformed-CLI errors.
  - The prompt and project context are now piped through stdin via
    `codex exec - --json`, which sidesteps the Windows argv limit
    entirely.
  - Switched to valid current flags: `--full-auto` for auto mode (now
    upgraded to `--dangerously-bypass-approvals-and-sandbox` so Auto
    actually allows file writes; codex 0.125's sandbox layer was
    silently overriding `--full-auto` and `--sandbox workspace-write`
    when the project wasn't pre-registered as trusted in
    `~/.codex/config.toml`), `--sandbox read-only` for ask mode,
    `--skip-git-repo-check`, `-m <model>`.
  - `_parse_event` rewritten for codex 0.125's actual JSONL schema:
    assistant text now arrives via `item.completed` /
    `agent_message.text`, file edits via `item.completed` /
    `file_change`, command runs via `command_execution`. The legacy
    0.46 `agent.message.delta` / `agent.message` paths are kept for
    back-compat. `thread.started` / `turn.started` /
    `turn.completed` are correctly ignored.
- **Codex sidebar now keeps conversation history across turns.**
  `codex exec` is stateless, so each follow-up was starting a fresh
  session with no memory of prior messages, so users would say "make
  the change" and the agent would ask which change. The adapter now
  captures the `thread_id` from the first turn's `thread.started`
  event and routes follow-ups through `codex exec resume <id> -`,
  which preserves the entire conversation. The system prompt is
  injected only on turn 1 since the resumed session already has it
  in history.
- **Codex file edits trigger live editor reload.** The sidebar's
  open-file refresh hook keys off `tool_use` chunks named `Edit` or
  `Write` (Claude Code's tool names). Codex 0.125 emits its own
  `file_change` items instead, so edits made by codex never
  triggered a reload: the file would change on disk but the
  Monaco editor kept showing the stale buffer until the user
  manually reopened it. The adapter now translates each
  `file_change` entry into an `Edit` or `Write` tool_use chunk
  (depending on `kind`), so codex edits behave identically to
  Claude Code edits in the UI.
- **Authentication failures surface as actionable errors.** A logged
  out codex previously emitted five `stream error: Failed to refresh
  token: 401 Unauthorized; retrying N/5` lines and exited with code
  0, leaving the user staring at retry spam. The adapter now detects
  the 401 / Unauthorized / "refresh token" pattern and appends a
  clear `Codex authentication failed (401 Unauthorized). Run
  \`codex login\` in a terminal to sign in, then try again.` line
  at the end of the stream.
- **Benign codex stderr noise is filtered.** Codex 0.125 emits
  `ERROR codex_core::session: failed to record rollout items:
  thread X not found` to stderr after every successful turn, a
  known internal recorder bug that doesn't affect the model reply.
  The adapter previously fell through to "surface stderr if no
  assistant text streamed" and showed this log as a fake error
  message. A small allow-list now drops these lines while keeping
  real errors.
- **`spawn_cli` accepts an optional `stdin` parameter** so adapters
  can pipe long inputs without losing the existing Windows
  npm-wrapper-resolution path that avoids `cmd.exe`
  command-injection risk.
- **Codex model dropdown** now lists the actual ChatGPT-account
  Codex model names (`gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`,
  `gpt-5.3-codex`, `gpt-5.2`) instead of the obsolete `gpt-5` /
  `o3` / `o4-mini` entries which the API now rejects with
  `400 Bad Request: The 'X' model is not supported when using
  Codex with a ChatGPT account.`

## [0.2.7] - 2026-04-25

### Fixed

- **Macros work on read-only connections.** `havn query`, the read pool,
  and any other read-only conn no longer log "Cannot execute statement
  of type 'CREATE' … read-only" -- `register_macros` now detects
  read-only mode, registers the Python UDFs (which `create_function`
  allows), and skips the `CREATE MACRO` aliases (which the writer has
  already persisted to the catalog).
- **`@table_macro` and SQL `CREATE MACRO` work on DuckLake.** The
  previous skip was based on a wrong assumption that DuckLake rejects
  persistent `CREATE MACRO`. Both paths are re-enabled; verified that
  non-TEMP `CREATE OR REPLACE MACRO ... AS TABLE` is visible from
  sibling cursors.
- **Parallel-transform "already exists" warnings on DuckDB silenced.**
  Sibling worker connections to the same on-disk DuckDB share the UDF
  catalog; the second registration's "already exists" is now a debug
  log, not a warning, since the function is callable either way.
- **`havn version create` / `havn version list` crashed** with
  `NameError: _warehouse_exists`. Missing import in `cli/version.py`.
- **`havn snapshot create` failed on DuckLake** because the
  `_havn.snapshots` DDL bypassed `_strip_pk`, then later `INSERT OR
  REPLACE` rejected the missing PK. Routed DDL through `_strip_pk`
  and switched to delete-then-insert.
- **`havn version create` failed on DuckLake** for the same two
  reasons (`_havn.version_history` DDL + `INSERT OR REPLACE`). Both
  fixed; `_ensure_version_tables` no longer silently swallows the
  underlying error.
- **`havn version create` snapshotted DuckLake's internal catalog**
  (every `__ducklake_metadata_warehouse.ducklake_*` table). Discovery
  now filters on `table_catalog = current_database()`.
- **Auth tables and `_havn.pr_builds`** routed through `_strip_pk` so
  `--auth` and the PR review surface work against DuckLake.
- **`havn tables` CLI** filtered out the DuckLake internal catalog
  (matching the API endpoint behavior).

### Docs

- `CLAUDE.md` referenced `havn diff --all`; the actual flag is
  `--full`.

## [0.2.6] - 2026-04-25

### Added

#### Resource Manager
- Per-category budgets (transform / query / streaming / system) with memory,
  thread, and max-concurrent knobs set in `project.yml`.
- `@governed` decorator + `governed_context` async context manager that
  acquires a category slot before each DuckDB operation and releases it on
  completion. Task list, duration, rows processed, and errors tracked live.
- `GET /api/resources`, `GET /api/resources/stream` (SSE),
  `PUT /api/resources/budgets`, and `POST /api/resources/cancel/{task_id}`.

#### Consumption layer
- `POST /v1/sql` Databricks-style SQL API: sync fast path, `202 + statement_id`
  for slow queries, `GET /v1/sql/{id}/result` polling, `DELETE /v1/sql/{id}`
  cancellation. Format negotiation via `Accept`: JSON envelope, NDJSON
  streaming, or Arrow IPC.
- Embedded Arrow Flight SQL server (`flight.havn.{domain}:8815`) with Bearer
  auth. Launched with `havn flight` or the `--flight` flag on `havn serve`.
- `POST /v1/export/duckdb` -- single-file DuckDB export (works for both
  DuckDB and DuckLake backends).

#### Streaming primitives
- `POST /api/ingest/webhook/{source}` -- staged webhook receiver. Background
  `FlushWorker` moves events from the staging table into `landing.<source>`
  every 15s (configurable).
- Postgres logical-replication CDC consumer via vendored pypgoutput.
  `havn cdc` command group to start/stop/inspect.
- Scheduled HTTP polling (`APIPollConsumer`) for REST sources without
  webhooks. High-watermark tracking in `_havn.cdc_state`. `havn poll`
  command group.
- DuckLake `MaintenanceScheduler` -- flush, merge small files, checkpoint,
  and snapshot expiration on a cron.

#### Observability
- `GET /metrics` Prometheus-format endpoint. Histograms for query and
  transform duration, counters for queries/transforms/rows/streaming events,
  gauge for active tasks per category.
- `GET /health` lightweight health probe (alias of `/api/health`).
- Optional JSON log format via `HAVN_LOG_FORMAT=json` -- one JSON object per
  record with ISO-8601 timestamps and structured context from `extra={…}`.

#### Macros
- `@table_macro` decorator for Python functions returning `list[dict]`,
  callable from SQL as `SELECT * FROM my_macro(arg)`. No pyarrow
  requirement -- DuckDB's native `json_each()` streams rows.
- Macro hot-reload: editing a file in `macros/` while `havn serve` runs
  re-registers the UDF on the next query (debounced 2s).
- Monaco editor autocomplete and hover for all registered macros.

### Fixed

- **DuckLake end-to-end:** DDL rewriting, cursor catalog tagging
  (`USE warehouse` applied per cursor and via `WriteQueue.cursor()`), UDF
  GC pinning, parallel-write serialization (`max_workers=1` on DuckLake),
  single-attach safety across every route handler that previously called
  `backend.connect()` directly. `havn init --backend ducklake` followed
  by `havn serve` and a job run now works without manual intervention.
- **DuckLake catalog auto-migration:** ATTACH passes
  `AUTOMATIC_MIGRATION TRUE`, so projects created with an older DuckLake
  build keep working after the extension upgrades.
- **DuckLake DDL strip is narrower.** Function-call DEFAULTs
  (`current_timestamp`, `gen_random_uuid()`) and boolean DEFAULTs are
  supported by DuckLake; only PRIMARY KEY / UNIQUE / CHECK and
  `nextval(...)` defaults are stripped now. Metadata tables get proper
  ids and timestamps again.
- **Runs panel was empty on DuckLake.** Endpoints reading `_havn.run_log`
  used a write-queue cursor that defaulted to the `memory` catalog, so
  they were querying an empty `memory._havn`. The cursor now applies
  `USE warehouse`.
- **Job orchestration didn't log transform steps to `run_log`,** so the
  History panel only showed stand-alone `havn run` invocations. Job
  steps now write to `run_log` tagged with the pipeline's `run_id`.
- **`information_schema` is browseable** in the table tree on both
  backends (it doesn't list itself in `information_schema.tables`, so
  the API now UNIONs `duckdb_views()`).
- **`__ducklake_metadata_warehouse`** internal catalog tables no longer
  pollute the table browser (filtered to `current_database()`).
- **System schemas** (`information_schema`, `_havn`, `main`, anything
  starting with `__`) are dimmed and collapsed by default in the schema
  tree.
- **`_havn.model_state` duplicate-key error** is no longer possible:
  `INSERT OR REPLACE` on DuckDB, transactional DELETE+INSERT on DuckLake.
- **VARCHAR columns with numeric content** (IDs, phone numbers, ZIP codes)
  no longer get thousands-separator formatting; the result grid respects
  the database column type when supplied (and the query API now returns
  `column_types` to do so).
- **Result-grid header / body alignment** in virtualized rendering
  (>200 rows). The two tables share fixed column widths so headers stop
  drifting when column names are wider than data.

### Changed

- `duckdb` pinned to `>=1.5.2` (DuckLake requires it).
- Ingest, export, and notebook runs now go through the ResourceManager
  for consistent budget enforcement and visibility.
- Consumption layer adds `prometheus_client` and `pyarrow` as core
  dependencies.

### Removed

- **Resources tab** is hidden from the Observe section. It will return
  once the panel is rebuilt -- the current implementation can't scroll
  and never registers as the active tab.
