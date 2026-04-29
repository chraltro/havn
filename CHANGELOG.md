# Changelog

All notable changes to havn are documented in this file.

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

## [0.2.7] -- 2026-04-25

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

## [0.2.6] -- 2026-04-25

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
