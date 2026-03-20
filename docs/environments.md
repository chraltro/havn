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
havn stream daily-etl       # uses prod

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

# Run a stream against staging
havn stream daily-etl --env staging

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
havn stream daily-etl --env prod
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

Currently, only `database.path` can be overridden per environment. All other settings (connections, streams, lint config) are shared across environments.

## Related Pages

- [Configuration](configuration) -- Full `project.yml` reference
- [Getting Started](getting-started) -- Project setup
- [Pipelines](pipelines) -- Running streams with environments
