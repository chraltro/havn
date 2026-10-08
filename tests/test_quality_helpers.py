"""Assertion-helper edge cases."""

from __future__ import annotations

import duckdb
import pytest

from havn.engine.transform.quality import _evaluate_assertion, failing_rows_sql


class _Model:
    full_name = "main.t"
    grain: list[str] = []
    owner = None


@pytest.fixture()
def conn():
    c = duckdb.connect()
    c.execute("CREATE TABLE t AS SELECT * FROM (VALUES (1, 'a'), (1, 'b')) v(id, cat)")
    yield c
    c.close()


@pytest.mark.parametrize(
    "expr, expected_hint",
    [
        ("unique(id, cat)", "@grain id, cat"),
        ("no_nulls(id, cat)", "no_nulls(id)"),
    ],
)
def test_multi_column_helper_explains_itself(conn, expr, expected_hint):
    """A composite unique()/no_nulls() used to surface as a DuckDB parser error.

    It is a real mistake to catch -- composite uniqueness is spelled
    ``@grain`` -- but the message has to name the fix.
    """
    result = _evaluate_assertion(conn, _Model(), expr)

    assert not result.passed
    assert "single column" in result.detail
    assert expected_hint in result.detail
    assert "Parser Error" not in result.detail


@pytest.mark.parametrize("expr", ["unique(id, cat)", "no_nulls(id, cat)"])
def test_multi_column_helper_offers_no_failing_rows(expr):
    """The assertion never ran, so there is no row set to show."""
    assert failing_rows_sql(_Model(), expr) is None


def test_single_column_unique_still_works(conn):
    result = _evaluate_assertion(conn, _Model(), "unique(id)")
    assert not result.passed
    assert "duplicate" in result.detail

    result = _evaluate_assertion(conn, _Model(), "unique(cat)")
    assert result.passed
