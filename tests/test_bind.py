"""Tests for the shadow-catalog bind pass."""

from __future__ import annotations

import time

import duckdb
import pytest

from havn.engine.database import ensure_meta_table
from havn.engine.transform import discover_models
from havn.engine.transform.bind import (
    ancestor_closure,
    as_validation_message,
    bind_models,
    extract_position,
    model_from_buffer,
)


@pytest.fixture
def project(tmp_path):
    """A project with landing tables and a transform tree, nothing built."""
    (tmp_path / "project.yml").write_text(
        "name: bindtest\ndatabase:\n  path: warehouse.duckdb\n"
    )
    for sub in ("bronze", "silver", "gold"):
        (tmp_path / "transform" / sub).mkdir(parents=True)
    return tmp_path


@pytest.fixture
def conn(project):
    """A writable warehouse with typed landing tables and no models built."""
    c = duckdb.connect(str(project / "warehouse.duckdb"))
    ensure_meta_table(c)
    c.execute("CREATE SCHEMA IF NOT EXISTS landing")
    c.execute(
        "CREATE TABLE landing.orders ("
        "  order_id INTEGER,"
        "  customer VARCHAR,"
        "  amount DOUBLE,"
        "  event_ts TIMESTAMP,"
        "  payload STRUCT(a INTEGER)"
        ")"
    )
    c.execute("CREATE TABLE landing.customers (customer VARCHAR, region VARCHAR)")
    yield c
    c.close()


def write(project, schema, name, sql):
    (project / "transform" / schema / f"{name}.sql").write_text(sql)


def bind_project(conn, project):
    return bind_models(
        conn, discover_models(project / "transform"), project_dir=project
    )


def messages(result, model):
    return [e.message for e in result.errors.get(model, [])]


# --- what the binder catches -------------------------------------------------


@pytest.mark.parametrize(
    "label,sql,needle",
    [
        (
            "wrong arity",
            "SELECT date_trunc(event_ts) AS x FROM landing.orders",
            "No function matches",
        ),
        (
            "unknown function",
            "SELECT no_such_fn(customer) AS x FROM landing.orders",
            "does not exist",
        ),
        (
            "operator overload",
            "SELECT customer + 1 AS x FROM landing.orders",
            "No function matches",
        ),
        (
            "missing column",
            "SELECT no_such_column AS x FROM landing.orders",
            "not found in FROM clause",
        ),
        (
            "missing struct key",
            "SELECT payload.zzz AS x FROM landing.orders",
            'Could not find key "zzz"',
        ),
        (
            "ambiguous reference",
            "SELECT customer FROM landing.orders a, landing.customers b",
            "Ambiguous reference",
        ),
        (
            "aggregation without GROUP BY",
            "SELECT customer, SUM(amount) AS total FROM landing.orders",
            "must appear in the GROUP BY clause",
        ),
        (
            "set operation arity",
            "SELECT order_id FROM landing.orders "
            "UNION ALL SELECT customer, region FROM landing.customers",
            "same number of result columns",
        ),
    ],
)
def test_binder_catches(conn, project, label, sql, needle):
    write(project, "silver", "probe", f"@config materialized=table, schema=silver\n\n{sql}\n")
    result = bind_project(conn, project)
    found = messages(result, "silver.probe")
    assert found, f"{label}: expected a bind error, got none"
    assert any(needle in m for m in found), f"{label}: {found}"


def test_binder_misses_value_conversion(conn, project):
    """A CAST of a non-numeric string binds clean and only fails at run time.

    This is the boundary of the whole feature and the reason the docs say
    "bind errors" rather than "type checking". If this test ever starts
    failing because DuckDB grew a const-folding check, the docs can be
    upgraded; until then, do not claim it.
    """
    write(
        project,
        "silver",
        "probe",
        "@config materialized=table, schema=silver\n\n"
        "SELECT CAST(customer AS INTEGER) AS x FROM landing.orders\n",
    )
    result = bind_project(conn, project)
    assert messages(result, "silver.probe") == []
    assert result.schemas["silver.probe"] == [("x", "INTEGER")]

    conn.execute("INSERT INTO landing.orders VALUES (1, 'abc', 1.0, NULL, NULL)")
    with pytest.raises(duckdb.Error):
        conn.execute(
            "SELECT CAST(customer AS INTEGER) FROM landing.orders"
        ).fetchall()


# --- unbuilt and stale upstreams --------------------------------------------


def test_unbuilt_upstream_chain_is_typed(conn, project):
    """Nothing is built; the whole chain still resolves with real types."""
    write(
        project, "bronze", "orders",
        "@config materialized=table, schema=bronze\n\n"
        "SELECT order_id, customer, amount FROM landing.orders\n",
    )
    write(
        project, "silver", "enriched",
        "@config materialized=table, schema=silver\n\n"
        "SELECT order_id, customer, amount * 2 AS doubled FROM bronze.orders\n",
    )
    write(
        project, "gold", "summary",
        "@config materialized=table, schema=gold\n\n"
        "SELECT customer, SUM(doubled) AS total FROM silver.enriched GROUP BY 1\n",
    )
    result = bind_project(conn, project)
    assert result.errors == {}
    assert result.schemas["gold.summary"][0] == ("customer", "VARCHAR")
    assert result.schemas["silver.enriched"] == [
        ("order_id", "INTEGER"),
        ("customer", "VARCHAR"),
        ("doubled", "DOUBLE"),
    ]


def test_bad_column_on_unbuilt_upstream_is_caught(conn, project):
    write(
        project, "silver", "enriched",
        "@config materialized=table, schema=silver\n\n"
        "SELECT order_id, amount FROM landing.orders\n",
    )
    write(
        project, "gold", "summary",
        "@config materialized=table, schema=gold\n\n"
        "SELECT order_id, no_such_column FROM silver.enriched\n",
    )
    result = bind_project(conn, project)
    assert any(
        "no_such_column" in m for m in messages(result, "gold.summary")
    ), messages(result, "gold.summary")


def test_stale_built_table_is_shadowed_by_fresh_sql(conn, project):
    """A built table with an old shape must not win over the file's SQL."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS silver")
    conn.execute("CREATE TABLE silver.enriched AS SELECT 1 AS old_column")
    write(
        project, "silver", "enriched",
        "@config materialized=table, schema=silver\n\n"
        "SELECT order_id, amount FROM landing.orders\n",
    )
    write(
        project, "gold", "summary",
        "@config materialized=table, schema=gold\n\n"
        "SELECT order_id FROM silver.enriched\n",
    )
    result = bind_project(conn, project)
    assert result.errors == {}
    assert result.schemas["silver.enriched"] == [
        ("order_id", "INTEGER"),
        ("amount", "DOUBLE"),
    ]
    # The stale table in the real catalog is untouched.
    assert conn.execute("DESCRIBE silver.enriched").fetchall()[0][0] == "old_column"


# --- cascade suppression -----------------------------------------------------


def test_downstream_reports_upstream_once_not_a_cascade(conn, project):
    write(
        project, "silver", "broken",
        "@config materialized=table, schema=silver\n\n"
        "SELECT no_such_column FROM landing.orders\n",
    )
    write(
        project, "gold", "mid",
        "@config materialized=table, schema=gold\n\nSELECT * FROM silver.broken\n",
    )
    write(
        project, "gold", "leaf",
        "@config materialized=table, schema=gold\n\nSELECT * FROM gold.mid\n",
    )
    result = bind_project(conn, project)

    assert len(result.errors["silver.broken"]) == 1
    assert result.errors["silver.broken"][0].kind == "bind"

    assert [e.kind for e in result.errors["gold.mid"]] == ["upstream"]
    assert result.errors["gold.mid"][0].message == "upstream silver.broken failed to bind"
    assert [e.kind for e in result.errors["gold.leaf"]] == ["upstream"]
    assert result.errors["gold.leaf"][0].message == "upstream gold.mid failed to bind"


# --- shadow seeding ----------------------------------------------------------


def test_macros_are_available_in_the_shadow(conn, project):
    (project / "macros").mkdir()
    (project / "macros" / "u.py").write_text(
        "from havn import macro\n\n\n"
        "@macro\n"
        "def shout(value: str) -> str:\n"
        "    return (value or '').upper()\n"
    )
    write(
        project, "silver", "loud",
        "@config materialized=table, schema=silver\n\n"
        "SELECT shout(customer) AS customer FROM landing.orders\n",
    )
    result = bind_project(conn, project)
    assert messages(result, "silver.loud") == []
    assert result.schemas["silver.loud"] == [("customer", "VARCHAR")]


def test_macros_survive_a_second_bind(conn, project):
    """The alias must not be created inside the shadow, or it dies on DETACH."""
    (project / "macros").mkdir()
    (project / "macros" / "u.py").write_text(
        "from havn import macro\n\n\n"
        "@macro\n"
        "def shout(value: str) -> str:\n"
        "    return (value or '').upper()\n"
    )
    write(
        project, "silver", "loud",
        "@config materialized=table, schema=silver\n\n"
        "SELECT shout(customer) AS customer FROM landing.orders\n",
    )
    bind_project(conn, project)
    second = bind_project(conn, project)
    assert messages(second, "silver.loud") == []


def test_extensions_loaded_on_the_parent_are_visible(conn, project):
    """An extension the run has loaded must bind inside the shadow too."""
    try:
        conn.execute("LOAD json")
    except duckdb.Error:  # pragma: no cover - depends on the local build
        pytest.skip("json extension not available in this DuckDB build")
    write(
        project, "silver", "j",
        "@config materialized=table, schema=silver\n\n"
        "SELECT json_extract_string('{\"a\":\"b\"}', '$.a') AS a FROM landing.orders\n",
    )
    result = bind_project(conn, project)
    assert messages(result, "silver.j") == []


def test_base_tables_override_lets_an_absent_source_bind(conn, project):
    """A source that is not in the catalog yet can be declared by spec."""
    write(
        project, "silver", "future",
        "@config materialized=table, schema=silver\n\n"
        "SELECT sku, price FROM landing.not_loaded_yet\n",
    )
    models = discover_models(project / "transform")
    result = bind_models(
        conn,
        models,
        base_tables={"landing.not_loaded_yet": [("sku", "VARCHAR"), ("price", "DECIMAL(10,2)")]},
        project_dir=project,
    )
    assert messages(result, "silver.future") == []
    assert result.schemas["silver.future"] == [
        ("sku", "VARCHAR"),
        ("price", "DECIMAL(10,2)"),
    ]


def test_base_tables_keep_their_real_types_including_nested_ones(conn, project):
    """Seeded tables come from information_schema, which round-trips any type.

    A struct, a list, a decimal and a column name that needs quoting all have
    to survive the trip into the shadow, or a legitimate model reports a
    spurious bind error.
    """
    conn.execute(
        'CREATE TABLE landing.wide ('
        '  "Order Id" INTEGER,'
        "  payload STRUCT(a INTEGER, b VARCHAR),"
        "  tags VARCHAR[],"
        "  lookup MAP(VARCHAR, INTEGER),"
        "  price DECIMAL(10,2)"
        ")"
    )
    write(
        project, "silver", "wide",
        "@config materialized=table, schema=silver\n\n"
        'SELECT "Order Id" AS order_id, payload.b AS b, tags[1] AS tag,\n'
        "       lookup['x'] AS hit, price * 2 AS doubled\n"
        "FROM landing.wide\n",
    )
    result = bind_project(conn, project)
    assert messages(result, "silver.wide") == []
    assert result.schemas["silver.wide"] == [
        ("order_id", "INTEGER"),
        ("b", "VARCHAR"),
        ("tag", "VARCHAR"),
        ("hit", "INTEGER"),
        ("doubled", "DECIMAL(18,2)"),
    ]


def test_seeded_base_tables_are_empty(conn, project):
    """Rows never leave the warehouse; the shadow sees the shape only."""
    conn.execute("INSERT INTO landing.orders VALUES (1, 'a', 2.0, NULL, NULL)")
    write(
        project, "silver", "counted",
        "@config materialized=table, schema=silver\n\n"
        "SELECT COUNT(*) AS n FROM landing.orders\n",
    )
    result = bind_project(conn, project)
    assert messages(result, "silver.counted") == []
    # The bind pass reports shapes, not values: it must not have read a row.
    assert result.schemas["silver.counted"] == [("n", "BIGINT")]


def test_a_read_only_connection_is_enough(project, tmp_path):
    """The catalog source is read only; the shadow does the binding."""
    path = str(project / "warehouse.duckdb")
    writer = duckdb.connect(path)
    ensure_meta_table(writer)
    writer.execute("CREATE SCHEMA IF NOT EXISTS landing")
    writer.execute("CREATE TABLE landing.orders (order_id INTEGER, amount DOUBLE)")
    writer.close()

    write(
        project, "silver", "ok",
        "@config materialized=table, schema=silver\n\n"
        "SELECT order_id, amount FROM landing.orders\n",
    )
    reader = duckdb.connect(path, read_only=True)
    try:
        result = bind_project(reader, project)
    finally:
        reader.close()
    assert result.errors == {}
    assert result.schemas["silver.ok"] == [
        ("order_id", "INTEGER"),
        ("amount", "DOUBLE"),
    ]


def test_concurrent_binds_do_not_interfere(conn, project):
    """Each call owns its shadow, so parallel binds cannot collide."""
    import threading

    write(
        project, "silver", "a",
        "@config materialized=table, schema=silver\n\n"
        "SELECT order_id FROM landing.orders\n",
    )
    write(
        project, "silver", "b",
        "@config materialized=table, schema=silver\n\n"
        "SELECT customer FROM landing.orders\n",
    )
    models = discover_models(project / "transform")
    results: list = []
    errors: list = []
    lock = threading.Lock()

    def run(target: str) -> None:
        try:
            chain = ancestor_closure(models, [target])
            bound = bind_models(conn.cursor(), chain, project_dir=project)
            with lock:
                results.append((target, bound))
        except Exception as e:  # pragma: no cover - the failure we are testing for
            with lock:
                errors.append(e)

    threads = [
        threading.Thread(target=run, args=(name,))
        for name in ("silver.a", "silver.b") * 4
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(results) == 8
    for target, result in results:
        assert result.errors == {}, (target, result.errors)
        assert set(result.schemas) == {target}


def test_a_buffer_cannot_write_to_the_warehouse_through_the_shadow(conn, project):
    """The multi-statement escape: a second statement must reach nothing."""
    from havn.engine.transform.bind import model_from_buffer

    buffer = model_from_buffer(
        "@config schema=scratch\n\n"
        "SELECT 1 AS a; CREATE TABLE landing.pwned AS SELECT 99\n",
        path="transform/silver/probe.sql",
        transform_dir=project / "transform",
    )
    bind_models(conn, [buffer], project_dir=project)
    existing = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'landing'"
        ).fetchall()
    }
    assert "pwned" not in existing


# --- cleanup -----------------------------------------------------------------


def _attached(conn):
    return {r[0] for r in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()}


def test_shadow_is_detached_on_success(conn, project):
    write(
        project, "silver", "ok",
        "@config materialized=table, schema=silver\n\nSELECT order_id FROM landing.orders\n",
    )
    before = _attached(conn)
    bind_project(conn, project)
    assert _attached(conn) == before


def test_shadow_is_detached_even_when_a_model_fails(conn, project):
    write(
        project, "silver", "broken",
        "@config materialized=table, schema=silver\n\nSELECT nope FROM landing.orders\n",
    )
    before = _attached(conn)
    result = bind_project(conn, project)
    assert result.errors
    assert _attached(conn) == before
    assert not any(d.startswith("shadow_") for d in _attached(conn))


def test_parent_connection_default_catalog_is_restored(conn, project):
    write(
        project, "silver", "ok",
        "@config materialized=table, schema=silver\n\nSELECT order_id FROM landing.orders\n",
    )
    before = conn.execute("SELECT current_database()").fetchone()[0]
    bind_project(conn, project)
    assert conn.execute("SELECT current_database()").fetchone()[0] == before


# --- position extraction -----------------------------------------------------


def test_position_points_at_the_offending_token(conn, project):
    write(
        project, "silver", "probe",
        "@config materialized=table, schema=silver\n"
        "\n"
        "SELECT\n"
        "  order_id,\n"
        "  no_such_column\n"
        "FROM landing.orders\n",
    )
    result = bind_project(conn, project)
    err = result.errors["silver.probe"][0]
    assert (err.line, err.col) == (5, 3)
    assert (err.end_line, err.end_col) == (5, 3 + len("no_such_column"))


def test_position_recovers_from_a_truncated_echo(conn, project):
    """A long SELECT list makes DuckDB echo the line with a leading '...'."""
    wide = ", ".join(f"customer AS c{i}" for i in range(60))
    write(
        project, "silver", "probe",
        "@config materialized=table, schema=silver\n\n"
        f"SELECT {wide}, no_such_column FROM landing.orders\n",
    )
    result = bind_project(conn, project)
    err = result.errors["silver.probe"][0]
    source_line = (project / "transform" / "silver" / "probe.sql").read_text().split("\n")[2]
    assert "..." in err.raw
    assert err.line == 3
    assert err.col == source_line.index("no_such_column") + 1
    assert err.end_col == err.col + len("no_such_column")


def test_truncation_only_at_the_end_keeps_the_caret(conn, project):
    """A trailing '...' does not move the caret, so the column still holds."""
    wide = ", ".join(f"customer AS c{i}" for i in range(60))
    write(
        project, "silver", "probe",
        "@config materialized=table, schema=silver\n\n"
        f"SELECT no_such_column AS a, {wide} FROM landing.orders\n",
    )
    result = bind_project(conn, project)
    err = result.errors["silver.probe"][0]
    assert err.raw.rstrip().endswith("^")
    assert (err.line, err.col) == (3, len("SELECT ") + 1)


def test_ambiguous_truncated_identifier_falls_back_to_the_whole_line(conn, project):
    """Two occurrences on the truncated line means the caret cannot be trusted."""
    wide = ", ".join(f"customer AS c{i}" for i in range(60))
    write(
        project, "silver", "probe",
        "@config materialized=table, schema=silver\n\n"
        f"SELECT {wide}, nope AS a, nope AS b FROM landing.orders\n",
    )
    result = bind_project(conn, project)
    err = result.errors["silver.probe"][0]
    source_line = (project / "transform" / "silver" / "probe.sql").read_text().split("\n")[2]
    assert err.line == 3
    assert err.col == 1
    assert err.end_col == len(source_line) + 1


def test_parser_error_without_a_position_gets_no_line():
    line, col, end_line, end_col = extract_position(
        "Parser Error: syntax error at end of input", "SELECT\n"
    )
    assert (line, col, end_line, end_col) == (None, None, None, None)


def test_position_is_none_when_the_error_points_at_the_wrapper():
    """A LINE 1 error belongs to the CREATE VIEW header, not the model."""
    raw = "Binder Error: something\n\nLINE 1: CREATE OR REPLACE VIEW x AS\n        ^"
    assert extract_position(raw, "SELECT 1\n") == (None, None, None, None)


def test_bind_error_lines_are_file_lines_not_query_lines(conn, project):
    """Directives are blanked in place, so a query line is already a file line."""
    write(
        project, "silver", "probe",
        "@config materialized=table, schema=silver\n"
        "@description a model with a tall header\n"
        "@assert count(*) > 0\n"
        "\n"
        "SELECT nope FROM landing.orders\n",
    )
    result = bind_project(conn, project)
    assert result.errors["silver.probe"][0].line == 5


# --- rendering ---------------------------------------------------------------


def test_validation_message_says_bind_error_and_drops_the_duckdb_prefix(conn, project):
    write(
        project, "silver", "probe",
        "@config materialized=table, schema=silver\n\nSELECT nope FROM landing.orders\n",
    )
    result = bind_project(conn, project)
    rendered = as_validation_message(result.errors["silver.probe"][0])
    assert rendered.startswith("bind error: ")
    assert "Binder Error" not in rendered
    assert "type error" not in rendered.lower()


# --- graph scoping and performance ------------------------------------------


def test_ancestor_closure_is_only_the_upstream_chain(project):
    write(project, "bronze", "a", "@config schema=bronze\n\nSELECT 1 AS x\n")
    write(project, "silver", "b", "@config schema=silver\n\nSELECT x FROM bronze.a\n")
    write(project, "gold", "c", "@config schema=gold\n\nSELECT x FROM silver.b\n")
    write(project, "gold", "unrelated", "@config schema=gold\n\nSELECT 1 AS y\n")
    models = discover_models(project / "transform")
    names = [m.full_name for m in ancestor_closure(models, ["silver.b"])]
    assert names == ["bronze.a", "silver.b"]


def test_binding_a_long_chain_stays_under_five_seconds(conn, project):
    write(
        project, "silver", "m0",
        "@config materialized=view, schema=silver\n\n"
        "SELECT order_id, amount FROM landing.orders\n",
    )
    for i in range(1, 200):
        write(
            project, "silver", f"m{i}",
            "@config materialized=view, schema=silver\n\n"
            f"SELECT order_id, amount, {i} AS step FROM silver.m{i - 1}\n",
        )
    models = discover_models(project / "transform")
    assert len(models) == 200

    started = time.perf_counter()
    result = bind_models(conn, models, project_dir=project)
    elapsed = time.perf_counter() - started

    assert result.errors == {}
    assert len(result.schemas) == 200
    assert elapsed < 5.0, f"200-model chain took {elapsed:.2f}s"


def test_one_model_binds_only_its_chain(conn, project):
    write(
        project, "silver", "m0",
        "@config materialized=view, schema=silver\n\nSELECT order_id FROM landing.orders\n",
    )
    for i in range(1, 60):
        write(
            project, "silver", f"m{i}",
            "@config materialized=view, schema=silver\n\n"
            f"SELECT order_id FROM silver.m{i - 1}\n",
        )
    models = discover_models(project / "transform")
    chain = ancestor_closure(models, ["silver.m3"])
    result = bind_models(conn, chain, project_dir=project)
    assert set(result.schemas) == {"silver.m0", "silver.m1", "silver.m2", "silver.m3"}


# --- buffer parsing ----------------------------------------------------------


def test_model_from_buffer_takes_its_schema_from_the_folder(project):
    model = model_from_buffer(
        "SELECT 1 AS x\n",
        path="transform/gold/report.sql",
        transform_dir=project / "transform",
    )
    assert model.full_name == "gold.report"


def test_model_from_buffer_honours_a_config_schema_override(project):
    model = model_from_buffer(
        "@config materialized=table, schema=silver\n\nSELECT 1 AS x\n",
        path="transform/gold/report.sql",
        transform_dir=project / "transform",
    )
    assert model.full_name == "silver.report"
    assert model.materialized == "table"


def test_model_from_buffer_extracts_dependencies(project):
    model = model_from_buffer(
        "@config schema=gold\n\nSELECT a.x FROM silver.one a JOIN silver.two b ON a.x = b.x\n",
        path="transform/gold/report.sql",
        transform_dir=project / "transform",
    )
    assert sorted(model.depends_on) == ["silver.one", "silver.two"]


def test_model_from_buffer_rejects_an_unsafe_name(project):
    with pytest.raises(ValueError):
        model_from_buffer(
            "@config schema=gold\n\nSELECT 1\n",
            path="transform/gold/drop table x.sql",
            transform_dir=project / "transform",
        )


# --- validate_models integration --------------------------------------------


def test_validate_models_with_bind_reports_lines(conn, project):
    from havn.engine.transform import validate_models

    write(
        project, "silver", "probe",
        "@config materialized=table, schema=silver\n"
        "\n"
        "SELECT\n"
        "  nope\n"
        "FROM landing.orders\n",
    )
    models = discover_models(project / "transform")

    without = validate_models(conn, models)
    assert not [e for e in without if "bind error" in e.message]

    with_bind = validate_models(conn, models, bind=True, project_dir=project)
    bind_rows = [e for e in with_bind if e.message.startswith("bind error")]
    assert len(bind_rows) == 1
    assert bind_rows[0].severity == "error"
    assert bind_rows[0].line == 4
    assert bind_rows[0].model == "silver.probe"


# --- persistence -------------------------------------------------------------


def test_built_model_columns_are_persisted(conn, project):
    from havn.engine.transform import load_model_columns, run_transform

    write(
        project, "silver", "orders",
        "@config materialized=table, schema=silver\n\n"
        "SELECT order_id, amount FROM landing.orders\n",
    )
    run_transform(conn, project / "transform", project_dir=project)
    assert load_model_columns(conn, "silver.orders") == [
        {"name": "order_id", "type": "INTEGER"},
        {"name": "amount", "type": "DOUBLE"},
    ]


def test_load_model_columns_is_empty_for_an_unbuilt_model(conn):
    from havn.engine.transform import load_model_columns

    assert load_model_columns(conn, "gold.never_built") == []


# --- CTE enumeration ---------------------------------------------------------


def test_cte_preview_slices_the_original_text():
    from havn.engine.transform.ctes import enumerate_ctes

    sql = (
        "WITH base AS (\n"
        "    SELECT   id,\n"
        "             amount   -- keep this spacing\n"
        "    FROM landing.orders\n"
        "),\n"
        "agg AS (\n"
        "    SELECT SUM(amount) AS total FROM base\n"
        ")\n"
        "SELECT * FROM agg\n"
    )
    ctes, active = enumerate_ctes(sql, line=7)
    assert [c.name for c in ctes] == ["base", "agg"]
    assert (ctes[0].start_line, ctes[0].end_line) == (1, 5)
    assert (ctes[1].start_line, ctes[1].end_line) == (6, 8)
    assert active == 1
    assert "-- keep this spacing" in ctes[0].preview_sql
    assert ctes[0].preview_sql.endswith("SELECT * FROM base")
    assert "LIMIT" not in ctes[1].preview_sql
    assert ctes[1].preview_sql.startswith("WITH base AS (")


def test_cte_preview_handles_parens_inside_literals():
    from havn.engine.transform.ctes import enumerate_ctes

    sql = (
        "WITH base AS (\n"
        "    SELECT ')' AS closer, id FROM landing.orders\n"
        ")\n"
        "SELECT * FROM base\n"
    )
    ctes, _ = enumerate_ctes(sql)
    assert ctes[0].end_line == 3
    assert ctes[0].preview_sql.count(")") >= 2


def test_no_ctes_is_an_empty_list():
    from havn.engine.transform.ctes import enumerate_ctes

    assert enumerate_ctes("SELECT 1 AS x", line=1) == ([], None)


def test_recursive_ctes_are_refused():
    from havn.engine.transform.ctes import CteParseError, enumerate_ctes

    with pytest.raises(CteParseError) as excinfo:
        enumerate_ctes("WITH RECURSIVE t AS (SELECT 1 AS n) SELECT * FROM t")
    assert "recursive" in str(excinfo.value).lower()


def test_unparseable_sql_raises():
    from havn.engine.transform.ctes import CteParseError, enumerate_ctes

    with pytest.raises(CteParseError):
        enumerate_ctes("WITH SELECT FROM WHERE (")
