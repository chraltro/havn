# CLI Reference

Complete reference for all `havn` CLI commands. Run `havn --help` or `havn <command> --help` for built-in help.

## Project Management

### havn init

Scaffold a new data platform project.

```bash
havn init [NAME] [--dir PATH]
```

| Argument/Flag | Default | Description |
|---------------|---------|-------------|
| `NAME` | `my-project` | Project name |
| `--dir, -d` | `./<NAME>` | Target directory |

Creates project structure with sample earthquake data pipeline, seeds, contracts, and notebooks.

### havn validate

Validate project structure, config, and SQL model dependencies.

```bash
havn validate [--project PATH]
```

Checks `project.yml` parsing, directory structure, stream actions, model dependencies, circular dependencies, and environment variable references.

### havn status

Show project health: git info, warehouse stats, last run.

```bash
havn status [--project PATH]
```

### havn context

Generate a project summary to paste into any AI assistant.

```bash
havn context [--project PATH]
```

Outputs a comprehensive markdown summary of the project including configuration, models, scripts, warehouse tables, and recent history.

### havn checkpoint

Smart git commit: stages files, auto-generates commit message.

```bash
havn checkpoint [--message TEXT] [--project PATH]
```

Automatically stages all files except `.env`, generates a descriptive commit message from changed file paths, and commits.

### havn backup

Create a verified backup of the warehouse database with SHA-256 checksum.

```bash
havn backup [--output PATH] [--no-verify] [--note TEXT] [--keep N] [--project PATH]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--output, -o` | `_backups/` | Output path |
| `--no-verify` | false | Skip post-backup integrity check |
| `--note` | none | Attach a note to the backup manifest entry |
| `--keep` | none | Retention: keep only the last N backups, remove older ones |

Flushes the DuckDB WAL, copies the database file, computes a SHA-256 checksum, and tracks the backup in `_backups/manifest.json`.

### havn backup-list

List all tracked backups from the manifest.

```bash
havn backup-list [--project PATH]
```

### havn backup-verify

Verify the integrity of a backup file against its stored checksum.

```bash
havn backup-verify BACKUP_PATH [--project PATH]
```

### havn backup-restore

Restore the warehouse database from a backup.

```bash
havn backup-restore BACKUP_PATH [--project PATH]
```

## Pipeline Execution

### havn run

Run a single ingest or export script.

```bash
havn run SCRIPT [--project PATH]
```

Examples:
```bash
havn run ingest/customers.py
havn run ingest/earthquakes.dpnb
havn run export/daily_report.py
```

### havn seed

Load CSV files from seeds/ directory.

```bash
havn seed [--force] [--schema NAME] [--env NAME] [--project PATH]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--force, -f` | false | Reload all seeds (ignore change detection) |
| `--schema, -s` | `seeds` | Target schema |
| `--env, -e` | none | Environment override |

### havn transform

Build SQL models in dependency order.

```bash
havn transform [TARGETS...] [--force] [--sequential] [--workers N] [--env NAME] [--skip-check] [--project PATH]
```

| Flag | Default | Description |
|------|---------|-------------|
| `TARGETS` | all | Specific models to build |
| `--force, -f` | false | Rebuild all (ignore change detection) |
| `--sequential` | false | Disable parallel execution; run models one at a time (independent models run concurrently by default) |
| `--workers, -w` | 4 | Max parallel workers |
| `--env, -e` | none | Environment override |
| `--skip-check` | false | Skip pre-transform validation |

### havn jobs run

Run an orchestration job from project.yml.

```bash
havn jobs run NAME [--force] [--env NAME] [--project PATH]
```

### havn lint

Lint SQL files with SQLFluff.

```bash
havn lint [--fix] [--project PATH]
```

| Flag | Description |
|------|-------------|
| `--fix` | Auto-fix violations |

## Querying and Inspection

### havn query

Run an ad-hoc SQL query.

```bash
havn query "SQL" [--csv] [--json] [--limit N] [--env NAME] [--project PATH]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--csv` | false | Output as CSV |
| `--json` | false | Output as JSON |
| `--limit, -n` | 0 (all) | Max rows to return |
| `--env, -e` | none | Environment override |

### havn tables

List tables and views in the warehouse.

```bash
havn tables [SCHEMA] [--env NAME] [--project PATH]
```

### havn history

Show recent run history.

```bash
havn history [--limit N] [--project PATH]
```

## Semantic Layer

Metrics are defined as YAML in the project's `metrics/` directory. See [Semantic Layer](semantic-layer) for the definition format.

### havn metrics

List the metrics defined in `metrics/*.yml`.

```bash
havn metrics [--project PATH]
havn metrics list [--project PATH]
```

### havn metrics query

Compile a metric to SQL and run it against the warehouse.

```bash
havn metrics query NAME [--by DIM] [--grain GRAIN] [--start DATE] [--end DATE] [--limit N] [--csv] [--json] [--env NAME] [--project PATH]
```

| Flag | Default | Description |
|------|---------|-------------|
| `NAME` | required | Metric name |
| `--by` | none | Dimension(s) to group by (repeatable) |
| `--grain` | none | Time grain: `hour`, `day`, `week`, `month`, `quarter`, `year` |
| `--start` / `--end` | none | Inclusive lower / exclusive upper bound on the time dimension |
| `--limit, -n` | none | Max rows to return |
| `--csv` / `--json` | false | Output format |
| `--env, -e` | none | Environment override |

```bash
havn metrics query revenue --by region --grain month
```

### havn metrics sql

Print the SQL a metric query compiles to, without executing it.

```bash
havn metrics sql NAME [--by DIM] [--grain GRAIN] [--start DATE] [--end DATE] [--limit N] [--project PATH]
```

## AI Agents

### havn mcp

Start an MCP stdio server exposing the warehouse to AI agents.

```bash
havn mcp [--read-only] [--env NAME] [--project PATH]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--read-only` | false | Disable the `run_transform` tool |
| `--env, -e` | none | Environment override |

Register it with an MCP client, for example:

```bash
claude mcp add havn -- havn mcp -p /path/to/project
```

Tools exposed: `query`, `list_tables`, `describe_table`, `list_models`, `get_model`, `model_lineage`, `run_history`, `list_metrics`, `query_metric`, `run_transform`.

## Data Quality

### havn check

Validate SQL models, run assertions, and run contracts.

```bash
havn check [TARGETS...] [--env NAME] [--project PATH]
```

Runs model validation, inline assertions, and YAML contracts.

### havn freshness

Check model and source freshness.

```bash
havn freshness [--hours N] [--alert] [--sources] [--env NAME] [--project PATH]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--hours, -h` | 24.0 | Max age before a model is stale |
| `--alert` | false | Send alerts for stale models |
| `--sources` | false | Check source freshness from sources.yml |

### havn profile

Show model profile statistics.

```bash
havn profile [MODEL] [--project PATH]
```

Without a model name, shows summary for all models. With a model name, shows detailed column statistics.

### havn assertions

Show recent assertion results.

```bash
havn assertions [--project PATH]
```

### havn contracts

Run data contracts from the contracts/ directory.

```bash
havn contracts [TARGETS...] [--history] [--project PATH]
```

| Flag | Description |
|------|-------------|
| `TARGETS` | Contract names or model names to run |
| `--history` | Show contract history instead of running |

## Model Analysis

### havn lineage

Show column-level lineage for a model.

```bash
havn lineage MODEL [--json] [--project PATH]
```

### havn impact

Analyze downstream impact of changing a model or column.

```bash
havn impact MODEL [--column NAME] [--json] [--project PATH]
```

### havn promote

Promote SQL to a transform model file.

```bash
havn promote SQL_SOURCE [--name NAME] [--schema NAME] [--desc TEXT] [--file PATH] [--overwrite] [--project PATH]
```

### havn debug

Generate a debug notebook for a failed model.

```bash
havn debug MODEL [--project PATH]
```

Creates a `.dpnb` notebook pre-populated with error info, upstream queries, and the failing SQL.

## Diff and Versioning

### havn diff

Compare model SQL output against materialized tables. Three modes:

```bash
# Single model: diff one specific table
havn diff gold.orders

# Changed + downstream (default): only diff models with SQL changes
havn diff

# Full database: diff everything
havn diff --full
```

```bash
havn diff [TARGETS...] [--target SCHEMA] [--format FMT] [--rows] [--full] [--against REF] [--snapshot NAME] [--exit-nonzero-on-change] [--project PATH]
```

| Flag | Default | Description |
|------|---------|-------------|
| `TARGETS` | none | Specific models to diff (single mode) |
| `--target, -t` | none | Diff all models in a schema |
| `--format, -f` | `table` | Output format: `table` or `json` |
| `--rows` | false | Include sample rows |
| `--full` | false | Show all changed rows, not just samples |
| `--against` | none | Git-aware: only diff models changed vs a branch/ref |
| `--snapshot` | none | Compare against a named snapshot |
| `--exit-nonzero-on-change` | false | Exit with code 2 if any model has added/removed/modified rows or schema changes (for CI) |

When no targets are given, diff uses change detection to only diff models whose SQL (or upstream) actually changed, plus their downstream dependents. This is much faster than diffing the entire database.

## Connectors

### havn connect

Set up a data connector.

```bash
havn connect TYPE [--name NAME] [--tables LIST] [--schema NAME] [--schedule CRON] [--test] [--discover] [--config JSON] [--set KEY=VALUE] [--host H] [--port P] [--database D] [--user U] [--password P] [--url U] [--api-key K] [--token T] [--path P] [--project PATH]
```

Use `havn connect list` to show available connector types.

### havn connectors list

List configured connectors.

```bash
havn connectors list [--project PATH]
```

### havn connectors test

Test a configured connector.

```bash
havn connectors test CONNECTION_NAME [--project PATH]
```

### havn connectors sync

Run sync for a connector.

```bash
havn connectors sync CONNECTION_NAME [--project PATH]
```

### havn connectors regenerate

Regenerate the ingest script for a connector.

```bash
havn connectors regenerate CONNECTION_NAME [--project PATH]
```

### havn connectors remove

Remove a connector (script and config).

```bash
havn connectors remove CONNECTION_NAME [--project PATH]
```

### havn connectors available

List all available connector types.

```bash
havn connectors available
```

## CDC

### havn cdc

View and manage CDC state.

```bash
havn cdc ACTION [--connector NAME] [--table NAME] [--project PATH]
```

Actions:
- `status` -- Show CDC state for all connectors
- `reset` -- Reset watermarks (requires `--connector`)

## Scheduling

### havn schedule

Start the cron scheduler.

```bash
havn schedule [--project PATH]
```

### havn watch

Watch for file changes and auto-rebuild.

```bash
havn watch [--project PATH]
```

## Masking

### havn mask list

List all masking policies.

```bash
havn mask list [--project PATH]
```

### havn mask add

Add a masking policy. Supports all 14 methods (`hash`, `redact`, `null`,
`partial`, `truncate`, `email`, `phone`, `first_initial`, `ip_address`,
`credit_card`, `range`, `noise`, `date_shift`, `consistent_hash`).

```bash
havn mask add --schema S --table T --column C --method M [--show-first N] [--show-last N] [--project PATH]
```

### havn mask remove

Remove a masking policy by ID.

```bash
havn mask remove --id POLICY_ID [--project PATH]
```

## Server

### havn serve

Start the web UI server.

```bash
havn serve [--port PORT] [--host HOST] [--auth] [--env NAME] [--project PATH]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | 3000 | Server port |
| `--host` | 127.0.0.1 | Server host |
| `--auth` | false | Enable authentication |
| `--env` | none | Environment to use |

## Version

### havn env

Manage active environment.

```bash
havn env ACTION [NAME]
```

Actions:
- `list` — Show all environments, mark active with star
- `use <name>` — Set active environment (writes `.havn-env`)
- `show` — Show current active environment
- `reset` — Clear active environment

### havn macros

List registered Python SQL macros.

```bash
havn macros [--project PATH]
```

Shows all macros discovered from the `macros/` directory: name, parameters, return type, source file, and docstring.

### havn version

Manage warehouse versions with Parquet-based time travel. `havn version` (or `havn version list`) lists tracked versions; other actions create, diff, restore, show a table timeline, or clean up old versions.

```bash
havn version list                          # list tracked versions (default action)
havn version create --desc "Before migration"
havn version diff --from run-5 --id run-8
havn version restore --id run-5
havn version timeline --table gold.customers
havn version cleanup --keep 5
```

To print the installed havn package version instead, use `havn --version` (or `havn -V`).

## Interactive

### havn shell

Open an interactive SQL REPL connected to the warehouse. Supports multi-line
queries, history, and tab completion.

```bash
havn shell [--env NAME] [--project PATH]
```

### havn explain

Print a model's query plan with operator timings.

```bash
havn explain MODEL [--analyze] [--project PATH]
```

`--analyze` runs the model's query with `EXPLAIN ANALYZE` and reports
operator-level wall-clock times.

## Pipeline Rewind

### havn rewind

Inspect rewind metadata.

```bash
havn rewind runs [--limit N] [--project PATH]
havn rewind snapshot --run RUN_ID [--limit N] [--project PATH]
havn rewind sample --run RUN_ID --model NAME [--limit N] [--project PATH]
havn rewind gc [--project PATH]
```

## Schema Sentinel

### havn sentinel

Detect upstream schema drift and analyze impact.

```bash
havn sentinel check [--source NAME] [--project PATH]
havn sentinel diffs [--limit N] [--project PATH]
havn sentinel impacts --diff DIFF_ID [--limit N] [--project PATH]
havn sentinel history --source NAME [--limit N] [--project PATH]
```

## Pull Requests (GitHub)

### havn pr

Create and review havn pipeline changes via GitHub pull requests.

```bash
havn pr open                    # open the current branch as a PR
havn pr review NUMBER           # local data diff for a PR
```

## Arrow Flight SQL

### havn flight

Start an Arrow Flight SQL server that exposes the warehouse to Flight-aware
clients (BI tools, Python `pyarrow.flight`, etc.).

```bash
havn flight start [--host HOST] [--port PORT] [--project PATH]
```

## Streaming

### havn streaming

Drive long-lived streaming sources from the CLI.

```bash
havn streaming start [--project PATH]
havn streaming stop [--project PATH]
havn streaming status [--project PATH]
havn streaming poll-once SOURCE [--project PATH]
```

## Migration

### havn migrate

Convert between backends (DuckDB to DuckLake or vice versa). Reads the
current backend, writes to the target.

```bash
havn migrate --to ducklake [--project PATH]
```
