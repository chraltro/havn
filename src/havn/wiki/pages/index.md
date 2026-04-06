# havn Data Platform

havn is a self-hosted data platform -- a lightweight alternative to Databricks and Snowflake. It uses DuckDB for OLAP analytics, plain SQL for transforms, and Python for ingest/export scripts. All data lives in a single `warehouse.duckdb` file. No data leaves your machine.

## Architecture Overview

havn follows a medallion architecture with four data layers, each represented as a DuckDB schema:

```
Ingest Scripts (.py / .dpnb)
        |
        v
  +-----------+      +----------+      +----------+      +----------+
  |  landing  | ---> |  bronze  | ---> |  silver  | ---> |   gold   |
  |  (raw)    |      | (cleaned)|      | (business|      | (consume)|
  +-----------+      +----------+      +----------+      +----------+
        ^                                                       |
        |                                                       v
   External Sources                                      Export Scripts
   (APIs, DBs, files)                                    (reports, APIs)
```

- **landing** -- Raw data ingested from external sources via Python scripts or notebooks
- **bronze** -- Light cleanup: column renaming, type casting, deduplication
- **silver** -- Business logic: joins, aggregations, calculations
- **gold** -- Consumption-ready tables for dashboards, APIs, and reports

All metadata (model state, run logs, users, tokens) is stored in a hidden `_havn` schema within the same DuckDB file.

## Web UI

The havn web interface is a React SPA (Single Page Application) built with Vite and Monaco editor, serving as the primary way to interact with the platform. It's organized into five main sections:

### Overview
- Project health dashboard with recent runs and pipeline status
- Quick actions for triggering transforms, ingest, and export steps
- Run history summary with success/failure counts and execution times
- Connector health and active data source status

### Develop
- **Editor** -- Monaco code editor with SQL syntax highlighting, formatting, autocomplete, and validation for transforms and scripts. See [Transforms](transforms).
- **Notebooks** -- Interactive `.dpnb` notebook editor with code, SQL, and markdown cells for exploratory analysis. See [Notebooks](notebooks).
- **DAG** -- Visual dependency graph showing all models, seeds, sources, and exposures with their relationships; supports Rewind to restore tables from snapshots. See [Lineage](lineage).

### Explore
- **Query** -- Interactive SQL runner with result pagination, CSV/JSON export, query history, EXPLAIN PLAN, and query profiling. See [Quality](quality).
- **Tables** -- Warehouse browser showing all schemas, tables, and column metadata with profiling stats and sample data. See [Transforms](transforms).
- **Data Sources** -- Connector management, import wizard for CSV/Parquet uploads, external database imports, and freshness monitoring. See [Connectors](connectors).

### Observe
- **Quality** -- Data quality dashboard with freshness status, column profiles, assertion results, and contract violations. See [Quality](quality).
- **Sentinel** -- Schema change detection alerting on unexpected column additions, removals, or type changes with impact analysis. See [Sentinel](sentinel).
- **Diff** -- Preview and compare changes before transforms are applied. See [Versioning](versioning).
- **History** -- Complete run log with execution times, error messages, and pipeline status.

### Configure
- **Masking** -- Column-level data masking with 14 methods across general, PII, financial, and analytics categories. See [Masking](masking).
- **Wiki** -- Documentation editor for project-specific knowledge base.
- **Docs** -- Auto-generated documentation from SQL comments and metadata.
- **Settings** -- Theme preferences, scheduler configuration, secrets management, user/role management, alerts, resource limits, and environment switching. See [Auth](auth), [Environments](environments).

## Key Features

### Data Pipeline
- **SQL Transforms** -- Plain SQL with `-- config:` and `-- depends_on:` comments; no Jinja or templating. Supports table, view, and incremental materializations. See [Transforms](transforms).
- **DAG Engine** -- Automatic dependency resolution and topological ordering with parallel execution and change detection via SHA256 hashing. See [Transforms](transforms).
- **Streams** -- Multi-step pipelines (ingest, seed, transform, export) defined in `project.yml` with retries, webhook notifications, and real-time SSE streaming. See [Pipelines](pipelines).
- **Orchestration Jobs** -- YAML-defined jobs with dbt-style selectors, multiple schedules (cron and interval), tags, and a visual DAG picker. See [Orchestration Jobs](orchestration-jobs).
- **Seeds** -- CSV files loaded as reference tables with change detection. See [Seeds](seeds).

### Connectors and Integration
- **Data Connectors** -- Pre-built connectors for PostgreSQL, MySQL, Stripe, Shopify, HubSpot, Google Sheets, REST APIs, S3/GCS, CSV files, and webhooks. See [Connectors](connectors).
- **CDC** -- Change Data Capture with high-watermark tracking, file modification tracking, and full-refresh modes. See [CDC](cdc).
- **Sources** -- Declared external source metadata with freshness SLAs. See [Sources](sources).
- **Import Wizard** -- Upload CSV/Parquet files or import from external databases through the web UI. See [Connectors](connectors).

### Data Quality
- **Inline Assertions** -- `-- assert:` comments in SQL files for row_count, no_nulls, unique, accepted_values, and custom expressions. See [Quality](quality).
- **YAML Contracts** -- Standalone data quality rules in `contracts/` with severity levels and history tracking. See [Contracts](contracts).
- **Profiling** -- Automatic column-level statistics: null percentages, distinct counts, min/max values.
- **Freshness Monitoring** -- Detect stale models and sources against configured SLAs with alerting support.
- **Alerts** -- Slack and webhook notifications for pipeline failures, assertion failures, and stale data. See [Quality](quality).

### Security
- **Authentication** -- Token-based auth with RBAC roles: admin, editor, viewer. See [Auth](auth).
- **Data Masking** -- Column-level masking with 14 methods (hash, redact, null, partial, email, phone, credit_card, first_initial, ip_address, range, noise, date_shift, truncate, consistent_hash) with role exemptions and conditional application. See [Masking](masking).
- **Audit Logging** -- Track user actions including logins, queries, pipeline runs, and file edits.
- **Secrets Management** -- Encrypted `.env` variable management via CLI, web UI, and API. See [Configuration](configuration).

### Development Tools
- **Column-Level Lineage** -- AST-based SQL analysis via sqlglot tracing columns through CTEs, subqueries, and joins. See [Lineage](lineage).
- **Impact Analysis** -- Analyze downstream effects of model or column changes. See [Lineage](lineage).
- **Notebooks** -- Interactive `.dpnb` notebooks with code, SQL, and markdown cells. See [Notebooks](notebooks).
- **Debug Notebooks** -- Auto-generated notebooks for investigating failed models. See [Notebooks](notebooks).
- **SQL Promotion** -- Convert ad-hoc queries or notebook cells into proper transform models. See [Notebooks](notebooks).
- **Agent Sidebar** -- AI coding assistant embedded in the UI for code generation, debugging, and optimization.
- **Collaboration** -- Real-time collaborative editing sessions with live cursor sync.
- **Pull Requests** -- Local PR system: create, build-in-worktree, review (AI or human), approve, and merge with pre-merge data diffs. See [Pull Requests](pull-requests).

### Versioning and Time Travel
- **Versioning** -- Parquet-based snapshots with time travel, diff, and restore. See [Versioning](versioning).
- **Rewind** -- Time travel through pipeline runs from the DAG view with row deltas and restore capability. See [Versioning](versioning).
- **Schema Sentinel** -- Automated schema change detection with alerts on unexpected column changes and impact analysis. See [Sentinel](sentinel).

### Operations
- **Scheduler** -- Cron-based scheduling with file watcher for auto-rebuild. See [Scheduler](scheduler).
- **SQL Linting** -- SQLFluff integration with DuckDB dialect support.
- **Environments** -- Multi-environment support (dev/staging/prod) with per-environment database paths. See [Environments](environments).
- **CI/CD** -- Generate GitHub Actions workflows, post diff comments to PRs. See [CLI Reference](cli-reference).
- **Circuit Breakers** -- Prevent cascading failures in pipeline execution.
- **Query Profiling** -- EXPLAIN PLAN and slow query logging for performance tuning. See [Quality](quality).

## Quick Start

```bash
pip install havn
havn init my-project
cd my-project
havn jobs run full-refresh
havn serve
```

## Documentation

| Category | Pages |
|----------|-------|
| Getting Started | [Getting Started](getting-started), [Configuration](configuration), [Environments](environments) |
| Core Concepts | [Transforms](transforms), [Pipelines](pipelines), [Seeds](seeds), [Sources](sources), [Migration: Streams to Jobs](streams-to-jobs) |
| Data Integration | [Connectors](connectors), [CDC](cdc) |
| Data Quality | [Quality](quality), [Contracts](contracts), [Sentinel](sentinel), [Lineage](lineage) |
| Security | [Auth](auth), [Masking](masking) |
| Advanced | [Scheduler](scheduler), [Notebooks](notebooks), [Versioning](versioning), [Orchestration Jobs](orchestration-jobs), [Pull Requests](pull-requests) |
| Reference | [CLI Reference](cli-reference), [API Reference](api-reference) |
