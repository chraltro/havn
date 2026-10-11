# Encryption at rest

havn can store the warehouse as an AES-encrypted DuckDB file, so a stolen
laptop, a synced backup or a misplaced disk does not hand over the data. It is
off by default and opt-in per project.

## Turn it on

Put the key in the environment (never in `project.yml`), then reference it:

```yaml
# project.yml
database:
  path: warehouse.duckdb
  encryption_key: ${HAVN_ENCRYPTION_KEY}
```

```bash
# .env (gitignored) or the real environment
HAVN_ENCRYPTION_KEY=a-long-random-secret
```

havn resolves `${HAVN_ENCRYPTION_KEY}` from the environment. If the variable is
not set, havn refuses to run rather than silently writing a plaintext file.

With a key set, havn opens the warehouse by attaching it to an in-memory root
(`ATTACH 'warehouse.duckdb' AS "warehouse" (ENCRYPTION_KEY …)`) and selecting
it. `current_database()` and every unqualified `schema.table` reference behave
exactly as for a plaintext file, so models, queries and the UI are unaffected.

## The one dependency: a crypto provider for writes

DuckDB's built-in crypto provider is **read-only**. Writing an encrypted file
needs the OpenSSL provider, which ships in the `httpfs` extension. So:

- **Reading** an encrypted warehouse, and **verifying** an encrypted backup,
  work anywhere with no extra setup.
- **Creating or updating** one needs `httpfs` installed. DuckDB fetches and
  caches it automatically on first use if the machine has network; on an
  air-gapped box, install it once where there is network
  (`duckdb -c "INSTALL httpfs"`) and copy the extension cache over.

If the provider cannot be loaded, havn raises a clear error instead of writing
a file that is not securely encrypted.

## What is covered

| Thing | Encrypted? |
|---|---|
| The warehouse file (`warehouse.duckdb`, each environment's file, branch warehouses) | Yes, when a key is set. |
| Backups (`havn backup`) | Yes — a backup is a byte copy of the (encrypted) file. Verifying one needs the key. |
| The WAL | Yes — DuckDB encrypts the write-ahead log alongside the file. |

## What is not covered yet

These paths open a warehouse file directly and do not yet thread the key
through. Until they do, do not rely on encryption in combination with them:

- **Defer / branch CI** (`havn transform --defer`, `havn branch build`): the
  base/defer target is a second file attached read-only and needs its own key
  passed. An encrypted defer target is not yet supported.
- **PR builds** and **`havn migrate`** destinations: these write new DuckDB
  files that are currently plaintext.
- **Pipeline Rewind snapshots and `_snapshots/` versions**: these are Parquet,
  not DuckDB files, and are written in the clear. Keep them off shared storage
  (they are gitignored; see the note below) or disable Rewind
  (`rewind.enabled: false`) if that matters.
- The **DuckLake backend** has its own catalog-level `encrypted:` option for
  the Parquet it writes; the file-level `encryption_key` is for the duckdb
  backend only.

## Concurrency note

An encrypted file is attached to a fresh in-memory root per connection, so
havn cannot share one DuckDB instance across the read pool and the write
connection the way it does for a plaintext file. If the read pool's read-only
opens conflict with the write connection's file lock, havn falls back to
serializing reads through the shared connection (the same fallback it uses on
Windows) rather than failing. For the single-user and small-team use
encryption targets this is not noticeable; a high-concurrency read workload
should weigh it.

## Related: snapshots are local-only

Pipeline Rewind writes a Parquet copy of every model under `.havn/snapshots/`.
These are gitignored by `havn init`, and `havn checkpoint` refuses to stage
them, so they stay on the machine that built them. Encryption at rest does not
cover them; treat that directory as local scratch.

## Key management

havn stores no key of its own; it reads the one you put in the environment.
Rotating the key means re-encrypting the file (open with the old key, `COPY
FROM DATABASE` into a new file opened with the new key). There is no "change
key in place" yet.
