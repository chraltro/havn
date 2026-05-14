"""End-to-end tests for the 0.2.18 feature roadmap.

Exercises every feature added in the roadmap commit through both the
engine layer and the FastAPI surface. These are integration-shaped:
they construct a temporary project, run havn through it, and assert
on user-visible outcomes (rows on disk, exit codes, API responses).

The test classes are grouped per phase so a single failure points
straight at the affected feature.
"""

from __future__ import annotations

import io
import json
import textwrap
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_macro_registry_between_tests():
    """Clear the per-connection registration cache so ``id(conn)`` reuse
    across tests doesn't make a fresh ``register_macros`` call short-circuit
    into a no-op. Same pattern as ``tests/test_macros.py``.
    """
    from havn.engine.macros import reset_macro_state
    reset_macro_state()
    yield
    reset_macro_state()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def base_project(tmp_path: Path) -> Path:
    """Minimal havn project scaffold with one ingest, one bronze, one gold."""
    p = tmp_path / "project"
    p.mkdir()
    for d in [
        "ingest", "transform/bronze", "transform/silver",
        "transform/gold", "macros", "seeds",
    ]:
        (p / d).mkdir(parents=True, exist_ok=True)

    (p / "project.yml").write_text(textwrap.dedent("""\
        name: roadmap-e2e
        database:
          path: warehouse.duckdb
    """))

    # ``current_timestamp`` keeps the data fresh against any
    # @source_freshness check, regardless of when the test runs.
    (p / "ingest" / "load.py").write_text(textwrap.dedent("""\
        db.execute("CREATE SCHEMA IF NOT EXISTS landing")
        db.execute(\"\"\"
            CREATE OR REPLACE TABLE landing.customers AS
            SELECT * FROM (VALUES
                (1, 'alice@example.com', '+47 99 88 77 66', '29128512345',
                 current_timestamp),
                (2, 'bob@example.com',   '+47 11 22 33 44', '15059087654',
                 current_timestamp)
            ) AS t(id, email, phone, national_id, created_at)
        \"\"\")
    """))
    return p


@pytest.fixture
def project_with_directives(base_project: Path) -> Path:
    """Project that exercises every new model directive."""
    p = base_project

    (p / "transform" / "bronze" / "customers.sql").write_text(textwrap.dedent("""\
        @config materialized=table, schema=bronze
        @description Cleaned customers.
        @grain id
        @owner @data-platform
        @col id: Customer surrogate key
        @col email: Email address (masked in gold)
        @assert row_count > 0, severity=error
        @assert no_nulls(id), severity=warn
        @source_freshness landing.customers, max_age=24h, on=created_at

        SELECT id, email, phone, national_id, created_at
        FROM landing.customers
    """))

    (p / "transform" / "gold" / "customer_summary.sql").write_text(textwrap.dedent("""\
        @config materialized=table, schema=gold
        @grain id
        @owner @analytics-team

        SELECT id,
               mask_email(email) AS email_masked,
               mask_phone(phone) AS phone_masked,
               mask_fnr(national_id) AS fnr_masked
        FROM bronze.customers
    """))
    return p


@pytest.fixture
def fresh_warehouse(project_with_directives: Path) -> Path:
    """Project + already-built warehouse."""
    from havn.config import load_project
    from havn.engine.database import open_warehouse
    from havn.engine.runner import run_scripts_in_dir
    from havn.engine.transform import run_transform

    config = load_project(project_with_directives)
    conn = open_warehouse(config, project_with_directives)
    try:
        run_scripts_in_dir(conn, project_with_directives / "ingest", "ingest")
        run_transform(conn, project_with_directives / "transform", force=True)
    finally:
        conn.close()
    return project_with_directives


# ---------------------------------------------------------------------------
# Hardening — parsers and edge cases
# ---------------------------------------------------------------------------


class TestParserHardening:
    """Malformed directive inputs should never crash the discovery pass."""

    def test_grain_handles_empty_and_extra_whitespace(self):
        from havn.engine.sql_analysis import parse_grain

        assert parse_grain("@grain") == []
        assert parse_grain("@grain   ") == []
        assert parse_grain("@grain   id  ,  name  ") == ["id", "name"]
        assert parse_grain("@grain id, , , name") == ["id", "name"]
        # Parenthesised form
        assert parse_grain("@grain(id, name)") == ["id", "name"]

    def test_owner_handles_empty(self):
        from havn.engine.sql_analysis import parse_owner

        assert parse_owner("@owner") == ""
        assert parse_owner("@owner   ") == ""
        assert parse_owner("@owner @data-team") == "@data-team"

    def test_source_freshness_invalid_duration_falls_back(self, caplog):
        """A typo in ``max_age=`` should warn-and-default, not crash."""
        import logging

        from havn.engine.sql_analysis import parse_source_freshness

        with caplog.at_level(logging.WARNING, logger="havn.sql_analysis"):
            specs = parse_source_freshness(
                "@source_freshness landing.t, max_age=invalid, on=ts"
            )

        assert len(specs) == 1
        assert specs[0]["max_age_seconds"] == 86400  # 24h fallback
        assert any("Unrecognized duration" in r.message for r in caplog.records)

    def test_severity_unknown_value_strips_qualifier(self, caplog):
        """``severity=critical`` (not in warn/error) should be stripped, not parsed as SQL."""
        import logging

        from havn.engine.sql_analysis import parse_assertion_specs

        with caplog.at_level(logging.WARNING, logger="havn.sql_analysis"):
            specs = parse_assertion_specs("@assert row_count > 0, severity=critical")

        assert len(specs) == 1
        # The qualifier was stripped — not left in the expression where it
        # would otherwise crash _evaluate_assertion as bad SQL.
        assert specs[0][0] == "row_count > 0"
        assert specs[0][1] == "error"  # default

    def test_severity_empty_value_strips_qualifier(self):
        from havn.engine.sql_analysis import parse_assertion_specs

        specs = parse_assertion_specs("@assert row_count > 0, severity=")
        assert specs[0][0] == "row_count > 0"
        assert specs[0][1] == "error"

    def test_duration_units(self):
        from havn.engine.sql_analysis import _parse_duration

        assert _parse_duration("30s") == 30
        assert _parse_duration("5m") == 300
        assert _parse_duration("2h") == 7200
        assert _parse_duration("3d") == 259200
        assert _parse_duration("60") == 60  # bare integer = seconds
        # Unknown unit falls back, doesn't crash
        assert _parse_duration("5x") == 86400
        assert _parse_duration("") == 86400


class TestShellStatementParser:
    """The shell's multi-line statement detector handles SQL string/comment edges."""

    def test_complete_statements(self):
        from havn.cli.shell import _statement_complete

        assert _statement_complete("SELECT 1;")
        assert _statement_complete("SELECT 1; -- trailing comment")
        assert _statement_complete("SELECT * FROM t WHERE x = 'a;b';")
        assert _statement_complete('SELECT * FROM t WHERE x = "col;name";')
        assert _statement_complete("SELECT 1;\n  ")  # trailing whitespace ok

    def test_incomplete_statements(self):
        from havn.cli.shell import _statement_complete

        assert not _statement_complete("SELECT 1")
        assert not _statement_complete("SELECT 'unterminated")
        assert not _statement_complete('SELECT "unterminated')
        # ``;`` inside a comment doesn't count
        assert not _statement_complete("SELECT 1 -- ; comment")
        # ``;`` inside an unterminated string doesn't count
        assert not _statement_complete("SELECT 'a;")


# ---------------------------------------------------------------------------
# Phase 2 — Directives
# ---------------------------------------------------------------------------


class TestDirectives:
    """End-to-end exercise of @grain, @owner, @assert severity, @source_freshness, @watermark."""

    def test_grain_synthesises_assertion(self, fresh_warehouse: Path):
        """A model with @grain id should auto-add a uniqueness assertion."""
        from havn.engine.database import open_warehouse
        from havn.config import load_project

        conn = open_warehouse(load_project(fresh_warehouse), fresh_warehouse)
        try:
            rows = conn.execute(
                "SELECT expression, passed, severity, owner FROM _havn.assertion_results "
                "WHERE model_path = 'bronze.customers' AND expression LIKE 'grain%'"
            ).fetchall()
            assert len(rows) == 1
            expr, passed, severity, owner = rows[0]
            assert "grain(id)" in expr
            assert passed is True
            # Auto-grain assertions inherit the model's @owner so alerts can route.
            assert owner == "@data-platform"
            assert severity == "error"
        finally:
            conn.close()

    def test_grain_violation_blocks_downstream(self, base_project: Path):
        """If grain is violated, downstream models should be skipped."""
        from havn.config import load_project
        from havn.engine.database import open_warehouse
        from havn.engine.runner import run_scripts_in_dir
        from havn.engine.transform import run_transform

        # Bronze with a grain violation: two rows, same id.
        (base_project / "ingest" / "load.py").write_text(textwrap.dedent("""\
            db.execute("CREATE SCHEMA IF NOT EXISTS landing")
            db.execute(\"\"\"
                CREATE OR REPLACE TABLE landing.dupes AS
                SELECT * FROM (VALUES (1, 'a'), (1, 'b')) AS t(id, val)
            \"\"\")
        """))
        (base_project / "transform" / "bronze" / "dupes.sql").write_text(textwrap.dedent("""\
            @config materialized=table, schema=bronze
            @grain id

            SELECT id, val FROM landing.dupes
        """))
        (base_project / "transform" / "silver" / "downstream.sql").write_text(textwrap.dedent("""\
            @config materialized=table, schema=silver

            SELECT * FROM bronze.dupes
        """))

        config = load_project(base_project)
        conn = open_warehouse(config, base_project)
        try:
            run_scripts_in_dir(conn, base_project / "ingest", "ingest")
            results = run_transform(conn, base_project / "transform", force=True, parallel=False)
        finally:
            conn.close()

        # Bronze built but assertion failed; silver skipped because upstream blocked.
        assert results.get("bronze.dupes") == "assertion_failed"
        assert results.get("silver.downstream") == "skipped_upstream_blocked"

    def test_severity_warn_does_not_block(self, base_project: Path):
        """severity=warn should log the failure but let downstream run."""
        from havn.config import load_project
        from havn.engine.database import open_warehouse
        from havn.engine.runner import run_scripts_in_dir
        from havn.engine.transform import run_transform

        (base_project / "ingest" / "load.py").write_text(textwrap.dedent("""\
            db.execute("CREATE SCHEMA IF NOT EXISTS landing")
            db.execute("CREATE OR REPLACE TABLE landing.t AS SELECT 1 AS id, NULL AS optional_col")
        """))
        (base_project / "transform" / "bronze" / "warn_only.sql").write_text(textwrap.dedent("""\
            @config materialized=table, schema=bronze
            @assert no_nulls(optional_col), severity=warn

            SELECT id, optional_col FROM landing.t
        """))
        (base_project / "transform" / "silver" / "after_warn.sql").write_text(textwrap.dedent("""\
            @config materialized=table, schema=silver

            SELECT * FROM bronze.warn_only
        """))

        config = load_project(base_project)
        conn = open_warehouse(config, base_project)
        try:
            run_scripts_in_dir(conn, base_project / "ingest", "ingest")
            results = run_transform(conn, base_project / "transform", force=True, parallel=False)
        finally:
            conn.close()

        # Warn-severity assertion failed but pipeline continued.
        assert results.get("bronze.warn_only") == "built"
        assert results.get("silver.after_warn") == "built"

    def test_source_freshness_blocks_when_stale(self, base_project: Path):
        """An impossibly tight max_age should mark every source stale."""
        from havn.config import load_project
        from havn.engine.database import open_warehouse
        from havn.engine.runner import run_scripts_in_dir
        from havn.engine.transform import run_transform

        (base_project / "ingest" / "load.py").write_text(textwrap.dedent("""\
            db.execute("CREATE SCHEMA IF NOT EXISTS landing")
            db.execute(\"\"\"
                CREATE OR REPLACE TABLE landing.events AS
                SELECT * FROM (VALUES (1, CAST('2020-01-01' AS TIMESTAMP))) AS t(id, ts)
            \"\"\")
        """))
        (base_project / "transform" / "bronze" / "stale_check.sql").write_text(textwrap.dedent("""\
            @config materialized=table, schema=bronze
            @source_freshness landing.events, max_age=1s, on=ts

            SELECT * FROM landing.events
        """))

        config = load_project(base_project)
        conn = open_warehouse(config, base_project)
        try:
            run_scripts_in_dir(conn, base_project / "ingest", "ingest")
            results = run_transform(conn, base_project / "transform", force=True, parallel=False)

            # Source-freshness check persisted to metadata
            sf_rows = conn.execute(
                "SELECT source_table, is_stale, severity FROM _havn.source_freshness "
                "WHERE model_path = 'bronze.stale_check'"
            ).fetchall()
        finally:
            conn.close()

        assert results.get("bronze.stale_check") == "source_stale"
        assert len(sf_rows) == 1
        assert sf_rows[0][0] == "landing.events"
        assert sf_rows[0][1] is True
        assert sf_rows[0][2] == "error"

    def test_source_freshness_severity_warn_continues(self, base_project: Path):
        """severity=warn should log the staleness but build the model."""
        from havn.config import load_project
        from havn.engine.database import open_warehouse
        from havn.engine.runner import run_scripts_in_dir
        from havn.engine.transform import run_transform

        (base_project / "ingest" / "load.py").write_text(textwrap.dedent("""\
            db.execute("CREATE SCHEMA IF NOT EXISTS landing")
            db.execute(\"\"\"
                CREATE OR REPLACE TABLE landing.warn_events AS
                SELECT 1 AS id, CAST('2020-01-01' AS TIMESTAMP) AS ts
            \"\"\")
        """))
        (base_project / "transform" / "bronze" / "warn_freshness.sql").write_text(textwrap.dedent("""\
            @config materialized=table, schema=bronze
            @source_freshness landing.warn_events, max_age=1s, on=ts, severity=warn

            SELECT * FROM landing.warn_events
        """))

        config = load_project(base_project)
        conn = open_warehouse(config, base_project)
        try:
            run_scripts_in_dir(conn, base_project / "ingest", "ingest")
            results = run_transform(conn, base_project / "transform", force=True, parallel=False)
        finally:
            conn.close()

        assert results.get("bronze.warn_freshness") == "built"

    def test_watermark_synthesises_incremental_filter(self, base_project: Path):
        """@watermark should generate an incremental WHERE clause on subsequent runs."""
        from havn.config import load_project
        from havn.engine.database import open_warehouse
        from havn.engine.runner import run_scripts_in_dir
        from havn.engine.transform import run_transform

        (base_project / "ingest" / "load.py").write_text(textwrap.dedent("""\
            db.execute("CREATE SCHEMA IF NOT EXISTS landing")
            db.execute(\"\"\"
                CREATE OR REPLACE TABLE landing.events AS
                SELECT * FROM (VALUES
                    (1, CAST('2024-01-01' AS DATE)),
                    (2, CAST('2024-01-02' AS DATE))
                ) AS t(id, event_date)
            \"\"\")
        """))
        (base_project / "transform" / "silver" / "events_inc.sql").write_text(textwrap.dedent("""\
            @config materialized=incremental, unique_key=id, watermark=event_date

            SELECT id, event_date FROM landing.events
        """))

        config = load_project(base_project)
        conn = open_warehouse(config, base_project)
        try:
            run_scripts_in_dir(conn, base_project / "ingest", "ingest")
            run_transform(conn, base_project / "transform", force=True, parallel=False)

            # Initial load — both rows present.
            cnt = conn.execute("SELECT COUNT(*) FROM silver.events_inc").fetchone()[0]
            assert cnt == 2

            # Add a newer row + an older row that should be filtered out.
            conn.execute(
                "INSERT INTO landing.events VALUES "
                "(3, CAST('2024-01-03' AS DATE)), "  # newer than max — picked up
                "(4, CAST('2023-12-01' AS DATE))"     # older than max — dropped
            )
            run_transform(conn, base_project / "transform", force=True, parallel=False)

            after = conn.execute(
                "SELECT id FROM silver.events_inc ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

        # 1 + 2 from first run, 3 newly added past watermark, 4 filtered by sugar.
        assert [r[0] for r in after] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Phase 3 — Freshness with sources
# ---------------------------------------------------------------------------


class TestFreshness:
    def test_check_freshness_rolls_up_source_staleness(self, fresh_warehouse: Path):
        """check_freshness(include_sources=True) should join source row counts."""
        from havn.config import load_project
        from havn.engine.database import open_warehouse
        from havn.engine.transform import check_freshness

        conn = open_warehouse(load_project(fresh_warehouse), fresh_warehouse)
        try:
            results = check_freshness(
                conn,
                max_age_hours=99999,  # everything fresh by time
                include_sources=True,
                transform_dir=fresh_warehouse / "transform",
            )
        finally:
            conn.close()

        bronze = next((r for r in results if r["model"] == "bronze.customers"), None)
        assert bronze is not None
        assert "sources" in bronze
        assert any(s["table"] == "landing.customers" for s in bronze["sources"])
        landing_src = next(s for s in bronze["sources"] if s["table"] == "landing.customers")
        assert landing_src["row_count"] == 2

    def test_source_min_rows_flips_to_stale(self, fresh_warehouse: Path):
        """source_min_rows=5 against a 2-row source should flip is_stale."""
        from havn.config import load_project
        from havn.engine.database import open_warehouse
        from havn.engine.transform import check_freshness

        conn = open_warehouse(load_project(fresh_warehouse), fresh_warehouse)
        try:
            results = check_freshness(
                conn,
                max_age_hours=99999,
                include_sources=True,
                source_min_rows=5,
                transform_dir=fresh_warehouse / "transform",
            )
        finally:
            conn.close()

        bronze = next(r for r in results if r["model"] == "bronze.customers")
        assert bronze["is_stale"] is True
        landing_src = next(s for s in bronze["sources"] if s["table"] == "landing.customers")
        assert landing_src["is_stale"] is True


# ---------------------------------------------------------------------------
# Phase 4 — PII stdlib + deny-list
# ---------------------------------------------------------------------------


class TestStdlibAndPolicies:
    def test_stdlib_pii_macros_callable_without_user_macros(self, base_project: Path):
        """A project with no macros/ files should still expose mask_email etc."""
        from havn.config import load_project
        from havn.engine.database import open_warehouse

        # Remove the macros dir entirely — stdlib should still work.
        macros_dir = base_project / "macros"
        for child in macros_dir.iterdir():
            child.unlink()
        macros_dir.rmdir()

        conn = open_warehouse(load_project(base_project), base_project)
        try:
            row = conn.execute(
                "SELECT mask_email('alice@example.com'), "
                "mask_phone('+47 99 88 77 66'), "
                "mask_fnr('29128512345'), "
                "mask_credit_card('4242 4242 4242 4242'), "
                "mask_ip('192.168.1.42'), "
                "hash_consistent('alice', 'havn')"
            ).fetchone()
        finally:
            conn.close()

        email, phone, fnr, cc, ip, h = row
        assert email == "***@example.com"
        # Last four digits are preserved; everything before is masked.
        assert phone.endswith("77 66")
        assert "*" in phone
        # ``mask_fnr`` keeps the date-of-birth prefix (first 4 chars).
        assert fnr.startswith("2912") and fnr.endswith("*******")
        assert cc.endswith("4242")
        assert "*" in cc
        assert ip == "192.168.1.0"
        # ``hash_consistent`` truncates to 16 hex chars.
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_stdlib_handles_null_inputs(self, base_project: Path):
        """All masking macros pass NULL through unchanged."""
        from havn.config import load_project
        from havn.engine.database import open_warehouse

        conn = open_warehouse(load_project(base_project), base_project)
        try:
            row = conn.execute(
                "SELECT mask_email(NULL), mask_phone(NULL), mask_fnr(NULL), "
                "mask_credit_card(NULL), mask_ip(NULL)"
            ).fetchone()
        finally:
            conn.close()

        # All five should be NULL on NULL input — no crashes, no '***' coercion.
        assert all(v is None for v in row)

    def test_user_macro_overrides_stdlib(self, base_project: Path, caplog):
        """User-defined ``mask_email`` should win over the stdlib version."""
        import logging

        (base_project / "macros" / "override.py").write_text(textwrap.dedent("""\
            from havn.engine.macros import macro

            @macro
            def mask_email(s: str) -> str:
                return 'OVERRIDDEN'
        """))

        from havn.config import load_project
        from havn.engine.database import open_warehouse
        from havn.engine.macros import reset_macro_state

        reset_macro_state()
        with caplog.at_level(logging.WARNING, logger="havn.macros"):
            conn = open_warehouse(load_project(base_project), base_project)
            try:
                result = conn.execute("SELECT mask_email('alice@x.com')").fetchone()[0]
            finally:
                conn.close()

        assert result == "OVERRIDDEN"
        # Shadowing should be logged so users can debug it.
        assert any("shadows havn.stdlib macro" in r.message for r in caplog.records)

    def test_deny_list_blocks_at_check_time(self, base_project: Path):
        """A deny rule should fire before any model is built."""
        # Append the policy to project.yml
        (base_project / "project.yml").write_text(textwrap.dedent("""\
            name: deny-test
            database:
              path: warehouse.duckdb
            policies:
              deny:
                - column: national_id
                  forbid_in_schemas: [gold]
                  reason: "PII may not surface in gold"
        """))
        (base_project / "transform" / "gold" / "leak.sql").write_text(textwrap.dedent("""\
            @config materialized=table, schema=gold

            SELECT national_id FROM landing.customers
        """))

        from havn.config import load_project
        from havn.engine.transform import discover_models, validate_models

        config = load_project(base_project)
        models = discover_models(base_project / "transform")
        errors = validate_models(
            None, models,
            deny_rules=list(config.policies.deny),
        )
        leak_errors = [e for e in errors if e.model == "gold.leak"]
        assert leak_errors, "deny rule did not fire on gold.leak"
        assert any("national_id" in e.message for e in leak_errors)
        assert any("PII may not surface" in e.message for e in leak_errors)

    def test_deny_list_does_not_fire_outside_forbidden_schemas(self, base_project: Path):
        """A bronze model touching national_id is fine when only gold is denied."""
        (base_project / "project.yml").write_text(textwrap.dedent("""\
            name: deny-test
            database:
              path: warehouse.duckdb
            policies:
              deny:
                - column: national_id
                  forbid_in_schemas: [gold]
        """))
        (base_project / "transform" / "bronze" / "okay.sql").write_text(textwrap.dedent("""\
            @config materialized=table, schema=bronze

            SELECT national_id FROM landing.customers
        """))

        from havn.config import load_project
        from havn.engine.transform import discover_models, validate_models

        config = load_project(base_project)
        models = discover_models(base_project / "transform")
        errors = validate_models(None, models, deny_rules=list(config.policies.deny))
        # No errors against bronze.okay specifically.
        assert not any(e.model == "bronze.okay" and "Policy" in e.message for e in errors)


# ---------------------------------------------------------------------------
# Phase 5 — Watch + UI surface
# ---------------------------------------------------------------------------


class TestWatchRoute:
    def test_glob_match_logic(self):
        """The route-glob matcher used by FileWatcher behaves like fnmatch."""
        # Hot-test the same matching logic the watcher uses without spinning
        # up the Observer (which would be flaky in CI).
        import fnmatch
        rels = [
            ("transform/gold/route_b_cfo_monthly.sql", True),
            ("transform/gold/route_a_alerts.sql", False),
            ("transform/silver/dim_customers.sql", False),
        ]
        glob = "transform/gold/route_b_*.sql"
        for path, expected in rels:
            assert fnmatch.fnmatchcase(path, glob) is expected


# ---------------------------------------------------------------------------
# CLI surface — Phase 1
# ---------------------------------------------------------------------------


class TestCLI:
    """Smoke tests for the new CLI commands using Typer's CliRunner."""

    def _runner(self):
        from typer.testing import CliRunner
        return CliRunner(mix_stderr=False) if hasattr(CliRunner.__init__, "__defaults__") and "mix_stderr" in (CliRunner.__init__.__code__.co_varnames or ()) else CliRunner()

    def test_explain_command_renders_plan(self, fresh_warehouse: Path):
        from havn.cli import app
        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(app, [
            "explain", "bronze.customers",
            "--json",
            "--project", str(fresh_warehouse),
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        # JSON plan has at minimum an operator string.
        assert "operator" in payload

    def test_diff_exit_nonzero_on_change(self, fresh_warehouse: Path):
        """After mutating a bronze model, diff --exit-nonzero-on-change should exit 2."""
        from havn.cli import app
        from typer.testing import CliRunner

        # First, no changes — exit 0.
        runner = CliRunner()
        result = runner.invoke(app, [
            "diff",
            "--exit-nonzero-on-change",
            "--format", "json",
            "--project", str(fresh_warehouse),
        ])
        assert result.exit_code == 0, result.output

        # Mutate the bronze model so its SQL output diverges from the
        # materialized table — append a WHERE that drops one of the two rows.
        bronze = fresh_warehouse / "transform" / "bronze" / "customers.sql"
        original = bronze.read_text()
        bronze.write_text(original + "\nWHERE id = 1\n")

        result = runner.invoke(app, [
            "diff",
            "--exit-nonzero-on-change",
            "--format", "json",
            "--project", str(fresh_warehouse),
        ])
        # Exit code 2 is the contract: distinct from engine errors (1).
        assert result.exit_code == 2, result.output
        # JSON should still be parseable on a non-zero exit.
        # Typer's CliRunner captures stdout; the JSON is the only thing on it.
        body = json.loads(result.stdout)
        bronze_diff = next(d for d in body if d["model"] == "bronze.customers")
        # The materialized table has 2 rows; the new SQL produces 1.
        assert bronze_diff["removed"] == 1 or bronze_diff["modified"] >= 1

    def test_freshness_sources_flag(self, fresh_warehouse: Path):
        from havn.cli import app
        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(app, [
            "freshness", "--sources", "--hours", "99999",
            "--project", str(fresh_warehouse),
        ])
        assert result.exit_code == 0, result.output
        # Output should include a Sources column heading.
        assert "Sources" in result.output

    def test_init_seeds_sqlfluff(self, tmp_path: Path):
        from havn.cli import app
        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(app, [
            "init", "demo",
            "--dir", str(tmp_path / "demo"),
            "--empty",
        ])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "demo" / ".sqlfluff").exists()
        content = (tmp_path / "demo" / ".sqlfluff").read_text()
        assert "exclude_rules" in content
        assert "RF03" in content


# ---------------------------------------------------------------------------
# API surface — Phase 5 (structured docs, deny-list, freshness)
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client(fresh_warehouse: Path):
    """FastAPI TestClient bound to the warm warehouse."""
    import havn.server.app as server_app
    from havn.server.deps import reset_shared_conn

    reset_shared_conn()
    server_app.PROJECT_DIR = fresh_warehouse
    server_app.AUTH_ENABLED = False
    server_app.ACTIVE_ENV = None
    yield TestClient(server_app.app)
    reset_shared_conn()


class TestAPI:
    def test_structured_docs_surfaces_grain_owner(self, api_client: TestClient):
        """/api/docs/structured should include grain and owner per model."""
        resp = api_client.get("/api/docs/structured")
        assert resp.status_code == 200
        body = resp.json()
        # Find bronze.customers in the schemas tree.
        bronze = None
        for schema in body.get("schemas", []):
            if schema.get("name") == "bronze":
                for t in schema.get("tables", []):
                    if t.get("name") == "customers":
                        bronze = t
                        break
        assert bronze is not None, body
        assert bronze.get("grain") == ["id"]
        assert bronze.get("owner") == "@data-platform"
        # Column-level descriptions came from @col directives.
        col_descriptions = {c["name"]: c.get("description", "") for c in bronze.get("columns", [])}
        assert "Customer surrogate key" in col_descriptions.get("id", "")

    def test_diff_endpoint_returns_json(self, api_client: TestClient):
        """The /api/diff endpoint should return list[DiffResultDict]."""
        resp = api_client.post("/api/diff", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        # Each entry exposes the dataclass fields used by the CLI.
        for entry in body:
            assert "model" in entry
            assert "added" in entry
            assert "removed" in entry
            assert "modified" in entry
            assert "schema_changes" in entry

    def test_query_endpoint_runs_stdlib_macros(self, api_client: TestClient):
        """The HTTP query path should expose stdlib PII macros."""
        resp = api_client.post(
            "/api/query",
            json={"sql": "SELECT mask_email('alice@example.com') AS m"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["rows"][0][0] == "***@example.com"


# ---------------------------------------------------------------------------
# Snapshot — Phase 3 (already shipped; verifying it still works)
# ---------------------------------------------------------------------------


class TestSnapshotRegression:
    def test_snapshot_create_and_diff(self, fresh_warehouse: Path):
        from havn.cli import app
        from typer.testing import CliRunner

        runner = CliRunner()

        # Create a labelled snapshot
        result = runner.invoke(app, [
            "snapshot", "create", "before-edit",
            "--project", str(fresh_warehouse),
        ])
        assert result.exit_code == 0, result.output

        # Mutate gold model so diff has something to find
        gold = fresh_warehouse / "transform" / "gold" / "customer_summary.sql"
        gold.write_text(gold.read_text() + "\nWHERE id = 1\n")

        # diff against snapshot should succeed (output is human-rendered)
        result = runner.invoke(app, [
            "diff", "--snapshot", "before-edit",
            "--project", str(fresh_warehouse),
        ])
        assert result.exit_code == 0, result.output
