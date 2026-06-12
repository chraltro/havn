# API Reference

havn exposes a REST API via FastAPI at `http://localhost:3000`. All endpoints are prefixed with `/api/`. When authentication is enabled (`havn serve --auth`), include an `Authorization: Bearer <token>` header.

## Authentication

### POST /api/auth/login

Authenticate and receive a token.

```json
{"username": "admin", "password": "your-password"}
```

Returns: `{"token": "...", "username": "admin"}`

### GET /api/auth/me

Get the current authenticated user. Returns username, role, and display name.

### GET /api/auth/status

Check if auth is enabled and whether initial setup is needed.

Returns: `{"auth_enabled": true, "needs_setup": false}`

### POST /api/auth/setup

Create the first admin user. Only works when no users exist.

```json
{"username": "admin", "password": "password", "role": "admin"}
```

## User Management

Requires `admin` role.

### GET /api/users

List all users (no passwords returned).

### POST /api/users

Create a new user.

```json
{"username": "analyst", "password": "pass", "role": "viewer", "display_name": "Data Analyst"}
```

### PUT /api/users/{username}

Update user role, password, or display name.

```json
{"role": "editor", "display_name": "Senior Analyst"}
```

### DELETE /api/users/{username}

Delete a user and revoke all their tokens.

## Secrets

Requires `admin` role.

### GET /api/secrets

List secrets (keys and masked values).

### POST /api/secrets

Set or update a secret.

```json
{"key": "DB_PASSWORD", "value": "new_value"}
```

### DELETE /api/secrets/{key}

Delete a secret from `.env`.

## Files

### GET /api/files

List project files as a tree structure.

### GET /api/files/{path}

Read a file's content. Returns `{path, content, language}`.

### PUT /api/files/{path}

Save or create a file. Allowed extensions: `.sql`, `.py`, `.yml`, `.yaml`, `.dpnb`, `.sqlfluff`.

```json
{"content": "SELECT 1"}
```

### DELETE /api/files/{path}

Delete a file. Optional `?drop_object=true` to also drop the corresponding database object.

## Query

### POST /api/query

Execute an ad-hoc SQL query with a role-based timeout (admin 300s, editor 120s, viewer 60s by default).

```json
{"sql": "SELECT * FROM gold.summary WHERE region = $region", "params": {"region": "US"}, "limit": 1000, "offset": 0}
```

`params` binds named `$name` placeholders as DuckDB prepared-statement values (no string interpolation, so values cannot inject SQL). Numbers and booleans are bound typed; strings can be cast in SQL (`$day::DATE`).

Returns: `{columns, column_types, rows, truncated, offset, limit}`

Also intercepts masking SQL commands: `SHOW MASKING POLICIES`, `CREATE MASKING POLICY ON ...`, `DROP MASKING POLICY <id>`.

### POST /api/query/explain

Return the DuckDB EXPLAIN plan (structured + raw text). Accepts the same `sql` and `params` fields.

### POST /api/query/export-csv

Stream full query results as a CSV download (no row limit). Accepts the same `sql` and `params` fields.

## Tables

### GET /api/tables

List warehouse tables and views. Optional `?schema=gold` filter.

Returns: `[{schema, name, type}]`

### GET /api/tables/{schema}/{table}

Describe a table's columns.

Returns: `{schema, name, columns: [{name, type, nullable}]}`

### GET /api/tables/{schema}/{table}/sample

Get sample rows with pagination.

Query params: `?limit=100&offset=0`

Returns: `{schema, table, columns, rows, limit, offset}`

### GET /api/tables/{schema}/{table}/profile

Get column-level statistics: null counts, distinct counts, min/max, averages, sample values.

### GET /api/autocomplete

Get table and column names for query editor autocomplete.

## Models

### GET /api/models

List all SQL transform models with metadata.

Returns: `[{name, schema, full_name, materialized, depends_on, path, content_hash}]`

### POST /api/transform

Run the SQL transformation pipeline.

```json
{"targets": null, "force": false}
```

### POST /api/models/create

Create a new SQL model file.

```json
{"name": "my_model", "schema_name": "silver", "materialized": "table", "sql": "SELECT 1"}
```

### POST /api/check

Validate models, run assertions, and run contracts.

### POST /api/diff

Compare SQL output against materialized tables.

```json
{"targets": null, "target_schema": null, "full": false}
```

### GET /api/models/{model_name}/notebook-view

Get a notebook-style view combining SQL source, sample data, lineage, and dependencies.

## DAG

### GET /api/dag

Get the model dependency DAG (nodes and edges).

### GET /api/dag/full

Get the full DAG including seeds, sources, ingest scripts, and exposures.

## Lineage

### GET /api/lineage/{model_name}

Get column-level lineage for a model. Returns `{model, columns, depends_on}`.

### GET /api/lineage

Get column-level lineage for all models.

### GET /api/impact/{model_name}

Analyze downstream impact. Optional `?column=name` for column-level analysis.

## Pipeline

### POST /api/run

Run an ingest or export script.

```json
{"script_path": "ingest/customers.py"}
```

### POST /api/stream/{stream_name}/start

Start a pipeline in a background thread. Returns immediately with
`{"status": "started", "stream_name": "..."}` or `{"status": "already_running"}`.
This is the preferred entry point; the legacy single-shot
`POST /api/stream/{name}` still works for short pipelines.

Optional query parameter: `?force=true`.

### GET /api/stream/events

Server-sent events for the currently running pipeline. Emits:
`start`, `step_start`, `model_start`, `model_end`, `complete`, `error`.
Events are pushed via a server-side `threading.Condition`, so listeners do
not poll. Heartbeat (`: keepalive`) every 15 seconds when idle.

### GET /api/stream/active

Snapshot of the current pipeline state: `{ running, operation,
operation_label, stream_name, started_at, total_events, finished, status,
duration_seconds }`. Stale runs older than 10 minutes are auto-cleared.

### POST /api/stream/cancel

Request cancellation of the running pipeline. Workers check the cancel
flag between steps; in-flight queries are not interrupted by default.

### GET /api/streams

List configured streams with steps and schedules.

### GET /api/history

Get run history. Optional `?limit=50`.

### GET /api/scheduler

Get scheduler status and scheduled streams.

## Connectors

### GET /api/connectors/available

List all available connector types with parameters.

### GET /api/connectors

List connectors configured in the project.

### POST /api/connectors/test

Test a connector without setup.

```json
{"connector_type": "postgres", "config": {"host": "...", "database": "..."}}
```

### POST /api/connectors/discover

Discover available resources for a connector.

```json
{"connector_type": "postgres", "config": {"host": "..."}}
```

### POST /api/connectors/setup

Full connector setup: test, generate script, update config.

```json
{
  "connector_type": "postgres",
  "connection_name": "prod_db",
  "config": {"host": "...", "database": "..."},
  "tables": ["users", "orders"],
  "target_schema": "landing",
  "schedule": "0 6 * * *"
}
```

### POST /api/connectors/regenerate/{connection_name}

Regenerate the ingest script for an existing connector.

### POST /api/connectors/sync/{connection_name}

Run sync for a configured connector.

### DELETE /api/connectors/{connection_name}

Remove a connector (script and config).

### GET /api/connectors/health

Get last sync status for each connector.

### POST /api/webhook/{webhook_name}

Receive webhook data and store in `landing.<name>_inbox`.

Authentication: requires a shared secret. Set
`HAVN_WEBHOOK_SECRET_<NAME>` (per-webhook) or `HAVN_WEBHOOK_SECRET` (global)
and pass it as the `X-Havn-Webhook-Secret` header. To run open for local
development set `HAVN_WEBHOOK_OPEN=true`. Payloads are capped at 5 MB.

## CDC

### GET /api/cdc

Get CDC state for all tracked connectors.

### GET /api/cdc/{connector_name}

Get CDC state for a specific connector.

### POST /api/cdc/{connector_name}/reset

Reset CDC watermarks for a connector.

## Data Quality

### GET /api/freshness

Check model freshness. Optional `?max_hours=24`.

### GET /api/profiles

Get profile stats for all models.

### GET /api/profiles/{model_name}

Get profile stats for a specific model.

### GET /api/assertions

Get recent assertion results. Optional `?limit=100`.

### GET /api/assertions/{model_name}

Get assertion results for a specific model.

### GET /api/alerts

Get alert history. Optional `?limit=50`.

### POST /api/alerts/test

Send a test alert.

```json
{"channel": "slack", "slack_webhook_url": "https://hooks.slack.com/..."}
```

### GET /api/contracts

List all discovered contracts.

### POST /api/contracts/run

Run all data contracts.

### GET /api/contracts/history

Get contract evaluation history.

## Masking

### GET /api/masking/policies

List all masking policies.

### POST /api/masking/policies

Create a new masking policy.

```json
{
  "schema_name": "gold",
  "table_name": "customers",
  "column_name": "email",
  "method": "hash",
  "exempted_roles": ["admin"]
}
```

### GET /api/masking/policies/{policy_id}

Get a specific masking policy.

### PUT /api/masking/policies/{policy_id}

Update a masking policy.

### DELETE /api/masking/policies/{policy_id}

Delete a masking policy.

## Backup

### POST /api/backup

Create a verified backup with SHA-256 checksum. Optional body:

```json
{"no_verify": false, "note": "before deploy", "keep": 10}
```

Returns: `{path, size_bytes, sha256, timestamp}`

### GET /api/backups

List all tracked backups from the manifest.

### POST /api/backup/verify

Verify a backup file's integrity against its stored checksum.

```json
{"path": "_backups/warehouse_20260407_120000.duckdb"}
```

### POST /api/backup/restore

Restore the warehouse from a backup.

```json
{"path": "_backups/warehouse_20260407_120000.duckdb"}
```

### POST /api/backup/cleanup

Remove old backups, keeping the most recent N.

```json
{"keep": 5}
```

## Catalog

### GET /api/seeds

List all seed CSV files.

### POST /api/seeds

Load all seeds. Body: `{"force": false, "schema_name": "seeds"}`

### GET /api/sources

List declared sources from sources.yml.

### GET /api/sources/freshness

Check source freshness against SLAs.

### GET /api/exposures

List declared exposures.

### GET /api/environment

Get current and available environments.

### PUT /api/environment/{env_name}

Switch the active environment.

### GET /api/overview

Get platform overview: schemas, tables, rows, recent runs, connectors.

## Versioning

### GET /api/versions

List all warehouse versions.

### POST /api/versions

Create a new version snapshot.

### GET /api/versions/{version_id}

Get version details.

### GET /api/versions/{from_version}/diff

Diff two versions. Optional `?to_version=...` (defaults to current state).

### POST /api/versions/{version_id}/restore

Restore tables from a version.

### GET /api/versions/timeline/{table_name}

Get version history for a specific table.

## Documentation

### GET /api/docs/markdown

Generate markdown documentation for the project.

### GET /api/docs/structured

Generate structured documentation for the UI.

## Lint

### POST /api/lint

Lint SQL files. Body: `{"fix": false}`

## Git

### GET /api/git/status

Get git status: branch, dirty flag, changed files, last commit.

## Wiki

### GET /api/wiki

List all wiki pages with slugs, titles, and categories.

### GET /api/wiki/{slug}

Get a wiki page by slug. Returns title, content (markdown), and category.

## Collaboration

### WebSocket /ws/collaborate

Real-time collaboration WebSocket for concurrent file editing.

## Notebooks

### Various notebook endpoints

Notebook endpoints handle listing, reading, executing cells, and saving `.dpnb` files. See the FastAPI auto-generated docs at `/docs` for the full notebook API.
