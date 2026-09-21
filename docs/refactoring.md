# Refactoring

Renaming a column is not a find-and-replace. The same word names different
things in different models, a downstream model can filter or join on a column
it never projects, and a model that re-exports the column under the same name
passes the rename on to its own children. havn indexes every place the column
is actually written, rewrites exactly those, and reports the places it cannot
see through instead of guessing at them.

## Find column references

The index on its own, with nothing written:

```bash
havn rename-column silver.customers customer_id cust_id --dry-run
```

```
                  silver.customers.customer_id
 file                              line  kind        clause
 transform/silver/customers.sql       5  definition  select
 transform/gold/customer_report.sql   3  reference   select
 transform/gold/customer_report.sql   5  reference   where
 transform/gold/orders.sql            9  reference   join
 transform/gold/orders.sql           12  reference   group

5 edits in 3 files: customer_id to cust_id
--dry-run: nothing written.
```

In the web editor, right-click a column and choose **Find column references**.
The sites appear in a panel under the editor; clicking one opens that file at
that line.

The `clause` column is the point of the exercise. A `where`, `join` or `group`
row is a model that reads the column without projecting it, which column
lineage alone does not report and a text search cannot tell apart from an
unrelated column of the same name.

### The kinds

| Kind | Meaning |
|---|---|
| `definition` | The identifier that gives the model its output column: the alias when the projection is aliased, the column itself when it is not. |
| `reference` | A downstream model reads the column, anywhere in the query. |
| `alias` | A downstream model re-exports the column under a different name. The rename stops there; that model's own children keep the new name, so they are not touched. Reported, never edited. |
| `yaml` | A metrics or contracts YAML file names the column in plain text. |

## Rename

```bash
havn rename-column silver.customers customer_id cust_id
```

It prints the same table, then asks before writing. In the editor, press `F2`
on the column.

The rename follows the column downstream only where it keeps its name. Given

```sql
-- transform/silver/customers.sql
SELECT c.customer_id, COUNT(o.order_id) AS order_count
FROM bronze.customers c
LEFT JOIN bronze.orders o ON c.customer_id = o.customer_ref
GROUP BY c.customer_id
```

renaming `bronze.customers.customer_id` rewrites all three mentions here, and
because `silver.customers` re-exports `customer_id` under its own name, it
carries on into everything that reads `silver.customers`. A model that writes
`SELECT customer_id AS account_id` ends the chain: its output is still
`account_id`, and its own consumers are left alone.

CTEs inside a model are followed the same way, one definition at a time.

### Writing

Every file is written or none of them is:

1. Every edit is re-read from the file and checked against the identifier it
   claims to replace. One mismatch and nothing is written.
2. Each file goes to a temporary neighbour and is renamed into place.
3. If a write fails part way through, the files already written are put back.

A read-only file is refused rather than replaced.

## What it refuses

These come back as blockers, with the file and the reason. `--force` renames
everything else and leaves them alone, which is the honest outcome: the places
below need a person.

| Blocker | Why |
|---|---|
| `select_star` | A model expands `SELECT *` over a relation that carries the column, with or without `EXCLUDE` or `REPLACE`. A star yields no identifier, so there is nothing to rewrite, and the model's output column silently changes name when its upstream is renamed. |
| `columns_expression` | `COLUMNS(...)` expands to columns that are never written out. |
| `union_by_name` | `UNION BY NAME` matches columns by name at run time, so renaming one branch changes what the query means. |
| `opaque_relation` | The model reads from a function rather than a table (a table macro, `read_csv`), whose columns are not visible. |
| `unresolved_reference` | An unqualified column that could belong to more than one relation in scope, with no schema to settle it. Build the project, or qualify the reference. |
| `yaml_mention` | A `metrics/*.yml` or `contracts/*.yml` file names the column. YAML is matched as plain text, which is precise enough to edit but too weak to apply unattended. |
| `no_definition` | The model has no output column by that name. |

In the editor a rename with blockers shows them in a dialog first, and does
nothing unless you confirm.

## After a rename

```bash
havn validate
```

The bind pass resolves every model through the DuckDB binder, which is what
catches a column the rename could not reach. Then rebuild:

```bash
havn transform state:modified+
```

## API

| Endpoint | Permission | Purpose |
|---|---|---|
| `GET /api/rename/references?model=&column=` | read | The sites and the blockers. |
| `POST /api/rename/plan` | read | The edits and the file contents they produce. Writes nothing. |
| `POST /api/rename/apply` | write | Applies them. `409` when a file changed since the plan. |
| `PUT /api/files` | write | Write several files as one unit, with a per-file hash check. |

```bash
curl "http://localhost:3000/api/rename/references?model=silver.customers&column=customer_id"
```

```json
{
  "model": "silver.customers",
  "column": "customer_id",
  "sites": [
    {
      "model": "silver.customers",
      "path": "transform/silver/customers.sql",
      "line": 5, "col": 18, "start": 61, "end": 72,
      "clause": "select", "kind": "definition",
      "resolved": true, "text": "customer_id", "needs_alias": false
    }
  ],
  "blocked": [],
  "models": ["silver.customers", "gold.customer_report"]
}
```

`start` and `end` are character offsets into the file, `end` exclusive, so
`content[start:end]` is the identifier.

## Related Pages

- [Lineage](lineage.md) -- column lineage and impact analysis
- [Transforms](transforms.md) -- the SQL model format
- [CLI Reference](cli-reference.md) -- `havn rename-column`
- [Limitations](limitations.md) -- what the editor does and does not do
