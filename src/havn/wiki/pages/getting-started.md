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

Five destinations sit in the rail down the left edge (a bottom bar on a phone), with Agent and Settings pinned below them. `Alt+1` to `Alt+6` jump between them. The search box in the top bar (`Ctrl+K` / `Cmd+K`) finds any model, table, file or command. The pill beside it shows the active environment and turns red for production.

### Home

What you see at localhost:3000. It answers "is my data OK, and if not, what do I click?":

- **Health tiles** -- models up to date / changed / not built, checks passing, the last pipeline run, and warehouse size with the last backup. Each tile opens the matching page.
- **Needs attention** -- failed builds, failed checks, broken contracts, late sources and recent anomalies in one list, errors first. Each row has its next step: **See rows** runs the query for a failed check's violating rows, **Open** opens the model.
- **Runs · last 24h** -- one bar per pipeline run, height is duration, failed runs in red with a ✗.
- **Layers** -- every model under landing, bronze, silver and gold with its status: fresh, changed, not built, failing, or blocked by a failing upstream. Click one to open it.

The Observe item in the rail carries a badge with the number of errors in the list.

### Build

**Editor** -- open any file from the tree. A SQL model opens in the model workbench: lineage above the code, an inspector beside it (Preview, Checks, Columns, Runs), failed `@assert` lines marked in the editor, and **Build model** / **Build + downstream** below. Save with `Ctrl+S`; preview unsaved SQL with `Ctrl+Enter`.

**Orchestration** -- pipeline jobs and schedules. **Git** -- status, commits and branches, plus **Reviews** for creating a change from a branch.

### Data

**Query** -- ad-hoc SQL. Select part of the SQL to run only the selection. Named parameters like `$region` get a Parameters row and are bound server-side, so they cannot inject SQL. Results export as CSV.

**Tables** -- browse every table and view by schema, with column types and a preview. **DAG** -- the model dependency graph. **Dashboards** -- saved charts. **Data Sources** -- connect databases, APIs and files.

### Observe

**Quality** (checks, contracts, profiles, anomalies), **Unit Tests**, **Sentinel** (source schema drift), **Diff** (what a rebuild would change) and **Runs** (full run history with errors).

### Ship

Review a change before it merges. A change is a branch opened from Git → Reviews. Ship shows the models it changes and everything downstream of them, **Build** runs the branch in an isolated copy of the warehouse and shows which tables' data differ, and the gate on the right lists what merging needs: approval, no requested changes, no conflicts, a clean working tree, plus (recommended) a passing build of the latest commit. Merging snapshots the warehouse first; it changes code, not data, so run the pipeline afterwards.

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
