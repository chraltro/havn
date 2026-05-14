## havn - Self-Hosted Data Platform

havn (Norwegian for harbour) uses DuckDB + plain SQL transforms + Python ingest/export. All data in a single `warehouse.duckdb` file. Data in safe waters.

### SQL models go in `transform/` with `@config` directive at top:

```sql
@config materialized=table, schema=silver
SELECT * FROM bronze.customers WHERE active = true
```

Folder name = default schema. Dependencies (e.g. `bronze.customers`) are auto-extracted from `FROM`/`JOIN` clauses; add `@depends_on` only when the parser can't see the reference. Other directives: `@assert`, `@description`, `@col`. No Jinja, plain SQL only. Legacy `-- config:` / `-- depends_on:` / `-- assert:` syntax still parses for back-compat.

### Python scripts go in `ingest/` or `export/`. Top-level code, with `db` (DuckDB connection) pre-injected:

```python
db.execute("CREATE OR REPLACE TABLE landing.x AS SELECT * FROM ...")
```

The legacy `def run(db): ...` form still works.

### Key commands: `havn transform`, `havn run <script>`, `havn query "<sql>"`, `havn lint`, `havn tables`

### Code patterns:
- `from __future__ import annotations` in all Python files
- Lazy imports in CLI commands (`src/havn/cli/`)
- DuckDB connections: always `try/finally` with `conn.close()`
- Tests: `pytest tests/` -- uses real temp DuckDB, no mocks
- API: FastAPI in `src/havn/server/app.py`, auth via `_require_permission()`

### Don't:
- Add Jinja/templating to SQL
- Add TypeScript to the frontend
- Mock DuckDB in tests
- Modify `_havn` schema from user-facing code
