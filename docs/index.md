# havn -- Data in safe waters

havn is a self-hosted data platform -- a lightweight, Nordic alternative to Databricks and Snowflake. It uses **DuckDB** for OLAP analytics, **plain SQL** for transforms, and **Python** for ingest/export scripts. All data lives in a single `warehouse.duckdb` file. No data leaves your machine.

## Quick Start

```bash
pip install havn
havn init my-project
cd my-project
havn jobs run full-refresh
havn serve
```

Or with Docker:

```bash
docker run -v $(pwd):/project -p 3000:3000 ghcr.io/chraltro/havn serve
```

## Why havn?

| | havn | Databricks | dbt + Airflow | Snowflake |
|---|---|---|---|---|
| **Runs on** | Your machine | Cloud | Cloud / self-hosted | Cloud |
| **Data stays** | Local | Vendor cloud | Depends | Vendor cloud |
| **Setup time** | 1 minute | Hours | Hours | Hours |
| **Cost** | Free | $$$ | $ - $$$ | $$$ |
| **SQL dialect** | DuckDB (Postgres-compatible) | Spark SQL | Varies | Snowflake SQL |
| **Templating** | None -- plain SQL | Jinja | Jinja | None |

## Features

### Transform Engine
Plain SQL with `@config` directives. Dependencies are auto-extracted from `FROM` and `JOIN` clauses, so the DAG builds itself. SHA256 content hashing means only changed models rebuild. No Jinja, no templating, just SQL.

### Data Quality
Inline assertions (`@assert row_count > 0`, `@assert unique(id)`), YAML contracts, column profiling, and freshness monitoring.

### Connectors
Pre-built connectors for PostgreSQL, MySQL, Stripe, Shopify, Google Sheets, REST APIs, S3/GCS, CSV, and webhooks.

### Web UI
React SPA with Monaco code editor, interactive DAG visualization, table browser, query runner, and notebook support.

### AI-Native
Ships with `CLAUDE.md`, `.cursorrules`, and Copilot instructions. Run `havn context` to generate an AI-ready project summary.

### Single-File Database
Everything in one `warehouse.duckdb` file. Back up with `cp`. No servers, no clusters, no cloud accounts.

## Architecture

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

## Documentation

| Category | Pages |
|----------|-------|
| Getting Started | [Getting Started](getting-started.md), [Configuration](configuration.md), [Environments](environments.md) |
| Core Concepts | [Transforms](transforms.md), [Pipelines](pipelines.md), [Seeds](seeds.md), [Sources](sources.md) |
| Data Integration | [Connectors](connectors.md), [CDC](cdc.md) |
| Data Quality | [Quality](quality.md), [Contracts](contracts.md), [Lineage](lineage.md) |
| Security | [Auth](auth.md), [Masking](masking.md) |
| Advanced | [Scheduler](scheduler.md), [Notebooks](notebooks.md), [Versioning](versioning.md) |
| Reference | [CLI Reference](cli-reference.md), [API Reference](api-reference.md) |
