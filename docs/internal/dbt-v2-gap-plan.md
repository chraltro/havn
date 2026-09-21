# dbt v2 gap plan

Written 2026-09-21. Input: the gap list from the dbt v2 comparison, plus five
research passes over the current code (sqlglot 30.18, DuckDB 1.5.5, havn 0.2.27).
Every claim below about "what exists today" was checked against the tree or run
as an experiment; scratch scripts are referenced where a number came from one.

Effort scale is the ideas-catalog one: S = hours to a day, M = days, L = a week
or more.

## 0. What dbt v2 actually is, as of this week

Checked against the docs source repo and GitHub on 2026-09-21 (docs.getdbt.com
itself is blocked from this environment, so the docs were read from
`dbt-labs/docs.getdbt.com@current`).

- dbt v2.0 went GA on 2026-09-16. Fusion was renamed to plain "dbt", v2.0.5
  is current, and v1.12 is maintained in parallel. Two distributions, both
  free: `dbt` (Rust, proprietary product license) and `dbt-oss` (Apache 2.0).
  No model-count gate anywhere in the licensing docs.
- Static analysis has three modes: `off`, `baseline` (default, findings are
  warnings, uses the remote warehouse as the source of truth) and `strict`
  (nothing runs until the whole project binds). **Strict requires `dbt login`
  with a free platform account; unauthenticated runs fall back to baseline.**
  Column-level lineage, SQL type diagnostics, rename column and go-to-column
  in the VS Code extension are strict-only, so in practice they need the login.
  Table lineage, ref go-to-definition, rename model and CTE preview are free
  without login.
- DuckDB in v2: built in, CLI only, not supported on the platform. The
  bundled driver cannot load DuckDB extensions (`httpfs`, `parquet`,
  `spatial`); the workaround is a system driver. Static analysis cannot infer
  the schema of `read_csv`, `read_parquet` or `read_json` at analysis time.
  Gaps are tracked in an open epic, `dbt-labs/dbt-core#14393` "[EPIC] DuckDB",
  opened 2026-03-17. There is no DuckDB function-support table; those exist
  for Snowflake and BigQuery only. dbt's own docs call DuckDB both a community
  adapter and a trusted adapter in the same paragraph.
- Unit tests: `compute: local` runs the test in DuckDB, but it is behind an
  experimental env var, Snowflake and BigQuery only, and the direct upstream
  models must already exist in the warehouse so dbt can fetch their schemas.
  It is warehouse-compute-free per test, not warehouse-free.
- Snapshots: `strategy: timestamp|check`, `hard_deletes: ignore|invalidate|
  new_record`, `snapshot_meta_column_names` to rename the four meta columns,
  `dbt_valid_to_current` for a sentinel instead of NULL. Nothing changed for
  v2.
- Microbatch: GA on v1.9+. `event_time`, `begin`, `batch_size: hour|day|month|
  year`, `lookback`, `concurrent_batches`. `dbt-duckdb` 1.10.1 (2026-02-17)
  supports it, but `dbt-duckdb` is a v1 adapter and v2's DuckDB adapter is a
  separate Rust implementation with a parity bug-bash still going.
- `on_schema_change` default is `ignore`, under which a removed column fails
  the run and an added column is not appended. `sync_all_columns` includes
  type changes. No option backfills old rows. Nested columns are not tracked.
- Selectors: `+`, `n+`, `@`, `*`, `,`; methods include `tag`, `path`, `fqn`,
  `config.*`, `state:modified` with subselectors, `result:`, `source_status`,
  `unit_test`. Tags and meta do not trigger `state:modified`. Defer needs
  `--defer` plus `--state <manifest dir>`; ephemeral models are never
  deferred; `--favor-state` prefers the manifest over local objects.
- Packages: `packages.yml` or `dependencies.yml`, hub with version, git with
  revision, local path, `package-lock.yml`. v2 requires a `provider` key for
  private packages, clones over system SSH, and warns (soon errors) on
  packages that do not declare `2.0.0` compatibility.
- Install: `python -m pip install dbt`, brew, curl or winget; a single
  self-contained binary. The GitHub README carries no shell one-liner at all,
  just a link to the install page.

What this means for the plan: havn's "no data leaves the machine" pitch lines
up exactly against the two places v2 sends you online. Strict analysis and
column-level features need a login; havn can do the same analysis fully
offline because the binder is in-process. And v2's unit tests need the
warehouse for schemas; havn's can be genuinely warehouse-free. Those are the
two sentences the README comparison should be built around once phases 1 and
2 ship.

## 1. Verdict table

| Gap | Feasible | Effort | Verdict |
|---|---|---|---|
| Static type inference | Yes, via the DuckDB binder | M | Build. Do not reimplement types in sqlglot. |
| Types into contracts | Yes, but contracts have no column surface yet | M | Build after type inference; needs a type-equivalence policy or it is a false-positive generator. |
| Lineage conformance suite | Yes | M | Build. 6 of 13 constructs are wrong today. 39 s per 1000 models is 78 % redundant catalog I/O, not sqlglot. |
| Editor: live markers | Yes | M | Build. Same bind endpoint as type inference; the work is position plumbing. |
| Editor: go-to-definition | Yes | S | Build. Cheapest item on the list. |
| Editor: CTE preview | Yes | S | Build. Execution half already exists. |
| Editor: rename column | Yes, but not on today's lineage | L | Defer. Would ship silently wrong edits until the lineage rewrite lands. |
| Unit tests | Yes | M | Build. Rewriter, comparator and macro registration all exist in pieces. |
| SCD2 snapshots | Yes | M | Build, after the incremental path is made transactional. Naming collision with three existing "snapshot" modules. |
| Ephemeral models | Yes | S to M | Build. Transitive hashing already handles invalidation. |
| Schema-change policy | Yes | S, plus a prerequisite fix | Build. Today: removed columns silently diverge, int to double silently rounds, int to varchar loses rows. |
| Microbatch | Yes, but thin foundation | M | Defer. The loop and per-batch state are the work; nothing exists. |
| Graph selectors | Yes | S | Build. The grammar already exists for jobs; it is one call site plus a hash-corruption fix. |
| Defer to prod | Yes, with a hard DuckDB constraint | M | Build as opt-in. Fails whenever any process holds prod open for writing. |
| Parse and lineage at 1000 models | Yes | S | Fix now. One bulk catalog fetch removes 36 of 39 seconds. |
| Packages | Yes | L | Defer. No config, no fetch, no multi-root discovery, no namespacing, silent name collisions. |
| README hero | Yes | S | Do first. The wheel bundles the frontend; the one-pip-line hero is honest today. |
| Screenshot | Yes | S | Do first. `landing/screenshots/overview.webp` already exists. |
| Supported / not supported page | Yes | S | Do first. |

The three items Christian called out (type inference, editor diagnostics, unit
tests) are all buildable and none of them needs a Rust rewrite. The first two are
one feature seen from two sides: a bind endpoint that types a model without
running it, and an editor that shows what the bind endpoint says.

## 2. Prerequisites: bugs found while researching

These are not features. Several of the features above would inherit them or make
them worse, so they go first.

1. **Incremental path is not transactional.** `_execute_incremental`
   (`engine/transform/execution.py:25-197`) issues ALTER, DELETE, UPDATE and
   INSERT as separate auto-commit statements. The only `BEGIN TRANSACTION` in
   the transform package is the DuckLake branch of `_update_state`. Reproduced:
   `delete+insert` with a retyped column commits the DELETE, fails the INSERT,
   and leaves the target empty. SCD2, microbatch and `on_schema_change` all
   layer more dependent writes on this path. Fix: wrap the staging branch in a
   transaction, rollback on any exception. Effort S.

2. **Targeted runs corrupt change detection.** `run_transform` filters models
   before `build_dag`, so `_compute_upstream_hash` sees no upstream models and
   writes `sha256("")` as the upstream hash. Reproduced on the sample project:
   `havn transform gold.earthquake_summary` followed by a plain
   `havn transform` spuriously rebuilds the model. Fix: hash against the full
   model map, execute against the selected subset. Selectors make this worse
   if not fixed. Effort S.

3. **Column lineage does O(n) catalog scans per model.**
   `extract_column_lineage` runs `information_schema.columns` once per
   dependency (`engine/sql_analysis.py:537-546`), and that query costs 3 to
   19 ms depending on catalog size. Over 1000 models: 39 s, of which 78 % is
   `_duckdb.execute`. `validate_models` already does one bulk fetch
   (`transform/analysis.py:86-92`). Hoist the same into lineage. Ideas-catalog
   1.8 (content-hash lineage cache) is aimed at the remaining 2 s. Effort S.

4. **Each model is parsed four times per pass.** Discovery, validation,
   deny-rule check and lineage each call `parse_one` on the same SQL. Cache the
   AST on `SQLModel`. Effort S.

5. **sqlglot floor is too low for positions.** `pyproject.toml` says
   `sqlglot>=26.0`. Token positions on `Identifier.meta` appear in 26.17.
   Below that, position-based features return empty results with no error.
   Raise the floor to 26.17 before any editor work. Effort S.

6. **`strip_config_comments` destroys the line map.** It deletes `@`-prefixed
   lines anywhere in the file, so sqlglot line numbers do not map to file lines
   by a constant offset. Blank the lines in place instead of removing them, or
   return a line map. Also: `lint_file` only strips the legacy `-- config:`
   form, so `/api/lint/file` line numbers are wrong for every modern model.
   Effort S.

7. **Unknown `@config` keys are silently ignored.** `materialised=table` builds
   a view. Any new key added below (`on_schema_change`, `tags`) would suffer the
   same. Add an unknown-key error in `validate_models`. Effort S.

8. **Model-name collisions are silent.** Two files producing the same
   `schema.name` make `build_dag` keep one with no warning. Packages would make
   this common; it is already possible with `@config schema=` overrides. Add a
   duplicate check in discovery. Effort S.

9. **`onOpenModel` in `App.jsx:1296` hardcodes `transform/{schema}/{name}.sql`.**
   Wrong whenever `@config schema=` overrides the folder. `/api/models` already
   returns the real path. Effort S.

All nine together are roughly two to three days and make the codebase honest
before anything visible is built on it.

## 3. SQL comprehension

### 3.1 Static type inference: delegate to the DuckDB binder

**What exists.** `validate_models` (`transform/analysis.py:32-294`) is a
sqlglot walk plus name lookups against `information_schema`. It does not select
`data_type`. It skips column checks entirely for any upstream that has not been
built, so on a fresh warehouse a bad column on an unbuilt upstream passes.
There is no type checking anywhere.

**Why the binder and not sqlglot.** Measured on DuckDB 1.5.5:

- `DESCRIBE`, `EXPLAIN` and `PREPARE` all bind without reading data. On a
  10M-row table each takes under 1 ms, flat in table size. Full execution of the
  same query: 53 ms.
- The binder catches, at bind time: wrong arity, unknown functions, operator
  overload failures (`'a' + 1`), missing columns, missing struct keys, ambiguous
  references, aggregation without GROUP BY, set-operation column count
  mismatch.
- The binder does not catch value-domain conversion: `CAST(varchar_col AS
  INTEGER)`, `varchar = int` in a WHERE, `int JOIN varchar` in an ON. Those bind
  fine and fail at execution. This is exactly dbt Fusion's boundary too.
- sqlglot `annotate_types` with a schema dict: 18 of 30 DuckDB expressions
  exactly right, 8 UNKNOWN (including `date_trunc`, which is in every model),
  and `epoch_ms` silently wrong (TIMESTAMP instead of BIGINT). It cannot see
  havn's Python `@macro` UDFs at all. Owning that gap against a `>=26.0` pin
  is a permanent tax.

**Design: shadow catalog bind pass.** Verified in `scratchpad/shadow_probe.py`:

1. Take a cursor off the write queue's connection (the read pool is
   `read_only=True`, and DuckDB refuses `ATTACH ':memory:'` on a read-only
   connection).
2. `ATTACH ':memory:' AS shadow_<request_id>`. ATTACH is instance-scoped, so the
   name must be unique per request. Create the model schemas inside it.
3. For every base object (landing tables, seeds, sources, anything not a
   model): `CREATE VIEW shadow.landing.x AS SELECT * FROM <main>.landing.x`.
   This binds the real types without copying rows.
4. Register macros on the cursor (`register_macros` works on any connection).
5. `USE shadow_<id>`, then for each model in topological order
   `CREATE VIEW schema.name AS <model.query>` using the file's SQL verbatim.
   Two-part names resolve inside the shadow, so a stale built table in the
   main catalog is shadowed by the fresh definition. This is the property that
   makes validation reflect the file, not the last build.
6. `DESCRIBE` each view to get `(column, type)`. Collect bind errors per model
   with the DuckDB message. Continue past a failed model; downstream models
   report "upstream failed to bind" rather than a cascade.
7. `USE <main>; DETACH shadow_<id>` in a finally block.

Cost: 2.6 ms per model on the synthetic 1000-model project, 2.6 s total, which
is cheaper than the current lineage pass. Single model with a short upstream
chain: about 6 ms end to end.

**Where it plugs in.**

- `validate_models` gains a `bind=True` path that returns `ValidationError`
  rows with severity error for bind failures, and returns the inferred schema
  per model as a side result.
- `havn validate` prints bind errors. The pre-build gate in
  `server/routes/pipeline.py:510` and `:908` already calls `validate_models`;
  note it is skipped when the run includes ingest steps, which needs a
  decision (run it after ingest, before transforms).
- New endpoint `POST /api/bind` taking `{sql, model}` for the editor
  (section 4).
- Persist the inferred schema per model in `_havn.model_columns` (or a JSON
  column on `model_state`) from `_update_state`, keyed by `content_hash`. Nothing
  persists column types today; `model_profiles` has names only.

**Risks.**

- The shadow must be seeded completely: extensions loaded, macros registered,
  sources and seeds present. A missing UDF produces a spurious "function does
  not exist" which is worse than no check. Seed the shadow from the same code
  path `connect()` uses.
- Marketing: this is "resolves types and catches bind errors", not "catches
  type errors". The first `CAST` that binds clean and dies mid-build will burn
  trust if the docs overclaim.
- DuckLake backend: the catalog is already an ATTACH and rejects a second
  file attach in-process. In-memory attach should still work, but verify.
- `DECIMAL(38,1)` for a `SUM`: inferred types are precise in ways users find
  surprising. Relevant for 3.2, not for the bind check itself.
- File reads: `read_parquet('x.parquet')` binds by reading the file footer,
  which is cheap; `read_csv` sniffs rows and can be slow on a large file.
  Cache by file mtime. This is an advantage to state plainly: dbt v2 cannot
  type these calls at analysis time on DuckDB at all.
- Extensions: the bind cursor must load the same extensions the run does, or
  `httpfs` and `spatial` calls fail to bind. Same seeding rule as macros.

Effort M. Steps: shadow bind function in `transform/analysis.py` (1 day),
wire into `validate_models` and CLI (half day), persistence (half day),
tests including the seeded-shadow cases (1 day).

### 3.2 Inferred types into contracts

**What exists.** Less than the gap list assumes. `Contract`
(`engine/contracts.py:59-70`) is name, assertions, severity, freshness. There
is no column, type or nullability declaration in the schema or the docs.
Contracts run strictly post-build against a materialized table and return
"table does not exist" otherwise. So this is not "move a check earlier"; the
check does not exist.

**Design.**

1. Add a `columns:` block to contract YAML: name, type, optional `nullable`,
   optional `description`. Also accept a `@contract` directive form if the
   YAML feels heavy for one model.
2. Compare the bind-pass schema (3.1) to the declaration before the build.
   Missing column, extra column when `strict: true`, type change.
3. Type-equivalence policy, or this is a false-positive generator. Reuse
   `_TYPE_GROUPS` and `_TYPE_WIDTH` from `engine/sentinel.py:250-289`, which
   already classify widen/narrow/changed for source schemas. Default: warn on
   widening (`INTEGER` to `BIGINT`, `DOUBLE` to `DECIMAL(38,p)`), error on
   narrowing or category change.
4. Secondary use: with the baseline persisted per model, "this change alters
   the output schema of gold.orders" becomes a validate-time warning even
   without a contract, which is the dbt Fusion pitch.

**Risk.** Volume of noise. The existing `Ambiguous column` warning fired 400
times on 1000 synthetic models. Ship the schema-change warning off by default
until the equivalence rules have been tuned on a real project.

Effort M, after 3.1.

### 3.3 Lineage conformance

**What exists.** A bespoke 360-line walker (`engine/sql_analysis.py:467-640`),
not `sqlglot.lineage`. Table-level dependency extraction is healthy and got
every construct right, including PIVOT and ASOF JOIN. Column lineage, on 13
constructs (`scratchpad/lineage_probe.py`):

| Construct | Result |
|---|---|
| CTE chain | correct |
| Nested subquery in FROM | wrong: emits the subquery alias `t` as a source table |
| Window function | correct |
| UNION ALL | wrong: every branch after the first is dropped (`_find_main_select`) |
| `SELECT *` over a join | wrong: duplicate `customer_id` collapses to one source by dict-key collision |
| `SELECT * EXCLUDE (x)` | wrong: `x` still reported |
| `SELECT * REPLACE (expr AS x)` | wrong: the replacement expression is never inspected |
| Struct dot `s.field` | wrong: `s` emitted as a table |
| Struct bracket `s['field']` | correct but lossy |
| `unnest` | correct |
| Correlated subquery | correct, over-broad |
| QUALIFY | correct |
| PIVOT | empty result |

`sqlglot.lineage` with a schema dict fixed nested-subquery and UNION ALL
outright in the same harness. It will not fix EXCLUDE, REPLACE or PIVOT,
because those are star-expansion features.

**Design.**

1. Turn `scratchpad/lineage_probe.py` into `tests/test_lineage_conformance.py`
   with DuckDB `DESCRIBE` as ground truth for the output column set. This is
   the suite the gap list asks for and it exists as a script already. Add
   ASOF JOIN, UNPIVOT, LATERAL, `COLUMNS(...)`, `GROUP BY ALL`, `UNION BY
   NAME` and recursive CTE.
2. Replace the walker's core with `sqlglot.lineage` fed by the bind-pass
   schema from 3.1. That is what makes `SELECT *` resolvable: the shadow
   `DESCRIBE` says what the star expands to, per CTE and per subquery.
3. Keep a star-expansion layer for EXCLUDE and REPLACE driven by the same
   schema.
4. PIVOT: derive the output columns from `DESCRIBE` and map them all to the
   pivoted source columns. Precise per-column lineage through PIVOT is not
   worth the effort.
5. Prerequisite 3 (bulk catalog fetch) and 4 (shared AST) first.

**Risk.** It is a rewrite of a module with four call sites and accumulated
regression fixes (see the `_is_cte_ref` comment at `sql_analysis.py:420`).
The conformance suite is the safety net; build it first and run the current
walker against it so the regressions are visible.

Effort M.

## 4. Editor

**What exists.** `Editor.jsx` registers a completion provider and a hover
provider on language `sql` at module load, and two actions (format, preview).
No markers, no definition, no rename, no code actions anywhere in
`frontend/`. Providers close over three caches and know nothing about which
file is open; the model's identity is not available to them. The column cache
has no TTL and no invalidation. `QueryPanel.jsx` is a textarea, not Monaco, so
nothing built here reaches the Query tab.

**Prerequisite for all four sub-features.** Plumb `activeFile` (and the
model's `full_name`) into provider scope, and give each open file a real Monaco
model URI. Today every file shares one anonymous model. Effort S, and it is
the same work for markers, definition and rename.

### 4.1 Live error markers

**Design.** One endpoint, two sources of diagnostics.

- `POST /api/bind` with `{path, content}`: runs the shadow bind (3.1) for the
  single model with its upstream chain, against the unsaved buffer. Returns
  `[{severity, message, line, col, endLine, endCol}]` plus the inferred output
  schema. Read permission, pooled cursor. Debounce at 300 to 500 ms in the
  editor. Also returns the inferred schema so hover can show types.
- Lint diagnostics from `/api/lint/file` (already accepts inline content and
  already returns `line` and `col`) on save or a 1 s idle debounce. SQLFluff
  is 155 ms per lint on a 60-line model, not a per-keystroke budget. Note it
  needs `execute` permission today; drop to `read` for the diagnostics use.
- `setModelMarkers` with owner `havn-bind` and `havn-lint` separately.

**Positions.** DuckDB binder and catalog errors carry `LINE n:` and a caret.
Column is `caret_index - len("LINE n: ")`. Two problems, both solvable:

- Long lines are truncated with a `...` prefix, so the caret offset is wrong.
  Recovery: search the real source line for the quoted identifier from the
  message. Ambiguous when the identifier appears twice on the line; fall back
  to a whole-line marker.
- Parser errors sometimes have no position ("syntax error at end of input").
  Whole-file marker on the last line.
- Prerequisite 6 (line map through `strip_config_comments`).

**Hover with types.** With the bind result cached per buffer, hover on a
column shows the inferred type. Cheap once 4.1 is done, and it is the most
visible "the editor understands my SQL" signal.

Effort M. Backend endpoint half a day on top of 3.1, frontend markers and
debounce one day, position recovery and tests one day.

### 4.2 Go-to-definition on a model reference

**What exists.** `/api/models` returns `full_name` and `path` from
mtime-cached discovery. `openFileAtLine` and the `goToLine` prop already
exist. Nothing wires them to a definition provider.

**Design.** `registerDefinitionProvider` on `sql`: regex the token under the
cursor as `schema.name`, look it up in a cached `/api/models` map, return a
`Location` with the file's URI. If URI plumbing is annoying, a
`registerLinkProvider` plus a click handler calling `openFile` sidesteps it.
Column-level definition (jump to the line that defines a column upstream)
needs positions from lineage, which arrive with 3.3; skip for now.

Effort S.

### 4.3 Preview a single CTE

**What exists.** `/api/query` accepts `WITH ... SELECT` (the safety validator
walks CTEs to find the real verb), applies masking (CTE-aware), the query
governor, and a `limit`. The whole-model preview on Cmd+Enter exists. Nothing
enumerates CTEs.

**Design.** Parse with sqlglot, enumerate `parsed.args["with"].expressions`,
find the CTE containing the cursor, slice the original text by
`Identifier.meta` `start` and `end` offsets (preserves formatting, avoids
round-trip drift on DuckDB syntax) to build `WITH <ctes 0..n> SELECT * FROM
<n> LIMIT 100`. Post to `/api/query` with `limit`. Surface as a code lens above
each CTE and as a Cmd+Shift+Enter action. Refuse recursive CTEs and CTEs that
reference later siblings.

While there: `previewCurrentFile` sends no limit and its directive-stripping
loop stops at the first non-directive line, so a mid-file `@assert` is sent to
DuckDB as SQL.

Effort S.

### 4.4 Rename a column across downstream models

**What exists.** `impact_analysis` (`transform/analysis.py:297-373`) gives
forward impact, but it is built on `extract_column_lineage`, which only walks
SELECT-list expressions. There is no reference to `exp.Where`, `exp.Join`,
`exp.Group`, `exp.Order` or `exp.Having` anywhere in `sql_analysis.py`. A
downstream model that filters or joins on the column without projecting it
produces zero hits. A rename built on this ships broken SQL with a green
check.

sqlglot positions are sufficient for exact edits: verified that both
occurrences of `customer_id` in `SELECT a.customer_id ... ON a.customer_id =
b.cid` carry distinct `start`/`end` offsets on `Identifier.meta`, including
qualifiers and aliases. `SELECT *` yields no `Column` nodes, so a rename cannot
see through a star and must refuse.

**Design, when it is time.** A new reference index that walks every
`exp.Column` in every downstream model (the shape already exists at
`analysis.py:179`), resolves each to a source via the schema from 3.1, and
returns `{path, start, end}` per occurrence. Then `registerRenameProvider`
returning a `WorkspaceEdit`, applied through a new multi-file write endpoint
(today `PUT /api/files/{path}` writes one file, non-transactionally). Refuse
when any downstream model reaches the column through `SELECT *`.

**Verdict.** Defer until 3.1 and 3.3 are in. Effort L including the multi-file
write path. Impact preview ("these 7 places reference this column") is a good
intermediate that needs the index but not the edit path, and it is honest.

## 5. Unit tests

**What exists.** Nothing in product code. The closest primitives, and all of
them are reusable:

- `diff.py:236-238` already runs a model's SQL into a temp table without
  touching the target.
- `_row_hash_expr` (`diff.py:195-209`) is a type-drift-proof row comparator.
- `register_macros(conn, project_dir)` works on any connection, including
  `:memory:` (verified).
- References are literal `schema.table`, with no `ref()` indirection, so the
  harness must rewrite `exp.Table` nodes. Nothing in the tree mutates Table
  nodes today, but the round-trip through sqlglot 30 was verified on 10
  DuckDB-specific constructs (EXCLUDE, REPLACE, lambdas, struct dot, slices,
  QUALIFY, UNION BY NAME, COLUMNS regex, TRY_CAST, GROUP BY ALL), all
  semantically identical after regeneration.

**File format.** YAML, following `load_metrics` in `engine/semantic.py:147`
(non-fatal collected errors, identifier validation, duplicate detection).
Multi-row fixtures do not fit the single-line `@` directive shape, and
`_split_config_pairs` would break on them.

```yaml
# tests/unit/silver_customers.yml
model: silver.customers
tests:
  - name: counts orders per customer
    given:
      bronze.customers:
        columns: {customer_id: INTEGER, name: VARCHAR}
        rows:
          - [1, "Ann"]
          - [2, "Bo"]
      bronze.orders:
        rows:
          - {order_id: 1, customer_id: 1, amount: 5.0}
    expect:
      rows:
        - {customer_id: 1, name: "Ann", order_count: 1}
        - {customer_id: 2, name: "Bo", order_count: 0}
```

`columns:` is optional but recommended: a `(VALUES ...)` mock retypes `5.0`
to DECIMAL, so mocks should be `CREATE TEMP TABLE` with declared types, and
when not declared, take the types from the bind pass (3.1) or from the live
catalog.

**Execution.**

1. Fresh `:memory:` connection, `register_macros`, load extensions.
2. Create one temp table per `given` entry.
3. Parse the model, replace every `exp.Table` whose `schema.name` is in
   `given` with the mock name, preserving the alias. Any upstream reference
   not mocked is an error, not a fallback to the warehouse; that is the
   "no warehouse state" property.
4. Run into a temp table, compare to `expect` with `_row_hash_expr`, order
   insensitive by default with an `ordered: true` option. Report missing and
   unexpected rows.
5. `havn test`, `POST /api/unit-tests/run`, and a UI tab beside Quality.
   `havn check` runs them too.

**Risks.** `SELECT *` models: a mock declaring fewer columns than production
silently narrows the output and the test passes against a fiction. Mitigate
by defaulting missing mock columns from the bind schema and warning when a
mock is narrower than the real table. Incremental models: test the full-refresh
SQL only, state it in the docs (dbt has the same limitation and tracks it in
an open issue). Models sqlglot cannot parse currently fall back to regex for
dependency extraction; the rewriter has no safe fallback and must hard-fail
with a clear message.

**Positioning.** dbt's `given`/`expect` shape is worth copying so the YAML
reads familiar. The difference to advertise: dbt v2's local unit tests are
experimental, Snowflake and BigQuery only, and need the upstream models to
exist in the warehouse for schema fetch. havn's run on an in-memory DuckDB
with nothing but the mocks, because the engine and the warehouse are the
same thing.

Effort M: engine one to two days, CLI and API half a day, UI one day, docs
half a day.

## 6. Modeling

### 6.1 SCD2 snapshots

**What exists.** Three modules already called snapshot-something, none of them
row-level: `snapshot.py` is metadata fingerprints, `snapshots.py` and
`versioning.py` are whole-table Parquet for rewind. `cdc.py` is extraction
watermarks. Grep for `valid_from`, `valid_to`, `is_current`, `check_cols`
across product code: zero hits. The `merge` incremental strategy is an
unconditional UPDATE plus anti-join INSERT with no hash comparison.

The SQL was verified in `scratchpad/exp_scd2.py` across three runs (initial
load, changes plus hard delete plus new key, identical replay): correct and
idempotent, including the A to B to A revert case. DuckDB 1.5.5 has `MERGE
INTO` but it cannot close and insert for the same key in one pass, so three
statements plus one for hard deletes are needed regardless.

**Design.**

- `@config materialized=snapshot, unique_key=customer_id, strategy=check`
  (or `strategy=timestamp, updated_at=updated_at`), `check_cols=all|[a,b]`,
  `hard_deletes=ignore|invalidate|new_record` (dbt's exact values, so the
  migration is a search and replace). Lives in `transform/` like any model;
  no fourth directory.
- Target columns: user columns plus `valid_from`, `valid_to`, `is_current`,
  `row_hash`. Make the meta column names configurable in project.yml for people
  migrating from dbt (`dbt_valid_from`, `dbt_valid_to`, `dbt_scd_id`,
  `dbt_updated_at`), and offer a `valid_to_current` sentinel option since
  dbt has one and BI tools prefer `9999-12-31` to NULL.
- Statements, inside one transaction (prerequisite 1): hash staging with
  `_row_hash_expr`; close changed (`IS NOT DISTINCT FROM` on the key, the
  `execution.py:142` comment documents why); close hard-deleted if enabled;
  insert new and changed.
- Enforce key uniqueness in staging before the merge, reusing
  `_evaluate_grain`. `UPDATE ... FROM` with a multi-matching source is
  non-deterministic in DuckDB and will silently pick a row.
- Naming: call the materialization `snapshot` in `@config` because that is
  what warehouse people will search for, and rename the internal modules in
  the same change (`snapshot.py` to `fingerprint.py`, `snapshots.py` to
  `rewind.py`) so the word means one thing. `havn snapshot` the CLI verb stays
  for rewind unless it gets renamed too; decide in the PR.

Effort M: execution one day, config and validation half a day, tests one day,
docs and the module renames half a day.

### 6.2 Ephemeral models

**What exists.** Three hardcoded materializations. Transitive hashing
(`_compute_upstream_hash`, `discovery.py:183-199`) already folds upstream
content hashes into consumers, so editing an ephemeral model already
invalidates every consumer. Nothing inlines SQL from one model into another
today.

**Design.**

- `materialized=ephemeral`: `execute_model` skips it; consumers get the
  ephemeral's query inlined as a CTE prepended to their own query (better error
  messages than a subquery, and DuckDB handles CTE chains well). Preserve the
  original alias. Recursive ephemeral chains resolve in topological order.
- `_drop_conflicting` gains a branch: a model switching to ephemeral drops the
  orphan table or view at `schema.name`.
- `@assert` on an ephemeral model: reject at validate time with a clear
  message, or evaluate it against the inlined SQL as a temp view. Reject is
  the honest first version.
- Frontend: a badge in the DAG panel; the table browser correctly does not
  list it.
- The bind pass (3.1) creates a view for it in the shadow, so validation and
  typing work unchanged.

**Risk.** Error messages: a DuckDB error in a three-level inlined query
reports a line the user never wrote. With named CTEs the message at least
names the CTE, which is the ephemeral model's name. That is the same
limitation dbt has.

Effort S to M.

### 6.3 Schema-change policy for incrementals

**What exists.** `execution.py:133-137` is the whole policy: columns in
staging but not in target are `ALTER TABLE ADD COLUMN`ed. The loop never looks
the other way and never compares types. Measured (`scratchpad/exp_schema.py`):

| Change | delete+insert | merge |
|---|---|---|
| Added column | auto-added | auto-added |
| Removed column | silent: new rows NULL, old rows stale | same |
| INTEGER to DOUBLE | silent: 20.5 stored as 21 | same |
| INTEGER to VARCHAR | error, and the target is left empty | error, data intact by accident |

**Design.** `on_schema_change=fail|ignore|append_new_columns|sync_all_columns`
as a `@config` key, hashed into `content_hash` like the other incremental keys.
Compare staging and target on name and type in both directions.

- `append_new_columns`: today's behavior.
- `ignore`: no ALTER, INSERT restricted to the intersection.
- `fail`: any delta raises before any write, with a message naming the column
  and both types.
- `sync_all_columns`: add, drop, and for retyped columns rebuild the column
  via `ALTER ... ALTER COLUMN TYPE` where DuckDB allows it, otherwise fail.

Default: dbt's default is `ignore`, under which a removed column fails the
run and an added column is dropped on the floor. havn's current behavior is
closest to `append_new_columns`. Keep `append_new_columns` as the default for
compatibility, but make retyped and removed columns always fail unless
`ignore` or `sync_all_columns` is explicit, because the current silent
rounding is a bug, not a feature. Like dbt, no option backfills old rows for a
new column, and only top-level columns are tracked; say both in the docs.
Prerequisite 1 first, or `fail` prevents one crash and every other crash
still loses the DELETEd rows.

Effort S plus prerequisite 1.

### 6.4 Microbatch

**What exists.** `{this}` is the only placeholder in `incremental_filter`.
`@watermark` synthesizes a one-sided open-ended filter and always runs one
batch. No `{start}`/`{end}`, no batch loop, no per-batch state, no backfill
command. Ideas-catalog 1.27 (backfill with date range) is scoped to streams,
not transform models, so it would not deliver this even if built.

**Design, when it is time.** Mirror dbt's keys so the docs can say "same
config": `event_time=col, batch_size=hour|day|month|year, begin=2024-01-01,
lookback=1` in `@config`; the engine computes windows, runs the model once
per window with `{start}`/`{end}` substituted, and records each window in
`_havn.batch_state` so a failed backfill resumes. `havn transform gold.events
--event-time-start 2024-01-01 --event-time-end 2024-03-01` for explicit
backfill (dbt's flag names). All timestamps UTC. The model must be written to
process exactly one batch, as in dbt.

**Verdict.** Defer. The placeholder is two lines; the loop, the state and the
CLI are the work, and it inherits prerequisite 1 in the worst way (a 30-batch
backfill failing at batch 17). Note that `dbt-duckdb` has had microbatch
since February 2026, so the gap is real for people coming from v1 on DuckDB.
Build it after 6.3 and after there is a user asking for it.

## 7. Running things

### 7.1 Graph selectors

**What exists.** `havn transform` takes exact `schema.name` or bare `name`
strings (`orchestration.py:79-88`). Jobs already have the whole dbt grammar:
`+x`, `x+`, `+x+`, `schema.*`, script paths, in `resolve_execution_plan`
(`engine/orchestration.py:302-445`) with `_collect_upstream` and
`_collect_downstream`. `state:modified` is essentially `_has_changed`, and
`diff.py:428-440` already composes `state:modified+`. There are three separate
downstream-closure implementations in the tree.

**Design.**

1. Factor the model-selection half of `resolve_execution_plan` out of the
   script-scheduling half into `select_models(selectors, dag, conn)` and use
   it from `havn transform`, `/api/transform` and jobs. Fold the diff.py
   closure into the same function.
2. Replace the `schema.*` prefix hack with `fnmatch` so `gold.fct_*` works.
3. Add `state:modified` and `state:modified+` backed by `_has_changed`.
4. Add `tag:` after adding `@config tags=a,b`. Decision: tags are not hashed
   into `content_hash` (retagging should not rebuild), and `validate_models`
   checks them for typos.
5. `path:transform/gold/` selector is free once selection takes the model
   list.
6. Prerequisite 2 in the same PR, or selectors poison state on every use.

Effort S to M. The grammar and closures exist; this is factoring and a CLI
argument.

### 7.2 Defer

**What exists.** Environments override only `database.path`. `pr.py:709,942`
already attaches a second warehouse and queries it three-part, so the
precedent is in the tree. Verified (`scratchpad/exp4.py`): `ATTACH
'prod.duckdb' AS prod (READ_ONLY)` then `CREATE TABLE silver.x AS SELECT *
FROM prod.bronze.customers` works, cross-database joins work, writes to prod
are refused. Parallel workers share one attach with `ATTACH IF NOT EXISTS`.
The sqlglot rewrite to three-part names is about 20 lines and leaves CTEs,
aliases and already-qualified refs alone.

**The hard constraint.** DuckDB's file lock: attaching prod read-only fails
with `Could not set lock on file` whenever any other process holds prod open
for writing, and with `Unique file handle conflict` if the same process has
it open already. That is exactly when a scheduled prod job is running. Not a
havn limitation, no workaround short of deferring to a copy.

**Design.**

- `environments.dev.defer: prod` in project.yml (new `EnvironmentConfig`
  field).
- At transform start: attach the defer target read-only, once per process,
  `IF NOT EXISTS`. On lock failure: clear error naming the holder PID, and a
  `--defer-snapshot` option that copies prod to a temp file first (DuckDB's
  `COPY FROM DATABASE`, already used in `routes/export.py:61`) for the case
  where prod is busy.
- Rewrite predicate: "not present in the local catalog", not "not in the
  model list", so landing, seeds and sources fall through to prod as
  intended. Rewrite at execution time only; never into `content_hash` or the
  file.
- `masking_rewriter` strips the catalog part when resolving policies
  (verified), so policies still apply to deferred reads.
- DuckLake backend: it already occupies the ATTACH slot; defer is unsupported
  there in the first version.

Effort M. Opt-in, documented with the lock caveat up front.

### 7.3 Parse and lineage time at scale

Measured on the synthetic 1000-model project:

| Pass | Time |
|---|---|
| discover_models (parse plus deps) | 1.6 s |
| build_dag | 3 ms |
| validate_models, built catalog | 2.7 s |
| column lineage, unbuilt catalog | 2.1 s |
| column lineage, built catalog | 39.0 s |
| shadow bind pass (3.1) | 2.6 s |

Prerequisites 3 and 4 take lineage from 39 s to roughly 2 to 3 s. After that,
ideas-catalog 1.8 (content-hash cache) addresses the residual and makes
incremental validation near-instant. The Rust question does not arise at
1000 models; Python is at 1.6 ms per model for parsing and the binder is in
C++ already. Add `benchmarks/bench_parse.py` from `scratchpad/scale_bench.py`
so regressions show up.

Effort S.

## 8. Reuse: packages

**What exists.** Nothing. No config key (`ProjectConfig` has `extra="ignore"`,
so a `packages:` block is silently dropped today), no fetch command,
`engine/git.py` is a UI git wrapper, macros scan one hardcoded non-recursive
directory, `project_dir / "transform"` is hardcoded at 149 sites. The stdlib
macro path (`_discover_stdlib_macros`, with user override and shadow logging)
is the one precedent for namespaced reuse.

**Design, when it is time.** `packages: [{name, git, rev}]` in project.yml;
`havn packages install` cloning into `havn_packages/<name>/` (not `havn
deps`; `engine/deps.py` already means something else); `discover_all_models`
walking `transform/` plus each package's `transform/`; package models forced
into a `<pkg>_`-prefixed schema or a three-part `pkg.schema.name` full name;
macro module names prefixed with the package to avoid the `havn_macros.utils`
collision in `sys.modules`; lint, DAG, jobs and the UI all going through the
same multi-root discovery.

**Verdict.** Defer. It is L, the hardcoded-path count makes a half-finished
state (works in CLI, invisible in the DAG panel) very likely, and prerequisite
8 (duplicate detection) is the part that matters even without packages. The
to-do list's "share macro packs (pip-installable)" is a cheaper first step:
a pip package exposing a `havn.macros` entry point gets macro reuse without
touching model discovery.

## 9. First impression

All verified, all README-only edits, all S.

- **Hero.** `README.md:29` is `git clone ... && npm install && npm run build
  && ...`. The PyPI wheel (`havn 0.2.27`, 30 releases) bundles the built
  frontend via `hatch_build.py` and `publish.yml`; verified end to end in a
  clean venv that `pip install havn && havn init demo && havn serve` serves
  the full SPA. The landing page already uses the honest four-line version.
  Replace the hero with:

  ```
  pip install havn && havn init my-project && cd my-project && havn jobs run full-refresh && havn serve
  ```

  Keep the clone-and-npm line under Contributing.

- **Screenshot.** `README.md:32-38` is commented out and points at
  `.github/assets/screenshot.png`, which does not exist.
  `landing/screenshots/overview.webp` (2560 by 1600) does. GitHub renders
  WebP. Uncomment and point at it. One more shot of the editor with markers
  once 4.1 lands.

- **Supported and not supported.** No limitations page exists; the two
  comparison tables (README and `docs/index.md`) disagree on axes and neither
  is feature-level. Add `docs/limitations.md`: one table per area (SQL
  features, materializations, selectors, environments, editor) with
  supported / partial / not supported / planned, and link it from the README
  comparison. Seed it from this document. Reads as maturity precisely because
  it admits things. dbt does this for DuckDB with an open GitHub epic, and
  for Snowflake and BigQuery with weekly-refreshed function tables; a single
  honest page beats both for a project this size.

- **The comparison row that matters.** Once phases 1 and 2 ship, the README
  comparison should carry two rows dbt cannot match on DuckDB today: "type
  checking and column lineage work offline, no login" and "unit tests run
  with no warehouse". Not before; overclaiming is the one way to lose the
  first-impression fight.

## 10. Sequencing

Phase 0, prerequisites and README (about a week). Items 1 to 9 in section 2,
plus section 9. Nothing user-visible except the README, but every later phase
gets cheaper and the codebase stops lying about incremental safety.

Phase 1, the bind pass (about two weeks). 3.1 shadow bind, persist schemas,
`havn validate` with bind errors, `/api/bind`, editor markers (4.1), hover
types, go-to-definition (4.2), CTE preview (4.3). This is the phase that
closes the perceived quality gap; everything visible in the editor comes from
it.

Phase 2, tests and modeling (about two weeks). Unit tests (5), schema-change
policy (6.3), ephemeral (6.2), selectors (7.1). Four independent items, can be
parallelized.

Phase 3, coverage (about two weeks). Lineage rewrite on the conformance suite
(3.3), types into contracts (3.2), SCD2 (6.1), defer (7.2).

Deferred with reasons: rename (4.4) until 3.3, microbatch (6.4) until a user
asks, packages (8) until the macro-pack version has been tried.

## 11. What I would push back on

- "Static type inference" as a phrase. The binder resolves types and catches
  bind errors; it does not catch conversion errors, and neither does Fusion.
  Say what it does.
- Rust versus Python at 500 to 1000 models. The measured bottleneck is a
  catalog query in a loop. Fix the loop before drawing conclusions about the
  language.
- Packages before there are two projects that want to share a model. Macro
  packs via pip cover the realistic case at a tenth of the cost.
- SCD2 under the name "snapshot" without renaming the three modules that
  already use the word. Do both or the docs will be unreadable.
