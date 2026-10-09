"""Python models: transform/**/*.py files as first-class DAG nodes."""

from __future__ import annotations

import textwrap
from pathlib import Path

import duckdb
import pytest

from havn.engine.transform import (
    build_dag,
    discover_all_models,
    discover_models,
    run_transform,
)
from havn.engine.transform.discovery import DuplicateModelError


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path):
    """A project with a SQL source model and a Python model reading it."""
    _write(tmp_path / "project.yml", "name: t\ndatabase:\n  path: warehouse.duckdb\n")
    _write(
        tmp_path / "transform/bronze/orders.sql",
        """
        @config materialized=table
        SELECT * FROM landing.orders
        """,
    )
    _write(
        tmp_path / "transform/silver/scores.py",
        '''
        """Score per customer."""
        from havn import model


        @model(materialized="table", tags=["daily"], assertions=["unique(customer)"])
        def scores(db, ref):
            orders = ref("bronze.orders")
            return orders.aggregate("customer, sum(amount) AS score")
        ''',
    )
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA landing")
    conn.execute(
        "CREATE TABLE landing.orders AS SELECT * FROM (VALUES "
        "(1, 'a', 10.0), (2, 'b', 5.0), (3, 'a', 1.0)) t(id, customer, amount)"
    )
    conn.close()
    return tmp_path


@pytest.fixture
def conn(project):
    c = duckdb.connect(str(project / "warehouse.duckdb"))
    yield c
    c.close()


def _run(conn, project, **kw):
    return run_transform(conn, project / "transform", project_dir=project, **kw)


def _model(project, name):
    return {m.full_name: m for m in discover_all_models(project)}[name]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discovers_python_model_with_config_and_refs(project):
    m = _model(project, "silver.scores")
    assert m.is_python and m.language == "python"
    assert m.materialized == "table"
    assert m.depends_on == ["bronze.orders"]
    assert m.tags == ["daily"]
    assert m.assertions == ["unique(customer)"]
    assert m.description == "Score per customer."
    assert m.python.function == "scores"
    assert m.python.params == ["db", "ref"]
    assert m.python.errors == []
    assert m.query == ""
    # SQL-only analysis never sees Python as SQL.
    assert m.ast is None and m.parse_error == ""


def test_dag_orders_python_between_sql(project):
    _write(project / "transform/gold/top.sql", "SELECT * FROM silver.scores WHERE score > 5\n")
    order = [m.full_name for m in build_dag(discover_models(project / "transform"))]
    assert order.index("bronze.orders") < order.index("silver.scores") < order.index("gold.top")


def test_plain_def_model_without_decorator(tmp_path):
    _write(tmp_path / "transform/silver/x.py", 'def model(ref):\n    return ref("bronze.a")\n')
    (m,) = discover_models(tmp_path / "transform")
    assert m.full_name == "silver.x" and m.python.function == "model"
    assert m.depends_on == ["bronze.a"] and m.materialized == "table"


def test_helpers_and_non_models_are_not_models(tmp_path):
    _write(tmp_path / "transform/silver/_helpers.py", "def model(ref):\n    return 1\n")
    _write(tmp_path / "transform/silver/utils.py", "X = 1\n\ndef build():\n    return X\n")
    _write(tmp_path / "transform/silver/__pycache__/y.py", "def model(ref):\n    return 1\n")
    assert discover_models(tmp_path / "transform") == []


def test_depends_on_and_literal_sql_become_dependencies(tmp_path):
    _write(
        tmp_path / "transform/gold/x.py",
        """
        from havn import model

        @model(depends_on=["silver.extra"])
        def x(db, ref):
            names = ["silver.extra"]
            other = db.sql("SELECT * FROM silver.raw_sql JOIN landing.t USING (id)")
            return ref(names[0])
        """,
    )
    (m,) = discover_models(tmp_path / "transform")
    assert m.depends_on == ["silver.extra", "landing.t", "silver.raw_sql"]
    warnings = [w for w, _ in m.python.warnings]
    assert any("not a string literal" in w for w in warnings)
    assert any("silver.raw_sql is read through literal SQL" in w for w in warnings)


def test_python_and_sql_of_one_name_is_a_duplicate(tmp_path):
    _write(tmp_path / "transform/silver/x.sql", "SELECT 1 AS a\n")
    _write(tmp_path / "transform/silver/x.py", "def model(db):\n    return db.sql('SELECT 1')\n")
    with pytest.raises(DuplicateModelError):
        discover_models(tmp_path / "transform")


@pytest.mark.parametrize(
    "source, fragment",
    [
        ("from havn import model\n@model(materialized=MAT)\ndef x(db):\n    pass\n", "must be a literal"),
        ("from havn import model\n@model(materialized='view')\ndef x(db):\n    pass\n", "cannot be materialized as view"),
        ("from havn import model\n@model(materialized='ephemeral')\ndef x(db):\n    pass\n", "cannot be materialized as ephemeral"),
        ("from havn import model\n@model(materialised='table')\ndef x(db):\n    pass\n", "Did you mean 'materialized'"),
        ("from havn import model\n@model(incremental_strategy='microbatch')\ndef x(db):\n    pass\n", "microbatch is not supported"),
        ("from havn import model\n@model()\ndef x(db, spark):\n    pass\n", "asks for 'spark'"),
        ("from havn import model\n@model()\ndef x(db):\n  return (\n", "SyntaxError"),
    ],
)
def test_problems_keep_the_model_and_are_reported(tmp_path, source, fragment):
    _write(tmp_path / "transform/silver/x.py", source)
    (m,) = discover_models(tmp_path / "transform")
    assert m.is_python
    assert m.materialized == "table"  # never left as view/ephemeral
    assert any(fragment in msg for msg, _ in m.python.errors), m.python.errors


def test_validate_models_reports_python_problems(tmp_path):
    from havn.engine.transform.analysis import validate_models

    _write(
        tmp_path / "transform/silver/x.py",
        "from havn import model\nimport surely_not_installed_pkg\n"
        "@model(materialized='view')\ndef x(db):\n    pass\n",
    )
    errors = validate_models(None, discover_models(tmp_path / "transform"))
    messages = [e.message for e in errors if e.model == "silver.x"]
    assert any("cannot be materialized as view" in m for m in messages)
    assert any("surely_not_installed_pkg" in m for m in messages)
    # Never a SQL parse error for a Python file.
    assert not any("SQL parse error" in m for m in messages)


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def test_builds_table_and_runs_assertions(project, conn):
    results = _run(conn, project)
    assert results == {"bronze.orders": "built", "silver.scores": "built"}
    rows = conn.execute("SELECT customer, score FROM silver.scores ORDER BY 1").fetchall()
    assert [(c, float(s)) for c, s in rows] == [("a", 11.0), ("b", 5.0)]
    passed = conn.execute(
        "SELECT passed FROM _havn.assertion_results WHERE model_path = 'silver.scores'"
    ).fetchall()
    assert passed == [(True,)]
    state = conn.execute(
        "SELECT materialized_as, row_count FROM _havn.model_state WHERE model_path = 'silver.scores'"
    ).fetchone()
    assert state == ("table", 2)
    # The staged TEMP table is gone after the build.
    assert conn.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name LIKE '_havn_py_%'"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "body",
    [
        "return db.sql('SELECT 1 AS a UNION ALL SELECT 2')",
        "import pandas as pd\n    return pd.DataFrame({'a': [1, 2]})",
        "import pyarrow as pa\n    return pa.table({'a': [1, 2]})",
        "other = __import__('duckdb').connect()\n    return other.sql('SELECT 1 AS a UNION ALL SELECT 2')",
    ],
    ids=["relation", "pandas", "arrow", "foreign-relation"],
)
def test_result_kinds(tmp_path, body):
    if "pandas" in body:
        pytest.importorskip("pandas")
    _write(tmp_path / "transform/silver/x.py", f"def model(db):\n    {body}\n")
    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    try:
        assert run_transform(conn, tmp_path / "transform") == {"silver.x": "built"}
        assert conn.execute("SELECT sum(a) FROM silver.x").fetchone()[0] == 3
    finally:
        conn.close()


@pytest.mark.parametrize(
    "body, fragment",
    [("return None", "returned None"), ("return [1, 2]", "returned list")],
)
def test_unsupported_results_fail_clearly(tmp_path, body, fragment):
    _write(tmp_path / "transform/silver/x.py", f"def model(db):\n    {body}\n")
    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    try:
        assert run_transform(conn, tmp_path / "transform") == {"silver.x": "error"}
        error = conn.execute(
            "SELECT error FROM _havn.run_log WHERE target = 'silver.x'"
        ).fetchone()[0]
        assert fragment in error
    finally:
        conn.close()


def test_failure_points_at_the_users_line(project, conn):
    _write(
        project / "transform/silver/boom.py",
        """
        from havn import model


        def helper(x):
            return x / 0


        @model()
        def boom(ref):
            print("about to fail")
            return helper(1)
        """,
    )
    results = _run(conn, project)
    assert results["silver.boom"] == "error"
    error, output = conn.execute(
        "SELECT error, log_output FROM _havn.run_log WHERE target = 'silver.boom'"
    ).fetchone()
    first = error.splitlines()[0]
    assert "transform/silver/boom.py:5" in first and "ZeroDivisionError" in first
    assert 'File "transform/silver/boom.py", line 11, in boom' in error
    assert "return helper(1)" in error
    # havn's own frames are not in the user's traceback.
    assert "python_models.py" not in error
    assert output == "about to fail\n"


def test_printed_output_lands_in_the_run_log(project, conn):
    _write(
        project / "transform/silver/chatty.py",
        "def model(db):\n    print('rows: 1')\n    return db.sql('SELECT 1 AS a')\n",
    )
    _run(conn, project)
    out = conn.execute(
        "SELECT log_output FROM _havn.run_log WHERE target = 'silver.chatty' AND status = 'success'"
    ).fetchone()[0]
    assert out == "rows: 1"


def test_undeclared_ref_is_refused(project, conn):
    _write(
        project / "transform/silver/sneaky.py",
        "def model(ref):\n    name = 'bronze.' + 'orders'\n    return ref(name)\n",
    )
    assert _run(conn, project)["silver.sneaky"] == "error"
    error = conn.execute(
        "SELECT error FROM _havn.run_log WHERE target = 'silver.sneaky'"
    ).fetchone()[0]
    assert "not a declared dependency" in error and "depends_on" in error
    assert "sneaky.py:3" in error


def test_static_error_fails_the_build(project, conn):
    _write(project / "transform/silver/bad.py", "from havn import model\n@model(materialized=X)\ndef bad(db):\n    pass\n")
    assert _run(conn, project)["silver.bad"] == "error"
    error = conn.execute("SELECT error FROM _havn.run_log WHERE target = 'silver.bad'").fetchone()[0]
    assert "bad.py:2" in error and "must be a literal" in error


def test_timeout(project, conn, monkeypatch):
    import havn.engine.runner as runner

    monkeypatch.setattr(runner, "SCRIPT_POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(runner, "SCRIPT_STOP_GRACE_SECONDS", 2)
    _write(
        project / "transform/silver/slow.py",
        """
        from havn import model

        @model(timeout=0.3)
        def slow(db):
            while True:
                pass
        """,
    )
    assert _run(conn, project)["silver.slow"] == "error"
    error = conn.execute("SELECT error FROM _havn.run_log WHERE target = 'silver.slow'").fetchone()[0]
    assert "timed out after 0.3s" in error


def test_ephemeral_upstream_is_inlined_through_ref(project, conn):
    _write(
        project / "transform/silver/big_orders.sql",
        "@config materialized=ephemeral\nSELECT * FROM bronze.orders WHERE amount > 4\n",
    )
    _write(
        project / "transform/gold/big.py",
        "def model(ref):\n    return ref('silver.big_orders')\n",
    )
    assert _run(conn, project)["gold.big"] == "built"
    assert conn.execute("SELECT count(*) FROM gold.big").fetchone()[0] == 2


def test_sql_model_reads_python_model(project, conn):
    _write(project / "transform/gold/top.sql", "@config materialized=table\nSELECT customer FROM silver.scores WHERE score > 6\n")
    assert _run(conn, project)["gold.top"] == "built"
    assert conn.execute("SELECT * FROM gold.top").fetchall() == [("a",)]


def test_parallel_tiers_run_python_models(project, conn):
    for i in range(3):
        _write(
            project / f"transform/gold/p{i}.py",
            f"def model(ref):\n    print('p{i}')\n    return ref('silver.scores').filter('score > {i}')\n",
        )
    results = _run(conn, project, parallel=True, max_workers=3, db_path=str(project / "warehouse.duckdb"))
    assert all(results[f"gold.p{i}"] == "built" for i in range(3))
    logged = conn.execute(
        "SELECT log_output FROM _havn.run_log WHERE target = 'gold.p1' AND status = 'success'"
    ).fetchone()[0]
    assert logged == "p1"


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------


def _status(conn, project, name):
    return _run(conn, project)[name]


def test_unchanged_skips_and_code_edit_rebuilds(project, conn):
    path = project / "transform/silver/scores.py"
    assert _status(conn, project, "silver.scores") == "built"
    assert _status(conn, project, "silver.scores") == "skipped"

    # Comments, blank lines, docstrings and tags are not code that runs.
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace('"""Score per customer."""', '"""Scores, documented better."""')
        .replace('tags=["daily"]', 'tags=["daily", "finance"]')
        .replace("    orders = ref", "    # a comment\n\n    orders = ref"),
        encoding="utf-8",
    )
    assert _status(conn, project, "silver.scores") == "skipped"

    path.write_text(path.read_text(encoding="utf-8").replace("sum(amount)", "max(amount)"), encoding="utf-8")
    assert _status(conn, project, "silver.scores") == "built"
    assert conn.execute("SELECT score FROM silver.scores WHERE customer = 'a'").fetchone()[0] == 10


def test_helper_edit_rebuilds_only_its_importers(project, conn):
    _write(project / "transform/silver/_factor.py", "FACTOR = 2\n")
    _write(
        project / "transform/silver/doubled.py",
        "from _factor import FACTOR\n\ndef model(ref):\n"
        "    return ref('silver.scores').project(f'customer, score * {FACTOR} AS s')\n",
    )
    first = _run(conn, project)
    assert first["silver.doubled"] == "built"
    assert conn.execute("SELECT s FROM silver.doubled WHERE customer = 'b'").fetchone()[0] == 10
    assert _model(project, "silver.doubled").python.helpers == ["silver/_factor.py"]

    _write(project / "transform/silver/_factor.py", "FACTOR = 3\n")
    second = _run(conn, project)
    assert second["silver.doubled"] == "built"
    assert second["silver.scores"] == "skipped"
    assert conn.execute("SELECT s FROM silver.doubled WHERE customer = 'b'").fetchone()[0] == 15
    # No bytecode is left behind in the user's transform/ folder.
    assert not list((project / "transform").rglob("__pycache__"))


def test_upstream_change_cascades_through_python(project, conn):
    _write(project / "transform/gold/top.sql", "@config materialized=table\nSELECT * FROM silver.scores\n")
    _run(conn, project)
    sql = project / "transform/bronze/orders.sql"
    sql.write_text(sql.read_text(encoding="utf-8").replace("SELECT *", "SELECT id, customer, amount * 2 AS amount"), encoding="utf-8")
    results = _run(conn, project)
    assert results == {"bronze.orders": "built", "silver.scores": "built", "gold.top": "built"}
    assert conn.execute("SELECT max(score) FROM gold.top").fetchone()[0] == 22


def test_state_modified_selector_sees_python_edit(project, conn):
    from havn.engine.selectors import select_models

    _run(conn, project)
    path = project / "transform/silver/scores.py"
    path.write_text(path.read_text(encoding="utf-8").replace("sum(", "min("), encoding="utf-8")
    picked = select_models(["state:modified"], discover_all_models(project), conn=conn, project_dir=project)
    assert picked.selected == ["silver.scores"]


# ---------------------------------------------------------------------------
# Incremental and snapshot
# ---------------------------------------------------------------------------


INCREMENTAL = '''
from havn import model


@model(materialized="incremental", unique_key="id", incremental_strategy="{strategy}")
def events(db, ref, is_incremental, this):
    src = ref("bronze.orders")
    if is_incremental:
        src = src.filter(f"id > (SELECT max(id) FROM {{this}})")
    print("incremental" if is_incremental else "full")
    return src.project("id, customer, amount")
'''


@pytest.mark.parametrize("strategy", ["delete+insert", "merge", "append"])
def test_incremental_strategies(project, conn, strategy):
    _write(project / "transform/silver/events.py", INCREMENTAL.format(strategy=strategy))
    _run(conn, project)
    assert conn.execute("SELECT count(*) FROM silver.events").fetchone()[0] == 3
    conn.execute("INSERT INTO landing.orders VALUES (4, 'c', 2.0)")
    _run(conn, project, force=True)
    assert conn.execute("SELECT count(*) FROM silver.events").fetchone()[0] == 4
    outputs = [r[0] for r in conn.execute(
        "SELECT log_output FROM _havn.run_log WHERE target = 'silver.events' "
        "AND status = 'success' ORDER BY started_at"
    ).fetchall()]
    assert outputs == ["full", "incremental"]


def test_incremental_on_schema_change_fail(project, conn):
    path = _write(
        project / "transform/silver/events.py",
        '''
        from havn import model

        @model(materialized="incremental", unique_key="id", on_schema_change="fail")
        def events(ref):
            return ref("bronze.orders").project("id, amount")
        ''',
    )
    _run(conn, project)
    path.write_text(path.read_text(encoding="utf-8").replace('"id, amount"', '"id, amount, customer"'), encoding="utf-8")
    assert _run(conn, project)["silver.events"] == "error"
    error = conn.execute(
        "SELECT error FROM _havn.run_log WHERE target = 'silver.events' ORDER BY started_at DESC LIMIT 1"
    ).fetchone()[0]
    assert "on_schema_change=fail" in error
    assert [r[0] for r in conn.execute("DESCRIBE silver.events").fetchall()] == ["id", "amount"]


def test_snapshot_materialization(project, conn):
    _write(
        project / "transform/silver/hist.py",
        '''
        from havn import model

        @model(materialized="snapshot", unique_key="id")
        def hist(ref):
            return ref("bronze.orders")
        ''',
    )
    _run(conn, project)
    conn.execute("UPDATE landing.orders SET amount = 99 WHERE id = 1")
    _run(conn, project, force=True)
    assert conn.execute("SELECT count(*) FROM silver.hist").fetchone()[0] == 4
    assert conn.execute("SELECT count(*) FROM silver.hist WHERE is_current").fetchone()[0] == 3


# ---------------------------------------------------------------------------
# Assertions, selectors, jobs
# ---------------------------------------------------------------------------


def test_failing_assertion_blocks_downstream(project, conn):
    _write(
        project / "transform/silver/dupes.py",
        '''
        from havn import model

        @model(assertions=["unique(customer)", "row_count > 100, severity=warn"])
        def dupes(ref):
            return ref("bronze.orders").project("customer")
        ''',
    )
    _write(project / "transform/gold/after.sql", "@config materialized=table\nSELECT * FROM silver.dupes\n")
    results = _run(conn, project)
    assert results["silver.dupes"] == "assertion_failed"
    assert results["gold.after"] == "skipped_upstream_blocked"
    severities = dict(conn.execute(
        "SELECT expression, severity FROM _havn.assertion_results WHERE model_path = 'silver.dupes'"
    ).fetchall())
    assert severities == {"unique(customer)": "error", "row_count > 100": "warn"}
    # A rerun rebuilds and re-checks it instead of waving it through.
    assert _run(conn, project)["silver.dupes"] == "assertion_failed"


def test_selectors(project):
    from havn.engine.selectors import select_models

    _write(project / "transform/gold/top.sql", "SELECT * FROM silver.scores\n")
    models = discover_all_models(project)

    def pick(*selectors):
        return sorted(select_models(list(selectors), models, project_dir=project).selected)

    assert pick("tag:daily") == ["silver.scores"]
    assert pick("path:transform/silver/scores.py") == ["silver.scores"]
    assert pick("+silver.scores") == ["bronze.orders", "silver.scores"]
    assert pick("silver.scores+") == ["gold.top", "silver.scores"]
    assert pick("config.language:python") == ["silver.scores"]
    assert pick("config.materialized:table") == ["bronze.orders", "silver.scores"]


def test_job_builds_python_model_with_assertions(project, conn):
    from havn.engine.database import ensure_meta_table
    from havn.engine.orchestration import Job, execute_job, resolve_execution_plan

    ensure_meta_table(conn)  # what open_warehouse does before a job runs
    dag = build_dag(discover_all_models(project))
    job = Job(name="py", target="+silver.scores", file_path=project / "orchestration/py.yml")
    plan = resolve_execution_plan(job.target, dag, project, conn=conn)
    result = execute_job(job, plan, conn, project, trigger="manual")
    assert result.status == "success", result.step_details
    assert conn.execute("SELECT count(*) FROM silver.scores").fetchone()[0] == 2
    assert conn.execute(
        "SELECT count(*) FROM _havn.assertion_results WHERE model_path = 'silver.scores'"
    ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Unit tests, packages, defer, bind, diff and the rest
# ---------------------------------------------------------------------------


def test_unit_test_mocks_refs(project):
    from havn.engine.unit_tests import run_unit_tests

    _write(
        project / "tests/unit/scores.yml",
        """
        model: silver.scores
        tests:
          - name: sums per customer
            given:
              bronze.orders:
                rows:
                  - {id: 1, customer: x, amount: 2.0}
                  - {id: 2, customer: x, amount: 3.0}
            expect:
              rows:
                - {customer: x, score: 5.0}
        """,
    )
    result = run_unit_tests(project)
    assert [r.status for r in result.results] == ["pass"], [r.message for r in result.results]


def test_unit_test_reports_function_failure(project):
    from havn.engine.unit_tests import run_unit_tests

    path = project / "transform/silver/scores.py"
    path.write_text(path.read_text(encoding="utf-8").replace("return orders", "return orders.project('nope')"), encoding="utf-8")
    _write(
        project / "tests/unit/scores.yml",
        """
        model: silver.scores
        tests:
          - name: t
            given:
              bronze.orders:
                rows: [{id: 1, customer: x, amount: 2.0}]
            expect:
              rows: [{customer: x, score: 2.0}]
        """,
    )
    (r,) = run_unit_tests(project).results
    assert r.status == "error" and "model function failed" in r.message


def test_package_python_model_refs_are_namespaced(tmp_path):
    from havn.engine.packages import PackageManifest, PackageRoot
    from havn.engine.transform.discovery import discover_package_models

    pkg = tmp_path / "crm"
    _write(pkg / "transform/bronze/contacts.sql", "SELECT 1 AS id\n")
    _write(pkg / "transform/silver/customers.py", "def model(ref):\n    return ref('bronze.contacts')\n")
    root = PackageRoot(
        name="crm", path=pkg, transform_dir=pkg / "transform",
        macros_dir=pkg / "macros", manifest=PackageManifest(),
    )
    models = {m.full_name: m for m in discover_package_models(root)}
    py = models["crm_silver.customers"]
    assert py.depends_on == ["crm_bronze.contacts"]
    assert py.package == "crm"
    assert py.python.ref_aliases == {"bronze.contacts": "crm_bronze.contacts"}

    conn = duckdb.connect()
    try:
        conn.execute("CREATE SCHEMA crm_bronze")
        conn.execute("CREATE TABLE crm_bronze.contacts AS SELECT 7 AS id")
        from havn.engine.transform.python_models import make_ref

        assert make_ref(conn, py)("bronze.contacts").fetchall() == [(7,)]
    finally:
        conn.close()


def test_ref_applies_the_defer_rewriter(project, conn):
    from havn.engine.transform.python_models import make_ref

    seen = []

    def rewriter(sql):
        seen.append(sql)
        return sql.replace("bronze.orders", "landing.orders")

    m = _model(project, "silver.scores")
    rel = make_ref(conn, m, query_rewriter=rewriter)("bronze.orders")
    assert seen == ["SELECT * FROM bronze.orders"]
    assert rel.aggregate("count(*)").fetchone()[0] == 3


def test_bind_uses_built_columns_for_python_model(project, conn):
    from havn.engine.transform.bind import bind_models

    _write(project / "transform/gold/top.sql", "SELECT customer, score + 1 AS s FROM silver.scores\n")
    models = discover_all_models(project)
    before = bind_models(conn, models, project_dir=project)
    assert "gold.top" not in before.errors
    assert any("Python model not bound" in w.message for w in before.warnings)

    _run(conn, project)
    after = bind_models(conn, models, project_dir=project)
    assert after.errors == {}
    assert [c for c, _ in after.schemas["gold.top"]] == ["customer", "s"]

    _write(project / "transform/gold/bad.sql", "SELECT nope FROM silver.scores\n")
    broken = bind_models(conn, discover_all_models(project), project_dir=project)
    assert "gold.bad" in broken.errors


def test_diff_runs_python_model(project, conn):
    from havn.engine.diff import diff_models

    _run(conn, project)
    conn.execute("INSERT INTO bronze.orders VALUES (4, 'c', 2.0)")
    (result,) = [
        r for r in diff_models(conn, project / "transform", targets=["silver.scores"])
    ]
    assert result.error is None and result.added == 1 and result.total_after == 3


def test_lineage_docs_rename_and_notebook_do_not_crash(project, conn):
    from havn.engine.docs import generate_docs
    from havn.engine.notebook.conversion import model_to_notebook
    from havn.engine.rename import PYTHON, find_column_references
    from havn.engine.transform import extract_column_lineage
    from havn.engine.transform.analysis import impact_analysis

    # An explicit projection, so the rename index can follow amount downstream.
    _write(project / "transform/bronze/orders.sql", "@config materialized=table\nSELECT id, customer, amount FROM landing.orders\n")
    _run(conn, project)
    models = discover_all_models(project)
    m = _model(project, "silver.scores")
    assert extract_column_lineage(m, conn) == {}
    impact = impact_analysis(models, "bronze.orders", column="amount", conn=conn)
    assert {"model": "silver.scores", "column": "amount", "clause": "python"} in impact["affected_columns"]
    docs = generate_docs(conn, project / "transform")
    assert "```python" in docs and "Score per customer." in docs
    report = find_column_references(models, "bronze.orders", "amount", project_dir=project)
    assert any(b.reason == PYTHON for b in report.blocked)
    nb = model_to_notebook(conn, "silver.scores", project / "transform", project / "notebooks")
    code = [c for c in nb["cells"] if c["type"] == "code"]
    assert code and "result = scores(" in code[0]["source"]


def test_notebook_cell_runs(project, conn):
    from havn.engine.transform.python_models import notebook_cell

    _run(conn, project)
    cell = notebook_cell(_model(project, "silver.scores"))
    ns = {"db": conn}
    exec(cell["source"].rsplit("\nresult\n", 1)[0], ns)
    assert ns["result"].aggregate("count(*)").fetchone()[0] == 2


def test_rewind_snapshot_captures_python_model(project, conn):
    from havn.engine.snapshots import get_snapshots_for_run, start_run

    run_id = start_run(project)
    _run(conn, project, run_id=run_id)
    names = {s.model_name for s in get_snapshots_for_run(project, run_id)}
    assert "silver.scores" in names


def test_contract_on_python_model(project, conn):
    from havn.engine.contracts import run_contracts

    _run(conn, project)
    _write(
        project / "contracts/scores.yml",
        """
        contracts:
          - name: scores_valid
            model: silver.scores
            assertions:
              - row_count > 0
              - unique(customer)
        """,
    )
    results = run_contracts(conn, project / "contracts")
    assert results and all(r.passed for r in results)
