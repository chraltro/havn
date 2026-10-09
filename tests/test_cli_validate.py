"""Tests for `havn validate`, including the --bind/--no-bind flag."""

from __future__ import annotations

import duckdb
import pytest
from typer.testing import CliRunner

from havn.cli import app

runner = CliRunner()


@pytest.fixture
def project(tmp_path):
    (tmp_path / "project.yml").write_text(
        "name: validate-test\ndatabase:\n  path: warehouse.duckdb\n"
    )
    for sub in ("silver", "gold"):
        (tmp_path / "transform" / sub).mkdir(parents=True)
    (tmp_path / "transform" / "silver" / "orders.sql").write_text(
        "@config materialized=table, schema=silver\n\n"
        "SELECT order_id, amount FROM landing.orders\n"
    )
    return tmp_path


@pytest.fixture
def warehouse(project):
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA landing")
    conn.execute("CREATE TABLE landing.orders (order_id INTEGER, amount DOUBLE)")
    conn.close()
    return project


def bad_model(project):
    (project / "transform" / "gold" / "summary.sql").write_text(
        "@config materialized=table, schema=gold\n"
        "\n"
        "SELECT\n"
        "  no_such_column\n"
        "FROM silver.orders\n"
    )


def test_validate_passes_on_a_clean_project(warehouse):
    result = runner.invoke(app, ["validate", "--project", str(warehouse)])
    assert result.exit_code == 0, result.output
    assert "bind" in result.output
    assert "Validation passed" in result.output


def test_validate_bind_reports_the_error_with_a_line(warehouse):
    bad_model(warehouse)
    result = runner.invoke(app, ["validate", "--project", str(warehouse)])
    assert result.exit_code == 1, result.output
    assert "bind error" in result.output
    assert "gold.summary:4" in result.output
    assert "no_such_column" in result.output


def test_validate_no_bind_skips_the_pass(warehouse):
    bad_model(warehouse)
    result = runner.invoke(
        app, ["validate", "--project", str(warehouse), "--no-bind"]
    )
    assert result.exit_code == 0, result.output
    assert "bind error" not in result.output


def test_validate_without_a_warehouse_skips_the_bind_pass(project):
    bad_model(project)
    result = runner.invoke(app, ["validate", "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert "bind error" not in result.output


def test_validate_bind_requested_without_a_warehouse_warns(project):
    result = runner.invoke(app, ["validate", "--project", str(project), "--bind"])
    assert result.exit_code == 0, result.output
    assert "--bind needs a warehouse" in result.output
