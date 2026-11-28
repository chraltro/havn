# Upgrading havn

havn keeps its own bookkeeping (model state, run history, users, assertion
results, lineage, performance records and more) in the `_havn` schema of your
warehouse. When a new release changes that schema, the warehouse is upgraded
for you.

## What happens on upgrade

The first time a new havn opens your warehouse for writing (`havn transform`,
`havn serve`, `havn run` and so on), it applies any pending metadata
migrations, then carries on. Each migration runs in its own transaction and is
recorded in `_havn.schema_migrations` only when it succeeds. If one fails, it is
rolled back, nothing from it is kept, and havn stops with an error that names
the migration. Migrations that succeeded before it stay applied.

Commands that only read (`havn query`, `havn status`, `havn tables`) never
change the schema.

Migrations add tables and columns and never drop, rename or retype a column.
Some also rewrite rows in `_havn`: migration 2 lowercases model names in every
table that records them, and where a model has rows under
both spellings it keeps the lowercase one and deletes the other. Your own
schemas (`landing`, `bronze`, `silver`, `gold`, ...) are never touched.

Warehouses created by any 0.2.x or 0.3.x release upgrade the same way,
whichever havn features they used.

## Check where a warehouse stands

```bash
havn status
```

shows a line such as

```
metadata schema: version 2 (up to date)
metadata schema: version 0 of 2, 2 pending (1 baseline, 2 lowercase_model_keys); applied the next time havn opens the warehouse for writing
```

## Back up first

Upgrades are designed to be safe, but the warehouse is your data. Take a
backup before installing a new release:

```bash
havn backup
```

and restore it with `havn backup-restore <path>` if you need to go back.

## Going back to an older release

A warehouse that a newer havn has opened records its schema version in
`_havn.schema_migrations`. Releases up to and including 0.3.1 predate that
table and ignore it: they log nothing about it and keep working with the
tables and columns they know, because migrations never remove or retype a
column. Later releases that find a version newer than they know log a warning
("newer than this havn knows"), `havn status` says the same, and they apply
nothing.

Data written by the older release while downgraded is readable after you
upgrade again, with one exception: 0.2.30 and earlier record a model whose
file name has capital letters (`silver/Customers.sql`) under that spelling.
Migration 2 has already run on the warehouse, so those rows are not
lowercased again. Each such model rebuilds once as if new, and a microbatch
model reprocesses its windows from `begin`. Lowercase file names avoid this.

If the newer release did something you want undone, restore the backup you
took before upgrading rather than relying on the older release.
