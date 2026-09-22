# Unit Tests

Assertions and contracts check the data that is currently in the warehouse.
A unit test checks the **SQL itself**: you give a model fixed input rows and
declare the rows it should produce.

Each test runs on a throwaway in-memory DuckDB database with your project's
macros registered. Nothing is read from the warehouse and nothing is written
to it, so a unit test cannot pass because of what happens to be built, and it
works on a clone that has never run a pipeline.

```bash
havn test                      # run every unit test
havn test --model silver.customers
havn test -v                   # show the rows that differ
havn check                     # validation + assertions + contracts + unit tests
```

## File format

Unit tests live in `tests/unit/*.yml` (or `.yaml`) inside your project.
`havn init` scaffolds one worked example there.

```yaml
# tests/unit/customers.yml
model: silver.customers

tests:
  - name: counts orders per customer
    description: Customers with no orders must still appear, with a zero count.
    given:
      bronze.customers:
        columns: {customer_id: INTEGER, name: VARCHAR}
        rows:
          - [1, "Ann"]                       # list form: columns order
          - {customer_id: 2, name: "Bo"}     # mapping form: by name
      bronze.orders:
        rows:
          - {order_id: 1, customer_id: 1, amount: 5.0}
    expect:
      rows:
        - {customer_id: 1, name: "Ann", order_count: 1}
        - {customer_id: 2, name: "Bo", order_count: 0}
      ordered: false                          # the default
```

| Key | Meaning |
| --- | --- |
| `model` | The model under test, `schema.name`. Can be set per test instead of per file. |
| `tests[].name` | Free text, unique within the model. |
| `tests[].description` | Optional. Say why the case matters, not what it does. |
| `given` | One entry per upstream table. Every upstream must be here. |
| `given.<table>.columns` | Optional column name to DuckDB type mapping. Recommended. |
| `given.<table>.rows` | Lists (positional) or mappings (by name). |
| `expect.rows` | The rows the model must produce. |
| `expect.columns` | Required only when `expect.rows` uses the list form. |
| `expect.ordered` | `true` compares row N to row N. Default `false`. |

!!! note "Quoting parameterized types"
    In YAML flow style, `{amount: DECIMAL(10,2)}` splits on the comma. Write
    `amount: "DECIMAL(10,2)"`, or use block style.

### CSV fixtures

Wide tables read better as CSV, the same as in dbt. Use `format: csv` with a
`csv:` block, on `given` entries and on `expect` alike:

```yaml
    given:
      bronze.orders:
        columns: {order_id: INTEGER, customer_id: INTEGER, amount: DOUBLE}
        format: csv
        csv: |
          order_id,customer_id,amount
          1,1,5.0
          2,1,7.5
    expect:
      format: csv
      csv: |
        customer_id,total
        1,12.5
```

An empty field is NULL. Fields that look like numbers become numbers;
everything else stays a string and is cast to the column's type on insert.

## How a test runs

1. A fresh `:memory:` DuckDB connection is opened and your `macros/` are
   registered on it, so Python macros work exactly as they do in a build.
   The connection is then locked down: `enable_external_access` is turned off
   and the configuration is locked, so nothing that runs on it afterwards can
   reach the filesystem or the network.
2. One `TEMP TABLE` is created per `given` entry.
3. Every reference to a mocked table in the model's SQL is rewritten to point
   at its mock, preserving aliases and leaving CTE names alone.
4. The rewritten query runs into a temp table and is compared to `expect`.

### Column types

Types come from the first of these that is available:

1. the `columns:` block on the mock,
2. the live warehouse catalog, when a warehouse exists (the CLI, API and MCP
   all pass one in),
3. the Python types of the fixture values (`1` is BIGINT, `5.0` is DOUBLE,
   `"x"` is VARCHAR, a bare date is DATE).

Declaring `columns:` is worth the lines. It pins the fixture to the shape you
mean, and it keeps the test reproducible on a machine where the warehouse has
not been built.

### Comparison

Rows are compared by hashing every column as text, the same comparator
`havn diff` uses. That means DECIMAL versus DOUBLE, or a `SUM` coming back
wider than you wrote in the fixture, cannot fail a test on their own:
expected rows are cast into the model's own output types before hashing.

Comparison is a multiset by default, so order does not matter but duplicates
do: two identical output rows need two identical expected rows. Set
`ordered: true` to compare positionally, which is what you want for a model
whose `ORDER BY` is part of its contract.

A failure reports three things: rows that were expected but not produced,
rows that were produced but not expected, and column-set mismatches.

## Warehouse-free, on purpose

If a model reads an upstream that has no `given` entry, the test is an
**error**, not a silent read of the real table:

```
error  no mock for upstream bronze.orders: every upstream must be mocked,
       unit tests never read the warehouse
```

This is the property that makes the result mean something. dbt's local unit
tests still need the direct upstream models to exist in the warehouse so the
schema can be fetched; havn's do not, because the engine and the warehouse
are the same thing and the mocks carry their own types.

### And file-free, for the same reason

The test connection cannot read or write files either. A model under test
that calls `read_csv`, `read_text`, `glob` or a bare `FROM '<path>'`, or that
tries `COPY ... TO`, `ATTACH`, `INSTALL` or `LOAD`, fails its test with a
permission error rather than touching the disk. `havn test` runs on the
server too, where the SQL being tested is a file anyone with write permission
just saved, so the run gets no filesystem at all.

Nothing legitimate needs it: fixture rows are the input a unit test is for.
If a test seems to need a file read, put the rows it would have read into a
`given` block, in the inline `csv:` form when there are a lot of columns.

## Limitations

- **Incremental models are tested as a full refresh.** The model's query runs
  as written; `incremental_filter`, `unique_key` and the merge strategy are
  not exercised, because there is no prior state to merge into. Test the
  transformation logic here and the incremental behaviour with `havn diff`.
- **`SELECT *` models are only as wide as the mock.** A mock that declares
  three columns makes a star model produce three columns, and the test then
  passes against a narrower fiction. When a warehouse is available, havn warns
  about this:

  ```
  warn  mock for bronze.customers omits 2 column(s) present in the warehouse
        (name, email); a SELECT * model is being tested against a narrower table
  ```

  The warning only appears for models that actually project a star.
- **Unparseable SQL cannot be tested.** Dependency extraction can fall back to
  regex, but rewriting cannot: a partial rewrite would point some references
  at the real warehouse. A model sqlglot cannot parse fails its test with a
  clear message instead.
- **Sources and seeds are mocked like anything else.** There is no special
  handling; mock them by name under `given`.

## API

```bash
# List declared tests (read permission)
curl http://localhost:3000/api/unit-tests

# Run them (execute permission)
curl -X POST http://localhost:3000/api/unit-tests/run \
     -H 'Content-Type: application/json' \
     -d '{"model": "silver.customers"}'
```

The web UI has the same thing under **Observe > Unit Tests**, and AI agents
can call the `run_unit_tests` MCP tool.

## Related Pages

- [Data Quality](quality) -- Assertions, profiling, freshness
- [Contracts](contracts) -- Standalone YAML rules against built data
- [Transforms](transforms) -- Model configuration and directives
- [Macros](macros) -- Python functions callable from model SQL
