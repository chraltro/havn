# Branch Warehouses

A warehouse per git branch. With branch warehouses on, checking out a git branch also switches to that branch's data. The branch's warehouse starts empty and costs nothing to create: every model the branch has not built is read, read-only, from the **base** warehouse (production, usually). Only what the branch changes is ever materialized in it.

## Turning it on

```yaml
# project.yml
branches:
  enabled: true
  base: prod                          # environment whose warehouse is the base
  # main: [main]                      # branches that keep the normal resolution
  # path: .havn/branches/{branch}.duckdb
```

- `base` must be a defined environment. Leave it out in a project without environments: the top-level `database.path` is the base.
- `main` defaults to `main`, or `master` in a repository without a `main`.
- `path` must contain `{branch}`. Branch names are made filename-safe: `feature/x` becomes `feature-x-217d2bf5.duckdb`.

## Which warehouse is used

1. `havn deploy` and `havn serve --auth` never use a branch warehouse.
2. `--name <branch>` on `havn branch` commands, or `HAVN_BRANCH`, forces that branch.
3. `--env` and `.havn-env` win over the checkout (`havn env reset` goes back to following git).
4. No repository, a detached HEAD, or a main branch: the normal resolution.
5. Any other branch: its own warehouse, deferring to the base.

`havn env show` and `havn branch status` say which applies and why.

## Working on a branch

```bash
git checkout -b feature/exclude-refunds
havn branch build          # state:modified+ against the base, everything else read from it
havn branch status         # built here, read from the base, stale, needing a build
havn branch diff           # rows and schema of every branch model vs the base
havn branch diff --markdown
```

- **Build** plans `state:modified+` judged against the base's build state. A branch copy that matches the base again (a reverted change) is pruned so it reads through to the base. A view that reads the base is built as a table, because the base is only attached while a build runs.
- **Stale**: a branch model is stale when the base rebuilt one of its base-read upstreams, or loaded new source data, after the branch model was built.
- **Diff** statuses: `changed`, `added`, `removed`, `not_built`, `unchanged`. Modified rows need a key (`-- havn:primary_key = id`, `models.<name>.primary_key`, or the model's `unique_key`).
- **Clean up** with `havn branch list`, `havn branch clean` (merged or deleted branches) and `havn branch reset` (this branch). The base and environment warehouses are never deleted.

## In the web UI

`havn serve` follows checkouts: it moves to the new branch's warehouse as soon as nothing is using the current one (no pipeline, deploy, change build or other request), and the Git panel's checkout switches at once. The top bar names the branch, with a `defer:` badge for the base; amber "switching…" means the server is waiting for a build to finish.

**Ship → Data changes on this branch** shows what is built locally, a **Build branch** button, and the per-model row and schema diff with sample rows. **Copy as markdown** copies the pull-request comment.

## Pull requests in CI

`havn ci generate` writes `havn-base.yml` (builds the base on every push to main and uploads it as the `havn-base` artifact) and `havn-ci.yml` (on every pull request: download the base, `havn branch build --base`, `havn branch diff --markdown`, `havn ci comment`). The comment is updated in place on later pushes.

## Limits

- DuckDB backend only; a DuckLake project never resolves to a branch warehouse.
- Ad-hoc queries see only what the branch warehouse holds; models it has not built are read from the base during builds. Query the base with `--env prod`.
- `havn serve --auth` does not follow branches: users, tokens and masking policies live in the warehouse.
- The base must not be open for writing in another process; `havn branch build --defer-snapshot` reads a copy.

## Related Pages

- [Environments](environments)
- [Pull Requests](pull-requests)
- [CLI Reference](cli-reference)
