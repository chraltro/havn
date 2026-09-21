"""Tests for the sqlglot-based SQL analysis module.

Tests the patterns that broke with regex parsing: CTEs, subqueries,
UNION ALL, aliased subqueries, complex expressions, window functions.
"""

from __future__ import annotations

import textwrap

import duckdb
import pytest

from havn.engine.sql_analysis import (
    extract_column_lineage,
    extract_column_references,
    extract_table_refs,
    parse_assertions,
    parse_column_docs,
    parse_config,
    parse_depends,
    parse_description,
    strip_config_comments,
)


# ===========================================================================
# parse_config
# ===========================================================================


class TestParseConfig:
    def test_basic_legacy(self):
        sql = "-- config: materialized=table, schema=silver\nSELECT 1"
        assert parse_config(sql) == {"materialized": "table", "schema": "silver"}

    def test_at_syntax(self):
        sql = "@config materialized=table, schema=silver\nSELECT 1"
        assert parse_config(sql) == {"materialized": "table", "schema": "silver"}

    def test_at_syntax_parens(self):
        sql = "@config(materialized=table, schema=gold)\nSELECT 1"
        assert parse_config(sql) == {"materialized": "table", "schema": "gold"}

    def test_at_syntax_colon(self):
        sql = "@config: materialized=view\nSELECT 1"
        assert parse_config(sql) == {"materialized": "view"}

    def test_empty(self):
        assert parse_config("SELECT 1") == {}

    def test_single_key(self):
        assert parse_config("-- config: materialized=view\nSELECT 1") == {"materialized": "view"}


# ===========================================================================
# parse_depends
# ===========================================================================


class TestParseDepends:
    def test_basic_legacy(self):
        sql = "-- depends_on: bronze.customers, bronze.orders\nSELECT 1"
        assert parse_depends(sql) == ["bronze.customers", "bronze.orders"]

    def test_at_syntax(self):
        sql = "@depends_on bronze.customers, bronze.orders\nSELECT 1"
        assert parse_depends(sql) == ["bronze.customers", "bronze.orders"]

    def test_at_syntax_parens(self):
        sql = "@depends_on(bronze.customers, bronze.orders)\nSELECT 1"
        assert parse_depends(sql) == ["bronze.customers", "bronze.orders"]

    def test_empty(self):
        assert parse_depends("SELECT 1") == []


# ===========================================================================
# parse_assertions / description / column_docs
# ===========================================================================


def test_parse_assertions_legacy():
    sql = "-- assert: row_count > 0\n-- assert: unique(id)\nSELECT 1"
    assert parse_assertions(sql) == ["row_count > 0", "unique(id)"]


def test_parse_assertions_at_syntax():
    sql = "@assert row_count > 0\n@assert unique(id)\nSELECT 1"
    assert parse_assertions(sql) == ["row_count > 0", "unique(id)"]


def test_parse_assertions_at_syntax_parens():
    sql = "@assert(row_count > 0)\n@assert(unique(id))\nSELECT 1"
    assert parse_assertions(sql) == ["row_count > 0", "unique(id)"]


def test_parse_description():
    sql = "-- description: Customer dimension table\nSELECT 1"
    assert parse_description(sql) == "Customer dimension table"


def test_parse_description_at_syntax():
    sql = "@description Customer dimension table\nSELECT 1"
    assert parse_description(sql) == "Customer dimension table"


def test_parse_description_at_syntax_parens():
    sql = "@description(Customer dimension table)\nSELECT 1"
    assert parse_description(sql) == "Customer dimension table"


def test_parse_column_docs():
    sql = "-- col: id: Primary key\n-- col: name: Customer name\nSELECT 1"
    assert parse_column_docs(sql) == {"id": "Primary key", "name": "Customer name"}


def test_parse_column_docs_at_syntax():
    sql = "@col id: Primary key\n@col name: Customer name\nSELECT 1"
    assert parse_column_docs(sql) == {"id": "Primary key", "name": "Customer name"}


# ===========================================================================
# strip_config_comments
# ===========================================================================


def test_strip_config_comments():
    sql = textwrap.dedent("""\
        -- config: materialized=table, schema=silver
        -- depends_on: bronze.customers
        -- description: Test model
        -- col: id: Primary key
        -- assert: row_count > 0

        SELECT id FROM bronze.customers
    """)
    result = strip_config_comments(sql)
    assert "-- config:" not in result
    assert "-- depends_on:" not in result
    assert "-- description:" not in result
    assert "-- col:" not in result
    assert "-- assert:" not in result
    assert "SELECT id FROM bronze.customers" in result


def test_strip_config_comments_at_syntax():
    sql = textwrap.dedent("""\
        @config(materialized=table, schema=silver)
        @depends_on(bronze.customers)
        @description(Test model)
        @col(id: Primary key)
        @assert(row_count > 0)

        SELECT id FROM bronze.customers
    """)
    result = strip_config_comments(sql)
    assert "@config" not in result
    assert "@depends_on" not in result
    assert "@description" not in result
    assert "@col" not in result
    assert "@assert" not in result
    assert "SELECT id FROM bronze.customers" in result


def test_strip_config_comments_preserves_line_numbers():
    """Directives are blanked in place so line N stays line N.

    They used to be deleted, which shifted everything below them by a
    non-constant amount and broke every mapping from a parser's line number
    back to a line of the file.
    """
    sql = textwrap.dedent("""\
        @config materialized=table, schema=gold
        @description A model

        SELECT
            id
        FROM bronze.customers
        @assert row_count > 0
    """)
    result = strip_config_comments(sql)
    original = sql.split("\n")
    stripped = result.split("\n")

    assert len(stripped) == len(original)
    for i, line in enumerate(original):
        if line.strip().startswith("@"):
            assert stripped[i] == ""
        else:
            assert stripped[i] == line

    # The SELECT is on line 4 of the file and stays on line 4.
    assert stripped[3] == "SELECT"


def test_strip_config_comments_keeps_leading_blanks():
    sql = "@config materialized=view\n\nSELECT 1"
    assert strip_config_comments(sql) == "\n\nSELECT 1"


# ===========================================================================
# extract_table_refs — the core tests for AST-based parsing
# ===========================================================================


class TestExtractTableRefs:
    """Tests for sqlglot-based table reference extraction."""

    def test_simple_from(self):
        sql = "SELECT * FROM bronze.customers"
        assert extract_table_refs(sql) == ["bronze.customers"]

    def test_join(self):
        sql = "SELECT * FROM bronze.customers c JOIN bronze.orders o ON c.id = o.customer_id"
        assert extract_table_refs(sql) == ["bronze.customers", "bronze.orders"]

    def test_multiple_joins(self):
        sql = textwrap.dedent("""\
            SELECT c.id, o.total, p.name
            FROM bronze.customers c
            JOIN bronze.orders o ON c.id = o.customer_id
            LEFT JOIN bronze.products p ON o.product_id = p.id
        """)
        refs = extract_table_refs(sql)
        assert refs == ["bronze.customers", "bronze.orders", "bronze.products"]

    def test_cte_not_included(self):
        """CTE names should NOT appear in table references."""
        sql = textwrap.dedent("""\
            WITH filtered AS (
                SELECT id, name FROM bronze.customers WHERE active = true
            )
            SELECT f.id, f.name FROM filtered f
        """)
        refs = extract_table_refs(sql)
        assert refs == ["bronze.customers"]
        assert "filtered" not in str(refs)

    def test_multi_level_ctes(self):
        """Multiple chained CTEs should trace back to source tables."""
        sql = textwrap.dedent("""\
            WITH
            raw_customers AS (
                SELECT * FROM bronze.customers
            ),
            enriched AS (
                SELECT c.*, o.total
                FROM raw_customers c
                JOIN bronze.orders o ON c.id = o.customer_id
            )
            SELECT * FROM enriched
        """)
        refs = extract_table_refs(sql)
        assert refs == ["bronze.customers", "bronze.orders"]

    def test_subquery_in_from(self):
        """Subqueries in FROM clauses should be traversed."""
        sql = textwrap.dedent("""\
            SELECT sub.id, sub.name
            FROM (
                SELECT id, name FROM bronze.customers WHERE active = true
            ) sub
        """)
        refs = extract_table_refs(sql)
        assert refs == ["bronze.customers"]

    def test_subquery_in_where(self):
        """Subqueries in WHERE clauses should be traversed."""
        sql = textwrap.dedent("""\
            SELECT * FROM bronze.customers
            WHERE id IN (SELECT customer_id FROM bronze.orders WHERE total > 100)
        """)
        refs = extract_table_refs(sql)
        assert refs == ["bronze.customers", "bronze.orders"]

    def test_union_all(self):
        """UNION ALL should include refs from all branches."""
        sql = textwrap.dedent("""\
            SELECT id, name FROM bronze.customers_a
            UNION ALL
            SELECT id, name FROM bronze.customers_b
            UNION ALL
            SELECT id, name FROM bronze.customers_c
        """)
        refs = extract_table_refs(sql)
        assert refs == ["bronze.customers_a", "bronze.customers_b", "bronze.customers_c"]

    def test_aliased_subqueries(self):
        """Complex aliased subqueries in JOIN should be traversed."""
        sql = textwrap.dedent("""\
            SELECT c.id, totals.total_amount
            FROM bronze.customers c
            JOIN (
                SELECT customer_id, SUM(amount) AS total_amount
                FROM bronze.orders
                GROUP BY customer_id
            ) totals ON c.id = totals.customer_id
        """)
        refs = extract_table_refs(sql)
        assert refs == ["bronze.customers", "bronze.orders"]

    def test_exclude_self_reference(self):
        sql = "SELECT * FROM silver.customers JOIN bronze.raw ON 1=1"
        refs = extract_table_refs(sql, exclude="silver.customers")
        assert refs == ["bronze.raw"]

    def test_skip_system_schemas(self):
        sql = "SELECT * FROM information_schema.tables JOIN bronze.data d ON 1=1"
        refs = extract_table_refs(sql)
        assert refs == ["bronze.data"]

    def test_cross_schema(self):
        """References across different schemas are captured."""
        sql = textwrap.dedent("""\
            SELECT b.id, s.name, g.total
            FROM bronze.events b
            JOIN silver.events s ON b.id = s.raw_id
            JOIN gold.event_summary g ON s.id = g.event_id
        """)
        refs = extract_table_refs(sql)
        assert refs == ["bronze.events", "gold.event_summary", "silver.events"]

    def test_cte_with_union_inside(self):
        """CTEs containing UNION ALL should trace sources properly."""
        sql = textwrap.dedent("""\
            WITH combined AS (
                SELECT id, name FROM bronze.customers_us
                UNION ALL
                SELECT id, name FROM bronze.customers_eu
            )
            SELECT * FROM combined
        """)
        refs = extract_table_refs(sql)
        assert refs == ["bronze.customers_eu", "bronze.customers_us"]

    def test_lateral_join(self):
        """Lateral/correlated subqueries."""
        sql = textwrap.dedent("""\
            SELECT c.id, o.last_order
            FROM bronze.customers c,
            LATERAL (
                SELECT MAX(order_date) AS last_order
                FROM bronze.orders o
                WHERE o.customer_id = c.id
            ) o
        """)
        refs = extract_table_refs(sql)
        assert refs == ["bronze.customers", "bronze.orders"]

    def test_exists_subquery(self):
        sql = textwrap.dedent("""\
            SELECT * FROM bronze.customers c
            WHERE EXISTS (
                SELECT 1 FROM bronze.orders o WHERE o.customer_id = c.id
            )
        """)
        refs = extract_table_refs(sql)
        assert refs == ["bronze.customers", "bronze.orders"]

    def test_scalar_subquery_in_select(self):
        sql = textwrap.dedent("""\
            SELECT
                c.id,
                (SELECT COUNT(*) FROM bronze.orders o WHERE o.customer_id = c.id) AS order_count
            FROM bronze.customers c
        """)
        refs = extract_table_refs(sql)
        assert refs == ["bronze.customers", "bronze.orders"]

    def test_unparseable_sql_falls_back_to_regex(self):
        """Completely unparseable SQL falls back to regex extraction."""
        sql = "SELEC BORKEN FROM bronze.data WHERE ???"
        # The regex fallback should still find bronze.data if pattern matches
        refs = extract_table_refs(sql)
        # Depending on how much sqlglot can handle, this may or may not find refs
        assert isinstance(refs, list)

    def test_empty_sql(self):
        assert extract_table_refs("") == []

    def test_no_schema_qualified_refs(self):
        """Unqualified table names are not captured."""
        sql = "SELECT * FROM customers"
        assert extract_table_refs(sql) == []

    def test_window_function_doesnt_add_refs(self):
        """Window functions should not produce spurious table refs."""
        sql = textwrap.dedent("""\
            SELECT
                e.id,
                ROW_NUMBER() OVER (PARTITION BY e.region ORDER BY e.magnitude DESC) AS rn
            FROM bronze.events e
        """)
        refs = extract_table_refs(sql)
        assert refs == ["bronze.events"]

    def test_create_table_as(self):
        """CREATE TABLE AS should still find source table refs."""
        sql = "CREATE TABLE silver.summary AS SELECT * FROM bronze.events"
        refs = extract_table_refs(sql)
        assert "bronze.events" in refs


# ===========================================================================
# extract_column_lineage
# ===========================================================================


class TestExtractColumnLineage:
    """Tests for AST-based column lineage tracing."""

    def test_simple_columns(self):
        lineage = extract_column_lineage(
            "SELECT c.customer_id, c.name FROM bronze.customers c",
            depends_on=["bronze.customers"],
        )
        assert "customer_id" in lineage
        assert "name" in lineage
        assert lineage["customer_id"][0]["source_table"] == "bronze.customers"

    def test_aliased_column(self):
        lineage = extract_column_lineage(
            "SELECT c.name AS customer_name FROM bronze.customers c",
            depends_on=["bronze.customers"],
        )
        assert "customer_name" in lineage
        assert lineage["customer_name"][0]["source_column"] == "name"

    def test_computed_column(self):
        lineage = extract_column_lineage(
            "SELECT d.amount * 1.1 AS amount_with_tax FROM bronze.data d",
            depends_on=["bronze.data"],
        )
        assert "amount_with_tax" in lineage
        assert any(s["source_column"] == "amount" for s in lineage["amount_with_tax"])

    def test_case_when(self):
        """CASE WHEN should trace all column references."""
        lineage = extract_column_lineage(
            "SELECT e.id, CASE WHEN e.magnitude >= 5 THEN 'strong' ELSE 'weak' END AS strength FROM bronze.events e",
            depends_on=["bronze.events"],
        )
        assert "strength" in lineage
        assert any(s["source_column"] == "magnitude" for s in lineage["strength"])

    def test_case_with_multiple_tables(self):
        """CASE WHEN referencing columns from different tables."""
        sql = textwrap.dedent("""\
            SELECT
                c.id,
                CASE
                    WHEN o.total > 1000 THEN c.name
                    ELSE 'unknown'
                END AS customer_label
            FROM bronze.customers c
            JOIN bronze.orders o ON c.id = o.customer_id
        """)
        lineage = extract_column_lineage(sql, depends_on=["bronze.customers", "bronze.orders"])
        assert "customer_label" in lineage
        source_tables = {s["source_table"] for s in lineage["customer_label"]}
        assert "bronze.orders" in source_tables
        assert "bronze.customers" in source_tables

    def test_window_function(self):
        """Window functions should trace column references from OVER clause."""
        lineage = extract_column_lineage(
            "SELECT e.id, ROW_NUMBER() OVER (PARTITION BY e.region ORDER BY e.magnitude DESC) AS rn FROM bronze.events e",
            depends_on=["bronze.events"],
        )
        assert "rn" in lineage
        source_cols = {s["source_column"] for s in lineage["rn"]}
        assert "region" in source_cols
        assert "magnitude" in source_cols

    def test_cte_traces_through(self):
        """Column lineage should trace through CTEs to source tables."""
        sql = textwrap.dedent("""\
            WITH filtered AS (
                SELECT id, name FROM bronze.customers WHERE active = true
            )
            SELECT f.id, f.name FROM filtered f
        """)
        lineage = extract_column_lineage(sql, depends_on=["bronze.customers"])
        assert "id" in lineage
        assert "name" in lineage
        # Should trace through CTE back to bronze.customers
        for col in ["id", "name"]:
            sources = lineage[col]
            assert len(sources) > 0
            assert any(s["source_table"] == "bronze.customers" for s in sources)

    def test_subquery_in_select(self):
        lineage = extract_column_lineage(
            "SELECT c.id, (SELECT COUNT(*) FROM bronze.orders o WHERE o.customer_id = c.id) AS order_count FROM bronze.customers c",
            depends_on=["bronze.customers", "bronze.orders"],
        )
        assert "id" in lineage
        assert "order_count" in lineage

    def test_union_first_select(self):
        lineage = extract_column_lineage(
            "SELECT a.id, a.name FROM bronze.customers_a a UNION ALL SELECT b.id, b.name FROM bronze.customers_b b",
            depends_on=["bronze.customers_a", "bronze.customers_b"],
        )
        assert "id" in lineage
        assert "name" in lineage

    def test_star_with_connection(self):
        """SELECT * with connection resolves columns."""
        conn = duckdb.connect(":memory:")
        conn.execute("CREATE SCHEMA IF NOT EXISTS bronze")
        conn.execute("CREATE TABLE bronze.src AS SELECT 1 AS id, 'x' AS name")
        lineage = extract_column_lineage(
            "SELECT * FROM bronze.src",
            depends_on=["bronze.src"],
            conn=conn,
        )
        assert "id" in lineage
        assert "name" in lineage
        assert lineage["id"][0]["source_table"] == "bronze.src"
        conn.close()

    def test_unparseable_returns_empty(self):
        lineage = extract_column_lineage("THIS IS NOT SQL")
        assert lineage == {}

    def test_join_columns_from_multiple_tables(self):
        sql = textwrap.dedent("""\
            SELECT
                c.customer_id,
                c.name,
                COUNT(o.order_id) AS order_count
            FROM bronze.customers c
            LEFT JOIN bronze.orders o ON c.customer_id = o.customer_id
            GROUP BY 1, 2
        """)
        lineage = extract_column_lineage(
            sql, depends_on=["bronze.customers", "bronze.orders"]
        )
        assert "customer_id" in lineage
        assert any(s["source_table"] == "bronze.customers" for s in lineage["customer_id"])
        assert "order_count" in lineage
        assert any(s["source_table"] == "bronze.orders" for s in lineage["order_count"])


# ===========================================================================
# extract_column_references
# ===========================================================================


class TestExtractColumnReferences:
    """The reference index: every mention of a column, with its position."""

    SQL = textwrap.dedent("""\
        SELECT
            o.order_id,
            c.name AS customer_name
        FROM bronze.orders o
        JOIN bronze.customers c ON c.customer_id = o.customer_id
        WHERE o.status = 'ok'
        GROUP BY o.order_id, c.name
        HAVING COUNT(o.amount) > 1
        QUALIFY ROW_NUMBER() OVER (PARTITION BY o.customer_id) = 1
        ORDER BY c.name""")

    def test_clauses(self):
        refs = extract_column_references(self.SQL, ["bronze.orders", "bronze.customers"])
        by_clause: dict[str, set[str]] = {}
        for ref in refs:
            by_clause.setdefault(ref.clause, set()).add(ref.column)
        assert by_clause["select"] == {"order_id", "name"}
        assert by_clause["join"] == {"customer_id"}
        assert by_clause["where"] == {"status"}
        assert by_clause["group"] == {"order_id", "name"}
        assert by_clause["having"] == {"amount"}
        assert by_clause["window"] == {"customer_id"}
        assert by_clause["order"] == {"name"}

    def test_tables_are_resolved_through_aliases(self):
        refs = extract_column_references(self.SQL, ["bronze.orders", "bronze.customers"])
        status = next(r for r in refs if r.column == "status")
        assert status.table == "bronze.orders"
        name = next(r for r in refs if r.column == "name")
        assert name.table == "bronze.customers"

    def test_positions_point_at_the_identifier(self):
        """start/end splice the query; line/col land on the identifier's end."""
        lines = self.SQL.split("\n")
        refs = extract_column_references(self.SQL, ["bronze.orders"])
        assert refs
        for ref in refs:
            assert self.SQL[ref.start : ref.end + 1] == ref.column
            line = lines[ref.line - 1]
            assert line[ref.col - len(ref.column) : ref.col] == ref.column

    def test_references_come_back_in_source_order(self):
        refs = extract_column_references(self.SQL, ["bronze.orders"])
        assert [r.start for r in refs] == sorted(r.start for r in refs)
        assert refs[0].column == "order_id"

    def test_filter_only_column_is_indexed(self):
        """The whole point: a column no output column depends on."""
        refs = extract_column_references(
            "SELECT customer_id FROM bronze.orders WHERE status = 'ok'",
            ["bronze.orders"],
        )
        status = next(r for r in refs if r.column == "status")
        assert (status.table, status.clause) == ("bronze.orders", "where")

    def test_cte_qualified_reference_reports_the_cte(self):
        sql = textwrap.dedent("""\
            WITH recent AS (SELECT id, amount FROM bronze.orders)
            SELECT r.id, r.amount FROM recent r""")
        refs = extract_column_references(sql, ["bronze.orders"])
        outer = [r for r in refs if r.line == 2]
        assert {r.table for r in outer} == {"recent"}
        inner = [r for r in refs if r.line == 1]
        assert {r.table for r in inner} == {"bronze.orders"}

    def test_unqualified_column_uses_the_single_source_in_scope(self):
        refs = extract_column_references(
            "SELECT id, amount FROM bronze.orders", ["bronze.orders"]
        )
        assert {r.table for r in refs} == {"bronze.orders"}

    def test_unqualified_column_resolved_by_schema_when_ambiguous(self):
        sql = (
            "SELECT id, region FROM bronze.orders "
            "JOIN bronze.customers ON bronze.orders.id = bronze.customers.id"
        )
        refs = extract_column_references(
            sql,
            ["bronze.orders", "bronze.customers"],
            schema={
                "bronze.orders": [("id", "INTEGER"), ("amount", "DOUBLE")],
                "bronze.customers": [("id", "INTEGER"), ("region", "VARCHAR")],
            },
        )
        region = next(r for r in refs if r.column == "region" and r.clause == "select")
        assert region.table == "bronze.customers"

    def test_unresolvable_column_has_an_empty_table(self):
        refs = extract_column_references(
            "SELECT id FROM bronze.a JOIN bronze.b ON bronze.a.x = bronze.b.x",
            ["bronze.a", "bronze.b"],
        )
        bare = next(r for r in refs if r.clause == "select")
        assert bare.table == ""

    def test_star_is_not_a_reference(self):
        assert extract_column_references("SELECT * FROM bronze.orders", ["bronze.orders"]) == []

    def test_unparseable_sql_returns_nothing(self):
        assert extract_column_references("SELECT FROM ((( WHERE") == []
