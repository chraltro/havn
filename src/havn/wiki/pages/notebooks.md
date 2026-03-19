# Notebooks

havn includes a custom notebook format (`.dpnb`) for interactive data exploration, pipeline development, and debugging. Notebooks combine code, SQL, and markdown cells in a single file that can be executed both interactively in the web UI and as part of data pipelines.

## Web UI Experience

### Notebook Editor in Develop Tab

1. Go to the **Develop** tab and click **Notebooks** (or open a `.dpnb` file from the file tree)
2. The notebook editor displays cells in order:
   - **Markdown cells** -- Rendered as formatted text with headers, lists, and links
   - **Code cells** -- Python editor with syntax highlighting and a Run button
   - **SQL cells** -- SQL editor with DuckDB syntax highlighting and a Run button
   - **Ingest cells** -- Special code cells for data loading operations
3. **Run a single cell** -- Click the play button on any cell to execute it
4. **Run all cells** -- Click the "Run All" button in the toolbar to execute the entire notebook top-to-bottom
5. **SQL results** -- SQL cell results display as formatted tables with row counts
6. **Code output** -- Python cell output (print statements, errors) displays below the cell
7. **Add cells** -- Click the "+" button between cells to insert a new markdown, code, or SQL cell
8. **Reorder cells** -- Drag and drop cells to rearrange them
9. **Auto-save** -- Changes are automatically saved as you edit

### Creating a New Notebook

1. In the file tree, right-click the `notebooks/` directory
2. Click "New Notebook" and enter a name
3. The new `.dpnb` notebook opens with a blank markdown cell

### Debug Notebooks

When a transform fails, you can generate a debug notebook directly from the UI or CLI:

1. Note the failed model name from the Output Panel or History
2. Run `havn debug silver.customers` from the CLI
3. Open the generated `notebooks/debug_silver_customers.dpnb` in the web UI
4. Execute cells interactively to investigate the failure

## Notebook Format

`.dpnb` files are JSON documents with a list of cells:

```json
{
  "cells": [
    {
      "type": "markdown",
      "source": "# Earthquake Data Analysis\nExploring USGS earthquake data."
    },
    {
      "type": "code",
      "source": "import requests\nresponse = requests.get('https://earthquake.usgs.gov/...')\ndata = response.json()"
    },
    {
      "type": "sql",
      "source": "SELECT COUNT(*) FROM landing.earthquakes"
    },
    {
      "type": "ingest",
      "source": "db.execute('CREATE OR REPLACE TABLE landing.raw AS SELECT * FROM ...')"
    }
  ]
}
```

## Cell Types

### markdown

Rendered as formatted text in the web UI. Use for documentation, headers, and explanations:

```json
{
  "type": "markdown",
  "source": "## Data Quality Check\nVerify that all required columns have data."
}
```

### code

Python code cells. The `db` DuckDB connection is available, just like in ingest scripts:

```json
{
  "type": "code",
  "source": "import pandas as pd\nresult = db.execute('SELECT * FROM gold.summary').fetchdf()\nprint(result.describe())"
}
```

Code cells share a namespace -- variables defined in one cell are available in subsequent cells.

### sql

SQL cells that execute against the DuckDB warehouse:

```json
{
  "type": "sql",
  "source": "SELECT region, COUNT(*) AS event_count\nFROM silver.earthquake_events\nGROUP BY region\nORDER BY event_count DESC\nLIMIT 10"
}
```

SQL results are displayed as formatted tables in the web UI.

### ingest

Special code cells that write data to the warehouse. Used for data loading operations:

```json
{
  "type": "ingest",
  "source": "db.execute('CREATE SCHEMA IF NOT EXISTS landing')\ndb.execute('CREATE OR REPLACE TABLE landing.data AS ...')"
}
```

## Using Notebooks in Pipelines

Notebooks can be used as ingest or export steps in your pipeline:

```yaml
streams:
  full-refresh:
    steps:
      - ingest: [earthquakes]      # Runs ingest/earthquakes.dpnb
      - transform: [all]
      - export: [all]
```

Place notebooks in `ingest/` or `export/` directories. havn executes all code, SQL, and ingest cells in order, providing the `db` connection.

### Pipeline Execution Rules

- Markdown cells are skipped during pipeline execution
- Code and ingest cells execute with `db` pre-injected
- SQL cells execute against the DuckDB connection
- Cell execution is sequential (top to bottom)
- Errors in any cell stop execution (for ingest notebooks)

## Interactive Execution

### Running from CLI

Execute a notebook as a script:

```bash
havn run ingest/earthquakes.dpnb
```

This runs all cells sequentially, logging output to the console.

## Debug Notebooks

havn can automatically generate debug notebooks for failed models:

```bash
havn debug silver.customers
```

This creates `notebooks/debug_silver_customers.dpnb` pre-populated with:

1. **Error description** -- The error message from the run log
2. **Upstream dependency cells** -- SQL cells querying each upstream table
3. **Failing model SQL** -- The model's SQL for interactive editing
4. **Assertion diagnostics** -- If assertions failed, cells to investigate each failure

### Debug Workflow

1. A transform fails: `havn transform` shows `silver.customers: ERROR`
2. Generate a debug notebook: `havn debug silver.customers`
3. Open in the web UI: navigate to the notebook in the Develop tab
4. Execute cells interactively to identify the issue
5. Fix the SQL model and re-run: `havn transform`

### Debug via API

```bash
POST /api/notebooks/debug/{model_name}
```

Generates and returns a debug notebook for the specified model.

## Promoting Notebook SQL to Models

Convert a SQL query from a notebook into a proper transform model:

### Via CLI

```bash
havn promote notebooks/explore.dpnb --name my_model --schema silver
```

This:
1. Extracts the last SQL cell from the notebook
2. Auto-detects table dependencies
3. Creates `transform/silver/my_model.sql` with proper config comments
4. Validates the new model fits in the DAG

You can also promote from a literal SQL string or file:

```bash
havn promote "SELECT * FROM bronze.data WHERE active = true" --name active_data --schema silver
havn promote query.sql --name my_model --schema gold
```

### Via API

```bash
POST /api/notebooks/promote-to-model
Content-Type: application/json

{
  "notebook_name": "explore.dpnb",
  "model_name": "my_model",
  "schema_name": "silver"
}
```

### Model to Notebook

Convert a model back to a notebook for interactive exploration:

```bash
POST /api/notebooks/model-to-notebook/{model_name}
```

## Notebook API Reference

### Save a Notebook

```bash
POST /api/notebooks/save/{name}
Content-Type: application/json

{"cells": [...]}
```

### Create a New Notebook

```bash
POST /api/notebooks/create/{name}
Content-Type: application/json

{"cells": [...]}
```

### Run Entire Notebook

```bash
POST /api/notebooks/run/{name}
```

Executes all cells sequentially and returns results.

### Run a Single Cell

```bash
POST /api/notebooks/run-cell/{name}
Content-Type: application/json

{"cell_index": 2}
```

Executes a specific cell and returns its output.

## Conversion

Notebooks support conversion between formats:

- `.dpnb` to Python script -- Extracts code cells into a `.py` file
- Python script to `.dpnb` -- Wraps a script in a single code cell
- Model to notebook -- Converts a SQL model into an interactive notebook
- Notebook to model -- Promotes a SQL cell to a transform model

## Best Practices

1. **Use notebooks for exploration** -- Notebooks are ideal for ad-hoc analysis and debugging. For production transforms, promote queries to SQL model files.

2. **Keep pipeline notebooks focused** -- Notebooks in `ingest/` should do one thing: load data. Keep exploration in `notebooks/`.

3. **Use markdown cells** -- Document your analysis with markdown cells. Future you will thank present you.

4. **Debug with havn debug** -- When transforms fail, use `havn debug` instead of manually creating debug notebooks.

5. **Version control notebooks** -- `.dpnb` files are JSON and work well with git. They are part of your project.

6. **Share execution context** -- Variables defined in code cells are available in subsequent cells, so build up analysis incrementally.

## Related Pages

- [Transforms](transforms) -- SQL models (the production target for promoted queries)
- [Pipelines](pipelines) -- Using notebooks in pipeline steps
- [Getting Started](getting-started) -- Project structure overview
- [CLI Reference](cli-reference) -- Notebook-related commands
- [API Reference](api-reference) -- Notebook API endpoints
