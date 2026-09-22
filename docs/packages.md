# Packages

A package is a directory laid out like a havn project that other projects
install and build against. It carries SQL models under `transform/` and macros
under `macros/`, and nothing else. Use one when the same staging models or the
same business logic have started being copy-pasted between projects.

Packages come from a git repository pinned to a revision, or from a local
directory. They are installed into `havn_packages/`, which is gitignored; what
travels with your repository is `havn_packages.lock`, which records the exact
commit each package resolved to.

## Installing

Declare what you need in `project.yml`:

```yaml
name: my-project

packages:
  - name: crm
    git: https://github.com/example/havn-crm.git
    rev: v1.4.0
  - name: shared
    path: ../shared-models
```

Then:

```bash
havn packages install          # fetch what the lock says, or resolve the rev
havn packages install --upgrade  # re-resolve each rev and rewrite the lock
havn packages list             # what is installed, with commit and counts
havn packages remove crm       # delete the checkout and the lock entry
```

Each entry needs a `name`, plus either `git` and `rev`, or `path`. Names must
be valid identifiers, because the name becomes part of a SQL schema, and they
must be unique within a project.

`havn packages remove` deletes the checkout but leaves `project.yml` alone. Take
the `packages:` entry out yourself, or the next install brings the package back.

### Which sources a `git:` entry may name

A `git:` URL must be `https://`, `ssh://`, or the scp-like `git@host:path`.
Nothing else is accepted, and the install fails with the reason:

- `ext::<command>` tells git to run that command as the transport, which would
  execute whatever a `project.yml` asked for on the machine installing it.
- `file://` and a bare local path turn a package entry into a read of the
  installing machine's own disk. A package that lives on this machine is a
  `path:` entry, which is resolved against the project directory.
- `git://` and `http://` are unauthenticated cleartext, so the code that ends
  up being imported is whatever the network returned.

## Pinning

`rev` is required for a git package, and it can be a tag, a commit SHA or a
branch. A branch is accepted and warned about, because a branch moves under
you and is therefore not a pin:

```
  ok  crm 9c1f2a0b4d33
        rev 'main' is a branch, not a pin: it moves under you.
        Use a tag or a commit for a reproducible build.
```

Prefer a tag. havn tries a shallow clone of the rev first, which covers tags
and branches in a single fetch, and falls back to a full clone plus a checkout
for a commit SHA.

## The lock file

`havn_packages.lock` sits next to `project.yml` and **should be committed**:

```yaml
version: 1
packages:
  - name: crm
    source: git
    git: https://github.com/example/havn-crm.git
    rev: v1.4.0
    commit: 9c1f2a0b4d33f1e8c2a7b6d5e4f3a2b1c0d9e8f7
```

Two things follow from that:

- A second `havn packages install` fetches the **locked commit**, not the rev.
  If the tag has since been moved, or the branch has advanced, your build does
  not change until you ask it to with `--upgrade`.
- Everything downstream of installation reads the lock rather than
  `project.yml`. A fresh checkout that has not run `install` yet sees no
  packages at all, rather than half of them, and `havn ls` will say so.

`havn init` adds `havn_packages/` to the scaffolded `.gitignore`. Only the lock
is committed.

## Namespacing

A package is written as if it were the whole project. Its files say
`schema=silver` and its siblings say `FROM silver.customers`. Installed into
someone else's project that would collide with their `silver` models, so havn
rewrites both halves at discovery time.

**Schemas get a package prefix.** `silver.customers` in package `crm` becomes
`crm_silver.customers`. A package can never quietly take a name the project
was already using.

**References to the package's own models are rewritten to match.** The package
author writes `FROM bronze.contacts`; what runs is `FROM crm_bronze.contacts`.
Neither side writes the prefix by hand.

**References to anything else are left exactly as written.** A package model
that reads `landing.raw_events` still reads `landing.raw_events`, which is how
a package asks the host project to provide a table.

**Your models use the namespaced name.** To build on a package model, write
the prefix:

```sql
@config materialized=table, schema=gold

SELECT region, COUNT(*) AS customers
FROM crm_silver.customers
GROUP BY 1
```

Package models show up everywhere project models do: `havn ls`, the DAG panel,
`havn transform`, `havn validate`, docs and the API. Two selectors exist for
sorting them out:

```bash
havn ls package:crm     # just that package's models
havn ls 'package:*'     # every package model
havn ls package:        # your own models, no packages
havn transform +gold.report   # pulls in package upstreams like any other
```

## The package manifest

A package may ship a `havn_package.yml` at its root. Every field is optional:

```yaml
name: crm
version: 1.4.0
description: Shared CRM staging models
requires_havn: ">=0.2"
schemas:
  silver: silver
```

- `name` is checked against the name it was installed under, and a mismatch is
  warned about. The installing project's name is what wins.
- `version` and `description` are shown by `havn packages list` and the API.
- `requires_havn` is compared against the running havn and warned about if it
  is not met. It does not refuse to load: a package that declares a floor you
  do not meet is usually still usable, and the author's own SQL will fail with
  a real message if it is not.
- `schemas` overrides the `<pkg>_` prefix per schema. `silver: silver` means
  the package's silver models land in plain `silver`, next to yours.

That last one is the only way a package can collide with a project model. When
it does, havn raises `DuplicateModelError` naming both files, the same error
you would get from two of your own files claiming one name. Remove the
override, or rename the model.

## Authoring a package

A package is a normal havn project with the parts nobody else needs left out:

```
havn-crm/
  havn_package.yml        # name, version, requires_havn
  transform/
    bronze/contacts.sql
    silver/customers.sql
  macros/
    crm.py                # @macro / @table_macro, or CREATE MACRO in .sql
  README.md
```

Write the SQL as if the package were the whole project. Do not write the
`<pkg>_` prefix anywhere; havn adds it. Tag the repository when you are ready
for someone to pin it.

Things worth knowing while authoring:

- **Declare what you expect from the host.** Any reference the package does
  not define itself (say `landing.raw_events`) is passed through untouched, so
  say in your README which tables the installing project has to provide.
- **A package model's SQL must parse.** The rewrite goes through sqlglot, and
  SQL it cannot parse cannot be namespaced.
- **`havn lint` does not lint installed packages.** It walks the project's own
  `transform/` only, so `havn lint --fix` will never rewrite a file the next
  install is going to overwrite. Lint your package in its own repository.

## Macros

A package's `macros/` directory is scanned during macro registration, between
the built-in library and the project's own. Precedence runs:

```
havn.stdlib  <  packages  <  your project
```

A name defined twice resolves to the copy closest to the person who has to
debug it, and every shadowing pair is logged. Two packages defining the same
macro is a genuine conflict with no right answer: the later one wins, both are
named in the warning, and you can settle it by defining your own.

Package macro modules are keyed `havn_macros.<pkg>.<file>`, so a package and
your project can both ship `macros/utils.py` without one silently replacing the
other. `havn macros` attributes each macro to the package it came from.

## Packages are trusted code

Installing a package is not only fetching SQL. A package's `macros/*.py` are
imported as Python modules and registered as DuckDB functions the moment macro
registration runs, which is on every connection havn opens: the CLI, the
server, a pipeline run. Import happens at module level, so the package's code
executes on your machine whether or not any model calls one of its macros.

So treat a package the way you would treat a dependency you `pip install`, not
the way you would treat a data file:

- Read what you are installing before you pin it, and pin it to a tag or a
  commit rather than a branch, so what you reviewed is what you get.
- `havn packages install` is an `execute`-permission endpoint on the server.
  Whoever can call it can run code on the server.
- `git:` sources are restricted to `https://`, `ssh://` and `git@host:path`
  for this reason. A package on this machine belongs under `path:`.

## Editing an installed package

`havn_packages/` is shown in the file tree, dimmed and tagged `installed`, and
the editor carries a banner on those files. They stay editable, because a local
patch is often how you find out whether a fix works, but the next
`havn packages install` replaces the whole checkout. Move the change upstream
into the package's own repository, then bump the `rev`.

## API

```
GET  /api/packages           installed packages plus any declared-but-missing
POST /api/packages/install   {"upgrade": false}, requires execute permission
```

The install endpoint drops the server's cached model discovery, so the DAG
panel reflects the new models without a restart.
