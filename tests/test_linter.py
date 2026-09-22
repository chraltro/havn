"""Tests for the SQLFluff integration.

The point of interest is line numbers: a violation has to be reported at the
line of the file it is actually on, so editor markers and CI output land in
the right place.
"""

from __future__ import annotations

import textwrap

import pytest

from havn.lint.linter import LintRefused, lint, lint_file

# Line 5 references a table that is not in the FROM clause (RF01). Lines 1-2
# are a modern `@config` header, and the `@assert` below the SQL is a
# directive in the middle of the file rather than in the header block.
MODEL_SQL = textwrap.dedent("""\
    @config materialized=table, schema=gold

    SELECT
        t.id,
        other.name
    FROM bronze.t AS t
    @assert row_count > 0
""")


@pytest.fixture
def project(tmp_path):
    transform_dir = tmp_path / "transform"
    (transform_dir / "gold").mkdir(parents=True)
    sql_file = transform_dir / "gold" / "model.sql"
    sql_file.write_text(MODEL_SQL)
    return tmp_path, transform_dir, sql_file


def test_lint_file_reports_file_line_numbers(project):
    project_dir, _, sql_file = project
    count, violations, fixed, content = lint_file(sql_file, project_dir)

    assert count >= 1
    assert [v["line"] for v in violations] == [5] * count
    assert content == MODEL_SQL
    assert fixed == 0


def test_lint_file_ignores_directives_below_the_sql(project):
    """A trailing `@assert` is not SQL and must not be reported as one."""
    _, _, sql_file = project
    _, violations, _, _ = lint_file(sql_file, sql_file.parents[2])
    assert not [v for v in violations if v["code"] == "PRS"]


def test_lint_reports_file_line_numbers(project):
    _, transform_dir, _ = project
    count, violations, fixed = lint(transform_dir)

    assert count >= 1
    assert [v["line"] for v in violations] == [5] * count
    assert not [v for v in violations if v["code"] == "PRS"]


def test_lint_file_legacy_header_line_numbers(tmp_path):
    """The legacy comment header must keep working."""
    transform_dir = tmp_path / "transform"
    (transform_dir / "gold").mkdir(parents=True)
    sql_file = transform_dir / "gold" / "legacy.sql"
    sql_file.write_text(textwrap.dedent("""\
        -- config: materialized=table, schema=gold
        -- depends_on: bronze.t

        SELECT
            t.id,
            other.name
        FROM bronze.t AS t
    """))
    _, violations, _, _ = lint_file(sql_file, tmp_path)
    assert [v["line"] for v in violations] == [6] * len(violations)
    assert violations


def test_lint_fix_keeps_the_directive_header(tmp_path):
    """`--fix` rewrites the SQL but must not eat the header."""
    transform_dir = tmp_path / "transform"
    (transform_dir / "gold").mkdir(parents=True)
    sql_file = transform_dir / "gold" / "fixme.sql"
    sql_file.write_text(textwrap.dedent("""\
        @config materialized=table, schema=gold
        @description A model to fix

        SELECT DISTINCT(t.id) AS id
        FROM bronze.t AS t
    """))
    lint_file(sql_file, tmp_path, fix=True)
    written = sql_file.read_text()
    assert "@config materialized=table, schema=gold" in written
    assert "@description A model to fix" in written
    assert "bronze.t" in written


# ---------------------------------------------------------------------------
# Installed packages
# ---------------------------------------------------------------------------


@pytest.fixture
def package_project(tmp_path):
    """A project whose transform dir sits inside an installed package."""
    pkg = tmp_path / "havn_packages" / "crm"
    transform_dir = pkg / "transform" / "gold"
    transform_dir.mkdir(parents=True)
    sql_file = transform_dir / "model.sql"
    sql_file.write_text(MODEL_SQL)
    return pkg, sql_file


def test_fix_refuses_a_path_inside_havn_packages(package_project):
    """`havn packages install` rebuilds the checkout, so a fix there is lost."""
    pkg, sql_file = package_project
    before = sql_file.read_text()

    with pytest.raises(LintRefused, match="havn_packages"):
        lint(pkg / "transform", fix=True)
    with pytest.raises(LintRefused, match="havn_packages"):
        lint_file(sql_file, pkg, fix=True)

    assert sql_file.read_text() == before


def test_checking_a_path_inside_havn_packages_is_allowed(package_project):
    """Only the rewrite is refused; reading the file is harmless."""
    pkg, sql_file = package_project
    count, violations, fixed = lint(pkg / "transform", fix=False)
    assert fixed == 0
    assert count == len(violations)


def test_cli_lint_fix_refuses_inside_havn_packages(tmp_path):
    from typer.testing import CliRunner

    from havn.cli import app

    pkg = tmp_path / "havn_packages" / "crm"
    (pkg / "transform" / "gold").mkdir(parents=True)
    (pkg / "transform" / "gold" / "model.sql").write_text(MODEL_SQL)
    (pkg / "project.yml").write_text("name: crm\ndatabase:\n  path: warehouse.duckdb\n")

    result = CliRunner().invoke(app, ["lint", "--fix", "-p", str(pkg)])
    assert result.exit_code == 1
    assert "havn_packages" in result.output
    assert "packages install" in result.output
