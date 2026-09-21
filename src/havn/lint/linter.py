"""SQLFluff integration for linting SQL transform files."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.table import Table

from havn.engine.sql_analysis import _META_PREFIXES, strip_config_comments

console = Console()


def _header_line_count(lines: list[str]) -> int:
    """Length of the leading run of blank and directive lines.

    Used only to split a file for ``--fix``: the header is set aside, SQLFluff
    rewrites the body, and the two are joined back together.
    """
    count = 0
    for line in lines:
        stripped = line.strip()
        if stripped == "" or any(stripped.startswith(p) for p in _META_PREFIXES):
            count += 1
        else:
            break
    return count


def _lint_text(sql: str) -> str:
    """The text to hand SQLFluff: the file with its directive lines blanked.

    ``strip_config_comments`` blanks directives in place, so SQLFluff's line
    numbers are the file's line numbers with no offset to add back, and a
    directive that sits below the SQL (a trailing ``@assert``, say) no longer
    shows up as an unparsable section.
    """
    return strip_config_comments(sql)

# `havn lint` separates correctness from style.
#
# Default behaviour ("correctness mode") runs only the rules that catch real
# bugs: ambiguity (AM*), references (RF*), select-list duplication (AL07),
# unused CTEs (ST03), nested joins (ST01), and the handful of CV rules that
# flag NULL-equality, blocked words, and broken control flow. Layout, naming,
# and capitalisation rules are excluded so a 9-line SQL file with aligned
# `AS` columns doesn't produce 130 violations.
#
# `--style` (or a project-level `.sqlfluff`) opts back into the full SQLFluff
# rule set for a one-off spring clean.
#
# NB: SQLFluff's FluffConfig.from_kwargs expects ``rules`` and ``exclude_rules``
# as Python lists (not comma-separated strings). Passing a string causes it
# to iterate character by character and silently produce nonsense.
_CORRECTNESS_RULES = [
    # Ambiguity -- catches "can't tell what this means"
    "AM01", "AM02", "AM03", "AM04", "AM05", "AM06", "AM07", "AM08", "AM09",
    # References -- unqualified columns, missing references, etc.
    # RF03 (unqualified reference in single-table SELECT) is intentionally
    # omitted: it fires on idiomatic SQL where only one table is in scope and
    # produced ~37 violations on a 12-model project in user testing.
    "RF01", "RF02", "RF04", "RF05", "RF06",
    # Aliasing correctness (AL07 = duplicate aliases; rest are style)
    "AL07",
    # Structure correctness (avoid 02/05/06/07/09 -- those are style)
    "ST01",  # nested joins
    "ST03",  # unused CTEs
    "ST04",  # nested CASE
    "ST08",  # DISTINCT redundant parens
    "ST10",  # constant join condition
    "ST11",  # unused sources
    "ST12",  # joins/set operators must be one-per-line keywords
    # Convention correctness (avoid 01/02/03/04/06/07/10 -- those are style)
    "CV05",  # NULL = NULL is wrong, use IS NULL
    "CV08",  # PRIOR (Oracle-only, blocks portable SQL)
    "CV09",  # blocked words
    "CV11",  # cast type
    "CV12",  # control flow correctness
]


def lint(
    transform_dir: Path,
    fix: bool = False,
    dialect: str = "duckdb",
    rules: list[str] | None = None,
    style: bool = False,
) -> tuple[int, list[dict], int]:
    """Lint SQL files in the transform directory.

    Args:
        transform_dir: Path to transform/ directory
        fix: Whether to auto-fix violations
        dialect: SQL dialect for SQLFluff
        rules: Specific rules to check (None = correctness defaults; ignored
            if a project ``.sqlfluff`` is present).
        style: When True, run the full SQLFluff rule set (layout, naming,
            capitalisation, etc.) instead of just the correctness subset.

    Returns:
        Tuple of (violation_count, violations_list, fixed_count)
    """
    # Import here to avoid hard dependency at module level
    from sqlfluff.core import FluffConfig, Linter

    sql_files = sorted(transform_dir.rglob("*.sql"))
    if not sql_files:
        console.print("[yellow]No SQL files found in transform/[/yellow]")
        return 0, [], 0

    # Use .sqlfluff config file from project root if it exists,
    # falling back to kwargs-based config
    project_dir = transform_dir.parent
    sqlfluff_file = project_dir / ".sqlfluff"
    if sqlfluff_file.exists():
        overrides: dict = {}
        if rules:
            overrides["rules"] = ",".join(rules)
        config = FluffConfig.from_path(path=str(project_dir), overrides=overrides or None)
    else:
        config_kwargs: dict = {"dialect": dialect}
        if rules:
            config_kwargs["rules"] = rules
        elif not style:
            # Default: correctness-only. Pass the rule allow-list so SQLFluff
            # skips evaluating layout/naming/capitalisation rules entirely.
            config_kwargs["rules"] = _CORRECTNESS_RULES
        # else: style=True and no explicit rules -> use SQLFluff full default.
        config = FluffConfig.from_kwargs(**config_kwargs)
    linter = Linter(config=config)

    all_violations: list[dict] = []
    total_fixed = 0

    for sql_file in sql_files:
        sql = sql_file.read_text()

        if fix:
            # Fixing rewrites the SQL, so the directive header is set aside
            # and joined back on afterwards rather than blanked.
            lines = sql.split("\n")
            header_count = _header_line_count(lines)
            body = "\n".join(lines[header_count:])
            fix_result = linter.lint_string(body, fix=True)
            violations_before = len(fix_result.get_violations())
            fixed_sql, changed = fix_result.fix_string()
            if changed:
                sql = "\n".join(lines[:header_count]) + "\n" + fixed_sql
                sql_file.write_text(sql)
                total_fixed += violations_before - len(
                    linter.lint_string(fixed_sql).get_violations()
                )

        result = linter.lint_string(_lint_text(sql))

        rel_path = sql_file.relative_to(transform_dir.parent)
        for violation in result.get_violations():
            all_violations.append({
                "file": str(rel_path),
                "line": violation.line_no,
                "col": violation.line_pos,
                "code": violation.rule_code(),
                "description": violation.desc(),
                "fixable": bool(violation.fixable),
            })

    return len(all_violations), all_violations, total_fixed


def lint_file(
    sql_file: Path,
    project_dir: Path,
    fix: bool = False,
    dialect: str = "duckdb",
    rules: list[str] | None = None,
    content: str | None = None,
    style: bool = False,
) -> tuple[int, list[dict], int, str]:
    """Lint (and optionally fix) a single SQL file.

    If content is provided, lint that instead of reading from disk.
    When fix=True and content is provided, the fixed content is written to disk.

    Returns:
        Tuple of (violation_count, violations_list, fixed_count, file_content)
        file_content is the (possibly fixed) SQL content.
    """
    from sqlfluff.core import FluffConfig, Linter

    sqlfluff_file = project_dir / ".sqlfluff"
    if sqlfluff_file.exists():
        overrides: dict = {}
        if rules:
            overrides["rules"] = ",".join(rules)
        config = FluffConfig.from_path(path=str(project_dir), overrides=overrides or None)
    else:
        config_kwargs: dict = {"dialect": dialect}
        if rules:
            config_kwargs["rules"] = rules
        elif not style:
            config_kwargs["rules"] = _CORRECTNESS_RULES
        config = FluffConfig.from_kwargs(**config_kwargs)
    linter = Linter(config=config)

    sql = content if content is not None else sql_file.read_text()
    total_fixed = 0
    final_content = sql

    if fix:
        # Only the leading header is set aside for the round-trip; the rest of
        # the file is what SQLFluff rewrites.
        lines = sql.split("\n")
        header_count = _header_line_count(lines)
        body = "\n".join(lines[header_count:])
        fix_result = linter.lint_string(body, fix=True)
        violations_before = len(fix_result.get_violations())
        fixed_sql, changed = fix_result.fix_string()
        if changed:
            final_content = "\n".join(lines[:header_count]) + "\n" + fixed_sql
            sql_file.write_text(final_content)
            total_fixed = violations_before - len(
                linter.lint_string(fixed_sql).get_violations()
            )

    result = linter.lint_string(_lint_text(final_content))

    transform_dir = project_dir / "transform"
    try:
        rel_path = sql_file.relative_to(transform_dir.parent)
    except ValueError:
        rel_path = sql_file

    all_violations: list[dict] = []
    for violation in result.get_violations():
        all_violations.append({
            "file": str(rel_path),
            "line": violation.line_no,
            "col": violation.line_pos,
            "code": violation.rule_code(),
            "description": violation.desc(),
            "fixable": bool(violation.fixable),
        })

    return len(all_violations), all_violations, total_fixed, final_content


def print_violations(violations: list[dict]) -> None:
    """Pretty-print lint violations."""
    if not violations:
        console.print("[green]All SQL files pass linting.[/green]")
        return

    table = Table(title="Lint Violations")
    table.add_column("File", style="cyan")
    table.add_column("Line", justify="right")
    table.add_column("Col", justify="right")
    table.add_column("Rule", style="yellow")
    table.add_column("Description")

    for v in violations:
        table.add_row(str(v["file"]), str(v["line"]), str(v["col"]), v["code"], v["description"])

    console.print(table)
