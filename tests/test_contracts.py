"""Tests for Data Contracts & Assertions Framework."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest


class TestContracts:
    """Test the contracts engine."""

    def _setup(self, tmp_path: Path):
        """Create a project with a warehouse and contracts."""
        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        from havn.engine.database import ensure_meta_table
        ensure_meta_table(conn)

        conn.execute("CREATE SCHEMA IF NOT EXISTS gold")
        conn.execute(
            "CREATE TABLE gold.orders AS "
            "SELECT 1 AS order_id, 'pending' AS status, 100.0 AS amount "
            "UNION ALL SELECT 2, 'shipped', 200.0 "
            "UNION ALL SELECT 3, 'delivered', 150.0"
        )

        contracts_dir = tmp_path / "contracts"
        contracts_dir.mkdir()
        (contracts_dir / "orders.yml").write_text(
            "contracts:\n"
            "  - name: orders_valid\n"
            "    model: gold.orders\n"
            "    description: Orders must be valid\n"
            "    assertions:\n"
            "      - row_count > 0\n"
            "      - unique(order_id)\n"
            "      - no_nulls(order_id)\n"
            "      - \"accepted_values(status, ['pending', 'shipped', 'delivered'])\"\n"
        )

        return conn, contracts_dir

    def test_discover_contracts(self, tmp_path: Path):
        from havn.engine.contracts import discover_contracts

        _, contracts_dir = self._setup(tmp_path)
        contracts = discover_contracts(contracts_dir)
        assert len(contracts) == 1
        assert contracts[0].name == "orders_valid"
        assert contracts[0].model == "gold.orders"
        assert len(contracts[0].assertions) == 4

    def test_discover_contracts_with_notify_and_escalation(self, tmp_path: Path):
        """Test that notify and escalate_after fields are parsed from YAML."""
        from havn.engine.contracts import discover_contracts

        contracts_dir = tmp_path / "contracts"
        contracts_dir.mkdir()
        (contracts_dir / "alert_contract.yml").write_text(
            "contracts:\n"
            "  - name: alerting_contract\n"
            "    model: gold.orders\n"
            "    severity: warn\n"
            "    notify: [slack, log]\n"
            "    escalate_after: 3\n"
            "    assertions:\n"
            "      - row_count > 0\n"
        )

        contracts = discover_contracts(contracts_dir)
        assert len(contracts) == 1
        assert contracts[0].notify == ["slack", "log"]
        assert contracts[0].escalate_after == 3
        assert contracts[0].severity == "warn"

    def test_run_contracts_all_pass(self, tmp_path: Path):
        from havn.engine.contracts import run_contracts

        conn, contracts_dir = self._setup(tmp_path)
        results = run_contracts(conn, contracts_dir)
        assert len(results) == 1
        assert results[0].passed is True
        assert all(r["passed"] for r in results[0].results)
        conn.close()

    def test_run_contracts_with_failure(self, tmp_path: Path):
        from havn.engine.contracts import run_contracts

        conn, contracts_dir = self._setup(tmp_path)

        # Add a contract that will fail
        (contracts_dir / "impossible.yml").write_text(
            "contracts:\n"
            "  - name: impossible\n"
            "    model: gold.orders\n"
            "    severity: warn\n"
            "    assertions:\n"
            "      - row_count > 1000\n"
        )

        results = run_contracts(conn, contracts_dir)
        assert len(results) == 2

        impossible = [r for r in results if r.contract_name == "impossible"][0]
        assert impossible.passed is False
        assert impossible.severity == "warn"
        conn.close()

    def test_contract_missing_table(self, tmp_path: Path):
        from havn.engine.contracts import evaluate_contract, Contract

        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        from havn.engine.database import ensure_meta_table
        ensure_meta_table(conn)

        contract = Contract(
            name="missing",
            model="gold.nonexistent",
            assertions=["row_count > 0"],
        )

        result = evaluate_contract(conn, contract)
        assert result.passed is False
        assert "does not exist" in result.error
        conn.close()

    def test_contract_history(self, tmp_path: Path):
        from havn.engine.contracts import get_contract_history, run_contracts

        conn, contracts_dir = self._setup(tmp_path)
        run_contracts(conn, contracts_dir)

        history = get_contract_history(conn, limit=10)
        assert len(history) == 1
        assert history[0]["contract_name"] == "orders_valid"
        assert history[0]["passed"] is True
        conn.close()


# ---------------------------------------------------------------------------
# Previous Baseline Tests
# ---------------------------------------------------------------------------


class TestPreviousBaseline:
    """Test {previous} placeholder resolution."""

    def _setup_with_profile(self, tmp_path: Path):
        """Create a warehouse with model profile data for {previous} tests."""
        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        from havn.engine.database import ensure_meta_table
        from havn.engine.contracts import _ensure_contracts_table
        ensure_meta_table(conn)
        _ensure_contracts_table(conn)

        conn.execute("CREATE SCHEMA IF NOT EXISTS gold")
        conn.execute(
            "CREATE TABLE gold.orders AS "
            "SELECT * FROM (VALUES "
            "(1, 'pending', 100.0), (2, 'shipped', 200.0), (3, 'delivered', 150.0)"
            ") AS t(order_id, status, amount)"
        )

        # Insert a previous profile with row_count=1000
        conn.execute(
            """
            INSERT OR REPLACE INTO _havn.model_profiles
                (model_path, row_count, column_count, null_percentages, distinct_counts, profiled_at)
            VALUES ('gold.orders', 1000, 3, '{}'::JSON, '{}'::JSON, current_timestamp)
            """
        )

        return conn

    def test_previous_resolution_basic(self, tmp_path: Path):
        """Test {previous} resolves to previous row_count."""
        from havn.engine.contracts import _resolve_previous

        conn = self._setup_with_profile(tmp_path)
        resolved, warning = _resolve_previous(conn, "gold.orders", "row_count > {previous}")
        assert warning is None
        assert resolved == "row_count > 1000"
        conn.close()

    def test_previous_multiplication(self, tmp_path: Path):
        """Test {previous * 0.9} arithmetic."""
        from havn.engine.contracts import _resolve_previous

        conn = self._setup_with_profile(tmp_path)
        resolved, warning = _resolve_previous(conn, "gold.orders", "row_count > {previous * 0.9}")
        assert warning is None
        assert resolved == "row_count > 900"
        conn.close()

    def test_previous_addition(self, tmp_path: Path):
        """Test {previous + 100} arithmetic."""
        from havn.engine.contracts import _resolve_previous

        conn = self._setup_with_profile(tmp_path)
        resolved, warning = _resolve_previous(conn, "gold.orders", "row_count > {previous + 100}")
        assert warning is None
        assert resolved == "row_count > 1100"
        conn.close()

    def test_previous_subtraction(self, tmp_path: Path):
        """Test {previous - 100} arithmetic."""
        from havn.engine.contracts import _resolve_previous

        conn = self._setup_with_profile(tmp_path)
        resolved, warning = _resolve_previous(conn, "gold.orders", "row_count > {previous - 100}")
        assert warning is None
        assert resolved == "row_count > 900"
        conn.close()

    def test_previous_no_baseline(self, tmp_path: Path):
        """Test that {previous} with no profile returns None with warning."""
        from havn.engine.contracts import _resolve_previous

        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        from havn.engine.database import ensure_meta_table
        ensure_meta_table(conn)

        resolved, warning = _resolve_previous(conn, "gold.nonexistent", "row_count > {previous}")
        assert resolved is None
        assert "No baseline" in warning
        conn.close()

    def test_previous_in_contract_evaluation(self, tmp_path: Path):
        """Test {previous} in a full contract evaluation — passes when within threshold."""
        from havn.engine.contracts import evaluate_contract, Contract, _ensure_contracts_table

        conn = self._setup_with_profile(tmp_path)
        _ensure_contracts_table(conn)

        # gold.orders has 3 rows, previous profile says 1000
        # row_count > {previous * 0.9} = row_count > 900 → 3 > 900 = False
        contract = Contract(
            name="previous_test",
            model="gold.orders",
            assertions=["row_count > {previous * 0.9}"],
        )
        result = evaluate_contract(conn, contract)
        assert result.passed is False
        # Check that the detail includes resolution context
        failed_assertions = [r for r in result.results if not r["passed"]]
        assert len(failed_assertions) == 1
        assert "resolved from" in failed_assertions[0]["detail"]
        conn.close()

    def test_previous_no_baseline_skips(self, tmp_path: Path):
        """Test that missing baseline skips assertion without failing."""
        from havn.engine.contracts import evaluate_contract, Contract, _ensure_contracts_table

        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        from havn.engine.database import ensure_meta_table
        ensure_meta_table(conn)
        _ensure_contracts_table(conn)

        conn.execute("CREATE SCHEMA IF NOT EXISTS gold")
        conn.execute("CREATE TABLE gold.orders AS SELECT 1 AS id")

        contract = Contract(
            name="no_baseline",
            model="gold.orders",
            assertions=["row_count > {previous * 0.9}"],
        )
        result = evaluate_contract(conn, contract)
        # Should pass (skipped) since there's no baseline
        assert result.passed is True
        assert result.results[0].get("skipped") is True
        assert "No baseline" in result.results[0]["detail"]
        conn.close()


# ---------------------------------------------------------------------------
# Freshness Assertion Tests
# ---------------------------------------------------------------------------


class TestFreshnessAssertions:
    """Test freshness assertion evaluation."""

    def _setup_with_model_state(self, tmp_path: Path, hours_ago: float = 1.0):
        """Create a warehouse with model_state for freshness tests."""
        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        from havn.engine.database import ensure_meta_table
        from havn.engine.contracts import _ensure_contracts_table
        ensure_meta_table(conn)
        _ensure_contracts_table(conn)

        conn.execute("CREATE SCHEMA IF NOT EXISTS gold")
        conn.execute("CREATE TABLE gold.orders AS SELECT 1 AS id")

        # Set last_run_at to N hours ago
        conn.execute(
            f"""
            INSERT OR REPLACE INTO _havn.model_state
                (model_path, content_hash, upstream_hash, materialized_as, last_run_at, row_count)
            VALUES ('gold.orders', 'abc', 'def', 'table',
                    current_timestamp - INTERVAL '{int(hours_ago)} hours', 1)
            """
        )

        return conn

    def test_freshness_passes(self, tmp_path: Path):
        """Test freshness < 24h passes when model is fresh."""
        from havn.engine.contracts import _evaluate_freshness

        conn = self._setup_with_model_state(tmp_path, hours_ago=1)
        passed, detail = _evaluate_freshness(conn, "gold.orders", "freshness < 24h")
        assert passed is True
        assert "within threshold" in detail
        conn.close()

    def test_freshness_fails(self, tmp_path: Path):
        """Test freshness < 6h fails when model is stale."""
        from havn.engine.contracts import _evaluate_freshness

        conn = self._setup_with_model_state(tmp_path, hours_ago=12)
        passed, detail = _evaluate_freshness(conn, "gold.orders", "freshness < 6h")
        assert passed is False
        assert "threshold is 6h" in detail
        conn.close()

    def test_freshness_days_unit(self, tmp_path: Path):
        """Test freshness with days unit."""
        from havn.engine.contracts import _evaluate_freshness

        conn = self._setup_with_model_state(tmp_path, hours_ago=12)
        passed, detail = _evaluate_freshness(conn, "gold.orders", "freshness < 7d")
        assert passed is True
        conn.close()

    def test_freshness_no_build_history(self, tmp_path: Path):
        """Test freshness when model has never been built."""
        from havn.engine.contracts import _evaluate_freshness

        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        from havn.engine.database import ensure_meta_table
        ensure_meta_table(conn)

        passed, detail = _evaluate_freshness(conn, "gold.nonexistent", "freshness < 24h")
        assert passed is False
        assert "never been built" in detail
        conn.close()

    def test_freshness_in_contract(self, tmp_path: Path):
        """Test freshness assertion within a full contract evaluation."""
        from havn.engine.contracts import evaluate_contract, Contract, _ensure_contracts_table

        conn = self._setup_with_model_state(tmp_path, hours_ago=1)

        contract = Contract(
            name="freshness_test",
            model="gold.orders",
            assertions=["freshness < 24h"],
        )
        result = evaluate_contract(conn, contract)
        assert result.passed is True
        assert len(result.results) == 1
        assert result.results[0]["passed"] is True
        conn.close()


# ---------------------------------------------------------------------------
# Severity Level Tests
# ---------------------------------------------------------------------------


class TestSeverityLevels:
    """Test severity levels and escalation."""

    def test_warning_severity_preserved(self, tmp_path: Path):
        """Test that warning severity doesn't block pipeline (just recorded)."""
        from havn.engine.contracts import run_contracts

        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        from havn.engine.database import ensure_meta_table
        ensure_meta_table(conn)

        conn.execute("CREATE SCHEMA IF NOT EXISTS gold")
        conn.execute("CREATE TABLE gold.orders AS SELECT 1 AS id")

        contracts_dir = tmp_path / "contracts"
        contracts_dir.mkdir()
        (contracts_dir / "warn.yml").write_text(
            "contracts:\n"
            "  - name: warn_contract\n"
            "    model: gold.orders\n"
            "    severity: warn\n"
            "    assertions:\n"
            "      - row_count > 1000\n"
        )

        results = run_contracts(conn, contracts_dir)
        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].severity == "warn"
        conn.close()

    def test_error_severity_default(self, tmp_path: Path):
        """Test that default severity is error."""
        from havn.engine.contracts import Contract

        c = Contract(name="test", model="gold.orders")
        assert c.severity == "error"

    def test_escalation_after_consecutive_failures(self, tmp_path: Path):
        """Test that severity escalates from warn to error after N failures."""
        from havn.engine.contracts import (
            run_contracts,
            _ensure_contracts_table,
        )

        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        from havn.engine.database import ensure_meta_table
        ensure_meta_table(conn)
        _ensure_contracts_table(conn)

        conn.execute("CREATE SCHEMA IF NOT EXISTS gold")
        conn.execute("CREATE TABLE gold.orders AS SELECT 1 AS id")

        contracts_dir = tmp_path / "contracts"
        contracts_dir.mkdir()
        (contracts_dir / "escalate.yml").write_text(
            "contracts:\n"
            "  - name: escalate_contract\n"
            "    model: gold.orders\n"
            "    severity: warn\n"
            "    escalate_after: 3\n"
            "    assertions:\n"
            "      - row_count > 1000\n"
        )

        # Run 1: fail — consecutive_failures = 1, severity = warn
        results = run_contracts(conn, contracts_dir)
        assert results[0].severity == "warn"
        assert results[0].consecutive_failures == 1

        # Run 2: fail — consecutive_failures = 2, severity = warn
        results = run_contracts(conn, contracts_dir)
        assert results[0].severity == "warn"
        assert results[0].consecutive_failures == 2

        # Run 3: fail — consecutive_failures = 3, severity escalated to error
        results = run_contracts(conn, contracts_dir)
        assert results[0].severity == "error"
        assert results[0].consecutive_failures == 3

        conn.close()

    def test_consecutive_failures_reset_on_pass(self, tmp_path: Path):
        """Test that consecutive failure count resets on a passing run."""
        from havn.engine.contracts import (
            run_contracts,
            _ensure_contracts_table,
        )

        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        from havn.engine.database import ensure_meta_table
        ensure_meta_table(conn)
        _ensure_contracts_table(conn)

        conn.execute("CREATE SCHEMA IF NOT EXISTS gold")
        conn.execute("CREATE TABLE gold.orders AS SELECT 1 AS id")

        contracts_dir = tmp_path / "contracts"
        contracts_dir.mkdir()

        # First: fail
        (contracts_dir / "dynamic.yml").write_text(
            "contracts:\n"
            "  - name: dynamic_contract\n"
            "    model: gold.orders\n"
            "    assertions:\n"
            "      - row_count > 1000\n"
        )
        results = run_contracts(conn, contracts_dir)
        assert results[0].consecutive_failures == 1

        # Now fix it so it passes
        (contracts_dir / "dynamic.yml").write_text(
            "contracts:\n"
            "  - name: dynamic_contract\n"
            "    model: gold.orders\n"
            "    assertions:\n"
            "      - row_count > 0\n"
        )
        results = run_contracts(conn, contracts_dir)
        assert results[0].passed is True
        assert results[0].consecutive_failures == 0

        conn.close()


# ---------------------------------------------------------------------------
# Alert Routing Tests
# ---------------------------------------------------------------------------


class TestAlertRouting:
    """Test that contract failures trigger alert routing."""

    def test_alert_routing_on_error_failure(self, tmp_path: Path):
        """Test that alerts are sent on error-severity contract failure."""
        from havn.engine.contracts import (
            Contract,
            ContractResult,
            _send_contract_alerts,
        )

        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        from havn.engine.database import ensure_meta_table
        ensure_meta_table(conn)

        contract = Contract(
            name="alert_test",
            model="gold.orders",
            severity="error",
            notify=["log"],
            assertions=["row_count > 0"],
        )
        cr = ContractResult(
            contract_name="alert_test",
            model="gold.orders",
            passed=False,
            severity="error",
            results=[{"expression": "row_count > 0", "passed": False, "detail": "failed"}],
            consecutive_failures=1,
        )

        # Should not raise — sends to "log" channel
        _send_contract_alerts(conn, contract, cr)
        conn.close()

    def test_no_alert_on_warning_severity(self, tmp_path: Path):
        """Test that alerts are NOT sent when severity is 'warn' (default behavior)."""
        from havn.engine.contracts import (
            Contract,
            ContractResult,
            _send_contract_alerts,
        )

        db_path = tmp_path / "test.duckdb"
        conn = duckdb.connect(str(db_path))
        from havn.engine.database import ensure_meta_table
        ensure_meta_table(conn)

        contract = Contract(
            name="warn_test",
            model="gold.orders",
            severity="warn",
            notify=["log"],
            assertions=["row_count > 0"],
        )
        cr = ContractResult(
            contract_name="warn_test",
            model="gold.orders",
            passed=False,
            severity="warn",
            results=[{"expression": "row_count > 0", "passed": False, "detail": "failed"}],
        )

        # Should not send because severity is warn
        _send_contract_alerts(conn, contract, cr)
        conn.close()

    def test_no_alert_on_pass(self, tmp_path: Path):
        """Test that no alerts are sent when contract passes."""
        from havn.engine.contracts import (
            Contract,
            ContractResult,
            _send_contract_alerts,
        )

        contract = Contract(
            name="pass_test",
            model="gold.orders",
            notify=["log"],
            assertions=["row_count > 0"],
        )
        cr = ContractResult(
            contract_name="pass_test",
            model="gold.orders",
            passed=True,
            severity="error",
            results=[{"expression": "row_count > 0", "passed": True, "detail": "ok"}],
        )

        # Should not send because contract passed
        _send_contract_alerts(None, contract, cr)

    def test_no_alert_when_notify_empty(self, tmp_path: Path):
        """Test that no alerts are sent when notify list is empty."""
        from havn.engine.contracts import (
            Contract,
            ContractResult,
            _send_contract_alerts,
        )

        contract = Contract(
            name="no_notify",
            model="gold.orders",
            notify=[],
            assertions=["row_count > 0"],
        )
        cr = ContractResult(
            contract_name="no_notify",
            model="gold.orders",
            passed=False,
            severity="error",
            results=[],
        )

        # Should not send because notify is empty
        _send_contract_alerts(None, contract, cr)


# ---------------------------------------------------------------------------
# API Integration Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def project_with_contracts(tmp_path):
    """Create a test project with contracts and a warehouse."""
    (tmp_path / "project.yml").write_text("name: test\ndatabase:\n  path: warehouse.duckdb\nstreams:\n  test:\n    steps:\n      - transform: [all]\n")
    (tmp_path / "transform" / "bronze").mkdir(parents=True)
    (tmp_path / "transform" / "bronze" / "test.sql").write_text(
        "-- config: materialized=table, schema=bronze\nSELECT 1 AS id, 'Alice' AS name"
    )
    (tmp_path / "ingest").mkdir()
    (tmp_path / "export").mkdir()

    # Create contracts
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    (contracts_dir / "test.yml").write_text(
        "contracts:\n"
        "  - name: test_contract\n"
        "    model: bronze.test\n"
        "    assertions:\n"
        "      - row_count > 0\n"
    )

    # Create warehouse
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    from havn.engine.database import ensure_meta_table
    ensure_meta_table(conn)
    conn.execute("CREATE SCHEMA IF NOT EXISTS bronze")
    conn.execute("CREATE TABLE bronze.test AS SELECT 1 AS id, 'Alice' AS name")
    conn.close()

    return tmp_path


@pytest.fixture
def api_client(project_with_contracts):
    """Create a FastAPI TestClient."""
    from starlette.testclient import TestClient
    import havn.server.app as server_app
    from havn.server.deps import reset_shared_conn
    reset_shared_conn()
    server_app.PROJECT_DIR = project_with_contracts
    server_app.AUTH_ENABLED = False
    yield TestClient(server_app.app)
    reset_shared_conn()


class TestContractsAPI:
    def test_list_contracts(self, api_client):
        resp = api_client.get("/api/contracts")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "test_contract"

    def test_run_contracts(self, api_client):
        resp = api_client.post("/api/contracts/run")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["passed"] == 1
        assert data["failed"] == 0
        # Check consecutive_failures is included
        assert "consecutive_failures" in data["results"][0]

    def test_contracts_history(self, api_client):
        # Run first to have history
        api_client.post("/api/contracts/run")
        resp = api_client.get("/api/contracts/history")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        # Check consecutive_failures is in history
        assert "consecutive_failures" in data[0]

    def test_contract_model_history(self, api_client):
        """Test the per-model contract history endpoint."""
        api_client.post("/api/contracts/run")
        resp = api_client.get("/api/contracts/bronze.test/history")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["model"] == "bronze.test"

    def test_contract_model_history_empty(self, api_client):
        """Test per-model history returns empty for unknown model."""
        resp = api_client.get("/api/contracts/gold.nonexistent/history")
        assert resp.status_code == 200
        data = resp.json()
        assert data == []


# ---------------------------------------------------------------------------
# Security Hardening Tests
# ---------------------------------------------------------------------------


class TestContractsSecurity:
    """Test identifier injection prevention in contracts."""

    def test_contract_rejects_invalid_model_name(self, tmp_path: Path):
        from havn.engine.contracts import Contract, evaluate_contract

        conn = duckdb.connect(str(tmp_path / "test.duckdb"))
        from havn.engine.database import ensure_meta_table
        ensure_meta_table(conn)

        contract = Contract(
            name="bad",
            model="gold.users; DROP TABLE--",
            assertions=["row_count > 0"],
        )
        result = evaluate_contract(conn, contract)
        assert result.passed is False
        assert "Invalid" in result.error
        conn.close()


# ---------------------------------------------------------------------------
# Column Contracts
# ---------------------------------------------------------------------------


def _write_contract(tmp_path: Path, body: str) -> Path:
    """Write a single contract file and return the contracts directory."""
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir(exist_ok=True)
    (contracts_dir / "shape.yml").write_text(body)
    return contracts_dir


class TestColumnDeclarationParsing:
    """The `columns:` block, and what happens when it is wrong."""

    def test_valid_declaration(self, tmp_path: Path):
        from havn.engine.contracts import discover_contracts

        contracts_dir = _write_contract(tmp_path, """
contracts:
  - name: orders_shape
    model: gold.orders
    strict: true
    on_widen: error
    columns:
      - name: order_id
        type: BIGINT
        nullable: false
        description: Surrogate key
      - name: total
        type: decimal(38,2)
""")
        contract = discover_contracts(contracts_dir)[0]
        assert contract.errors == []
        assert contract.strict is True
        assert contract.on_widen == "error"
        assert [(c.name, c.type) for c in contract.columns] == [
            ("order_id", "BIGINT"),
            # DuckDB itself canonicalizes the type text.
            ("total", "DECIMAL(38,2)"),
        ]
        assert contract.columns[0].nullable is False
        assert contract.columns[0].description == "Surrogate key"
        assert contract.columns[1].nullable is None

    def test_declaration_is_optional(self, tmp_path: Path):
        """Every contract written before columns existed still loads."""
        from havn.engine.contracts import discover_contracts

        contracts_dir = _write_contract(tmp_path, """
contracts:
  - name: legacy
    model: gold.orders
    assertions:
      - row_count > 0
""")
        contract = discover_contracts(contracts_dir)[0]
        assert contract.columns == []
        assert contract.strict is False
        assert contract.on_widen == "warn"
        assert contract.errors == []

    def test_invalid_type_is_collected_not_raised(self, tmp_path: Path):
        from havn.engine.contracts import discover_contracts

        contracts_dir = _write_contract(tmp_path, """
contracts:
  - name: orders_shape
    model: gold.orders
    columns:
      - name: good
        type: VARCHAR
      - name: bad
        type: NOTATYPE
""")
        contract = discover_contracts(contracts_dir)[0]
        # The good column still loads: one typo must not disable the check.
        assert [c.name for c in contract.columns] == ["good"]
        assert len(contract.errors) == 1
        assert "NOTATYPE" in contract.errors[0]

    def test_type_text_that_is_not_a_type_never_reaches_duckdb(self, tmp_path: Path):
        from havn.engine.contracts import discover_contracts

        contracts_dir = _write_contract(tmp_path, """
contracts:
  - name: orders_shape
    model: gold.orders
    columns:
      - name: inj
        type: "INTEGER); DROP TABLE x; --"
""")
        contract = discover_contracts(contracts_dir)[0]
        assert contract.columns == []
        assert "not a type name" in contract.errors[0]

    def test_invalid_identifier(self, tmp_path: Path):
        from havn.engine.contracts import discover_contracts

        contracts_dir = _write_contract(tmp_path, """
contracts:
  - name: orders_shape
    model: gold.orders
    columns:
      - name: 9lives
        type: INTEGER
      - name: "order id"
        type: INTEGER
""")
        contract = discover_contracts(contracts_dir)[0]
        assert contract.columns == []
        assert len(contract.errors) == 2
        assert all("not a valid column name" in e for e in contract.errors)

    def test_duplicate_columns(self, tmp_path: Path):
        from havn.engine.contracts import discover_contracts

        contracts_dir = _write_contract(tmp_path, """
contracts:
  - name: orders_shape
    model: gold.orders
    columns:
      - name: order_id
        type: INTEGER
      - name: ORDER_ID
        type: BIGINT
""")
        contract = discover_contracts(contracts_dir)[0]
        assert len(contract.columns) == 1
        assert "duplicate column" in contract.errors[0]

    def test_unknown_on_widen_falls_back_to_warn(self, tmp_path: Path):
        from havn.engine.contracts import discover_contracts

        contracts_dir = _write_contract(tmp_path, """
contracts:
  - name: orders_shape
    model: gold.orders
    on_widen: explode
    columns:
      - name: order_id
        type: INTEGER
""")
        contract = discover_contracts(contracts_dir)[0]
        assert contract.on_widen == "warn"
        assert "unknown on_widen" in contract.errors[0]


class TestCheckContractSchema:
    """Each finding kind, from the same (column, type) list every caller passes."""

    def _contract(self, **kwargs):
        from havn.engine.contracts import Contract, ContractColumn

        columns = [
            ContractColumn(name=name, type=ctype, nullable=nullable)
            for name, ctype, nullable in kwargs.pop("columns", [])
        ]
        return Contract(
            name="shape", model="gold.orders", columns=columns, **kwargs
        )

    def test_match_produces_nothing(self):
        from havn.engine.contracts import check_contract_schema

        contract = self._contract(columns=[("order_id", "BIGINT", None)])
        assert check_contract_schema(contract, [("order_id", "BIGINT")]) == []

    def test_parameter_change_within_a_type_is_not_a_finding(self):
        """DECIMAL(10,2) to DECIMAL(38,2) is the same type, differently sized."""
        from havn.engine.contracts import check_contract_schema

        contract = self._contract(columns=[("total", "DECIMAL(10,2)", None)])
        assert check_contract_schema(contract, [("total", "DECIMAL(38,2)")]) == []

    def test_missing_column_is_an_error(self):
        from havn.engine.contracts import check_contract_schema

        contract = self._contract(columns=[("order_id", "BIGINT", None)])
        findings = check_contract_schema(contract, [("other", "BIGINT")])
        missing = [f for f in findings if f.kind == "missing_column"]
        assert len(missing) == 1
        assert missing[0].severity == "error"
        assert missing[0].column == "order_id"

    def test_extra_column_is_informational_by_default(self):
        from havn.engine.contracts import check_contract_schema

        contract = self._contract(columns=[("order_id", "BIGINT", None)])
        findings = check_contract_schema(
            contract, [("order_id", "BIGINT"), ("note", "VARCHAR")]
        )
        assert [(f.kind, f.severity) for f in findings] == [("extra_column", "info")]

    def test_extra_column_is_an_error_when_strict(self):
        from havn.engine.contracts import check_contract_schema

        contract = self._contract(strict=True, columns=[("order_id", "BIGINT", None)])
        findings = check_contract_schema(
            contract, [("order_id", "BIGINT"), ("note", "VARCHAR")]
        )
        assert [(f.kind, f.severity) for f in findings] == [("extra_column", "error")]

    def test_widening_warns_by_default(self):
        from havn.engine.contracts import check_contract_schema

        contract = self._contract(columns=[("order_id", "INTEGER", None)])
        findings = check_contract_schema(contract, [("order_id", "BIGINT")])
        assert [(f.kind, f.severity) for f in findings] == [
            ("type_widened", "warning")
        ]

    def test_double_to_decimal_is_a_widening(self):
        """The shape a SUM of a DOUBLE resolves to must not read as a break."""
        from havn.engine.contracts import check_contract_schema

        contract = self._contract(columns=[("total", "DOUBLE", None)])
        findings = check_contract_schema(contract, [("total", "DECIMAL(38,1)")])
        assert [f.kind for f in findings] == ["type_widened"]

    def test_on_widen_error(self):
        from havn.engine.contracts import check_contract_schema

        contract = self._contract(
            on_widen="error", columns=[("order_id", "INTEGER", None)]
        )
        findings = check_contract_schema(contract, [("order_id", "BIGINT")])
        assert [(f.kind, f.severity) for f in findings] == [("type_widened", "error")]

    def test_on_widen_ignore(self):
        from havn.engine.contracts import check_contract_schema

        contract = self._contract(
            on_widen="ignore", columns=[("order_id", "INTEGER", None)]
        )
        assert check_contract_schema(contract, [("order_id", "BIGINT")]) == []

    def test_narrowing_is_an_error(self):
        from havn.engine.contracts import check_contract_schema

        contract = self._contract(columns=[("order_id", "BIGINT", None)])
        findings = check_contract_schema(contract, [("order_id", "SMALLINT")])
        assert [(f.kind, f.severity) for f in findings] == [
            ("type_narrowed", "error")
        ]

    def test_category_change_is_an_error(self):
        from havn.engine.contracts import check_contract_schema

        contract = self._contract(columns=[("order_id", "BIGINT", None)])
        findings = check_contract_schema(contract, [("order_id", "VARCHAR")])
        assert [(f.kind, f.severity) for f in findings] == [
            ("type_changed", "error")
        ]

    def test_list_of_a_type_is_not_the_type(self):
        from havn.engine.contracts import check_contract_schema

        contract = self._contract(columns=[("tags", "VARCHAR", None)])
        findings = check_contract_schema(contract, [("tags", "VARCHAR[]")])
        assert [f.kind for f in findings] == ["type_changed"]

    def test_nullability_is_skipped_when_unknown(self):
        from havn.engine.contracts import check_contract_schema

        contract = self._contract(columns=[("order_id", "BIGINT", False)])
        assert check_contract_schema(contract, [("order_id", "BIGINT")]) == []

    def test_nullability_error_when_the_source_distinguishes(self):
        from havn.engine.contracts import check_contract_schema

        contract = self._contract(columns=[("order_id", "BIGINT", False)])
        findings = check_contract_schema(
            contract, [("order_id", "BIGINT")], nullability={"order_id": True}
        )
        assert [(f.kind, f.severity) for f in findings] == [
            ("not_null_violated", "error")
        ]

    def test_no_declaration_means_no_findings(self):
        from havn.engine.contracts import check_contract_schema

        contract = self._contract(columns=[])
        assert check_contract_schema(contract, [("anything", "VARCHAR")]) == []


class TestDescribeWithNullability:
    """A CTAS table reports every column nullable, which is no answer at all."""

    def test_all_nullable_is_treated_as_no_information(self):
        from havn.engine.contracts import describe_with_nullability

        conn = duckdb.connect(":memory:")
        conn.execute("CREATE SCHEMA gold")
        conn.execute("CREATE TABLE gold.orders AS SELECT 1 AS id, 'x' AS note")
        columns, nullability = describe_with_nullability(conn, "gold.orders")
        assert [c[0] for c in columns] == ["id", "note"]
        assert nullability == {}
        conn.close()

    def test_a_not_null_constraint_makes_the_map_meaningful(self):
        from havn.engine.contracts import describe_with_nullability

        conn = duckdb.connect(":memory:")
        conn.execute("CREATE SCHEMA gold")
        conn.execute(
            "CREATE TABLE gold.orders (id INTEGER NOT NULL, note VARCHAR)"
        )
        _, nullability = describe_with_nullability(conn, "gold.orders")
        assert nullability == {"id": False, "note": True}
        conn.close()


class TestContractSchemaBeforeTheBuild:
    """The point of the whole feature: a break caught on an empty warehouse."""

    def _project(self, tmp_path: Path, contract_body: str, query: str):
        from havn.engine.database import ensure_meta_table
        from havn.engine.transform import SQLModel

        _write_contract(tmp_path, contract_body)
        (tmp_path / "transform" / "gold").mkdir(parents=True, exist_ok=True)
        (tmp_path / "project.yml").write_text("name: p\n")
        conn = duckdb.connect(str(tmp_path / "w.duckdb"))
        ensure_meta_table(conn)
        conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
        conn.execute(
            "CREATE TABLE landing.raw AS "
            "SELECT 1::BIGINT AS order_id, 2.0::DOUBLE AS total, 'x' AS note"
        )
        model = SQLModel(
            path=tmp_path / "transform" / "gold" / "orders.sql",
            name="orders",
            schema="gold",
            full_name="gold.orders",
            sql="",
            query=query,
            materialized="table",
            depends_on=["landing.raw"],
        )
        return conn, model

    def test_break_is_caught_before_any_table_exists(self, tmp_path: Path):
        from havn.engine.transform import validate_models

        conn, model = self._project(tmp_path, """
contracts:
  - name: orders_shape
    model: gold.orders
    columns:
      - name: order_id
        type: BIGINT
      - name: gone
        type: VARCHAR
""", "SELECT order_id, total FROM landing.raw")
        try:
            # gold.orders has never been built.
            assert conn.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = 'gold'"
            ).fetchone()[0] == 0

            errors = validate_models(conn, [model], bind=True, project_dir=tmp_path)
            messages = [e.message for e in errors if e.severity == "error"]
            assert any("does not have the declared column 'gone'" in m for m in messages)
        finally:
            conn.close()

    def test_a_clean_declaration_reports_nothing(self, tmp_path: Path):
        from havn.engine.transform import validate_models

        conn, model = self._project(tmp_path, """
contracts:
  - name: orders_shape
    model: gold.orders
    strict: true
    columns:
      - name: order_id
        type: BIGINT
      - name: total
        type: DOUBLE
""", "SELECT order_id, total FROM landing.raw")
        try:
            errors = validate_models(conn, [model], bind=True, project_dir=tmp_path)
            assert [e.message for e in errors] == []
        finally:
            conn.close()

    def test_a_widening_is_a_warning_not_a_break(self, tmp_path: Path):
        from havn.engine.transform import validate_models

        conn, model = self._project(tmp_path, """
contracts:
  - name: orders_shape
    model: gold.orders
    columns:
      - name: order_id
        type: INTEGER
""", "SELECT order_id FROM landing.raw")
        try:
            errors = validate_models(conn, [model], bind=True, project_dir=tmp_path)
            assert [e.severity for e in errors] == ["warning"]
            assert "widened" in errors[0].message
        finally:
            conn.close()

    def test_declaration_errors_are_reported_at_validate_time(self, tmp_path: Path):
        from havn.engine.transform import validate_models

        conn, model = self._project(tmp_path, """
contracts:
  - name: orders_shape
    model: gold.orders
    columns:
      - name: order_id
        type: NOTATYPE
""", "SELECT order_id, total FROM landing.raw")
        try:
            errors = validate_models(conn, [model], bind=True, project_dir=tmp_path)
            assert any(
                "NOTATYPE" in e.message and e.severity == "error" for e in errors
            )
        finally:
            conn.close()

    def test_undeclared_column_is_quiet_unless_strict(self, tmp_path: Path):
        from havn.engine.transform import validate_models

        conn, model = self._project(tmp_path, """
contracts:
  - name: orders_shape
    model: gold.orders
    columns:
      - name: order_id
        type: BIGINT
""", "SELECT order_id, total, note FROM landing.raw")
        try:
            errors = validate_models(conn, [model], bind=True, project_dir=tmp_path)
            assert [e.message for e in errors] == []
        finally:
            conn.close()

    def test_strict_rejects_an_undeclared_column(self, tmp_path: Path):
        from havn.engine.transform import validate_models

        conn, model = self._project(tmp_path, """
contracts:
  - name: orders_shape
    model: gold.orders
    strict: true
    columns:
      - name: order_id
        type: BIGINT
""", "SELECT order_id, note FROM landing.raw")
        try:
            errors = validate_models(conn, [model], bind=True, project_dir=tmp_path)
            assert [e.severity for e in errors] == ["error"]
            assert "undeclared column 'note'" in errors[0].message
        finally:
            conn.close()

    def test_persisted_columns_are_the_fallback(self, tmp_path: Path):
        """A contract on a model outside the bound set still gets checked."""
        from havn.engine.transform import SQLModel, validate_models
        from havn.engine.transform.columns import save_model_columns

        conn, model = self._project(tmp_path, """
contracts:
  - name: other_shape
    model: gold.other
    columns:
      - name: missing_everywhere
        type: VARCHAR
""", "SELECT order_id FROM landing.raw")
        try:
            other = SQLModel(
                path=tmp_path / "transform" / "gold" / "other.sql",
                name="other", schema="gold", full_name="gold.other",
                sql="", query="SELECT 1 AS id", materialized="table",
            )
            save_model_columns(conn, other, [("id", "INTEGER")])

            # gold.other is not in the model list, so the bind pass never
            # sees it; the schema recorded at its last build is used instead.
            errors = validate_models(conn, [model], bind=True, project_dir=tmp_path)
            assert any(
                "missing_everywhere" in e.message and e.severity == "error"
                for e in errors
            )
        finally:
            conn.close()


class TestContractSchemaAfterTheBuild:
    """The same check against the live table, so the report is complete."""

    def _run(self, tmp_path: Path, contract_body: str):
        from havn.engine.contracts import discover_contracts, evaluate_contract
        from havn.engine.database import ensure_meta_table

        contracts_dir = _write_contract(tmp_path, contract_body)
        conn = duckdb.connect(":memory:")
        ensure_meta_table(conn)
        conn.execute("CREATE SCHEMA gold")
        conn.execute(
            "CREATE TABLE gold.orders AS "
            "SELECT 1::BIGINT AS order_id, 'x' AS note"
        )
        contract = discover_contracts(contracts_dir)[0]
        try:
            return evaluate_contract(conn, contract)
        finally:
            conn.close()

    def test_post_build_break_fails_the_contract(self, tmp_path: Path):
        result = self._run(tmp_path, """
contracts:
  - name: orders_shape
    model: gold.orders
    columns:
      - name: order_id
        type: VARCHAR
    assertions:
      - row_count > 0
""")
        assert result.passed is False
        # `note` is undeclared, which on a non-strict contract is information.
        assert [f.kind for f in result.schema_findings] == [
            "type_changed", "extra_column"
        ]
        assert any("type_changed" in r["expression"] for r in result.results)

    def test_post_build_clean_declaration_passes(self, tmp_path: Path):
        result = self._run(tmp_path, """
contracts:
  - name: orders_shape
    model: gold.orders
    strict: true
    columns:
      - name: order_id
        type: BIGINT
      - name: note
        type: VARCHAR
    assertions:
      - row_count > 0
""")
        assert result.passed is True
        assert result.schema_findings == []

    def test_informational_findings_do_not_fail_the_contract(self, tmp_path: Path):
        result = self._run(tmp_path, """
contracts:
  - name: orders_shape
    model: gold.orders
    columns:
      - name: order_id
        type: BIGINT
    assertions:
      - row_count > 0
""")
        assert result.passed is True
        assert [f.severity for f in result.schema_findings] == ["info"]
        # An informational finding stays out of the pass/fail rule list.
        assert all("extra_column" not in r["expression"] for r in result.results)

    def test_declaration_errors_fail_the_contract(self, tmp_path: Path):
        result = self._run(tmp_path, """
contracts:
  - name: orders_shape
    model: gold.orders
    columns:
      - name: order_id
        type: NOTATYPE
    assertions:
      - row_count > 0
""")
        assert result.passed is False


class TestSchemaDriftWarning:
    """The contract check without a contract, off until it has been tuned."""

    def _project(self, tmp_path: Path, project_yml: str = "name: p\n"):
        from havn.engine.database import ensure_meta_table
        from havn.engine.transform import SQLModel
        from havn.engine.transform.columns import save_model_columns

        (tmp_path / "transform" / "gold").mkdir(parents=True, exist_ok=True)
        (tmp_path / "project.yml").write_text(project_yml)
        conn = duckdb.connect(str(tmp_path / "w.duckdb"))
        ensure_meta_table(conn)
        conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
        conn.execute(
            "CREATE TABLE landing.raw AS "
            "SELECT 1::BIGINT AS order_id, 'x' AS note"
        )
        model = SQLModel(
            path=tmp_path / "transform" / "gold" / "orders.sql",
            name="orders", schema="gold", full_name="gold.orders", sql="",
            query="SELECT order_id, note FROM landing.raw",
            materialized="table", depends_on=["landing.raw"],
        )
        save_model_columns(
            conn, model, [("order_id", "INTEGER"), ("dropped", "DATE")]
        )
        return conn, model

    def test_off_by_default(self, tmp_path: Path):
        from havn.engine.transform import validate_models

        conn, model = self._project(tmp_path)
        try:
            errors = validate_models(conn, [model], bind=True, project_dir=tmp_path)
            assert [e.message for e in errors] == []
        finally:
            conn.close()

    def test_on_by_argument(self, tmp_path: Path):
        from havn.engine.transform import validate_models

        conn, model = self._project(tmp_path)
        try:
            errors = validate_models(
                conn, [model], bind=True, project_dir=tmp_path, schema_drift=True
            )
            assert len(errors) == 1
            assert errors[0].severity == "warning"
            message = errors[0].message
            assert message.startswith("output schema of gold.orders changes:")
            assert "added note" in message
            assert "removed dropped" in message
            assert "retyped order_id INTEGER -> BIGINT" in message
        finally:
            conn.close()

    def test_on_by_project_setting(self, tmp_path: Path):
        from havn.engine.transform import validate_models

        conn, model = self._project(
            tmp_path, "name: p\nvalidation:\n  schema_drift: warn\n"
        )
        try:
            errors = validate_models(conn, [model], bind=True, project_dir=tmp_path)
            assert len(errors) == 1
            assert errors[0].severity == "warning"
        finally:
            conn.close()

    def test_an_unbuilt_model_has_not_drifted(self, tmp_path: Path):
        """No baseline means no drift, not drift from nothing."""
        from havn.engine.database import ensure_meta_table
        from havn.engine.transform import SQLModel, validate_models

        (tmp_path / "transform" / "gold").mkdir(parents=True, exist_ok=True)
        (tmp_path / "project.yml").write_text("name: p\n")
        conn = duckdb.connect(str(tmp_path / "w.duckdb"))
        ensure_meta_table(conn)
        conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
        conn.execute("CREATE TABLE landing.raw AS SELECT 1 AS order_id")
        model = SQLModel(
            path=tmp_path / "transform" / "gold" / "orders.sql",
            name="orders", schema="gold", full_name="gold.orders", sql="",
            query="SELECT order_id FROM landing.raw",
            materialized="table", depends_on=["landing.raw"],
        )
        try:
            errors = validate_models(
                conn, [model], bind=True, project_dir=tmp_path, schema_drift=True
            )
            assert [e.message for e in errors] == []
        finally:
            conn.close()
