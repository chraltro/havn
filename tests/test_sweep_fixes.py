"""Regression tests for the sweep fixes.

Each test pins a specific behavior that the sweep introduced or
strengthened, so future refactors do not silently regress these
correctness, security, and performance properties.
"""

from __future__ import annotations

import time
from pathlib import Path

import duckdb
import pytest
from fastapi import HTTPException

from havn.engine.circuit_breaker import CircuitBreaker, CircuitState
from havn.engine.scheduler import _cron_field_matches
from havn.engine.sql_analysis import parse_depends
from havn.engine.transform.discovery import discover_models


# ---------------------------------------------------------------------------
# SQL query validation (server/routes/query.py)
# ---------------------------------------------------------------------------


def _validator():
    from havn.server.routes.query import _validate_query_sql
    return _validate_query_sql


def test_validator_allows_simple_select():
    _validator()("SELECT 1")


def test_validator_allows_select_with_trailing_semicolon():
    _validator()("SELECT 1;")


def test_validator_allows_cte_select():
    _validator()(
        "WITH foo AS (SELECT 1) SELECT * FROM foo"
    )


def test_validator_blocks_cte_with_delete_tail():
    with pytest.raises(HTTPException) as exc:
        _validator()(
            "WITH foo AS (SELECT 1) DELETE FROM landing.events"
        )
    assert exc.value.status_code == 403


def test_validator_blocks_recursive_cte_with_update():
    with pytest.raises(HTTPException) as exc:
        _validator()(
            "WITH RECURSIVE r AS (SELECT 1) UPDATE landing.x SET y = 1"
        )
    assert exc.value.status_code == 403


def test_validator_blocks_multi_statement():
    with pytest.raises(HTTPException) as exc:
        _validator()("SELECT 1; DROP TABLE foo")
    assert exc.value.status_code == 403


def test_validator_ignores_keyword_in_string_literal():
    _validator()("SELECT 'DELETE FROM x' AS msg")


def test_validator_ignores_keyword_in_line_comment():
    _validator()("SELECT 1 -- DELETE FROM x\nFROM (SELECT 1) t")


def test_validator_ignores_keyword_in_block_comment():
    _validator()("SELECT /* DELETE FROM x */ 1")


def test_validator_blocks_read_csv():
    with pytest.raises(HTTPException) as exc:
        _validator()("SELECT * FROM read_csv('/etc/passwd')")
    assert exc.value.status_code == 403


def test_validator_blocks_quoted_read_csv():
    with pytest.raises(HTTPException) as exc:
        _validator()('SELECT * FROM "read_csv"(\'/etc/passwd\')')
    assert exc.value.status_code == 403


def test_validator_blocks_replacement_scan_file_read():
    # DuckDB reads a bare string path as a file via replacement scan — no
    # function call for the read_csv checks to catch.
    with pytest.raises(HTTPException) as exc:
        _validator()("SELECT * FROM '/etc/passwd'")
    assert exc.value.status_code == 403


def test_validator_blocks_replacement_scan_url():
    with pytest.raises(HTTPException) as exc:
        _validator()("SELECT * FROM 'https://example.com/data.csv'")
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT * FROM "/etc/passwd"',
        'SELECT * FROM "C:/Windows/win.ini"',
        'SELECT * FROM "C:\\Windows\\win.ini"',
        'SELECT * FROM "https://example.com/data.csv"',
        'SELECT * FROM "s3://bucket/secret.parquet"',
        'SELECT * FROM "./relative.csv"',
        'SELECT * FROM ("/etc/passwd")',
        'SELECT * FROM gold.x JOIN "/etc/passwd" ON 1=1',
    ],
)
def test_validator_blocks_double_quoted_replacement_scan(sql):
    # DuckDB resolves a double-quoted path in table position as a replacement
    # scan too, so the single-quote rule alone left a trivial bypass.
    with pytest.raises(HTTPException) as exc:
        _validator()(sql)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT * FROM gold."my orders"',
        'SELECT * FROM "gold"."orders"',
        'SELECT * FROM "MyTable"',
        'SELECT "weird col" FROM gold.orders',
        "SELECT * FROM gold.orders WHERE name = 'a/b'",
    ],
)
def test_validator_allows_plain_quoted_identifiers(sql):
    # Only path-shaped quoted names are a replacement scan; ordinary quoted
    # identifiers (and slashes inside string values) must keep working.
    _validator()(sql)


def test_validator_blocks_parenthesised_replacement_scan():
    with pytest.raises(HTTPException) as exc:
        _validator()("SELECT * FROM ('/tmp/secret.parquet')")
    assert exc.value.status_code == 403


def test_validator_allows_string_literal_in_projection():
    # A string literal that is not in table position is fine.
    _validator()("SELECT 'hello' AS greeting FROM landing.events")


def test_validator_passes_unknown_keyword_to_duckdb():
    # Unknown leading keywords are not rejected: DuckDB will produce
    # the correct parse error so the user sees a 400, not a misleading 403.
    _validator()("INVALID SQL FOOBAR")


# ---------------------------------------------------------------------------
# Cron parsing (engine/scheduler.py)
# ---------------------------------------------------------------------------


def test_cron_star_matches():
    assert _cron_field_matches("*", 0) is True
    assert _cron_field_matches("*", 59) is True


def test_cron_literal_matches():
    assert _cron_field_matches("5", 5) is True
    assert _cron_field_matches("5", 6) is False


def test_cron_list():
    assert _cron_field_matches("1,15,30", 15) is True
    assert _cron_field_matches("1,15,30", 14) is False


def test_cron_range():
    assert _cron_field_matches("10-20", 15) is True
    assert _cron_field_matches("10-20", 21) is False


def test_cron_step_on_wildcard():
    assert _cron_field_matches("*/5", 0) is True
    assert _cron_field_matches("*/5", 5) is True
    assert _cron_field_matches("*/5", 7) is False


def test_cron_range_with_step():
    # 0-29/5 -> 0,5,10,15,20,25
    assert _cron_field_matches("0-29/5", 10) is True
    assert _cron_field_matches("0-29/5", 11) is False
    assert _cron_field_matches("0-29/5", 30) is False


def test_cron_zero_step_does_not_match():
    assert _cron_field_matches("*/0", 0) is False


def test_cron_negative_step_does_not_match():
    assert _cron_field_matches("*/-1", 0) is False


def test_cron_combined_list_with_range():
    assert _cron_field_matches("1-3,10-12", 2) is True
    assert _cron_field_matches("1-3,10-12", 11) is True
    assert _cron_field_matches("1-3,10-12", 7) is False


# ---------------------------------------------------------------------------
# Depends_on parsing (sql_analysis.parse_depends)
# ---------------------------------------------------------------------------


def test_parse_depends_collects_all_lines():
    sql = (
        "@depends_on bronze.a\n"
        "@depends_on bronze.b, bronze.c\n"
        "SELECT * FROM bronze.a"
    )
    assert parse_depends(sql) == ["bronze.a", "bronze.b", "bronze.c"]


def test_parse_depends_dedupes():
    sql = "@depends_on bronze.a\n@depends_on bronze.a, bronze.b\nSELECT 1"
    assert parse_depends(sql) == ["bronze.a", "bronze.b"]


def test_parse_depends_legacy_appended():
    sql = "@depends_on bronze.a\n-- depends_on: bronze.b\nSELECT 1"
    assert parse_depends(sql) == ["bronze.a", "bronze.b"]


# ---------------------------------------------------------------------------
# Auto-extract + explicit @depends_on union (transform/discovery)
# ---------------------------------------------------------------------------


def test_discover_models_unions_explicit_with_auto(tmp_path):
    transform_dir = tmp_path / "transform"
    (transform_dir / "silver").mkdir(parents=True)
    (transform_dir / "silver" / "joined.sql").write_text(
        "@depends_on bronze.users\n"
        "SELECT u.id, e.event\n"
        "FROM bronze.users u\n"
        "LEFT JOIN bronze.events e ON e.user_id = u.id\n"
    )
    models = discover_models(transform_dir)
    assert len(models) == 1
    deps = set(models[0].depends_on)
    # explicit dep preserved AND auto-extracted dep added
    assert "bronze.users" in deps
    assert "bronze.events" in deps


# ---------------------------------------------------------------------------
# Circuit breaker windowed decay (engine/circuit_breaker)
# ---------------------------------------------------------------------------


def test_circuit_breaker_window_decays_old_failures():
    cb = CircuitBreaker(
        failure_threshold=3,
        recovery_timeout=60,
        failure_window_seconds=0.05,
    )

    def boom():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        cb.execute("flaky", boom)
    with pytest.raises(RuntimeError):
        cb.execute("flaky", boom)
    assert cb.get_state("flaky") == CircuitState.CLOSED
    time.sleep(0.1)
    with pytest.raises(RuntimeError):
        cb.execute("flaky", boom)
    # Decayed: this third failure should not have tripped the breaker.
    assert cb.get_state("flaky") == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# Webhook auth
# ---------------------------------------------------------------------------


def test_webhook_double_quoted_table(shared_client, shared_project, monkeypatch):
    monkeypatch.setenv("HAVN_WEBHOOK_SECRET", "abc")
    resp = shared_client.post(
        "/api/webhook/select_events",
        json={"event": "test"},
        headers={"X-Havn-Webhook-Secret": "abc"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "select_events_inbox" in data["table"]


# ---------------------------------------------------------------------------
# Path traversal (files.py)
# ---------------------------------------------------------------------------


def test_read_file_rejects_traversal(shared_client, shared_project):
    resp = shared_client.get("/api/files/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404)


def test_read_file_rejects_absolute_path(shared_client, shared_project):
    resp = shared_client.get("/api/files/etc/passwd")
    assert resp.status_code in (400, 404)


def test_read_file_blocks_dotfile(shared_client, shared_project):
    (shared_project / ".env").write_text("SECRET=value\n")
    resp = shared_client.get("/api/files/.env")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# WriteQueue does not hang on cancelled future
# ---------------------------------------------------------------------------


def test_write_queue_survives_cancelled_future(tmp_path):
    from concurrent.futures import Future
    from havn.engine.write_queue import WriteQueue
    from havn.config import DatabaseConfig
    from havn.engine.backends import create_backend

    cfg = DatabaseConfig(path=str(tmp_path / "wq.duckdb"))
    backend = create_backend(cfg, project_dir=tmp_path)
    wq = WriteQueue(backend)
    try:
        # Submit a task whose future is cancelled by the caller before
        # the worker delivers the result. The worker must not crash.
        f1: Future = wq.submit(lambda c: c.execute("SELECT 1").fetchone())
        f1.cancel()
        try:
            f1.result(timeout=2)
        except Exception:
            pass
        # The worker is still alive: a follow-up submit must succeed.
        f2 = wq.submit(lambda c: c.execute("SELECT 42").fetchone())
        result = f2.result(timeout=5)
        assert result[0] == 42
    finally:
        wq.close()


# ---------------------------------------------------------------------------
# Incremental: validate unique_key
# ---------------------------------------------------------------------------


def test_snapshots_reject_malicious_model_name(tmp_path):
    """capture_snapshot must refuse a model name containing SQL terminators."""
    from havn.engine.snapshots import capture_snapshot, start_run

    db_path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE SCHEMA IF NOT EXISTS bronze")
    conn.execute("CREATE TABLE bronze.t AS SELECT 1 AS id")

    run_id = start_run(tmp_path, trigger="test")
    ok = capture_snapshot(
        tmp_path, conn,
        'bronze.t"; DROP TABLE bronze.t; --',
        run_id, row_count=1,
    )
    conn.close()
    assert ok is False


def test_versioning_rejects_traversal_in_table_name(tmp_path):
    """create_version must skip table refs containing path-traversal characters."""
    from havn.engine.versioning import create_version

    db_path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE SCHEMA IF NOT EXISTS bronze")
    conn.execute("CREATE TABLE bronze.t AS SELECT 1 AS id")
    result = create_version(
        conn, tmp_path,
        description="evil",
        tables=["../etc/passwd", "bronze.t"],
    )
    conn.close()
    expected = result["tables"]
    assert "bronze.t" in expected
    assert "../etc/passwd" not in expected


def test_mask_partial_handles_zero_show_last():
    from havn.engine.masking import mask_partial

    assert mask_partial("abcdefgh", show_first=2, show_last=0) == "ab******"
    assert mask_partial("abcdefgh", show_first=0, show_last=2) == "******gh"
    assert mask_partial("abc", show_first=5, show_last=5) == "abc"
    assert mask_partial(None) is None


def test_freshness_minutes_is_minutes_not_months(tmp_path):
    """The freshness 'm' unit must mean minutes, matching industry convention."""
    from havn.engine.contracts import _evaluate_freshness
    from havn.engine.database import ensure_meta_table

    db_path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    ensure_meta_table(conn)
    conn.execute(
        "INSERT INTO _havn.model_state (model_path, content_hash, upstream_hash, "
        "materialized_as, row_count, run_duration_ms, last_run_at) "
        "VALUES (?, ?, ?, ?, ?, ?, current_timestamp - INTERVAL '10 minutes')",
        ["bronze.x", "h", "h", "table", 0, 0],
    )
    passed, detail = _evaluate_freshness(conn, "bronze.x", "freshness < 5m")
    conn.close()
    assert passed is False
    assert "ago" in detail or "hour" in detail or "minute" in detail


def test_cdc_reset_returns_row_count(tmp_path):
    from havn.engine.cdc import ensure_cdc_table, update_watermark, reset_watermark

    conn = duckdb.connect(str(tmp_path / "wh.duckdb"))
    ensure_cdc_table(conn)
    update_watermark(conn, "conn1", "t1", "high_watermark", "2025-01-01", rows_synced=10)
    update_watermark(conn, "conn1", "t2", "high_watermark", "2025-01-01", rows_synced=5)
    removed = reset_watermark(conn, "conn1")
    conn.close()
    assert removed == 2


def test_unique_key_validates_identifiers(tmp_path):
    """A unique_key containing a non-identifier value should fail loudly,
    not silently splice into SQL."""
    from havn.engine.database import ensure_meta_table
    from havn.engine.transform.execution import _execute_incremental
    from havn.engine.transform.models import SQLModel

    db_path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    ensure_meta_table(conn)
    conn.execute("CREATE SCHEMA IF NOT EXISTS bronze")
    conn.execute("CREATE TABLE bronze.t AS SELECT 1 AS id, 'x' AS val")

    model = SQLModel(
        path=Path(str(tmp_path / "model.sql")),
        name="t",
        schema="bronze",
        full_name="bronze.t",
        sql="@config materialized=incremental, unique_key=id; DROP TABLE evil --",
        query="SELECT 1 AS id, 'x' AS val",
        materialized="incremental",
        depends_on=[],
        unique_key='id; DROP TABLE evil --',
        incremental_strategy="delete+insert",
    )
    with pytest.raises(ValueError):
        _execute_incremental(conn, model)
    conn.close()
