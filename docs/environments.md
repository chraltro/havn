# Environments

havn supports multiple environments so you can maintain separate databases for development, staging, and production. Environments are defined in `project.yml` and selected at runtime via the `--env` flag or the web UI.

## Defining Environments

Add an `environments:` section to your `project.yml`:

```yaml
name: my-project
database:
  path: warehouse.duckdb          # Default database path

environments:
  dev:
    database:
      path: dev_warehouse.duckdb
  staging:
    database:
      path: staging_warehouse.duckdb
  prod:
    database:
      path: prod_warehouse.duckdb
  test:
    database:
      path: ":memory:"             # In-memory database for tests
```

Each environment can override the `database.path` setting. When no environment is specified, the top-level `database.path` is used.

## Using Environments

### Setting an Active Environment

Instead of passing `--env` on every command, set a persistent active environment:

```bash
# Set the active environment
havn env use prod

# Now all commands use prod automatically
havn transform              # uses prod
havn query "SELECT 1"       # uses prod
havn jobs run daily-etl      # uses prod

# Check which environment is active
havn env show

# List all available environments
havn env list

# Reset to default (no environment override)
havn env reset
```

The active environment is stored in a `.havn-env` file in the project root. This file is local (added to `.gitignore`) and not shared with the team.

The `--env` flag still works and overrides the active environment:

```bash
havn env use prod
havn transform --env dev    # overrides to dev for this command only
```

### CLI Commands

Most CLI commands accept an `--env` flag:

```bash
# Transform against the dev database
havn transform --env dev

# Query the production database
havn query "SELECT COUNT(*) FROM gold.customers" --env prod

# Run a job against staging
havn jobs run daily-etl --env staging

# List tables in the dev database
havn tables --env dev

# Load seeds into the dev environment
havn seed --env dev

# Check freshness in production
havn freshness --env prod
```

### Web UI

Start the server with a specific environment:

```bash
havn serve --env staging
```

You can also switch environments at runtime through the API:

```bash
curl -X PUT http://localhost:3000/api/environment/prod \
  -H "Authorization: Bearer <token>"
```

The current environment is shown in the API response:

```bash
curl http://localhost:3000/api/environment
```

Returns:

```json
{
  "active": "staging",
  "available": ["dev", "staging", "prod", "test"],
  "database_path": "staging_warehouse.duckdb"
}
```

## Deploying to an environment

`havn deploy <env>` builds a git ref (default `main`) in that environment's
warehouse and rebuilds only what differs there (`state:modified+`, compared
against that environment's own build state), using the ref's code and macros.
The models it will touch are snapshotted first. If any of them fails, every
one of them is restored exactly (data, views, build state), so a failed deploy
leaves the environment as it was and the next deploy plans the same models
again. Use `--plan` to see the models first.

In the web UI, Ship offers the same after a change merges, defaulting to the
production-looking environment (`prod`, `production`, `prd`, `live`), which the
environment pill in the top bar also shows in red. Every deploy is recorded in
`_havn.deploys` and listed in Ship.

A deploy is a code change applied to data that already exists. Ingest does not
run, so a model that reads a landing table the environment lacks fails and
rolls back, and the failure names the missing table.

## Defer

Defer lets a run build in one environment while reading everything it has not
built from another environment's warehouse. You change one gold model in dev,
run it, and its upstreams come from prod instead of being rebuilt.

### Read the lock caveat first

havn reads the other environment's warehouse file directly, and DuckDB puts a
lock on that file. A deferred run fails whenever any other process holds the
target open for writing, which is exactly when a scheduled run against it is
going. The error names the process DuckDB reported:

```
Cannot defer to /data/prod.duckdb: another process has it open for writing,
so DuckDB will not attach it read-only. It is held by /usr/bin/python3.11
(PID 10344). This happens whenever a run against that environment is in
flight. Retry once it finishes, or use --defer-snapshot to defer to a
consistent copy instead.
```

This is DuckDB's concurrency model, not a havn limitation, and there is no
way around it short of not reading the live file:

```bash
havn transform gold.orders --defer-snapshot
```

`--defer-snapshot` copies the target first and defers to the copy. While the
target is free, the copy is made with DuckDB's own `COPY FROM DATABASE`, so
it is a catalog-level export of committed data rather than bytes taken from
under a writer. While the target is locked, that copy cannot be made either,
and havn falls back to the newest verified backup from `havn backup`. If the
target is locked and no verified backup exists, the run stops and says so:
there is nothing consistent left to read.

To check before you run:

```bash
havn env show
```

```
Active environment: dev
Defer target: prod (/data/prod.duckdb)
Defer target readable: no (locked by another process (/usr/bin/python3.11 (PID 10344)))
```

The same answer is in `GET /api/environment` under `defer`, and the web UI
shows it next to the environment name in the header: a green dot when the
target is readable, amber when it is not, with the path, the reason and the
`--defer-snapshot` way out in the tooltip. All three are readings of that
instant; another process can take the lock a moment later.

### Configuration

```yaml
environments:
  dev:
    database:
      path: dev_warehouse.duckdb
    defer: prod
  prod:
    database:
      path: /data/prod_warehouse.duckdb
```

The target must be another defined environment, and it cannot be the
environment itself; both mistakes fail when project.yml is loaded.

With that in place, deferring is the default for runs in dev:

```bash
havn transform gold.orders          # reads prod for anything dev lacks
havn transform gold.orders --no-defer   # build against dev alone
```

`POST /api/transform` takes the same choice as `"defer": true | false | null`
and `"defer_snapshot": true`, where null means "defer if the environment says
to".

### What gets redirected

The rule is the local catalog, not the model list. When a run defers, every
schema-qualified table reference is checked against this warehouse:

- present here, read here, including a model this same run just built;
- absent here but present in the target, read from the target. That covers
  `landing` tables, seeds and declared sources without listing any of them;
- absent from both, left exactly as written, so the run fails with DuckDB's
  own "table does not exist" about the table you wrote;
- a reference that already names a database, a CTE name or a table function
  is never touched;
- an ephemeral model is inlined into its consumer before defer looks at the
  query, so it is never read from the target.

Writes are never redirected. Everything a deferred run builds lands in its own
warehouse, and the target is attached read-only, so DuckDB would refuse a
write to it even if havn tried.

The rewrite happens in memory at execution time. Your `.sql` files are not
touched, and `content_hash` is computed from the model text as written, so a
deferred run and a normal run agree on what has changed.

Masking policies are unaffected: they resolve on `schema.table` and ignore
which database the table came from, so a policy on `bronze.customers` still
masks a deferred read of it.

### Two runs at once

`havn serve` can have two transform runs in flight in one process: a
`POST /api/transform` and a scheduled run both execute outside the pipeline
lock. Defer is scoped to the run, not the process, so they do not interfere:

- the set of redirects belongs to the run that asked for it and is passed
  down to each model, including to parallel workers, which are threads inside
  that run. A run started with `--no-defer`, or against an environment with no
  defer target, builds exactly what it would have built alone, whatever
  another run is doing at the same time;
- the read-only attach is shared, because DuckDB shares it between every
  connection to the same warehouse file. havn counts the runs using it, so the
  target stays attached until the last deferred run finishes rather than being
  detached by the first one to end;
- two runs deferring to two *different* environments each get their own
  attach alias, so both targets are readable at once.

### Not supported yet

- The DuckLake backend. DuckLake already uses the attach slot defer needs, so
  a deferred run on it stops with a clear error.
- Deferring across machines. The target is a file this machine can open.

### Compared with dbt

dbt's `--defer` needs a manifest from a previous run plus `--state <dir>`
pointing at it, and it resolves `ref()` against that manifest. havn has no
manifest and no `ref()`: it needs the other environment's warehouse file,
attaches it, and decides per reference from the two live catalogs.

## Environment Variable Expansion

Environment variables in `project.yml` are resolved from the `.env` file. This is independent of the `environments:` feature but works well together:

```yaml
connections:
  production_db:
    type: postgres
    host: ${DB_HOST}
    password: ${DB_PASSWORD}
```

The `.env` file at the project root:

```
DB_HOST=db.production.internal
DB_PASSWORD=s3cure_p@ssw0rd
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

### Per-Environment .env Files

havn resolves variables from a single `.env` file. If you need different secrets per environment, manage this outside havn (e.g., using separate `.env.dev` and `.env.prod` files and symlinking, or using a secrets manager).

## Common Patterns

### Development vs Production

```yaml
environments:
  dev:
    database:
      path: dev_warehouse.duckdb
  prod:
    database:
      path: /data/prod_warehouse.duckdb
```

Development:

```bash
havn transform --env dev
havn serve --env dev
```

Production:

```bash
havn jobs run daily-etl --env prod
```

### In-Memory Testing

Use `:memory:` for fast, disposable test databases:

```yaml
environments:
  test:
    database:
      path: ":memory:"
```

```bash
havn transform --env test
havn check --env test
```

### Isolated Feature Development

Create a per-branch database to avoid conflicts:

```bash
havn transform --env dev
```

Each developer can have their own `dev_warehouse.duckdb` file that is not checked into version control.

## How It Works

When you specify `--env <name>`:

1. havn loads `project.yml` as normal
2. It looks up the environment by name in `environments:`
3. It overlays the environment-specific settings onto the base config
4. The merged config is used for all operations

`database.path`, `connections` and `defer` can be set per environment. All other settings (streams, lint config) are shared across environments.

## Related Pages

- [Configuration](configuration) -- Full `project.yml` reference
- [Getting Started](getting-started) -- Project setup
- [Pipelines](pipelines) -- Running streams with environments
