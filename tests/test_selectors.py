"""Graph selectors: grammar, tags, and the callers that share them."""

from __future__ import annotations

from pathlib import Path

import pytest

from havn.engine.selectors import UNLIMITED, _parse_atom, select_models
from havn.engine.transform.analysis import validate_models
from havn.engine.transform.discovery import build_dag, discover_models


# ---------------------------------------------------------------------------
# A small synthetic DAG, shared by the grammar tests
# ---------------------------------------------------------------------------
#
#   bronze.customers ─┐
#                     ├─> silver.customers ──> gold.fct_orders
#   bronze.orders ────┘                   └──> gold.dim_customer
#   bronze.events ─────────────────────────────> gold.fct_events
#
# Tags:  daily  on the two bronze order/customer models and silver.customers
#        finance on gold.fct_orders
# Materializations: gold.fct_orders is incremental, everything else a table.


_DAG_FILES = {
    "bronze/customers.sql": (
        "@config materialized=table, tags=daily\n"
        "SELECT * FROM landing.raw_customers\n"
    ),
    "bronze/orders.sql": (
        "@config materialized=table, tags=daily\n"
        "SELECT * FROM landing.raw_orders\n"
    ),
    "bronze/events.sql": (
        "@config materialized=table\nSELECT * FROM landing.raw_events\n"
    ),
    "silver/customers.sql": (
        "@config materialized=table, tags=daily\n"
        "SELECT c.id FROM bronze.customers c JOIN bronze.orders o ON c.id = o.id\n"
    ),
    "gold/fct_orders.sql": (
        "@config materialized=incremental, unique_key=id, tags=finance\n"
        "SELECT id FROM silver.customers\n"
    ),
    "gold/dim_customer.sql": (
        "@config materialized=table\nSELECT id FROM silver.customers\n"
    ),
    "gold/fct_events.sql": (
        "@config materialized=table\nSELECT * FROM bronze.events\n"
    ),
}

ALL_MODELS = [
    "bronze.customers",
    "bronze.events",
    "bronze.orders",
    "gold.dim_customer",
    "gold.fct_events",
    "gold.fct_orders",
    "silver.customers",
]


@pytest.fixture
def dag_project(tmp_path):
    """A project directory holding the synthetic DAG above."""
    (tmp_path / "project.yml").write_text(
        "name: test\ndatabase:\n  path: warehouse.duckdb\n"
    )
    for rel, sql in _DAG_FILES.items():
        path = tmp_path / "transform" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sql)
    return tmp_path


@pytest.fixture
def dag_models(dag_project):
    return build_dag(discover_models(dag_project / "transform"))


def sel(models, *selectors, project_dir=None, **kwargs):
    """Resolve selectors and return the matches, sorted for comparison."""
    result = select_models(
        list(selectors), models, project_dir=project_dir, **kwargs
    )
    return sorted(result.selected)


# ---------------------------------------------------------------------------
# @config tags=
# ---------------------------------------------------------------------------


def _write(transform_dir: Path, rel: str, sql: str) -> Path:
    path = transform_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sql)
    return path


def test_tags_parsed_as_list(tmp_path):
    transform = tmp_path / "transform"
    _write(
        transform,
        "gold/orders.sql",
        "@config materialized=table, tags=daily,finance\nSELECT 1 AS x\n",
    )
    model = discover_models(transform)[0]
    assert model.tags == ["daily", "finance"]


def test_tags_default_to_empty(tmp_path):
    transform = tmp_path / "transform"
    _write(transform, "gold/orders.sql", "@config materialized=table\nSELECT 1 AS x\n")
    assert discover_models(transform)[0].tags == []


def test_tags_tolerate_spaces_after_commas(tmp_path):
    transform = tmp_path / "transform"
    _write(
        transform,
        "gold/orders.sql",
        "@config tags=daily, finance, nightly\nSELECT 1 AS x\n",
    )
    assert discover_models(transform)[0].tags == ["daily", "finance", "nightly"]


def test_retagging_does_not_change_content_hash(tmp_path):
    """Tags are metadata, not build semantics: retagging must not rebuild.

    Everything else that `@config` sets is folded into `content_hash` so a
    change rebuilds the model. Tags deliberately are not -- adding
    `tags=finance` to a hundred models should not rebuild a hundred tables.
    """
    transform = tmp_path / "transform"
    path = _write(
        transform, "gold/orders.sql", "@config materialized=table\nSELECT 1 AS x\n"
    )
    before = discover_models(transform)[0].content_hash

    path.write_text("@config materialized=table, tags=finance\nSELECT 1 AS x\n")
    after_model = discover_models(transform)[0]
    assert after_model.tags == ["finance"]
    assert after_model.content_hash == before

    # But a real config change still does move the hash.
    path.write_text("@config materialized=view, tags=finance\nSELECT 1 AS x\n")
    assert discover_models(transform)[0].content_hash != before


def test_tags_is_a_known_config_key(tmp_path):
    """`tags` must not be reported as an unknown @config key."""
    transform = tmp_path / "transform"
    _write(transform, "gold/orders.sql", "@config tags=daily\nSELECT 1 AS x\n")
    models = discover_models(transform)
    errors = validate_models(None, models)
    assert not [e for e in errors if "Unknown @config key" in e.message]


@pytest.mark.parametrize("bad", ["2fast", "has space", "with.dot", "with:colon"])
def test_validate_models_rejects_non_identifier_tags(tmp_path, bad):
    transform = tmp_path / "transform"
    _write(transform, "gold/orders.sql", f"@config tags={bad}\nSELECT 1 AS x\n")
    models = discover_models(transform)
    errors = validate_models(None, models)
    assert [e for e in errors if "Invalid tag" in e.message], (
        f"expected {bad!r} to be rejected"
    )


def test_validate_models_accepts_identifier_tags(tmp_path):
    transform = tmp_path / "transform"
    _write(
        transform,
        "gold/orders.sql",
        "@config tags=daily,finance_v2,end-of-day\nSELECT 1 AS x\n",
    )
    models = discover_models(transform)
    errors = validate_models(None, models)
    assert not [e for e in errors if "Invalid tag" in e.message]


# ---------------------------------------------------------------------------
# Atom parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "atom,core,up,down,at_sign",
    [
        ("gold.x", "gold.x", None, None, False),
        ("+gold.x", "gold.x", UNLIMITED, None, False),
        ("gold.x+", "gold.x", None, UNLIMITED, False),
        ("+gold.x+", "gold.x", UNLIMITED, UNLIMITED, False),
        ("2+gold.x", "gold.x", 2, None, False),
        ("gold.x+3", "gold.x", None, 3, False),
        ("2+gold.x+3", "gold.x", 2, 3, False),
        ("@gold.x", "gold.x", None, None, True),
        ("+downstream:gold.x", "gold.x", None, UNLIMITED, False),
        ("  gold.x  ", "gold.x", None, None, False),
    ],
)
def test_parse_atom(atom, core, up, down, at_sign):
    parsed = _parse_atom(atom)
    assert (parsed.core, parsed.up, parsed.down, parsed.at_sign) == (
        core, up, down, at_sign
    )


# ---------------------------------------------------------------------------
# Names and wildcards
# ---------------------------------------------------------------------------


def test_empty_selectors_select_everything(dag_models):
    assert sorted(select_models([], dag_models).selected) == ALL_MODELS
    assert sorted(select_models(None, dag_models).selected) == ALL_MODELS
    assert sorted(select_models(["all"], dag_models).selected) == ALL_MODELS


def test_exact_name(dag_models):
    assert sel(dag_models, "silver.customers") == ["silver.customers"]


def test_bare_name_matches_in_any_schema(dag_models):
    """`customers` names both bronze.customers and silver.customers."""
    assert sel(dag_models, "customers") == ["bronze.customers", "silver.customers"]


def test_bare_name_that_is_unique(dag_models):
    assert sel(dag_models, "fct_orders") == ["gold.fct_orders"]


def test_schema_wildcard(dag_models):
    assert sel(dag_models, "bronze.*") == [
        "bronze.customers", "bronze.events", "bronze.orders",
    ]


def test_prefix_wildcard_inside_a_schema(dag_models):
    """The regression this replaced: `gold.fct_*` used to match nothing."""
    assert sel(dag_models, "gold.fct_*") == ["gold.fct_events", "gold.fct_orders"]


def test_wildcard_in_the_schema_half(dag_models):
    assert sel(dag_models, "*.customers") == ["bronze.customers", "silver.customers"]


def test_bare_star_matches_everything(dag_models):
    assert sel(dag_models, "*") == ALL_MODELS


def test_selector_matching_nothing_warns_and_selects_nothing(dag_models):
    result = select_models(["nope.*"], dag_models)
    assert result.selected == []
    assert result.matched == {"nope.*": []}
    assert any("nope.*" in w for w in result.warnings)


def test_matched_records_what_each_selector_contributed(dag_models):
    result = select_models(["bronze.events", "+gold.fct_events"], dag_models)
    assert result.matched["bronze.events"] == ["bronze.events"]
    assert result.matched["+gold.fct_events"] == ["bronze.events", "gold.fct_events"]


# ---------------------------------------------------------------------------
# Graph operators
# ---------------------------------------------------------------------------


def test_upstream(dag_models):
    assert sel(dag_models, "+gold.fct_orders") == [
        "bronze.customers", "bronze.orders", "gold.fct_orders", "silver.customers",
    ]


def test_downstream(dag_models):
    assert sel(dag_models, "bronze.orders+") == [
        "bronze.orders", "gold.dim_customer", "gold.fct_orders", "silver.customers",
    ]


def test_both_directions(dag_models):
    assert sel(dag_models, "+silver.customers+") == [
        "bronze.customers", "bronze.orders", "gold.dim_customer",
        "gold.fct_orders", "silver.customers",
    ]


def test_n_plus_upstream_depth(dag_models):
    """1+ is one hop, 2+ reaches the bronze layer, bare + is unbounded."""
    assert sel(dag_models, "1+gold.fct_orders") == [
        "gold.fct_orders", "silver.customers",
    ]
    assert sel(dag_models, "2+gold.fct_orders") == [
        "bronze.customers", "bronze.orders", "gold.fct_orders", "silver.customers",
    ]


def test_n_plus_downstream_depth(dag_models):
    assert sel(dag_models, "bronze.orders+1") == [
        "bronze.orders", "silver.customers",
    ]
    assert sel(dag_models, "bronze.orders+2") == [
        "bronze.orders", "gold.dim_customer", "gold.fct_orders", "silver.customers",
    ]


def test_zero_depth_is_the_model_alone(dag_models):
    assert sel(dag_models, "0+gold.fct_orders") == ["gold.fct_orders"]


def test_at_sign_adds_ancestors_of_descendants(dag_models):
    """@bronze.customers must pull in bronze.orders.

    bronze.orders is neither upstream nor downstream of bronze.customers, but
    silver.customers (a descendant) needs it, so the selection would not
    build from scratch without it.
    """
    assert sel(dag_models, "@bronze.customers") == [
        "bronze.customers", "bronze.orders", "gold.dim_customer",
        "gold.fct_orders", "silver.customers",
    ]


def test_at_sign_on_a_leaf_is_just_its_upstream(dag_models):
    assert sel(dag_models, "@gold.fct_events") == [
        "bronze.events", "gold.fct_events",
    ]


def test_operators_combine_with_wildcards(dag_models):
    assert sel(dag_models, "+gold.*") == ALL_MODELS


# ---------------------------------------------------------------------------
# Methods
# ---------------------------------------------------------------------------


def test_tag_selector(dag_models):
    assert sel(dag_models, "tag:daily") == [
        "bronze.customers", "bronze.orders", "silver.customers",
    ]


def test_tag_selector_takes_wildcards(dag_models):
    assert sel(dag_models, "tag:fin*") == ["gold.fct_orders"]


def test_tag_selector_combines_with_operators(dag_models):
    assert sel(dag_models, "tag:finance+") == ["gold.fct_orders"]
    assert sel(dag_models, "+tag:finance") == [
        "bronze.customers", "bronze.orders", "gold.fct_orders", "silver.customers",
    ]


def test_unknown_tag_matches_nothing(dag_models):
    assert sel(dag_models, "tag:nope") == []


def test_path_selector_prefix(dag_project, dag_models):
    assert sel(dag_models, "path:transform/gold/", project_dir=dag_project) == [
        "gold.dim_customer", "gold.fct_events", "gold.fct_orders",
    ]


def test_path_selector_without_trailing_slash(dag_project, dag_models):
    assert sel(dag_models, "path:transform/gold", project_dir=dag_project) == [
        "gold.dim_customer", "gold.fct_events", "gold.fct_orders",
    ]


def test_path_selector_exact_file(dag_project, dag_models):
    assert sel(
        dag_models, "path:transform/silver/customers.sql", project_dir=dag_project
    ) == ["silver.customers"]


def test_path_selector_glob(dag_project, dag_models):
    assert sel(dag_models, "path:transform/*/fct_*.sql", project_dir=dag_project) == [
        "gold.fct_events", "gold.fct_orders",
    ]


def test_config_selector_on_materialization(dag_models):
    assert sel(dag_models, "config.materialized:incremental") == ["gold.fct_orders"]
    assert sel(dag_models, "config.materialized:table") == [
        "bronze.customers", "bronze.events", "bronze.orders",
        "gold.dim_customer", "gold.fct_events", "silver.customers",
    ]


def test_config_selector_on_any_key(dag_models):
    assert sel(dag_models, "config.unique_key:id") == ["gold.fct_orders"]
    assert sel(dag_models, "config.schema:gold") == [
        "gold.dim_customer", "gold.fct_events", "gold.fct_orders",
    ]


def test_config_selector_on_a_list_valued_key(dag_models):
    """`tags` is a list, so config.tags: matches any element."""
    assert sel(dag_models, "config.tags:finance") == ["gold.fct_orders"]


def test_config_selector_without_a_key_warns(dag_models):
    result = select_models(["config:table"], dag_models)
    assert result.selected == []
    assert any("config: needs a key" in w for w in result.warnings)


def test_unknown_state_selector_warns(dag_models):
    result = select_models(["state:ancient"], dag_models)
    assert result.selected == []
    assert any("state:ancient" in w for w in result.warnings)


def test_a_colon_that_is_not_a_method_is_a_name(dag_models):
    """`foo:bar` with an unknown head is treated as a (non-matching) name."""
    result = select_models(["weird:thing"], dag_models)
    assert result.selected == []
    assert any("matched no models" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# state:modified, against a real warehouse
# ---------------------------------------------------------------------------


def _build(project):
    """Run a full transform against the project's warehouse."""
    import duckdb

    from havn.engine.transform import run_transform

    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
        for name in ("raw_customers", "raw_orders", "raw_events"):
            conn.execute(
                f"CREATE OR REPLACE TABLE landing.{name} AS SELECT 1 AS id"
            )
        return run_transform(conn, project / "transform", project_dir=project)
    finally:
        conn.close()


def test_state_modified_after_a_full_build(dag_project):
    import duckdb

    _build(dag_project)
    models = build_dag(discover_models(dag_project / "transform"))
    conn = duckdb.connect(str(dag_project / "warehouse.duckdb"))
    try:
        assert sel(models, "state:modified", conn=conn) == []
    finally:
        conn.close()


def test_state_modified_sees_an_edited_model(dag_project):
    import duckdb

    _build(dag_project)
    (dag_project / "transform" / "silver" / "customers.sql").write_text(
        "@config materialized=table, tags=daily\n"
        "SELECT c.id FROM bronze.customers c "
        "JOIN bronze.orders o ON c.id = o.id WHERE c.id > 0\n"
    )
    models = build_dag(discover_models(dag_project / "transform"))
    conn = duckdb.connect(str(dag_project / "warehouse.duckdb"))
    try:
        # The edited model, plus the two downstream of it whose upstream hash
        # moved with it.
        assert sel(models, "state:modified", conn=conn) == [
            "gold.dim_customer", "gold.fct_orders", "silver.customers",
        ]
        assert sel(models, "state:modified+", conn=conn) == [
            "gold.dim_customer", "gold.fct_orders", "silver.customers",
        ]
    finally:
        conn.close()


def test_retagging_does_not_show_up_as_modified(dag_project):
    """The other half of "tags are not hashed", end to end."""
    import duckdb

    _build(dag_project)
    (dag_project / "transform" / "gold" / "dim_customer.sql").write_text(
        "@config materialized=table, tags=freshly_added\n"
        "SELECT id FROM silver.customers\n"
    )
    models = build_dag(discover_models(dag_project / "transform"))
    conn = duckdb.connect(str(dag_project / "warehouse.duckdb"))
    try:
        assert sel(models, "state:modified", conn=conn) == []
        assert sel(models, "tag:freshly_added", conn=conn) == ["gold.dim_customer"]
    finally:
        conn.close()


def test_state_without_a_connection_warns(dag_models):
    result = select_models(["state:modified"], dag_models)
    assert result.selected == []
    assert any("needs a warehouse connection" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Intersection, union, exclude
# ---------------------------------------------------------------------------


def test_comma_intersects(dag_models):
    assert sel(dag_models, "tag:daily,bronze.*") == [
        "bronze.customers", "bronze.orders",
    ]


def test_comma_intersects_after_operators(dag_models):
    """Each piece is expanded in full before the intersection."""
    assert sel(dag_models, "+gold.fct_orders,tag:daily") == [
        "bronze.customers", "bronze.orders", "silver.customers",
    ]


def test_comma_with_no_overlap_is_empty(dag_models):
    assert sel(dag_models, "tag:finance,bronze.*") == []


def test_multiple_selectors_union(dag_models):
    assert sel(dag_models, "bronze.events", "gold.fct_orders") == [
        "bronze.events", "gold.fct_orders",
    ]


def test_repeated_selector_is_idempotent(dag_models):
    assert sel(dag_models, "bronze.events", "bronze.events") == ["bronze.events"]


def test_exclude_subtracts(dag_models):
    assert sel(dag_models, "*", exclude=["gold.*"]) == [
        "bronze.customers", "bronze.events", "bronze.orders", "silver.customers",
    ]


def test_exclude_takes_the_full_grammar(dag_models):
    assert sel(dag_models, "*", exclude=["tag:daily"]) == [
        "bronze.events", "gold.dim_customer", "gold.fct_events", "gold.fct_orders",
    ]
    assert sel(dag_models, "*", exclude=["+gold.fct_orders"]) == [
        "bronze.events", "gold.dim_customer", "gold.fct_events",
    ]


def test_exclude_everything_leaves_nothing(dag_models):
    assert sel(dag_models, "*", exclude=["*"]) == []


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_selection_is_returned_in_dag_order(dag_models):
    selected = select_models(["+gold.fct_orders"], dag_models).selected
    assert selected.index("bronze.customers") < selected.index("silver.customers")
    assert selected.index("bronze.orders") < selected.index("silver.customers")
    assert selected.index("silver.customers") < selected.index("gold.fct_orders")


def test_selection_order_holds_for_unordered_input(dag_project):
    """Callers that skip build_dag still get a buildable order back."""
    models = discover_models(dag_project / "transform")
    selected = select_models(["+gold.fct_orders"], models).selected
    assert selected.index("silver.customers") < selected.index("gold.fct_orders")


# ---------------------------------------------------------------------------
# Jobs: resolve_execution_plan must not have changed
# ---------------------------------------------------------------------------


@pytest.fixture
def job_project(tmp_path):
    """The fixture tests/test_orchestration.py uses, rebuilt here."""
    (tmp_path / "project.yml").write_text(
        "name: test\ndatabase:\n  path: warehouse.duckdb\n"
    )
    (tmp_path / "orchestration").mkdir()
    (tmp_path / "transform" / "bronze").mkdir(parents=True)
    (tmp_path / "transform" / "silver").mkdir(parents=True)
    (tmp_path / "ingest").mkdir()
    (tmp_path / "export").mkdir()
    (tmp_path / "transform" / "bronze" / "orders.sql").write_text(
        "-- config: materialized=table, schema=bronze\n"
        "-- depends_on: landing.raw_orders\n\n"
        "SELECT * FROM landing.raw_orders\n"
    )
    (tmp_path / "transform" / "silver" / "orders.sql").write_text(
        "-- config: materialized=table, schema=silver\n"
        "-- depends_on: bronze.orders\n\n"
        "SELECT * FROM bronze.orders WHERE 1=1\n"
    )
    (tmp_path / "ingest" / "orders.py").write_text(
        "db.execute('CREATE SCHEMA IF NOT EXISTS landing')\n"
        "db.execute('CREATE OR REPLACE TABLE landing.raw_orders AS SELECT 1 AS id')\n"
    )
    (tmp_path / "export" / "report.py").write_text(
        "result = db.execute('SELECT * FROM silver.orders').fetchall()\n"
    )
    return tmp_path


def _plan(project, targets, **kwargs):
    from havn.engine.orchestration import resolve_execution_plan

    dag = build_dag(discover_models(project / "transform"))
    plan = resolve_execution_plan(targets, dag, project, **kwargs)
    return [(s.type, s.target) for s in plan.steps]


@pytest.mark.parametrize(
    "targets,kwargs,expected",
    [
        # Default resolve="upstream": a bare target means +target, and the
        # ingest script feeding landing.raw_orders is scheduled first.
        (
            "silver.orders", {},
            [("ingest", "ingest/orders.py"),
             ("transform", "bronze.orders"),
             ("transform", "silver.orders")],
        ),
        ("ingest/orders.py", {}, [("ingest", "ingest/orders.py")]),
        (
            "bronze.*", {},
            [("ingest", "ingest/orders.py"), ("transform", "bronze.orders")],
        ),
        # resolve="none" runs targets literally, no ingest step either.
        ("silver.orders", {"resolve": "none"}, [("transform", "silver.orders")]),
        (
            "+silver.orders", {"resolve": "none"},
            [("transform", "bronze.orders"), ("transform", "silver.orders")],
        ),
        (
            "bronze.orders+", {"resolve": "none"},
            [("transform", "bronze.orders"), ("transform", "silver.orders")],
        ),
        (
            "+silver.orders+", {"resolve": "none"},
            [("transform", "bronze.orders"), ("transform", "silver.orders")],
        ),
        ("silver.*", {"resolve": "none"}, [("transform", "silver.orders")]),
        (
            "bronze.*+", {"resolve": "none"},
            [("transform", "bronze.orders"), ("transform", "silver.orders")],
        ),
        # The legacy +downstream: prefix.
        (
            ["+downstream:bronze.orders"], {"resolve": "none"},
            [("transform", "bronze.orders"), ("transform", "silver.orders")],
        ),
        # A wildcard that matches nothing yields an empty plan, not an error.
        ("nonexistent_schema.*", {}, []),
    ],
)
def test_job_plans_are_unchanged(job_project, targets, kwargs, expected):
    assert _plan(job_project, targets, **kwargs) == expected


def test_job_export_target_pulls_in_referenced_models(job_project):
    steps = _plan(job_project, "export/report.py")
    assert ("export", "export/report.py") in steps
    assert ("transform", "silver.orders") in steps
    assert ("transform", "bronze.orders") in steps


def test_job_multi_target_unions(job_project):
    steps = _plan(job_project, ["bronze.orders", "silver.orders"], resolve="none")
    assert steps == [("transform", "bronze.orders"), ("transform", "silver.orders")]


def test_job_exclude_subtracts(job_project):
    steps = _plan(job_project, "+silver.orders", resolve="none", exclude=["bronze.*"])
    assert steps == [("transform", "silver.orders")]


def test_job_targets_take_the_new_grammar(job_project):
    """Selectors jobs never had, reaching them through the same entry point."""
    assert _plan(job_project, "1+silver.orders", resolve="none") == [
        ("transform", "bronze.orders"), ("transform", "silver.orders"),
    ]
    assert _plan(job_project, "@bronze.orders", resolve="none") == [
        ("transform", "bronze.orders"), ("transform", "silver.orders"),
    ]


def test_job_discovery_reads_exclude(job_project):
    from havn.engine.orchestration import discover_jobs

    (job_project / "orchestration" / "nightly.yml").write_text(
        "name: nightly\ntargets:\n  - silver.*\nexclude:\n  - bronze.*\n"
    )
    job = discover_jobs(job_project)[0]
    assert job.exclude == ["bronze.*"]


def test_save_job_rejects_traversal_in_exclude(job_project):
    from havn.engine.orchestration import save_job

    with pytest.raises(ValueError):
        save_job(job_project, {
            "name": "bad", "targets": ["silver.*"], "exclude": ["../../etc"],
        })


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _invoke(*args):
    from typer.testing import CliRunner

    from havn.cli import app

    return CliRunner().invoke(app, list(args))


def test_cli_transform_with_upstream_selector(dag_project):
    _build(dag_project)  # create the landing tables the bronze models read
    result = _invoke(
        "transform", "+gold.fct_orders", "-v", "--project", str(dag_project)
    )
    assert result.exit_code == 0, result.output
    # The upstream was pulled in; the unrelated branch was not.
    assert "silver.customers" in result.output
    assert "bronze.customers" in result.output
    assert "gold.fct_events" not in result.output


def test_cli_transform_reports_a_selector_that_matched_nothing(dag_project):
    result = _invoke("transform", "nope.*", "--project", str(dag_project))
    assert result.exit_code == 1
    assert "matched no models" in result.output


def test_cli_transform_exclude(dag_project):
    _build(dag_project)
    result = _invoke(
        "transform", "-s", "bronze.*", "-x", "tag:daily", "--force",
        "--project", str(dag_project),
    )
    assert result.exit_code == 0, result.output
    # Only the untagged bronze model survived the exclusion.
    assert "bronze.events" in result.output
    assert "bronze.customers" not in result.output
    assert "1 built" in result.output


def test_cli_ls_lists_every_model(dag_project):
    result = _invoke("ls", "--project", str(dag_project))
    assert result.exit_code == 0, result.output
    for name in ALL_MODELS:
        assert name in result.output
    # The tags and materialization columns are populated.
    assert "daily" in result.output
    assert "incremental" in result.output


def test_cli_ls_names_only(dag_project):
    result = _invoke(
        "ls", "+gold.fct_orders", "--names", "--project", str(dag_project)
    )
    assert result.exit_code == 0, result.output
    assert sorted(result.output.split()) == [
        "bronze.customers", "bronze.orders", "gold.fct_orders", "silver.customers",
    ]


def test_cli_ls_exits_nonzero_when_nothing_matched(dag_project):
    result = _invoke("ls", "tag:nope", "--project", str(dag_project))
    assert result.exit_code == 1
    assert "matched no models" in result.output


def test_cli_ls_needs_no_warehouse(dag_project):
    """A project that has never been built can still dry-run selectors."""
    assert not (dag_project / "warehouse.duckdb").exists()
    result = _invoke("ls", "tag:daily", "--names", "--project", str(dag_project))
    assert result.exit_code == 0, result.output
    assert "silver.customers" in result.output


def test_cli_ls_state_modified_without_a_warehouse_matches_everything(dag_project):
    """`ls` said "state: selectors match every model" and then selected none.

    `havn transform state:modified` on the same project opens (and creates)
    the warehouse, finds no model_state and builds everything, so `ls` has to
    give the same answer -- without creating the file.
    """
    assert not (dag_project / "warehouse.duckdb").exists()
    result = _invoke(
        "ls", "state:modified", "--names", "--project", str(dag_project)
    )
    assert result.exit_code == 0, result.output
    assert set(result.output.split()) >= set(ALL_MODELS)
    # Listing is not building: no warehouse was created on the way.
    assert not (dag_project / "warehouse.duckdb").exists()


def test_cli_ls_and_transform_agree_on_state_modified(dag_project):
    """The two commands answer the same selector the same way.

    The build itself fails on this fixture (the landing tables only exist
    after ``_build`` creates them), which does not matter: what is compared is
    the selection each command made, and every selected model is named in the
    transform output whether it built or failed.
    """
    listed = _invoke(
        "ls", "state:modified", "--names", "--project", str(dag_project)
    )
    assert listed.exit_code == 0, listed.output
    selected = set(listed.output.split()) & set(ALL_MODELS)
    assert selected

    built = _invoke("transform", "state:modified", "--project", str(dag_project))
    assert "No models matched targets" not in built.output
    for name in sorted(selected):
        assert name in built.output


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture
def dag_client(dag_project):
    from fastapi.testclient import TestClient

    import havn.server.app as server_app
    from havn.server.deps import reset_shared_conn

    _build(dag_project)
    reset_shared_conn()
    server_app.PROJECT_DIR = dag_project
    server_app.AUTH_ENABLED = False
    yield TestClient(server_app.app)
    reset_shared_conn()


def test_api_models_select_filters_the_list(dag_client):
    names = [m["full_name"] for m in dag_client.get("/api/models").json()]
    assert sorted(names) == ALL_MODELS

    filtered = dag_client.get("/api/models", params={"select": "tag:daily"}).json()
    assert sorted(m["full_name"] for m in filtered) == [
        "bronze.customers", "bronze.orders", "silver.customers",
    ]
    assert all("daily" in m["tags"] for m in filtered)


def test_api_models_select_takes_operators_and_wildcards(dag_client):
    resp = dag_client.get("/api/models", params={"select": "+gold.fct_orders"})
    assert sorted(m["full_name"] for m in resp.json()) == [
        "bronze.customers", "bronze.orders", "gold.fct_orders", "silver.customers",
    ]
    resp = dag_client.get("/api/models", params={"select": "gold.fct_*"})
    assert sorted(m["full_name"] for m in resp.json()) == [
        "gold.fct_events", "gold.fct_orders",
    ]


def test_api_transform_takes_selectors(dag_client):
    resp = dag_client.post(
        "/api/transform", json={"targets": ["+gold.fct_orders"], "force": True}
    )
    assert resp.status_code == 200, resp.text
    assert sorted(resp.json()["results"]) == [
        "bronze.customers", "bronze.orders", "gold.fct_orders", "silver.customers",
    ]


def test_api_transform_takes_exclude(dag_client):
    resp = dag_client.post(
        "/api/transform",
        json={"targets": ["bronze.*"], "exclude": ["tag:daily"], "force": True},
    )
    assert resp.status_code == 200, resp.text
    assert sorted(resp.json()["results"]) == ["bronze.events"]


def test_api_transform_exact_name_still_works(dag_client):
    """What the UI's "run model" button sends."""
    resp = dag_client.post(
        "/api/transform", json={"targets": ["gold.fct_events"], "force": True}
    )
    assert resp.status_code == 200, resp.text
    assert sorted(resp.json()["results"]) == ["gold.fct_events"]
