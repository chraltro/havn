# Environments

havn supports multiple environments so you can maintain separate databases for development, staging, and production. Environments are defined in `project.yml` and selected at runtime via the `--env` flag or the web UI.

## Web UI Experience

### Environment Switcher

The web UI supports environment selection:

1. Start the server with a specific environment: `havn serve --env staging`
2. The current environment is shown in the UI
3. Switch environments at runtime via the API:

```bash
curl -X PUT http://localhost:3000/api/environment/prod \
  -H "Authorization: Bearer <token>"
```

### Viewing the Active Environment

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

### Impact on All Panels

When you switch environments:
- **Tables** shows tables from the new database
- **Query** runs against the new database
- **History** shows run history from the new database
- **Quality** shows profiles and assertions from the new database
- **DAG** reflects model states in the new database

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

# Run contracts against staging
havn contracts --env staging

# Run quality checks against test
havn check --env test

# Run Sentinel checks against production
havn sentinel check --env prod
```

### Web UI

Start the server with a specific environment:

```bash
havn serve --env staging
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

This is ideal for CI/CD pipelines where you want to validate transforms without persisting data.

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

## API Reference

### Get Current Environment

```bash
GET /api/environment
```

Returns the active environment, available environments, and the database path.

### Switch Environment

```bash
PUT /api/environment/{env_name}
```

Switches the active database to the specified environment. Requires write permission when auth is enabled.

## Related Pages

- [Configuration](configuration) -- Full `project.yml` reference
- [Getting Started](getting-started) -- Project setup
- [Pipelines](pipelines) -- Running streams with environments
