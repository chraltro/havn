"""Catalog probes describe this warehouse, not every attached one.

``information_schema`` spans every ATTACHed database. A deferred run has the
defer target attached, so any probe that filters only on ``table_schema`` and
``table_name`` sees the target's copy of a same-named object too: columns come
back doubled, or a column that exists only over there is profiled, asserted on
and reported against a model that built here perfectly well.

Every test here attaches a second warehouse holding ``silver.orders`` with two
extra columns and checks that the probe under test still reports the local
three.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from havn.engine.transform.models import SQLModel

LOCAL_COLUMNS = ["id", "amount", "region"]


@pytest.fixture
def two_warehouses(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    """A local warehouse with ``other`` attached, both holding silver.orders."""
    other = tmp_path / "other.duckdb"
    conn = duckdb.connect(str(other))
    conn.execute("CREATE SCHEMA silver")
    conn.execute(
        "CREATE TABLE silver.orders AS SELECT 1 AS id, "
        "CAST(10.0 AS DOUBLE) AS amount, 'north' AS region, "
        "'only-there' AS ghost, 99 AS extra"
    )
    conn.execute("CREATE TABLE silver.only_there AS SELECT 1 AS id")
    conn.close()

    local = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    local.execute("CREATE SCHEMA silver")
    local.execute(
        "CREATE TABLE silver.orders AS SELECT 1 AS id, "
        "CAST(10.0 AS DOUBLE) AS amount, 'north' AS region "
        "UNION ALL SELECT 2, CAST(20.0 AS DOUBLE), 'south'"
    )
    local.execute(f"ATTACH '{other}' AS other (READ_ONLY)")
    yield local
    local.close()


def _orders_model() -> SQLModel:
    return SQLModel(
        path=Path("transform/silver/orders.sql"),
        name="orders",
        schema="silver",
        full_name="silver.orders",
        sql="",
        query="SELECT 1 AS id",
        materialized="table",
    )


def test_information_schema_really_does_span_catalogs(two_warehouses):
    """The premise. If this ever stops holding, the rest is moot."""
    rows = two_warehouses.execute(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = 'silver' AND table_name = 'orders'"
    ).fetchone()[0]
    assert rows == 8


def test_profile_model_reports_only_local_columns(two_warehouses):
    from havn.engine.transform.quality import profile_model

    profile = profile_model(two_warehouses, _orders_model())

    assert profile.column_count == 3
    assert sorted(profile.null_percentages) == sorted(LOCAL_COLUMNS)
    assert sorted(profile.distinct_counts) == sorted(LOCAL_COLUMNS)


def test_row_count_pseudo_column_is_not_shadowed_by_the_other_catalog(tmp_path):
    """The ``row_count`` probe must not see the other warehouse's column."""
    from havn.engine.transform.quality import _evaluate_assertion

    other = tmp_path / "other.duckdb"
    conn = duckdb.connect(str(other))
    conn.execute("CREATE SCHEMA silver")
    conn.execute("CREATE TABLE silver.orders AS SELECT 1 AS id, 7 AS row_count")
    conn.close()

    local = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    local.execute("CREATE SCHEMA silver")
    local.execute("CREATE TABLE silver.orders AS SELECT 1 AS id UNION ALL SELECT 2")
    local.execute(f"ATTACH '{other}' AS other (READ_ONLY)")
    try:
        # Compound, so it goes through the substitution probe rather than the
        # dedicated bare-`row_count` branch.
        result = _evaluate_assertion(
            local, _orders_model(), "row_count > 1 AND id > 0"
        )
        assert result.passed, result.detail
    finally:
        local.close()


def test_contract_columns_come_from_the_local_table(two_warehouses, tmp_path):
    from havn.engine.contracts import Contract, ContractColumn, evaluate_contract

    contract = Contract(
        name="orders",
        model="silver.orders",
        columns=[
            ContractColumn(name="id", type="INTEGER"),
            ContractColumn(name="amount", type="DOUBLE"),
            ContractColumn(name="region", type="VARCHAR"),
        ],
        strict=True,
    )

    result = evaluate_contract(two_warehouses, contract)

    assert result.error is None
    assert [f.message for f in result.schema_findings] == []
    assert result.passed


def test_contract_on_a_model_only_the_other_warehouse_has_is_missing(
    two_warehouses,
):
    """The existence probe must not count the attached warehouse's copy.

    Unscoped, the contract was reported as running against a table this
    warehouse does not hold, and every assertion then failed with DuckDB's
    binder error instead of the plain "does not exist".
    """
    from havn.engine.contracts import Contract, evaluate_contract

    contract = Contract(
        name="only-there",
        model="silver.only_there",
        assertions=["row_count > 0"],
    )

    result = evaluate_contract(two_warehouses, contract)

    assert result.error == "Table silver.only_there does not exist"


def test_diff_column_info_is_local(two_warehouses):
    from havn.engine.diff import _get_column_info, _table_exists

    assert sorted(_get_column_info(two_warehouses, "silver", "orders")) == sorted(
        LOCAL_COLUMNS
    )
    assert _table_exists(two_warehouses, "silver", "orders")
    assert not _table_exists(two_warehouses, "silver", "only_there")


def test_diff_model_sees_no_schema_change_against_the_local_table(two_warehouses):
    """`havn diff` builds the new version and diffs it against the local one.

    Unscoped, the target's column list carried ``ghost`` and ``extra``, so the
    diff reported two removed columns on a model nothing had touched.
    """
    from havn.engine.diff import diff_model

    result = diff_model(
        two_warehouses,
        "SELECT 1 AS id, CAST(10.0 AS DOUBLE) AS amount, 'north' AS region "
        "UNION ALL SELECT 3, CAST(30.0 AS DOUBLE), 'east'",
        "silver",
        "orders",
    )

    assert result.error is None, result.error
    assert result.schema_changes == []
    assert (result.added, result.removed) == (1, 1)


def test_unit_test_catalog_snapshot_is_local(two_warehouses):
    from havn.engine.unit_tests import catalog_from_connection

    catalog = catalog_from_connection(two_warehouses)

    assert [c for c, _ in catalog["silver.orders"]] == LOCAL_COLUMNS


def test_describe_table_endpoint_is_local(two_warehouses):
    from havn.server.routes.query import describe_table

    payload = describe_table(
        SimpleNamespace(), "silver", "orders", two_warehouses
    )

    assert [c["name"] for c in payload["columns"]] == LOCAL_COLUMNS


def test_column_catalog_for_lineage_is_local(two_warehouses):
    from havn.engine.sql_analysis import fetch_column_catalog

    catalog = fetch_column_catalog(two_warehouses)

    assert catalog["silver.orders"] == LOCAL_COLUMNS


def test_star_expansion_for_masking_is_local(two_warehouses):
    from havn.engine.masking_rewriter import _expand_star

    assert _expand_star(two_warehouses, "silver", "orders") == LOCAL_COLUMNS
