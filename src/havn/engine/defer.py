"""Defer: build in this environment, read unbuilt upstreams from another.

A developer working in ``dev`` rarely wants to rebuild the whole project
just to run one gold model. Defer lets the dev run read everything it has
not built itself from another environment's warehouse file, while every
write still lands in dev.

The mechanics are DuckDB's, not havn's: the other environment's file is
attached read-only under a fixed alias, and at execution time a model's
query has its unbuilt references rewritten from ``bronze.orders`` to
``havn_defer.bronze.orders``. Nothing is rewritten on disk and nothing
reaches ``content_hash``: the same model text builds the same hash whether
or not the run deferred.

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

Scope is the process, like the ATTACH itself. Parallel transform workers
open their own connections to the same warehouse file, which DuckDB serves
from one shared instance, so the attach one worker sees is the attach every
worker sees and ``ATTACH IF NOT EXISTS`` makes the siblings a no-op. The
rewriter is likewise built once per run and installed process-wide for
:func:`havn.engine.transform.execution.resolve_query` to pick up. Two
unrelated transform runs in one process would share it; that is the same
single-process constraint ``engine/pr.py`` documents for its own ATTACH.
"""

from __future__ import annotations

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
# Model SQL carries `{this}`, `{start}` and `{end}` placeholders that are
# substituted after the query is resolved. sqlglot parses `{start}` as a
# struct literal and regenerates it as `{'start': start}`, so they are hidden
# behind plain identifiers for the duration of the rewrite.
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_PLACEHOLDER_TOKEN = "__havn_defer_ph_{}__"


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
    alias: str = DEFAULT_ALIAS
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
    """Attach for the duration of the block, detach on the way out."""
    attach_defer_target(conn, path, alias=alias)
    try:
        yield alias
    finally:
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

        masked, restore = _mask_placeholders(sql)
        try:
            refs = find_table_refs(masked, skip_catalog_qualified=True)
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
                masked, mapping, skip_catalog_qualified=True
            )
        except SQLRewriteError as e:
            logger.debug("Defer rewrite skipped (generate): %s", e)
            return sql

        redirected = sorted(mapping)
        logger.info("defer: redirected %s", ", ".join(redirected))
        if report is not None:
            report(redirected)
        return restore(rewritten)

    return rewrite


def _mask_placeholders(sql: str) -> tuple[str, Callable[[str], str]]:
    """Hide ``{this}`` / ``{start}`` / ``{end}`` from the SQL parser.

    Returns the masked SQL and the function that puts the placeholders back.
    Without this an incremental or microbatch model would come out of the
    rewrite with ``{'start': start}`` where its placeholder used to be, and
    the later substitution would find nothing to replace.
    """
    found: list[str] = []

    def _mask(match: re.Match) -> str:
        found.append(match.group(1))
        return _PLACEHOLDER_TOKEN.format(match.group(1))

    masked = _PLACEHOLDER_RE.sub(_mask, sql)
    if not found:
        return sql, lambda out: out

    def restore(out: str) -> str:
        for name in found:
            out = out.replace(_PLACEHOLDER_TOKEN.format(name), "{" + name + "}")
        return out

    return masked, restore


# ---------------------------------------------------------------------------
# Run-scoped installation
# ---------------------------------------------------------------------------

_rewriter_lock = threading.Lock()
_active_rewriter: Callable[[str], str] | None = None


def active_query_rewriter() -> Callable[[str], str] | None:
    """The rewriter the current run installed, if any.

    ``resolve_query`` asks for this once per model. It is process-wide
    because the ATTACH it goes with is process-wide, and because transform
    workers are threads that must all see the same answer.
    """
    return _active_rewriter


def _install_rewriter(rewriter: Callable[[str], str] | None) -> None:
    global _active_rewriter
    with _rewriter_lock:
        _active_rewriter = rewriter


@contextmanager
def defer_session(
    conn: duckdb.DuckDBPyConnection,
    spec: DeferSpec,
    *,
    on_message: Callable[[str], None] | None = None,
) -> Iterator[Callable[[str], str]]:
    """Attach, install the rewriter, and undo both on the way out.

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

    def say(message: str) -> None:
        logger.info("%s", message)
        if on_message is not None:
            on_message(message)

    attach_defer_target(conn, path, alias=spec.alias)
    say(f"defer: reading unbuilt models from '{spec.target}' ({origin})")

    local = catalog_objects(conn) | {m.lower() for m in spec.local_models}

    def report_redirects(redirected: list[str]) -> None:
        say("defer: " + ", ".join(f"{r} -> {spec.alias}.{r}" for r in redirected))

    rewriter = make_defer_rewriter(
        conn,
        spec.alias,
        local,
        report=report_redirects if spec.verbose else None,
    )
    _install_rewriter(rewriter)
    try:
        yield rewriter
    finally:
        _install_rewriter(None)
        detach_defer_target(conn, alias=spec.alias)
        if snapshot is not None and snapshot.cleanup_dir is not None:
            shutil.rmtree(snapshot.cleanup_dir, ignore_errors=True)


def _validate_alias(alias: str) -> None:
    if not _ALIAS_RE.match(alias or ""):
        raise DeferError(f"Invalid defer alias: {alias!r}")
