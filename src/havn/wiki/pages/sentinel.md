# Schema Sentinel

Schema Sentinel monitors external source tables for breaking changes and alerts you when upstream schemas change in ways that could break your models. Instead of discovering schema issues during pipeline failures, Sentinel proactively detects them and provides impact analysis across your data lineage.

## Overview

When data arrives from external sources (Postgres, Snowflake, APIs, files), the schema can change without warning--columns get renamed, types change, or fields disappear entirely. These upstream changes propagate downstream and break your SQL models. Sentinel watches source schemas for changes, calculates which of your models are affected, and provides actionable fix suggestions.

## UI Experience

Sentinel lives in the **Observe** section of the web UI. It has three views:

### Check View

Run an on-demand schema check:

1. Click **Run Schema Check** to scan all configured sources
2. The system compares each source's current schema against its last recorded snapshot
3. Changes are displayed as a list of diffs by source, with severity badges

Each change shows:
- **Change Type**: column added, removed, renamed, type changed, etc.
- **Severity**: red badge for breaking changes, yellow for warnings, gray for info
- **Column**: which column was affected
- **Details**: old value → new value, or rename candidates if available

Below each diff, an **Impact Analysis** section shows:
- Which of your models are affected (direct or transitive)
- Which columns in those models depend on the changed source column
- Fix suggestions (e.g., "Update SELECT to use new_column_name")
- **Dismiss** button to mark the impact as resolved (records that you've addressed it)

### Diffs View

Browse historical schema diffs in a two-pane interface:

- **Left pane**: List of all recorded schema changes, sorted by most recent first. Shows source name, timestamp, number of changes, and BREAKING badge if any breaking changes exist.
- **Right pane**: Impact analysis for the selected diff. Click a diff to see which models are downstream and what fix suggestions apply.

### History View

Track schema snapshots over time:

- **Left pane**: List of all monitored sources with a green "exists" indicator or gray "missing" status
- **Right pane**: Complete schema history for the selected source. Each snapshot shows the captured timestamp, column count, schema hash, and a preview of the first 10 columns (name and type)

This view is useful for auditing: you can see exactly what the schema looked like at any point in time and trace when columns were added or removed.

## Severity Levels

Each detected change has a severity badge:

| Severity | Color | Meaning | Action |
|----------|-------|---------|--------|
| **breaking** | Red | Column removed, type incompatible change, or constraint violation | Must fix model SQL to avoid pipeline failure |
| **warning** | Yellow | Column renamed (with high confidence), nullable constraint removed | Should update model to use new names; may cause issues |
| **info** | Gray | Column added, nullable constraint added, or low-confidence rename candidate | Informational; models unaffected but may want to consume new column |

## Impact Analysis

For each schema change, Sentinel analyzes downstream impact:

| Impact Type | Meaning | Example |
|-------------|---------|---------|
| **direct** | Your model directly selects from the changed source | `SELECT * FROM prod.customers` and `prod.customers.name` was deleted |
| **transitive** | Your model depends on another model that depends on the changed source | Your `silver.customer_demographics` depends on `bronze.customers` which depends on `prod.customers` |
| **safe** | The change doesn't affect any of your models | New column added to a source you don't consume |

Each impacted model also shows:
- **Columns affected**: which of your model's columns depend on the changed source column
- **Fix suggestion**: suggested SQL changes to resolve the issue

## Resolving Issues

### From the Check View

When you see an impact listed:

1. Read the fix suggestion to understand what changed
2. If auto-fix is applicable, the suggestion may include the exact SQL change
3. Click **Dismiss** to mark the impact as resolved. This records that you've acknowledged the change and taken action.

### Manual Resolution

1. Open the affected model's SQL file in the **Develop** section
2. Apply the fix (e.g., replace `old_column_name` with `new_column_name`)
3. Run **havn transform** to rebuild and validate the fix
4. Return to Sentinel and click **Dismiss** on the resolved impacts

## History Tracking

Every schema change is recorded in `_havn.sentinel_diffs`:

| Column | Type | Description |
|--------|------|-------------|
| `diff_id` | VARCHAR | Unique diff identifier |
| `source_name` | VARCHAR | Source table (schema.table) |
| `changes` | JSON | Array of change objects |
| `has_breaking` | BOOLEAN | True if any change is breaking |
| `created_at` | TIMESTAMP | When the diff was detected |

Each change object in `changes` contains:
- `change_type`: added, removed, renamed, type_changed, constraint_changed
- `column_name`: affected column
- `severity`: breaking, warning, info
- `old_value`, `new_value`: before/after values
- `rename_candidate`: suggested new name (if column likely renamed)

Schema snapshots are stored in `_havn.sentinel_snapshots`:

| Column | Type | Description |
|--------|------|-------------|
| `source_name` | VARCHAR | Source table |
| `schema_hash` | VARCHAR | SHA256 of normalized schema definition |
| `columns` | JSON | Array of {name, type, constraints} |
| `captured_at` | TIMESTAMP | When this snapshot was taken |

## Configuration

Configure Sentinel in `project.yml` under the `sentinel` section:

```yaml
sentinel:
  enabled: true
  on_change: warn                    # warn | error | ignore
  track_ordering: false              # Track column order changes
  rename_inference: true             # Suggest renames for deleted+added pairs
  auto_fix: false                    # Auto-fix obvious renames in models
  select_star_warning: true          # Warn about SELECT * (vulnerable to schema changes)
```

### Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enabled` | boolean | `true` | Enable schema monitoring |
| `on_change` | string | `warn` | Severity when schema changes: `warn`, `error`, or `ignore` |
| `track_ordering` | boolean | `false` | Treat column reordering as a breaking change |
| `rename_inference` | boolean | `true` | Suggest column renames by comparing deleted/added columns |
| `auto_fix` | boolean | `false` | Automatically apply obvious rename fixes to models |
| `select_star_warning` | boolean | `true` | Flag models using `SELECT *` as high-risk for schema changes |

### Source Declaration

Sentinel automatically discovers sources by scanning your model SQL files for upstream references (auto-extracted from `FROM`/`JOIN` or declared via `@depends_on`). You can also explicitly list sources:

```yaml
sentinel:
  sources:
    - prod.customers
    - prod.orders
    - staging.temp_data
```

## Common Workflows

### Detect a Breaking Change

1. Go to **Observe** → **Sentinel** → **Check** tab
2. Click **Run Schema Check**
3. If breaking changes are found, they show with red BREAKING badges
4. Review the impact analysis to see which models are affected
5. Go to **Develop** section and fix each impacted model

### Track Schema History

1. Go to **Observe** → **Sentinel** → **History** tab
2. Select a source from the left pane
3. View all snapshots from oldest to newest
4. Click on a snapshot to see the exact columns at that point in time

### Investigate a Previous Change

1. Go to **Observe** → **Sentinel** → **Diffs** tab
2. Find the relevant diff in the left pane (sorted by most recent)
3. Click it to see the impact analysis on the right
4. Review which models were affected and what fix was suggested

## Best Practices

1. **Run checks regularly** -- Include `havn sentinel check` in your CI/CD pipeline to catch upstream changes early.

2. **Enable auto-fix for safe renames** -- Set `auto_fix: true` if you trust Sentinel's rename inference; review the fix suggestions before deployment.

3. **Avoid SELECT *** -- Use explicit column lists in your models. Enable `select_star_warning: true` to flag these as high-risk.

4. **Monitor critical sources** -- Pay close attention to sources that feed many downstream models (high transitive impact).

5. **Dismiss acknowledged changes** -- Use the **Dismiss** button to mark impacts as resolved; this helps track which issues you've already handled.

6. **Review history regularly** -- Use the History view to understand how source schemas evolve over time and plan schema-aware transformations.

## API Reference

### Run Check

```bash
POST /api/sentinel/check
```

Scan all sources and detect schema changes. Returns diffs with impact analysis.

### Get Diffs

```bash
GET /api/sentinel/diffs?limit=50
```

Get recent schema diffs (default: 50, max: 500).

### Get Impacts

```bash
GET /api/sentinel/impacts/{diff_id}
```

Get impact analysis for a specific diff.

### Get Sources

```bash
GET /api/sentinel/sources
```

Get all monitored sources with existence status.

### Get History

```bash
GET /api/sentinel/history/{source_name}?limit=20
```

Get schema snapshot history for a source (default: 20 snapshots).

### Resolve Impact

```bash
POST /api/sentinel/resolve
Content-Type: application/json

{
  "diff_id": "diff_123",
  "model_name": "silver.customers"
}
```

Mark an impact as resolved/dismissed.

### Apply Fix

```bash
POST /api/sentinel/apply-fix
Content-Type: application/json

{
  "model_path": "transform/silver/customers.sql",
  "old_name": "customer_name",
  "new_name": "full_name"
}
```

Apply a rename fix to a model SQL file (write permission required).

## Related Pages

- [Observe](quality) -- Data quality overview
- [Pipelines](pipelines) -- Running schema checks in automated pipelines
- [Configuration](configuration) -- project.yml schema sentinel settings
- [CLI Reference](cli-reference) -- `havn sentinel` command reference
