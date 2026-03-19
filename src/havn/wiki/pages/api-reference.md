# API Reference

havn exposes a REST API via FastAPI at `http://localhost:3000`. All endpoints are prefixed with `/api/`. When authentication is enabled (`havn serve --auth`), include an `Authorization: Bearer <token>` header. Full OpenAPI docs are available at `/docs` when the server is running.

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

### POST /api/files/{path}/move

Move or rename a file.

```json
{"new_path": "transform/silver/renamed.sql"}
```

### DELETE /api/files/{path}

Delete a file. Optional `?drop_object=true` to also drop the corresponding database object.

## Query

### POST /api/query

Execute an ad-hoc SQL query with timeout (30s).

```json
{"sql": "SELECT * FROM gold.summary", "limit": 1000, "offset": 0}
```

Returns: `{columns, rows, truncated, offset, limit}`

Also intercepts masking SQL commands: `SHOW MASKING POLICIES`, `CREATE MASKING POLICY ON ...`, `DROP MASKING POLICY <id>`.

### POST /api/query/explain

Get the DuckDB EXPLAIN PLAN for a query.

```json
{"sql": "SELECT * FROM gold.summary WHERE region = 'US'"}
```

### POST /api/query/profile

Profile query performance with detailed execution metrics.

```json
{"sql": "SELECT * FROM gold.summary WHERE region = 'US'"}
```

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

### POST /api/models/create

Create a new SQL model file.

```json
{"name": "my_model", "schema_name": "silver", "materialized": "table", "sql": "SELECT 1"}
```

### GET /api/models/{model_name}/notebook-view

Get a notebook-style view combining SQL source, sample data, lineage, and dependencies.

### POST /api/transform

Run the SQL transformation pipeline.

```json
{"targets": null, "force": false}
```

### POST /api/check

Validate models, run assertions, and run contracts.

### POST /api/diff

Compare SQL output against materialized tables.

```json
{"targets": null, "target_schema": null, "full": false}
```

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

### POST /api/stream/{stream_name}

Run a full stream. Optional `?force=true`.

### GET /api/stream/{stream_name}/events

Server-Sent Events (SSE) for real-time pipeline execution progress. Each event includes step type, target, status, row count, duration, and error messages.

### POST /api/stream/cancel

Cancel a running stream.

### GET /api/streams

List configured streams with steps and schedules.

### GET /api/history

Get run history. Optional `?limit=50`.

### GET /api/scheduler

Get scheduler status and scheduled streams.

## Connectors

### GET /api/connectors/available

List all available connector types with their parameters and descriptions.

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

Receive inbound webhook data. Stores in `landing.<name>_inbox`.

## Data Import

### POST /api/import/preview-file

Preview an uploaded CSV/Parquet file before importing.

### POST /api/import/file

Import an uploaded file into a landing table.

### POST /api/import/test-connection

Test an external database connection for import.

### POST /api/import/from-connection

Import data from an external database.

```json
{
  "connection_type": "postgres",
  "config": {"host": "...", "database": "..."},
  "source_table": "users",
  "target_schema": "landing",
  "target_table": "users"
}
```

### POST /api/upload

Upload a file to the project.

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
  "method_config": {},
  "condition_column": null,
  "condition_value": null,
  "exempted_roles": ["admin"]
}
```

### GET /api/masking/policies/{policy_id}

Get a specific masking policy.

### PUT /api/masking/policies/{policy_id}

Update a masking policy.

### DELETE /api/masking/policies/{policy_id}

Delete a masking policy.

## Schema Sentinel

### POST /api/sentinel/check

Run a schema check on all configured sources. Returns diffs with impact analysis.

### GET /api/sentinel/diffs

Get recent schema diffs. Optional `?limit=50`.

### GET /api/sentinel/impacts/{diff_id}

Get impact analysis for a specific diff.

### GET /api/sentinel/sources

Get all monitored sources with existence status.

### GET /api/sentinel/history/{source_name}

Get schema snapshot history for a source. Optional `?limit=20`.

### POST /api/sentinel/resolve

Mark an impact as resolved/dismissed.

```json
{"diff_id": "diff_123", "model_name": "silver.customers"}
```

### POST /api/sentinel/apply-fix

Apply a rename fix to a model SQL file.

```json
{"model_path": "transform/silver/customers.sql", "old_name": "customer_name", "new_name": "full_name"}
```

## Pipeline Rewind

### GET /api/rewind/runs

List pipeline runs with timestamps, status, trigger type, and model count.

### GET /api/rewind/snapshots

List all snapshot metadata.

### GET /api/rewind/snapshots/{run_id}

Get snapshots for a specific run.

### GET /api/rewind/sample/{run_id}/{model_name}

Preview snapshot data. Optional `?limit=50`.

### POST /api/rewind/restore

Restore a model from a snapshot.

```json
{"run_id": "run-123", "model_name": "gold.customers", "cascade": true}
```

### GET /api/rewind/downstream/{model_name}

Get downstream models for cascade rebuild.

### POST /api/rewind/gc

Run garbage collection on expired snapshots.

## Versioning

### GET /api/versions

List all warehouse versions.

### POST /api/versions

Create a new version snapshot.

```json
{"description": "before-refactor"}
```

### GET /api/versions/{version_id}

Get version details including table list and metadata.

### GET /api/versions/{from_version}/diff

Diff two versions. Optional `?to_version=...` (defaults to current state).

### POST /api/versions/{version_id}/restore

Restore tables from a version.

### GET /api/versions/timeline/{table_name}

Get version history for a specific table.

## Snapshots

### POST /api/snapshot

Create a named project snapshot.

```json
{"name": "before-refactor"}
```

## Catalog

### GET /api/seeds

List all seed CSV files.

### POST /api/seeds

Load all seeds.

```json
{"force": false, "schema_name": "seeds"}
```

### GET /api/sources

List declared sources from project.yml.

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

## Documentation

### GET /api/docs/markdown

Generate markdown documentation for the project.

### GET /api/docs/structured

Generate structured documentation for the UI.

## Lint

### POST /api/lint

Lint SQL files.

```json
{"fix": false}
```

### POST /api/lint/file

Lint a single file.

```json
{"path": "transform/silver/customers.sql", "fix": false}
```

### GET /api/lint/config

Get current lint configuration.

### PUT /api/lint/config

Update lint configuration.

### DELETE /api/lint/config

Reset lint configuration to defaults.

## Git

### GET /api/git/status

Get git status: branch, dirty flag, changed files, last commit.

## Notebooks

### POST /api/notebooks/save/{name}

Save a notebook.

```json
{"cells": [...]}
```

### POST /api/notebooks/create/{name}

Create a new notebook.

### POST /api/notebooks/run/{name}

Run an entire notebook (all cells sequentially).

### POST /api/notebooks/run-cell/{name}

Run a single cell.

```json
{"cell_index": 2}
```

### POST /api/notebooks/promote-to-model

Convert a notebook SQL cell to a transform model.

```json
{"notebook_name": "explore.dpnb", "model_name": "my_model", "schema_name": "silver"}
```

### POST /api/notebooks/model-to-notebook/{model_name}

Convert a model to a notebook for interactive exploration.

### POST /api/notebooks/debug/{model_name}

Generate a debug notebook for a failed model.

## Monitoring and Metrics

### GET /api/health

Server health check. Returns status and uptime.

### GET /api/metrics

System metrics including memory usage, database size, and connection stats.

### GET /api/metrics/models

Model-specific metrics: build times, row counts, change frequency.

### GET /api/metrics/slow-queries

Slow query log sorted by execution time.

### GET /api/audit

Audit log of user actions. Optional `?limit=100`.

## Circuit Breakers

### GET /api/circuits

List circuit breaker states.

### POST /api/circuits/{name}/reset

Reset a tripped circuit breaker.

## Collaboration

### GET /api/sessions

List active collaboration sessions.

### POST /api/sessions

Create a new collaboration session.

### GET /api/sessions/{session_id}

Get session details.

### DELETE /api/sessions/{session_id}

Delete a collaboration session.

### POST /api/sessions/{session_id}/query

Run a shared query within a session.

### WebSocket /ws/collaboration/{session_id}

Real-time collaboration WebSocket for concurrent editing with live cursor and selection sync.

## Configuration

### GET /api/config/database

Get current database configuration (threads, memory_limit).

### PUT /api/config/database

Update database configuration.

```json
{"threads": 8, "memory_limit": "4GB"}
```

## Wiki

### GET /api/wiki

List all wiki pages with slugs, titles, and categories.

### GET /api/wiki/search/{query}

Search wiki pages by keyword. Returns matching pages with excerpts.

### GET /api/wiki/{slug}

Get a wiki page by slug. Returns title, content (markdown), and category.

## Agent

### GET /api/agents

List available AI agent adapters.

### WebSocket /ws/agent

AI agent streaming WebSocket for the sidebar assistant. Supports code generation, debugging, and optimization commands.
