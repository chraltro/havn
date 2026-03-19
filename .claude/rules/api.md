---
paths:
  - "src/havn/server/**"
---

# havn Server API Documentation

## Architecture Overview

- **Framework**: FastAPI with Pydantic validation
- **Database**: DuckDB — single shared connection singleton (with per-thread cursors for thread safety)
- **Authentication**: Bearer token via `Authorization` header; RBAC roles: admin/editor/viewer
- **Frontend**: React 19 + Vite, API client in `frontend/src/api.ts` (thin fetch wrapper)
- **Streaming**: SSE for pipeline output, WebSocket for collaboration & agent sidebar

### Global State (app.py)

```python
PROJECT_DIR: Path          # Set by CLI before starting uvicorn
AUTH_ENABLED: bool         # Set by --auth flag
ACTIVE_ENV: str | None     # Set by --env flag
```

### Middleware

- CORS: allows localhost:3000, localhost:5173, 127.0.0.1:3000/5173
- Frontend SPA catch-all: serves index.html for all non-API routes (except /docs, /redoc, /openapi.json)

---

## Database Connection Pattern

**deps.py**: Shared connection management

```python
_get_shared_conn()              # Thread-safe singleton getter (locks)
get_db()                        # FastAPI Depends: yields cursor from shared conn
get_db_readonly()               # Read-only cursor
get_db_readonly_optional()      # Yields None if DB doesn't exist
DbConn                          # Type alias: Annotated[...]
DbConnReadOnly                  # Type alias
DbConnReadOnlyOptional          # Type alias
```

- Windows file locking: Only one connection per warehouse.duckdb file allowed; shared connection with cursor-per-thread pattern (per DuckDB docs).
- Config caching: Loaded once, re-checked on mtime change; invalidated on environment switch.
- Model discovery caching: File-mtime-based cache (version 2).

---

## Authentication & Authorization

**deps.py**

```python
_get_user(request: Request) -> dict | None
    # Extract user from Bearer token; returns None if auth disabled
    # Returns: {"username", "role", "display_name"} or None

_require_user(request: Request) -> dict
    # Requires authentication; raises 401 if not authenticated

_require_permission(request: Request, permission: str) -> dict
    # Validates RBAC role has permission; raises 403 if denied
    # Permissions: "read", "write", "execute", "manage_users", "manage_secrets"
```

**auth.py endpoints:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| POST | /api/auth/login | `login()` | None (rate-limited) | Authenticate & get token |
| GET | /api/auth/me | `get_current_user()` | Required | Get current user |
| GET | /api/auth/status | `get_auth_status()` | None | Check if auth enabled, needs setup |
| POST | /api/auth/setup | `initial_setup()` | None | Create first admin user |
| GET | /api/users | `list_users()` | admin | List all users |
| POST | /api/users | `create_user_endpoint()` | admin | Create user |
| PUT | /api/users/{username} | `update_user_endpoint()` | admin | Update user role/password |
| DELETE | /api/users/{username} | `delete_user_endpoint()` | admin | Delete user |
| GET | /api/secrets | `list_secrets()` | admin | List secrets (masked) |
| POST | /api/secrets | `set_secret()` | admin | Set/update secret |
| DELETE | /api/secrets/{key} | `delete_secret()` | admin | Delete secret |

Rate limiting: 5 attempts per 60s per client IP on login.

---

## File Management

**files.py endpoints:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| GET | /api/files | `list_files()` | read | Tree of project files |
| GET | /api/files/{file_path:path} | `read_file()` | read | Read file content + language |
| PUT | /api/files/{file_path:path} | `save_file()` | write | Save/create file |
| POST | /api/files/{file_path:path}/move | `move_file()` | write | Move/rename file |
| DELETE | /api/files/{file_path:path} | `delete_file()` | write | Delete file (optionally drop DB object) |

Supported file types: .sql, .py, .yml, .yaml, .dpnb, .csv, .md, .sqlfluff

---

## SQL Query Execution

**query.py endpoints:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| POST | /api/query | `run_query()` | read | Execute SQL with 30s timeout & masking |
| POST | /api/query/explain | `explain_query()` | read | EXPLAIN PLAN |
| POST | /api/query/profile | `profile_query()` | read | EXPLAIN ANALYZE |
| GET | /api/metrics/slow-queries | `get_slow_queries()` | read | Slow query log |
| GET | /api/tables | `list_tables()` | read | List schemas/tables/views |
| GET | /api/tables/{schema}/{table} | `describe_table()` | read | Column info |
| GET | /api/tables/{schema}/{table}/sample | `sample_table()` | read | Sample rows with pagination |
| GET | /api/tables/{schema}/{table}/profile | `profile_table()` | read | Column statistics |
| GET | /api/autocomplete | `get_autocomplete()` | read | Tables/columns for editor autocomplete |

**Masking interception** in run_query():
- `SHOW MASKING POLICIES` → returns masking table
- `CREATE MASKING POLICY ON schema.table.column METHOD method [EXEMPT roles]`
- `DROP MASKING POLICY <id>`

---

## Models & Transforms

**models.py endpoints:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| GET | /api/models | `list_models()` | read | All SQL models with schema/materialized/depends_on |
| POST | /api/transform | `run_transform_endpoint()` | execute | Build SQL DAG (with targets & force flags) |
| POST | /api/diff | `run_diff_endpoint()` | read | Compare SQL output vs materialized tables |
| GET | /api/lineage/{model_name} | `get_lineage()` | read | Column-level lineage (AST via sqlglot) |
| GET | /api/lineage | `get_all_lineage()` | read | All models' lineage |
| GET | /api/impact/{model_name} | `get_impact()` | read | Downstream impact analysis |
| GET | /api/docs/markdown | `get_docs_markdown()` | read | Auto-generated markdown docs |
| GET | /api/docs/structured | `get_docs_structured()` | read | Structured docs for two-pane UI |
| GET | /api/models/{model_name}/notebook-view | `get_model_notebook_view()` | read | Notebook-style model view + sample data |
| POST | /api/models/create | `create_model_endpoint()` | write | Create new SQL model file |
| POST | /api/validate | `run_validate()` | read | Pre-build validation (deps, incremental, conflicts) |
| POST | /api/check | `run_check()` | read | Full validation + assertions + contracts |

---

## DAG & Lineage

**dag.py endpoints:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| GET | /api/dag | `get_dag()` | read | Model DAG nodes/edges (includes ingest & import scripts) |
| GET | /api/dag/full | `get_full_dag()` | read | Full DAG with seeds, sources, exposures |

---

## Pipeline Execution & Streaming

**pipeline.py endpoints:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| POST | /api/run | `run_script_endpoint()` | execute | Run single ingest/export script |
| POST | /api/stream/{stream_name}/start | `start_stream()` | execute | Start stream in background, returns immediately |
| GET | /api/stream/events | `get_stream_events_sse()` | execute | **SSE**: Read from event buffer (decoupled from execution) |
| GET | /api/stream/active | `get_stream_active_status()` | read | Get running state, total events, completion status |
| GET | /api/stream/{stream_name}/events | `run_stream_sse()` | execute | **(Legacy) SSE**: Execute stream with real-time progress |
| POST | /api/stream/{stream_name} | `run_stream_endpoint()` | execute | **(Legacy) Blocking**: Run full stream, returns summary |
| POST | /api/stream/cancel | `cancel_stream()` | execute | Cancel running stream |
| GET | /api/streams | `list_streams()` | read | List configured streams |
| GET | /api/history | `get_history()` | read | Run log (ordered DESC by started_at) |
| GET | /api/scheduler | `get_scheduler_status()` | read | Scheduler & scheduled streams |

**Architecture**: Pipeline now runs in background worker thread. `start_stream()` spawns async task and returns immediately. Events are buffered and streamed via SSE endpoint. Old SSE endpoint still works for backward compatibility.

**SSE Stream Events** (from `get_stream_events_sse()`):
- `event: start` → `{stream, steps, total}`
- `event: model_start` → `{name, action, num, materialized?}`
- `event: model_end` → `{name, action, status, duration_ms, row_count, rows_affected, error?, num}`
- `event: validation` → `{model, severity, message}`
- `event: complete` → `{stream, status, duration_seconds}`
- `: keepalive` (heartbeat every 2s of queue wait)

**Stream DAG**: ingest nodes (roots) → transform nodes (depend on landing.* tables or other models) → export nodes (depend on all transforms). Uses GraphLib TopologicalSorter with ThreadPoolExecutor (max_workers from config.database.threads).

---

## Notebooks

**notebooks.py endpoints:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| GET | /api/notebooks | `list_notebooks()` | read | All .dpnb notebooks in project |
| GET | /api/notebooks/open/{name:path} | `get_notebook()` | read | Get notebook JSON + cells |
| POST | /api/notebooks/save/{name:path} | `save_notebook_endpoint()` | write | Save notebook |
| POST | /api/notebooks/create/{name} | `create_notebook_endpoint()` | write | Create new notebook |
| POST | /api/notebooks/run/{name:path} | `run_notebook()` | execute | Run entire notebook |
| POST | /api/notebooks/run-cell/{name:path} | `run_cell()` | execute | Run single cell (code/sql/ingest) |

Notebook namespace management: LRU cache of 50 runtime namespaces (stored in `_notebook_namespaces`).

---

## Catalog Management

**catalog.py endpoints:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| GET | /api/seeds | `list_seeds_endpoint()` | read | Discover seed CSV files |
| POST | /api/seeds | `run_seeds_endpoint()` | execute | Load all seeds into target schema |
| GET | /api/sources | `list_sources_endpoint()` | read | Declared sources from sources.yml |
| GET | /api/sources/freshness | `check_sources_freshness()` | read | Source freshness check vs SLA |
| GET | /api/exposures | `list_exposures_endpoint()` | read | Declared exposures |
| GET | /api/config/database | `get_database_config()` | read | memory_limit & threads settings |
| PUT | /api/config/database | `update_database_config()` | write | Update memory_limit & threads in project.yml |
| GET | /api/environment | `get_environment()` | read | Active & available environments |
| PUT | /api/environment/{env_name} | `switch_environment()` | write | Switch active environment |
| GET | /api/overview | `get_overview()` | read | Project health: recent runs, schemas, table count, row count |
| GET | /api/versions | `list_versions_endpoint()` | read | Warehouse versions |
| POST | /api/versions | `create_version_endpoint()` | write | Create version snapshot |
| GET | /api/versions/{version_id} | `get_version_endpoint()` | read | Version details |
| GET | /api/versions/{from_version}/diff | `diff_versions_endpoint()` | read | Diff two versions |
| POST | /api/versions/{version_id}/restore | `restore_version_endpoint()` | write | Restore from version |
| GET | /api/versions/timeline/{table_name} | `get_table_timeline()` | read | Table version history timeline |

---

## Data Import & Connectors

**connectors.py endpoints:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| POST | /api/import/preview-file | `preview_file_endpoint()` | execute | Preview file before import |
| POST | /api/import/file | `import_file_endpoint()` | execute | Import file into warehouse |
| POST | /api/import/test-connection | `test_connection_endpoint()` | execute | Test DB connection |
| POST | /api/import/from-connection | `import_from_connection_endpoint()` | execute | Import from remote DB |
| POST | /api/upload | `upload_file()` | execute | File upload handler (FormData) |
| GET | /api/connectors/available | `list_available_connectors()` | read | Available connector types |
| GET | /api/connectors | `list_configured_connectors()` | read | Configured connectors |
| POST | /api/connectors/test | `test_connector_endpoint()` | execute | Test connector config |
| POST | /api/connectors/discover | `discover_connector_endpoint()` | execute | Discover tables from connector |
| POST | /api/connectors/setup | `setup_connector_endpoint()` | execute | Setup & register connector |
| POST | /api/connectors/regenerate/{connection_name} | `regenerate_connector()` | execute | Regenerate connector |
| POST | /api/connectors/sync/{connection_name} | `sync_connector()` | execute | Trigger connector sync |
| DELETE | /api/connectors/{connection_name} | `remove_connector()` | execute | Remove connector |
| GET | /api/connectors/health | `get_connectors_health()` | read | Connector health status |

---

## Data Quality

**quality.py endpoints:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| GET | /api/freshness | `get_freshness()` | read | Model freshness check (stale detection) |
| GET | /api/profiles | `get_profiles()` | read | All model profiles (row count, column stats) |
| GET | /api/profiles/{model_name} | `get_profile()` | read | Single model profile |
| GET | /api/assertions | `get_assertions()` | read | Run assertions/tests |
| GET | /api/contracts | `get_contracts()` | read | YAML contracts |
| GET | /api/contracts/history | `get_contracts_history()` | read | Contract execution history |
| POST | /api/contracts/run | `run_contracts()` | execute | Execute all contracts |
| GET | /api/alerts | `get_alert_history()` | read | Alert history |
| POST | /api/alerts/test | `test_alert()` | execute | Test alert channel (Slack/webhook) |
| GET | /api/cdc | `get_cdc_status()` | read | Change Data Capture status |
| POST | /api/cdc/{name}/reset | `reset_cdc_watermark()` | write | Reset CDC watermark |

---

## Linting

**lint.py endpoints:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| POST | /api/lint | `lint_endpoint()` | execute | Lint all transform files (SQLFluff) |
| POST | /api/lint/file | `lint_file_endpoint()` | execute | Lint single SQL file |
| GET | /api/lint/config | `get_lint_config()` | read | Get .sqlfluff contents |
| PUT | /api/lint/config | `save_lint_config()` | write | Save .sqlfluff |
| DELETE | /api/lint/config | `delete_lint_config()` | write | Delete .sqlfluff (revert to defaults) |

---

## Masking Policies

**masking.py endpoints:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| GET | /api/masking/methods | `list_methods()` | none | Available masking methods catalog |
| GET | /api/masking/policies | `list_policies()` | write | List all masking policies |
| POST | /api/masking/policies | `create_policy()` | write | Create masking policy |
| GET | /api/masking/policies/{policy_id} | `get_policy()` | write | Get single policy |
| PUT | /api/masking/policies/{policy_id} | `update_policy()` | write | Update policy |
| DELETE | /api/masking/policies/{policy_id} | `delete_policy()` | write | Delete policy |

**Masking methods** (14 total): hash, redact, null, partial, email, phone, credit_card, first_initial, ip_address, range, noise, date_shift, truncate, consistent_hash. Policies can have role exemptions.

---

## Collaboration

**collaboration.py endpoints & WebSocket:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| GET | /api/sessions | `list_sessions()` | read | List active collaboration sessions |
| POST | /api/sessions | `create_session()` | write | Create new session |
| GET | /api/sessions/{session_id} | `get_session_detail()` | read | Session details + participants |
| DELETE | /api/sessions/{session_id} | `delete_session_endpoint()` | write | Delete session |
| **WS** | /ws/collaborate/{session_id} | `register_websocket(app)` | required | Collaborate WebSocket |

WebSocket message format: JSON with `action`, `user_id`, `sql`, `result`, etc. Broadcasts shared SQL & query history to all participants.

---

## Agent Sidebar

**agent.py endpoints & WebSocket:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| **WS** | /ws/agent | `register_agent_websocket(app)` | required | Agent WebSocket |

Agent WebSocket: For AI coding assistants to interact with the havn UI. Messages are JSON with `type`, `content`, `code`, etc. Agents receive context (files, DAG, etc.) and can read/write files & execute transforms via the WebSocket.

System prompt includes:
- havn architecture & conventions
- Web UI tab structure (Develop, Explore, Observe, Configure)
- CLI commands
- Data layer schema (landing → bronze → silver → gold)
- SQL model & Python script conventions
- Security constraint: no access outside project root

---

## Git Operations

**git.py endpoints:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| GET | /api/git/status | `get_git_status()` | read | Branch, dirty, changed files, detailed file status |
| GET | /api/git/log | `get_git_log()` | read | Commit history (hash, message, author, date) |
| GET | /api/git/diff | `get_git_diff()` | read | Diff text (optional file filter, staged flag) |
| GET | /api/git/branches | `get_git_branches()` | read | List local + remote branches |
| GET | /api/git/stash | `get_git_stash_list()` | read | List stashes |
| GET | /api/git/remote | `get_git_remote()` | read | Remote URL |
| POST | /api/git/stage | `post_git_stage()` | write | Stage files |
| POST | /api/git/unstage | `post_git_unstage()` | write | Unstage files |
| POST | /api/git/commit | `post_git_commit()` | write | Create a commit |
| POST | /api/git/pull | `post_git_pull()` | write | Pull from remote |
| POST | /api/git/push | `post_git_push()` | write | Push to remote |
| POST | /api/git/branch | `post_git_create_branch()` | write | Create new branch |
| POST | /api/git/checkout | `post_git_checkout()` | write | Checkout branch |
| DELETE | /api/git/branch | `delete_git_branch()` | write | Delete branch |
| POST | /api/git/stash | `post_git_stash()` | write | Stash changes |
| POST | /api/git/stash/pop | `post_git_stash_pop()` | write | Pop latest stash |
| POST | /api/git/discard | `post_git_discard()` | write | Discard working directory changes |

**Pydantic Models:**
- StageRequest: files (list[str])
- UnstageRequest: files (list[str])
- CommitRequest: message (str, max 5000)
- PullRequest: remote (default "origin"), branch?
- PushRequest: remote (default "origin"), branch?
- CreateBranchRequest: name (str, max 250), checkout (default True)
- CheckoutRequest: branch (str)
- StashRequest: message?
- DiscardRequest: files (list[str])

All write endpoints require the project to be a git repo (400 if not). Read endpoints return empty/false gracefully. Engine functions shell out to the git CLI (no Python git libraries). Branch names and file paths are validated to prevent shell injection.

---

## Wiki & Documentation

**wiki.py endpoints:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| GET | /api/wiki | `list_pages()` | none | List wiki pages with slug/title/category |
| GET | /api/wiki/search/{query} | `search_pages()` | none | Search wiki by keyword |
| GET | /api/wiki/{slug} | `get_page()` | none | Get wiki page markdown |

---

## Pipeline Rewind (Time Travel)

**rewind.py endpoints:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| GET | /api/rewind/runs | `get_rewind_runs()` | read | Pipeline runs for slider |
| GET | /api/rewind/snapshots | `get_rewind_snapshots()` | read | All snapshot metadata for DAG time slider |
| GET | /api/rewind/snapshots/{run_id} | `get_run_snapshots()` | read | Snapshots from single run |
| GET | /api/rewind/sample/{run_id}/{model_name} | `get_snapshot_sample()` | read | Sample rows from snapshot |
| POST | /api/rewind/restore | `restore_snapshot()` | write | Restore from snapshot |
| GET | /api/rewind/downstream/{model_name} | `get_downstream_models()` | read | Downstream impact |
| POST | /api/rewind/gc | `run_rewind_gc()` | write | Garbage collect old snapshots |

---

## Schema Sentinel

**sentinel.py endpoints:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| POST | /api/sentinel/check | `run_check()` | read | Detect schema diffs on source tables |
| GET | /api/sentinel/diffs | `get_sentinel_diffs()` | read | List schema diffs |
| GET | /api/sentinel/impacts/{diff_id} | `get_sentinel_impacts()` | read | Impact of schema diff |
| GET | /api/sentinel/history/{source_name} | `get_sentinel_history()` | read | Source schema history |
| GET | /api/sentinel/sources | `get_sentinel_sources()` | read | All monitored sources |
| POST | /api/sentinel/apply-fix | `apply_sentinel_fix()` | write | Apply schema fix to model |
| POST | /api/sentinel/resolve | `resolve_sentinel_impact()` | write | Resolve schema impact |

---

## Metrics & Observability

**metrics.py endpoints:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| GET | /api/health | `health_check()` | none | Basic health check (DB connectivity, uptime, boot timestamp) |
| GET | /api/metrics | `get_metrics()` | read | Aggregate metrics (models, rows, build time) |

**audit.py endpoints:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| GET | /api/audit | `get_audit_log()` | read | Audit log (filterable by user/action/resource) |

**circuits.py endpoints:**

| Method | Path | Function | Auth | Purpose |
|--------|------|----------|------|---------|
| GET | /api/circuits | `get_circuits()` | read | Circuit breaker states |
| POST | /api/circuits/{name}/reset | `reset_circuit()` | execute | Reset circuit breaker to CLOSED |

---

## Error Handling

All endpoints return JSON. Common HTTP status codes:

- **200**: Success
- **400**: Bad request (validation, path traversal, invalid identifier, etc.)
- **401**: Unauthenticated (no Bearer token or invalid token)
- **403**: Forbidden (insufficient RBAC permission)
- **404**: Resource not found
- **408**: Query timeout (30s on /api/query)
- **409**: Conflict (file exists, can't overwrite)
- **422**: Unprocessable entity (unsupported encoding)
- **429**: Rate limited (login attempts)
- **500+**: Server error

---

## Request/Response Patterns

### Pydantic Models Summary

**Auth**
- LoginRequest: username, password
- CreateUserRequest: username, password, role (admin|editor|viewer), display_name?
- UpdateUserRequest: role?, password?, display_name?
- SetSecretRequest: key, value

**Files**
- SaveFileRequest: content (max 5MB)
- MoveFileRequest: destination

**Queries**
- QueryRequest: sql, limit?, offset?
- ExplainRequest: sql

**Transform**
- TransformRequest: targets?, force?
- DiffRequest: targets?, target_schema?, full?
- CreateModelRequest: name, schema_name, materialized, sql?

**Pipeline**
- RunScriptRequest: script_path

**Notebooks**
- SaveNotebookRequest: notebook (dict)
- NotebookRunCellRequest: source, cell_type (code|sql|ingest), reset?
- PromoteToModelRequest: sql_source, model_name, target_schema, description?, overwrite?

**Connectors**
- ImportFileRequest: file_path, target_schema, target_table?
- TestConnectionRequest: connection_type, params
- ConnectorSetupRequest: connector_type, connection_name, config, tables?, target_schema, schedule?

**Masking**
- PolicyCreate: schema_name, table_name, column_name, method, method_config?, condition_column?, condition_value?, exempted_roles?
- PolicyUpdate: (all fields optional)

**Quality**
- TestAlertRequest: channel, slack_webhook_url?, webhook_url?

**Versioning**
- DatabaseConfigUpdate: memory_limit?, threads?

**Collaboration**
- CreateSessionRequest: name?
- SessionQueryRequest: sql, user_id?

**Sentinel**
- ApplyFixRequest: model_path, old_name, new_name
- ResolveRequest: diff_id, model_name

**Git**
- StageRequest: files
- UnstageRequest: files
- CommitRequest: message
- PullRequest: remote?, branch?
- PushRequest: remote?, branch?
- CreateBranchRequest: name, checkout?
- CheckoutRequest: branch
- StashRequest: message?
- DiscardRequest: files

---

## Frontend API Client (api.ts)

All client calls go through the `api` object with Bearer token auto-injection from localStorage (key: `dp_token`). Auth events (401) dispatch `dp_auth_required` event.

**Example usage**:
```js
const result = await api.runQuery("SELECT * FROM bronze.customers");
await api.runStream("daily-refresh");  // Non-SSE (legacy)
api.runStreamSSE("daily-refresh", false, (event, data) => {
  // event: "start", "model_start", "model_end", "validation", "complete"
});
```

---

## Key Dependencies & Utilities

**deps.py exports**:
- `_get_project_dir()` / `_get_config()` / `_get_active_env()` / `_get_db_path()`
- `_discover_models_cached()` / `build_dag()` / `connect()` / `ensure_meta_table()`
- `_serialize()` / `_detect_language()` / `_validate_identifier()`
- `reset_shared_conn()` / `invalidate_config_cache()`

**Config schema** (from havn.config):
- name, database.path, database.memory_limit, database.threads
- streams: {name: StreamConfig}
- sources: [SourceConfig], exposures: [ExposureConfig]
- lint: {dialect, rules}, sentinel: {enabled, on_change}
- environments: {env_name: EnvConfig}, active_environment

**Internal DB schemas**:
- `_dp_internal.run_log` — pipeline execution history
- `_dp_internal.model_state` — SQL model metadata & cache state
- `_dp_internal.slow_queries` — query performance metrics
- `_dp_internal.model_profiles` — column statistics
- (auth only): `_dp_internal.users`, `_dp_internal.tokens`
- (audit only): `_dp_internal.audit_log`

---

## Performance & Limits

- **Query timeout**: 30 seconds (query interrupted on client timeout)
- **File size**: 5MB max for file saves
- **Slow query threshold**: 5000ms (logged to _dp_internal.slow_queries)
- **Rate limiting**: 5 login attempts per 60s per client IP
- **Notebook namespaces**: LRU cache of 50
- **Model cache**: Invalidated on transform/ file changes (mtime-based)
- **Config cache**: Invalidated on project.yml mtime change or env switch
- **Rewind snapshots**: Limit 5000 per query
- **History/Audit**: Default limit 50, configurable up to 500

---

## Security & Access Control

- **Path traversal**: All file paths validated to stay within PROJECT_DIR
- **Identifier validation**: SQL schema/table names validated (alphanumeric + underscore)
- **Critical files**: Cannot delete project.yml, .env, .gitignore
- **Auth required**: Most endpoints require Bearer token (except /auth/status, /health, /wiki)
- **RBAC**: read, write, execute, manage_users, manage_secrets permissions
- **Audit logging**: All transforms, queries, file edits, logins logged (if audit enabled)
- **Secrets masking**: Database connection passwords, API keys masked in logs
- **Agent sandbox**: Agent WebSocket strictly confined to project directory
