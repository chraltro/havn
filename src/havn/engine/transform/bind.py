"""Shadow catalog bind pass: resolve model SQL through the DuckDB binder.

The binder is the only thing in the stack that knows every DuckDB function,
every operator overload and every column type, including the Python ``@macro``
UDFs havn registers at runtime. Rather than re-implement a type system in
sqlglot, this module hands the model SQL to DuckDB and collects what comes
back.

The SQL handed over is, on the API path, an unsaved editor buffer from anyone
with *read* permission. It is therefore never executed against the warehouse.
Mechanics:

1. Open a private ``duckdb.connect(":memory:")``. It is not attached to the
   warehouse, holds no writable handle, and is closed at the end of the call.
   Nothing a buffer does can reach the real catalog, so there is no
   three-part-name escape, no cross-request ATTACH name collision and no
   ``USE`` juggling on a shared connection.
2. Seed every non-model object the models reference (landing tables, seeds,
   sources, anything already built that is outside the bound chain) as an
   **empty** table with the real column types, from one bulk
   ``information_schema.columns`` fetch on the caller's warehouse connection
   (read-only is fine: this pass only reads the catalog). An empty table binds
   exactly like a populated one, and no row ever leaves the warehouse.
3. Load the extensions the caller's connection has, register the project's
   macros, then ``SET enable_external_access = false`` and
   ``SET lock_configuration = true`` **before any user SQL runs**. From that
   point on ``read_csv``, ``read_text``, ``glob``, ``COPY ... TO``, ``ATTACH``,
   ``INSTALL``/``LOAD`` and re-enabling the switch all fail inside the shadow.
4. ``CREATE OR REPLACE VIEW schema.name AS <model.query>`` in topological
   order, with the model's SQL verbatim. The shadow holds only the models and
   their base tables, so a stale built table in the warehouse cannot win over
   the fresh definition. That is the property that makes this reflect the file
   rather than the last build.
5. ``DESCRIBE`` each view for its ``(name, type)`` pairs.

A model that legitimately reads a file (``read_parquet('data/x.parquet')``)
cannot bind under the lockdown. That is reported as a warning naming the
model, not as an error, and the model is skipped.

What it catches: wrong arity, unknown functions, operator overload failures,
missing columns (including on upstreams that were never built), missing
struct keys, ambiguous references, aggregation without GROUP BY, set
operations with mismatched column counts.

What it does not catch: value conversions. ``CAST(some_varchar AS INTEGER)``
binds clean and fails at run time on the first row that is not a number.
These are bind errors, not type errors, and the docs say so.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from .models import SQLModel

logger = logging.getLogger("havn.transform.bind")

# How many lines the ``CREATE VIEW ... AS`` header adds in front of the
# model's own SQL. Keep the header on its own line so DuckDB's ``LINE n``
# maps back to the file by subtracting exactly this.
_STATEMENT_LINE_OFFSET = 1

_LINE_ECHO = re.compile(r"^LINE (\d+):[ ]?(.*)$")
_QUOTED = re.compile(r'"([^"]+)"')
_BARE_FUNCTION = re.compile(r"with name ([A-Za-z_][A-Za-z0-9_]*) does not exist")

UNAVAILABLE_MESSAGE = "bind pass unavailable on this backend"

# What the shadow says about a model it cannot bind because the model reads a
# file. The lockdown is deliberate, so this is a warning, not an error.
FILE_ACCESS_MESSAGE = (
    "file functions are not available in the bind pass; this model is skipped"
)

# DuckDB's own wording when the lockdown refuses something.
_FILE_ACCESS_MARKERS = (
    "file system operations are disabled",
    "Loading external extensions is disabled",
    "the configuration has been locked",
)

# What a DuckDB type name may look like: MAP(VARCHAR, INTEGER), DECIMAL(10,2),
# STRUCT(a INTEGER), INTEGER[], STRUCT("a b" INTEGER). Deliberately no single
# quotes, semicolons or dashes, so the text cannot close the DDL it goes into.
_TYPE_TEXT = re.compile(r'[A-Za-z_"][A-Za-z0-9_ ,()\[\]".]*')

# Extensions the shadow loads before it locks itself down. ``connect()``
# installs nothing itself (DuckDB auto-loads what a query needs), but
# auto-loading is exactly what ``enable_external_access = false`` prevents, so
# the statically linked ones are loaded up front and the rest is copied from
# whatever the caller's connection already has.
_BASE_EXTENSIONS = ("json", "parquet", "icu")

_EXTENSION_NAME = re.compile(r"[a-z0-9_]+")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class BindError:
    """One diagnostic from the bind pass, positioned in the model's file."""

    message: str
    raw: str = ""
    line: int | None = None
    col: int | None = None
    end_line: int | None = None
    end_col: int | None = None
    kind: str = "bind"  # "bind", "catalog", "parse", "upstream", "unavailable"


@dataclass
class BindResult:
    """Inferred schemas and bind errors for one call of :func:`bind_models`."""

    schemas: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    errors: dict[str, list[BindError]] = field(default_factory=dict)
    duration_ms: int = 0
    # False when the backend could not host a shadow catalog at all. The
    # single reason is then in ``warnings``.
    available: bool = True
    warnings: list[BindError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no model recorded an error."""
        return not any(self.errors.values())


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------


def ancestor_closure(
    models: list[SQLModel], targets: list[str]
) -> list[SQLModel]:
    """Return ``targets`` plus every model they transitively depend on.

    Binding one model from the editor must not bind the other 999 in the
    project. ``depends_on`` is already resolved at discovery, so the closure
    is a plain walk. Names that are not models (landing tables, seeds) drop
    out here and are seeded into the shadow as empty typed tables instead.
    """
    by_name = {m.full_name: m for m in models}
    wanted: set[str] = set()
    stack = [t for t in targets if t in by_name]
    while stack:
        name = stack.pop()
        if name in wanted:
            continue
        wanted.add(name)
        for dep in by_name[name].depends_on:
            if dep in by_name and dep not in wanted:
                stack.append(dep)
    # Keep the caller's model order so the result is deterministic.
    return [m for m in models if m.full_name in wanted]


# ---------------------------------------------------------------------------
# Position extraction
# ---------------------------------------------------------------------------


def _first_identifier(message: str) -> str | None:
    """The identifier a DuckDB message is complaining about, if it names one."""
    head = message.split("\n", 1)[0]
    m = _QUOTED.search(head)
    if m:
        return m.group(1)
    m = _BARE_FUNCTION.search(head)
    if m:
        return m.group(1)
    return None


def _whole_line(line_no: int, source_line: str) -> tuple[int, int, int, int]:
    """Marker covering an entire source line."""
    return line_no, 1, line_no, max(len(source_line), 1) + 1


def extract_position(
    raw: str,
    query: str,
    *,
    line_offset: int = _STATEMENT_LINE_OFFSET,
) -> tuple[int | None, int | None, int | None, int | None]:
    """Map a DuckDB error onto ``(line, col, end_line, end_col)``, 1-based.

    Binder and catalog errors carry a ``LINE n:`` echo of the offending line
    plus a caret line under it. The column is ``caret_index - len("LINE n: ")``.

    Two things break that arithmetic:

    - Long lines are echoed truncated with a leading ``...``, so the caret
      offset refers to the truncated text. Recovery is to look for the quoted
      identifier from the message in the real source line; if it appears more
      than once, or not at all, fall back to marking the whole line.
    - Parser errors sometimes carry no position at all. Those get ``None``,
      which the API renders as a whole-file diagnostic.

    ``line_offset`` is how many lines the wrapping statement added in front of
    the model SQL. ``strip_config_comments`` blanks directive lines in place
    instead of deleting them, so a line number in ``query`` is already the
    line number in the file.
    """
    lines = raw.split("\n")
    echo_index = None
    reported = 0
    echoed = ""
    for i, line in enumerate(lines):
        m = _LINE_ECHO.match(line)
        if m:
            echo_index = i
            reported = int(m.group(1))
            echoed = m.group(2)
            break
    if echo_index is None:
        return None, None, None, None

    file_line = reported - line_offset
    if file_line < 1:
        return None, None, None, None

    source_lines = query.split("\n")
    source_line = source_lines[file_line - 1] if file_line <= len(source_lines) else ""

    ident = _first_identifier(raw)
    caret_line = lines[echo_index + 1] if echo_index + 1 < len(lines) else ""
    caret_index = caret_line.find("^")

    truncated = echoed.startswith("...")
    if truncated or caret_index < 0:
        if ident:
            first = source_line.find(ident)
            last = source_line.rfind(ident)
            if first >= 0 and first == last:
                return (
                    file_line,
                    first + 1,
                    file_line,
                    first + 1 + len(ident),
                )
        return _whole_line(file_line, source_line)

    prefix_len = len(f"LINE {reported}: ")
    col0 = caret_index - prefix_len
    if col0 < 0 or col0 > len(source_line):
        return _whole_line(file_line, source_line)

    width = len(ident) if ident and source_line[col0:].startswith(ident) else 1
    return file_line, col0 + 1, file_line, col0 + 1 + width


def _classify(exc: Exception) -> str:
    name = type(exc).__name__
    if "Parser" in name:
        return "parse"
    if "Catalog" in name:
        return "catalog"
    return "bind"


def _clean_message(raw: str) -> str:
    """The part of a DuckDB message worth putting in a marker tooltip.

    Candidate-function dumps run to fifty lines; the ``LINE n``/caret echo is
    redundant once the position is extracted. Keep the first sentence plus any
    short hint line.
    """
    kept: list[str] = []
    for line in raw.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("LINE ") or set(stripped) <= {"^"}:
            break
        if stripped.startswith("Candidate functions:") or stripped.startswith(
            "Candidate"
        ):
            if len(kept) >= 1:
                break
            continue
        kept.append(stripped)
        if len(kept) >= 2:
            break
    return " ".join(kept) if kept else raw.split("\n")[0]


def bind_error_from_exception(
    exc: Exception, query: str, *, line_offset: int = _STATEMENT_LINE_OFFSET
) -> BindError:
    """Turn a DuckDB exception raised while binding into a :class:`BindError`."""
    raw = str(exc)
    line, col, end_line, end_col = extract_position(
        raw, query, line_offset=line_offset
    )
    return BindError(
        message=_clean_message(raw),
        raw=raw,
        line=line,
        col=col,
        end_line=end_line,
        end_col=end_col,
        kind=_classify(exc),
    )


# ---------------------------------------------------------------------------
# Shadow seeding
# ---------------------------------------------------------------------------


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _split_two_part(name: str) -> tuple[str, str] | None:
    parts = name.split(".")
    if len(parts) != 2:
        return None
    schema, table = parts
    if not schema or not table:
        return None
    return schema, table


def _fetch_base_columns(
    source: duckdb.DuckDBPyConnection, names: list[str]
) -> dict[str, list[tuple[str, str]]]:
    """One bulk ``information_schema.columns`` fetch for ``names``.

    ``names`` are lowercased ``schema.table`` keys. The result maps each key
    that exists in the catalog to its ``(column, type)`` pairs in ordinal
    order. Reading the catalog is all this needs, so a read-only connection is
    enough; no rows are touched.
    """
    if not names:
        return {}
    placeholders = ", ".join("?" for _ in names)
    sql = (
        "SELECT lower(table_schema), lower(table_name), column_name, data_type "
        "FROM information_schema.columns "
        f"WHERE lower(table_schema) || '.' || lower(table_name) IN ({placeholders}) "
        "ORDER BY table_schema, table_name, ordinal_position"
    )
    try:
        rows = source.execute(sql, list(names)).fetchall()
    except Exception as e:  # pragma: no cover - catalog always readable
        logger.debug("Could not read base table columns for the bind pass: %s", e)
        return {}
    out: dict[str, list[tuple[str, str]]] = {}
    for schema, table, column, dtype in rows:
        out.setdefault(f"{schema}.{table}", []).append((str(column), str(dtype)))
    return out


def _load_extensions(
    shadow: duckdb.DuckDBPyConnection, source: duckdb.DuckDBPyConnection
) -> None:
    """Load into the shadow what the caller's connection has, plus the basics.

    Must run before the lockdown: ``enable_external_access = false`` blocks
    both ``LOAD`` of an installed extension and DuckDB's own auto-loading. A
    missing ``json`` or ``spatial`` would otherwise produce a spurious
    "function does not exist", which is worse than no check at all.
    """
    names = set(_BASE_EXTENSIONS)
    try:
        rows = source.execute(
            "SELECT extension_name FROM duckdb_extensions() WHERE loaded"
        ).fetchall()
        names.update(str(r[0] or "") for r in rows)
    except Exception:
        logger.debug("Bind pass could not list the caller's extensions")
    for name in sorted(names):
        if not _EXTENSION_NAME.fullmatch(name):
            continue
        try:
            shadow.execute(f"LOAD {name}")
        except Exception:
            logger.debug("Bind pass could not load extension %s", name)


def _register_shadow_macros(
    shadow: duckdb.DuckDBPyConnection, project_dir: Path | None
) -> None:
    """Register the project's macros on the shadow connection.

    The shadow is its own DuckDB instance, so neither the scalar UDFs nor the
    public-name ``CREATE MACRO`` aliases come across from the warehouse: they
    have to be registered here. Registration reads ``macros/*.py`` through
    Python, not through DuckDB, so it needs no external access of its own --
    but the ``CREATE MACRO`` bodies for table macros call ``json_each``, which
    is why extensions load first and the lockdown comes last.
    """
    if project_dir is None:
        return
    try:
        from havn.engine.macros import register_macros

        register_macros(shadow, Path(project_dir))
    except Exception as e:
        logger.debug("Bind pass macro registration skipped: %s", e)


def _lock_down(shadow: duckdb.DuckDBPyConnection) -> None:
    """Take file, network and configuration access away from the shadow.

    Runs before any model SQL. After this, ``read_csv``/``read_text``/``glob``
    and the rest of the file functions, ``COPY ... TO``, ``ATTACH``,
    ``INSTALL``/``LOAD`` and re-enabling the switch itself all fail to bind.
    """
    shadow.execute("SET enable_external_access = false")
    shadow.execute("SET lock_configuration = true")


def _create_typed_stub(
    shadow: duckdb.DuckDBPyConnection,
    schema: str,
    table: str,
    columns: list[tuple[str, str]],
) -> None:
    """Create an empty, correctly typed table for one base object.

    Empty tables bind identically to populated ones, and no warehouse row is
    ever copied into the shadow. The type text goes into the DDL unquoted,
    because there is no other way to say ``DECIMAL(10,2)`` or
    ``STRUCT(a INTEGER)``, so it is checked against the shape a DuckDB type
    name can take first. ``information_schema.columns`` round-trips every
    DuckDB type this way, nested ones included.
    """
    if not columns:
        return
    spec = ", ".join(
        f"{_quote_ident(cname)} {ctype}"
        for cname, ctype in columns
        if _TYPE_TEXT.fullmatch(str(ctype))
    )
    if not spec:
        return
    shadow.execute(
        f"CREATE OR REPLACE TABLE {_quote_ident(schema)}.{_quote_ident(table)} ({spec})"
    )


def _seed_base_tables(
    shadow: duckdb.DuckDBPyConnection,
    source: duckdb.DuckDBPyConnection,
    models: list[SQLModel],
    base_tables: dict[str, list[tuple[str, str]]] | None,
) -> None:
    """Seed every non-model object the models reference into the shadow."""
    model_names = {m.full_name.lower() for m in models}
    wanted: dict[str, str] = {}
    for model in models:
        for dep in model.depends_on:
            key = dep.lower()
            if key in model_names:
                continue
            wanted.setdefault(key, dep)
    for name in (base_tables or {}):
        key = name.lower()
        if key not in model_names:
            wanted.setdefault(key, name)

    if not wanted:
        return

    declared = {k.lower(): v for k, v in (base_tables or {}).items()}
    fetched = _fetch_base_columns(
        source, sorted(k for k in wanted if k not in declared)
    )

    created_schemas: set[str] = set()
    for key, original in sorted(wanted.items()):
        parts = _split_two_part(original)
        if parts is None:
            continue
        schema, table = parts
        if not _IDENTIFIER.fullmatch(schema) or not _IDENTIFIER.fullmatch(table):
            continue
        columns = declared.get(key) or fetched.get(key)
        if not columns:
            # Not a model and not in the catalog: leave it missing so the
            # binder reports "Table ... does not exist" on the line that
            # references it, which is the honest answer.
            continue
        if schema not in created_schemas:
            shadow.execute(f"CREATE SCHEMA IF NOT EXISTS {_quote_ident(schema)}")
            created_schemas.add(schema)
        try:
            _create_typed_stub(shadow, schema, table, columns)
        except Exception as e:
            logger.debug("Bind pass could not seed %s: %s", original, e)


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


def _unavailable(reason: str, started: float) -> BindResult:
    return BindResult(
        duration_ms=int((time.perf_counter() - started) * 1000),
        available=False,
        warnings=[
            BindError(
                message=UNAVAILABLE_MESSAGE,
                raw=reason,
                kind="unavailable",
            )
        ],
    )


def _bindable_query(model: SQLModel) -> str:
    """The model's query with anything the binder cannot see filled in.

    A microbatch model's SQL carries ``{start}`` and ``{end}``, which are not
    SQL and would fail to parse. Substituting the model's first window gives
    the binder real literals of the right type, and since the placeholders
    are replaced in place the line numbers in any error still point at the
    line the user wrote.
    """
    if model.incremental_strategy != "microbatch":
        return model.query
    if "{start}" not in model.query and "{end}" not in model.query:
        return model.query

    from .execution import (
        parse_event_time,
        shift_batch,
        substitute_batch_window,
        truncate_to_batch,
    )

    batch_size = model.batch_size if model.batch_size in ("hour", "day", "month", "year") else "day"
    try:
        start = truncate_to_batch(parse_event_time(model.begin or ""), batch_size)
    except Exception:
        start = datetime(1970, 1, 1)
    return substitute_batch_window(
        model.query, start, shift_batch(start, batch_size, 1)
    )


def _shadow_body(model: SQLModel, snapshot_settings: object | None) -> str:
    """The body of the shadow view for ``model``, starting at a newline.

    Every materialization but ``snapshot`` binds its own SELECT verbatim. A
    snapshot's target carries meta columns the query never selects, so a
    downstream model that reads ``is_current`` would fail to bind against the
    raw query even though the built table has the column. The wrapper adds
    them as typed NULLs, which is enough for the binder to resolve and type
    them, and it is kept on the ``CREATE VIEW`` line so a DuckDB ``LINE n``
    still maps back to the model's own file by subtracting
    ``_STATEMENT_LINE_OFFSET``.
    """
    if model.materialized != "snapshot":
        return "\n" + _bindable_query(model)

    from .execution import SnapshotSettings

    st = snapshot_settings if snapshot_settings is not None else SnapshotSettings()
    meta = [
        f"CAST(NULL AS TIMESTAMP) AS {_quote_ident(st.valid_from)}",
        f"CAST(NULL AS TIMESTAMP) AS {_quote_ident(st.valid_to)}",
        f"CAST(NULL AS BOOLEAN) AS {_quote_ident(st.is_current)}",
        f"CAST(NULL AS VARCHAR) AS {_quote_ident(st.row_hash)}",
    ]
    if model.hard_deletes == "new_record":
        meta.append(f"CAST(NULL AS BOOLEAN) AS {_quote_ident(st.is_deleted)}")
    inner = _bindable_query(model).rstrip().rstrip(";")
    return (
        " SELECT *, " + ", ".join(meta) + " FROM (\n"
        + inner
        + "\n) AS _havn_snapshot_src"
    )


def _is_file_access_denied(exc: Exception) -> bool:
    """True when the shadow's lockdown, not the model, refused the statement."""
    text = str(exc)
    return any(marker in text for marker in _FILE_ACCESS_MARKERS)


def bind_models(
    conn: duckdb.DuckDBPyConnection,
    models: list[SQLModel],
    *,
    base_tables: dict[str, list[tuple[str, str]]] | None = None,
    project_dir: Path | str | None = None,
) -> BindResult:
    """Bind ``models`` against a private, locked-down shadow and report back.

    No model SQL ever runs against ``conn``. The shadow is a separate
    ``duckdb.connect(":memory:")`` instance with no attachment to the
    warehouse, no file access and a locked configuration, and it is closed
    before this returns.

    Args:
        conn: The warehouse connection, used **only** as the catalog source
            for base table column types. Read-only is fine; nothing is
            written, attached or ``USE``d on it.
        models: The models to bind. Callers that only care about one model
            should pass :func:`ancestor_closure` of it, not the whole project.
        base_tables: Explicit ``schema.table -> [(column, type)]`` specs for
            base objects that are not in the catalog yet. Anything not listed
            here is read from ``conn``'s catalog when it exists.
        project_dir: Used to register the project's Python macros on the
            shadow so they resolve while binding.

    Returns:
        A :class:`BindResult`. ``available`` is False, with a single warning,
        when a shadow connection could not be opened at all.
    """
    started = time.perf_counter()
    result = BindResult()
    if not models:
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result

    from .discovery import CircularDependencyError, build_dag

    try:
        ordered = build_dag(models)
    except CircularDependencyError as e:
        for model in models:
            result.errors[model.full_name] = [
                BindError(message=str(e), raw=str(e), kind="bind")
            ]
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result

    try:
        shadow = duckdb.connect(":memory:")
    except Exception as e:  # pragma: no cover - only under memory exhaustion
        return _unavailable(str(e), started)

    try:
        try:
            shadow.execute("SET threads = 1")
        except Exception:  # pragma: no cover - defensive
            logger.debug("Bind pass could not pin the shadow to one thread")

        # Everything that needs the outside world happens here, before the
        # lockdown and before a single line of model SQL.
        _load_extensions(shadow, conn)
        _register_shadow_macros(
            shadow, Path(project_dir) if project_dir else None
        )

        # Snapshot meta column names are a project-level setting, and a
        # downstream model referring to them has to bind against the names
        # this project actually writes.
        snapshot_settings = None
        if any(m.materialized == "snapshot" for m in ordered):
            from .execution import snapshot_settings_for

            snapshot_settings = snapshot_settings_for(project_dir)

        for schema in sorted({m.schema for m in ordered}):
            shadow.execute(f"CREATE SCHEMA IF NOT EXISTS {_quote_ident(schema)}")
        _seed_base_tables(shadow, conn, ordered, base_tables)

        _lock_down(shadow)

        failed: set[str] = set()
        skipped: set[str] = set()
        for model in ordered:
            broken = [d for d in model.depends_on if d in failed]
            if broken:
                result.errors[model.full_name] = [
                    BindError(
                        message=f"upstream {broken[0]} failed to bind",
                        raw="",
                        kind="upstream",
                    )
                ]
                failed.add(model.full_name)
                continue
            unbound = [d for d in model.depends_on if d in skipped]
            if unbound:
                # The upstream was skipped, not broken. Reporting an error
                # here would blame this model for the lockdown.
                result.warnings.append(
                    BindError(
                        message=(
                            f"{model.full_name}: upstream {unbound[0]} was "
                            "skipped by the bind pass"
                        ),
                        kind="skipped",
                    )
                )
                skipped.add(model.full_name)
                continue
            view = f"{_quote_ident(model.schema)}.{_quote_ident(model.name)}"
            try:
                shadow.execute(
                    f"CREATE OR REPLACE VIEW {view} AS"
                    + _shadow_body(model, snapshot_settings)
                )
                rows = shadow.execute(f"DESCRIBE {view}").fetchall()
            except Exception as e:
                if _is_file_access_denied(e):
                    result.warnings.append(
                        BindError(
                            message=f"{model.full_name}: {FILE_ACCESS_MESSAGE}",
                            raw=str(e),
                            kind="skipped",
                        )
                    )
                    skipped.add(model.full_name)
                    continue
                result.errors[model.full_name] = [
                    bind_error_from_exception(e, model.query)
                ]
                failed.add(model.full_name)
                continue
            result.schemas[model.full_name] = [
                (str(r[0]), str(r[1])) for r in rows
            ]
    finally:
        try:
            shadow.close()
        except Exception:  # pragma: no cover - defensive
            logger.debug("Bind pass could not close the shadow connection")

    result.duration_ms = int((time.perf_counter() - started) * 1000)
    return result


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------

_DUCKDB_PREFIX = re.compile(
    r"^(Binder|Catalog|Parser|Conversion|Invalid Input|Not implemented)\s+Error:\s*"
)


def as_validation_message(error: BindError) -> str:
    """Render a :class:`BindError` for a validation report.

    The wording stays "bind error". This pass resolves names and types and
    reports what the DuckDB binder refuses; it is not a type checker, and
    calling it one would be a promise the first clean-binding CAST breaks.
    DuckDB's own ``Binder Error:`` / ``Catalog Error:`` prefix is dropped so
    the line does not say "error" three times.
    """
    return "bind error: " + _DUCKDB_PREFIX.sub("", error.message)


# ---------------------------------------------------------------------------
# Buffer parsing
# ---------------------------------------------------------------------------


def model_from_buffer(
    content: str,
    *,
    path: str | Path | None = None,
    transform_dir: Path | str | None = None,
) -> SQLModel:
    """Build a :class:`SQLModel` out of unsaved editor text.

    Reads the same directives ``discover_models`` reads, from the same
    parsers, so the buffer binds exactly as the saved file would. The schema
    comes from the containing folder unless ``@config schema=`` overrides it,
    which is the convention discovery uses.

    ``path`` may be relative to the project root or absolute. A buffer with no
    path at all still binds; it is named ``scratch.buffer`` and depends on
    whatever its SQL references.

    Raises:
        ValueError: the schema or model name is not a safe SQL identifier.
    """
    from havn.engine.sql_analysis import (
        extract_table_refs,
        parse_assertion_specs,
        parse_assertions,
        parse_column_docs,
        parse_config,
        parse_depends,
        parse_description,
        parse_grain,
        parse_owner,
        parse_source_freshness,
        parse_sql,
        strip_config_comments,
    )
    from havn.engine.utils import validate_identifier

    config = parse_config(content)
    query = strip_config_comments(content)
    ast = parse_sql(query)

    file_path = Path(path) if path is not None else Path("scratch.sql")
    name = file_path.stem or "buffer"
    folder_schema = "scratch"
    if path is not None:
        parent = file_path.parent.name
        if transform_dir is not None:
            try:
                relative = file_path.relative_to(Path(transform_dir))
                parent = relative.parent.name
            except ValueError:
                pass
        folder_schema = parent or "public"
    schema = config.get("schema", folder_schema)

    validate_identifier(schema, "schema for the buffer")
    validate_identifier(name, "model name for the buffer")

    depends = parse_depends(content)
    auto_refs = extract_table_refs(query, exclude=f"{schema}.{name}", ast=ast)
    if depends:
        seen = set(depends)
        for ref in auto_refs:
            if ref not in seen:
                depends.append(ref)
                seen.add(ref)
    else:
        depends = auto_refs

    model = SQLModel(
        path=file_path,
        name=name,
        schema=schema,
        full_name=f"{schema}.{name}",
        sql=content,
        query=query,
        materialized=config.get("materialized", "view"),
        depends_on=depends,
        description=parse_description(content),
        column_docs=parse_column_docs(content),
        assertions=parse_assertions(content),
        assertion_specs=parse_assertion_specs(content),
        unique_key=config.get("unique_key"),
        incremental_strategy=config.get("incremental_strategy", "delete+insert"),
        incremental_filter=config.get("incremental_filter"),
        partition_by=config.get("partition_by"),
        watermark=config.get("watermark"),
        grain=parse_grain(content),
        owner=parse_owner(content),
        source_freshness=parse_source_freshness(content),
    )
    if ast is not None:
        model.ast = ast
    return model
