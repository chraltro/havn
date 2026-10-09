# Python models

Most transforms are plain SQL. Some logic reads better as code: a loop over
sorted values, a statistical estimate, a call into a Python library. A Python
model is a `.py` file in `transform/` that builds a model with a function. It
is a node in the DAG like any `.sql` file beside it: it is ordered by its
dependencies, selected by the same selectors, change-detected, built with the
same materializations, checked by the same assertions and recorded in the
same run log.

```python
# transform/silver/customer_scores.py
"""One score per customer, from their orders."""
from havn import model


@model(
    materialized="table",
    tags=["daily"],
    assertions=["unique(customer_id)", "row_count > 0"],
    columns={"score": "Sum of order amounts, halved for returns"},
)
def customer_scores(db, ref):
    orders = ref("bronze.orders")
    returns = ref("bronze.returns")
    joined = orders.join(returns, "order_id", how="left")
    return joined.aggregate(
        "customer_id, sum(amount * CASE WHEN returned THEN 0.5 ELSE 1 END) AS score"
    )
```

The model's name comes from the file (`customer_scores`) and its schema from
the folder (`silver`), exactly as for SQL. `@model(schema="...")` overrides
the schema.

## The function

A file is a model when it has a function decorated with `@model` (or
`@havn.model`), or failing that a top-level `def model(...)`. Any other `.py`
file under `transform/` is a helper module (see below); by convention helpers
start with `_`, and `_`-prefixed files are never read as models.

The function asks for what it needs by parameter name:

| Parameter | What it is |
|---|---|
| `db` | The build's DuckDB connection. |
| `ref` | `ref("schema.name")` returns a lazy DuckDB relation over a model or table, and declares the dependency. |
| `is_incremental` | `True` when the model is incremental and its table already exists, so the function can read only new rows. |
| `this` | The model's own `schema.name`, for SQL that reads the existing table. |

A function may take any subset, or `**kwargs` for all of them.

It returns one of:

- a DuckDB relation (`ref(...)`, `db.sql(...)`, `relation.filter(...)`, ...),
- a pandas DataFrame,
- a polars DataFrame,
- a pyarrow Table.

Returning `None`, a list or anything else fails the build with a message
saying what to return.

## Configuration

`@model(...)` takes the keys `@config` takes, plus the directives SQL spells
as their own lines:

| Key | Same as |
|---|---|
| `materialized` | `table` (default), `incremental` or `snapshot` |
| `schema`, `unique_key`, `incremental_strategy`, `incremental_filter`, `partition_by`, `watermark`, `on_schema_change` | the `@config` keys |
| `strategy`, `updated_at`, `check_cols`, `hard_deletes` | the snapshot keys |
| `tags` | `@config tags=` (a list or a comma string) |
| `description` | `@description` (defaults to the function's docstring, else the module's) |
| `columns` | `@col` lines, as a dict of column name to text |
| `assertions` | `@assert` lines, as a list; `"expr, severity=warn"` works as in SQL |
| `grain` | `@grain` |
| `owner` | `@owner` |
| `depends_on` | `@depends_on`, for names `ref` cannot see (below) |
| `timeout`, `idle_timeout` | seconds; the script runner's hard and idle timeouts |

The arguments are read **without running the file**: havn parses it and reads
the literals off the syntax tree. That is how `havn ls`, the DAG and change
detection know a Python model before it has ever run, and it is why every
value must be a literal (a string, number, list or dict). `@model(materialized=MAT)`
is reported as an error, not guessed at.

`view` and `ephemeral` are refused: both are stored SQL that something reads
later, and a Python model's rows only exist once its function has run.
`incremental_strategy=microbatch` is refused too: it substitutes `{start}` and
`{end}` into SQL text, which a function does not have.

## Dependencies

Dependencies come from three places, all found statically:

1. every `ref("schema.name")` call with a string literal argument;
2. `@model(depends_on=[...])`;
3. tables named in literal SQL handed to `db.sql(...)`, `db.execute(...)` or
   `db.query(...)`.

At run time `ref` only accepts names in that list, so a dependency can never
be hidden from the ordering. A name built at run time
(`ref(f"silver.{name}")`) is refused unless it is listed in `depends_on`, and
`havn validate` warns about the call.

Prefer `ref` over literal SQL. Only `ref` is redirected by
[defer](environments.md), inlines an ephemeral upstream, and is replaced by
unit-test mocks; `havn validate` says so for a table read through `db.sql`.

## Building

`havn transform`, jobs, the web UI and the scheduler all build Python models
through the same path as SQL models:

1. the file is loaded as a fresh module (an edit is picked up without a
   restart) and the function is called in-process on the build connection,
   in a supervised thread, under the hard and idle timeouts;
2. its result is staged into a TEMP table;
3. the ordinary SQL writers materialize the staged rows: `CREATE OR REPLACE
   TABLE`, the incremental strategies (`delete+insert`, `merge`, `append`,
   with `on_schema_change` and `incremental_filter`), or the snapshot merge;
4. assertions, profiling, the run log, upstream blocking and rewind
   snapshots follow exactly as for SQL.

Python models are trusted project code, like macros and ingest scripts. They
run with the permissions of the havn process.

Whatever the function prints goes to the run log and is echoed under the
model's line in the console. A failure names the model file and line first,
followed by the frames from your own code only:

```
fail  silver.customer_scores (table): Python model silver.customer_scores failed at
      transform/silver/customer_scores.py:14: KeyError: 'amount'
Traceback (your code, most recent call last):
  File "transform/silver/customer_scores.py", line 14, in customer_scores
    total = row["amount"]
```

### Incremental models

```python
@model(materialized="incremental", unique_key="event_id", incremental_strategy="merge")
def events(ref, is_incremental, this):
    src = ref("bronze.events")
    if is_incremental:
        src = src.filter(f"updated_at > (SELECT max(updated_at) FROM {this})")
    return src
```

The function decides what to read; havn decides how to write it. Incremental
models always run (an incremental with nothing new to read writes nothing),
except a plain `append` without an `incremental_filter`, which would duplicate
rows and so only runs when it changed.

## Change detection

A Python model rebuilds when its **code** changes. The fingerprint is a hash
of the file's syntax tree, so comments, blank lines, formatting and docstrings
never trigger a rebuild, and neither do the metadata keys (`tags`,
`description`, `columns`, `owner`, the timeouts). Anything that runs does.

Local helper modules count too. An `import _helpers` (or `from _helpers
import x`) that resolves to a `.py` file next to the model or at the root of
`transform/` is fingerprinted the same way, transitively, and folded into the
model's hash. Like a macro edit, a helper edit rebuilds the models that import
it, and their descendants follow in the same run.

Upstream changes cascade as for SQL: a rebuilt parent rebuilds its Python
children, and a rebuilt Python model rebuilds its children.

## Validation and the bind pass

`havn validate` reads every Python model without running it and reports:

- syntax errors, with the line;
- unknown or non-literal `@model` keys, invalid materializations, parameters
  the function asks for that havn does not provide;
- imports that are neither installed nor a local helper;
- `ref` names that are neither a model nor a table in the warehouse;
- `ref` calls with a non-literal argument, and tables read through literal
  SQL instead of `ref` (warnings).

The bind pass binds SQL and does not run Python. A Python model that has been
built stands in with its built columns, so the SQL models downstream of it
still bind; one that has never been built is reported as skipped, and so are
the models reading it, until it is built once.

## Unit tests

[Unit tests](unit-tests.md) work unchanged. The function runs for real on the
in-memory test connection, and `ref` returns the mocks:

```yaml
model: silver.customer_scores
tests:
  - name: halves returned orders
    given:
      bronze.orders:
        rows: [{order_id: 1, customer_id: 7, amount: 10.0}]
      bronze.returns:
        rows: [{order_id: 1, returned: true}]
    expect:
      rows: [{customer_id: 7, score: 5.0}]
```

## Packages

A [package](packages.md) can ship Python models. Their schemas are prefixed
like any package model, and `ref("silver.customers")` inside the package
resolves to the package's own `crm_silver.customers` without the author
writing the prefix.

## Everything else

- **Selectors**: `tag:`, `path:`, `+x`, `state:modified` and the rest work on
  Python models; `config.language:python` selects them all.
- **Lineage**: column lineage is not traced through a function. Impact
  analysis lists a Python consumer of a changed column as possibly affected,
  the schema sentinel likewise, and `havn rename-column` reports it as a
  blocker to edit by hand.
- **Diff**: `havn diff` runs the function (as a full, non-incremental build
  into a temp table) and compares the result with the built table.
- **Explain**: there is no SQL to explain; `havn explain` says so.
- **Web UI**: the new-model dialog creates Python models, the file tree marks
  them, the DAG labels them `python`, and the editor workbench shows their
  lineage, checks, columns and runs. **Preview** runs the function in the
  editor buffer on a read-only connection and shows the first rows; it needs
  both write and execute permission, since it runs code you have not saved.
- **Notebooks**: "model to notebook" and the debug notebook give a Python
  model a code cell that defines and calls the function against live tables.
