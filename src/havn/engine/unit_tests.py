"""Unit tests for SQL models: fixed input rows in, expected rows out.

A unit test declares mock rows for every upstream a model reads and the rows
the model should produce from them. The test runs on a fresh in-memory DuckDB
connection with the project's macros registered, so it needs no warehouse and
touches no real data. That is the property worth keeping: a unit test can
never pass because of what happens to be sitting in ``warehouse.duckdb``.

Tests live in ``tests/unit/*.yml`` inside the project:

.. code-block:: yaml

    model: silver.customers
    tests:
      - name: counts orders per customer
        given:
          bronze.customers:
            columns: {customer_id: INTEGER, name: VARCHAR}
            rows:
              - [1, "Ann"]
              - {customer_id: 2, name: "Bo"}
          bronze.orders:
            rows:
              - {order_id: 1, customer_id: 1, amount: 5.0}
        expect:
          rows:
            - {customer_id: 1, name: "Ann", order_count: 1}
            - {customer_id: 2, name: "Bo", order_count: 0}
          ordered: false

Execution, per test:

1. ``duckdb.connect(":memory:")``, extensions loaded, macros registered.
2. One TEMP TABLE per ``given`` entry. Column types come from ``columns:``
   when declared, else from the live warehouse catalog when the caller
   supplies one, else from the Python types of the fixture values.
3. The model's query is rewritten with
   :func:`havn.engine.sql_rewrite.rewrite_table_refs` so every upstream
   reference points at its mock. An upstream in ``depends_on`` with no mock
   is a test error, never a fallback to the warehouse.
4. The rewritten query runs into a temp table and is compared against the
   expected rows with the row-hash comparator from :mod:`havn.engine.diff`,
   so DECIMAL/DOUBLE drift and other type noise cannot fail a test on their
   own. Comparison is multiset-based (duplicates count) unless
   ``ordered: true``, which compares positionally.

Incremental models are tested against their full-refresh query only: the
``incremental_filter`` is not applied, because there is no prior state to
filter against.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import sqlglot
import yaml

from havn.engine.diff import _row_hash_expr
from havn.engine.sql_rewrite import SQLRewriteError, rewrite_table_refs
from havn.engine.utils import validate_identifier

logger = logging.getLogger("havn.unit_tests")

# Extensions the in-memory test connection loads. ``database.connect()``
# installs nothing itself (DuckDB auto-loads what a query needs), so this is
# a best-effort LOAD of the ones that ship statically with the Python build;
# a mock run never talks to a remote catalog, so ducklake/postgres/httpfs are
# deliberately absent.
_TEST_EXTENSIONS = ("json", "parquet", "icu")

_SAMPLE_LIMIT = 20
_MAX_NAME_LEN = 200

# A column type is written into DDL, so keep it to the shape of a type
# expression: names, sizes, nested type syntax. No quotes, semicolons or
# comment markers.
_TYPE_RE = re.compile(r"^[A-Za-z0-9_ ,()\[\]]+$")

_MOCK_PREFIX = "_havn_ut_mock_"
_ACTUAL_TABLE = "_havn_ut_actual"
_EXPECTED_TABLE = "_havn_ut_expected"
_POS_COLUMN = "_havn_ut_pos"
_HASH_COLUMN = "_havn_ut_hash"
_RANK_COLUMN = "_havn_ut_rank"


class UnitTestError(ValueError):
    """A unit test definition is invalid."""


# --------------------------------------------------------------------------
# Definitions
# --------------------------------------------------------------------------


@dataclass
class MockTable:
    """Fixture rows standing in for one upstream table."""

    ref: str                                       # "bronze.customers"
    columns: dict[str, str] = field(default_factory=dict)   # declared name -> type
    rows: list[Any] = field(default_factory=list)  # each a list or a dict

    def to_dict(self) -> dict:
        return {"ref": self.ref, "columns": dict(self.columns), "row_count": len(self.rows)}


@dataclass
class ExpectedRows:
    """The rows a model should produce, and how strictly to compare them."""

    rows: list[Any] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)   # required for list-form rows
    ordered: bool = False


@dataclass
class UnitTestCase:
    """One declared unit test."""

    name: str
    model: str
    given: dict[str, MockTable]
    expect: ExpectedRows
    description: str = ""
    source_path: str = ""

    @property
    def key(self) -> str:
        return f"{self.model}::{self.name}"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "model": self.model,
            "description": self.description,
            "source_path": self.source_path,
            "given": [m.to_dict() for m in self.given.values()],
            "expected_row_count": len(self.expect.rows),
            "ordered": self.expect.ordered,
        }


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass
class UnitTestResult:
    """Outcome of running a single unit test."""

    name: str
    model: str
    status: str = "pass"                # "pass", "fail", or "error"
    duration_ms: int = 0
    message: str = ""
    source_path: str = ""
    columns: list[str] = field(default_factory=list)
    missing_rows: list[dict] = field(default_factory=list)      # expected, not produced
    unexpected_rows: list[dict] = field(default_factory=list)   # produced, not expected
    missing_count: int = 0
    unexpected_count: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "model": self.model,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "message": self.message,
            "source_path": self.source_path,
            "columns": list(self.columns),
            "missing_rows": self.missing_rows,
            "unexpected_rows": self.unexpected_rows,
            "missing_count": self.missing_count,
            "unexpected_count": self.unexpected_count,
            "warnings": list(self.warnings),
        }


@dataclass
class UnitTestRunResult:
    """Outcome of a whole unit-test run."""

    results: list[UnitTestResult] = field(default_factory=list)
    load_errors: list[str] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == "pass")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "fail")

    @property
    def errored(self) -> int:
        return sum(1 for r in self.results if r.status == "error")

    @property
    def ok(self) -> bool:
        """True when nothing failed, errored, or failed to load."""
        return not self.failed and not self.errored and not self.load_errors

    def to_dict(self) -> dict:
        return {
            "results": [r.to_dict() for r in self.results],
            "load_errors": list(self.load_errors),
            "duration_ms": self.duration_ms,
            "passed": self.passed,
            "failed": self.failed,
            "errored": self.errored,
            "ok": self.ok,
        }


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def unit_tests_dir(project_dir: str | Path) -> Path:
    """Return the project's unit-test directory (``<project>/tests/unit``)."""
    return Path(project_dir) / "tests" / "unit"


def load_unit_tests(project_dir: str | Path) -> tuple[list[UnitTestCase], list[str]]:
    """Load every unit test under ``<project>/tests/unit``.

    Returns ``(tests, errors)``. Like :func:`havn.engine.semantic.load_metrics`,
    a broken file or entry is collected as an error string and skipped rather
    than aborting the load, so one bad fixture doesn't hide every other test.
    """
    root = unit_tests_dir(project_dir)
    tests: list[UnitTestCase] = []
    errors: list[str] = []
    if not root.is_dir():
        return tests, errors

    files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix in (".yml", ".yaml")
    )
    seen: dict[str, str] = {}
    for path in files:
        rel = path.relative_to(root).as_posix()
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            errors.append(f"{rel}: invalid YAML ({e})")
            continue
        if not isinstance(raw, dict):
            errors.append(f"{rel}: top level must be a mapping with 'model' and 'tests' keys")
            continue

        try:
            default_model = _check_model(raw.get("model"))
        except UnitTestError as e:
            errors.append(f"{rel}: {e}")
            continue

        entries = raw.get("tests")
        if entries is None:
            errors.append(f"{rel}: missing 'tests' list")
            continue
        if not isinstance(entries, list):
            errors.append(f"{rel}: 'tests' must be a list")
            continue

        for entry in entries:
            try:
                case = _parse_case(entry, default_model=default_model, source_path=rel)
            except UnitTestError as e:
                errors.append(f"{rel}: {e}")
                continue
            if case.key in seen:
                errors.append(
                    f"{rel}: duplicate test {case.name!r} for {case.model} "
                    f"(first defined in {seen[case.key]})"
                )
                continue
            seen[case.key] = rel
            tests.append(case)

    return tests, errors


def _check_model(value: Any) -> str | None:
    if value is None:
        return None
    model = str(value).strip()
    if not model:
        return None
    parts = model.split(".")
    if len(parts) not in (1, 2):
        raise UnitTestError(f"model must be 'table' or 'schema.table', got {model!r}")
    for part in parts:
        try:
            validate_identifier(part, "model")
        except ValueError as e:
            raise UnitTestError(str(e))
    return model


def _check_type(value: Any, label: str) -> str:
    text = str(value).strip()
    if not text:
        raise UnitTestError(f"{label}: column type must not be empty")
    if not _TYPE_RE.match(text):
        raise UnitTestError(f"{label}: invalid column type {text!r}")
    return text


def _parse_case(raw: Any, default_model: str | None, source_path: str) -> UnitTestCase:
    if not isinstance(raw, dict):
        raise UnitTestError(f"test entry must be a mapping, got {type(raw).__name__}")

    name = str(raw.get("name", "")).strip()
    if not name:
        raise UnitTestError("test entry is missing 'name'")
    if len(name) > _MAX_NAME_LEN:
        raise UnitTestError(f"test name too long (max {_MAX_NAME_LEN} chars)")
    if "\n" in name:
        raise UnitTestError(f"test name {name!r} must be a single line")

    model = _check_model(raw.get("model")) or default_model
    if not model:
        raise UnitTestError(f"test {name!r}: no 'model' set on the test or the file")

    given_raw = raw.get("given")
    if not isinstance(given_raw, dict) or not given_raw:
        raise UnitTestError(f"test {name!r}: 'given' must be a non-empty mapping of table -> fixture")
    given: dict[str, MockTable] = {}
    for ref_raw, spec in given_raw.items():
        ref = _check_model(ref_raw)
        if not ref:
            raise UnitTestError(f"test {name!r}: empty table name in 'given'")
        key = ref.lower()
        if key in given:
            raise UnitTestError(f"test {name!r}: duplicate 'given' entry for {ref}")
        given[key] = _parse_mock(ref, spec, label=f"test {name!r}: given {ref}")

    expect = _parse_expect(raw.get("expect"), label=f"test {name!r}")

    return UnitTestCase(
        name=name,
        model=model,
        given=given,
        expect=expect,
        description=str(raw.get("description", "")),
        source_path=source_path,
    )


def _parse_mock(ref: str, spec: Any, label: str) -> MockTable:
    if not isinstance(spec, dict):
        raise UnitTestError(f"{label}: fixture must be a mapping with 'rows' or 'csv'")

    columns: dict[str, str] = {}
    cols_raw = spec.get("columns")
    if cols_raw is not None:
        columns = _parse_columns(cols_raw, label)

    rows = _parse_rows(spec, label, declared=list(columns))
    return MockTable(ref=ref, columns=columns, rows=rows)


def _parse_columns(cols_raw: Any, label: str) -> dict[str, str]:
    columns: dict[str, str] = {}
    if isinstance(cols_raw, dict):
        items = list(cols_raw.items())
    elif isinstance(cols_raw, list):
        # A bare list declares names without types.
        items = [(c, "VARCHAR") for c in cols_raw]
    else:
        raise UnitTestError(f"{label}: 'columns' must be a mapping or a list")
    for col, col_type in items:
        try:
            name = validate_identifier(str(col), "column")
        except ValueError as e:
            raise UnitTestError(f"{label}: {e}")
        if name.lower() in {c.lower() for c in columns}:
            raise UnitTestError(f"{label}: duplicate column {name!r}")
        columns[name] = _check_type(col_type, label)
    return columns


def _parse_rows(spec: dict, label: str, declared: list[str]) -> list[Any]:
    """Read ``rows:`` (list form) or ``format: csv`` + ``csv:`` (dbt's shape)."""
    fmt = str(spec.get("format", "") or "").strip().lower()
    if fmt and fmt not in ("dict", "rows", "csv"):
        raise UnitTestError(f"{label}: unsupported format {fmt!r} (use 'csv' or omit)")

    if fmt == "csv" or "csv" in spec:
        text = spec.get("csv")
        if text is None:
            raise UnitTestError(f"{label}: format csv needs a 'csv' string")
        if not isinstance(text, str):
            raise UnitTestError(f"{label}: 'csv' must be a string")
        return _rows_from_csv(text, label)

    rows = spec.get("rows")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise UnitTestError(f"{label}: 'rows' must be a list")
    for row in rows:
        if not isinstance(row, (list, dict)):
            raise UnitTestError(
                f"{label}: each row must be a list or a mapping, got {type(row).__name__}"
            )
        if isinstance(row, list) and declared and len(row) != len(declared):
            raise UnitTestError(
                f"{label}: row has {len(row)} value(s) but {len(declared)} column(s) declared"
            )
    return rows


def _rows_from_csv(text: str, label: str) -> list[dict]:
    """Parse an inline CSV block into dict rows.

    An empty field is NULL. Values that look like integers or floats are
    converted, so a CSV fixture behaves like the list form; anything else
    stays a string and is cast by DuckDB on insert when the column has a
    declared or catalog type.
    """
    reader = csv.reader(io.StringIO(text.strip("\n")))
    try:
        rows = list(reader)
    except csv.Error as e:
        raise UnitTestError(f"{label}: invalid CSV ({e})")
    if not rows:
        return []
    header = [h.strip() for h in rows[0]]
    for col in header:
        try:
            validate_identifier(col, "column")
        except ValueError as e:
            raise UnitTestError(f"{label}: {e}")
    out: list[dict] = []
    for i, raw_row in enumerate(rows[1:], start=2):
        if not raw_row or all(v.strip() == "" for v in raw_row) and len(raw_row) < len(header):
            continue
        if len(raw_row) != len(header):
            raise UnitTestError(
                f"{label}: CSV line {i} has {len(raw_row)} field(s), header has {len(header)}"
            )
        out.append({col: _csv_value(v) for col, v in zip(header, raw_row)})
    return out


def _csv_value(value: str) -> Any:
    text = value.strip()
    if text == "":
        return None
    for caster in (int, float):
        try:
            return caster(text)
        except ValueError:
            continue
    return text


def _parse_expect(raw: Any, label: str) -> ExpectedRows:
    if raw is None:
        raise UnitTestError(f"{label}: missing 'expect'")
    if isinstance(raw, list):
        raw = {"rows": raw}
    if not isinstance(raw, dict):
        raise UnitTestError(f"{label}: 'expect' must be a mapping or a list of rows")

    columns: list[str] = []
    cols_raw = raw.get("columns")
    if cols_raw is not None:
        columns = list(_parse_columns(cols_raw, f"{label}: expect"))

    ordered = raw.get("ordered", False)
    if not isinstance(ordered, bool):
        raise UnitTestError(f"{label}: 'ordered' must be true or false")

    rows = _parse_rows(raw, f"{label}: expect", declared=columns)
    for row in rows:
        if isinstance(row, list) and not columns:
            raise UnitTestError(
                f"{label}: expect rows given as lists need an 'expect.columns' declaration"
            )
    return ExpectedRows(rows=rows, columns=columns, ordered=ordered)


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


CATALOG_SQL = (
    "SELECT lower(table_schema), lower(table_name), column_name, data_type "
    "FROM information_schema.columns "
    "WHERE table_schema NOT IN ('information_schema', 'pg_catalog') "
    "ORDER BY table_schema, table_name, ordinal_position"
)


def catalog_from_rows(rows) -> dict[str, list[tuple[str, str]]]:
    """Fold :data:`CATALOG_SQL` rows into ``schema.table -> [(column, type)]``."""
    catalog: dict[str, list[tuple[str, str]]] = {}
    for schema, table, column, data_type in rows:
        catalog.setdefault(f"{schema}.{table}", []).append((column, data_type))
    return catalog


def catalog_from_connection(conn: duckdb.DuckDBPyConnection) -> dict[str, list[tuple[str, str]]]:
    """Snapshot ``schema.table -> [(column, type), ...]`` from the warehouse.

    Callers (CLI, API, MCP) pass the result into :func:`run_unit_tests` so
    mocks without a ``columns:`` block get the real column types, and so a
    mock narrower than the real table can be warned about. The unit-test run
    itself never queries the warehouse.
    """
    try:
        rows = conn.execute(CATALOG_SQL).fetchall()
    except Exception as e:  # a locked or missing warehouse must not fail the run
        logger.debug("Could not read catalog for unit tests: %s", e)
        return {}
    return catalog_from_rows(rows)


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


def run_unit_tests(
    project_dir: str | Path,
    *,
    model: str | None = None,
    catalog: dict[str, list[tuple[str, str]]] | None = None,
    cases: list[UnitTestCase] | None = None,
    sample_limit: int = _SAMPLE_LIMIT,
) -> UnitTestRunResult:
    """Load and run the project's unit tests.

    Args:
        project_dir: The havn project directory.
        model: Only run tests for this model (``silver.customers`` or
            ``customers``).
        catalog: Optional ``schema.table -> [(column, type)]`` snapshot from
            :func:`catalog_from_connection`, used for mock column types and
            narrow-mock warnings.
        cases: Pre-loaded tests, skipping discovery (used by callers that
            already listed them).
        sample_limit: Max differing rows reported per test.
    """
    from havn.engine.transform import discover_all_models

    started = time.perf_counter()
    if cases is None:
        cases, load_errors = load_unit_tests(project_dir)
    else:
        load_errors = []

    if model:
        wanted = model.lower()
        cases = [c for c in cases if c.model.lower() == wanted or c.model.lower().split(".")[-1] == wanted]

    result = UnitTestRunResult(load_errors=load_errors)
    if not cases:
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result

    models = {m.full_name.lower(): m for m in discover_all_models(Path(project_dir))}
    by_short: dict[str, Any] = {}
    for m in models.values():
        by_short.setdefault(m.name.lower(), m)

    for case in cases:
        result.results.append(
            _run_case(
                case,
                project_dir=Path(project_dir),
                models=models,
                by_short=by_short,
                catalog=catalog or {},
                sample_limit=sample_limit,
            )
        )

    result.duration_ms = int((time.perf_counter() - started) * 1000)
    return result


def _run_case(
    case: UnitTestCase,
    *,
    project_dir: Path,
    models: dict,
    by_short: dict,
    catalog: dict[str, list[tuple[str, str]]],
    sample_limit: int,
) -> UnitTestResult:
    started = time.perf_counter()
    res = UnitTestResult(name=case.name, model=case.model, source_path=case.source_path)

    def finish(status: str, message: str = "") -> UnitTestResult:
        res.status = status
        if message:
            res.message = message
        res.duration_ms = int((time.perf_counter() - started) * 1000)
        return res

    sql_model = models.get(case.model.lower()) or by_short.get(case.model.lower())
    if sql_model is None:
        available = ", ".join(sorted(models)) or "none"
        return finish("error", f"unknown model {case.model!r} (available: {available})")

    missing_mocks = [
        dep for dep in sql_model.depends_on if dep.lower() not in case.given
    ]
    if missing_mocks:
        return finish(
            "error",
            "no mock for upstream " + ", ".join(sorted(missing_mocks))
            + ": every upstream must be mocked, unit tests never read the warehouse",
        )

    declared_deps = {d.lower() for d in sql_model.depends_on}
    for ref in case.given:
        if ref not in declared_deps:
            res.warnings.append(
                f"mock for {ref} is not an upstream of {sql_model.full_name}; it will be ignored"
            )

    # A narrow mock only distorts the result when the model expands a star:
    # an explicit column list is unaffected by columns the mock leaves out.
    star_model = _selects_star(sql_model.query)

    conn = duckdb.connect(":memory:")
    try:
        _prepare_connection(conn, project_dir)

        mapping: dict[str, str] = {}
        for index, (ref, mock) in enumerate(sorted(case.given.items())):
            table_name = f"{_MOCK_PREFIX}{index}"
            try:
                warnings = _create_mock_table(
                    conn, table_name, mock, catalog, warn_narrow=star_model
                )
            except UnitTestError as e:
                return finish("error", str(e))
            except Exception as e:
                return finish("error", f"could not build mock for {mock.ref}: {e}")
            res.warnings.extend(warnings)
            mapping[ref] = table_name

        try:
            rewritten = rewrite_table_refs(sql_model.query, mapping)
        except SQLRewriteError as e:
            return finish("error", f"could not rewrite model SQL: {e}")

        try:
            conn.execute(
                f"CREATE TEMP TABLE {_ACTUAL_TABLE} AS "
                f"SELECT row_number() OVER () AS {_POS_COLUMN}, * "
                f"FROM ({rewritten}) AS _havn_ut_sub"
            )
        except Exception as e:
            return finish("error", f"model SQL failed: {e}")

        actual_columns = _table_columns(conn, _ACTUAL_TABLE)
        actual_columns = [c for c in actual_columns if c[0] != _POS_COLUMN]
        res.columns = [c for c, _ in actual_columns]

        expected_columns = _expected_column_names(case.expect)
        mismatch = _column_mismatch(res.columns, expected_columns)
        if mismatch:
            return finish("fail", mismatch)

        try:
            _create_expected_table(conn, case.expect, actual_columns)
        except UnitTestError as e:
            return finish("error", str(e))
        except Exception as e:
            return finish("error", f"could not build expected rows: {e}")

        if case.expect.ordered:
            missing, unexpected = _compare_ordered(conn, res.columns, sample_limit)
        else:
            missing, unexpected = _compare_unordered(conn, res.columns, sample_limit)

        res.missing_count = missing[0]
        res.unexpected_count = unexpected[0]
        res.missing_rows = missing[1]
        res.unexpected_rows = unexpected[1]

        if res.missing_count or res.unexpected_count:
            return finish(
                "fail",
                f"{res.missing_count} expected row(s) missing, "
                f"{res.unexpected_count} unexpected row(s)",
            )
        return finish("pass")
    finally:
        conn.close()


def _prepare_connection(conn: duckdb.DuckDBPyConnection, project_dir: Path) -> None:
    from havn.engine.macros import register_macros

    # Single-threaded with insertion order preserved: `ordered: true` compares
    # positionally, so the model's output order has to be reproducible.
    for setting in ("SET threads = 1", "SET preserve_insertion_order = true"):
        try:
            conn.execute(setting)
        except Exception:  # pragma: no cover - defensive
            pass
    for ext in _TEST_EXTENSIONS:
        try:
            conn.execute(f"LOAD {ext}")
        except Exception:
            logger.debug("Extension %s not available for unit tests", ext)
    register_macros(conn, project_dir)


def _selects_star(sql: str) -> bool:
    """True when the query projects a star (``SELECT *`` or ``SELECT t.*``).

    ``count(*)`` does not count: only a star in a select list pulls in whatever
    columns the source happens to have, which is what makes a narrow mock
    misleading.
    """
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:
        return True  # unparseable: assume the worst and keep the warning
    for select in tree.find_all(exp.Select):
        for projection in select.expressions:
            if isinstance(projection, exp.Star):
                return True
            if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
                return True
    return False


def _create_mock_table(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    mock: MockTable,
    catalog: dict[str, list[tuple[str, str]]],
    *,
    warn_narrow: bool = True,
) -> list[str]:
    """Create one TEMP TABLE for a mocked upstream. Returns warnings."""
    warnings: list[str] = []
    catalog_cols = catalog.get(mock.ref.lower(), [])
    catalog_types = {c.lower(): t for c, t in catalog_cols}

    if mock.columns:
        columns = list(mock.columns.items())
    elif catalog_cols:
        # No declared columns: take the real table's shape, restricted to the
        # columns the fixture actually supplies so the rows still line up.
        supplied = _row_column_names(mock.rows)
        if supplied:
            columns = [
                (c, catalog_types.get(c.lower()) or _infer_type(mock.rows, c))
                for c in supplied
            ]
        else:
            columns = list(catalog_cols)
    else:
        supplied = _row_column_names(mock.rows)
        if not supplied:
            raise UnitTestError(
                f"mock for {mock.ref} has no columns: declare 'columns:' or use mapping rows"
            )
        columns = [(c, _infer_type(mock.rows, c)) for c in supplied]

    if catalog_cols and warn_narrow:
        declared = {c.lower() for c, _ in columns}
        narrower = [c for c, _ in catalog_cols if c.lower() not in declared]
        if narrower:
            warnings.append(
                f"mock for {mock.ref} omits {len(narrower)} column(s) present in the warehouse "
                f"({', '.join(narrower[:5])}{'...' if len(narrower) > 5 else ''}); "
                "a SELECT * model is being tested against a narrower table"
            )

    ddl_cols = ", ".join(f'"{name}" {col_type}' for name, col_type in columns)
    conn.execute(f"CREATE TEMP TABLE {table_name} ({ddl_cols})")

    names = [name for name, _ in columns]
    values = [_row_values(row, names, mock.ref) for row in mock.rows]
    if values:
        placeholders = ", ".join("?" for _ in names)
        quoted = ", ".join(f'"{n}"' for n in names)
        try:
            conn.executemany(
                f"INSERT INTO {table_name} ({quoted}) VALUES ({placeholders})", values
            )
        except Exception as e:
            raise UnitTestError(f"mock for {mock.ref}: could not insert rows ({e})")
    return warnings


def _row_column_names(rows: list[Any]) -> list[str]:
    """Union of mapping-row keys, in first-seen order."""
    names: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            for key in row:
                if key not in names:
                    names.append(str(key))
    return names


def _infer_type(rows: list[Any], column: str) -> str:
    """Infer a DuckDB type from the first non-null Python value in a column."""
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get(column)
        if value is None:
            continue
        return _duckdb_type(value)
    return "VARCHAR"


def _duckdb_type(value: Any) -> str:
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        return "BIGINT"
    if isinstance(value, float):
        return "DOUBLE"
    if isinstance(value, _dt.datetime):
        return "TIMESTAMP"
    if isinstance(value, _dt.date):
        return "DATE"
    return "VARCHAR"


def _row_values(row: Any, names: list[str], label: str) -> list[Any]:
    if isinstance(row, dict):
        unknown = [k for k in row if str(k) not in names]
        if unknown:
            raise UnitTestError(
                f"{label}: row has undeclared column(s) {', '.join(sorted(map(str, unknown)))}"
            )
        return [row.get(name) for name in names]
    if len(row) != len(names):
        raise UnitTestError(
            f"{label}: row has {len(row)} value(s) but the table has {len(names)} column(s)"
        )
    return list(row)


def _table_columns(conn: duckdb.DuckDBPyConnection, table: str) -> list[tuple[str, str]]:
    rows = conn.execute(f"DESCRIBE {table}").fetchall()
    return [(r[0], r[1]) for r in rows]


def _expected_column_names(expect: ExpectedRows) -> list[str]:
    if expect.columns:
        return list(expect.columns)
    return _row_column_names(expect.rows)


def _column_mismatch(actual: list[str], expected: list[str]) -> str:
    if not expected:
        return ""
    actual_set = {c.lower() for c in actual}
    expected_set = {c.lower() for c in expected}
    if actual_set == expected_set:
        return ""
    only_expected = sorted(expected_set - actual_set)
    only_actual = sorted(actual_set - expected_set)
    parts = []
    if only_expected:
        parts.append(f"expected but not produced: {', '.join(only_expected)}")
    if only_actual:
        parts.append(f"produced but not expected: {', '.join(only_actual)}")
    return "column mismatch (" + "; ".join(parts) + ")"


def _create_expected_table(
    conn: duckdb.DuckDBPyConnection,
    expect: ExpectedRows,
    actual_columns: list[tuple[str, str]],
) -> None:
    """Materialize the expected rows using the actual output's column types.

    Casting the fixture into the model's own types is what makes
    ``order_count: 1`` compare equal to a BIGINT and ``amount: 5.0`` equal to
    a DECIMAL: the row hash then sees the same string on both sides.
    """
    ddl_cols = ", ".join(f'"{name}" {col_type}' for name, col_type in actual_columns)
    conn.execute(
        f'CREATE TEMP TABLE {_EXPECTED_TABLE} ("{_POS_COLUMN}" BIGINT, {ddl_cols})'
    )
    names = [name for name, _ in actual_columns]
    by_lower = {n.lower(): n for n in names}
    values: list[list[Any]] = []
    for position, row in enumerate(expect.rows, start=1):
        if isinstance(row, dict):
            normalized = {}
            for key, value in row.items():
                target = by_lower.get(str(key).lower())
                if target is None:
                    raise UnitTestError(f"expected row references unknown column {key!r}")
                normalized[target] = value
            values.append([position] + [normalized.get(n) for n in names])
        else:
            ordered_names = expect.columns or names
            if len(row) != len(ordered_names):
                raise UnitTestError(
                    f"expected row has {len(row)} value(s) but {len(ordered_names)} column(s)"
                )
            mapping = dict(zip([by_lower.get(c.lower(), c) for c in ordered_names], row))
            values.append([position] + [mapping.get(n) for n in names])

    if values:
        placeholders = ", ".join("?" for _ in range(len(names) + 1))
        try:
            conn.executemany(
                f"INSERT INTO {_EXPECTED_TABLE} VALUES ({placeholders})", values
            )
        except Exception as e:
            raise UnitTestError(f"expected rows do not fit the model's output types: {e}")


def _compare_unordered(
    conn: duckdb.DuckDBPyConnection,
    columns: list[str],
    sample_limit: int,
) -> tuple[tuple[int, list[dict]], tuple[int, list[dict]]]:
    """Multiset comparison: duplicates count, order does not."""
    hash_expr = _row_hash_expr(columns)
    select_cols = ", ".join(f'"{c}"' for c in columns)
    for alias, source in (("_havn_ut_a", _ACTUAL_TABLE), ("_havn_ut_e", _EXPECTED_TABLE)):
        conn.execute(
            f"CREATE TEMP TABLE {alias} AS SELECT {select_cols}, "
            f"{hash_expr} AS {_HASH_COLUMN}, "
            f"row_number() OVER (PARTITION BY {hash_expr}) AS {_RANK_COLUMN} "
            f"FROM {source}"
        )

    def diff(left: str, right: str) -> tuple[int, list[dict]]:
        where = (
            f"NOT EXISTS (SELECT 1 FROM {right} r "
            f"WHERE r.{_HASH_COLUMN} = l.{_HASH_COLUMN} "
            f"AND r.{_RANK_COLUMN} = l.{_RANK_COLUMN})"
        )
        count = conn.execute(f"SELECT count(*) FROM {left} l WHERE {where}").fetchone()[0]
        rows = conn.execute(
            f"SELECT {select_cols} FROM {left} l WHERE {where} LIMIT {int(sample_limit)}"
        ).fetchall()
        return count, [dict(zip(columns, row)) for row in rows]

    missing = diff("_havn_ut_e", "_havn_ut_a")
    unexpected = diff("_havn_ut_a", "_havn_ut_e")
    return missing, unexpected


def _compare_ordered(
    conn: duckdb.DuckDBPyConnection,
    columns: list[str],
    sample_limit: int,
) -> tuple[tuple[int, list[dict]], tuple[int, list[dict]]]:
    """Positional comparison: row N of the output must equal row N of expect."""
    hash_expr = _row_hash_expr(columns)
    select_cols = ", ".join(f'"{c}"' for c in columns)
    for alias, source in (("_havn_ut_a", _ACTUAL_TABLE), ("_havn_ut_e", _EXPECTED_TABLE)):
        conn.execute(
            f"CREATE TEMP TABLE {alias} AS SELECT {_POS_COLUMN}, {select_cols}, "
            f"{hash_expr} AS {_HASH_COLUMN} FROM {source}"
        )

    def diff(left: str, right: str) -> tuple[int, list[dict]]:
        where = (
            f"NOT EXISTS (SELECT 1 FROM {right} r "
            f"WHERE r.{_POS_COLUMN} = l.{_POS_COLUMN} "
            f"AND r.{_HASH_COLUMN} = l.{_HASH_COLUMN})"
        )
        count = conn.execute(f"SELECT count(*) FROM {left} l WHERE {where}").fetchone()[0]
        rows = conn.execute(
            f"SELECT {select_cols} FROM {left} l WHERE {where} "
            f"ORDER BY {_POS_COLUMN} LIMIT {int(sample_limit)}"
        ).fetchall()
        return count, [dict(zip(columns, row)) for row in rows]

    missing = diff("_havn_ut_e", "_havn_ut_a")
    unexpected = diff("_havn_ut_a", "_havn_ut_e")
    return missing, unexpected
