# Changelog

All notable changes to havn are documented in this file.

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
