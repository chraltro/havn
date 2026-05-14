"""Data contracts & assertions framework.

Discovers YAML contract files in the contracts/ directory, evaluates them
against the warehouse, and tracks historical pass/fail results. Contracts
are standalone data quality rules that complement inline ``-- assert:`` comments.

Contract YAML format::

    contracts:
      - name: orders_not_empty
        description: "Orders table must have data"
        model: gold.orders
        assertions:
          - row_count > 0
          - no_nulls(order_id)
          - unique(order_id)
          - accepted_values(status, ['pending', 'shipped', 'delivered'])
          - "total_amount >= 0"
          - "row_count > {previous * 0.9}"
          - "freshness < 24h"

      - name: customers_fresh
        description: "Customers must be loaded within 24h"
        model: silver.customers
        severity: warn
        notify: [slack, log]
        escalate_after: 3
        assertions:
          - row_count > 0
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import yaml

from havn.engine.database import ensure_meta_table, log_run
from havn.engine.utils import validate_identifier

logger = logging.getLogger("havn.contracts")

# Regex for {previous}, {previous * N}, {previous + N}, {previous - N}
_PREVIOUS_RE = re.compile(
    r"\{previous\s*(?:([*/+\-])\s*([0-9]+(?:\.[0-9]+)?))?\}"
)

# Regex for freshness assertions: freshness < 24h, freshness < 7d
_FRESHNESS_RE = re.compile(
    r"freshness\s*(<=|>=|<|>)\s*(\d+(?:\.\d+)?)\s*(h|d|m|s)"
)


@dataclass
class Contract:
    """A single data contract definition."""

    name: str
    model: str  # e.g. "gold.orders"
    assertions: list[str] = field(default_factory=list)
    description: str = ""
    severity: str = "error"  # "error" or "warn"
    path: Path | None = None  # source file
    notify: list[str] = field(default_factory=list)  # ["slack", "webhook", "log"]
    escalate_after: int = 0  # consecutive failures before severity upgrades


@dataclass
class ContractResult:
    """Result of evaluating a single contract."""

    contract_name: str
    model: str
    passed: bool
    severity: str
    results: list[dict]  # [{expression, passed, detail}]
    duration_ms: int = 0
    error: str | None = None
    consecutive_failures: int = 0


def discover_contracts(contracts_dir: Path) -> list[Contract]:
    """Discover all contract YAML files in the contracts/ directory.

    Each .yml file can contain a ``contracts:`` list with one or more
    contract definitions.
    """
    contracts: list[Contract] = []
    if not contracts_dir.exists():
        return contracts

    for yml_file in sorted(contracts_dir.glob("*.yml")):
        try:
            raw = yaml.safe_load(yml_file.read_text()) or {}
            for c_raw in raw.get("contracts", []):
                contracts.append(Contract(
                    name=c_raw.get("name", yml_file.stem),
                    model=c_raw.get("model", ""),
                    assertions=c_raw.get("assertions", []),
                    description=c_raw.get("description", ""),
                    severity=c_raw.get("severity", "error"),
                    path=yml_file,
                    notify=c_raw.get("notify", []),
                    escalate_after=int(c_raw.get("escalate_after", 0)),
                ))
        except Exception as e:
            logger.warning("Failed to parse contract file %s: %s", yml_file, e)

    return contracts


def _resolve_previous(
    conn: duckdb.DuckDBPyConnection,
    model: str,
    expr: str,
    metric: str = "row_count",
) -> tuple[str | None, str | None]:
    """Resolve {previous} placeholders in an expression.

    Returns (resolved_expression, warning_or_None). If no previous baseline
    exists, returns (None, warning_message).
    """
    matches = list(_PREVIOUS_RE.finditer(expr))
    if not matches:
        return expr, None

    # Look up previous profile data
    try:
        row = conn.execute(
            "SELECT row_count, null_percentages, distinct_counts "
            "FROM _havn.model_profiles WHERE model_path = ?",
            [model],
        ).fetchone()
    except Exception:
        row = None

    if not row:
        return None, f"No baseline available yet for {model}"

    prev_value = row[0]  # row_count by default

    resolved = expr
    for m in reversed(matches):
        operator = m.group(1)
        operand = m.group(2)

        if operator and operand:
            operand_f = float(operand)
            if operator == "*":
                computed = prev_value * operand_f
            elif operator == "/":
                computed = prev_value / operand_f if operand_f != 0 else 0
            elif operator == "+":
                computed = prev_value + operand_f
            elif operator == "-":
                computed = prev_value - operand_f
            else:
                computed = prev_value
        else:
            computed = prev_value

        # Use integer if it's a whole number
        if computed == int(computed):
            replacement = str(int(computed))
        else:
            replacement = str(computed)

        resolved = resolved[:m.start()] + replacement + resolved[m.end():]

    return resolved, None


def _evaluate_freshness(
    conn: duckdb.DuckDBPyConnection,
    model: str,
    expr: str,
) -> tuple[bool, str]:
    """Evaluate a freshness assertion like 'freshness < 24h'.

    Returns (passed, detail_message).
    """
    m = _FRESHNESS_RE.match(expr.strip())
    if not m:
        return False, f"Invalid freshness expression: {expr}"

    operator = m.group(1)
    value = float(m.group(2))
    unit = m.group(3)

    # Convert to hours. `m` means minutes (industry standard), not months.
    # Use `d` for days.
    if unit == "d":
        threshold_hours = value * 24
    elif unit == "m":
        threshold_hours = value / 60.0
    elif unit == "s":
        threshold_hours = value / 3600.0
    else:
        threshold_hours = value

    # Get last run time from model_state
    try:
        row = conn.execute(
            "SELECT last_run_at FROM _havn.model_state WHERE model_path = ?",
            [model],
        ).fetchone()
    except Exception:
        row = None

    if not row or not row[0]:
        return False, f"Model {model} has never been built"

    last_run_at = row[0]
    hours_since = conn.execute(
        "SELECT EXTRACT(EPOCH FROM (current_timestamp - ?::TIMESTAMP)) / 3600.0",
        [last_run_at],
    ).fetchone()[0]

    # Evaluate comparison
    if operator == "<":
        passed = hours_since < threshold_hours
    elif operator == "<=":
        passed = hours_since <= threshold_hours
    elif operator == ">":
        passed = hours_since > threshold_hours
    elif operator == ">=":
        passed = hours_since >= threshold_hours
    else:
        passed = False

    hours_since_r = round(hours_since, 1)
    # Clean display: "6h" not "6.0h"
    threshold_display = f"{int(value)}{unit}" if value == int(value) else f"{value}{unit}"

    if passed:
        detail = f"Model last built {hours_since_r}h ago, within threshold of {threshold_display}"
    else:
        detail = f"Model last built {hours_since_r}h ago, threshold is {threshold_display}"

    return passed, detail


def _get_consecutive_failures(
    conn: duckdb.DuckDBPyConnection,
    contract_name: str,
) -> int:
    """Get the consecutive failure count for a contract."""
    try:
        row = conn.execute(
            """
            SELECT consecutive_failures
            FROM _havn.contract_results
            WHERE contract_name = ?
            ORDER BY checked_at DESC
            LIMIT 1
            """,
            [contract_name],
        ).fetchone()
        return row[0] if row and row[0] is not None else 0
    except Exception:
        return 0


def evaluate_contract(
    conn: duckdb.DuckDBPyConnection,
    contract: Contract,
) -> ContractResult:
    """Evaluate a single contract against the warehouse.

    Uses the same assertion evaluation logic as inline ``-- assert:`` comments.
    Supports {previous} placeholders for relative thresholds, freshness
    assertions, severity levels, and escalation.
    """
    from havn.engine.transform import SQLModel, _evaluate_assertion

    start = time.perf_counter()
    results: list[dict] = []
    all_passed = True

    # Check table exists
    parts = contract.model.split(".")
    if len(parts) != 2:
        return ContractResult(
            contract_name=contract.name,
            model=contract.model,
            passed=False,
            severity=contract.severity,
            results=[],
            error=f"Invalid model name: {contract.model}",
        )

    schema, name = parts

    # Validate identifiers to prevent SQL injection via crafted YAML
    try:
        validate_identifier(schema, "contract model schema")
        validate_identifier(name, "contract model name")
    except ValueError as e:
        return ContractResult(
            contract_name=contract.name,
            model=contract.model,
            passed=False,
            severity=contract.severity,
            results=[],
            error=str(e),
        )

    exists = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
        [schema, name],
    ).fetchone()[0] > 0

    if not exists:
        return ContractResult(
            contract_name=contract.name,
            model=contract.model,
            passed=False,
            severity=contract.severity,
            results=[],
            error=f"Table {contract.model} does not exist",
        )

    # Build a minimal SQLModel for assertion evaluation
    dummy_model = SQLModel(
        path=contract.path or Path("."),
        name=name,
        schema=schema,
        full_name=contract.model,
        sql="",
        query="",
        materialized="table",
    )

    for expr in contract.assertions:
        try:
            # Check for freshness assertions
            if _FRESHNESS_RE.match(expr.strip()):
                passed, detail = _evaluate_freshness(conn, contract.model, expr)
                results.append({
                    "expression": expr,
                    "passed": passed,
                    "detail": detail,
                })
                if not passed:
                    all_passed = False
                continue

            # Resolve {previous} placeholders
            resolved_expr = expr
            if "{previous" in expr:
                resolved, warning = _resolve_previous(conn, contract.model, expr)
                if resolved is None:
                    # No baseline — skip with warning
                    results.append({
                        "expression": expr,
                        "passed": True,  # don't fail on missing baseline
                        "detail": warning or "No baseline available yet",
                        "skipped": True,
                    })
                    continue
                resolved_expr = resolved

            ar = _evaluate_assertion(conn, dummy_model, resolved_expr)
            detail = ar.detail
            # If we resolved a {previous} placeholder, include context
            if resolved_expr != expr:
                detail = f"{ar.detail} (resolved from: {expr})"
            results.append({
                "expression": expr,
                "passed": ar.passed,
                "detail": detail,
            })
            if not ar.passed:
                all_passed = False
        except Exception as e:
            results.append({
                "expression": expr,
                "passed": False,
                "detail": f"Error: {e}",
            })
            all_passed = False

    duration_ms = int((time.perf_counter() - start) * 1000)

    # Determine consecutive failures and effective severity
    prev_consecutive = _get_consecutive_failures(conn, contract.name)
    if all_passed:
        consecutive_failures = 0
    else:
        consecutive_failures = prev_consecutive + 1

    # Escalation: upgrade severity from warn to error after N consecutive failures
    effective_severity = contract.severity
    if (
        contract.escalate_after > 0
        and contract.severity == "warn"
        and consecutive_failures >= contract.escalate_after
    ):
        effective_severity = "error"
        logger.warning(
            "Contract '%s' escalated from warn to error after %d consecutive failures",
            contract.name,
            consecutive_failures,
        )

    return ContractResult(
        contract_name=contract.name,
        model=contract.model,
        passed=all_passed,
        severity=effective_severity,
        results=results,
        duration_ms=duration_ms,
        consecutive_failures=consecutive_failures,
    )


def _send_contract_alerts(
    conn: duckdb.DuckDBPyConnection,
    contract: Contract,
    cr: ContractResult,
    alert_config: dict | None = None,
) -> None:
    """Send alerts for a failed contract based on notify channels.

    Only sends on severity='error' by default. If notify list is empty,
    no alerts are sent.
    """
    if cr.passed:
        return
    if not contract.notify:
        return
    # Only alert on error severity by default
    if cr.severity != "error":
        return

    try:
        from havn.engine.alerts import Alert, AlertConfig, send_alert

        failed_assertions = [r for r in cr.results if not r.get("passed")]
        assertion_list = ", ".join(r.get("expression", "?") for r in failed_assertions)

        alert = Alert(
            alert_type="assertion_failed",
            target=cr.model,
            message=(
                f"Contract '{cr.contract_name}' failed for `{cr.model}`: {assertion_list}"
                + (f" (consecutive failures: {cr.consecutive_failures})" if cr.consecutive_failures > 1 else "")
            ),
            details={
                "contract": cr.contract_name,
                "model": cr.model,
                "failed_assertions": len(failed_assertions),
                "consecutive_failures": cr.consecutive_failures,
                "severity": cr.severity,
            },
        )

        config = AlertConfig(
            slack_webhook_url=(alert_config or {}).get("slack_webhook_url"),
            webhook_url=(alert_config or {}).get("webhook_url"),
            channels=contract.notify,
        )

        send_alert(alert, config, conn)
    except Exception as e:
        logger.warning("Failed to send contract alert for %s: %s", contract.name, e)


def run_contracts(
    conn: duckdb.DuckDBPyConnection,
    contracts_dir: Path,
    targets: list[str] | None = None,
    alert_config: dict | None = None,
) -> list[ContractResult]:
    """Discover and run all contracts. Returns results for each contract.

    Args:
        conn: DuckDB connection.
        contracts_dir: Path to contracts/ directory.
        targets: Optional list of contract names or model names to filter.
        alert_config: Optional dict with slack_webhook_url/webhook_url for alerts.

    Returns:
        List of ContractResult for each evaluated contract.
    """
    ensure_meta_table(conn)
    _ensure_contracts_table(conn)

    contracts = discover_contracts(contracts_dir)
    if not contracts:
        return []

    # Filter if targets specified
    if targets:
        target_set = set(targets)
        contracts = [
            c for c in contracts
            if c.name in target_set or c.model in target_set
        ]

    results: list[ContractResult] = []
    for contract in contracts:
        cr = evaluate_contract(conn, contract)
        results.append(cr)

        # Save to metadata
        _save_contract_result(conn, cr)
        log_run(
            conn,
            "contract",
            f"{cr.contract_name}:{cr.model}",
            "success" if cr.passed else "failed",
            cr.duration_ms,
        )

        # Send alerts if configured
        _send_contract_alerts(conn, contract, cr, alert_config)

    return results


def get_contract_history(
    conn: duckdb.DuckDBPyConnection,
    limit: int = 100,
    model: str | None = None,
) -> list[dict]:
    """Get recent contract evaluation history.

    Args:
        conn: DuckDB connection.
        limit: Max results.
        model: Optional model name to filter history.
    """
    try:
        if model:
            rows = conn.execute(
                """
                SELECT contract_name, model, passed, severity, detail,
                       checked_at, consecutive_failures
                FROM _havn.contract_results
                WHERE model = ?
                ORDER BY checked_at DESC
                LIMIT ?
                """,
                [model, limit],
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT contract_name, model, passed, severity, detail,
                       checked_at, consecutive_failures
                FROM _havn.contract_results
                ORDER BY checked_at DESC
                LIMIT ?
                """,
                [limit],
            ).fetchall()
        return [
            {
                "contract_name": r[0],
                "model": r[1],
                "passed": r[2],
                "severity": r[3],
                "detail": r[4],
                "checked_at": str(r[5]),
                "consecutive_failures": r[6] if r[6] is not None else 0,
            }
            for r in rows
        ]
    except Exception:
        return []


def get_contract_model_history(
    conn: duckdb.DuckDBPyConnection,
    model: str,
    limit: int = 50,
) -> list[dict]:
    """Get contract evaluation history for a specific model."""
    return get_contract_history(conn, limit=limit, model=model)


def _ensure_contracts_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the contract results metadata table (no-op on read-only connections)."""
    try:
        conn.execute("CREATE SCHEMA IF NOT EXISTS _havn")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS _havn.contract_results (
                id                    VARCHAR DEFAULT gen_random_uuid()::VARCHAR,
                contract_name         VARCHAR NOT NULL,
                model                 VARCHAR NOT NULL,
                passed                BOOLEAN NOT NULL,
                severity              VARCHAR NOT NULL DEFAULT 'error',
                detail                JSON,
                checked_at            TIMESTAMP DEFAULT current_timestamp,
                consecutive_failures  INTEGER DEFAULT 0
            )
        """)
        # Migrate: add consecutive_failures column if table exists but column doesn't
        try:
            conn.execute("""
                ALTER TABLE _havn.contract_results
                ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER DEFAULT 0
            """)
        except Exception:
            pass
    except Exception:
        pass  # Read-only connection — table may already exist


def _save_contract_result(
    conn: duckdb.DuckDBPyConnection,
    cr: ContractResult,
) -> None:
    """Save a contract evaluation result to the metadata table."""
    import json
    conn.execute(
        """
        INSERT INTO _havn.contract_results
            (contract_name, model, passed, severity, detail, checked_at, consecutive_failures)
        VALUES (?, ?, ?, ?, ?::JSON, current_timestamp, ?)
        """,
        [cr.contract_name, cr.model, cr.passed, cr.severity,
         json.dumps(cr.results), cr.consecutive_failures],
    )
