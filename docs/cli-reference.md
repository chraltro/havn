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
| `--force` | false | Scaffold into a non-empty directory, adding only missing files |

A target directory that already has files in it (other than `.git/`) is refused. With `--force`, only the scaffold files that do not exist yet are written; existing files, `.env` and `project.yml` included, are never overwritten.

Creates project structure with sample earthquake data pipeline, seeds, contracts, and notebooks.

### havn validate

Validate project structure, config, and SQL model dependencies.

```bash
havn validate [--project PATH] [--bind | --no-bind]
```

| Option | Default | Description |
|---|---|---|
| `--project, -p` | `.` | Project directory |
| `--bind / --no-bind` | on when a warehouse exists | Resolve every model's SQL through the DuckDB binder |

Checks `project.yml` parsing, directory structure, stream actions, model dependencies, circular dependencies, and environment variable references.

With the bind pass on, each model is also created as a view inside a throwaway
shadow catalog -- a private in-memory database with no attachment to the
warehouse and no file access -- and described, which resolves output types and
reports **bind errors** with a line number:

```
  error gold.summary:4: bind error: Referenced column "no_such_column" not found in FROM clause!
```

The pass reads no rows and writes nothing to the warehouse. It catches wrong
arity, unknown functions, operator overload failures, missing columns
(including on upstream models that have never been built), missing struct
keys, ambiguous references, aggregation without `GROUP BY`, and set-operation
arity mismatches. It does not catch value conversions such as
`CAST(some_varchar AS INTEGER)`, which bind clean and fail at run time. See
[Data Quality](quality.md#validation-and-type-resolution).

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
| `--keep` | none | Retention: keep only the last N backups (N >= 1), remove older ones |

Flushes the DuckDB WAL, copies the database file, computes a SHA-256 checksum, and tracks the backup in `_backups/manifest.json`.

### havn backup-list

List all tracked backups from the manifest.

```bash
havn backup-list [--project PATH]
```

Each file on disk is re-checked against its recorded SHA-256; one that has changed since it was taken shows `checksum mismatch` instead of `yes` under Verified.

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

The backup is verified first (including its manifest checksum), copied to a temporary file beside the warehouse and swapped in with an atomic rename, so a failure part-way leaves the original warehouse and its WAL untouched. A warehouse held open by another process (such as `havn serve`) is reported as such; stop that process and retry.

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
havn transform [TARGETS...] [--select SEL] [--exclude SEL] [--force] [--sequential] [--workers N] [--env NAME] [--skip-check] [--event-time-start TS] [--event-time-end TS] [--defer/--no-defer] [--defer-snapshot] [--verbose] [--project PATH]
```

| Flag | Default | Description |
|------|---------|-------------|
| `TARGETS` | all | Graph selectors picking what to build |
| `--select, -s` | none | Graph selector, repeatable; the same grammar as `TARGETS`, for people coming from dbt |
| `--exclude, -x` | none | Graph selector whose matches are removed from the selection |
| `--force, -f` | false | Rebuild all (ignore change detection) |
| `--sequential` | false | Disable parallel execution; run models one at a time (independent models run concurrently by default) |
| `--workers, -w` | 4 | Max parallel workers |
| `--env, -e` | none | Environment override |
| `--skip-check` | false | Skip pre-transform validation |
| `--event-time-start` | none | Backfill microbatch models from this event time (UTC), e.g. `2024-01-01` |
| `--event-time-end` | none | Backfill microbatch models up to this event time (UTC), exclusive |
| `--defer / --no-defer` | on when the environment declares `defer:` | Read models this warehouse has not built from the environment's defer target |
| `--defer-snapshot` | false | Defer to a consistent copy of the target, for when it is open for writing elsewhere |
| `--verbose, -v` | false | Print the resolved selection and which selector matched what |

```bash
havn transform                          # everything
havn transform gold.orders              # one model
havn transform +gold.orders             # and its upstream
havn transform gold.orders+             # and its downstream
havn transform 2+gold.orders            # two hops of upstream
havn transform @silver.customers        # it, its downstream, and their upstream
havn transform 'gold.fct_*'             # wildcard (quote it)
havn transform tag:daily                # by tag
havn transform path:transform/gold/     # by path
havn transform config.materialized:incremental
havn transform state:modified+          # what changed, plus downstream
havn transform 'tag:daily,gold.*'       # comma intersects
havn transform -s tag:daily -x tag:expensive

# backfill a microbatch model over an explicit event-time range
havn transform gold.events --event-time-start 2024-01-01 --event-time-end 2024-03-01

# build one model in dev, reading its upstreams from prod
havn transform gold.orders                     # defers if the environment says to
havn transform gold.orders --no-defer          # build against dev alone
havn transform gold.orders --defer-snapshot    # prod is busy; read a copy
```

`--defer` needs `environments.<name>.defer` in project.yml, and reads the
other environment's warehouse file directly, so that file must not be open
for writing anywhere else. When it is, the run stops with the holder's PID
and `--defer-snapshot` is the way through. See
[Environments: Defer](environments#defer).

`--event-time-start` / `--event-time-end` process exactly that range of
windows instead of resuming from recorded state; either may be given alone,
and the end is exclusive. `--force` on a microbatch model reprocesses every
window from its `begin`. See
[Microbatch incremental models](transforms#microbatch-incremental-models).

A selector that matched nothing is a warning; a run that selected nothing at
all exits non-zero. Full grammar: [Selecting models](transforms#selecting-models).

### havn ls

List the models a selector resolves to, without building anything. A dry run
for the selector grammar `havn transform` takes.

```bash
havn ls [TARGETS...] [--select SEL] [--exclude SEL] [--names] [--env NAME] [--project PATH]
```

| Flag | Default | Description |
|------|---------|-------------|
| `TARGETS` | all | Graph selectors to resolve |
| `--select, -s` | none | Graph selector, repeatable |
| `--exclude, -x` | none | Graph selector whose matches are removed |
| `--names, -n` | false | Print bare model names, one per line, for piping |
| `--env, -e` | none | Environment override |

```bash
havn ls                          # every model, with schema, materialization and tags
havn ls '+gold.orders'           # what a build of gold.orders would touch
havn ls state:modified+ --names  # what `havn transform` would rebuild
```

The warehouse is only opened when a `state:` selector needs it, so `havn ls`
works in a project that has never been built. Exits non-zero when nothing
matched.

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

Tools exposed: `query`, `list_tables`, `describe_table`, `list_models`, `get_model`, `model_lineage`, `run_history`, `list_metrics`, `query_metric`, `run_unit_tests`, `run_transform`.

## Data Quality

### havn check

Validate SQL models, run assertions, contracts, and unit tests.

```bash
havn check [TARGETS...] [--unit-tests/--no-unit-tests] [--env NAME] [--project PATH]
```

Runs model validation, inline assertions, YAML contracts, and the unit tests
in `tests/unit/`. Pass `--no-unit-tests` to skip the last step.

### havn test

Run model unit tests: fixture rows in, expected rows out.

```bash
havn test [--model NAME] [-v] [--env NAME] [--project PATH]
```

Each test runs its model against the mock rows declared in `tests/unit/*.yml`
on a throwaway in-memory DuckDB, so nothing is read from or written to the
warehouse. `-v` prints the rows that differ. Exits 1 if any test fails.
See [Unit Tests](unit-tests).

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

### havn rename-column

Rename a column in the model that defines it and everywhere it is read.

```bash
havn rename-column MODEL COLUMN NEW_NAME [--dry-run] [--force] [--yes] [--env NAME] [--project PATH]
```

Prints every site it found, with the clause each one sits in, and every place
it cannot see through. Writes nothing until you confirm, and either writes
every file or none.

```bash
# Look first: the sites and the blockers, nothing written
havn rename-column silver.customers customer_id cust_id --dry-run

# Rename, asking before it writes
havn rename-column silver.customers customer_id cust_id

# Rename the places it can see, leaving the blocked ones alone
havn rename-column silver.customers customer_id cust_id --force
```

A rename refuses while anything is blocked. A downstream `SELECT *`,
`COLUMNS(...)`, `UNION BY NAME`, a relation-position macro call, an
unqualified reference two relations could own, or a metrics or contracts YAML
naming the column each block it; `--force` goes ahead with the rest. See
[Refactoring](refactoring.md).

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

## Governance

### havn rls

Manage row-level security policies. Policies are admin-managed and audit logged; see [Governance](governance.md).

```bash
havn rls ACTION [--table T] [--filter SQL] [--roles R,R] [--users U,U] [--exempt R,R] [--name NAME] [--id ID] [--env NAME] [--project PATH]
```

| Argument/Flag | Default | Description |
|---------------|---------|-------------|
| `ACTION` | required | `list`, `add` or `remove` |
| `--table, -t` | none | `schema.table` the policy protects |
| `--filter, -f` | none | SQL boolean filter; can use `havn_user()`, `havn_role()`, `havn_attr('key')` |
| `--roles` | none | Comma list of roles the policy applies to |
| `--users` | none | Comma list of users the policy applies to |
| `--exempt` | `admin` | Roles exempt from the policy |
| `--name` | none | Policy name |
| `--id` | none | Policy ID (for `remove`) |
| `--env, -e` | none | Environment to use |

```bash
havn rls list
havn rls add -t silver.customers -f "region = havn_attr('region')" --roles viewer,editor
havn rls remove --id <policy-id>
```

### havn pii

Show PII classifications, including those inherited through lineage. A column is classified by a masking policy, an `@pii` tag in its model, or by being derived from a classified column (unless the model says `@declassify`).

```bash
havn pii [RELATION] [--env NAME] [--project PATH]
```

`RELATION` limits the output to one `schema.table`.

## Branches

### havn branch

A warehouse per git branch: build what the branch changed, diff it against the base. Requires `branches: {enabled: true}` in `project.yml`; see [Branches](branches.md).

```bash
havn branch status|build|diff|list|reset|clean
```

| Subcommand | Description |
|------------|-------------|
| `status` | Which branch and warehouse are active, what is built locally, what is deferred |
| `build` | Build the models this branch changed (`state:modified+` against the base); everything else is read from the base, read-only |
| `diff [MODELS]...` | Data diff of the branch's models against the base: schema and rows |
| `list` | Branch warehouses on disk, and whether their git branch still exists |
| `reset` | Delete this branch's warehouse so the next build starts from the base again |
| `clean` | Delete warehouses of branches that were merged into main or deleted |

Options shared by `status`, `build`, `diff` and `reset`:

| Flag | Description |
|------|-------------|
| `--name, -n` | Git branch to act for (default: the checked-out one). CI passes the PR branch here |
| `--base PATH` | Warehouse file to use as the base instead of the configured one (a CI artifact, a backup). Not on `reset` |
| `--project, -p` | Project directory (default: current dir) |

Per subcommand:

| Subcommand | Flag | Description |
|------------|------|-------------|
| `status`, `diff`, `list` | `--json` | Machine-readable output |
| `build` | `--force, -f` | Rebuild every planned model, changed since the last branch build or not |
| `build` | `--no-prune` | Keep branch copies of models that match the base again |
| `build` | `--plan` | Show what would be built and pruned, change nothing |
| `build` | `--defer-snapshot` | Read the base through a consistent copy, for when a job holds it open |
| `diff` | `--markdown, --md` | Print the pull-request comment markdown |
| `diff` | `--output, -o` | Write the output to this file instead of stdout |
| `diff` | `--full` | Every changed row, not samples |
| `diff` | `--exit-nonzero-on-change` | Exit 2 when anything differs (for CI) |
| `reset` | `--yes, -y` | Do not ask for confirmation |
| `clean` | `--dry-run` | List what would be deleted |
| `clean` | `--include-unknown` | Also delete files whose branch cannot be named |

### havn ci

```bash
havn ci generate [--project PATH]
havn ci comment [--markdown FILE] [--repo OWNER/REPO] [--pr N]
```

`generate` writes GitHub Actions workflows that put a data diff on every pull request: a base workflow and a PR workflow.

`comment` posts a markdown data diff to a pull request and updates havn's earlier comment instead of adding a new one.

| Flag | Default | Description |
|------|---------|-------------|
| `--markdown, -m` | `havn-data-diff.md` | Markdown file to post (from `havn branch diff --markdown`) |
| `--repo` | none | GitHub repo (`owner/repo`) |
| `--pr` | none | Pull request number |

## Reports

### havn reports

List, send and preview scheduled dashboard reports. See [Dashboards and sharing](dashboards-sharing.md).

```bash
havn reports list [--env NAME] [--project PATH]
havn reports send NAME [--force] [--env NAME] [--project PATH]
havn reports preview NAME [--out FILE] [--format html|pdf|png] [--env NAME] [--project PATH]
```

| Subcommand | Description |
|------------|-------------|
| `list` | Reports with their schedule, recipients and last delivery |
| `send NAME` | Send a report now, to its configured recipients, as its owner. `--force` sends even if the report's condition is not met |
| `preview NAME` | Render a report to a local file without sending it. `--out, -o` sets the file (default `<name>.<format>`), `--format, -f` is `html` (default), `pdf` or `png` |

`NAME` is the report name or id.

## Ask and Change Sets

### havn ask

Ask a question; havn answers it from the metrics in `metrics/*.yml`. The model picks metrics, dimensions, grain, filters and a time range; havn validates that choice, compiles it with the semantic layer and runs it read-only. Only catalog metadata is sent to the model. See [Ask](ask.md).

```bash
havn ask [QUESTION...] [--continue] [--exploratory] [--summarize] [--save-suggestion] [--sql | --no-sql] [--json] [--env NAME] [--project PATH]
havn ask --eval [FILES...] [--min-accuracy FLOAT]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--eval` | off | Run question to spec eval files (default `tests/ask/*.yml`) |
| `--min-accuracy` | `1.0` | With `--eval`: exit non-zero below this accuracy (0-1) |
| `--continue, -c` | off | Refine the previous question ("now by month") |
| `--exploratory` | off | When no metric fits, try unverified exploratory SQL (needs `ai.exploratory_sql`) |
| `--summarize` | off | Summarise the result in words (sends result rows to the model; needs `ai.summarize_results`) |
| `--save-suggestion` | off | Write a suggested metric definition to `metrics/` |
| `--sql / --no-sql` | `--sql` | Print the compiled SQL |
| `--json` | off | Print the full answer as JSON |
| `--env, -e` | none | Environment to use |

### havn changes

Change sets: agent-proposed model edits, verified before they are applied.

```bash
havn changes list [--all]
havn changes show CHANGE_SET_ID [--diff]
havn changes verify CHANGE_SET_ID [--env NAME]
havn changes apply CHANGE_SET_ID [--force]
havn changes discard CHANGE_SET_ID
```

| Subcommand | Description |
|------------|-------------|
| `list` | List change sets; `--all` includes applied and discarded |
| `show` | Files and verification report; `--diff` shows the file diffs |
| `verify` | Re-run verification (through `havn serve` when it is running) |
| `apply` | Write a verified change set into the project; `--force` applies even though verification failed |
| `discard` | Discard a change set without applying it |

All accept `--project, -p`.

## Performance and Live Models

### havn perf

Performance advisor: slow models, regressions, advice, plans, critical path. Nothing is re-run to produce this. See [Performance](performance.md).

```bash
havn perf [MODEL] [--days N] [--limit N] [--regressions] [--advice] [--all] [--runs] [--critical-path] [--run ID] [--dismiss RULE [--snooze DAYS]] [--reopen RULE] [--plan | --no-plan] [--json] [--env NAME] [--project PATH]
```

| Flag | Default | Description |
|------|---------|-------------|
| `MODEL` | none | A model to show in detail, e.g. `gold.orders` |
| `--days, -d` | 7 | Look-back window in days |
| `--limit, -n` | 15 | Rows per table |
| `--regressions` | off | Only show regressions |
| `--advice` | off | Only show advice |
| `--all` | off | Include dismissed and snoozed advice |
| `--runs` | off | List recent pipeline runs |
| `--critical-path` | off | Critical path of a run (`--run`, default the latest) |
| `--run` | latest | Pipeline run id (a prefix is enough) |
| `--dismiss RULE` | none | Dismiss this advice rule for `MODEL` |
| `--snooze DAYS` | none | With `--dismiss`: snooze for this many days instead |
| `--reopen RULE` | none | Reopen a dismissed or snoozed rule for `MODEL` |
| `--plan / --no-plan` | `--plan` | Show the captured plan in model detail |
| `--json` | off | Output as JSON |

### havn live

Live models: continuous refresh from streaming ingest to gold. Run `havn live` on its own to start the runner in the foreground. See [Live models](live-models.md).

```bash
havn live [--once] [--env NAME] [--project PATH]
havn live status [--json]
havn live pause MODEL
havn live resume MODEL
havn live advance SOURCE
```

| Command | Description |
|---------|-------------|
| `havn live` | Start the runner in the foreground; `--once` runs one refresh cycle and exits |
| `status` | Live models: status, lag, events per second, last refresh |
| `pause MODEL` | Stop refreshing a live model (its downstream live models wait) |
| `resume MODEL` | Resume a paused (or failing) live model; it catches up on everything queued |
| `advance SOURCE` | Stamp rows committed to a landing table (e.g. `landing.orders`) and announce them to live models |

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
- `show` — Show current active environment, plus its defer target and whether
  that target's file can be opened right now
- `reset` — Clear active environment

### havn deploy

Build a git ref in an environment's warehouse, rolling back on failure.

```bash
havn deploy prod --plan              # what would rebuild; changes nothing
havn deploy prod                     # deploy main to prod
havn deploy staging --ref release-3  # any branch, tag or commit
```

The ref is checked out into a temporary worktree, so the code that runs is that
commit's, not whatever is checked out. It rebuilds every model whose SQL or
upstream differs from what that environment last built (`state:modified+`).
Those models are snapshotted first: table data, view definitions, which ones did
not exist yet, and their build state. If any of them fails to build (an error,
a failed error-level check, a blocked upstream), all of them are put back
exactly as they were, and the command exits 1 naming the models that failed.
Ingest does not run; deploy the models, not the data feeding them.

Also available from the web UI (Ship) and `POST /api/deploys`.

### havn macros

List registered Python SQL macros.

```bash
havn macros [--project PATH]
```

Shows all macros discovered from the `macros/` directory, from installed packages, and from havn's built-in library: name, parameters, return type, origin, source file, and docstring.

### havn packages

Install and inspect shared model and macro packages. See [Packages](packages.md).

```bash
havn packages list               # list installed packages: rev, commit, counts
havn packages                    # same as `havn packages list`
havn packages install            # install what project.yml declares
havn packages install --upgrade  # re-resolve each rev instead of using the lock
havn packages remove crm         # delete a checkout and its lock entry
```

Packages are declared under `packages:` in `project.yml` as `{name, git, rev}`
or `{name, path}`, installed into `havn_packages/`, and pinned by
`havn_packages.lock`. A package's models are namespaced into `<pkg>_<schema>`
and selectable with `package:<name>`.

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
