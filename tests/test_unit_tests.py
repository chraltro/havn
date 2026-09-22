"""Tests for model unit tests: loader, runner, CLI, and API."""

from __future__ import annotations

import duckdb
import pytest
from typer.testing import CliRunner

from havn.engine.unit_tests import (
    catalog_from_connection,
    load_unit_tests,
    run_unit_tests,
)

CUSTOMERS_SQL = """\
@config materialized=table, schema=silver
@depends_on bronze.customers, bronze.orders

SELECT
    c.customer_id,
    c.name,
    count(o.order_id) AS order_count
FROM bronze.customers c
LEFT JOIN bronze.orders o ON c.customer_id = o.customer_id
GROUP BY 1, 2
"""

BASE_TEST_YML = """\
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
"""


@pytest.fixture
def project(tmp_path):
    """A minimal havn project with one model and one unit test."""
    (tmp_path / "project.yml").write_text(
        "name: test\ndatabase:\n  path: warehouse.duckdb\nconnections: {}\n"
    )
    (tmp_path / "transform" / "silver").mkdir(parents=True)
    (tmp_path / "transform" / "silver" / "customers.sql").write_text(CUSTOMERS_SQL)
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "customers.yml").write_text(BASE_TEST_YML)
    return tmp_path


def write_test(project, body: str, name: str = "customers.yml") -> None:
    (project / "tests" / "unit" / name).write_text(body)


def only(result):
    assert len(result.results) == 1, result.to_dict()
    return result.results[0]


# --- loading -----------------------------------------------------------


def test_loads_declared_test(project):
    cases, errors = load_unit_tests(project)
    assert errors == []
    assert len(cases) == 1
    case = cases[0]
    assert case.model == "silver.customers"
    assert case.name == "counts orders per customer"
    assert set(case.given) == {"bronze.customers", "bronze.orders"}
    assert case.given["bronze.customers"].columns == {
        "customer_id": "INTEGER",
        "name": "VARCHAR",
    }
    assert case.expect.ordered is False


def test_missing_tests_dir_is_not_an_error(tmp_path):
    cases, errors = load_unit_tests(tmp_path)
    assert cases == []
    assert errors == []


def test_invalid_yaml_is_collected(project):
    write_test(project, "model: silver.customers\ntests: [oops\n")
    cases, errors = load_unit_tests(project)
    assert cases == []
    assert len(errors) == 1
    assert "invalid YAML" in errors[0]


def test_top_level_must_be_mapping(project):
    write_test(project, "- just\n- a\n- list\n")
    _, errors = load_unit_tests(project)
    assert any("top level must be a mapping" in e for e in errors)


def test_missing_tests_key(project):
    write_test(project, "model: silver.customers\n")
    _, errors = load_unit_tests(project)
    assert any("missing 'tests' list" in e for e in errors)


def test_invalid_model_identifier(project):
    write_test(project, "model: 'silver.customers; DROP'\ntests: []\n")
    _, errors = load_unit_tests(project)
    assert any("Invalid model" in e or "must be 'table'" in e for e in errors)


def test_missing_name_is_an_error(project):
    write_test(
        project,
        "model: silver.customers\ntests:\n  - given: {bronze.customers: {rows: []}}\n"
        "    expect: {rows: []}\n",
    )
    _, errors = load_unit_tests(project)
    assert any("missing 'name'" in e for e in errors)


def test_missing_expect_is_an_error(project):
    write_test(
        project,
        "model: silver.customers\ntests:\n  - name: t\n"
        "    given: {bronze.customers: {rows: []}}\n",
    )
    _, errors = load_unit_tests(project)
    assert any("missing 'expect'" in e for e in errors)


def test_empty_given_is_an_error(project):
    write_test(
        project,
        "model: silver.customers\ntests:\n  - name: t\n    given: {}\n    expect: {rows: []}\n",
    )
    _, errors = load_unit_tests(project)
    assert any("'given' must be a non-empty mapping" in e for e in errors)


def test_invalid_column_type_rejected(project):
    write_test(
        project,
        "model: silver.customers\ntests:\n  - name: t\n"
        "    given:\n      bronze.customers:\n"
        "        columns: {customer_id: \"INTEGER); DROP TABLE x; --\"}\n"
        "        rows: []\n"
        "    expect: {rows: []}\n",
    )
    _, errors = load_unit_tests(project)
    assert any("invalid column type" in e for e in errors)


def test_duplicate_test_names_detected(project):
    body = BASE_TEST_YML + BASE_TEST_YML.split("tests:\n")[1]
    write_test(project, body)
    cases, errors = load_unit_tests(project)
    assert len(cases) == 1
    assert any("duplicate test" in e for e in errors)


def test_list_rows_need_matching_column_count(project):
    write_test(
        project,
        "model: silver.customers\ntests:\n  - name: t\n"
        "    given:\n      bronze.customers:\n"
        "        columns: {customer_id: INTEGER, name: VARCHAR}\n"
        "        rows: [[1]]\n"
        "    expect: {rows: []}\n",
    )
    _, errors = load_unit_tests(project)
    assert any("1 value(s) but 2 column(s)" in e for e in errors)


def test_expect_list_rows_need_columns(project):
    write_test(
        project,
        "model: silver.customers\ntests:\n  - name: t\n"
        "    given:\n      bronze.customers: {rows: [{customer_id: 1}]}\n"
        "    expect:\n      rows: [[1, 'Ann', 0]]\n",
    )
    _, errors = load_unit_tests(project)
    assert any("expect.columns" in e for e in errors)


# --- running -----------------------------------------------------------


def test_passing_test(project):
    result = run_unit_tests(project)
    res = only(result)
    assert res.status == "pass", res.message
    assert result.ok
    assert res.columns == ["customer_id", "name", "order_count"]


@pytest.mark.parametrize(
    "label,expression",
    [
        ("read_csv", "(SELECT count(*) FROM read_csv('{path}'))"),
        ("read_text", "(SELECT count(*) FROM read_text('{path}'))"),
        ("glob", "(SELECT count(*) FROM glob('{path}'))"),
        ("replacement scan", "(SELECT count(*) FROM '{path}')"),
    ],
)
def test_a_model_under_test_cannot_read_a_server_file(
    project, tmp_path, label, expression
):
    """The in-memory test connection has no filesystem.

    A unit test runs the model's own SQL, which on the server can be a file
    someone with write permission just saved. Fixture rows are the whole
    input; nothing legitimate needs the disk.
    """
    secret = tmp_path / "secret.csv"
    secret.write_text("token\nhunter2\n")
    expr = expression.format(path=secret)
    (project / "transform" / "silver" / "customers.sql").write_text(
        "@config materialized=table, schema=silver\n"
        "@depends_on bronze.customers\n\n"
        f"SELECT customer_id, name, {expr} AS order_count FROM bronze.customers\n"
    )
    res = only(run_unit_tests(project))
    assert res.status == "error", (label, res.to_dict())
    assert "hunter2" not in str(res.to_dict()), label


def test_a_model_under_test_cannot_write_a_server_file(project, tmp_path):
    target = tmp_path / "exfiltrated.csv"
    from havn.engine.unit_tests import _prepare_connection

    conn = duckdb.connect(":memory:")
    try:
        _prepare_connection(conn, project)
        for statement in (
            f"COPY (SELECT 1 AS a) TO '{target}'",
            f"ATTACH '{target}' AS ex",
            "INSTALL httpfs",
            "LOAD httpfs",
            "SET enable_external_access = true",
        ):
            with pytest.raises(duckdb.Error):
                conn.execute(statement)
    finally:
        conn.close()
    assert not target.exists()


def test_dict_and_list_rows_are_equivalent(project):
    write_test(
        project,
        """\
model: silver.customers
tests:
  - name: list form
    given:
      bronze.customers:
        columns: {customer_id: INTEGER, name: VARCHAR}
        rows: [[1, "Ann"]]
      bronze.orders:
        columns: {order_id: INTEGER, customer_id: INTEGER}
        rows: [[1, 1]]
    expect:
      columns: {customer_id: INTEGER, name: VARCHAR, order_count: BIGINT}
      rows: [[1, "Ann", 1]]
""",
    )
    assert only(run_unit_tests(project)).status == "pass"


def test_csv_rows(project):
    write_test(
        project,
        """\
model: silver.customers
tests:
  - name: csv fixtures
    given:
      bronze.customers:
        columns: {customer_id: INTEGER, name: VARCHAR}
        format: csv
        csv: |
          customer_id,name
          1,Ann
          2,Bo
      bronze.orders:
        format: csv
        csv: |
          order_id,customer_id
          1,1
          2,1
    expect:
      format: csv
      csv: |
        customer_id,name,order_count
        1,Ann,2
        2,Bo,0
""",
    )
    res = only(run_unit_tests(project))
    assert res.status == "pass", res.message


def test_inferred_types_from_fixture_values(project):
    """No columns: block anywhere, so types come from the Python values."""
    write_test(
        project,
        """\
model: silver.customers
tests:
  - name: inferred
    given:
      bronze.customers:
        rows:
          - {customer_id: 1, name: "Ann"}
      bronze.orders:
        rows:
          - {order_id: 1, customer_id: 1, amount: 5.0}
    expect:
      rows:
        - {customer_id: 1, name: "Ann", order_count: 1}
""",
    )
    res = only(run_unit_tests(project))
    assert res.status == "pass", res.message


def test_decimal_double_drift_does_not_fail(tmp_path):
    """A DECIMAL SUM must compare equal to a plain 7.5 in the fixture."""
    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: warehouse.duckdb\n")
    (tmp_path / "transform" / "gold").mkdir(parents=True)
    (tmp_path / "transform" / "gold" / "totals.sql").write_text(
        "@config materialized=table, schema=gold\n"
        "@depends_on bronze.orders\n\n"
        "SELECT customer_id, sum(amount) AS total FROM bronze.orders GROUP BY 1\n"
    )
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "totals.yml").write_text(
        """\
model: gold.totals
tests:
  - name: sums amounts
    given:
      bronze.orders:
        columns:
          customer_id: INTEGER
          amount: "DECIMAL(10,2)"
        rows:
          - [1, 5.0]
          - [1, 2.5]
    expect:
      rows:
        - {customer_id: 1, total: 7.5}
"""
    )
    res = only(run_unit_tests(tmp_path))
    assert res.status == "pass", res.message


def test_missing_and_unexpected_rows_reported(project):
    write_test(
        project,
        """\
model: silver.customers
tests:
  - name: wrong expectation
    given:
      bronze.customers:
        columns: {customer_id: INTEGER, name: VARCHAR}
        rows: [[1, "Ann"]]
      bronze.orders:
        columns: {order_id: INTEGER, customer_id: INTEGER}
        rows: []
    expect:
      rows:
        - {customer_id: 1, name: "Ann", order_count: 9}
""",
    )
    res = only(run_unit_tests(project))
    assert res.status == "fail"
    assert res.missing_count == 1
    assert res.unexpected_count == 1
    assert res.missing_rows[0]["order_count"] == 9
    assert res.unexpected_rows[0]["order_count"] == 0


def test_unordered_comparison_ignores_order(project):
    write_test(
        project,
        """\
model: silver.customers
tests:
  - name: any order
    given:
      bronze.customers:
        columns: {customer_id: INTEGER, name: VARCHAR}
        rows: [[1, "Ann"], [2, "Bo"]]
      bronze.orders:
        columns: {order_id: INTEGER, customer_id: INTEGER}
        rows: []
    expect:
      rows:
        - {customer_id: 2, name: "Bo", order_count: 0}
        - {customer_id: 1, name: "Ann", order_count: 0}
""",
    )
    assert only(run_unit_tests(project)).status == "pass"


def test_ordered_comparison_is_positional(tmp_path):
    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: warehouse.duckdb\n")
    (tmp_path / "transform" / "silver").mkdir(parents=True)
    (tmp_path / "transform" / "silver" / "sorted_names.sql").write_text(
        "@config materialized=table, schema=silver\n"
        "@depends_on bronze.customers\n\n"
        "SELECT name FROM bronze.customers ORDER BY name\n"
    )
    (tmp_path / "tests" / "unit").mkdir(parents=True)

    def yml(first: str, second: str, ordered: str) -> str:
        return (
            "model: silver.sorted_names\n"
            "tests:\n"
            "  - name: order check\n"
            "    given:\n"
            "      bronze.customers:\n"
            "        columns: {name: VARCHAR}\n"
            "        rows: [[\"Bo\"], [\"Ann\"]]\n"
            "    expect:\n"
            f"      ordered: {ordered}\n"
            "      rows:\n"
            f"        - {{name: \"{first}\"}}\n"
            f"        - {{name: \"{second}\"}}\n"
        )

    path = tmp_path / "tests" / "unit" / "sorted.yml"

    path.write_text(yml("Ann", "Bo", "true"))
    assert only(run_unit_tests(tmp_path)).status == "pass"

    path.write_text(yml("Bo", "Ann", "true"))
    res = only(run_unit_tests(tmp_path))
    assert res.status == "fail"
    assert res.missing_count == 2

    # The same rows in the wrong order pass when ordering is not asserted.
    path.write_text(yml("Bo", "Ann", "false"))
    assert only(run_unit_tests(tmp_path)).status == "pass"


def test_duplicate_rows_are_counted(project):
    """Multiset semantics: two identical output rows need two expected rows."""
    write_test(
        project,
        """\
model: silver.customers
tests:
  - name: duplicates
    given:
      bronze.customers:
        columns: {customer_id: INTEGER, name: VARCHAR}
        rows: [[1, "Ann"], [1, "Ann"]]
      bronze.orders:
        columns: {order_id: INTEGER, customer_id: INTEGER}
        rows: []
    expect:
      rows:
        - {customer_id: 1, name: "Ann", order_count: 0}
""",
    )
    res = only(run_unit_tests(project))
    assert res.status == "pass"


def test_column_mismatch_reported(project):
    write_test(
        project,
        """\
model: silver.customers
tests:
  - name: wrong columns
    given:
      bronze.customers:
        columns: {customer_id: INTEGER, name: VARCHAR}
        rows: [[1, "Ann"]]
      bronze.orders:
        columns: {order_id: INTEGER, customer_id: INTEGER}
        rows: []
    expect:
      rows:
        - {customer_id: 1, total_orders: 0}
""",
    )
    res = only(run_unit_tests(project))
    assert res.status == "fail"
    assert "column mismatch" in res.message
    assert "total_orders" in res.message


def test_unmocked_upstream_is_an_error(project):
    write_test(
        project,
        """\
model: silver.customers
tests:
  - name: forgot orders
    given:
      bronze.customers:
        columns: {customer_id: INTEGER, name: VARCHAR}
        rows: [[1, "Ann"]]
    expect:
      rows: []
""",
    )
    res = only(run_unit_tests(project))
    assert res.status == "error"
    assert "bronze.orders" in res.message
    assert "never read the warehouse" in res.message


def test_unknown_model_is_an_error(project):
    write_test(
        project,
        "model: silver.nope\ntests:\n  - name: t\n"
        "    given: {bronze.customers: {rows: [{customer_id: 1}]}}\n"
        "    expect: {rows: []}\n",
    )
    res = only(run_unit_tests(project))
    assert res.status == "error"
    assert "unknown model" in res.message


def test_model_sql_failure_is_an_error(tmp_path):
    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: warehouse.duckdb\n")
    (tmp_path / "transform" / "silver").mkdir(parents=True)
    (tmp_path / "transform" / "silver" / "broken.sql").write_text(
        "@config materialized=table, schema=silver\n"
        "@depends_on bronze.customers\n\n"
        "SELECT no_such_column FROM bronze.customers\n"
    )
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "broken.yml").write_text(
        "model: silver.broken\ntests:\n  - name: t\n"
        "    given:\n      bronze.customers:\n        columns: {customer_id: INTEGER}\n"
        "        rows: [[1]]\n"
        "    expect: {rows: []}\n"
    )
    res = only(run_unit_tests(tmp_path))
    assert res.status == "error"
    assert "model SQL failed" in res.message


def test_macro_is_available_inside_the_model(tmp_path):
    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: warehouse.duckdb\n")
    (tmp_path / "macros").mkdir()
    (tmp_path / "macros" / "utils.py").write_text(
        "from havn import macro\n\n\n"
        "@macro\n"
        "def mask_email(email: str) -> str:\n"
        '    if not email or "@" not in email:\n'
        '        return "***"\n'
        '    return "***@" + email.split("@", 1)[1]\n'
    )
    (tmp_path / "transform" / "silver").mkdir(parents=True)
    (tmp_path / "transform" / "silver" / "masked.sql").write_text(
        "@config materialized=table, schema=silver\n"
        "@depends_on bronze.customers\n\n"
        "SELECT customer_id, mask_email(email) AS email FROM bronze.customers\n"
    )
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "masked.yml").write_text(
        """\
model: silver.masked
tests:
  - name: masks the local part
    given:
      bronze.customers:
        columns: {customer_id: INTEGER, email: VARCHAR}
        rows: [[1, "ann@example.com"]]
    expect:
      rows:
        - {customer_id: 1, email: "***@example.com"}
"""
    )
    res = only(run_unit_tests(tmp_path))
    assert res.status == "pass", res.message


def test_narrow_mock_warning_for_select_star(tmp_path):
    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: warehouse.duckdb\n")
    (tmp_path / "transform" / "silver").mkdir(parents=True)
    (tmp_path / "transform" / "silver" / "passthrough.sql").write_text(
        "@config materialized=table, schema=silver\n"
        "@depends_on bronze.customers\n\n"
        "SELECT * FROM bronze.customers\n"
    )
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "passthrough.yml").write_text(
        """\
model: silver.passthrough
tests:
  - name: passes rows through
    given:
      bronze.customers:
        columns: {customer_id: INTEGER}
        rows: [[1]]
    expect:
      rows:
        - {customer_id: 1}
"""
    )
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA bronze")
    conn.execute(
        "CREATE TABLE bronze.customers (customer_id INTEGER, name VARCHAR, email VARCHAR)"
    )
    catalog = catalog_from_connection(conn)
    conn.close()

    res = only(run_unit_tests(tmp_path, catalog=catalog))
    assert res.status == "pass", res.message
    assert any("narrower table" in w for w in res.warnings), res.warnings
    assert any("name" in w for w in res.warnings)

    # Without a catalog there is nothing to compare against, so no warning.
    res = only(run_unit_tests(tmp_path))
    assert res.warnings == []


def test_narrow_mock_does_not_warn_without_a_star(project):
    """An explicit column list is unaffected by columns the mock omits."""
    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA bronze")
    conn.execute("CREATE TABLE bronze.customers (customer_id INTEGER, name VARCHAR, email VARCHAR)")
    conn.execute("CREATE TABLE bronze.orders (order_id INTEGER, customer_id INTEGER, amount DOUBLE)")
    catalog = catalog_from_connection(conn)
    conn.close()

    res = only(run_unit_tests(project, catalog=catalog))
    assert res.status == "pass", res.message
    assert not any("narrower" in w for w in res.warnings), res.warnings


def test_catalog_supplies_types_when_columns_omitted(tmp_path):
    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: warehouse.duckdb\n")
    (tmp_path / "transform" / "silver").mkdir(parents=True)
    (tmp_path / "transform" / "silver" / "typed.sql").write_text(
        "@config materialized=table, schema=silver\n"
        "@depends_on bronze.events\n\n"
        "SELECT event_id, typeof(amount) AS amount_type FROM bronze.events\n"
    )
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "typed.yml").write_text(
        """\
model: silver.typed
tests:
  - name: uses warehouse types
    given:
      bronze.events:
        rows: [{event_id: 1, amount: 5.0}]
    expect:
      rows: [{event_id: 1, amount_type: "DECIMAL(10,2)"}]
"""
    )
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA bronze")
    conn.execute("CREATE TABLE bronze.events (event_id INTEGER, amount DECIMAL(10,2))")
    catalog = catalog_from_connection(conn)
    conn.close()

    res = only(run_unit_tests(tmp_path, catalog=catalog))
    assert res.status == "pass", res.message


def test_model_filter(project):
    (project / "transform" / "silver" / "other.sql").write_text(
        "@config materialized=table, schema=silver\n"
        "@depends_on bronze.customers\n\nSELECT customer_id FROM bronze.customers\n"
    )
    (project / "tests" / "unit" / "other.yml").write_text(
        "model: silver.other\ntests:\n  - name: t\n"
        "    given:\n      bronze.customers:\n        columns: {customer_id: INTEGER}\n"
        "        rows: [[1]]\n"
        "    expect: {rows: [{customer_id: 1}]}\n"
    )
    assert len(run_unit_tests(project).results) == 2
    assert len(run_unit_tests(project, model="silver.other").results) == 1
    assert len(run_unit_tests(project, model="other").results) == 1


def test_mock_for_non_upstream_warns(project):
    write_test(
        project,
        """\
model: silver.customers
tests:
  - name: extra mock
    given:
      bronze.customers:
        columns: {customer_id: INTEGER, name: VARCHAR}
        rows: [[1, "Ann"]]
      bronze.orders:
        columns: {order_id: INTEGER, customer_id: INTEGER}
        rows: []
      bronze.unused:
        columns: {x: INTEGER}
        rows: []
    expect:
      rows:
        - {customer_id: 1, name: "Ann", order_count: 0}
""",
    )
    res = only(run_unit_tests(project))
    assert res.status == "pass", res.message
    assert any("bronze.unused" in w for w in res.warnings)


def test_incremental_model_runs_full_refresh_query(tmp_path):
    (tmp_path / "project.yml").write_text("name: t\ndatabase:\n  path: warehouse.duckdb\n")
    (tmp_path / "transform" / "silver").mkdir(parents=True)
    (tmp_path / "transform" / "silver" / "events.sql").write_text(
        "@config materialized=incremental, schema=silver, unique_key=event_id\n"
        "@depends_on bronze.events\n\n"
        "SELECT event_id, amount FROM bronze.events\n"
    )
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "events.yml").write_text(
        """\
model: silver.events
tests:
  - name: passes every row on a full refresh
    given:
      bronze.events:
        columns: {event_id: INTEGER, amount: INTEGER}
        rows: [[1, 10], [2, 20]]
    expect:
      rows:
        - {event_id: 1, amount: 10}
        - {event_id: 2, amount: 20}
"""
    )
    res = only(run_unit_tests(tmp_path))
    assert res.status == "pass", res.message


def test_run_result_serializes(project):
    payload = run_unit_tests(project).to_dict()
    assert payload["passed"] == 1
    assert payload["ok"] is True
    assert payload["results"][0]["status"] == "pass"


# --- CLI ---------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


def test_cli_test_passes(project, runner):
    from havn.cli import app

    result = runner.invoke(app, ["test", "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert "counts orders per customer" in result.output
    assert "1 passed" in result.output


def test_cli_test_fails_with_nonzero_exit(project, runner):
    from havn.cli import app

    write_test(
        project,
        """\
model: silver.customers
tests:
  - name: broken
    given:
      bronze.customers:
        columns: {customer_id: INTEGER, name: VARCHAR}
        rows: [[1, "Ann"]]
      bronze.orders:
        columns: {order_id: INTEGER, customer_id: INTEGER}
        rows: []
    expect:
      rows:
        - {customer_id: 1, name: "Ann", order_count: 3}
""",
    )
    result = runner.invoke(app, ["test", "--project", str(project), "-v"])
    assert result.exit_code == 1
    assert "broken" in result.output


def test_cli_test_model_filter(project, runner):
    from havn.cli import app

    result = runner.invoke(
        app, ["test", "--project", str(project), "--model", "silver.nope"]
    )
    assert result.exit_code == 0
    assert "No unit tests" in result.output


def test_cli_check_runs_unit_tests(project, runner):
    from havn.cli import app

    result = runner.invoke(app, ["check", "--project", str(project)])
    assert "unit test" in result.output.lower()

    skipped = runner.invoke(app, ["check", "--project", str(project), "--no-unit-tests"])
    assert "unit test" not in skipped.output.lower()


# --- API ---------------------------------------------------------------


@pytest.fixture
def client(project):
    from fastapi.testclient import TestClient

    import havn.server.app as server_app

    server_app.PROJECT_DIR = project
    return TestClient(server_app.app)


def test_api_lists_unit_tests(client):
    resp = client.get("/api/unit-tests")
    assert resp.status_code == 200
    body = resp.json()
    assert body["errors"] == []
    assert len(body["tests"]) == 1
    assert body["tests"][0]["model"] == "silver.customers"


def test_api_runs_unit_tests(client):
    resp = client.post("/api/unit-tests/run", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["passed"] == 1
    assert body["results"][0]["status"] == "pass"


def test_api_run_respects_model_filter(client):
    resp = client.post("/api/unit-tests/run", json={"model": "silver.nope"})
    assert resp.status_code == 200
    assert resp.json()["results"] == []


# --- MCP ---------------------------------------------------------------


def test_mcp_run_unit_tests_tool(project):
    from havn.mcp.server import MCPServer

    server = MCPServer(project)
    tools = server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )["result"]["tools"]
    assert any(t["name"] == "run_unit_tests" for t in tools)

    resp = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "run_unit_tests", "arguments": {}},
        }
    )
    assert "isError" not in resp["result"]
    assert '"passed": 1' in resp["result"]["content"][0]["text"]
