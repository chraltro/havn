"""Tests for the shared table-reference rewriter."""

from __future__ import annotations

import duckdb
import pytest

from havn.engine.sql_rewrite import (
    SQLRewriteError,
    find_table_refs,
    rewrite_table_refs,
)


def _norm(sql: str) -> str:
    return " ".join(sql.split()).lower()


def test_rewrites_qualified_reference():
    out = rewrite_table_refs(
        "SELECT * FROM bronze.orders", {"bronze.orders": "_mock_orders"}
    )
    assert "_mock_orders" in out
    assert "bronze.orders" not in _norm(out)


def test_preserves_alias():
    out = rewrite_table_refs(
        "SELECT o.id FROM bronze.orders o WHERE o.amount > 0",
        {"bronze.orders": "_mock_orders"},
    )
    assert "_mock_orders AS o" in out
    assert "o.id" in out


def test_preserves_alias_with_as_keyword():
    out = rewrite_table_refs(
        "SELECT x.id FROM bronze.orders AS x", {"bronze.orders": "m"}
    )
    assert "m AS x" in out


def test_case_insensitive_key_match():
    out = rewrite_table_refs(
        "SELECT * FROM Bronze.Orders", {"bronze.orders": "_mock"}
    )
    assert "_mock" in out


def test_unqualified_reference():
    out = rewrite_table_refs("SELECT * FROM orders", {"orders": "_mock"})
    assert "_mock" in out


def test_cte_names_are_not_rewritten():
    sql = """
    WITH orders AS (SELECT 1 AS id)
    SELECT * FROM orders
    """
    out = rewrite_table_refs(sql, {"orders": "_mock"})
    assert "_mock" not in out


def test_cte_body_reference_is_rewritten():
    sql = """
    WITH recent AS (SELECT * FROM bronze.orders WHERE amount > 0)
    SELECT * FROM recent
    """
    out = rewrite_table_refs(sql, {"bronze.orders": "_mock_orders"})
    assert "_mock_orders" in out
    # The CTE reference itself stays intact.
    assert "FROM recent" in out


def test_join_and_multiple_mappings():
    sql = """
    SELECT c.customer_id, count(o.order_id) AS n
    FROM bronze.customers c
    LEFT JOIN bronze.orders o ON c.customer_id = o.customer_id
    GROUP BY 1
    """
    out = rewrite_table_refs(
        sql, {"bronze.customers": "_mc", "bronze.orders": "_mo"}
    )
    assert "_mc AS c" in out
    assert "_mo AS o" in out


def test_unmapped_reference_untouched():
    out = rewrite_table_refs(
        "SELECT * FROM bronze.orders JOIN silver.x ON true",
        {"bronze.orders": "_mock"},
    )
    assert "silver.x" in _norm(out)


def test_empty_mapping_returns_input_verbatim():
    sql = "SELECT * FROM bronze.orders /* keep me */"
    assert rewrite_table_refs(sql, {}) == sql


def test_unparseable_sql_raises():
    with pytest.raises(SQLRewriteError) as exc:
        rewrite_table_refs("SELECT FROM FROM WHERE )(", {"a.b": "c"})
    assert "parse" in str(exc.value).lower()


def test_three_part_target_is_generated_verbatim():
    out = rewrite_table_refs(
        "SELECT c.id FROM bronze.customers c",
        {"bronze.customers": "havn_defer.bronze.customers"},
    )
    assert _norm(out) == "select c.id from havn_defer.bronze.customers as c"


def test_skip_catalog_qualified_leaves_three_part_refs_alone():
    sql = "SELECT * FROM other.bronze.customers"
    mapping = {"bronze.customers": "havn_defer.bronze.customers"}
    assert "havn_defer" in rewrite_table_refs(sql, mapping)
    out = rewrite_table_refs(sql, mapping, skip_catalog_qualified=True)
    assert _norm(out) == "select * from other.bronze.customers"


def test_find_table_refs_skips_table_functions():
    refs = find_table_refs(
        "SELECT * FROM read_csv('a.csv') UNION ALL SELECT * FROM bronze.x"
    )
    assert refs == ["bronze.x"]


def test_find_table_refs_skip_catalog_qualified():
    sql = "SELECT * FROM bronze.a JOIN other.bronze.b ON true"
    assert find_table_refs(sql) == ["bronze.a", "bronze.b"]
    assert find_table_refs(sql, skip_catalog_qualified=True) == ["bronze.a"]


def test_find_table_refs_excludes_ctes():
    sql = """
    WITH recent AS (SELECT * FROM bronze.orders)
    SELECT * FROM recent JOIN bronze.customers ON true
    """
    refs = find_table_refs(sql)
    assert "bronze.orders" in refs
    assert "bronze.customers" in refs
    assert "recent" not in refs


# --- DuckDB-specific constructs survive the round trip ------------------

DUCKDB_CONSTRUCTS = [
    ("exclude", "SELECT * EXCLUDE (b) FROM bronze.t"),
    ("replace", "SELECT * REPLACE (a * 2 AS a) FROM bronze.t"),
    ("lambda", "SELECT list_transform([1, 2, 3], x -> x + 1) AS v FROM bronze.t"),
    ("struct_dot", "SELECT s.field FROM (SELECT {'field': 1} AS s, a FROM bronze.t)"),
    ("qualify", "SELECT a, row_number() OVER (ORDER BY a) AS rn FROM bronze.t QUALIFY rn = 1"),
    ("union_by_name", "SELECT a FROM bronze.t UNION ALL BY NAME SELECT a FROM bronze.t"),
    ("group_by_all", "SELECT a, count(*) AS n FROM bronze.t GROUP BY ALL"),
    ("try_cast", "SELECT try_cast(a AS VARCHAR) AS a FROM bronze.t"),
    ("list_slice", "SELECT [1, 2, 3][1:2] AS v, a FROM bronze.t"),
    ("columns_regex", "SELECT COLUMNS('a.*') FROM bronze.t"),
]


@pytest.mark.parametrize("label,sql", DUCKDB_CONSTRUCTS, ids=[c[0] for c in DUCKDB_CONSTRUCTS])
def test_duckdb_constructs_rewrite_and_still_run(label, sql):
    conn = duckdb.connect(":memory:")
    try:
        conn.execute("CREATE TEMP TABLE _mock_t AS SELECT 1 AS a, 2 AS b")
        out = rewrite_table_refs(sql, {"bronze.t": "_mock_t"})
        assert "_mock_t" in out, f"{label}: reference not rewritten"
        assert "bronze.t" not in _norm(out), f"{label}: original reference left behind"
        # The rewritten SQL must still be valid DuckDB.
        conn.execute(out).fetchall()
    finally:
        conn.close()


def test_rewrite_to_schema_qualified_target():
    out = rewrite_table_refs(
        "SELECT * FROM bronze.orders o", {"bronze.orders": "prod.orders"}
    )
    assert "prod.orders AS o" in out
