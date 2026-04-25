# CLAUDE.md — Agent Instructions for havn

## What is havn?

havn is a self-hosted data platform — a Nordic alternative to Databricks/Snowflake. It uses **DuckDB** for OLAP analytics, **plain SQL** for transforms, and **Python** for ingest/export scripts. All data lives in a single `warehouse.duckdb` file. No data leaves the machine. Data in safe waters.

## Quick Reference

```bash
# Install
pip install -e .              # from source
pip install -e ".[dev]"       # with test deps (pytest, httpx)

# Build frontend
cd frontend && npm install && npm run build

# Run tests
pytest tests/

# Lint SQL
havn lint                       # check
havn lint --fix                 # auto-fix

# Common commands
havn init my-project            # scaffold new project
havn transform                  # build all SQL models
havn transform --force          # force rebuild (ignore cache)
havn query "SELECT 1"           # ad-hoc SQL
havn tables                     # list warehouse objects
havn serve                      # start web UI on :3000
havn serve --auth               # with authentication
havn run ingest/example.py      # run a script
havn jobs run full-refresh      # run full pipeline
havn history                    # show run log
havn env use prod               # switch environment
havn env list                   # show all environments
havn diff gold.orders           # diff a single model
havn diff                       # diff changed models + downstream
havn diff --all                 # diff entire database
havn macros                     # list registered SQL macros
havn backup                     # create verified backup
havn backup --keep 10           # backup with retention
havn backup-list                # list tracked backups
havn backup-verify <path>       # verify backup integrity
havn restore <path>             # restore from backup
```

## Project Structure

```
src/havn/                       # Python package (the platform itself)
  cli/                          # Typer CLI — commands split into modules
  config.py                     # project.yml parsing, scaffold templates
  engine/
    database.py               # DuckDB connection, metadata tables
    transform/                # SQL DAG engine with change detection
    runner.py                 # Python script executor (ingest/export)
    macros.py                 # Python SQL macros (@macro → DuckDB UDFs)
    explain.py                # Query plan parsing (EXPLAIN/EXPLAIN ANALYZE)
    anomaly.py                # Statistical anomaly detection
    diff.py                   # 3-mode diff engine (single/changed/all)
    auth.py                   # Token auth, RBAC (admin/editor/viewer)
    secrets.py                # .env secrets management
    scheduler.py              # Cron scheduler (Huey) + file watcher
    importer.py               # Data import wizard (CSV, Parquet, DB)
    masking_rewriter.py       # Pre-query SQL masking (alias bypass prevention)
    write_queue.py            # Write queue + read pool for DuckDB connections
    query_governor.py         # Query timeout enforcement via DuckDB interrupt()
    backup.py                 # Verified backup/restore with integrity checks
    notebook/                 # .dpnb notebook execution
    docs.py                   # Markdown doc generator
  lint/
    linter.py                 # SQLFluff integration
  server/
    app.py                    # FastAPI backend (150+ endpoints)

frontend/                     # React + Vite SPA
  src/
    App.jsx                   # Main app, tab routing
    api.ts                    # API client (fetch wrapper)
    Editor.jsx                # Monaco code editor
    FileTree.jsx              # Project file browser
    QueryPanel.jsx            # Ad-hoc SQL runner
    TablesPanel.jsx           # Table browser
    DAGPanel.jsx              # Model dependency graph
    ...                       # ~15 components total

tests/                        # pytest test suite
  test_transform.py           # SQL DAG + change detection
  test_runner.py              # Script execution
  test_api.py                 # FastAPI endpoints (uses httpx)
  test_auth.py                # Authentication + RBAC
  test_config.py              # Config parsing
  test_secrets.py             # Secrets management
  test_scheduler.py           # Scheduler
  test_docs.py                # Doc generation
  test_importer.py            # Data import
  test_notebook_runner.py     # Notebook execution
  test_masking_rewriter.py    # Pre-query masking rewriter
  test_backup.py              # Backup/restore
  test_query_governor.py      # Query timeouts
  test_e2e_api.py             # End-to-end API tests
  test_connectors_warehouse.py # Warehouse migration connectors
```

## Architecture

```
User project layout (created by `havn init`):
  ingest/         Python scripts (or .dpnb notebooks)
  transform/
    bronze/       Light cleanup SQL
    silver/       Business logic SQL
    gold/         Consumption-ready SQL
  export/         Python scripts (or .dpnb notebooks)
  notebooks/      .dpnb interactive notebooks
  macros/         Python SQL macros (auto-registered as DuckDB UDFs)
  project.yml     Config: connections, lint, alerts
  .env            Secrets (never committed)
  .havn-env       Active environment (local, not committed)
  warehouse.duckdb   Single-file DuckDB database

Internal DuckDB schemas:
  landing/        Raw data from ingest scripts
  bronze/         Cleaned data
  silver/         Business logic
  gold/           Consumption-ready
  _havn/   Metadata (model_state, run_log, users, tokens)
```

## Key Conventions

### SQL Transform Files

Every `.sql` file in `transform/` follows this convention:

```sql
-- config: materialized=table, schema=silver
-- depends_on: bronze.customers, bronze.orders

SELECT
    c.customer_id,
    c.name,
    COUNT(o.order_id) AS order_count
FROM bronze.customers c
LEFT JOIN bronze.orders o ON c.customer_id = o.customer_id
GROUP BY 1, 2
```

- `-- config:` sets materialization (`view` or `table`) and schema
- `-- depends_on:` declares upstream dependencies (used for DAG ordering)
- Folder name is the default schema (e.g., `transform/bronze/` → `schema=bronze`)
- No Jinja, no templating — just plain SQL (use Python macros for reusable logic)
- Change detection uses SHA256 hash of normalized SQL content

### Python SQL Macros

Python functions in `macros/` are auto-registered as DuckDB UDFs, callable directly in SQL:

```python
# macros/utils.py
from havn import macro

@macro
def mask_email(email: str) -> str:
    """Mask the local part of an email, keep domain."""
    if not email or "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    return f"***@{domain}"
```
```sql
-- transform/silver/customers.sql
SELECT customer_id, mask_email(email) AS email FROM bronze.customers
```

- `@macro` decorator marks functions for registration as scalar UDFs
- Type hints map to DuckDB types (str→VARCHAR, int→INTEGER, float→DOUBLE, etc.)
- `.sql` files in `macros/` with `CREATE MACRO` also supported
- `havn macros` lists all available macros

Use `@table_macro` for functions that return multiple rows (called with `FROM` in SQL):

```python
# macros/utils.py
from havn import table_macro

@table_macro(schema={"id": "INTEGER", "name": "VARCHAR", "active": "BOOLEAN"})
def active_users(status: str) -> list:
    """Return users filtered by status. Called as: SELECT * FROM active_users('all')"""
    rows = [{"id": 1, "name": "Alice", "active": True}, {"id": 2, "name": "Bob", "active": False}]
    if status != "all":
        rows = [r for r in rows if r["active"] == (status == "active")]
    return rows
```
```sql
SELECT * FROM active_users('active')
```

- `schema=` declares output columns and DuckDB types — required (or inferred from first row)
- Each dict in the returned list is one row; keys are column names
- No pyarrow required — uses DuckDB's native SQL TABLE MACRO + json_each internally

### Python Script Convention

Ingest and export scripts are plain Python with a `db` (DuckDB connection) pre-injected:

```python
# A DuckDB connection is available as `db` — just write top-level code
db.execute("CREATE SCHEMA IF NOT EXISTS landing")
db.execute("CREATE OR REPLACE TABLE landing.data AS SELECT * FROM ...")
```

- Scripts run as top-level code with `db` pre-injected (no wrapper function needed)
- Legacy `def run(db)` scripts are still supported (backward compatible)
- `.dpnb` notebooks can also be used as ingest/export pipeline steps
- Scripts prefixed with `_` are skipped
- Ingest failures stop the pipeline (data integrity)
- `stdout`/`stderr` are captured and logged

### project.yml

```yaml
name: my-project
database:
  path: warehouse.duckdb
connections:
  prod_postgres:
    type: postgres
    host: ${DB_HOST}          # env var expansion via .env
    password: ${DB_PASSWORD}
lint:
  dialect: duckdb
```

Pipelines are defined as YAML job files in `orchestration/`:

```yaml
# orchestration/full-refresh.yml
name: full-refresh
targets:
  - gold.*
  - export/earthquake_report.py
resolve: upstream
retry: 1
# schedules:
#   - "0 6 * * *"
```

## Development Workflow

### Running Tests

```bash
pytest tests/                    # all tests
pytest tests/test_transform.py   # specific file
pytest tests/ -v                 # verbose
pytest tests/ -x                 # stop on first failure
```

Tests use temporary DuckDB databases (in-memory or tmp files). No external services needed.

### Making Backend Changes

1. Source is in `src/havn/`
2. CLI commands are in `cli/` — each `@app.command()` function maps to a `havn <command>`
3. Engine logic is in `engine/` — transform/ is the core SQL DAG engine
4. API endpoints are in `server/app.py` — FastAPI with Pydantic models
5. Run `pytest tests/` after changes

### Making Frontend Changes

1. Source is in `frontend/src/`
2. React 19 + Vite, no TypeScript
3. Monaco editor for code editing
4. API client in `api.ts` (thin fetch wrapper)
5. Dev server: `cd frontend && npm run dev` (port 5173, proxies /api to 3000)
6. Build: `cd frontend && npm run build`

### Adding a New CLI Command

1. Add `@app.command()` function in `src/havn/cli/` (new file or existing module)
2. Import engine modules lazily (inside the function body)
3. Use `_resolve_project()` for project dir resolution
4. Use `rich` Console for output formatting
5. Add corresponding API endpoint in `server/app.py` if needed
6. Add tests in `tests/`

### Adding a New SQL Model

Create a `.sql` file in the appropriate `transform/` subdirectory:

```sql
-- config: materialized=table, schema=gold
-- depends_on: silver.dim_customer

SELECT * FROM silver.dim_customer WHERE active = true
```

Run `havn transform` to build it.

## MemPalace (Project Memory)

This repo is indexed by MemPalace — 6,823 drawers across 7 rooms (`src`, `frontend`, `testing`, `documentation`, `landing`, `general`, `design`), wing `havn`. Registered as an MCP server (`mempalace`); tools auto-load at session start. Palace data lives in `~/.mempalace/` (outside the repo). The repo-local `mempalace.yaml` + `entities.json` are gitignored.

### Use it actively

**Before you Grep, Glob, or Read to figure out "where does X live?" or "how does Y work?" — search the palace first.** It is hybrid semantic + BM25 + cross-reference, built for exactly these queries and orders of magnitude cheaper than loading many files.

1. **Session start:** call `mempalace_status` once to see the palace overview, then `mempalace_wake-up` when you need the ~800-token L0/L1 grounding bundle.
2. **"Where is the X implementation?"** → `mempalace_search "X"` (keep the query short and literal — the retriever does the expansion). Add `wing: "havn"`, and `room: "src"` / `"frontend"` / `"testing"` when you know the area. The returned drawers contain the code verbatim; you usually don't need to Read the file afterwards.
3. **"How does X interact with Y?"** → search for the concept, then follow `mempalace_traverse` / `mempalace_follow_tunnels` on the returned drawers to walk the cross-reference graph.
4. **Design decisions, rationale, past incidents** → use `mempalace_kg_query` for structured facts; `mempalace_search` for prose (READMEs, `docs/`, `docs/internal/`).
5. **Recording what you learn:** when you discover something non-obvious during a task (a gotcha, a non-intuitive dependency, a fix rationale), call `mempalace_kg_add` so the next session doesn't rediscover it. When a fact becomes wrong, `mempalace_kg_invalidate` the old one.

### After non-trivial changes

Re-mine so the palace reflects reality. It's incremental (SHA256-keyed, only re-chunks changed files):
```bash
mempalace mine .
```
Do this after large refactors, new features, or whenever `git diff --stat` shows many files touched. Small edits can wait.

### CLI fallback (MCP unavailable)

```bash
mempalace search "query"                     # semantic + BM25
mempalace search "query" --wing havn         # scope to this repo
mempalace wake-up                            # ~800-token grounding
mempalace status                             # drawer/room counts
mempalace mine .                             # re-index changed files
```

### Guardrails

- Search results are a starting point, not the final truth. **Verify against the current code** before recommending a function, flag, or file path — the palace can lag real-time edits by one mine cycle.
- Keep queries short and use the repo's own vocabulary (e.g. `"masking rewriter"`, not `"SQL query rewriting for PII redaction"`). Over-paraphrased queries miss.
- If the first query misses, try: (a) a shorter variant, (b) adding `room` scope, (c) switching to an exact symbol/filename string. Don't try five variants of the same paraphrase.

## Code Style

- Python 3.10+, type hints used throughout
- `from __future__ import annotations` in all modules
- Imports: stdlib → third-party → local (standard Python convention)
- Rich library for terminal formatting
- Lazy imports in CLI commands (faster startup)
- SQLFluff config in `pyproject.toml` — DuckDB dialect, keywords UPPER, identifiers lower

## Testing Patterns

- Tests use `tmp_path` fixture for temp databases
- API tests use `httpx.AsyncClient` with FastAPI's `TestClient`
- No mocking of DuckDB — tests use real (temporary) databases
- Test files mirror source structure: `test_transform.py` tests `engine/transform.py`

## Common Tasks for Agents

### "Add a new ingest source"
1. Create `ingest/source_name.py` with `run(db)` function
2. Load data into `landing.table_name`
3. Add SQL transforms in `transform/bronze/` → `silver/` → `gold/`
4. Update `project.yml` streams if needed
5. Test with `havn run ingest/source_name.py` then `havn transform`

### "Add a new API endpoint"
1. Add Pydantic request/response models in `server/app.py`
2. Add `@app.post("/api/...")` or `@app.get("/api/...")` handler
3. Use `_require_permission(request, "read"|"write"|"execute")` for auth
4. Always use `connect()`/`conn.close()` pattern with try/finally
5. Add test in `tests/test_api.py`

### "Fix a SQL model"
1. Edit the `.sql` file in `transform/`
2. Run `havn transform` — change detection will rebuild only changed models
3. Use `havn transform --force` to rebuild everything
4. Validate with `havn query "SELECT * FROM schema.table LIMIT 10"`

### "Debug a failed pipeline"
1. Check `havn history` for recent failures
2. Look at error messages in the run log
3. Run individual steps: `havn run ingest/script.py`, then `havn transform`
4. Use `havn query` to inspect data at each layer
