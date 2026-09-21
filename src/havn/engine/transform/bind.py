"""Shadow catalog bind pass: resolve model SQL through the DuckDB binder.

The binder is the only thing in the stack that knows every DuckDB function,
every operator overload and every column type, including the Python ``@macro``
UDFs havn registers at runtime. Rather than re-implement a type system in
sqlglot, this module hands the model SQL to DuckDB and collects what comes
back.

Mechanics:

1. Take a cursor off a **writable** connection. The read pool opens its
   connections ``read_only=True`` and DuckDB refuses ``ATTACH ':memory:'``
   there.
2. ``ATTACH ':memory:' AS shadow_<uuid8>``. ATTACH is instance-scoped, so
   the name has to be unique per call or two concurrent binds collide.
3. Every non-model object the models reference (landing tables, seeds,
   sources, anything already in the main catalog that is not a model) becomes
   a view inside the shadow that selects from the real object. Binding a view
   reads types, never rows.
4. ``USE shadow_<uuid8>``, then ``CREATE VIEW schema.name AS <model.query>``
   in topological order, with the model's SQL verbatim. Two-part names
   resolve inside the shadow first, so a stale built table in the main
   catalog is shadowed by the fresh definition. That is the property that
   makes this reflect the file rather than the last build.
5. ``DESCRIBE`` each view for its ``(name, type)`` pairs.
6. ``USE <main>; DETACH`` in a ``finally``, always.

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
import uuid
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
    out here and are seeded into the shadow as views instead.
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


def _catalog_objects(cur: duckdb.DuckDBPyConnection) -> set[str]:
    """Lowercased ``schema.name`` of every table and view in the main catalog."""
    try:
        rows = cur.execute(
            "SELECT lower(table_schema) || '.' || lower(table_name) "
            "FROM information_schema.tables"
        ).fetchall()
    except Exception as e:  # pragma: no cover - catalog always readable
        logger.debug("Could not list catalog objects for the bind pass: %s", e)
        return set()
    return {r[0] for r in rows}


def _seed_extensions(
    cur: duckdb.DuckDBPyConnection, source: duckdb.DuckDBPyConnection
) -> None:
    """Load on the bind cursor whatever the parent connection has loaded.

    Extensions are instance-scoped in DuckDB, so a cursor off the same
    connection already sees them; this is belt and braces for the case where
    the caller hands in a connection opened elsewhere. A missing ``httpfs``
    or ``spatial`` would otherwise produce a spurious "function does not
    exist", which is worse than no check at all.
    """
    try:
        rows = source.execute(
            "SELECT extension_name FROM duckdb_extensions() WHERE loaded"
        ).fetchall()
    except Exception:
        return
    for (name,) in rows:
        if not re.fullmatch(r"[a-z0-9_]+", str(name or "")):
            continue
        try:
            cur.execute(f"LOAD {name}")
        except Exception:
            logger.debug("Bind pass could not load extension %s", name)


def _seed_macros(
    source: duckdb.DuckDBPyConnection,
    project_dir: Path | None,
) -> None:
    """Make the project's Python macros resolvable from inside the shadow.

    Scalar UDFs registered with ``create_function`` live at instance level and
    are visible from any cursor, including inside an attached database. The
    public-name ``CREATE MACRO`` aliases do not: they live in the main
    catalog's ``main`` schema, which drops out of the search path the moment
    the cursor does ``USE shadow``. Keeping the main catalog on the search
    path (see :func:`bind_models`) is what brings those back.

    Registration goes on the parent connection, never on the bind cursor.
    Aliases created while inside the shadow die with the DETACH, so the next
    bind would lose them and report a spurious "function does not exist" --
    the one failure mode that makes this check worse than no check at all.
    ``register_macros`` is idempotent per connection, so on the server (where
    the write connection already has them) this is a no-op.
    """
    if project_dir is None:
        return
    try:
        from havn.engine.macros import register_macros

        register_macros(source, Path(project_dir))
    except Exception as e:
        # Sibling connections share the UDF catalog, so a second registration
        # reports "already exists" and the functions are callable anyway.
        logger.debug("Bind pass macro registration skipped: %s", e)


def _create_typed_stub(
    cur: duckdb.DuckDBPyConnection,
    shadow: str,
    schema: str,
    table: str,
    columns: list[tuple[str, str]],
) -> None:
    """Create an empty, correctly typed view for a base table given by spec."""
    if not columns:
        return
    projection = ", ".join(
        f"CAST(NULL AS {ctype}) AS {_quote_ident(cname)}" for cname, ctype in columns
    )
    cur.execute(
        f"CREATE OR REPLACE VIEW {_quote_ident(shadow)}.{_quote_ident(schema)}."
        f"{_quote_ident(table)} AS "
        f"SELECT * FROM (SELECT {projection}) AS _stub WHERE false"
    )


def _seed_base_tables(
    cur: duckdb.DuckDBPyConnection,
    shadow: str,
    main_catalog: str,
    models: list[SQLModel],
    base_tables: dict[str, list[tuple[str, str]]] | None,
) -> None:
    """Mirror every non-model object the models reference into the shadow."""
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

    existing = _catalog_objects(cur)
    lowered_specs = {k.lower(): v for k, v in (base_tables or {}).items()}

    created_schemas: set[str] = set()
    for key, original in sorted(wanted.items()):
        parts = _split_two_part(original)
        if parts is None:
            continue
        schema, table = parts
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", table
        ):
            continue
        if schema not in created_schemas:
            cur.execute(
                f"CREATE SCHEMA IF NOT EXISTS {_quote_ident(shadow)}.{_quote_ident(schema)}"
            )
            created_schemas.add(schema)
        spec = lowered_specs.get(key)
        if spec:
            try:
                _create_typed_stub(cur, shadow, schema, table, spec)
            except Exception as e:
                logger.debug("Bind pass could not stub %s: %s", original, e)
            continue
        if key not in existing:
            # Not a model and not in the catalog: leave it missing so the
            # binder reports "Table ... does not exist" on the line that
            # references it, which is the honest answer.
            continue
        try:
            cur.execute(
                f"CREATE OR REPLACE VIEW {_quote_ident(shadow)}.{_quote_ident(schema)}."
                f"{_quote_ident(table)} AS SELECT * FROM "
                f"{_quote_ident(main_catalog)}.{_quote_ident(schema)}.{_quote_ident(table)}"
            )
        except Exception as e:
            logger.debug("Bind pass could not mirror %s: %s", original, e)


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


def bind_models(
    conn: duckdb.DuckDBPyConnection,
    models: list[SQLModel],
    *,
    base_tables: dict[str, list[tuple[str, str]]] | None = None,
    project_dir: Path | str | None = None,
) -> BindResult:
    """Bind ``models`` against a throwaway shadow catalog and report back.

    Args:
        conn: A **writable** DuckDB connection. A cursor is taken off it; the
            connection itself is never mutated beyond an ATTACH and DETACH of
            a uniquely named in-memory catalog.
        models: The models to bind. Callers that only care about one model
            should pass :func:`ancestor_closure` of it, not the whole project.
        base_tables: Explicit ``schema.table -> [(column, type)]`` specs for
            base objects that are not in the catalog yet. Anything not listed
            here is mirrored from the main catalog when it exists.
        project_dir: Used to register Python macros on the bind cursor when
            the connection does not already have them.

    Returns:
        A :class:`BindResult`. ``available`` is False, with a single warning,
        when the backend cannot host a shadow catalog.
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

    shadow = f"shadow_{uuid.uuid4().hex[:8]}"
    try:
        cur = conn.cursor()
    except Exception as e:  # pragma: no cover - only a closed connection
        return _unavailable(str(e), started)

    attached = False
    main_catalog = "memory"
    try:
        try:
            main_catalog = cur.execute("SELECT current_database()").fetchone()[0]
        except Exception as e:
            return _unavailable(str(e), started)
        try:
            cur.execute(f"ATTACH ':memory:' AS {_quote_ident(shadow)}")
            attached = True
        except Exception as e:
            # DuckLake attaches the catalog itself and has historically been
            # picky about a second attach in-process. Degrade to a warning
            # rather than failing the caller's validation run.
            return _unavailable(str(e), started)

        _seed_extensions(cur, conn)
        _seed_macros(conn, Path(project_dir) if project_dir else None)

        for schema in sorted({m.schema for m in ordered}):
            cur.execute(
                f"CREATE SCHEMA IF NOT EXISTS {_quote_ident(shadow)}.{_quote_ident(schema)}"
            )
        _seed_base_tables(cur, shadow, main_catalog, ordered, base_tables)

        cur.execute(f"USE {_quote_ident(shadow)}")
        # Keeping the main catalog on the search path behind the shadow is
        # what makes CREATE MACRO aliases (the public names of the project's
        # Python macros) resolve while the cursor is inside the shadow. The
        # shadow comes first, so a model still shadows a stale built table of
        # the same two-part name.
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(main_catalog)):
            try:
                cur.execute(
                    f"SET search_path = '{shadow}.main,{main_catalog}.main'"
                )
            except Exception:
                logger.debug("Bind pass could not extend the search path")

        failed: set[str] = set()
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
            view = f"{_quote_ident(model.schema)}.{_quote_ident(model.name)}"
            try:
                cur.execute(f"CREATE OR REPLACE VIEW {view} AS\n{model.query}")
                rows = cur.execute(f"DESCRIBE {view}").fetchall()
            except Exception as e:
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
            cur.execute(f"USE {_quote_ident(main_catalog)}")
        except Exception:
            logger.debug("Bind pass could not restore the default catalog")
        if attached:
            try:
                cur.execute(f"DETACH {_quote_ident(shadow)}")
            except Exception:
                logger.warning("Bind pass could not detach %s", shadow)
        try:
            cur.close()
        except Exception:
            pass

    result.duration_ms = int((time.perf_counter() - started) * 1000)
    return result
