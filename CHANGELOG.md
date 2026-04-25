# Changelog

All notable changes to havn are documented in this file.

## [0.2.7] — 2026-04-25

### Fixed

- **Macros work on read-only connections.** `havn query`, the read pool,
  and any other read-only conn no longer log "Cannot execute statement
  of type 'CREATE' … read-only" — `register_macros` now detects
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

## [0.2.6] — 2026-04-25

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
- `POST /v1/export/duckdb` — single-file DuckDB export (works for both
  DuckDB and DuckLake backends).

#### Streaming primitives
- `POST /api/ingest/webhook/{source}` — staged webhook receiver. Background
  `FlushWorker` moves events from the staging table into `landing.<source>`
  every 15s (configurable).
- Postgres logical-replication CDC consumer via vendored pypgoutput.
  `havn cdc` command group to start/stop/inspect.
- Scheduled HTTP polling (`APIPollConsumer`) for REST sources without
  webhooks. High-watermark tracking in `_havn.cdc_state`. `havn poll`
  command group.
- DuckLake `MaintenanceScheduler` — flush, merge small files, checkpoint,
  and snapshot expiration on a cron.

#### Observability
- `GET /metrics` Prometheus-format endpoint. Histograms for query and
  transform duration, counters for queries/transforms/rows/streaming events,
  gauge for active tasks per category.
- `GET /health` lightweight health probe (alias of `/api/health`).
- Optional JSON log format via `HAVN_LOG_FORMAT=json` — one JSON object per
  record with ISO-8601 timestamps and structured context from `extra={…}`.

#### Macros
- `@table_macro` decorator for Python functions returning `list[dict]`,
  callable from SQL as `SELECT * FROM my_macro(arg)`. No pyarrow
  requirement — DuckDB's native `json_each()` streams rows.
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
  once the panel is rebuilt — the current implementation can't scroll
  and never registers as the active tab.
