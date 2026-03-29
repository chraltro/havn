# Seeds

Seeds are CSV files that are loaded into DuckDB as reference tables. They provide a simple way to include static or slowly-changing data (lookup tables, configuration data, reference codes) in your data warehouse without writing ingest scripts.

## Web UI Experience

### Seeds in the DAG

1. Go to the **Develop** tab and click **DAG**
2. Seeds appear as special nodes in the dependency graph with a distinct icon
3. Seeds are linked to downstream models that reference them via `-- depends_on:`
4. This shows how reference data flows into your transforms

### Seeds in the File Tree

1. In the **Develop** tab, expand the `seeds/` directory in the file tree
2. Click any `.csv` file to view and edit its contents in the Monaco editor
3. Changes are auto-saved, and the next `havn seed` run will reload the modified file

### Browsing Seed Data

1. Go to **Explore** > **Tables**
2. Expand the `seeds` schema to see all loaded seed tables
3. Click a table to see its columns, data types, and a preview of the data
4. Use **Explore** > **Query** to write SQL against seed tables

### Loading Seeds from the UI

Use the **Run menu** dropdown in the Develop tab:
- Seeds are loaded as part of the pipeline when you run a stream that includes a `seed: [all]` step

## Directory Structure

Place CSV files in the `seeds/` directory at the project root:

```
my-project/
  seeds/
    country_codes.csv
    magnitude_scale.csv
    status_definitions.csv
```

Each CSV file becomes a table in the `seeds` schema. For example, `seeds/country_codes.csv` creates `seeds.country_codes`.

## CSV Format

Seeds are standard CSV files with a header row:

```csv
code,name,magnitude_min,magnitude_max,description
micro,Micro,0.0,1.9,"Generally not felt"
minor,Minor,2.0,3.9,"Often felt, rarely causes damage"
light,Light,4.0,4.9,"Noticeable shaking, minor damage"
moderate,Moderate,5.0,5.9,"Can cause damage to poorly constructed buildings"
strong,Strong,6.0,6.9,"Can cause severe damage in populated areas"
major,Major,7.0,7.9,"Can cause serious damage over large areas"
great,Great,8.0,9.9,"Can cause devastating damage near the epicenter"
```

DuckDB auto-detects column types from the CSV content. Headers with special characters are handled automatically.

## Loading Seeds

### Load All Seeds

```bash
havn seed
```

Seeds use change detection via content hashing. Only modified CSV files are reloaded.

### Force Reload

```bash
havn seed --force
```

Reloads all seeds regardless of whether they have changed.

### Custom Schema

```bash
havn seed --schema reference_data
```

Loads seeds into a schema other than the default `seeds`.

### With Environment Override

```bash
havn seed --env prod
```

### As Part of a Stream

```yaml
streams:
  full-refresh:
    steps:
      - seed: [all]
      - ingest: [all]
      - transform: [all]
```

## Change Detection

havn tracks seed changes using SHA256 content hashing:

1. When a seed is loaded, a hash of the CSV file content is computed
2. The hash is stored in `_havn.model_state`
3. On subsequent runs, the current hash is compared against the stored hash
4. If the hash matches, the seed is **skipped**
5. If the hash differs, the seed is **reloaded**

This makes repeated `havn seed` calls fast -- only changed CSVs are processed.

## Using Seeds in Transforms

Reference seed tables in your SQL models:

```sql
-- config: materialized=table, schema=silver
-- depends_on: bronze.earthquakes, seeds.magnitude_scale

SELECT
    e.earthquake_id,
    e.magnitude,
    m.name AS magnitude_category,
    m.description AS magnitude_description
FROM bronze.earthquakes e
LEFT JOIN seeds.magnitude_scale m
    ON e.magnitude BETWEEN m.magnitude_min AND m.magnitude_max
```

Add seeds to your `-- depends_on:` comment so havn knows about the dependency and includes seeds in the DAG visualization.

## Empty CSVs

If a CSV file is empty (contains only whitespace or newlines), havn creates an empty table with a single `empty_file BOOLEAN` column. This prevents errors in downstream models that reference the seed.

## API Reference

### List Seeds

```bash
curl http://localhost:3000/api/seeds
```

Returns a list of seed files with their names, schemas, and file paths.

### Load Seeds

```bash
curl -X POST http://localhost:3000/api/seeds \
  -H "Content-Type: application/json" \
  -d '{"force": false, "schema_name": "seeds"}'
```

## Best Practices

1. **Keep seeds small** -- Seeds are best for lookup tables and reference data (hundreds to low thousands of rows). For larger datasets, use ingest scripts.

2. **Use descriptive filenames** -- The filename becomes the table name. Use `country_codes.csv` not `data.csv`.

3. **Include headers** -- Always include a header row. DuckDB uses it for column naming.

4. **Version control seeds** -- CSV seed files should be committed to git. They are part of your project definition.

5. **Declare dependencies** -- Always add seed tables to `-- depends_on:` in your SQL models for correct DAG ordering.

6. **Use seeds for test fixtures** -- In the `test` environment with `:memory:` databases, seeds provide consistent reference data for testing.

## Related Pages

- [Transforms](transforms) -- Using seeds in SQL models
- [Pipelines](pipelines) -- Loading seeds as a pipeline step
- [Configuration](configuration) -- Project configuration
- [Lineage](lineage) -- Seeds in the dependency graph
