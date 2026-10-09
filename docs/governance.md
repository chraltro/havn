# Governance: row-level security, lineage-aware masking, governed Python

havn governs what a person sees at query time. Three things work together:

- **Column masking** (see `masking.md`): policies per `schema.table.column`.
- **Row-level security**: row policies per `schema.table`.
- **Lineage**: both follow column lineage into downstream models.

Every surface that hands warehouse rows to a person goes through one function,
`havn.engine.governance.govern_query(sql, viewer, conn)`: `/api/query`, CSV
export, EXPLAIN / EXPLAIN ANALYZE, table samples and profiles, dashboard
widgets, semantic-layer metrics, collaboration sessions, step previews, model
notebook views, notebooks run through the server, governed Python and the
`/v1/sql` statement API.

## Row policies

A row policy is a SQL boolean filter over one table's columns. It applies to
the roles and users it names (none named means everybody), minus its
exemptions (`admin` by default). Several policies that apply to the same
viewer on the same table are ANDed. A viewer no policy applies to is not
restricted.

The filter reads the viewer through functions that are replaced by literals
before the query runs:

| Function | Value |
|---|---|
| `havn_user()` | the viewer's username |
| `havn_role()` | the viewer's role |
| `havn_attr('region')` | a user attribute, NULL when unset (so the row is hidden) |
| `havn_attr_list('regions')` | an attribute as `VARCHAR[]` |

`{user.username}`, `{user.role}` and `{user.<attribute>}` are accepted as
shorthand, quoted or not.

```bash
havn rls add -t silver.customers -f "region = havn_attr('region')" --roles viewer,editor
havn rls list
havn rls remove --id <id>
```

API (admin only, `manage_users`, audit logged):

- `GET/POST /api/governance/row-policies`, `PUT/DELETE /api/governance/row-policies/{id}`
- `PUT /api/users/{username}/attributes` with `{"attributes": {"region": "north"}}`
- `POST /api/governance/preview` with `{"username", "sql"}`: the rows that user would see
- `GET /api/governance`: classifications, inherited policies, declassifications

Policies live in `_havn.row_policies`; attributes in `_havn.users.attributes`.

### How it is enforced

The rewriter resolves every relation the way DuckDB does (quoting, case,
`catalog.schema.table`, unqualified names), inlines every view whose body
reads a protected table, and replaces each protected table reference with
`(SELECT * FROM t WHERE <filter>) AS t`. Aliases, CTEs, subqueries, joins,
UNION, LATERAL and correlated subqueries all read the filtered rows. Then
DuckDB plans the rewritten query (`EXPLAIN`) and the plan's table scans are
checked against what the rewrite accounts for; an extra scan of a governed
table (a path the rewriter did not see) refuses the query.

Refused for governed viewers: `_havn.*` (non-admins), SQL macros whose body
reads a governed table, `pragma_storage_info`, dynamic `PIVOT` over governed
data, views or models whose lineage could not be traced while they read
masked columns, and anything that does not parse while naming governed data.

UPDATE and DELETE by a governed user touch only the rows they see, and may not
read masked columns.

## Masking and row policies that follow lineage

Every model build records its column lineage in `_havn.model_lineage` (what a
table holds is what its model said when it was built). At query time:

- A column derived from a masked column (directly or through expressions)
  inherits the source policy's masking method and exemptions. Several masked
  sources: the strongest method, the intersection of exemptions.
- A column can be classified without a policy: `@pii email, phone` in the model.
  The classification propagates the same way, for reporting.
- `@declassify col: <reason>` stops inheritance at that column (`@declassify *`
  for all columns).
- A model reading a row-protected table carries the row filter when it passes
  the filter's columns through unchanged (renames are followed). If it does
  not (it aggregates them away), the policy's subjects see **no rows** of it,
  unless the model says `@declassify rows: <reason>`.
- Views are not inherited onto: they are inlined, so their rows are filtered at
  the base table and aggregates over them are computed over visible rows.

`havn pii [schema.table]` lists classified columns (source, origin, masking),
declassifications and inherited row policies. `havn validate` and `havn check`
warn about a classified column without masking in `governance.pii_schemas`
(default `gold`), an exposure, or a table an export script reads; about models
whose governed readers see no rows; and about untraceable lineage.

Declassification is a model author's statement. Editors write models, so an
editor can declassify; declassifications are listed by `havn pii` for review.

## Governed Python

A script or notebook run from the web UI or API by a user that masking or row
policies apply to runs in a **separate process**. Its `db` is a proxy: each
statement goes to the server over an authenticated local socket (JSON plus
Arrow frames, never pickle) and is executed there for that user:

- reads are governed; `CREATE TABLE ... AS`, `INSERT ... SELECT` and `COPY ... TO`
  write what the user may read (masked, filtered);
- writes their role allows work as before (ingest into `landing`, etc.);
- DataFrames referenced in SQL (`SELECT * FROM df`) are found in the caller's
  scope and shipped as Arrow, so replacement scans keep working;
  `db.register`, `db.sql(...).df()`, `fetchall/fetchone/fetchdf/arrow/pl`,
  `executemany`, `read_csv/read_parquet` relations are supported;
- refused: macros, functions and secrets, PRAGMA/CALL, USE, SET VARIABLE and
  most settings (credentials for remote readers are allowed), prepared
  statements, EXPORT/IMPORT DATABASE, ATTACH except postgres/mysql/sqlite,
  INSTALL/LOAD from a path, and any path naming the warehouse, its WAL,
  `.havn/` (Pipeline Rewind snapshots) or `_backups/`.

Inside the child an audit hook blocks `import duckdb`, ctypes calls, process
creation, and opening the warehouse, WAL, snapshots or backups through Python
file APIs. The hard timeout, idle timeout, stdout capture and circuit breaker
apply as for in-process scripts; a timed-out child is killed (no orphans).

Notebook code cells run in a per-user, per-notebook governed kernel (idle
kernels stop after 30 minutes); SQL and ingest cells run governed on the
server. A governed user's notebook outputs are not saved into the `.dpnb`
file, and saved outputs are withheld from governed users opening it.

Not governed (in-process, as the system): CLI runs, scheduled jobs, the file
watcher, and runs by admins or by users no policy applies to. Jobs started
from the UI or API run their script steps as the user who started them.

```yaml
governance:
  python: subprocess      # or: refuse (governed users cannot run Python)
  isolation: best_effort  # or: strict (refuse where the OS cannot isolate the file)
  pii_schemas: [gold]
```

### Residual risk (read this)

- **Windows**: the server holds the warehouse file and its WAL with an
  exclusive lock; the child cannot open either at the OS level (checked at
  startup). **Linux/macOS**: the child runs as the same OS user and could read
  the file with native code; the audit hook stops Python-level access only.
  The run log says so; `isolation: strict` refuses governed Python there.
- Native code in already-loaded extensions (e.g. `pyarrow.parquet.read_table`
  on a Pipeline Rewind snapshot file in `.havn/`, or a backup copy) is not
  seen by the audit hook on any OS. Keep snapshots and backups out of reach
  of governed users if that matters, or set `governance.python: refuse`.
- Python files in `macros/` run inside the server process. An editor who can
  write them can run code as the server.
- Model authors (editors) are trusted with what their models compute:
  declassification, and SQL that hides a dependency from lineage (a model
  whose lineage cannot be traced is refused to governed readers when it reads
  masked columns, but row inheritance relies on its recorded reads).
- Scheduled jobs run as the system; anyone who can edit job files and scripts
  can schedule code that runs unrestricted.
- Lines a script prints are visible in run history and the pipeline event
  stream to readers, as before.
- Other closed paths: raw warehouse export (`/v1/export/duckdb`) and the AI
  agent sidebar are refused to governed users; `/v1/sql` results are visible
  only to the user who ran them; diff samples, assertion details and
  collaboration history from other users are withheld from governed users.
