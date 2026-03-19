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

Create a backup of the warehouse database.

```bash
havn backup [--output PATH] [--project PATH]
```

Flushes the DuckDB WAL and copies the database file with a timestamped name.

### havn restore

Restore the warehouse database from a backup.

```bash
havn restore BACKUP_PATH [--project PATH]
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
| `TARGETS` | all | Specific models to build (e.g., `gold.summary`) |
| `--force, -f` | false | Rebuild all (ignore change detection) |
| `--sequential` | false | Disable parallel execution; run models one at a time |
| `--workers, -w` | 4 | Max parallel workers |
| `--env, -e` | none | Environment override |
| `--skip-check` | false | Skip pre-transform validation |

### havn stream

Run a full stream from project.yml.

```bash
havn stream NAME [--force] [--env NAME] [--project PATH]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--force, -f` | false | Force rebuild all transforms |
| `--env, -e` | none | Environment override |

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

## Data Quality

### havn check

Validate SQL models, run assertions, and run contracts.

```bash
havn check [TARGETS...] [--env NAME] [--project PATH]
```

Runs model validation, inline assertions, and YAML contracts. Exit code 1 on any failure.

### havn freshness

Check model and source freshness.

```bash
havn freshness [--hours N] [--alert] [--sources] [--env NAME] [--project PATH]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--hours, -h` | 24.0 | Max age before a model is stale |
| `--alert` | false | Send alerts for stale models |
| `--sources` | false | Check source freshness from project.yml |
| `--env, -e` | none | Environment override |

### havn profile

Show model profile statistics.

```bash
havn profile [MODEL] [--env NAME] [--project PATH]
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
havn contracts [TARGETS...] [--history] [--env NAME] [--project PATH]
```

| Flag | Description |
|------|-------------|
| `TARGETS` | Contract names or model names to run |
| `--history` | Show contract history instead of running |
| `--env, -e` | Environment override |

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

| Flag | Description |
|------|-------------|
| `SQL_SOURCE` | SQL string, .sql file path, or .dpnb notebook path |
| `--name, -n` | Model name |
| `--schema, -s` | Target schema |
| `--desc, -d` | Model description |
| `--file, -f` | Output file path (auto-generated if omitted) |
| `--overwrite` | Overwrite existing file |

### havn debug

Generate a debug notebook for a failed model.

```bash
havn debug MODEL [--project PATH]
```

Creates a `.dpnb` notebook pre-populated with error info, upstream queries, and the failing SQL.

## Diff and Versioning

### havn diff

Compare model SQL output against materialized tables.

```bash
havn diff [TARGETS...] [--target SCHEMA] [--format FMT] [--rows] [--full] [--against REF] [--snapshot NAME] [--project PATH]
```

| Flag | Default | Description |
|------|---------|-------------|
| `TARGETS` | all | Models to diff |
| `--target, -t` | none | Diff all models in a schema |
| `--format, -f` | `table` | Output format: `table` or `json` |
| `--rows` | false | Include sample rows |
| `--full` | false | Show all changed rows |
| `--against` | none | Git-aware: only diff models changed vs a branch |
| `--snapshot` | none | Compare against a named snapshot |

### havn snapshot

Create and manage project snapshots.

```bash
havn snapshot create [--name NAME] [--project PATH]
havn snapshot list [--project PATH]
havn snapshot delete SNAPSHOT_ID [--project PATH]
```

### havn version

Manage warehouse versions (Parquet-based snapshots).

```bash
havn version <ACTION> [OPTIONS]
```

Actions:
- `create [--desc TEXT]` -- Create a named version with Parquet snapshots
- `list` -- Show all versions
- `diff [--id VERSION_ID] [--from FROM_ID]` -- Compare versions
- `restore --id VERSION_ID` -- Restore from version
- `timeline --table TABLE_NAME` -- Show table's version history
- `cleanup [--keep N]` -- Remove old versions keeping last N

## Pipeline Rewind

### havn rewind

Manage pipeline run snapshots and time travel.

```bash
havn rewind <ACTION> [OPTIONS]
```

Actions:
- `runs [--limit N] [--env NAME]` -- List recent pipeline runs
- `snapshot --run RUN_ID [--env NAME]` -- View snapshots for a run
- `sample --run RUN_ID --model MODEL [--env NAME]` -- Preview snapshot data
- `gc [--env NAME]` -- Run garbage collection on expired snapshots

### havn restore (model)

Restore a model from a pipeline snapshot.

```bash
havn restore MODEL --run RUN_ID [--cascade] [--no-cascade] [--env NAME] [--project PATH]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--run, -r` | required | Run ID to restore from |
| `--cascade` | true | Re-build downstream models after restore |
| `--no-cascade` | false | Only restore the specified model |

## Schema Sentinel

### havn sentinel

Monitor and manage schema changes.

```bash
havn sentinel <ACTION> [OPTIONS]
```

Actions:
- `check [--env NAME]` -- Detect upstream schema changes
- `diffs [--limit N] [--env NAME]` -- Show recent schema diffs
- `impacts --diff DIFF_ID [--env NAME]` -- Analyze impact for a diff
- `history --source SOURCE_NAME [--limit N] [--env NAME]` -- Show schema history for a source

## Connectors

### havn connect

Set up a data connector.

```bash
havn connect TYPE [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `--name, -n` | Connection name (default: auto-generated) |
| `--tables, -t` | Comma-separated tables to sync |
| `--schema, -s` | Target schema (default: landing) |
| `--schedule` | Cron schedule for automatic sync |
| `--test` | Only test the connection |
| `--discover` | Only list available resources |
| `--config, -c` | JSON string or file path with params |
| `--set key=value` | Set individual parameters (repeatable) |
| `--host`, `--port`, `--database`, `--user`, `--password` | Common connection params |
| `--url`, `--api-key`, `--token`, `--path` | Additional connection params |

Available types: `postgres`, `mysql`, `csv`, `stripe`, `shopify`, `hubspot`, `google_sheets`, `rest_api`, `s3_gcs`, `webhook`

### havn connectors

Manage configured connectors.

```bash
havn connectors list [--project PATH]
havn connectors test CONNECTION_NAME [--project PATH]
havn connectors sync CONNECTION_NAME [--project PATH]
havn connectors regenerate CONNECTION_NAME [--project PATH]
havn connectors remove CONNECTION_NAME [--project PATH]
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

## Data Masking

### havn mask

Manage data masking policies.

```bash
havn mask add --schema S --table T --column C --method M [--exempt ROLES]
havn mask list
havn mask remove POLICY_ID
```

Methods: `hash`, `redact`, `null`, `partial`, `email`, `phone`, `credit_card`, `first_initial`, `ip_address`, `range`, `noise`, `date_shift`, `truncate`, `consistent_hash`

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

## Secrets Management

### havn secrets

Manage `.env` secrets.

```bash
havn secrets list [--project PATH]
havn secrets set KEY VALUE [--project PATH]
havn secrets delete KEY [--project PATH]
```

## User Management

### havn users

Manage platform users (requires auth enabled).

```bash
havn users create --username NAME --password PASS --role ROLE [--project PATH]
havn users list [--project PATH]
havn users delete USERNAME [--project PATH]
```

Roles: `admin`, `editor`, `viewer`

## CI/CD Integration

### havn ci

Generate CI/CD configuration.

```bash
havn ci generate [--project PATH]    # Generate GitHub Actions workflow
havn ci diff-comment [--project PATH] # Post formatted diff to PR
```

## Server

### havn serve

Start the web UI server.

```bash
havn serve [--port PORT] [--host HOST] [--auth] [--watch] [--schedule] [--env NAME] [--project PATH]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | 3000 | Server port |
| `--host` | 127.0.0.1 | Server host |
| `--auth` | false | Enable authentication |
| `--watch, -w` | false | Enable file watcher for auto-rebuild |
| `--schedule, -s` | false | Enable cron scheduler |
| `--env` | none | Environment to use |

## Version

### havn version

Show havn version.

```bash
havn version
```
