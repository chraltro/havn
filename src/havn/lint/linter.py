"""SQLFluff integration for linting SQL transform files."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()

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
    "RF01", "RF02", "RF03", "RF04", "RF05", "RF06",
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
        return 0, []

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

        # Strip directive lines before linting (they're not SQL). Recognises
        # both the canonical @-prefixed form and the legacy SQL-comment form.
        # Count how many header lines to skip, then take the rest -- so
        # SQLFluff line numbers can be adjusted back via header_count.
        from havn.engine.sql_analysis import _META_PREFIXES
        lines = sql.split("\n")
        header_count = 0
        for line in lines:
            stripped = line.strip()
            if stripped == "" or any(stripped.startswith(p) for p in _META_PREFIXES):
                header_count += 1
            else:
                break
        clean_sql = "\n".join(lines[header_count:])

        result = linter.lint_string(clean_sql, fix=fix)
        violations_before = len(result.get_violations())

        if fix:
            fixed_sql, changed = result.fix_string()
            if changed:
                # Re-insert config comment header
                header_lines = lines[:header_count]
                sql_file.write_text("\n".join(header_lines) + "\n" + fixed_sql)
                # Re-lint to report only remaining (unfixable) violations
                result = linter.lint_string(fixed_sql)
                total_fixed += violations_before - len(result.get_violations())

        rel_path = sql_file.relative_to(transform_dir.parent)
        for violation in result.get_violations():
            all_violations.append({
                "file": str(rel_path),
                "line": violation.line_no + header_count,
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
    lines = sql.split("\n")
    header_count = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("-- config:") or stripped.startswith("-- depends_on:") or stripped.startswith("-- assert:") or stripped == "":
            header_count += 1
        else:
            break
    clean_sql = "\n".join(lines[header_count:])

    result = linter.lint_string(clean_sql, fix=fix)
    violations_before = len(result.get_violations())
    total_fixed = 0
    final_content = sql

    if fix:
        fixed_sql, changed = result.fix_string()
        if changed:
            header_lines = lines[:header_count]
            final_content = "\n".join(header_lines) + "\n" + fixed_sql
            sql_file.write_text(final_content)
            result = linter.lint_string(fixed_sql)
            total_fixed = violations_before - len(result.get_violations())
        else:
            final_content = sql

    transform_dir = project_dir / "transform"
    try:
        rel_path = sql_file.relative_to(transform_dir.parent)
    except ValueError:
        rel_path = sql_file

    all_violations: list[dict] = []
    for violation in result.get_violations():
        all_violations.append({
            "file": str(rel_path),
            "line": violation.line_no + header_count,
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
