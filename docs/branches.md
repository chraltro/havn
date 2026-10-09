# Branch warehouses

A warehouse per git branch. With branch warehouses on, checking out a git
branch also switches to that branch's data: `havn transform`, `havn query`,
the web UI and every other command work against a warehouse file of the
branch's own. It starts empty and costs nothing to create, because every
model the branch has not built is read from the **base** warehouse
(production, usually) through [defer](environments.md#defer). Only what the
branch changes is ever materialized in it, and the base is only ever opened
read-only.

A pull request then shows the row-level data diff next to the code diff, in
CI and in the web UI.

## Turning it on

```yaml
# project.yml
environments:
  dev:
    database:
      path: dev.duckdb
  prod:
    database:
      path: prod.duckdb

branches:
  enabled: true
  base: prod                          # environment whose warehouse is the base
  # main: [main]                      # branches that keep the normal resolution
  # path: .havn/branches/{branch}.duckdb
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Resolve the warehouse from the checked-out branch. |
| `base` | top-level `database.path` | Environment whose warehouse unbuilt models are read from. Must be a defined environment. Leave it out in a project without environments. |
| `main` | `main`, or `master` when there is no `main` | Branch name, or list of names, that never get a branch warehouse. |
| `path` | `.havn/branches/{branch}.duckdb` | Where branch warehouses go. Must contain `{branch}`. |

`havn init` already ignores `.havn/branches/` (and `*.duckdb`) in git.

## Which warehouse is used

First match wins:

1. `havn deploy`, and `havn serve --auth`: never a branch warehouse (see
   below).
2. `--name <branch>` on a `havn branch` command, or the `HAVN_BRANCH`
   environment variable: that branch's warehouse, whatever is checked out.
3. `--env <name>` or `.havn-env` (from `havn env use`): that environment.
   An explicit choice wins over the checkout; `havn env reset` goes back to
   following git.
4. No git repository, or a detached HEAD: the normal resolution.
5. One of the `main` branches: the normal resolution.
6. Any other branch: `.havn/branches/<branch>.duckdb`, deferring to the base.

`havn env show` and `havn branch status` print the result and, when no branch
warehouse is used, why not.

Branch names are made filename-safe. A plain lowercase name (`fix-orders`)
is used as it is; anything else (`feature/x`, `Fix`, a Windows device name,
a very long name) is flattened and given 8 hex characters of the name's hash,
so `feature/x` becomes `feature-x-217d2bf5.duckdb` and never collides with a
branch called `feature-x`. A small `<file>.branch.json` beside each warehouse
records the real branch name.

## Working on a branch

```bash
git checkout -b feature/exclude-refunds
# edit transform/silver/orders_enriched.sql
havn branch build          # build what changed, read everything else from prod
havn branch diff           # rows and schema of every branch model vs prod
havn branch status         # what is built here, what is read from prod, what is stale
```

`havn branch build` builds `state:modified+` *judged against the base*: every
model whose SQL or upstream differs from what the base last built, plus
everything downstream of those. Everything else is read from the base. Two
details keep the branch honest:

- A model whose branch copy matches the base again (you reverted the change)
  is **pruned**: dropped from the branch warehouse so it reads through to the
  base instead of shadowing it with old data. `--no-prune` keeps it.
- A view that reads the base is built as a table on the branch. The base is
  attached only while a build runs, so a view pointing at it would fail on
  every later query.

`--plan` shows what would be built and pruned without changing anything;
`--force` rebuilds every planned model even if the branch copy is current;
`--defer-snapshot` reads the base through a consistent copy when a job holds
it open for writing.

Plain `havn transform` works too and also defers to the base, but it builds
what you ask for: with no selector, that is every model.

### Status and staleness

`havn branch status` lists the models built on the branch, the ones read
from the base, the planned models that still need a build, the prunable ones
and the **stale** ones. A branch model is stale when the base rebuilt one of
the upstreams it reads from the base after the branch model was built, or
loaded new source data after it. `havn branch build` brings it up to date.
`--json` gives the same as data.

### Diff

`havn branch diff` compares every model the branch built or changed with the
same model in the base, using the [diff engine](transforms.md): schema changes
(added, removed and retyped columns), and added, removed and modified rows.
Modified rows need a key: `-- havn:primary_key = id`, `models.<name>.primary_key`
in project.yml, or the model's `unique_key`. Statuses:

| Status | Meaning |
|---|---|
| `changed` | Rows or schema differ from the base. |
| `added` | New on the branch; the base has no such model. |
| `removed` | The base built it; the branch no longer defines it. |
| `not_built` | Planned for the branch but not built yet. |
| `unchanged` | Rebuilt on the branch with identical data. |

`--markdown` prints the pull-request comment (`--output file.md` writes it);
`--json` the full report; `--full` every changed row instead of samples;
`--exit-nonzero-on-change` exits 2 when anything differs.

### Cleaning up

```bash
havn branch list           # warehouses on disk, and whether their branch still exists
havn branch clean          # delete warehouses of branches merged into main or deleted
havn branch clean --dry-run
havn branch reset          # delete this branch's warehouse; it reads everything from the base again
```

`clean` never removes the current branch's warehouse, and every delete
refuses a file that is the base, an environment's warehouse, or does not
match `branches.path`. A branch with no commits of its own counts as merged;
its warehouse only held main's code anyway.

## The web UI

`havn serve` follows checkouts. It checks `.git/HEAD` at most once a second
and moves to the new branch's warehouse when nothing is using the current
one: no pipeline, deploy or change build running and no other request open.
Until then the top bar shows the branch in amber ("switching…") and the
switch happens on a later request. A checkout from the Git panel switches at
once.

The top bar names the checked-out branch: in the accent colour on a branch
warehouse, next to a `defer: <base>` badge whose dot says whether the base
can be read right now. When the warehouse moves, the tables and files reload
and the Output panel says where the data is now. The Home page calls models
the branch has not built "from base".

**Ship → Data changes on this branch** shows the same as `havn branch status`
and `havn branch diff`: what is built locally, what needs a build, a **Build
branch** button, and the per-model diff with schema changes and sample rows.
**Copy as markdown** copies the pull-request comment.

The API: `GET /api/branch`, `GET /api/branch/status`, `POST /api/branch/build`,
`POST /api/branch/diff`, `GET /api/branch/list`; `GET /api/environment`
carries the branch too.

## Pull requests in CI

```bash
havn ci generate
```

writes two GitHub Actions workflows:

- **`havn-base.yml`** runs on every push to main. It builds the base
  warehouse (`havn jobs run full-refresh --env prod`, or `havn transform`
  when there is no full-refresh job) and uploads it as the `havn-base`
  artifact. Change the build step to match how production is built, or to
  copy a recent backup of it.
- **`havn-ci.yml`** runs on every pull request. It downloads the newest base
  artifact, runs `havn branch build --base <artifact>`, renders
  `havn branch diff --markdown` and posts it with `havn ci comment`, which
  updates its earlier comment on later pushes instead of adding one each
  time. The PR checkout is a detached merge commit, so the workflow names
  the branch with `HAVN_BRANCH: ${{ github.head_ref }}`.

The same two commands work anywhere, given a base file:

```bash
havn branch build --name feature/x --base /backups/prod.duckdb
havn branch diff  --name feature/x --base /backups/prod.duckdb --markdown > diff.md
```

The in-app change build (Ship → Build) keeps diffing against main's
warehouse even while the server is on a branch warehouse.

## Deploys

Deploying is unchanged: `havn deploy prod` merges into production as before.
A branch warehouse is never a deploy target; deploy resolves environments as
if no branch were checked out.

## Limits

- **DuckDB backend only.** A DuckLake project never resolves to a branch
  warehouse (`havn branch status` says so). DuckLake's snapshots allow
  read-only time travel but not a writable fork of a catalog, and defer
  cannot attach a DuckLake base yet; branch warehouses on DuckLake would
  need both.
- **No read-through for ad-hoc queries.** Queries in the web UI and `havn
  query` see what the branch warehouse holds. Models the branch has not
  built are read from the base during builds, not by ad-hoc queries, because
  masking policies live in each warehouse and a read-through would show base
  data without the base's policies. Query the base with `--env prod`.
- **`havn serve --auth` does not follow branches.** Users, tokens and masking
  policies live in the warehouse, and a new branch warehouse has none:
  following a checkout would sign everyone out and offer first-run admin
  setup to the next visitor.
- **The base must be readable.** DuckDB refuses a read-only attach while
  another process writes the file; `havn branch build --defer-snapshot`
  reads a consistent copy instead.
- **Scheduled jobs follow the checkout.** `havn serve --schedule` without
  `--env` runs jobs against whatever the checkout resolves to. Start a
  production scheduler with `--env prod`.
