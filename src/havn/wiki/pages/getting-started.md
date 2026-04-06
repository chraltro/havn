# Getting Started

This guide walks you through installing havn, creating your first project, running a pipeline, and exploring data in the web UI.

## Prerequisites

- Python 3.10 or later
- Node.js 18+ (for building the frontend)
- pip (Python package manager)

## Installation

### Install from Package

The easiest way to get started is to install havn from PyPI:

```bash
pip install havn
```

### Install from Source

To develop havn or run the latest version from the repository:

```bash
git clone <repo-url>
cd db
pip install -e .
```

### Install with Development Dependencies

If you plan to run tests or contribute to the project:

```bash
pip install -e ".[dev]"
```

This adds pytest and httpx for running the test suite.

### Build the Frontend (Source Only)

If installing from source, build the web UI (a React SPA built with Vite):

```bash
cd frontend
npm install
npm run build
```

For frontend development, use the dev server instead:

```bash
cd frontend
npm run dev
```

This starts a dev server on port 5173 that proxies API requests to port 3000.

## Create a New Project

Scaffold a new project with `havn init`:

```bash
havn init my-project
cd my-project
```

This creates the following structure:

```
my-project/
  ingest/               # Python scripts and .dpnb notebooks for data ingestion
    earthquakes.dpnb    # Sample ingest notebook (USGS earthquake data)
  transform/
    bronze/             # Light cleanup SQL
      earthquakes.sql
    silver/             # Business logic SQL
      earthquake_events.sql
      earthquake_daily.sql
    gold/               # Consumption-ready SQL
      earthquake_summary.sql
      top_earthquakes.sql
      region_risk.sql
  export/               # Python scripts for exporting data
    earthquake_report.py
  seeds/                # CSV reference data
    magnitude_scale.csv
  contracts/            # YAML data quality contracts
    quality.yml
  notebooks/            # Interactive .dpnb notebooks
    explore.dpnb
  project.yml           # Project configuration
  .env                  # Secrets (never commit this)
  .gitignore
  warehouse.duckdb      # Created after first pipeline run
```

## Run Your First Pipeline

The scaffolded project includes a complete earthquake data pipeline. Run it:

```bash
havn jobs run full-refresh
```

This executes the pipeline steps defined in `project.yml`:

1. **Ingest** -- Fetches earthquake data from the USGS API (falls back to sample data offline)
2. **Seed** -- Loads `seeds/magnitude_scale.csv` as a reference table
3. **Transform** -- Builds SQL models in dependency order: `bronze` -> `silver` -> `gold`
4. **Export** -- Generates a summary report

## Start the Web UI

```bash
havn serve
```

This starts the web server on `http://localhost:3000`. Open it in your browser to see the havn interface.

### With Authentication

```bash
havn serve --auth
```

On first launch with `--auth`, you will be prompted to create an admin user through the web UI. See [Auth](auth) for details.

## Using the Web UI

The web UI is organized into four main sections, accessible from the top navigation tabs:

### Overview Tab

When you first open localhost:3000, you'll see the Overview tab. It displays:

- **Stats Row** -- Key metrics including total tables, rows, connectors, and recent pipeline health (success/failure ratio)
- **Pipeline Health** -- Recent pipeline runs with their status, affected table/file, row counts, and duration
- **Warehouse Summary** -- Schemas in your warehouse grouped by name, with table/view counts and total rows per schema
- **Quick Actions** -- Fast navigation buttons to common tasks: add data sources, run queries, edit transforms, and view the DAG
- **Failed Runs Detail** -- If any recent runs failed, click the "Runs OK" stat card to expand a list of failures with error messages

The Overview is your control center for monitoring warehouse health at a glance.

### Develop Tab

The Develop tab contains the code editor and DAG viewer for building your data warehouse.

**Editor** -- The file tree on the left shows your project structure. Click any `.sql` or `.py` file to open it in the Monaco editor with syntax highlighting and autocomplete. Changes are automatically saved. Use this to write and edit SQL transforms and Python scripts.

**DAG** -- Click the DAG section to see an interactive dependency graph of all your SQL models. Hover over nodes to see upstream and downstream dependencies. This helps you understand data lineage and debug circular dependencies.

### Explore Tab

The Explore tab has two sub-sections for querying and browsing your data.

**Query** -- Run ad-hoc SQL queries against any table in your warehouse. Write SQL in the editor, press Ctrl+Enter (or Cmd+Enter on Mac), and see results in the table below. Results can be exported as CSV or JSON. Autocomplete suggests table names and columns as you type.

**Tables** -- Browse all tables and views in your warehouse organized by schema. Click on any table to see column details, data types, and a preview of the first rows. This is useful for exploring data without writing SQL, and for understanding the structure of bronze/silver/gold models.

### Observe Tab

The Observe tab shows pipeline execution history and logs.

**History** -- View all recent pipeline runs sorted by timestamp. Each run shows:
- Run type (ingest, seed, transform, export)
- Affected target (table name or file)
- Status (success or failure with error message)
- Duration and rows affected
- Timestamp

Click on a failed run to see the error details. This is your main tool for debugging pipeline issues.

### Configure Tab

The Configure tab is for data source management and project settings.

**Data Sources** -- Connect external data sources (databases, APIs, file uploads) to ingest data into your warehouse. This wizard guides you through importing CSVs, connecting to Postgres, setting up recurring API connectors, or loading Parquet files.

## Explore Your Data (CLI)

You can also explore data from the command line:

### List Tables

```bash
havn tables
```

This shows all tables and views in the warehouse, organized by schema.

### Run Queries

```bash
havn query "SELECT * FROM gold.earthquake_summary LIMIT 10"
```

Output options:

```bash
havn query "SELECT * FROM gold.top_earthquakes" --csv
havn query "SELECT * FROM gold.top_earthquakes" --json
havn query "SELECT COUNT(*) FROM landing.earthquakes" --limit 5
```

### Check Data Quality

```bash
havn contracts
```

This runs all YAML contracts from the `contracts/` directory and reports pass/fail results.

### View Run History

```bash
havn history
```

Shows recent pipeline runs with status, duration, and row counts.


## Project Configuration

The `project.yml` file is the central configuration. See [Configuration](configuration) for the full reference. Here is a minimal example:

```yaml
name: my-project
database:
  path: warehouse.duckdb
streams:
  full-refresh:
    description: "Full pipeline rebuild"
    steps:
      - seed: [all]
      - ingest: [all]
      - transform: [all]
      - export: [all]
```

## Next Steps

- [Transforms](transforms) -- Learn how to write SQL transform models
- [Pipelines](pipelines) -- Configure multi-step data pipelines
- [Connectors](connectors) -- Connect to external data sources
- [Quality](quality) -- Add data quality checks
- [CLI Reference](cli-reference) -- Full command reference
