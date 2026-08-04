"""Regression tests for the audit-pass fixes.

Each test pins one defect that was found and fixed, so a later refactor can't
silently reintroduce it. Grouped by the module the fix landed in.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from havn.engine.sql_analysis import extract_table_refs, parse_config
from havn.engine.sql_safety import ReadOnlyQueryError, validate_read_only_query
from havn.engine.transform.discovery import (
    CircularDependencyError,
    build_dag,
    build_dag_tiers,
)
from havn.engine.transform.execution import _execute_incremental
from havn.engine.transform.models import SQLModel
from havn.engine.transform.quality import _evaluate_assertion


def _model(**kw) -> SQLModel:
    base = dict(
        path=Path("transform/silver/m.sql"),
        name="m",
        schema="silver",
        full_name="silver.m",
        sql="",
        query="SELECT 1 AS x",
        materialized="table",
    )
    base.update(kw)
    return SQLModel(**base)


# ---------------------------------------------------------------------------
# Dependency extraction (engine/sql_analysis.py)
# ---------------------------------------------------------------------------


def test_cte_name_does_not_shadow_qualified_dependency():
    """A CTE sharing a model's bare name must not drop the real dependency.

    `WITH orders AS (...) ... JOIN bronze.orders` still depends on
    bronze.orders; dropping it put the model in the wrong DAG position and left
    it un-rebuilt when bronze.orders changed.
    """
    sql = (
        "WITH orders AS (SELECT 1 AS x) "
        "SELECT o.x, b.id FROM orders o CROSS JOIN bronze.orders b"
    )
    assert extract_table_refs(sql) == ["bronze.orders"]


def test_cte_name_does_not_shadow_schema_name():
    sql = (
        "WITH bronze AS (SELECT 1 AS x) "
        "SELECT * FROM bronze b CROSS JOIN bronze.orders c"
    )
    assert extract_table_refs(sql) == ["bronze.orders"]


def test_unqualified_cte_reference_is_still_skipped():
    sql = "WITH t AS (SELECT * FROM bronze.a) SELECT * FROM t"
    assert extract_table_refs(sql) == ["bronze.a"]


# ---------------------------------------------------------------------------
# @config parsing (engine/sql_analysis.py)
# ---------------------------------------------------------------------------


def test_composite_unique_key_survives_config_parsing():
    """Splitting on every comma truncated composite keys to the first column.

    A delete+insert keyed on (cust, day) then deleted a customer's whole
    history whenever a new day arrived.
    """
    cfg = parse_config(
        "@config materialized=incremental, schema=silver, unique_key=cust, day"
    )
    assert cfg["unique_key"] == "cust, day"
    assert cfg["materialized"] == "incremental"
    assert cfg["schema"] == "silver"


def test_incremental_filter_keeps_commas():
    cfg = parse_config(
        "@config materialized=incremental, incremental_filter=WHERE x IN (1,2)"
    )
    assert cfg["incremental_filter"] == "WHERE x IN (1,2)"


@pytest.mark.parametrize(
    "header,expected",
    [
        ("@config materialized=table, schema=silver", {"materialized": "table", "schema": "silver"}),
        ("@config(materialized=table, schema=gold)", {"materialized": "table", "schema": "gold"}),
        ("-- config: materialized=view, schema=bronze", {"materialized": "view", "schema": "bronze"}),
    ],
)
def test_config_syntaxes_still_parse(header, expected):
    assert parse_config(header) == expected


# ---------------------------------------------------------------------------
# Change detection (engine/transform/models.py)
# ---------------------------------------------------------------------------


def test_adding_an_assertion_changes_the_content_hash():
    """@assert lines are stripped from `query`, so they have to be hashed here.

    Otherwise adding an assertion to an already-built model left the hash
    unchanged: the model was skipped and the assertion never ran.
    """
    plain = _model()
    with_assert = _model(assertions=["row_count > 1000"])
    assert plain.content_hash != with_assert.content_hash


def test_adding_a_grain_changes_the_content_hash():
    assert _model().content_hash != _model(grain=["x"]).content_hash


def test_assertion_severity_change_changes_the_content_hash():
    a = _model(assertion_specs=[("row_count > 0", "error")])
    b = _model(assertion_specs=[("row_count > 0", "warn")])
    assert a.content_hash != b.content_hash


def test_content_hash_stable_for_plain_model():
    """Models with no extra settings must keep their pre-existing hash so an
    upgrade doesn't rebuild every model in every project."""
    assert _model().content_hash == _model().content_hash


# ---------------------------------------------------------------------------
# Assertions (engine/transform/quality.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    c = duckdb.connect()
    c.execute("CREATE SCHEMA silver")
    yield c
    c.close()


def test_row_level_assertion_checks_every_row(conn):
    """The old implementation evaluated the expression against one arbitrary
    row (`... FROM t LIMIT 1`) and reported "pass" for violating data."""
    conn.execute("CREATE TABLE silver.m AS SELECT * FROM (VALUES (5), (-7)) v(amt)")
    result = _evaluate_assertion(conn, _model(), "amt > 0")
    assert result.passed is False
    assert "1 of 2" in result.detail


def test_row_level_assertion_passes_when_all_rows_hold(conn):
    conn.execute("CREATE TABLE silver.m AS SELECT * FROM (VALUES (5), (7)) v(amt)")
    assert _evaluate_assertion(conn, _model(), "amt > 0").passed is True


def test_aggregate_assertion_still_works(conn):
    """Aggregates aren't valid in a WHERE clause, so they take the fallback."""
    conn.execute("CREATE TABLE silver.m AS SELECT * FROM (VALUES (5), (-7)) v(amt)")
    assert _evaluate_assertion(conn, _model(), "sum(amt) > 0").passed is False
    assert _evaluate_assertion(conn, _model(), "sum(amt) > -100").passed is True


def test_compound_assertion_is_not_truncated_to_its_first_conjunct(conn):
    """`re.match` was unanchored, so `row_count > 0 AND ...` matched the
    row_count builtin and the second conjunct was silently discarded."""
    conn.execute("CREATE TABLE silver.m AS SELECT * FROM (VALUES (1), (2)) v(amt)")
    result = _evaluate_assertion(conn, _model(), "row_count > 0 AND row_count > 99999")
    assert result.passed is False


def test_row_count_pseudo_column_works_inside_a_compound_expression(conn):
    conn.execute("CREATE TABLE silver.m AS SELECT * FROM (VALUES (1), (2)) v(amt)")
    assert _evaluate_assertion(conn, _model(), "row_count = 2 AND amt > 0").passed is True
    assert _evaluate_assertion(conn, _model(), "row_count = 3 AND amt > 0").passed is False


def test_builtin_assertions_still_work(conn):
    conn.execute(
        "CREATE TABLE silver.m AS SELECT * FROM (VALUES (1), (1), (NULL)) v(amt)"
    )
    assert _evaluate_assertion(conn, _model(), "row_count > 0").passed is True
    assert _evaluate_assertion(conn, _model(), "no_nulls(amt)").passed is False
    assert _evaluate_assertion(conn, _model(), "unique(amt)").passed is False


def test_accepted_values_escapes_quotes(conn):
    """A value containing a single quote used to break out of the SQL literal
    and produce a syntax error instead of a verdict."""
    conn.execute(
        "CREATE TABLE silver.m AS SELECT * FROM (VALUES ('O''Brien'), ('Smith')) v(amt)"
    )
    assert _evaluate_assertion(conn, _model(), "accepted_values(amt, [O'Brien, Smith])").passed
    bad = _evaluate_assertion(conn, _model(), "accepted_values(amt, [O'Brien])")
    assert bad.passed is False
    assert "Smith" in bad.detail


# ---------------------------------------------------------------------------
# Incremental execution (engine/transform/execution.py)
# ---------------------------------------------------------------------------


def _incremental(conn, query: str, unique_key: str, strategy: str, runs: int = 3):
    model = _model(
        query=query,
        materialized="incremental",
        unique_key=unique_key,
        incremental_strategy=strategy,
    )
    for _ in range(runs):
        _execute_incremental(conn, model)
    return conn.execute("SELECT * FROM silver.m ORDER BY 1 NULLS FIRST, 2").fetchall()


@pytest.mark.parametrize("strategy", ["delete+insert", "merge"])
def test_null_unique_key_does_not_duplicate_rows(conn, strategy):
    """`=` and `(k) IN (...)` are NULL rather than TRUE for NULL keys, so those
    rows were never deleted and duplicated on every run."""
    rows = _incremental(
        conn,
        "SELECT * FROM (VALUES (NULL, 'x'), (1, 'y')) v(k, d)",
        "k",
        strategy,
    )
    assert rows == [(None, "x"), (1, "y")]


@pytest.mark.parametrize("strategy", ["delete+insert", "merge"])
def test_composite_unique_key_keeps_distinct_rows(conn, strategy):
    rows = _incremental(
        conn,
        "SELECT * FROM (VALUES (1, '2024-01-01', 10), (1, '2024-01-02', 20)) v(cust, day, amt)",
        "cust, day",
        strategy,
    )
    assert rows == [(1, "2024-01-01", 10), (1, "2024-01-02", 20)]


def test_switching_a_view_to_incremental_does_not_wedge(conn):
    """information_schema.tables counts views, so the existence probe said
    "exists" and every run failed with "Can only delete from base table"."""
    conn.execute("CREATE VIEW silver.m AS SELECT 1 AS id, 2 AS val")
    model = _model(
        query="SELECT 1 AS id, 2 AS val",
        materialized="incremental",
        unique_key="id",
    )
    _execute_incremental(conn, model)
    _execute_incremental(conn, model)
    kind = conn.execute(
        "SELECT table_type FROM information_schema.tables "
        "WHERE table_schema = 'silver' AND table_name = 'm'"
    ).fetchone()[0]
    assert kind == "BASE TABLE"
    assert conn.execute("SELECT count(*) FROM silver.m").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# DAG cycles (engine/transform/discovery.py)
# ---------------------------------------------------------------------------


def _cyclic_pair() -> list[SQLModel]:
    a = _model(path=Path("transform/gold/a.sql"), name="a", schema="gold",
               full_name="gold.a", depends_on=["gold.b"])
    b = _model(path=Path("transform/gold/b.sql"), name="b", schema="gold",
               full_name="gold.b", depends_on=["gold.a"])
    return [a, b]


@pytest.mark.parametrize("builder", [build_dag, build_dag_tiers])
def test_dependency_cycle_raises_a_domain_error(builder):
    """graphlib's CycleError used to escape as a bare traceback through the
    CLI, the API, and the scheduler."""
    with pytest.raises(CircularDependencyError) as exc:
        builder(_cyclic_pair())
    message = str(exc.value)
    assert "gold.a" in message and "gold.b" in message
    assert "a.sql" in message


def test_acyclic_dag_still_orders_correctly():
    c = _model(path=Path("transform/gold/c.sql"), name="c", schema="gold",
               full_name="gold.c")
    d = _model(path=Path("transform/gold/d.sql"), name="d", schema="gold",
               full_name="gold.d", depends_on=["gold.c"])
    assert [m.full_name for m in build_dag([d, c])] == ["gold.c", "gold.d"]
    assert [[m.full_name for m in t] for t in build_dag_tiers([d, c])] == [
        ["gold.c"],
        ["gold.d"],
    ]


# ---------------------------------------------------------------------------
# Read-only SQL validation (engine/sql_safety.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        # Re-entrant SQL execution: the inner query lives in a string literal,
        # which the validator strips before any of its scans run.
        "SELECT * FROM json_execute_serialized_sql(json_serialize_sql('SELECT 42'))",
        # Filesystem enumeration and metadata readers.
        "SELECT * FROM glob('C:/Users/**')",
        "SELECT * FROM sniff_csv('/etc/passwd')",
        "SELECT * FROM parquet_schema('x.parquet')",
        # Foreign-database attach/scan paths.
        "SELECT * FROM read_duckdb('other.duckdb')",
        "SELECT * FROM postgres_query('db', 'SELECT 1')",
        # FORCE puts the real verb in second position.
        "FORCE INSTALL httpfs",
        "FORCE CHECKPOINT",
        # Transaction / catalog state changes.
        "BEGIN TRANSACTION",
        "USE other_db",
        # Bare data filename in table position is a replacement scan relative
        # to the server's working directory.
        'SELECT * FROM "warehouse.duckdb"',
        'SELECT * FROM "secret.env"',
        # Previously covered cases must stay blocked.
        "SELECT * FROM '/etc/passwd'",
        "SELECT * FROM read_csv_auto('x.csv')",
        "DROP TABLE t",
        "SELECT 1; DROP TABLE t",
    ],
)
def test_dangerous_sql_is_rejected(sql):
    with pytest.raises(ReadOnlyQueryError):
        validate_read_only_query(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM gold.orders",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        'SELECT * FROM "gold"."orders"',
        'SELECT * FROM "my table"',
        "DESCRIBE gold.orders",
        "SUMMARIZE gold.orders",
        "EXPLAIN SELECT 1",
        "SHOW TABLES",
        "SELECT 'read_csv is a nice function' AS note",
        "PIVOT gold.orders ON region USING sum(amt)",
        "SELECT * FROM (VALUES (1), (2)) t(x)",
        "TABLE gold.orders",
    ],
)
def test_legitimate_sql_is_allowed(sql):
    validate_read_only_query(sql)


# ---------------------------------------------------------------------------
# Secrets (engine/secrets.py)
# ---------------------------------------------------------------------------


def test_masked_secret_reveals_no_plaintext():
    """The old mask kept the first and last two characters, which is a
    meaningful chunk of a short or prefixed secret -- and GET /api/secrets
    returns it."""
    from havn.engine.secrets import _mask

    for value in ("hunter2", "sk-abc123", "1234", "a"):
        masked = _mask(value)
        for i in range(len(value) - 1):
            assert value[i : i + 2] not in masked
    assert _mask("") == "(empty)"
