# havn — To-Do

## Cloud / Hosted Version

### Database Abstraction Layer (`database.py`)
- [ ] Design a backend interface that supports both direct DuckDB (self-hosted) and DuckLake (cloud)
- [ ] Sketch out the abstraction: `connect()`, `execute()`, `cursor()` should work the same regardless of backend
- [ ] DuckLake backend: catalog in Postgres (schema-per-tenant or row-level), data as Parquet in S3/R2
- [ ] Keep all engine code (transforms, DAG, quality, connectors) backend-agnostic — they just call the abstraction
- [ ] "Download as .duckdb" export feature — reads from DuckLake, writes single file for local use
- [ ] Zero lock-in pitch: user can leave cloud and run self-hosted anytime with one export

### Multi-User / Concurrency
- [ ] DuckLake enables real concurrent read/write from multiple DuckDB workers
- [ ] Watch DuckLake maturity (v0.3 now, 1.0 expected early 2026) — concurrent write issues (#233, #243) still being fixed
- [ ] Team/org model: upgrade existing auth.py with org membership, roles, SSO (SAML/OIDC)

### Infrastructure
- [ ] Container-per-tenant on Fly.io or K8s for initial hosted version
- [ ] Control plane service: tenant provisioning, billing (Stripe), DNS routing
- [ ] Gateway: route `{tenant}.havn.dev` to correct container/worker
- [ ] Automated backups: Parquet files in S3 = naturally durable, add scheduled catalog snapshots
- [ ] Consider Fly.io Machines for scale-to-zero (ETL workloads are bursty)

## Presentations
- [x] Create presentations folder with original deck
- [x] Data Engineering deck (9 slides — architecture, transform engine, tradeoffs)
- [x] VC / Investors deck (8 slides — market, business model, GTM, roadmap)
- [x] Open-Source Community deck (8 slides — getting started, features, contributing)
- [x] All exported as PDF
- [ ] Review decks in PowerPoint and refine any text/layout issues
- [ ] Add web UI screenshots to decks
- [ ] Add real traction metrics when available

## Python SQL Macros (UDFs)

Replace Jinja with Python functions callable directly in SQL. "Why template SQL when you have Python right there?"

### Approach: DuckDB scalar UDFs
- [x] `macros/` directory convention: Python files with decorated functions
- [x] Auto-discover and register UDFs at connection time (`database.py` or `connect()`)
- [x] Decorator API: `@macro` decorator, importable as `from havn import macro`
- [x] DuckDB `create_function()` for scalar UDFs (str, int, float, bool, date, datetime)
- [x] SQL macros via `CREATE MACRO` in `.sql` files in `macros/`
- [x] CLI: `havn macros` to list registered UDFs with signatures
- [x] API: `GET /api/macros` endpoint for editor autocomplete
- [x] Scaffold: `havn init` creates `macros/` with example `utils.py`
- [ ] Support table-returning UDFs via `duckdb.create_function(..., type="table")` for reusable CTEs
- [ ] Hot-reload: file watcher re-registers UDFs when macros/ changes
- [ ] Web UI: show available macros in editor autocomplete + hover docs
- [ ] Aggregate UDFs for custom rollups
- [ ] Share macro packs (pip-installable macro libraries?)

## Shipped (2026-03-20)

### 3-Mode Diff System
- [x] Single model diff (`havn diff gold.orders`)
- [x] Changed + downstream diff (`havn diff --changed`, smart default)
- [x] Full database diff (`havn diff --all`, explicit opt-in)
- [x] Frontend: mode selector, model autocomplete, skipped model toggle

### SSE Stream Reconnection
- [x] Exponential backoff (1s→16s) with jitter, max 10 retries
- [x] Heartbeat timeout (30s), resume from last event index
- [x] "Reconnecting..." / "Connection lost" banners in OutputPanel

### Assertion Debugging
- [x] Diagnostic details on failure (duplicated values, null samples, unexpected values)
- [x] `GET /api/quality/assertion-debug/{model}` endpoint
- [x] "Re-run with diagnostics" button in QualityPanel

### `havn env` Command
- [x] `havn env list/use/show/reset` with `.havn-env` file
- [x] Config loading respects `.havn-env`, `--env` flag overrides

### Query Plan Visualization
- [x] `explain.py` module with DuckDB JSON EXPLAIN parsing
- [x] `POST /api/query/explain-analyze` endpoint
- [x] ExplainPanel with collapsible tree, color-coded operators, timing bars
- [x] "Explain Analyze" button + "Plan" tab in QueryPanel

### File Edit Conflict Detection
- [x] SHA256 file hash on read (ETag header), 409 Conflict on stale save
- [x] Conflict dialog: Cancel / Load latest / Overwrite

## Agent Onboarding
- [x] Improve CLAUDE.md and project documentation for fast agent onboarding
- [x] Root CLAUDE.md with full project structure, conventions, common tasks
- [x] .claude/CLAUDE.md with dev-specific instructions, feature map, architecture decisions
- [x] .claude/rules/ with auto-loading context per module (engine.md, etc.)
