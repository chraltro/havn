"""Data quality assertions and auto-profiling."""

from __future__ import annotations

import logging
import re

import duckdb

from .models import AssertionResult, ProfileResult, SQLModel

logger = logging.getLogger("havn.transform")


def run_assertions(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
) -> list[AssertionResult]:
    """Run data quality assertions against a built model.

    Supported assertion forms:
        -- assert: row_count > 0
        -- assert: no_nulls(column_name)
        -- assert: unique(column_name)
        -- assert: accepted_values(column, ['a', 'b', 'c'])
        -- assert: expression_that_returns_true
    """
    results: list[AssertionResult] = []
    # Prefer parsed (expr, severity) tuples when present so per-assertion
    # severity flows through. Fall back to model.assertions for callers
    # that only populated the legacy list.
    specs: list[tuple[str, str]]
    if model.assertion_specs:
        specs = list(model.assertion_specs)
    elif model.assertions:
        specs = [(e, "error") for e in model.assertions]
    else:
        specs = []

    # Note: we do NOT early-return on ``not specs`` — a model with only
    # @grain (no @assert lines) still needs the synthesised grain check
    # to run, which happens below the spec loop.

    for expr, severity in specs:
        try:
            result = _evaluate_assertion(conn, model, expr)
            result.severity = severity
            result.owner = model.owner
            results.append(result)
        except Exception as e:
            results.append(AssertionResult(
                expression=expr,
                passed=False,
                detail=f"Assertion error: {e}",
                severity=severity,
                owner=model.owner,
            ))

    # Auto-grain assertion: synthesised after parsed assertions so it
    # always shows up next to user-declared @asserts in the results.
    if model.grain:
        try:
            results.append(_evaluate_grain(conn, model))
        except Exception as e:
            results.append(AssertionResult(
                expression=f"grain({', '.join(model.grain)})",
                passed=False,
                detail=f"Grain check failed: {e}",
                severity="error",
                owner=model.owner,
            ))

    return results


def _evaluate_grain(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
) -> AssertionResult:
    """Synthesize and evaluate a uniqueness assertion for ``model.grain``."""
    table = model.full_name
    cols = ", ".join(f'"{c}"' for c in model.grain)
    expr_label = f"grain({', '.join(model.grain)})"
    row = conn.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT ({cols})) FROM {table}"
    ).fetchone()
    total, distinct = row[0], row[1]
    passed = total == distinct
    if passed:
        detail = f"unique on ({', '.join(model.grain)}) — {total:,} rows"
    else:
        detail = (
            f"grain violated: {total:,} rows, {distinct:,} distinct on "
            f"({', '.join(model.grain)}); {total - distinct:,} duplicate(s)"
        )
    return AssertionResult(
        expression=expr_label,
        passed=passed,
        detail=detail,
        severity="error",
        owner=model.owner,
    )


def check_source_freshness(
    conn: duckdb.DuckDBPyConnection,
    specs: list[dict],
) -> list[dict]:
    """Evaluate each ``@source_freshness`` spec.

    Returns one result dict per spec::

        {"table": "landing.txns", "max_age_seconds": 86400,
         "age_seconds": 12345, "is_stale": False, "severity": "error",
         "error": None}

    A spec without an ``on`` column falls back to ``COUNT(*) == 0`` as the
    staleness signal.
    """
    out: list[dict] = []
    for spec in specs:
        table = spec["table"]
        on_col = spec.get("on")
        max_age = int(spec.get("max_age_seconds") or 0)
        result: dict = {
            "table": table,
            "max_age_seconds": max_age,
            "on": on_col,
            "severity": spec.get("severity", "error"),
            "age_seconds": None,
            "is_stale": False,
            "error": None,
        }
        try:
            if on_col:
                # EPOCH yields a DOUBLE, sidestepping pytz on TIMESTAMPTZ.
                row = conn.execute(
                    f"SELECT EXTRACT(EPOCH FROM (current_timestamp - MAX({on_col}))) FROM {table}"
                ).fetchone()
                age = row[0] if row else None
                result["age_seconds"] = float(age) if age is not None else None
                if age is None:
                    # Empty source counts as stale.
                    result["is_stale"] = True
                else:
                    result["is_stale"] = float(age) > max_age
            else:
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                result["is_stale"] = (row[0] or 0) == 0
        except Exception as e:
            result["error"] = str(e)
            result["is_stale"] = True
        out.append(result)
    return out


def _evaluate_assertion(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    expr: str,
) -> AssertionResult:
    """Evaluate a single assertion expression."""
    table = model.full_name

    # Each builtin below is anchored with \s*$ so a compound expression such as
    # `row_count > 0 AND amount >= 0` is NOT swallowed by the builtin branch
    # (which would evaluate only the first conjunct and silently discard the
    # rest). Anything that isn't exactly one builtin falls through to the
    # generic SQL evaluator at the bottom.

    # row_count > N / row_count >= N / etc.
    # Order alternatives longest-first so `>=` doesn't get split into `>` + `=`.
    m = re.match(r"row_count\s*(>=|<=|==|!=|>|<|=)\s*(\d+)\s*$", expr)
    if m:
        op, val = m.group(1), int(m.group(2))
        if op == "==":
            op = "="
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        check = conn.execute(f"SELECT {count} {op} {val}").fetchone()[0]
        passed = bool(check)
        detail = f"got {count:,} rows, expected {op} {val:,}"
        return AssertionResult(expression=expr, passed=passed, detail=detail)

    # no_nulls(column)
    m = re.match(r"no_nulls\((\w+)\)\s*$", expr)
    if m:
        col = m.group(1)
        row = conn.execute(
            f'SELECT COUNT(*) FILTER (WHERE "{col}" IS NULL), COUNT(*) FROM {table}'
        ).fetchone()
        null_count, total = row[0], row[1]
        passed = null_count == 0
        if passed:
            detail = f"0 nulls in {total:,} rows"
        else:
            pct = round((null_count / total) * 100, 1) if total > 0 else 0
            detail = f"{null_count:,} nulls out of {total:,} rows ({pct}%)"
        return AssertionResult(expression=expr, passed=passed, detail=detail)

    # unique(column)
    m = re.match(r"unique\((\w+)\)\s*$", expr)
    if m:
        col = m.group(1)
        row = conn.execute(
            f'SELECT COUNT(*) - COUNT(DISTINCT "{col}"), COUNT(*), COUNT(DISTINCT "{col}") FROM {table}'
        ).fetchone()
        dup_count, total, distinct = row[0], row[1], row[2]
        passed = dup_count == 0
        if passed:
            detail = f"all {total:,} values unique"
        else:
            detail = f"{dup_count:,} duplicate(s) — {distinct:,} distinct out of {total:,} rows"
        return AssertionResult(expression=expr, passed=passed, detail=detail)

    # accepted_values(column, ['val1', 'val2'])
    m = re.match(r"accepted_values\((\w+),\s*\[(.+)\]\)\s*$", expr)
    if m:
        col = m.group(1)
        raw_values = m.group(2)
        values = [v.strip().strip("'\"") for v in raw_values.split(",")]
        # Escape embedded single quotes -- otherwise a value like O'Brien closes
        # the literal and the rest of it is parsed as SQL.
        placeholders = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
        bad_count = conn.execute(
            f'SELECT COUNT(*) FROM {table} WHERE "{col}" IS NOT NULL AND "{col}"::VARCHAR NOT IN ({placeholders})'
        ).fetchone()[0]
        passed = bad_count == 0
        if passed:
            detail = f"all values in [{', '.join(values)}]"
        else:
            # Fetch sample unexpected values
            sample = conn.execute(
                f'SELECT DISTINCT "{col}"::VARCHAR FROM {table} '
                f'WHERE "{col}" IS NOT NULL AND "{col}"::VARCHAR NOT IN ({placeholders}) LIMIT 5'
            ).fetchall()
            sample_vals = [str(r[0]) for r in sample]
            detail = f"{bad_count:,} row(s) with unexpected values: {', '.join(sample_vals)}"
        return AssertionResult(expression=expr, passed=passed, detail=detail)

    # Generic SQL expression.
    #
    # A row-level predicate (`amount >= 0`) has to hold for EVERY row, so count
    # the rows that violate it. The previous implementation evaluated the
    # expression against a single arbitrary row (`... FROM t LIMIT 1`), which
    # reported "pass" for a table whose very next row violated the assertion.
    #
    # An aggregate predicate (`sum(amount) > 0`, `count(*) = count(DISTINCT id)`)
    # is not valid in a WHERE clause, so DuckDB rejects the counting form; fall
    # back to evaluating it as the single-row aggregate it is.
    #
    # Rows where the predicate is NULL are not counted as violations: an
    # unevaluable predicate is not a demonstrated failure, and treating NULL as a
    # violation would fail every assertion written over a nullable column.
    # `row_count` is a havn-provided pseudo-column, not real SQL. The dedicated
    # branch above handles it alone; here it can still appear inside a compound
    # expression (`row_count > 0 AND amount >= 0`), so substitute the literal
    # count -- unless the model actually has a column by that name, which wins.
    sql_expr = expr
    if re.search(r"\brow_count\b", expr):
        has_col = conn.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? AND lower(column_name) = 'row_count'",
            [model.schema, model.name],
        ).fetchone()[0]
        if not has_col:
            total_rows = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            sql_expr = re.sub(r"\brow_count\b", str(total_rows), expr)

    try:
        bad = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE NOT ({sql_expr})"
        ).fetchone()
        bad_count = bad[0] if bad else 0
        if bad_count == 0:
            return AssertionResult(
                expression=expr, passed=True, detail="holds for all rows"
            )
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return AssertionResult(
            expression=expr,
            passed=False,
            detail=f"{bad_count:,} of {total:,} row(s) violate the expression",
        )
    except duckdb.Error:
        check = conn.execute(
            f"SELECT CASE WHEN ({expr}) THEN true ELSE false END FROM {table}"
        ).fetchone()
        passed = bool(check[0]) if check else False
        detail = "expression evaluated to true" if passed else "expression evaluated to false"
        return AssertionResult(expression=expr, passed=passed, detail=detail)


def profile_model(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
) -> ProfileResult:
    """Compute profile statistics for a model after execution."""
    table = model.full_name

    row_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    cols = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
        [model.schema, model.name],
    ).fetchall()
    column_names = [c[0] for c in cols]

    null_pcts: dict[str, float] = {}
    distinct_counts: dict[str, int] = {}

    if row_count > 0:
        for col_name in column_names:
            qcol = f'"{col_name}"'
            stats = conn.execute(
                f"SELECT COUNT(*) - COUNT({qcol}), COUNT(DISTINCT {qcol}) FROM {table}"
            ).fetchone()
            null_count = stats[0]
            null_pcts[col_name] = round((null_count / row_count) * 100, 1) if row_count > 0 else 0.0
            distinct_counts[col_name] = stats[1]

    return ProfileResult(
        row_count=row_count,
        column_count=len(column_names),
        null_percentages=null_pcts,
        distinct_counts=distinct_counts,
    )


def _save_profile(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    profile: ProfileResult,
) -> None:
    """Save profile stats to the metadata table and append to history.

    Uses DELETE+INSERT for the primary profile row so it works on both
    DuckDB (PK-enforced) and DuckLake (no PK support).
    """
    import json
    null_json = json.dumps(profile.null_percentages)
    distinct_json = json.dumps(profile.distinct_counts)
    conn.execute(
        "DELETE FROM _havn.model_profiles WHERE model_path = ?",
        [model.full_name],
    )
    conn.execute(
        """
        INSERT INTO _havn.model_profiles
            (model_path, row_count, column_count, null_percentages, distinct_counts, profiled_at)
        VALUES (?, ?, ?, ?::JSON, ?::JSON, current_timestamp)
        """,
        [
            model.full_name,
            profile.row_count,
            profile.column_count,
            null_json,
            distinct_json,
        ],
    )
    # Append to profile_history for anomaly detection baselines
    try:
        conn.execute(
            """
            INSERT INTO _havn.profile_history
                (model_path, row_count, column_count, null_percentages, distinct_counts, profiled_at)
            VALUES (?, ?, ?, ?::JSON, ?::JSON, current_timestamp)
            """,
            [
                model.full_name,
                profile.row_count,
                profile.column_count,
                null_json,
                distinct_json,
            ],
        )
    except Exception:
        pass  # profile_history table may not exist in older databases


def _save_assertions(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    results: list[AssertionResult],
) -> None:
    """Save assertion results to the metadata table."""
    for ar in results:
        try:
            conn.execute(
                """
                INSERT INTO _havn.assertion_results
                    (model_path, expression, passed, detail, severity, owner, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, current_timestamp)
                """,
                [model.full_name, ar.expression, ar.passed, ar.detail, ar.severity, ar.owner],
            )
        except Exception:
            # Older databases may not have severity/owner columns yet
            # (ALTER TABLE migration runs in ensure_meta_table). Fall back
            # to the legacy 5-column insert so we don't lose the result.
            conn.execute(
                """
                INSERT INTO _havn.assertion_results
                    (model_path, expression, passed, detail, checked_at)
                VALUES (?, ?, ?, ?, current_timestamp)
                """,
                [model.full_name, ar.expression, ar.passed, ar.detail],
            )


def _save_source_freshness(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    results: list[dict],
) -> None:
    """Persist source-freshness check results."""
    for r in results:
        try:
            conn.execute(
                """
                INSERT INTO _havn.source_freshness
                    (model_path, source_table, on_column, max_age_seconds,
                     age_seconds, is_stale, severity, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    model.full_name,
                    r["table"],
                    r.get("on"),
                    int(r.get("max_age_seconds") or 0),
                    r.get("age_seconds"),
                    bool(r["is_stale"]),
                    r.get("severity", "error"),
                    r.get("error"),
                ],
            )
        except Exception:
            pass
