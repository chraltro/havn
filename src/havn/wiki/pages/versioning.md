# Versioning & Rewind

havn lets you travel back in time through your pipeline runs. Every transform execution creates snapshots of your data, allowing you to inspect historical states, compare changes across runs, and restore tables to previous points.

## The Rewind Feature

The **Rewind** button in the DAG tab (Observe) gives you an interactive timeline to explore your pipeline history.

### How to Use Rewind

1. Go to the **Observe** tab and click the **DAG** panel
2. Click the **Rewind** button in the top right of the DAG panel
3. A timeline slider appears showing all recorded pipeline runs
4. Click on any run in the timeline to view that moment in your data
5. Click on any model in the DAG to see a detail panel with that model's state at that point

### What You See in Rewind Mode

The DAG canvas changes to show:

- **Row counts** -- Each model displays its row count at the selected run (e.g., "1,234 rows")
- **Row deltas** -- Green or red badges show how many rows were added or removed compared to the previous run (e.g., "+432" or "-50")
- **Schema changes** -- A small orange dot in the corner indicates models where columns were added or removed
- **Restorable status** -- Models show "expired" if their data is no longer stored (old snapshots may be cleaned up)

### The Timeline Slider

At the top of the DAG panel:

- **Track line** -- Shows the time range from your oldest to newest recorded run
- **Run dots** -- Each dot represents a pipeline run:
  - Green = successful run
  - Red = failed run
  - Yellow = running or partial status
  - Click any dot to jump to that run
- **Selected run info** -- Shows the timestamp, status, and trigger type (e.g., "scheduled" or "manual")

### The Detail Panel

Click on any model in the DAG (in rewind mode) to open a side panel showing:

- **Row count** -- Exact row count for that model at the selected run
- **Columns** -- Number of columns in that version
- **Size** -- Storage size of the snapshot
- **Status** -- "Restorable" or "Expired" (restored data may expire after retention)
- **Row count history** -- A sparkline showing how row counts changed across recent runs
- **Sample data** -- A preview of the first 50 rows from that run
- **Restore to this point** -- Button to restore this model to its state at the selected run

## Restoring from Rewind

When you click "Restore to this point" in the detail panel:

1. havn automatically creates a snapshot of your current data (for safety)
2. The selected model is restored from the historical snapshot
3. All downstream models are automatically re-built (to keep dependencies consistent)
4. The timeline updates to show the new run

### Why Restore Matters

- **Data quality issues** -- Discovered a bad transformation? Restore a good version and re-run
- **Schema exploration** -- Check what columns existed in an older run
- **Debugging** -- Compare current vs. historical state side-by-side
- **Rollback** -- Undo changes without deleting anything from git

## How Snapshots Work Under the Hood

Every time a transform runs (a pipeline execution), havn creates **snapshots** -- lightweight copies of table metadata and data:

- **Row counts** -- Stored with each table's state
- **Column schema** -- Column names, types, stored for drift detection
- **Parquet files** -- Actual data is stored in Parquet format in `_snapshots/` directory
- **Run metadata** -- Timestamp, status, trigger type (manual, scheduled, etc.)

These snapshots are what power the Rewind feature. When you restore a model, havn reads the Parquet file and recreates the table.

### Snapshot Retention

Snapshots take disk space. Configure retention in `project.yml`:

```yaml
rewind:
  enabled: true
  retention: 30  # Keep 30 days of snapshots
  max_storage: 10GB  # Or limit by disk usage
  exclude: [temp_table, scratch]  # Don't snapshot certain tables
```

When you exceed retention limits, old snapshots are automatically cleaned up (garbage collected).

## Project Snapshots (File State)

In addition to run snapshots, havn can create **project snapshots** -- metadata-only records of your entire project:

```bash
havn snapshot
```

Or with a name:

```bash
havn snapshot --name before-refactor
```

These capture:

- SHA256 hashes of all files in `transform/`, `ingest/`, `export/`, and `project.yml`
- Row counts and schema hashes for every table
- Combined project hash

Useful for comparing changes across time without storing full data. Via API:

```bash
curl -X POST http://localhost:3000/api/snapshot \
  -H "Content-Type: application/json" \
  -d '{"name": "before-refactor"}'
```

### Comparing Snapshots

Compare your current state against a named snapshot:

```bash
havn diff --snapshot before-refactor
```

Shows file changes and table modifications:

```
Snapshot: before-refactor
Created: 2025-01-15 06:00:00

File changes:
  + transform/gold/new_model.sql
  ~ transform/silver/dim_customer.sql
  - transform/bronze/old_cleanup.sql

Table Changes
  Table                    Status     Before Rows   After Rows
  gold.new_model          added                        1,234
  silver.dim_customer     modified        5,000         5,432
  bronze.old_cleanup      removed         2,100
```

## Model-Level Diff

Compare the current SQL execution against the materialized table (without time travel):

```bash
havn diff
```

This re-executes each model's SQL and shows what would change if you rebuild:

```
Diff Summary
  Model                    Before   After   Added   Removed   Modified   Schema
  gold.customer_summary     5,000   5,432    +432         0          0     --
  silver.dim_customer       3,200   3,200       0         0         15     --
```

### Git-Aware Diff

Only diff models whose SQL changed:

```bash
havn diff --against main
```

Finds SQL files that differ between HEAD and `main`, then diffs only those.

## API Reference

All rewind features are accessible via the REST API:

### Get Pipeline Runs

```bash
curl http://localhost:3000/api/rewind/runs
```

Returns list of runs with timestamps, status, trigger type, and model count.

### Get All Snapshots

```bash
curl http://localhost:3000/api/rewind/snapshots
```

Returns all snapshot metadata (row counts, column count, schema hash, file path).

### Get Snapshots for a Run

```bash
curl http://localhost:3000/api/rewind/snapshots/run-123
```

### Sample Data from a Snapshot

```bash
curl http://localhost:3000/api/rewind/sample/run-123/gold.customers?limit=50
```

### Restore a Model

```bash
curl -X POST http://localhost:3000/api/rewind/restore \
  -H "Content-Type: application/json" \
  -d '{"run_id": "run-123", "model_name": "gold.customers", "cascade": true}'
```

The `cascade` flag tells havn to re-build downstream models after restore.

### Garbage Collect Old Snapshots

```bash
curl -X POST http://localhost:3000/api/rewind/gc
```

Removes expired snapshots based on `project.yml` retention settings.

## Database Backup & Restore

For complete database recovery, use database-level backup:

```bash
# Create a backup
havn backup

# Restore from a backup
havn restore warehouse.duckdb.backup_20250115_060000
```

Backups are full copies of `warehouse.duckdb`. Use for disaster recovery, not for time travel within a run.

## Related Pages

- [Transforms](transforms) -- SQL models and the DAG
- [Pipelines](pipelines) -- Running streams and understanding run lifecycle
- [Observe Tab](observe) -- Dashboard and monitoring features
- [API Reference](api-reference) -- Full API endpoint documentation
