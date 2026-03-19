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

All metadata (model state, run logs, users, tokens) is stored in a hidden `_dp_internal` schema within the same DuckDB file.

## Web UI

The havn web interface is a React SPA (Single Page Application) built with Vite and Monaco editor, serving as the primary way to interact with the platform. It's organized into five main sections:

### Overview
- Project health dashboard with recent runs and pipeline status
- Quick actions for triggering transforms, ingest, and export steps
- Run history summary with success/failure counts and execution times

### Develop
- **Editor** -- Monaco code editor with SQL syntax highlighting, formatting, and validation for transforms and scripts
- **Notebooks** -- Interactive `.dpnb` notebook editor with code, SQL, and markdown cells for exploratory analysis
- **DAG** -- Visual dependency graph showing all models and their relationships; supports Rewind to restore tables from snapshots

### Explore
- **Query** -- Interactive SQL runner with result pagination, download, and query history
- **Tables** -- Warehouse browser showing all schemas, tables, and column metadata with profiling stats
- **Data Sources** -- Connector management and monitoring of external data sources with freshness indicators

### Observe
- **Quality** -- Data quality dashboard with freshness status, column profiles, assertion results, and contract violations
- **Sentinel** -- Schema change detection alerting on unexpected column additions, removals, or type changes
- **Diff** -- Preview and compare changes before transforms are applied
- **History** -- Complete run log with execution times, error messages, and lineage visualization

### Configure
- **Masking** -- Column-level data masking policies with role-based exemptions (hash, redact, null, partial)
- **Wiki** -- Documentation editor for project-specific knowledge base
- **Docs** -- Auto-generated documentation from SQL comments and metadata
- **Settings** -- Theme preferences, scheduler configuration, secrets management, user/role management, alerts, and resource limits

## Key Features

### Data Pipeline
- **SQL Transforms** -- Plain SQL with `-- config:` and `-- depends_on:` comments; no Jinja or templating. See [Transforms](transforms).
- **DAG Engine** -- Automatic dependency resolution and topological ordering with change detection via SHA256 hashing.
- **Streams** -- Multi-step pipelines (ingest, transform, export) defined in `project.yml`. See [Pipelines](pipelines).
- **Seeds** -- CSV files loaded as reference tables with change detection. See [Seeds](seeds).

### Connectors and Integration
- **Data Connectors** -- Pre-built connectors for PostgreSQL, MySQL, Stripe, Shopify, HubSpot, Google Sheets, REST APIs, S3/GCS, CSV files, and webhooks. See [Connectors](connectors).
- **CDC** -- Change Data Capture with high-watermark tracking, file modification tracking, and full-refresh modes. See [CDC](cdc).
- **Sources** -- Declared external source metadata with freshness SLAs. See [Sources](sources).

### Data Quality
- **Inline Assertions** -- `-- assert:` comments in SQL files for row_count, no_nulls, unique, and custom expressions. See [Quality](quality).
- **YAML Contracts** -- Standalone data quality rules in `contracts/` with severity levels and history tracking. See [Contracts](contracts).
- **Profiling** -- Automatic column-level statistics: null percentages, distinct counts, min/max values.
- **Freshness Monitoring** -- Detect stale models and sources against configured SLAs.

### Security
- **Authentication** -- Token-based auth with RBAC roles: admin, editor, viewer. See [Auth](auth).
- **Data Masking** -- Column-level masking policies (hash, redact, null, partial) with role exemptions. See [Masking](masking).

### Development Tools
- **Column-Level Lineage** -- AST-based SQL analysis via sqlglot tracing columns through CTEs, subqueries, and joins. See [Lineage](lineage).
- **Notebooks** -- Interactive `.dpnb` notebooks with code, SQL, and markdown cells. See [Notebooks](notebooks).
- **Versioning** -- Parquet-based snapshots with time travel, diff, and restore. See [Versioning](versioning).
- **Rewind** -- Time travel and restore tables to previous snapshots from the DAG view. See [Versioning](versioning).
- **Agent Sidebar** -- AI coding assistant embedded in the UI for code generation, debugging, and optimization.

### Data Quality and Monitoring
- **Schema Sentinel** -- Automated schema change detection with alerts on unexpected column changes. See [Sentinel](sentinel).

### Operations
- **Scheduler** -- Cron-based scheduling with file watcher for auto-rebuild. See [Scheduler](scheduler).
- **SQL Linting** -- SQLFluff integration with DuckDB dialect support.
- **Environments** -- Multi-environment support with variable expansion from `.env`. See [Environments](environments).

## Quick Start

```bash
pip install -e .
havn init my-project
cd my-project
havn stream full-refresh
havn serve
```

## Documentation

| Category | Pages |
|----------|-------|
| Getting Started | [Getting Started](getting-started), [Configuration](configuration), [Environments](environments) |
| Core Concepts | [Transforms](transforms), [Pipelines](pipelines), [Seeds](seeds), [Sources](sources) |
| Data Integration | [Connectors](connectors), [CDC](cdc) |
| Data Quality | [Quality](quality), [Contracts](contracts), [Sentinel](sentinel), [Lineage](lineage) |
| Security | [Auth](auth), [Masking](masking) |
| Advanced | [Scheduler](scheduler), [Notebooks](notebooks), [Versioning](versioning) |
| Reference | [CLI Reference](cli-reference), [API Reference](api-reference) |
