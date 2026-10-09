"""Column-lineage conformance suite.

Every case here is checked twice:

1. The *output column set* is compared against DuckDB itself. The case SQL is
   materialized as a table and ``DESCRIBE`` on that table is the ground
   truth, so the expected column names are the ones a built havn model really
   has (including DuckDB's ``_1`` suffix for a duplicated name).
2. The *source mapping* per output column is compared against a hand-written
   expectation: ``{output_column: {"schema.table.column", ...}}``.

The point of the suite is that the hard SQL DuckDB accepts (stars with
EXCLUDE/REPLACE, PIVOT, UNPIVOT, ASOF, LATERAL, COLUMNS, recursive CTEs) is
pinned down, so a lineage change either keeps every construct working or says
out loud which one it broke.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from havn.engine.sql_analysis import extract_column_lineage, fetch_column_catalog
from havn.engine.transform import SQLModel, impact_analysis


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(scope="module")
def conn():
    """An in-memory warehouse with the tables every case reads."""
    c = duckdb.connect(":memory:")
    c.execute("CREATE SCHEMA bronze")
    c.execute(
        """CREATE TABLE bronze.customers (
            customer_id INTEGER, name VARCHAR, email VARCHAR,
            region VARCHAR, created_at TIMESTAMP
        )"""
    )
    c.execute(
        """CREATE TABLE bronze.orders (
            order_id INTEGER, customer_id INTEGER, amount DOUBLE,
            status VARCHAR, ordered_at TIMESTAMP
        )"""
    )
    c.execute(
        """CREATE TABLE bronze.events (
            event_id INTEGER, payload STRUCT(kind VARCHAR, score DOUBLE), tags VARCHAR[]
        )"""
    )
    c.execute("INSERT INTO bronze.customers VALUES (1, 'a', 'a@x', 'eu', now())")
    c.execute("INSERT INTO bronze.orders VALUES (1, 1, 10.0, 'ok', now())")
    c.execute("INSERT INTO bronze.events VALUES (1, {'kind': 'k', 'score': 1.0}, ['a', 'b'])")
    yield c
    c.close()


def describe_columns(conn, sql: str) -> list[str]:
    """The output columns DuckDB gives a model built from ``sql``.

    A materialized table rather than a bare ``DESCRIBE`` of the query:
    building is what havn does, and it is building that disambiguates a
    duplicated output name (``customer_id``, ``customer_id_1``). A dynamic
    ``PIVOT`` cannot be a view at all, so a table it is.
    """
    conn.execute("CREATE OR REPLACE TEMP TABLE _conformance_gt AS " + sql)
    return [r[0] for r in conn.execute("DESCRIBE _conformance_gt").fetchall()]


class AtLeast(frozenset):
    """An expectation for a construct lineage only approximates.

    The listed sources must be there; extra ones are tolerated because the
    construct is known to trace wider than it should. Everything else in the
    suite is asserted exactly.
    """


def flatten(lineage: dict[str, list[dict[str, str]]]) -> dict[str, set[str]]:
    """``{out_col: {"schema.table.column", ...}}`` for readable assertions."""
    return {
        col: {f"{s['source_table']}.{s['source_column']}" for s in sources}
        for col, sources in lineage.items()
    }


# --- Cases ------------------------------------------------------------------

# (case id, sql, depends_on, expected {out_col: {"table.column", ...}})
# An expectation of None for a column means "any sources, do not assert".
CASES: list[tuple[str, str, list[str], dict[str, set[str] | None]]] = [
    (
        "cte_chain",
        """
        WITH a AS (SELECT customer_id, name FROM bronze.customers),
             b AS (SELECT customer_id AS cid, upper(name) AS uname FROM a)
        SELECT cid, uname FROM b
        """,
        ["bronze.customers"],
        {
            "cid": {"bronze.customers.customer_id"},
            "uname": {"bronze.customers.name"},
        },
    ),
    (
        "nested_subquery_in_from",
        """
        SELECT t.customer_id, t.total
        FROM (SELECT customer_id, SUM(amount) AS total FROM bronze.orders GROUP BY 1) t
        """,
        ["bronze.orders"],
        {
            "customer_id": {"bronze.orders.customer_id"},
            "total": {"bronze.orders.amount"},
        },
    ),
    (
        "window_function",
        """
        SELECT order_id,
               ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY ordered_at DESC) AS rn,
               SUM(amount) OVER (PARTITION BY customer_id) AS cust_total
        FROM bronze.orders
        """,
        ["bronze.orders"],
        {
            "order_id": {"bronze.orders.order_id"},
            "rn": {"bronze.orders.customer_id", "bronze.orders.ordered_at"},
            "cust_total": {"bronze.orders.customer_id", "bronze.orders.amount"},
        },
    ),
    (
        "union_all_all_branches",
        """
        SELECT customer_id, name AS label FROM bronze.customers
        UNION ALL
        SELECT customer_id, status AS label FROM bronze.orders
        """,
        ["bronze.customers", "bronze.orders"],
        {
            "customer_id": {"bronze.customers.customer_id", "bronze.orders.customer_id"},
            "label": {"bronze.customers.name", "bronze.orders.status"},
        },
    ),
    (
        "union_by_name",
        """
        SELECT customer_id, name FROM bronze.customers
        UNION ALL BY NAME
        SELECT customer_id, status AS name FROM bronze.orders
        """,
        ["bronze.customers", "bronze.orders"],
        {
            "customer_id": {"bronze.customers.customer_id", "bronze.orders.customer_id"},
            "name": {"bronze.customers.name", "bronze.orders.status"},
        },
    ),
    (
        "star_over_join",
        """
        SELECT * FROM bronze.customers c
        JOIN bronze.orders o ON c.customer_id = o.customer_id
        """,
        ["bronze.customers", "bronze.orders"],
        {
            # Both customer_id sources survive; the name collision is resolved
            # the way DuckDB resolves it when the model is built.
            "customer_id": {"bronze.customers.customer_id"},
            "customer_id_1": {"bronze.orders.customer_id"},
            "name": {"bronze.customers.name"},
            "email": {"bronze.customers.email"},
            "region": {"bronze.customers.region"},
            "created_at": {"bronze.customers.created_at"},
            "order_id": {"bronze.orders.order_id"},
            "amount": {"bronze.orders.amount"},
            "status": {"bronze.orders.status"},
            "ordered_at": {"bronze.orders.ordered_at"},
        },
    ),
    (
        "star_exclude",
        "SELECT * EXCLUDE (email) FROM bronze.customers",
        ["bronze.customers"],
        {
            "customer_id": {"bronze.customers.customer_id"},
            "name": {"bronze.customers.name"},
            "region": {"bronze.customers.region"},
            "created_at": {"bronze.customers.created_at"},
        },
    ),
    (
        "star_replace",
        "SELECT * REPLACE (upper(email) AS name) FROM bronze.customers",
        ["bronze.customers"],
        {
            # The replacement expression, not the replaced column, is the source.
            "name": {"bronze.customers.email"},
            "customer_id": {"bronze.customers.customer_id"},
            "email": {"bronze.customers.email"},
            "region": {"bronze.customers.region"},
            "created_at": {"bronze.customers.created_at"},
        },
    ),
    (
        "struct_dot_access",
        "SELECT event_id, payload.kind AS kind FROM bronze.events",
        ["bronze.events"],
        {
            "event_id": {"bronze.events.event_id"},
            # The struct column is the source; the field name stays in the output.
            "kind": {"bronze.events.payload"},
        },
    ),
    (
        "struct_bracket_access",
        "SELECT event_id, payload['score'] AS score FROM bronze.events",
        ["bronze.events"],
        {
            "event_id": {"bronze.events.event_id"},
            "score": {"bronze.events.payload"},
        },
    ),
    (
        "unnest",
        "SELECT event_id, unnest(tags) AS tag FROM bronze.events",
        ["bronze.events"],
        {
            "event_id": {"bronze.events.event_id"},
            "tag": {"bronze.events.tags"},
        },
    ),
    (
        "correlated_subquery_in_select",
        """
        SELECT c.customer_id,
               (SELECT MAX(o.amount) FROM bronze.orders o
                WHERE o.customer_id = c.customer_id) AS max_amount
        FROM bronze.customers c
        """,
        ["bronze.customers", "bronze.orders"],
        {
            "customer_id": {"bronze.customers.customer_id"},
            "max_amount": {"bronze.orders.amount"},
        },
    ),
    (
        "qualify",
        """
        SELECT order_id, customer_id, amount
        FROM bronze.orders
        QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY amount DESC) = 1
        """,
        ["bronze.orders"],
        {
            "order_id": {"bronze.orders.order_id"},
            "customer_id": {"bronze.orders.customer_id"},
            "amount": {"bronze.orders.amount"},
        },
    ),
    (
        "pivot",
        "PIVOT bronze.orders ON status USING SUM(amount) GROUP BY customer_id",
        ["bronze.orders"],
        {
            "customer_id": {"bronze.orders.customer_id"},
            # A pivoted output column comes from the ON and USING columns.
            "ok": {"bronze.orders.status", "bronze.orders.amount"},
        },
    ),
    (
        "unpivot",
        "UNPIVOT bronze.orders ON amount, order_id INTO NAME metric VALUE val",
        ["bronze.orders"],
        {
            "customer_id": {"bronze.orders.customer_id"},
            "status": {"bronze.orders.status"},
            "ordered_at": {"bronze.orders.ordered_at"},
            "metric": {"bronze.orders.amount", "bronze.orders.order_id"},
            "val": {"bronze.orders.amount", "bronze.orders.order_id"},
        },
    ),
    (
        "asof_join",
        """
        SELECT c.customer_id, o.amount
        FROM bronze.customers c
        ASOF JOIN bronze.orders o ON c.customer_id >= o.customer_id
        """,
        ["bronze.customers", "bronze.orders"],
        {
            "customer_id": {"bronze.customers.customer_id"},
            "amount": {"bronze.orders.amount"},
        },
    ),
    (
        "lateral",
        """
        SELECT c.customer_id, t.amt
        FROM bronze.customers c,
        LATERAL (SELECT o.amount AS amt FROM bronze.orders o
                 WHERE o.customer_id = c.customer_id) t
        """,
        ["bronze.customers", "bronze.orders"],
        {
            "customer_id": {"bronze.customers.customer_id"},
            # sqlglot does not give a LATERAL body its own scope, so the
            # correlation predicate's columns come along with the projection.
            # Over-broad, never wrong: the real source is in there.
            "amt": AtLeast({"bronze.orders.amount"}),
        },
    ),
    (
        "columns_regex",
        "SELECT COLUMNS('c.*') FROM bronze.customers",
        ["bronze.customers"],
        {
            "customer_id": {"bronze.customers.customer_id"},
            "created_at": {"bronze.customers.created_at"},
        },
    ),
    (
        "group_by_all",
        "SELECT region, COUNT(*) AS n FROM bronze.customers GROUP BY ALL",
        ["bronze.customers"],
        {
            "region": {"bronze.customers.region"},
            # COUNT(*) has no column source.
            "n": set(),
        },
    ),
    (
        "recursive_cte",
        """
        WITH RECURSIVE r(n) AS (
            SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 5
        )
        SELECT n FROM r
        """,
        [],
        {"n": set()},
    ),
    (
        "filter_only_column",
        """
        SELECT customer_id
        FROM bronze.orders
        WHERE status = 'ok'
        GROUP BY customer_id
        """,
        ["bronze.orders"],
        {"customer_id": {"bronze.orders.customer_id"}},
    ),
]

CASES_BY_ID = {case[0]: case for case in CASES}


# --- The suite --------------------------------------------------------------


@pytest.mark.parametrize("case_id", [c[0] for c in CASES])
def test_output_columns_match_duckdb(conn, case_id):
    """The lineage keys are exactly the columns DuckDB gives the built model."""
    _, sql, depends_on, _ = CASES_BY_ID[case_id]
    sql = sql.strip()
    expected = [c.lower() for c in describe_columns(conn, sql)]
    lineage = extract_column_lineage(sql, depends_on, conn)
    assert list(lineage.keys()) == expected


@pytest.mark.parametrize("case_id", [c[0] for c in CASES])
def test_column_sources(conn, case_id):
    """Each output column maps to the source columns it really comes from."""
    _, sql, depends_on, expected = CASES_BY_ID[case_id]
    sql = sql.strip()
    got = flatten(extract_column_lineage(sql.strip(), depends_on, conn))
    for col, want in expected.items():
        assert col in got, f"{col} missing from {sorted(got)}"
        if want is None:
            continue
        if isinstance(want, AtLeast):
            assert want <= got[col], f"{col}: {sorted(got[col])} lacks {sorted(want)}"
        else:
            assert got[col] == want, f"{col}: {sorted(got[col])} != {sorted(want)}"


def test_filter_only_column_is_not_an_output_source(conn):
    """A column used only in WHERE must not show up as an output's source.

    ``status`` is the filter; it feeds no output column. The reference index
    (``extract_column_references``) is where a rename finds it instead.
    """
    _, sql, depends_on, _ = CASES_BY_ID["filter_only_column"]
    lineage = extract_column_lineage(sql.strip(), depends_on, conn)
    every_source = {s["source_column"] for sources in lineage.values() for s in sources}
    assert "status" not in every_source


def test_star_over_join_keeps_both_customer_id_sources(conn):
    """The duplicate-name collapse is gone: two columns in, two columns out."""
    _, sql, depends_on, _ = CASES_BY_ID["star_over_join"]
    lineage = extract_column_lineage(sql.strip(), depends_on, conn)
    sources = [
        (s["source_table"], s["source_column"])
        for col in lineage
        for s in lineage[col]
        if s["source_column"] == "customer_id"
    ]
    assert ("bronze.customers", "customer_id") in sources
    assert ("bronze.orders", "customer_id") in sources


def test_catalog_and_connection_agree(conn):
    """A pre-fetched catalog resolves stars exactly like a live connection."""
    catalog = fetch_column_catalog(conn)
    for case_id, sql, depends_on, _ in CASES:
        if case_id in ("pivot", "columns_regex"):
            # These need the database itself to enumerate their outputs.
            continue
        with_conn = extract_column_lineage(sql.strip(), depends_on, conn)
        with_catalog = extract_column_lineage(
            sql.strip(), depends_on, None, column_catalog=catalog
        )
        assert with_catalog == with_conn, case_id


# --- A chain of three models ------------------------------------------------


def _model(full_name: str, query: str, depends_on: list[str]) -> SQLModel:
    schema, _, name = full_name.partition(".")
    return SQLModel(
        path=Path(f"{name}.sql"),
        name=name,
        schema=schema,
        full_name=full_name,
        sql="",
        query=query,
        materialized="table",
        depends_on=depends_on,
    )


@pytest.fixture
def chain(tmp_path):
    """bronze.raw_orders -> silver.orders -> gold.order_totals, built."""
    conn = duckdb.connect(str(tmp_path / "chain.duckdb"))
    conn.execute("CREATE SCHEMA bronze")
    conn.execute("CREATE SCHEMA silver")
    conn.execute("CREATE SCHEMA gold")
    conn.execute(
        "CREATE TABLE bronze.raw_orders AS "
        "SELECT 1 AS order_id, 1 AS customer_id, 10.0 AS gross, 'ok' AS status"
    )
    conn.execute(
        "CREATE TABLE silver.orders AS "
        "SELECT order_id, customer_id, gross AS amount FROM bronze.raw_orders "
        "WHERE status = 'ok'"
    )
    conn.execute(
        "CREATE TABLE gold.order_totals AS "
        "SELECT customer_id, SUM(amount) AS total FROM silver.orders GROUP BY customer_id"
    )
    models = [
        _model(
            "silver.orders",
            "SELECT order_id, customer_id, gross AS amount FROM bronze.raw_orders "
            "WHERE status = 'ok'",
            ["bronze.raw_orders"],
        ),
        _model(
            "gold.order_totals",
            "SELECT customer_id, SUM(amount) AS total FROM silver.orders "
            "GROUP BY customer_id",
            ["silver.orders"],
        ),
    ]
    yield conn, models
    conn.close()


def test_three_model_chain(chain):
    """Lineage composes across a bronze -> silver -> gold chain."""
    conn, models = chain
    by_name = {m.full_name: m for m in models}

    silver = flatten(
        extract_column_lineage(
            by_name["silver.orders"].query, by_name["silver.orders"].depends_on, conn
        )
    )
    assert silver["amount"] == {"bronze.raw_orders.gross"}

    gold = flatten(
        extract_column_lineage(
            by_name["gold.order_totals"].query,
            by_name["gold.order_totals"].depends_on,
            conn,
        )
    )
    assert gold["total"] == {"silver.orders.amount"}
    assert gold["customer_id"] == {"silver.orders.customer_id"}


def test_three_model_chain_impact(chain):
    """Changing silver.orders.amount reaches gold.order_totals.total."""
    conn, models = chain
    result = impact_analysis(models, "silver.orders", column="amount", conn=conn)
    assert "gold.order_totals" in result["downstream_models"]
    hits = {(a["model"], a["column"]) for a in result["affected_columns"]}
    assert ("gold.order_totals", "total") in hits
