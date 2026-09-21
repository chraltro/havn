# Getting Started

This guide walks you through installing havn, creating your first project, running a pipeline, and exploring data in the web UI.

## Prerequisites

- Python 3.10 or later
- pip (Python package manager)

## Installation

### Install from PyPI

```bash
pip install havn
```

The web UI frontend is bundled into the package — no separate build step needed.

### Install from Source (development)

```bash
git clone https://github.com/chraltro/havn
cd havn
pip install -e ".[dev]"
cd frontend && npm install && npm run build
```

### Docker

```bash
docker run -v $(pwd):/project -p 3000:3000 ghcr.io/chraltro/havn serve
```

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

## Explore Your Data

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

## Start the Web UI

```bash
havn serve
```

This starts the web server on `http://localhost:3000` with:

- **File Browser** -- Edit SQL and Python files with Monaco editor
- **Query Panel** -- Run ad-hoc SQL queries with autocomplete and `$name` parameters
- **Table Browser** -- Browse schemas, tables, and column profiles
- **DAG Viewer** -- Interactive dependency graph visualization
- **Notebook Runner** -- Execute `.dpnb` notebooks interactively
- **Pipeline Controls** -- Run streams and view history

### Editor

Open a `.sql` file under `transform/` and the editor works against the
warehouse catalog rather than just colouring text.

- **Live error markers.** As you type, the model is bound against its upstream
  tables and the errors are underlined in place: a misspelled column, a table
  that does not exist, a syntax error. Lint warnings from SQLFluff appear the
  same way, on save and after a short pause in typing. The count sits next to
  the filename above the editor.
- **Hover types.** Hover a column and the inferred type appears at the top of
  the tooltip, for the model's own output columns and for `alias.column`
  references into upstream models. Hovering a table still lists its columns,
  and hovering a macro still shows its signature.
- **Go to definition.** `Ctrl+Click` (`Cmd+Click` on macOS) or `F12` on a
  `schema.model` reference opens the file that declares it.
- **Preview a single CTE.** A `Preview` link appears above each CTE in a `WITH`
  chain. Click it, or press `Ctrl+Shift+Enter` (`Cmd+Shift+Enter`) with the
  cursor inside a CTE, to run just that step and see the rows below the editor.

| Shortcut | Action |
|---|---|
| `Ctrl/Cmd+Enter` | Preview the whole model |
| `Ctrl/Cmd+Shift+Enter` | Preview the CTE at the cursor |
| `Ctrl/Cmd+Shift+F` | Format the file (`havn lint --fix`) |
| `F12` or `Ctrl/Cmd+Click` | Go to the definition of a model reference |

Previews are capped at 100 rows. Renaming a column across downstream models is
not available yet; see [Limitations](limitations).

### With Authentication

```bash
havn serve --auth
```

On first launch with `--auth`, you will be prompted to create an admin user through the web UI. See [Auth](auth) for details.

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
