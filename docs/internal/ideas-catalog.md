# havn Ideas Catalog

Compiled 2026-03-20. Six-agent deep analysis of the full codebase: backend engine, connectors/CLI, server/API, tests/docs, UI design, and UI edge cases.

---

## How to read this

- **Priority**: HIGH = blocking or high-value, MEDIUM = important but not urgent, LOW = nice-to-have
- **Effort**: S = hours, M = days, L = weeks
- Items marked with a star are **quick wins** (high impact, low effort)

---

## 1. Backend Engine

### Transform Engine

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 1.1 | Incremental config validation at discovery time (require unique_key for merge/delete+insert, validate partition_by columns exist) | HIGH | M | Currently only fails at runtime |
| 1.2 | Execution step logging & replay (`havn replay <model>` with per-SQL-step timing) | HIGH | M | Hard to debug parallel failures today |
| 1.3 | Partition-by auto-detection (suggest partition columns from GROUP BY/ORDER BY heuristics) | HIGH | M | Reduces config burden |
| 1.4 | Schema evolution audit trail (track column additions in audit_log, add schema_hash to model_state) | HIGH | M | No rollback path today |
| 1.5 | Model-level retry with backoff for transforms (transient lock/contention failures) | MEDIUM | M | Circuit breaker exists for scripts but not transforms |
| 1.6 | Soft delete incremental strategy (flag deleted rows instead of hard delete, with purge CLI) | MEDIUM | M | Needed for CDC sources |
| 1.7 | Model execution timing breakdown (t_plan, t_staging, t_merge, t_insert per model) | MEDIUM | S | Currently just total duration |
| 1.8 | Column lineage caching in metadata table (invalidate on upstream content_hash change) | MEDIUM | S | Full lineage recompute is slow on 100+ models |

### Diff Engine

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 1.9 | 3-mode diff: single model, changed+downstream, full database | HIGH | M | **In progress** — being built now |
| 1.10 | Dry-run / what-if mode (`havn transform --dry-run <model>` — diff without persisting) | MEDIUM | S | |

### Snapshots & Rewind

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 1.11 | Point-in-time restore by timestamp (`havn rewind --to "2h ago"`) | HIGH | M | Currently requires knowing exact run_id |
| 1.12 | Incremental/delta snapshots (store only changed rows for incremental models, ~70% storage savings) | HIGH | M | Full parquet per snapshot is wasteful |
| 1.13 | Snapshot diff preview before restore (show what restore would change) | MEDIUM | M | |
| 1.14 | GC dashboard (track freed bytes, deletion history, storage growth chart) | MEDIUM | S | |

### Data Quality

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 1.15 | Contract-based alerting with thresholds (`row_count > {previous * 0.9}`, freshness escalation) | HIGH | M | Binary pass/fail is too simple |
| 1.16 | Assertion debugging (on failure: show top 10 duplicates, actual values, sample null rows) | HIGH | M | Currently just "5 duplicates" with no detail |
| 1.17 | Anomaly detection via statistical profiling (Z-score on row_count/null%/distinct% over N runs) | HIGH | M | |
| 1.18 | Assertion sampling for large tables (TABLESAMPLE for non-exhaustive checks) | MEDIUM | S | |
| 1.19 | Incremental assertion evaluation (skip assertions for unchanged models) | MEDIUM | M | |

### Masking & Security

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 1.20 | Masking policy versioning & audit trail (who changed what, when, revert) | HIGH | M | |
| 1.21 | Fine-grained role-based masking exemptions (analyst sees email but not credit_card) | HIGH | M | Currently all-or-nothing per role |
| 1.22 | Masking performance: column-level lazy evaluation, pre-compiled mask plans | MEDIUM | M | Slow on 1M rows with 50 policies |

### Circuit Breaker & Error Handling

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 1.23 | Circuit breaker status UI in Observe section (state, failure count, recovery time, manual reset) | MEDIUM | M | |
| 1.24 | Error categorization (transient vs permanent) with auto-recovery suggestions | MEDIUM | M | |

### Scheduling & Orchestration

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 1.25 | Stream dependency ordering (cross-stream DAG with `depends_on: [other_stream]`) | HIGH | M | |
| 1.26 | Schedule preview (`havn schedule --preview` — show next 10 runs with timestamps) | HIGH | M | |
| 1.27 | Backfill with date range (`havn stream backfill <stream> --from --to`) | HIGH | M | |
| 1.28 | File watcher debounce configuration (currently hardcoded 2s) | MEDIUM | S | |

---

## 2. Connectors & CLI

### Missing Connectors

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 2.1 | Snowflake connector | HIGH | M | Critical for competitive positioning / migration story |
| 2.2 | BigQuery connector | HIGH | M | GCP trifecta |
| 2.3 | Redshift connector | HIGH | M | AWS trifecta |
| 2.4 | MongoDB connector | MEDIUM | M | Unstructured data gap |
| 2.5 | Salesforce connector | MEDIUM | M | Alongside HubSpot |
| 2.6 | Airtable, Jira, Slack, GitHub/GitLab connectors | LOW | L | Community value |

### Connector Framework

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 2.7 | ParamSpec validation (regex, enums, min/max, cross-param checks) | HIGH | S | Catches config errors early |
| 2.8 | Connector dry-run (`havn connect --dry-run` — preview generated script) | MEDIUM | S | |
| 2.9 | Connector versioning & upgrade (`havn connect --upgrade <name>`) | MEDIUM | M | Scripts are frozen after generation today |
| 2.10 | Standardize incremental sync across all connectors (high-watermark pattern) | MEDIUM | M | Only Postgres/MySQL have CDC today |
| 2.11 | Multi-table transaction safety (atomic sync option) | MEDIUM | S | |
| 2.12 | Connector health dashboard (last sync, duration, failure rate per source) | LOW | M | |
| 2.13 | Community connector registry (pip-installable connector packs) | LOW | L | |

### CLI Commands

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 2.14 | `havn env` command (list/use/create environments) | HIGH | S | Currently repeating `--env` on every command |
| 2.15 | `havn sample <table>` (quick data preview without writing SQL) | MEDIUM | S | |
| 2.16 | `havn export <model> --format csv/parquet` | MEDIUM | S | |
| 2.17 | `havn model <name> --watch` (auto-rebuild on save with live feedback) | MEDIUM | M | FileWatcher exists, just needs single-model mode |
| 2.18 | `havn repl` (interactive SQL shell against warehouse) | MEDIUM | M | |
| 2.19 | `havn monitor --stream <name>` (live pipeline status in terminal) | MEDIUM | S | |
| 2.20 | `havn test` (end-to-end: ingest sample data, transform, validate, export) | MEDIUM | M | |
| 2.21 | `havn context` (dump project metadata for debugging/onboarding) | LOW | S | |
| 2.22 | Better error messages with hints ("No project.yml found" → suggest `havn init`) | MEDIUM | S | |
| 2.23 | Grouped output & summary stats for large outputs (`--errors-only`, `--json`) | MEDIUM | S | |
| 2.24 | `--profile` flag on major commands (per-step timing breakdown) | HIGH | S | |

### Configuration

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 2.25 | Config validation (cron expressions, retention duration format, stream step targets) | HIGH | M | |
| 2.26 | Config inheritance for environments (deep merge base + env overrides) | MEDIUM | M | Currently must duplicate entire connection block |
| 2.27 | Secret reference validation (check all `${VAR}` refs exist in .env) | MEDIUM | S | |
| 2.28 | Per-stream config (timeout, retries, retry_delay, alert channels) | MEDIUM | S | |
| 2.29 | Config versioning with migration logic for breaking changes | LOW | S | |
| 2.30 | Project templates (`havn init --template ecommerce/saas`) | MEDIUM | S | |

---

## 3. Server & API

### API Design

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 3.1 | Standardized response envelope (`{success, data, error: {code, message}}`) | HIGH | M | Inconsistent formats across 150+ endpoints |
| 3.2 | Pagination for high-volume list endpoints (history, audit, rewind runs, table samples) | HIGH | M | |
| 3.3 | Centralized request validation module (file paths, SQL identifiers, git refs, resource names) | HIGH | M | Currently scattered, inconsistent |
| 3.4 | Request ID tracking (`X-Request-ID` header for correlation) | MEDIUM | M | |
| 3.5 | Endpoint documentation (docstrings, example requests/responses on all routes) | MEDIUM | S | FastAPI auto-generates OpenAPI but descriptions are sparse |
| 3.6 | Helpful error messages ("Invalid identifier 'foo'" → "Must start with letter, contain only alphanumeric + underscore") | MEDIUM | S | |

### Auth & Security

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 3.7 | Token refresh flow (short-lived access + long-lived refresh token) | HIGH | M | Currently fixed 30-day tokens |
| 3.8 | Token metadata (device name, last used, IP address) | MEDIUM | M | |
| 3.9 | Custom RBAC roles with fine-grained permissions | MEDIUM | M | |
| 3.10 | Schema-level access control (role can query gold but not landing) | MEDIUM | M | |
| 3.11 | Permission denial audit logging | HIGH | S | |
| 3.12 | Secret rotation strategy (versioning, expiry dates, rotation reminders) | MEDIUM | M | |
| 3.13 | SQL injection audit of dynamic SQL paths | HIGH | S | |

### Audit & Observability

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 3.14 | Expand audit actions (auth_failed, permission_denied, connector_sync, masking_policy changes, agent file edits) | HIGH | S | |
| 3.15 | Persistent pipeline event log (store SSE events to `_havn.pipeline_events`) | MEDIUM | M | Lost on server restart today |
| 3.16 | Circuit breaker state change logging | MEDIUM | S | |
| 3.17 | Webhook retry with exponential backoff (currently fire-and-forget) | MEDIUM | S | |

### Pipeline Execution

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 3.18 | Query cancellation via DuckDB API (cancel in-flight SQL for long transforms) | HIGH | M | Cancel flag not always checked |
| 3.19 | Pipeline pause & resume (pause after current step, resume from checkpoint) | MEDIUM | M | |
| 3.20 | Ring buffer for pipeline events (prevent OOM on very long runs) | MEDIUM | S | Currently unbounded list |

### Collaboration

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 3.21 | WebSocket heartbeat/ping mechanism | HIGH | M | No reconnection on network blips |
| 3.22 | Session persistence to `_havn` (survive server restarts) | MEDIUM | L | |

### Alerting

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 3.23 | New alert types: circuit_breaker_open, schema_drift, slow_query, token_expiring | MEDIUM | S | |
| 3.24 | Alert deduplication (skip if same alert sent in last 5 min) | MEDIUM | S | |
| 3.25 | Email alert channel | MEDIUM | M | Only Slack/webhook/log today |
| 3.26 | Alert health check endpoint (is Slack webhook still working?) | MEDIUM | S | |

### Missing Endpoints

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 3.27 | Pipeline dry-run (`POST /api/stream/{name}/dry-run`) | MEDIUM | M | |
| 3.28 | Config validation endpoint (`POST /api/validate/config`) | MEDIUM | S | |
| 3.29 | Audit log export (`GET /api/audit/export?format=csv`) | MEDIUM | S | |
| 3.30 | User session management (list/force-logout active sessions) | MEDIUM | M | |

---

## 4. Frontend — Design & UX

### Critical (Ship Blockers)

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 4.1 | Accessibility: ARIA labels, roles, semantic landmarks across all components | HIGH | L | Zero WCAG support today |
| 4.2 | Visible focus indicators for keyboard navigation | HIGH | S | |
| 4.3 | Modal focus trapping (dialogs don't trap focus or block background interaction) | HIGH | M | |

### Moderate

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 4.4 | Consistent disabled button styling and hover states | MEDIUM | S | Mix of inline handlers and missing states |
| 4.5 | Color contrast validation across all 8 themes | MEDIUM | S | No WCAG AA checks |
| 4.6 | Reusable LoadingSpinner, EmptyState, ErrorState components | MEDIUM | M | Different patterns everywhere |
| 4.7 | Scroll hints on long lists (shadow/gradient at bottom) | MEDIUM | S | |
| 4.8 | Responsive design fixes (sidebar collapse, dialog overflow at <1200px) | MEDIUM | M | Desktop-only today |
| 4.9 | Actionable error messages ("Failed to open: Error" → explain + suggest fix) | MEDIUM | S | |
| 4.10 | Search/filter in file tree, schema tree, table browser | MEDIUM | M | No search on large lists |
| 4.11 | Consistent spacing system (CSS variables instead of hardcoded px values) | MEDIUM | M | |
| 4.12 | Viewport-aware positioning for dropdowns and tooltips | MEDIUM | S | PipelineMenu, GuideTour can overflow |

### Minor Polish

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 4.13 | Tooltips for truncated table cells | LOW | S | |
| 4.14 | Proper file type icons (replace colored dots and "V"/"T" letters) | LOW | S | |
| 4.15 | Consistent date/time formatting (relative time everywhere, absolute on hover) | LOW | S | |
| 4.16 | Breadcrumb / back navigation for deep views | LOW | S | |
| 4.17 | Standardize button text casing (mix of "Run", "LINT", "Format SQL") | LOW | S | |
| 4.18 | DAG node dynamic sizing for long model names | LOW | S | Fixed 160px truncates |
| 4.19 | Colorblind-safe status indicators (icons + text, not just red/green) | LOW | S | |
| 4.20 | Dark mode `<meta name="color-scheme">` tag | LOW | S | |

---

## 5. Frontend — Edge Cases & Bugs

### Critical

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 5.1 | SSE stream reconnection with backoff (server restart mid-pipeline = silent hang) | HIGH | M | **Most critical frontend bug** |
| 5.2 | API error handling: retry logic, human-readable errors instead of raw HTML | HIGH | M | |
| 5.3 | File edit conflict detection (etag/version check before save, agent vs user) | HIGH | M | |
| 5.4 | Monaco size limit check (warn >10MB, refuse >50MB) | MEDIUM | S | |

### Moderate

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 5.5 | Autocomplete debounce (fires on every keystroke, no debounce) | MEDIUM | S | |
| 5.6 | Schema cache expiry (global Map with no TTL, stale completions) | MEDIUM | S | |
| 5.7 | Git panel merge conflict detection and display | MEDIUM | M | Pull succeeds but conflicts invisible |
| 5.8 | Git branch name validation before creation | MEDIUM | S | |
| 5.9 | Rewind timeline virtualization (5000 runs = 250K snapshots in state) | MEDIUM | M | |
| 5.10 | Prevent multiple dialogs opening simultaneously (single dialog queue) | MEDIUM | S | |
| 5.11 | Unsaved changes warning before file delete | MEDIUM | S | |
| 5.12 | WarehouseContext: show error when initial load fails (tables/files/streams) | MEDIUM | S | Fails silently, shows blank |
| 5.13 | sessionStorage quota handling (try-catch on writes, LRU eviction) | MEDIUM | S | |
| 5.14 | FileTree memoization and virtualization for large projects | MEDIUM | M | Re-sorts on every render |
| 5.15 | OutputPanel regex link detection improvements (Windows paths, dashes in names) | MEDIUM | S | |
| 5.16 | WebSocket cleanup on sidebar close / page unload | MEDIUM | S | |
| 5.17 | Autocomplete dropdown viewport clamping | LOW | S | |
| 5.18 | DAG layout cycle handling (circular deps = corrupted visualization) | LOW | M | |
| 5.19 | Query history deduplication and bounded growth | LOW | S | |

---

## 6. Testing & Code Quality

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 6.1 | Custom exception hierarchy (`HavnError`, `TransformError`, `ConnectorError`, etc.) | HIGH | M | 83 bare `except Exception:` blocks |
| 6.2 | Performance benchmarks with regression detection (pytest-benchmark) | HIGH | M | No guardrails against perf regressions |
| 6.3 | Chaos/failure injection tests (connection loss mid-pipeline, timeouts, corrupted snapshots) | HIGH | M | |
| 6.4 | Structured logging with correlation IDs (JSON format, run_id tracing) | MEDIUM | M | 123 logging calls, no structure |
| 6.5 | Centralized input validation module (Pydantic models for identifiers, paths, cron) | MEDIUM | M | Duplicated across modules |
| 6.6 | Shared test fixtures in conftest.py (sample_project, complex_dag) | MEDIUM | M | Repeated setup across 40 files |
| 6.7 | Windows-specific CI tests (file locking, path separators, CRLF) | MEDIUM | M | |
| 6.8 | Integration tests with real external services (Postgres, MySQL via docker-compose) | MEDIUM | L | All tests use local DuckDB only |

---

## 7. New Feature Ideas

| # | Idea | Priority | Effort | Notes |
|---|------|----------|--------|-------|
| 7.1 | Query plan visualization in web UI (EXPLAIN ANALYZE with operator tree) | HIGH | M | DuckDB generates detailed plans, UI doesn't show them |
| 7.2 | Model validation report UI (run all checks before pipeline, show fixes) | MEDIUM | M | |
| 7.3 | Column ownership & CODEOWNERS-style docs per model/column | MEDIUM | M | |
| 7.4 | Script parameter passing (`havn run ingest/fetch.py --param source=prod`) | HIGH | M | Currently all hardcoded |
| 7.5 | Structured script output protocol (JSON metrics from scripts, not just regex) | HIGH | M | |
| 7.6 | CDC watermark management UI (inspect, reset, sync history per connector) | HIGH | M | Currently requires raw SQL to fix |
| 7.7 | Multi-project workspace support (multiple warehouses in one server) | MEDIUM | L | |
| 7.8 | OpenTelemetry distributed tracing (optional) | LOW | M | |
| 7.9 | Health check & Prometheus metrics endpoints | MEDIUM | S | |
| 7.10 | Architecture Decision Records (ADRs) in `docs/adr/` | MEDIUM | S | |

---

## Quick Wins Summary

High impact, low effort — do these first:

| # | Idea | Est. |
|---|------|------|
| 2.7 | Connector ParamSpec validation | hours |
| 2.14 | `havn env` command | hours |
| 2.24 | `--profile` flag on major commands | hours |
| 3.14 | Expand audit log actions | hours |
| 3.6 | Helpful API error messages | hours |
| 3.13 | SQL injection audit of dynamic SQL | hours |
| 4.2 | Visible focus indicators | hours |
| 5.5 | Autocomplete debounce | hours |
| 5.8 | Git branch name validation | hours |
| 5.11 | Unsaved changes warning before delete | hours |

---

## Strategic Priorities

### For competitive positioning (vs Databricks/Snowflake)
- Snowflake + BigQuery + Redshift connectors (2.1–2.3)
- Query plan visualization (7.1)
- Anomaly detection (1.17)

### For enterprise readiness
- Token refresh flow (3.7)
- Custom RBAC (3.9)
- Audit completeness (3.14)
- Masking policy versioning (1.20)

### For developer experience
- `havn env` (2.14), `havn sample` (2.15), `havn export` (2.16)
- Watch mode (2.17)
- Better error messages (2.22, 3.6)
- Assertion debugging (1.16)

### For production reliability
- SSE reconnection (5.1)
- Chaos tests (6.3)
- Circuit breaker UI (1.23)
- Pipeline pause/resume (3.19)

---

*Generated by 6 parallel analysis agents scanning the full havn codebase.*
