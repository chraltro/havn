# CLAUDE.md — havn Development

This file is for **developing havn itself**. The root `CLAUDE.md` is shipped to end users — don't modify it here.
Detailed implementation docs are in `.claude/rules/` and auto-load when you touch relevant files.

## Build & Test

```bash
pip install -e ".[dev]"                          # install with test deps
cd frontend && npm install && npm run build      # build frontend
pytest tests/                                    # run all tests (40 files)
pytest tests/test_transform.py -x               # single file, stop on fail
cd frontend && npm run dev                       # frontend dev server (:5173, proxies to :3000)
havn serve                                       # backend (:3000)
```

## What is havn?

A self-hosted data platform — Nordic alternative to Databricks/Snowflake. Everything runs on one machine, data lives in a single `warehouse.duckdb` file. Transforms are plain SQL, ingest/export are Python scripts. No Jinja, no cloud, no YAML spaghetti.

**Data flow:** `landing/` (raw) → `bronze/` (cleaned) → `silver/` (modeled) → `gold/` (ready) — all DuckDB schemas in one file.

## Complete Feature Map

### SQL Transform Engine (`src/havn/engine/transform/`)
- **DAG execution** — topological sort from `-- depends_on:` comments, parallel execution by tier
- **Change detection** — SHA256 hash of normalized SQL + transitive upstream hashes, only rebuilds what changed
- **Incremental models** — four strategies: delete+insert, append, merge (true upsert), partition_by
- **Schema evolution** — auto-adds new columns to incremental targets
- **Config via comments** — `-- config: materialized=table, schema=gold, unique_key=id, incremental_strategy=merge`
- **Auto-dependency extraction** — if `-- depends_on:` missing, uses sqlglot AST to find FROM/JOIN refs

### Data Quality (`src/havn/engine/transform/quality.py`, `contracts.py`)
- **Inline assertions** — `-- assert: row_count > 0`, `unique(col)`, `no_nulls(col)`, `accepted_values(col, [...])`
- **YAML contracts** — `contracts/*.yml` with row_count min/max, freshness warn_after, column-level rules
- **Auto-profiling** — row_count, column_count, null_percentages, distinct_counts per model
- **Freshness checks** — detect stale models by last run time

### Schema Sentinel (`src/havn/engine/sentinel.py`)
- Detects upstream schema changes (added/removed/renamed columns)
- Impact analysis — which downstream models are affected
- Auto-fix suggestions — apply column renames to dependent SQL

### Pipeline Rewind (`src/havn/engine/snapshots.py`)
- Parquet snapshots per model per run (ZSTD compressed)
- Deduplication — identical snapshots share files (checksum-based)
- Restore with cascade — restore a snapshot and re-run all downstream models
- Garbage collection — retention-based + storage cap
- Time travel UI — slider to browse warehouse state at any past run

### Data Masking (`src/havn/engine/masking.py`)
- Column-level PII masking with 14 methods:
  - **General**: hash (SHA256), redact, null, partial, truncate
  - **PII**: email (domain-only), phone (last N digits), first_initial (name initials), ip_address (mask host octets)
  - **Financial**: credit_card (PCI-DSS compliant, last 4 digits)
  - **Analytics-safe**: range (numeric bucketing), noise (deterministic +/- %), date_shift (consistent random offset), consistent_hash (JOIN-safe pseudonyms)
- Policy-based — per schema/table/column with conditional rules
- Role exemptions — admins bypass masking by default
- Post-query application — masking happens after query, not in SQL
- Method catalog API — `GET /api/masking/methods` returns all methods with config schemas and examples

### Connectors (`src/havn/connectors/`)
- **10 built-in**: Postgres, MySQL, REST API, Google Sheets, CSV, S3/GCS, Stripe, HubSpot, Shopify, Webhook
- **BaseConnector interface**: `test_connection()`, `discover()`, `generate_script()`
- **Auto-generation**: `havn connect <type>` creates ingest script + updates project.yml + .env
- **CDC**: high watermark tracking, file modification monitoring, full refresh fallback

### Script Execution (`src/havn/engine/runner.py`)
- Python scripts get `db` (DuckDB connection) pre-injected — just write top-level code
- Legacy `def run(db)` pattern still supported
- `.dpnb` notebooks also supported as pipeline steps
- Configurable timeout (default 300s), stdout/stderr captured
- Scripts prefixed with `_` are skipped
- **Ingest failures stop the pipeline** (data integrity guarantee)

### Interactive Notebooks (`src/havn/engine/notebook/`)
- `.dpnb` format — code (Python), SQL, Markdown, Ingest cells
- Shared namespace across cells (variables persist)
- Wire notebooks into pipeline streams as ingest/export steps
- Convert between SQL models and notebooks

### Orchestration (`src/havn/engine/scheduler.py`)
- Cron scheduling via Huey (SQLite-backed queue, survives restarts)
- File watcher — auto-rebuild on .sql/.py changes (debounced 2s)
- Multi-step streams defined in `project.yml` (ingest → transform → export)
- Retry with configurable count/delay

### Alerting (`src/havn/engine/alerts.py`)
- Multi-channel: Slack webhooks, generic webhooks, Python logging
- Alert types: pipeline_success, pipeline_failure, assertion_failed, stale_model, anomaly
- All alerts logged to `_dp_internal.alert_log`

### Circuit Breaker (`src/havn/engine/circuit_breaker.py`)
- Prevents cascading failures from repeatedly executing failing scripts
- States: CLOSED → OPEN (after N failures) → HALF_OPEN (probe after timeout)
- Exponential backoff with jitter

### Auth & RBAC (`src/havn/engine/auth.py`)
- Token-based auth with PBKDF2-HMAC-SHA256 password hashing
- Three roles: admin (full), editor (read/write/execute), viewer (read-only)
- 30-day token expiry, rate-limited login (5 attempts/60s/IP)

### Versioning (`src/havn/engine/versioning.py`)
- Warehouse snapshots — create, diff, restore named versions
- Table timelines — version history per table
- Auto-snapshot before restore operations

### Diff Engine (`src/havn/engine/diff.py`)
- Preview what SQL transforms would change before running
- Row-level diff: added, removed, modified counts
- Compare against git branches

### CI/CD Integration (`src/havn/engine/ci.py`)
- `havn ci generate` creates GitHub Actions workflow
- Data diff PR comments — show what would change in warehouse

### Live Collaboration (`src/havn/engine/collaboration.py`)
- WebSocket shared query sessions
- Real-time SQL editor sync with cursor tracking
- Auto-cleanup stale sessions (24h)

### AI Agent Integration (`src/havn/engine/agents/`)
- Built-in agent sidebar in web UI
- Adapters: Claude Code, Codex, Gemini CLI
- Agents can read/write files, execute transforms via WebSocket
- Plain SQL = LLMs write correct transforms (no Jinja to hallucinate)

### Audit Logging (`src/havn/engine/audit.py`)
- Tracks: query, transform, ingest, export, file_edit, file_delete, login, config_change
- Filterable by user, action, resource

### Git Operations (`src/havn/engine/git.py`, `src/havn/server/routes/git.py`)
- Full git workflow from the web UI via GitPanel in the Develop section
- Read operations: status, log, diff (file-level, staged/unstaged), branches (local + remote), stash list, remote URL
- Write operations: stage, unstage, commit, pull, push, create/checkout/delete branch, stash/pop, discard changes
- 17 API endpoints (6 read, 11 write) with RBAC (read permission for reads, write for mutations)
- Engine functions shell out to git CLI (no Python git libraries), with input validation against shell injection
- Graceful fallback when not a git repo (returns empty/false instead of errors)

### SQL Analysis (`src/havn/engine/sql_analysis.py`)
- sqlglot AST parsing for column lineage, table reference extraction
- Validation: parse check, dependency existence, column resolution, ambiguity detection
- Impact analysis: BFS downstream traversal with column tracing

## Web UI (`frontend/src/`)

React 19 + Vite SPA with Monaco editor. 5 sections, 14+ tabs:
- **Overview** — dashboard with stats, pipeline health, git status, quick actions
- **Develop** — Monaco editor, file tree, model notebook view, new model dialog, GitPanel (commit, pull, push, branch, stash, diff viewer)
- **Explore** — SQL runner with autocomplete, table browser with stats, DAG visualization (canvas-based with rewind timeline), interactive notebooks, chart builder
- **Observe** — pipeline history, data quality (contracts/assertions/freshness), schema sentinel, masking policies, audit log
- **Configure** — settings (themes, auth, scheduler), data sources (connector setup), wiki

8 color themes, 7 font pairings (composable). SSE for pipeline streaming. WebSocket for collaboration + agent sidebar. Output panel shows live running status with in-flight task names. Session storage cleared on server restart to reset pipeline output and agent messages.

## Server/API (`src/havn/server/`)

FastAPI with 150+ endpoints across 21 route modules. Shared DuckDB connection singleton with per-thread cursors (Windows file locking constraint). **Decoupled pipeline**: runs in background worker thread; `POST /api/stream/{stream_name}/start` spawns async task, `GET /api/stream/events` SSE endpoint reads from event buffer. Legacy blocking endpoints still supported. WebSocket for collaboration and agent sidebar.

## CLI (`src/havn/cli/`)

50+ Typer commands. Key ones: `init`, `transform`, `stream`, `query`, `tables`, `serve`, `run`, `connect`, `diff`, `lint`, `snapshot`, `rewind`, `sentinel`, `version`, `ci generate`, `context`.

## Internal Metadata (`_dp_internal` schema)

All metadata lives in the warehouse itself:
- `model_state` — change detection hashes, materialization info, row counts, build times
- `run_log` — pipeline execution history (run_id, type, status, duration, error, output)
- `model_profiles` — auto-computed column statistics
- `assertion_results` — data quality check results
- `masking_policies` — column masking rules
- `audit_log` — user action trail
- `slow_queries` — queries exceeding 5s threshold
- `alert_log` — notification delivery tracking
- `circuit_state` — circuit breaker persistence
- `users`, `tokens` — auth (when enabled)
- `contract_results`, `cdc_state`, `versions` — quality, CDC, versioning state

## Code Conventions

- Python 3.10+, `from __future__ import annotations` in all modules
- Lazy imports in CLI commands (faster startup)
- `rich` for terminal output formatting
- FastAPI + Pydantic for all API endpoints
- No mocking of DuckDB in tests — use real temp databases (`tmp_path` fixture)
- SQL keywords UPPER, identifiers lower (SQLFluff enforced)
- Connection pattern: `connect()` / `conn.close()` with try/finally
- Frontend: React 19 + Vite, JSX only (no TypeScript except api.ts), no component library

## Architecture Decisions

- Single `warehouse.duckdb` file for self-hosted — this is the product's core selling point
- Cloud/hosted version will use DuckLake (Parquet + Postgres catalog) — see `docs/internal/to-do.md`
- `database.py` will need a backend abstraction to support both — not yet started
- All engine code (transforms, DAG, quality, connectors) must stay backend-agnostic

## Gotchas

- DuckDB on Windows uses exclusive file locks — parallel connections from separate processes will fail
- The SSE streaming pipeline uses thread-local cursors from a shared connection singleton
- Frontend is React 19 + Vite, no TypeScript (JSX only)
- `_dp_internal` tables bootstrap lazily on first `ensure_meta_table()` call
- Config comments (`-- config:`, `-- depends_on:`) are regex-parsed, not AST-parsed
- Incremental filter syntax uses `{this}` placeholder for target table FQN

## Project Structure

```
src/havn/
  cli/              12 command modules (Typer)
  connectors/       10 built-in connectors + BaseConnector
  engine/
    transform/      DAG engine: discovery, execution, orchestration, quality, analysis
    notebook/       .dpnb cell execution (code, SQL, ingest, markdown)
    agents/         LLM adapters (Claude, Codex, Gemini) + registry
    database.py     DuckDB connection + metadata bootstrap
    auth.py         Token auth + RBAC
    runner.py       Script execution with timeout + circuit breaker
    scheduler.py    Cron + file watcher
    connector.py    Connector framework
    importer.py     CSV/Parquet/JSON/XLSX + DB import
    masking.py      Column-level data masking
    snapshots.py    Pipeline Rewind (Parquet snapshots)
    alerts.py       Slack/webhook notifications
    audit.py        Action audit logging
    circuit_breaker.py  Failure prevention
    sql_analysis.py sqlglot AST parsing
    cdc.py          Change data capture
    collaboration.py WebSocket shared sessions
    contracts.py    YAML data quality contracts
    diff.py         Transform diff engine
    ci.py           GitHub Actions generation
    git.py          Git integration
    sentinel.py     Schema change detection
    versioning.py   Warehouse versioning
    docs.py         Markdown/JSON doc generation
    secrets.py      .env management
  server/
    app.py          FastAPI setup + middleware
    deps.py         Shared connection, auth helpers, caching
    routes/         21 route modules (150+ endpoints)
  config.py         project.yml parsing (Pydantic models)
  lint/linter.py    SQLFluff integration

frontend/src/       React 19 + Vite SPA
  App.jsx           Main app, 5-section navigation
  api.ts            API client (40+ functions, auth token injection)
  Editor.jsx        Monaco with SQL completion/hover
  FileTree.jsx      Recursive file browser with drag-drop
  QueryPanel.jsx    SQL runner with autocomplete + history
  DAGPanel.jsx      Canvas DAG with rewind timeline
  TablesPanel.jsx   Table browser with sorting + stats
  GitPanel.jsx      Git operations (commit, branch, stash, diff)
  + 30 more components, 5 context providers, 8 themes

tests/              40 test files, real DuckDB (no mocks)
```

## Internal Docs

Working files that don't ship are in `docs/internal/` (gitignored):
- `to-do.md` — roadmap, tasks, cloud architecture planning
- `presentations/` — pitch decks for different audiences (DE, VC, community)
