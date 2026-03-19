# Connectors

Connectors automate data ingestion from external sources. havn includes pre-built connectors for databases, SaaS APIs, file storage, and webhooks. Each connector tests the connection, discovers available resources, generates an ingest script, and updates `project.yml`.

## Web UI Experience

### Data Sources Panel in Explore Tab

1. Go to the **Explore** tab and click **Data Sources**
2. View all configured connectors with their type, status, and last sync time
3. **Add Data Source** -- Click to open the connector setup wizard:
   - Select a connector type (Postgres, Stripe, CSV, etc.)
   - Enter connection parameters (credentials are stored in `.env`)
   - Test the connection
   - Discover available tables/resources
   - Select which tables to sync
   - Choose a target schema (default: `landing`)
   - Optionally set a sync schedule
4. **Sync** -- Click to manually trigger a sync for any connector
5. **Test** -- Re-test a connector's connection
6. **Remove** -- Delete a connector and its generated ingest script

### Import Wizard

The web UI includes a data import wizard for one-off imports:

1. Go to **Explore** > **Data Sources**
2. Use the import options:
   - **Upload CSV/Parquet** -- Drag and drop a file to preview and import it into a landing table
   - **Connect to Database** -- Enter connection details for a one-time import from Postgres, MySQL, etc.
3. The wizard shows a **preview** of the data before importing, so you can verify column types and content

### Connector Health in Overview

The **Overview** tab shows connector health alongside pipeline status, including the number of active connectors and their most recent sync status.

## Available Connectors

| Connector | Type | Description |
|-----------|------|-------------|
| PostgreSQL | `postgres` | PostgreSQL database tables |
| MySQL | `mysql` | MySQL/MariaDB database tables |
| CSV Files | `csv` | Local or remote CSV files |
| Stripe | `stripe` | Stripe payments data (charges, customers, subscriptions) |
| Shopify | `shopify` | Shopify e-commerce data (orders, products, customers) |
| HubSpot | `hubspot` | HubSpot CRM data (contacts, companies, deals) |
| Google Sheets | `google_sheets` | Google Spreadsheets |
| REST API | `rest_api` | Generic REST API endpoints with pagination support |
| S3/GCS | `s3_gcs` | Amazon S3 or Google Cloud Storage files (CSV, Parquet, JSON) |
| Webhook | `webhook` | Receive data via inbound HTTP POST |

List all available connectors:

```bash
havn connectors available
```

## Setting Up a Connector

### Interactive Setup

Use `havn connect` to set up a connector:

```bash
# PostgreSQL
havn connect postgres --host localhost --database mydb --user admin --password secret

# Stripe
havn connect stripe --api-key sk_live_xxx

# CSV file
havn connect csv --path /data/customers.csv

# Google Sheets
havn connect google-sheets --set spreadsheet_id=1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms

# REST API
havn connect rest_api --url https://api.example.com/data --api-key mykey

# S3
havn connect s3_gcs --path s3://my-bucket/data/ --set aws_access_key_id=... --set aws_secret_access_key=...

# With JSON config
havn connect postgres --config '{"host":"db.prod","database":"app","user":"ro","password":"s3cret"}'

# From a config file
havn connect postgres --config ./postgres.json
```

The setup process:

1. **Tests the connection** -- Verifies credentials and connectivity
2. **Discovers resources** -- Lists available tables, endpoints, or sheets
3. **Generates an ingest script** -- Creates `ingest/connector_<name>.py`
4. **Updates project.yml** -- Adds the connection and creates a sync stream
5. **Stores secrets in .env** -- Passwords and API keys go to `.env`, not `project.yml`

### Configuration Options

```bash
havn connect <type> [OPTIONS]

Options:
  --name, -n          Connection name (default: auto-generated)
  --tables, -t        Comma-separated tables to sync
  --schema, -s        Target schema (default: landing)
  --schedule          Cron schedule for automatic sync
  --test              Only test the connection (don't set up)
  --discover          Only list available resources (don't set up)
  --config, -c        JSON string or file path with params
  --set key=value     Set individual parameters (repeatable)
```

Convenience shortcuts for common parameters:

```bash
  --host              Hostname
  --port              Port number
  --database          Database name
  --user              Username
  --password          Password
  --url               URL
  --api-key           API key
  --token             Access token
  --path              File or bucket path
```

## Managing Connectors

### List Configured Connectors

```bash
havn connectors list
```

Shows all connectors in `project.yml` with their type, script path, and status.

### Test a Connection

```bash
havn connectors test prod_postgres
```

Verifies that the connection still works with the stored credentials.

### Sync Data

```bash
havn connectors sync prod_postgres
```

Runs the generated ingest script for a connector.

### Regenerate Script

```bash
havn connectors regenerate prod_postgres
```

Re-discovers resources and regenerates the ingest script. Useful when the connector code is updated or the source schema changes.

### Remove a Connector

```bash
havn connectors remove prod_postgres
```

Deletes the ingest script and removes the connection from `project.yml`.

## Connector Architecture

Each connector implements the `BaseConnector` contract:

- `test_connection(config)` -- Verify the connection works
- `discover(config)` -- List available tables/resources
- `generate_script(config, tables, target_schema)` -- Emit a Python ingest script

The generated ingest script is a standard havn Python script that uses the `db` DuckDB connection. You can customize it after generation.

### Secret Handling

Connector parameters marked as `secret` (passwords, API keys, tokens) are:

1. Stored in `.env` as environment variables (e.g., `PROD_POSTGRES_PASSWORD=...`)
2. Referenced in `project.yml` as `${ENV_VAR_NAME}` placeholders
3. Never written to `project.yml` in plaintext

## Webhook Connector

The webhook connector receives data via HTTP POST and stores it in a landing table:

```bash
havn connect webhook --name orders_webhook
```

Once configured, send data to the webhook endpoint:

```bash
curl -X POST http://localhost:3000/api/webhook/orders \
  -H "Content-Type: application/json" \
  -d '{"order_id": 123, "amount": 99.99}'
```

Data is stored in `landing.<webhook_name>_inbox` with columns: `id`, `received_at`, `payload` (JSON).

## Import Wizard (File Upload)

For one-off imports without setting up a connector:

### Preview a File

```bash
POST /api/import/preview-file
Content-Type: multipart/form-data

file: <csv-or-parquet-file>
```

Returns a preview of the first rows with auto-detected column types.

### Import a File

```bash
POST /api/import/file
Content-Type: multipart/form-data

file: <csv-or-parquet-file>
table_name: my_table
schema_name: landing
```

### Import from External Database

```bash
POST /api/import/from-connection
Content-Type: application/json

{
  "connection_type": "postgres",
  "config": {"host": "...", "database": "..."},
  "source_table": "users",
  "target_schema": "landing",
  "target_table": "users"
}
```

## Connector Health

Check the sync status and health of all connectors:

```bash
curl http://localhost:3000/api/connectors/health
```

Returns the most recent run status, timestamp, duration, and row count for each connector's ingest script.

## API Reference

### List Available Connector Types

```bash
GET /api/connectors/available
```

### List Configured Connectors

```bash
GET /api/connectors
```

### Test a Connection

```bash
POST /api/connectors/test
Content-Type: application/json

{"connector_type": "postgres", "config": {"host": "...", "database": "..."}}
```

### Discover Resources

```bash
POST /api/connectors/discover
Content-Type: application/json

{"connector_type": "postgres", "config": {"host": "..."}}
```

### Full Connector Setup

```bash
POST /api/connectors/setup
Content-Type: application/json

{
  "connector_type": "postgres",
  "connection_name": "prod_db",
  "config": {"host": "...", "database": "..."},
  "tables": ["users", "orders"],
  "target_schema": "landing",
  "schedule": "0 6 * * *"
}
```

### Regenerate Ingest Script

```bash
POST /api/connectors/regenerate/{connection_name}
```

### Sync a Connector

```bash
POST /api/connectors/sync/{connection_name}
```

### Remove a Connector

```bash
DELETE /api/connectors/{connection_name}
```

### Connector Health

```bash
GET /api/connectors/health
```

## Related Pages

- [CDC](cdc) -- Incremental sync with change data capture
- [Configuration](configuration) -- Connection configuration in project.yml
- [Pipelines](pipelines) -- Running connectors as pipeline steps
- [Scheduler](scheduler) -- Automating connector syncs on a schedule
- [API Reference](api-reference) -- Full connector API endpoints
