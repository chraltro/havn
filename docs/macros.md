# Python SQL Macros

havn lets you write Python functions that are callable directly in SQL. No Jinja templating — just Python functions registered as DuckDB UDFs. "Why template SQL when you have Python right there?"

## Quick Start

Create a Python file in the `macros/` directory with `@macro`-decorated functions:

```python
# macros/utils.py
from havn import macro

@macro
def mask_email(email: str) -> str:
    """Mask the local part of an email, keep domain."""
    if not email or "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    return f"***@{domain}"

@macro
def fiscal_quarter(d: str) -> int:
    """Return fiscal quarter (FY starts April)."""
    from datetime import datetime
    month = datetime.fromisoformat(d).month
    return ((month - 4) % 12) // 3 + 1
```

Use them in any SQL model:

```sql
-- transform/silver/customers.sql
-- config: materialized=table, schema=silver
-- depends_on: bronze.customers

SELECT
    customer_id,
    mask_email(email) AS masked_email,
    fiscal_quarter(created_date) AS fiscal_q
FROM bronze.customers
```

That's it. The functions are automatically discovered and registered when havn connects to the database.

## How It Works

1. On `connect()`, havn scans the `macros/` directory for `.py` files
2. Functions decorated with `@macro` are collected with their type signatures
3. Each macro is registered as a DuckDB scalar UDF via `create_function()`
4. The functions are then available in any SQL query or transform

## Type Mapping

Python type hints are mapped to DuckDB types:

| Python Type | DuckDB Type |
|-------------|-------------|
| `str` | `VARCHAR` |
| `int` | `INTEGER` |
| `float` | `DOUBLE` |
| `bool` | `BOOLEAN` |
| `datetime.date` | `DATE` |
| `datetime.datetime` | `TIMESTAMP` |
| No annotation | `VARCHAR` (default) |

## SQL Macros

You can also place `.sql` files in `macros/` containing `CREATE MACRO` statements:

```sql
-- macros/date_helpers.sql
CREATE MACRO quarter_start(d) AS
    DATE_TRUNC('quarter', d::DATE);

CREATE MACRO is_weekend(d) AS
    EXTRACT(DOW FROM d::DATE) IN (0, 6);
```

SQL macros are executed directly on connection — they don't need the `@macro` decorator.

## Listing Macros

```bash
havn macros
```

Shows all registered macros with their name, parameters, return type, source file, and documentation.

Via API:

```bash
curl http://localhost:3000/api/macros
```

## Conventions

- Files prefixed with `_` (e.g., `_helpers.py`) are skipped
- Errors in individual macro files are logged but don't prevent other macros from loading
- Macros are registered before transforms run, so they're available in all SQL models
- `havn init` creates a `macros/` directory with example functions

## Related Pages

- [Transforms](transforms) — Using macros in SQL models
- [CLI Reference](cli-reference) — `havn macros` command
- [Configuration](configuration) — Project structure
