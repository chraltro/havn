# havn -- Platform Summary Report

> **Version:** 0.2.19
> **Classification:** Self-hosted data platform (open-source alternative to Databricks / Snowflake)

---

## Executive Summary

**havn** is a self-hosted data platform that consolidates ingest, transformation, data quality, orchestration, masking, collaboration, and serving into a single tool backed by a single DuckDB file (self-hosted) or a DuckLake catalog (cloud-friendly). No data leaves the machine, no cloud account required, no vendor lock-in.

Where Databricks needs a cloud account, a Spark cluster, and dozens of services, havn runs on a laptop with `pip install havn` and delivers most of the same capability surface for everyday analytics workloads.

---

## Architecture

```
+--------------------------------------------------------------------+
|                       havn CLI / Web UI / SDK                       |
+--------------+----------------+----------------+-------------------+
|   Ingest     |   Transform    |   Export       |   Notebooks       |
|   (Python)   |   (SQL DAG)    |   (Python)     |   (.dpnb)         |
+--------------+----------------+----------------+-------------------+
|                       Backend abstraction                           |
|   DuckDB (single file)  |  DuckLake (Postgres/file catalog +        |
|                         |             Parquet on local/S3)          |
+--------------------------------------------------------------------+
|     Metadata (`_havn` schema): runs, model_state, profiles,         |
|     assertions, contracts, masking, audit, circuits, slow queries   |
+--------------------------------------------------------------------+
```

### Backends

- **DuckDB**: single warehouse.duckdb file. Default for self-hosted users.
- **DuckLake**: DuckDB compute + Postgres or file catalog + Parquet data path
  (local, S3, GCS, R2). Enables real multi-writer concurrency.

Both backends share the same engine, the same metadata schema, and the same
`_havn` table layout, so projects can move between them with no SQL changes.

---

## Feature Surface

### Transform engine (`src/havn/engine/transform/`)

- DAG execution with topological tier ordering, optional parallel workers.
- Change detection: SHA256 of normalized SQL plus transitive upstream hashes.
- Incremental materialization with four strategies: `delete+insert`,
  `append`, `merge` (true upsert), `partition_by`.
- Schema evolution: new columns are auto-added to incremental targets.
- Inline assertions (`@assert row_count > 0`, `unique(col)`, `no_nulls(col)`,
  `accepted_values(col, [...])`) and YAML contracts.
- Assertion debugging surfaces duplicates, null samples, unexpected values.
- Anomaly detection (z-score over historical profiles).
- Schema sentinel detects upstream schema drift and impact.
- Snapshots and rewind: deduplicated Parquet snapshots per run with restore-with-cascade.

### Connectors (`src/havn/connectors/`)

Ten built-in connectors: Postgres, MySQL, REST API, Google Sheets, CSV, S3/GCS,
Stripe, HubSpot, Shopify, Webhook. Each generates an ingest script and
optionally registers a CDC watermark column.

### Python SQL macros (`src/havn/engine/macros.py`)

Python functions become DuckDB UDFs at connection time. Includes scalar and
table-returning macro support, hot-reload via the file watcher, and editor
autocomplete via `GET /api/macros`. A `havn.stdlib` ships PII helpers
(mask_email, hash_consistent, etc.) usable directly in SQL.

### Data masking (`src/havn/engine/masking.py`, `masking_rewriter.py`)

Pre-query SQL rewriting (with post-query fallback). Fourteen methods cover
general redaction, PII categories, financial data, and analytics-safe
transforms (range bucketing, deterministic noise, date shift, consistent
hashing). Per-schema/table/column policies with conditional rules and
role-based exemption.

### Server (`src/havn/server/`)

FastAPI app with 21 route modules and 150+ endpoints. Shared DuckDB connection
singleton with per-thread cursors (Windows file-lock constraint). Pipeline
runs in a background worker thread; SSE listeners are pushed via a
`threading.Condition`. WebSockets for collaboration and the agent sidebar.
Arrow Flight SQL server is also available.

### Web UI (`frontend/`)

React 19 + Vite single-page app with Monaco editor. Five sections (Overview,
Develop, Explore, Observe, Configure), 14+ tabs, canvas-based DAG
visualization with rewind timeline, dashboard designer, query plan tree,
3-mode diff, command palette, agent sidebar.

### CLI (`src/havn/cli/`)

50+ commands across project lifecycle (init, validate, status, checkpoint,
context, backup, restore), pipeline (transform, jobs, run, watch, schedule,
lint, seed), model analysis (validate, promote, debug, impact, lineage,
explain), querying (query, tables, shell, history), data quality (check,
freshness, profile, assertions, contracts), masking (mask), diff and
snapshots (diff, snapshot, rewind, restore, sentinel, version), admin
(serve, ci, secrets, users, env), connectors (connect, connectors), and
streaming integration (flight, streaming).

### Auth and RBAC (`src/havn/engine/auth.py`)

Token-based auth with PBKDF2-HMAC-SHA256 password hashing. Three roles:
admin (full), editor (read/write/execute), viewer (read). 30-day token
expiry, rate-limited login. Token validation cached for 30 seconds to keep
authenticated traffic off the single write connection.

### Observability

Prometheus metrics at `/metrics` (optional bearer-token auth), slow-query
log, audit log of 20+ action types, alert log with multi-channel delivery
(Slack, generic webhook, Python logger).

---

## Internal metadata (`_havn` schema)

All metadata lives inside the warehouse itself:

- `model_state` -- content_hash, upstream_hash, materialized_as, row_count, run_duration_ms
- `run_log` -- pipeline execution history with status, duration, error, log_output
- `model_profiles` -- auto-computed column statistics (input to anomaly detection)
- `assertion_results` -- DQ check results with diagnostics JSON
- `anomaly_log` -- z-score-driven anomaly detections
- `masking_policies` -- column masking rules
- `audit_log` -- user action trail
- `slow_queries` -- queries exceeding the 5s threshold
- `alert_log` -- notification delivery tracking
- `circuit_state` -- circuit breaker persistence
- `users`, `tokens` -- auth (when enabled)
- `contract_results`, `cdc_state`, `version_history`, `job_runs` -- quality,
  CDC, versioning, orchestration state

---

## Security posture

- Strict identifier validation (regex allowlist) on every place a user
  string enters SQL.
- Path traversal closed via `Path.is_relative_to()` everywhere.
- Query endpoint validates SQL after stripping strings and comments,
  walks past CTE bodies via balanced-paren skipping, and rejects file-access
  functions (`read_csv`, `read_parquet`, `httpfs_*`) including quoted-identifier
  variants.
- Webhook endpoint requires a shared secret (`HAVN_WEBHOOK_SECRET`).
- Token validation cached but invalidated on user delete / token revoke.
- Subprocess agents use `create_subprocess_exec` with arg arrays (no shell).
- WriteQueue worker survives cancelled futures (no permanent stall).

---

## Roadmap (excerpt)

- Aggregate UDFs and pip-installable macro packs.
- Live collaboration sessions: multi-user cursor + presence in the editor.
- Cloud / hosted version on DuckLake with control-plane provisioning,
  Stripe billing, and tenant-per-container routing.

For the full backlog see `docs/internal/to-do.md` (gitignored, working
file).
