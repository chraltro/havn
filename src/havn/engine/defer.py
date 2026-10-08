"""Defer: build in this environment, read unbuilt upstreams from another.

A developer working in ``dev`` rarely wants to rebuild the whole project
just to run one gold model. Defer lets the dev run read everything it has
not built itself from another environment's warehouse file, while every
write still lands in dev.

The mechanics are DuckDB's, not havn's: the other environment's file is
attached read-only under an alias derived from its path, and at execution
time a model's query has its unbuilt references rewritten from
``bronze.orders`` to ``havn_defer_<hash>.bronze.orders``. Nothing is
rewritten on disk and nothing reaches ``content_hash``: the same model text
builds the same hash whether or not the run deferred.

What decides a rewrite is the local DuckDB catalog, not the model list. An
object that exists in this warehouse is read from this warehouse; anything
else that the defer target does have is read from there. That is what makes
``landing``, seeds and declared sources fall through to the other
environment without any of them having to be listed anywhere. Objects that
exist in neither catalog are left exactly as written, so the run fails with
DuckDB's own "Table with name X does not exist" rather than a confusing
message about a database the user never mentioned.

**The lock caveat comes first.** DuckDB takes a file lock. Attaching the
defer target read-only fails with ``Could not set lock on file`` whenever
any other process holds that file open for writing, which is precisely when
a scheduled run against it is going. :func:`attach_defer_target` turns that
into :class:`DeferLockedError`, naming the holder DuckDB reported, and
``--defer-snapshot`` defers to a consistent copy instead.

**Concurrency.** Two transform runs can be in flight in one process at the
same time: ``POST /api/transform`` and the scheduler both run outside the
pipeline lock. So nothing about defer is process-global.

The rewriter is built once per run and handed down the call stack --
``run_transform`` passes it to ``_run_transform_sequential`` /
``_run_transform_parallel``, which pass it to ``execute_model`` and on to
:func:`havn.engine.transform.execution.resolve_query`. Parallel workers are
threads inside one run and get it as an argument, so a run that did not ask
to defer has no rewriter to find, whatever another run is doing.

The ATTACH is shared, because DuckDB shares it: workers open their own
connections to the same warehouse file and DuckDB serves them from one
instance, so one worker's attach is every worker's attach. That makes it
concurrent state, and :func:`defer_attachment` refcounts it per
``(warehouse, target)`` pair so that the first run to finish does not detach
the target out from under a longer one. The alias is derived from the target
path, so two runs deferring to two different environments coexist instead of
silently sharing one alias.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import duckdb

logger = logging.getLogger("havn.defer")

DEFAULT_ALIAS = "havn_defer"

_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DeferError(Exception):
    """Defer could not be set up."""


class DeferLockedError(DeferError):
    """The defer target is held open for writing by another process."""


class DeferSameProcessError(DeferError):
    """The defer target is already attached in this process."""


class DeferUnsupportedError(DeferError):
    """Defer is not available for this warehouse backend."""


@dataclass
class DeferSpec:
    """What a run needs to know to defer: which environment, which file."""

    target: str
    path: Path
    snapshot: bool = False
    # None means "derive the alias from the target path", which is what every
    # caller wants: two runs deferring to two different environments must not
    # collide on one alias. An explicit alias is honoured as written.
    alias: str | None = None
    verbose: bool = False
    project_dir: Path | None = None
    # Models this run will build itself. They are never redirected even when
    # the warehouse does not hold them yet: the run is about to create them,
    # and a downstream model must read what this run just built rather than
    # the other environment's older copy.
    local_models: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def resolve_defer(
    config: Any,
    project_dir: Path,
    *,
    enabled: bool | None = None,
    snapshot: bool = False,
    verbose: bool = False,
) -> DeferSpec | None:
    """Build the :class:`DeferSpec` for a run, or None if it should not defer.

    ``enabled`` is the tri-state ``--defer/--no-defer``: None means "defer if
    the active environment declares a target", True means "defer, and say so
    if nothing is configured", False means "do not".
    """
    if enabled is False:
        return None

    env_name = getattr(config, "active_environment", None)
    environments = getattr(config, "environments", {}) or {}
    env_cfg = environments.get(env_name) if env_name else None
    target = getattr(env_cfg, "defer", None) if env_cfg is not None else None

    if not target:
        if enabled:
            where = f"environments.{env_name}" if env_name else "environments"
            raise DeferError(
                "--defer was asked for but no defer target is configured. "
                f"Add `defer: <environment>` under {where} in project.yml."
            )
        return None

    backend = getattr(getattr(config, "database", None), "backend", "duckdb")
    if backend == "ducklake":
        raise DeferUnsupportedError(
            "defer is not supported on the DuckLake backend yet. DuckLake "
            "already occupies the ATTACH slot havn would use for the defer "
            "target. Run without --defer, or use a DuckDB-backed environment."
        )

    target_cfg = environments.get(target)
    if target_cfg is None:
        raise DeferError(
            f"environments.{env_name}.defer names '{target}', which is not a "
            "defined environment."
        )
    path = defer_target_path(config, target)
    if path is None:
        raise DeferError(
            f"Environment '{target}' has no database path to defer to."
        )
    resolved = path if path.is_absolute() else (project_dir / path)
    if not resolved.exists():
        raise DeferError(
            f"Defer target '{target}' has no warehouse at {resolved}. Build "
            f"it there first, or run with --no-defer."
        )
    return DeferSpec(
        target=target,
        path=resolved,
        snapshot=snapshot,
        verbose=verbose,
        project_dir=project_dir,
    )


def defer_target_path(config: Any, target: str) -> Path | None:
    """The warehouse path of environment ``target``, as project.yml has it.

    Environment overrides are shallow: an environment that sets no
    ``database.path`` of its own uses the top-level one.
    """
    environments = getattr(config, "environments", {}) or {}
    env_cfg = environments.get(target)
    if env_cfg is None:
        return None
    path = (getattr(env_cfg, "database", {}) or {}).get("path")
    if not path:
        base = getattr(config, "_raw", {}) or {}
        path = (base.get("database", {}) or {}).get("path")
    if not path:
        return None
    return Path(str(path))


# ---------------------------------------------------------------------------
# Attach / detach
# ---------------------------------------------------------------------------


def alias_for_path(path: Path | str) -> str:
    """The attach alias for one defer target.

    Derived from the resolved path, so two runs deferring to two different
    environments get two aliases and can be attached at the same time. Two
    runs deferring to the *same* environment get the same alias on purpose:
    that is one attach, refcounted by :func:`defer_attachment`.
    """
    digest = hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()[:12]
    return f"{DEFAULT_ALIAS}_{digest}"


def attach_defer_target(
    conn: duckdb.DuckDBPyConnection,
    path: Path | str,
    *,
    alias: str = DEFAULT_ALIAS,
) -> None:
    """Attach ``path`` read-only under ``alias``.

    ``IF NOT EXISTS`` so that a second call, whether from a parallel worker's
    connection or from a second run in the same process, is a no-op rather
    than an error.
    """
    _validate_alias(alias)
    literal = str(path).replace("'", "''")
    try:
        conn.execute(f"ATTACH IF NOT EXISTS '{literal}' AS {alias} (READ_ONLY)")
    except Exception as e:
        raise _classify_attach_failure(e, Path(path)) from e


def detach_defer_target(
    conn: duckdb.DuckDBPyConnection,
    *,
    alias: str = DEFAULT_ALIAS,
) -> None:
    """Detach ``alias`` if it is attached. Never raises."""
    _validate_alias(alias)
    try:
        conn.execute(f"DETACH {alias}")
    except Exception as e:
        logger.debug("DETACH %s skipped: %s", alias, e)


@contextmanager
def attached_defer_target(
    conn: duckdb.DuckDBPyConnection,
    path: Path | str,
    *,
    alias: str = DEFAULT_ALIAS,
) -> Iterator[str]:
    """Attach for the duration of the block, detach on the way out.

    The unconditional form, for a single caller that knows it is alone. A
    transform run uses :func:`defer_attachment` instead, which refcounts.
    """
    attach_defer_target(conn, path, alias=alias)
    try:
        yield alias
    finally:
        detach_defer_target(conn, alias=alias)


# How many live defer sessions share one attach, keyed by (warehouse, target).
# The ATTACH is visible to every connection DuckDB serves from the same
# instance, which is what makes it shared state between concurrent runs, and
# what makes a plain DETACH in one run's `finally` a bug in another's.
_attach_lock = threading.Lock()
_attach_counts: dict[tuple[str, str], int] = {}


def _warehouse_key(conn: duckdb.DuckDBPyConnection) -> str:
    """Identify the warehouse instance ``conn`` belongs to.

    Two connections to the same file share one DuckDB instance, and therefore
    one set of attachments. In-memory connections have no file to name, so
    they fall back to the connection's identity, which is the right answer for
    them: nothing else shares their catalog.
    """
    try:
        row = conn.execute(
            "SELECT path FROM duckdb_databases() "
            "WHERE database_name = current_database()"
        ).fetchone()
        if row and row[0]:
            return str(Path(str(row[0])).resolve())
    except Exception as e:
        logger.debug("Could not read the warehouse path: %s", e)
    return f"conn:{id(conn)}"


@contextmanager
def defer_attachment(
    conn: duckdb.DuckDBPyConnection,
    path: Path | str,
    *,
    alias: str,
) -> Iterator[str]:
    """Attach ``path`` for one run, sharing the attach with concurrent runs.

    The first run to enter attaches; the last to leave detaches. Without the
    refcount, a short deferred run's ``finally`` detached the target while a
    longer one was still building, and the long run's remaining models failed
    with "Table with name bronze.orders does not exist".
    """
    key = (_warehouse_key(conn), str(Path(path).resolve()))
    with _attach_lock:
        # The attach happens under the lock so two runs starting at once
        # cannot both read a count of zero and race on the ATTACH.
        attach_defer_target(conn, path, alias=alias)
        _attach_counts[key] = _attach_counts.get(key, 0) + 1
    try:
        yield alias
    finally:
        with _attach_lock:
            remaining = _attach_counts.get(key, 1) - 1
            if remaining > 0:
                _attach_counts[key] = remaining
            else:
                _attach_counts.pop(key, None)
                detach_defer_target(conn, alias=alias)


def _classify_attach_failure(error: Exception, path: Path) -> DeferError:
    """Turn DuckDB's ATTACH failure into the defer error that explains it."""
    text = str(error)
    if "Could not set lock on file" in text:
        holder = _lock_holder(text)
        who = f" It is held by {holder}." if holder else ""
        return DeferLockedError(
            f"Cannot defer to {path}: another process has it open for "
            f"writing, so DuckDB will not attach it read-only.{who} This "
            "happens whenever a run against that environment is in flight. "
            "Retry once it finishes, or use --defer-snapshot to defer to a "
            "consistent copy instead."
        )
    if "Unique file handle conflict" in text:
        return DeferSameProcessError(
            f"Cannot defer to {path}: this process already has that database "
            "open. DuckDB allows one handle per file per process. Defer to a "
            "different environment, or run the command in its own process."
        )
    return DeferError(f"Cannot defer to {path}: {text}")


def _lock_holder(text: str) -> str | None:
    """Pull the holder DuckDB named out of its lock message.

    The message reads: ``Conflicting lock is held in /usr/bin/python3.11
    (PID 10344)``.
    """
    match = re.search(r"Conflicting lock is held in (.+?\(PID \d+\))", text)
    if match:
        return match.group(1).strip()
    return None


def target_lockable(path: Path | str) -> tuple[bool, str | None]:
    """Can this process open ``path`` read-only right now?

    Returns ``(True, None)`` or ``(False, reason)``. Used by ``havn env show``
    and the environment API to answer "would a deferred run start?" without
    starting one. The check opens and immediately closes its own connection,
    so it says nothing about the moment after it returns.
    """
    p = Path(path)
    if not p.exists():
        return False, "no warehouse file at that path"
    conn = None
    try:
        conn = duckdb.connect(str(p), read_only=True)
        return True, None
    except Exception as e:
        err = _classify_attach_failure(e, p)
        if isinstance(err, DeferLockedError):
            holder = _lock_holder(str(e))
            return False, f"locked by another process{f' ({holder})' if holder else ''}"
        if isinstance(err, DeferSameProcessError):
            return False, "already open in this process"
        return False, str(e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as e:
                logger.debug("Closing lock probe failed: %s", e)


# ---------------------------------------------------------------------------
# Snapshot mode
# ---------------------------------------------------------------------------


@dataclass
class DeferSnapshot:
    """A stand-in file for the defer target, and where it came from."""

    path: Path
    source: str          # "copy" or "backup"
    description: str
    cleanup_dir: Path | None = None


def snapshot_defer_target(
    path: Path,
    *,
    project_dir: Path | None = None,
) -> DeferSnapshot:
    """Produce a consistent, attachable copy of the defer target.

    Two sources, in this order, and the order is the whole point:

    1. **DuckDB's ``COPY FROM DATABASE``**, on a separate in-memory
       connection that attaches the target read-only and copies it into a
       temp file. This is the preferred source because DuckDB does the
       reading: the copy is a catalog-level export of committed data, WAL
       included, rather than bytes off a file that may be mid-checkpoint.
       ``routes/export.py`` materialises DuckLake warehouses the same way.

    2. **The newest verified backup**, when step 1 cannot run. It cannot run
       for exactly one reason, and it is the reason ``--defer-snapshot``
       exists: a writer holds the target's lock, and ``COPY FROM DATABASE``
       needs the source attached, which is the operation that just failed.
       There is no third option worth having. Copying the file underneath a
       live writer is not a consistent copy, it is a race whose torn result
       may or may not open; ``engine/backup.py`` writes files that were
       checkpointed before the copy and verified after it, which is the
       stronger guarantee and the one already in the tree.

    Raises :class:`DeferLockedError` when the target is locked and no
    verified backup is on hand, because at that point there is nothing
    consistent left to read.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="havn-defer-"))
    out = tmp_dir / "defer-snapshot.duckdb"
    literal = str(path).replace("'", "''")
    copier = None
    try:
        copier = duckdb.connect()
        copier.execute(f"ATTACH '{literal}' AS defer_src (READ_ONLY)")
        copier.execute(f"ATTACH '{out.as_posix()}' AS defer_snap (TYPE DUCKDB)")
        copier.execute("COPY FROM DATABASE defer_src TO defer_snap")
        copier.execute("DETACH defer_snap")
        copier.execute("DETACH defer_src")
        return DeferSnapshot(
            path=out,
            source="copy",
            description=f"COPY FROM DATABASE of {path}",
            cleanup_dir=tmp_dir,
        )
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        classified = _classify_attach_failure(e, path)
        if not isinstance(classified, DeferLockedError):
            raise classified from e
        backup = _newest_verified_backup(project_dir)
        if backup is None:
            raise DeferLockedError(
                f"Cannot snapshot {path}: it is locked by another process and "
                "there is no verified backup to fall back on. Run `havn "
                "backup` against that environment while it is idle, or wait "
                "for the writer to finish."
            ) from e
        return DeferSnapshot(
            path=backup,
            source="backup",
            description=f"verified backup {backup.name} ({path} is locked)",
        )
    finally:
        if copier is not None:
            try:
                copier.close()
            except Exception as close_err:
                logger.debug("Closing snapshot connection failed: %s", close_err)


def _newest_verified_backup(project_dir: Path | None) -> Path | None:
    """The most recent backup that is on disk, verified and still readable."""
    if project_dir is None:
        return None
    from havn.engine.backup import list_backups, verify_backup

    try:
        entries = list_backups(project_dir)
    except Exception as e:
        logger.debug("Could not read the backup manifest: %s", e)
        return None
    candidates = [
        e for e in entries
        if e.get("exists") and e.get("verified", True)
    ]
    candidates.sort(key=lambda e: str(e.get("timestamp", "")), reverse=True)
    for entry in candidates:
        candidate = Path(entry["path"])
        try:
            result = verify_backup(candidate)
        except Exception as e:
            logger.debug("Verifying %s failed: %s", candidate, e)
            continue
        if result.get("valid"):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Query rewriting
# ---------------------------------------------------------------------------


def catalog_objects(
    conn: duckdb.DuckDBPyConnection,
    catalog: str | None = None,
) -> set[str]:
    """Lowercase ``schema.name`` of every table and view in one catalog.

    ``catalog=None`` means the connection's own database. Passing the defer
    alias reads the attached one instead: ``information_schema.tables`` spans
    every attached database, so the filter is what keeps the two apart.
    """
    if catalog is None:
        rows = conn.execute(
            "SELECT lower(table_schema), lower(table_name) "
            "FROM information_schema.tables "
            "WHERE table_catalog = current_database()"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT lower(table_schema), lower(table_name) "
            "FROM information_schema.tables WHERE lower(table_catalog) = ?",
            [catalog.lower()],
        ).fetchall()
    return {f"{schema}.{name}" for schema, name in rows}


def make_defer_rewriter(
    conn: duckdb.DuckDBPyConnection,
    alias: str,
    local_catalog: set[str],
    *,
    report: Callable[[list[str]], None] | None = None,
) -> Callable[[str], str]:
    """Return the run's query rewriter.

    Both catalogs are read once, here, and the returned callable does no
    further catalog queries: it is called for every model in the run, and
    parallel workers share the one instance.

    A reference is redirected when all of these hold: it is schema-qualified,
    it names no catalog of its own, it is not a CTE or a table function, the
    local warehouse does not have it, and the defer target does. A reference
    that neither catalog has is left alone on purpose, so the user sees
    DuckDB's own error about the table they actually wrote.
    """
    _validate_alias(alias)
    remote_catalog = catalog_objects(conn, alias)

    def rewrite(sql: str) -> str:
        from havn.engine.sql_rewrite import (
            SQLRewriteError,
            find_table_refs,
            rewrite_table_refs,
        )

        # `{this}` / `{start}` / `{end}` survive the round trip because
        # find_table_refs and rewrite_table_refs mask them internally.
        try:
            refs = find_table_refs(sql, skip_catalog_qualified=True)
        except SQLRewriteError as e:
            # Unparseable SQL is left exactly as written. Falling through is
            # safe in this direction: the query runs against the local
            # warehouse, which is the behaviour without defer.
            logger.debug("Defer rewrite skipped (parse): %s", e)
            return sql

        mapping = {
            ref: f"{alias}.{ref}"
            for ref in refs
            if "." in ref and ref not in local_catalog and ref in remote_catalog
        }
        if not mapping:
            return sql
        try:
            rewritten = rewrite_table_refs(
                sql, mapping, skip_catalog_qualified=True
            )
        except SQLRewriteError as e:
            logger.debug("Defer rewrite skipped (generate): %s", e)
            return sql

        redirected = sorted(mapping)
        logger.info("defer: redirected %s", ", ".join(redirected))
        if report is not None:
            report(redirected)
        return rewritten

    return rewrite


# ---------------------------------------------------------------------------
# Run-scoped session
# ---------------------------------------------------------------------------


@contextmanager
def defer_session(
    conn: duckdb.DuckDBPyConnection,
    spec: DeferSpec,
    *,
    on_message: Callable[[str], None] | None = None,
) -> Iterator[Callable[[str], str]]:
    """Attach, build the run's rewriter, and undo both on the way out.

    The rewriter is *yielded*, not installed anywhere. Its only reader is the
    run that entered this block, which threads it down to ``resolve_query``;
    a concurrent run that did not ask to defer must never pick it up.

    One message is emitted at the start naming the target and the file the
    run will actually read, which is the snapshot's path in snapshot mode.
    """
    snapshot: DeferSnapshot | None = None
    path = spec.path
    origin = str(spec.path)
    if spec.snapshot:
        snapshot = snapshot_defer_target(spec.path, project_dir=spec.project_dir)
        path = snapshot.path
        origin = snapshot.description

    alias = spec.alias or alias_for_path(path)

    def say(message: str) -> None:
        logger.info("%s", message)
        if on_message is not None:
            on_message(message)

    # The snapshot copy is a whole warehouse in a temp dir, so its cleanup
    # guards everything that follows, not just the body of the session. An
    # ATTACH that fails -- the target went away, the alias is taken -- raises
    # before the manager ever yields, and a cleanup that lived only in the
    # inner finally would never run for it.
    try:
        with defer_attachment(conn, path, alias=alias):
            say(f"defer: reading unbuilt models from '{spec.target}' ({origin})")

            local = catalog_objects(conn) | {m.lower() for m in spec.local_models}

            def report_redirects(redirected: list[str]) -> None:
                say("defer: " + ", ".join(f"{r} -> {alias}.{r}" for r in redirected))

            rewriter = make_defer_rewriter(
                conn,
                alias,
                local,
                report=report_redirects if spec.verbose else None,
            )
            yield rewriter
    finally:
        if snapshot is not None and snapshot.cleanup_dir is not None:
            shutil.rmtree(snapshot.cleanup_dir, ignore_errors=True)


def _validate_alias(alias: str) -> None:
    if not _ALIAS_RE.match(alias or ""):
        raise DeferError(f"Invalid defer alias: {alias!r}")
