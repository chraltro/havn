"""SCD2 snapshot models: `@config materialized=snapshot`.

Naming note: these are *snapshot models*, which keep a row-level history of a
source table. They have nothing to do with `havn snapshot` / `havn rewind`,
which capture whole-warehouse restore points.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from havn.engine.transform import (
    SQLModel,
    SnapshotError,
    SnapshotSettings,
    _execute_snapshot,
    discover_models,
    execute_model,
    run_transform,
    validate_models,
)
from havn.engine.transform.execution import (
    SchemaChangeError,
    snapshot_settings_from_config,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    c.execute("CREATE SCHEMA landing")
    c.execute("CREATE SCHEMA silver")
    yield c
    c.close()


def make_model(query: str, **kwargs) -> SQLModel:
    """A snapshot model over ``query``, with sane defaults for the tests."""
    kwargs.setdefault("unique_key", "customer_id")
    return SQLModel(
        path=Path("transform/silver/dim_customer.sql"),
        name="dim_customer",
        schema="silver",
        full_name="silver.dim_customer",
        sql="",
        query=query,
        materialized="snapshot",
        **kwargs,
    )


def seed(conn: duckdb.DuckDBPyConnection, rows: list[tuple]) -> None:
    """Replace landing.customers with ``rows``."""
    conn.execute("DELETE FROM landing.customers")
    for row in rows:
        conn.execute(
            "INSERT INTO landing.customers VALUES (?, ?, ?)", list(row)
        )


@pytest.fixture()
def customers(conn: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    conn.execute(
        "CREATE TABLE landing.customers "
        "(customer_id INTEGER, name VARCHAR, tier VARCHAR)"
    )
    return conn


BASE_QUERY = "SELECT customer_id, name, tier FROM landing.customers"


def history(conn: duckdb.DuckDBPyConnection, *extra: str) -> list[tuple]:
    cols = ", ".join(["customer_id", "name", "tier", "is_current", *extra])
    return conn.execute(
        f"SELECT {cols} FROM silver.dim_customer "
        "ORDER BY customer_id, valid_from, rowid"
    ).fetchall()


# ---------------------------------------------------------------------------
# Initial load
# ---------------------------------------------------------------------------


def test_initial_load_creates_meta_columns(customers):
    seed(customers, [(1, "Ann", "gold"), (2, "Bo", "silver")])
    model = make_model(BASE_QUERY)

    _, row_count = _execute_snapshot(customers, model)

    assert row_count == 2
    cols = [
        r[0]
        for r in customers.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'silver' AND table_name = 'dim_customer' "
            "ORDER BY ordinal_position"
        ).fetchall()
    ]
    assert cols == [
        "customer_id", "name", "tier",
        "valid_from", "valid_to", "is_current", "row_hash",
    ]
    assert history(customers) == [
        (1, "Ann", "gold", True),
        (2, "Bo", "silver", True),
    ]
    open_valid_to = customers.execute(
        "SELECT DISTINCT valid_to FROM silver.dim_customer"
    ).fetchall()
    assert open_valid_to == [(None,)]


def test_initial_load_with_new_record_adds_is_deleted(customers):
    seed(customers, [(1, "Ann", "gold")])
    model = make_model(BASE_QUERY, hard_deletes="new_record")

    _execute_snapshot(customers, model)

    assert customers.execute(
        "SELECT is_deleted FROM silver.dim_customer"
    ).fetchall() == [(False,)]


# ---------------------------------------------------------------------------
# Change / delete / new across runs, per hard_deletes mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy", ["ignore", "invalidate", "new_record"])
def test_change_and_new_key_are_versioned(customers, policy):
    model = make_model(BASE_QUERY, hard_deletes=policy)
    seed(customers, [(1, "Ann", "gold"), (2, "Bo", "silver")])
    _execute_snapshot(customers, model)

    # Ann unchanged, Bo upgraded, Dee new.
    seed(customers, [(1, "Ann", "gold"), (2, "Bo", "gold"), (4, "Dee", "bronze")])
    _execute_snapshot(customers, model)

    rows = history(customers)
    assert rows == [
        (1, "Ann", "gold", True),
        (2, "Bo", "silver", False),
        (2, "Bo", "gold", True),
        (4, "Dee", "bronze", True),
    ]


def test_hard_delete_ignore_leaves_the_row_current(customers):
    model = make_model(BASE_QUERY, hard_deletes="ignore")
    seed(customers, [(1, "Ann", "gold"), (3, "Cy", "bronze")])
    _execute_snapshot(customers, model)

    seed(customers, [(1, "Ann", "gold")])
    _execute_snapshot(customers, model)

    assert history(customers) == [
        (1, "Ann", "gold", True),
        (3, "Cy", "bronze", True),
    ]


def test_hard_delete_invalidate_closes_the_row(customers):
    model = make_model(BASE_QUERY, hard_deletes="invalidate")
    seed(customers, [(1, "Ann", "gold"), (3, "Cy", "bronze")])
    _execute_snapshot(customers, model)

    seed(customers, [(1, "Ann", "gold")])
    _execute_snapshot(customers, model)

    assert history(customers) == [
        (1, "Ann", "gold", True),
        (3, "Cy", "bronze", False),
    ]
    assert customers.execute(
        "SELECT valid_to IS NOT NULL FROM silver.dim_customer "
        "WHERE customer_id = 3"
    ).fetchone() == (True,)


def test_hard_delete_new_record_appends_a_tombstone(customers):
    model = make_model(BASE_QUERY, hard_deletes="new_record")
    seed(customers, [(1, "Ann", "gold"), (3, "Cy", "bronze")])
    _execute_snapshot(customers, model)

    seed(customers, [(1, "Ann", "gold")])
    _execute_snapshot(customers, model)

    assert history(customers, "is_deleted") == [
        (1, "Ann", "gold", True, False),
        (3, "Cy", "bronze", False, False),
        (3, "Cy", "bronze", True, True),
    ]


def test_new_record_tombstone_is_not_re_emitted(customers):
    """A key that stays gone must not grow a tombstone per run."""
    model = make_model(BASE_QUERY, hard_deletes="new_record")
    seed(customers, [(1, "Ann", "gold"), (3, "Cy", "bronze")])
    _execute_snapshot(customers, model)
    seed(customers, [(1, "Ann", "gold")])
    _execute_snapshot(customers, model)
    before = history(customers, "is_deleted")

    _execute_snapshot(customers, model)
    _execute_snapshot(customers, model)

    assert history(customers, "is_deleted") == before


def test_new_record_revives_a_returning_key(customers):
    model = make_model(BASE_QUERY, hard_deletes="new_record")
    seed(customers, [(3, "Cy", "bronze")])
    _execute_snapshot(customers, model)
    seed(customers, [])
    _execute_snapshot(customers, model)
    seed(customers, [(3, "Cy", "bronze")])
    _execute_snapshot(customers, model)

    rows = history(customers, "is_deleted")
    assert rows == [
        (3, "Cy", "bronze", False, False),
        (3, "Cy", "bronze", False, True),
        (3, "Cy", "bronze", True, False),
    ]


# ---------------------------------------------------------------------------
# Idempotency and reverts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy", ["ignore", "invalidate", "new_record"])
def test_identical_replay_is_a_no_op(customers, policy):
    model = make_model(BASE_QUERY, hard_deletes=policy)
    seed(customers, [(1, "Ann", "gold"), (2, "Bo", "silver")])
    _execute_snapshot(customers, model)
    seed(customers, [(1, "Ann", "gold"), (2, "Bo", "gold")])
    _execute_snapshot(customers, model)
    before = history(customers)

    _execute_snapshot(customers, model)
    _execute_snapshot(customers, model)

    assert history(customers) == before


def test_a_to_b_to_a_produces_three_versions(customers):
    model = make_model(BASE_QUERY)
    for tier in ("gold", "silver", "gold"):
        seed(customers, [(1, "Ann", tier)])
        _execute_snapshot(customers, model)

    assert history(customers) == [
        (1, "Ann", "gold", False),
        (1, "Ann", "silver", False),
        (1, "Ann", "gold", True),
    ]


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def test_timestamp_strategy_dates_versions_from_the_source_clock(conn):
    conn.execute(
        "CREATE TABLE landing.customers "
        "(customer_id INTEGER, tier VARCHAR, changed_at TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO landing.customers VALUES (1, 'gold', TIMESTAMP '2024-01-01')"
    )
    model = make_model(
        "SELECT customer_id, tier, changed_at FROM landing.customers",
        strategy="timestamp",
        updated_at="changed_at",
    )
    _execute_snapshot(conn, model)
    # Same updated_at: no new version, even though the run is later.
    _execute_snapshot(conn, model)
    assert conn.execute("SELECT count(*) FROM silver.dim_customer").fetchone() == (1,)

    conn.execute(
        "UPDATE landing.customers SET tier = 'plat', "
        "changed_at = TIMESTAMP '2024-02-01'"
    )
    _execute_snapshot(conn, model)

    assert conn.execute(
        "SELECT tier, valid_from, valid_to, is_current FROM silver.dim_customer "
        "ORDER BY valid_from"
    ).fetchall() == [
        ("gold", __import__("datetime").datetime(2024, 1, 1),
         __import__("datetime").datetime(2024, 2, 1), False),
        ("plat", __import__("datetime").datetime(2024, 2, 1), None, True),
    ]
    # row_hash is stored under the timestamp strategy too.
    assert all(
        r[0] for r in conn.execute(
            "SELECT row_hash IS NOT NULL FROM silver.dim_customer"
        ).fetchall()
    )


def test_check_cols_subset_ignores_untracked_columns(conn):
    conn.execute(
        "CREATE TABLE landing.customers "
        "(customer_id INTEGER, tier VARCHAR, note VARCHAR)"
    )
    conn.execute("INSERT INTO landing.customers VALUES (1, 'gold', 'first')")
    model = make_model(
        "SELECT customer_id, tier, note FROM landing.customers",
        check_cols="tier",
    )
    _execute_snapshot(conn, model)

    conn.execute("UPDATE landing.customers SET note = 'second'")
    _execute_snapshot(conn, model)
    assert conn.execute("SELECT count(*) FROM silver.dim_customer").fetchone() == (1,)

    conn.execute("UPDATE landing.customers SET tier = 'plat'")
    _execute_snapshot(conn, model)
    assert conn.execute("SELECT count(*) FROM silver.dim_customer").fetchone() == (2,)


def test_check_cols_naming_a_missing_column_is_refused(customers):
    seed(customers, [(1, "Ann", "gold")])
    model = make_model(BASE_QUERY, check_cols="nope")

    with pytest.raises(SnapshotError, match="check_cols names column"):
        _execute_snapshot(customers, model)


# ---------------------------------------------------------------------------
# Key uniqueness
# ---------------------------------------------------------------------------


def test_duplicate_key_is_refused_and_target_is_untouched(customers):
    model = make_model(BASE_QUERY)
    seed(customers, [(1, "Ann", "gold")])
    _execute_snapshot(customers, model)
    before = history(customers)

    customers.execute("INSERT INTO landing.customers VALUES (1, 'Ann', 'plat')")
    with pytest.raises(SnapshotError, match="is not unique"):
        _execute_snapshot(customers, model)

    assert history(customers) == before


def test_duplicate_key_on_the_first_run_creates_nothing(customers):
    seed(customers, [(1, "Ann", "gold"), (1, "Ann", "plat")])
    model = make_model(BASE_QUERY)

    with pytest.raises(SnapshotError, match="is not unique"):
        _execute_snapshot(customers, model)

    assert customers.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = 'silver' AND table_name = 'dim_customer'"
    ).fetchone() == (0,)


def test_null_key_is_matched_against_its_own_history(customers):
    model = make_model(BASE_QUERY)
    seed(customers, [(None, "Ann", "gold")])
    _execute_snapshot(customers, model)
    _execute_snapshot(customers, model)

    assert customers.execute(
        "SELECT count(*) FROM silver.dim_customer"
    ).fetchone() == (1,)


# ---------------------------------------------------------------------------
# Custom meta column names and the valid_to sentinel
# ---------------------------------------------------------------------------


DBT_SETTINGS = SnapshotSettings(
    valid_from="dbt_valid_from",
    valid_to="dbt_valid_to",
    is_current="dbt_is_current",
    row_hash="dbt_scd_id",
    is_deleted="dbt_is_deleted",
    valid_to_current="'9999-12-31'::TIMESTAMP",
)


def test_custom_meta_columns_and_sentinel(customers):
    model = make_model(BASE_QUERY)
    seed(customers, [(1, "Ann", "gold")])
    _execute_snapshot(customers, model, settings=DBT_SETTINGS)
    seed(customers, [(1, "Ann", "plat")])
    _execute_snapshot(customers, model, settings=DBT_SETTINGS)

    rows = customers.execute(
        "SELECT tier, dbt_valid_to, dbt_is_current FROM silver.dim_customer "
        "ORDER BY dbt_valid_from"
    ).fetchall()
    import datetime

    assert rows[0][0] == "gold" and rows[0][2] is False
    assert rows[0][1] < datetime.datetime(9999, 12, 31)
    assert rows[1] == ("plat", datetime.datetime(9999, 12, 31), True)


def test_sentinel_snapshot_is_still_idempotent(customers):
    model = make_model(BASE_QUERY, hard_deletes="new_record")
    seed(customers, [(1, "Ann", "gold")])
    _execute_snapshot(customers, model, settings=DBT_SETTINGS)
    before = customers.execute(
        "SELECT count(*) FROM silver.dim_customer"
    ).fetchone()

    _execute_snapshot(customers, model, settings=DBT_SETTINGS)

    assert customers.execute(
        "SELECT count(*) FROM silver.dim_customer"
    ).fetchone() == before


def test_a_query_column_clashing_with_a_meta_column_is_refused(conn):
    conn.execute(
        "CREATE TABLE landing.customers (customer_id INTEGER, is_current BOOLEAN)"
    )
    conn.execute("INSERT INTO landing.customers VALUES (1, TRUE)")
    model = make_model("SELECT customer_id, is_current FROM landing.customers")

    with pytest.raises(SnapshotError, match="collide with the snapshot"):
        _execute_snapshot(conn, model)


def test_snapshot_settings_from_project_config(tmp_path):
    (tmp_path / "project.yml").write_text(
        "name: p\n"
        "snapshots:\n"
        "  meta_columns:\n"
        "    valid_from: dbt_valid_from\n"
        "    valid_to: dbt_valid_to\n"
        "  valid_to_current: \"'9999-12-31'::TIMESTAMP\"\n"
    )
    from havn.config import load_project

    settings = snapshot_settings_from_config(load_project(tmp_path))
    assert settings.valid_from == "dbt_valid_from"
    assert settings.valid_to == "dbt_valid_to"
    assert settings.is_current == "is_current"
    assert settings.valid_to_current == "'9999-12-31'::TIMESTAMP"


def test_unknown_meta_column_role_is_refused(tmp_path):
    (tmp_path / "project.yml").write_text(
        "name: p\nsnapshots:\n  meta_columns:\n    valid_form: oops\n"
    )
    from havn.config import load_project

    with pytest.raises(SnapshotError, match="unknown meta column"):
        snapshot_settings_from_config(load_project(tmp_path))


def test_non_literal_sentinel_is_refused(tmp_path):
    (tmp_path / "project.yml").write_text(
        "name: p\nsnapshots:\n"
        "  valid_to_current: \"(SELECT max(x) FROM t); DROP TABLE t\"\n"
    )
    from havn.config import load_project

    with pytest.raises(SnapshotError, match="must be a literal"):
        snapshot_settings_from_config(load_project(tmp_path))


# ---------------------------------------------------------------------------
# Schema evolution
# ---------------------------------------------------------------------------


def test_new_source_column_is_appended_with_null_history(conn):
    conn.execute(
        "CREATE TABLE landing.customers "
        "(customer_id INTEGER, tier VARCHAR, region VARCHAR)"
    )
    conn.execute("INSERT INTO landing.customers VALUES (1, 'gold', 'eu')")
    narrow = make_model("SELECT customer_id, tier FROM landing.customers")
    _execute_snapshot(conn, narrow)

    wide = make_model("SELECT customer_id, tier, region FROM landing.customers")
    _execute_snapshot(conn, wide)

    assert conn.execute(
        "SELECT tier, region, is_current FROM silver.dim_customer "
        "ORDER BY valid_from, rowid"
    ).fetchall() == [
        ("gold", None, False),
        ("gold", "eu", True),
    ]


def test_removed_source_column_is_refused(conn):
    conn.execute(
        "CREATE TABLE landing.customers "
        "(customer_id INTEGER, tier VARCHAR, region VARCHAR)"
    )
    conn.execute("INSERT INTO landing.customers VALUES (1, 'gold', 'eu')")
    wide = make_model("SELECT customer_id, tier, region FROM landing.customers")
    _execute_snapshot(conn, wide)
    before = conn.execute("SELECT count(*) FROM silver.dim_customer").fetchone()

    narrow = make_model("SELECT customer_id, tier FROM landing.customers")
    with pytest.raises(SchemaChangeError, match="region"):
        _execute_snapshot(conn, narrow)

    assert conn.execute(
        "SELECT count(*) FROM silver.dim_customer"
    ).fetchone() == before


def test_retyped_source_column_is_refused(conn):
    conn.execute("CREATE TABLE landing.customers (customer_id INTEGER, tier VARCHAR)")
    conn.execute("INSERT INTO landing.customers VALUES (1, 'gold')")
    model = make_model("SELECT customer_id, tier FROM landing.customers")
    _execute_snapshot(conn, model)

    retyped = make_model(
        "SELECT customer_id, CAST(1 AS INTEGER) AS tier FROM landing.customers"
    )
    with pytest.raises(SchemaChangeError, match="tier"):
        _execute_snapshot(conn, retyped)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def write_project(tmp_path: Path, files: dict[str, str]) -> Path:
    transform = tmp_path / "transform"
    for rel, body in files.items():
        target = transform / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return transform


def errors_for(tmp_path: Path, body: str) -> list[str]:
    transform = write_project(tmp_path, {"silver/snap.sql": body})
    models = discover_models(transform)
    return [
        e.message for e in validate_models(None, models) if e.severity == "error"
    ]


def test_validate_requires_unique_key(tmp_path):
    messages = errors_for(
        tmp_path, "@config materialized=snapshot\nSELECT 1 AS id\n"
    )
    assert any("unique_key" in m for m in messages)


def test_validate_timestamp_requires_updated_at(tmp_path):
    messages = errors_for(
        tmp_path,
        "@config materialized=snapshot, unique_key=id, strategy=timestamp\n"
        "SELECT 1 AS id\n",
    )
    assert any("updated_at" in m for m in messages)


def test_validate_check_cols_only_with_check(tmp_path):
    messages = errors_for(
        tmp_path,
        "@config materialized=snapshot, unique_key=id, strategy=timestamp, "
        "updated_at=ts, check_cols=a\nSELECT 1 AS id\n",
    )
    assert any("check_cols only applies" in m for m in messages)


def test_validate_rejects_unknown_strategy_and_hard_deletes(tmp_path):
    messages = errors_for(
        tmp_path,
        "@config materialized=snapshot, unique_key=id, strategy=chekc, "
        "hard_deletes=delete\nSELECT 1 AS id\n",
    )
    assert any("Unknown snapshot strategy" in m for m in messages)
    assert any("Unknown hard_deletes policy" in m for m in messages)


def test_validate_rejects_incremental_filter_on_a_snapshot(tmp_path):
    messages = errors_for(
        tmp_path,
        "@config materialized=snapshot, unique_key=id, "
        "incremental_filter=WHERE 1=1\nSELECT 1 AS id\n",
    )
    assert any("not supported on a snapshot" in m for m in messages)


def test_snapshot_config_is_folded_into_the_content_hash():
    base = make_model(BASE_QUERY)
    assert make_model(BASE_QUERY).content_hash == base.content_hash
    assert make_model(BASE_QUERY, hard_deletes="invalidate").content_hash != base.content_hash
    assert make_model(BASE_QUERY, check_cols="tier").content_hash != base.content_hash
    assert make_model(
        BASE_QUERY, strategy="timestamp", updated_at="ts"
    ).content_hash != base.content_hash


# ---------------------------------------------------------------------------
# End to end through run_transform
# ---------------------------------------------------------------------------


SNAPSHOT_SQL = """@config materialized=snapshot, unique_key=customer_id, hard_deletes=invalidate

SELECT customer_id, name, tier FROM landing.customers
"""

DOWNSTREAM_SQL = """@config materialized=table, schema=gold

SELECT customer_id, tier FROM silver.dim_customer WHERE is_current
"""


def _project(tmp_path: Path) -> tuple[Path, duckdb.DuckDBPyConnection]:
    transform = write_project(
        tmp_path,
        {"silver/dim_customer.sql": SNAPSHOT_SQL, "gold/current.sql": DOWNSTREAM_SQL},
    )
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA landing")
    conn.execute(
        "CREATE TABLE landing.customers "
        "(customer_id INTEGER, name VARCHAR, tier VARCHAR)"
    )
    return transform, conn


def test_run_transform_builds_a_snapshot_and_its_consumer(tmp_path):
    transform, conn = _project(tmp_path)
    seed(conn, [(1, "Ann", "gold"), (2, "Bo", "silver")])

    results = run_transform(conn, transform, project_dir=tmp_path)

    assert results["silver.dim_customer"] == "built"
    assert results["gold.current"] == "built"
    assert conn.execute("SELECT count(*) FROM gold.current").fetchone() == (2,)
    conn.close()


def test_force_does_not_drop_snapshot_history(tmp_path):
    transform, conn = _project(tmp_path)
    seed(conn, [(1, "Ann", "gold")])
    run_transform(conn, transform, project_dir=tmp_path)
    seed(conn, [(1, "Ann", "plat")])
    run_transform(conn, transform, force=True, project_dir=tmp_path)
    before = conn.execute("SELECT count(*) FROM silver.dim_customer").fetchone()
    assert before == (2,)

    # Forcing again re-runs the merge; with the source unchanged it adds
    # nothing, and above all it does not start history over.
    run_transform(conn, transform, force=True, project_dir=tmp_path)

    assert conn.execute(
        "SELECT count(*) FROM silver.dim_customer"
    ).fetchone() == before
    conn.close()


def test_snapshot_is_profiled_like_a_table(tmp_path):
    transform, conn = _project(tmp_path)
    seed(conn, [(1, "Ann", "gold")])
    run_transform(conn, transform, project_dir=tmp_path)

    assert conn.execute(
        "SELECT count(*) FROM _havn.model_profiles WHERE model_path = ?",
        ["silver.dim_customer"],
    ).fetchone()[0] > 0
    conn.close()


def test_assertions_run_against_the_snapshot_table(tmp_path):
    transform = write_project(
        tmp_path,
        {
            "silver/dim_customer.sql": (
                "@config materialized=snapshot, unique_key=customer_id\n"
                "@assert COUNT(*) FILTER (WHERE is_current) > 0\n\n"
                "SELECT customer_id, name, tier FROM landing.customers\n"
            )
        },
    )
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA landing")
    conn.execute(
        "CREATE TABLE landing.customers "
        "(customer_id INTEGER, name VARCHAR, tier VARCHAR)"
    )
    seed(conn, [(1, "Ann", "gold")])

    results = run_transform(conn, transform, project_dir=tmp_path)

    assert results["silver.dim_customer"] == "built"
    conn.close()


def test_a_view_at_the_target_name_is_replaced_by_the_history_table(customers):
    customers.execute("CREATE VIEW silver.dim_customer AS SELECT 1 AS x")
    seed(customers, [(1, "Ann", "gold")])

    _execute_snapshot(customers, make_model(BASE_QUERY))

    assert customers.execute(
        "SELECT table_type FROM information_schema.tables "
        "WHERE table_schema = 'silver' AND table_name = 'dim_customer'"
    ).fetchone() == ("BASE TABLE",)


def test_execute_model_routes_snapshots(customers):
    seed(customers, [(1, "Ann", "gold")])

    _, row_count = execute_model(customers, make_model(BASE_QUERY))

    assert row_count == 1


# ---------------------------------------------------------------------------
# Bind pass
# ---------------------------------------------------------------------------


def test_bind_pass_exposes_snapshot_meta_columns(tmp_path):
    from havn.engine.transform.bind import bind_models

    transform, conn = _project(tmp_path)
    models = discover_models(transform)

    result = bind_models(conn, models, project_dir=tmp_path)

    assert result.ok, result.errors
    snapshot_cols = dict(result.schemas["silver.dim_customer"])
    assert snapshot_cols["valid_from"] == "TIMESTAMP"
    assert snapshot_cols["is_current"] == "BOOLEAN"
    assert "gold.current" in result.schemas
    conn.close()


def test_bind_pass_exposes_is_deleted_for_new_record(tmp_path):
    from havn.engine.transform.bind import bind_models

    transform = write_project(
        tmp_path,
        {
            "silver/dim_customer.sql": (
                "@config materialized=snapshot, unique_key=customer_id, "
                "hard_deletes=new_record\n"
                "SELECT customer_id, name, tier FROM landing.customers\n"
            ),
            "gold/live.sql": (
                "@config materialized=view, schema=gold\n"
                "SELECT customer_id FROM silver.dim_customer "
                "WHERE is_current AND NOT is_deleted\n"
            ),
        },
    )
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA landing")
    conn.execute(
        "CREATE TABLE landing.customers "
        "(customer_id INTEGER, name VARCHAR, tier VARCHAR)"
    )

    result = bind_models(conn, discover_models(transform), project_dir=tmp_path)

    assert result.ok, result.errors
    conn.close()
