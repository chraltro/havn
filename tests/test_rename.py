"""Tests for the column rename engine, its CLI and its endpoints."""

from __future__ import annotations

import os
import stat

import duckdb
import pytest
from typer.testing import CliRunner

from havn.cli import app
from havn.engine.rename import (
    RenameError,
    apply_rename,
    find_column_references,
    plan_rename,
    schemas_from_models,
)
from havn.engine.transform import discover_models

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def write(project, schema, name, sql):
    path = project / "transform" / schema / f"{name}.sql"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sql)
    return path


@pytest.fixture
def project(tmp_path):
    """A project whose models reach ``bronze.customers.customer_id`` every way.

    bronze defines it with an alias; silver projects, filters, joins and
    groups on it and re-exports it under the same name; gold reads silver two
    different ways, one of which re-aliases and so ends the chain.
    """
    (tmp_path / "project.yml").write_text(
        "name: renametest\ndatabase:\n  path: warehouse.duckdb\n"
    )
    write(
        tmp_path,
        "bronze",
        "customers",
        "@config materialized=table\n"
        "@description raw customers\n"
        "\n"
        "SELECT\n"
        "    id AS customer_id,\n"
        "    name,\n"
        "    region\n"
        "FROM landing.customers\n",
    )
    write(
        tmp_path,
        "bronze",
        "orders",
        "@config materialized=table\n"
        "\n"
        "SELECT order_id, cust AS customer_ref, amount\n"
        "FROM landing.orders\n",
    )
    write(
        tmp_path,
        "silver",
        "customers",
        "@config materialized=table\n"
        "\n"
        "SELECT\n"
        "    c.customer_id,\n"
        "    c.name,\n"
        "    COUNT(o.order_id) AS order_count\n"
        "FROM bronze.customers c\n"
        "LEFT JOIN bronze.orders o ON c.customer_id = o.customer_ref\n"
        "WHERE c.customer_id > 0\n"
        "GROUP BY c.customer_id, c.name\n",
    )
    write(
        tmp_path,
        "gold",
        "customer_report",
        "@config materialized=view\n"
        "\n"
        "SELECT customer_id, order_count\n"
        "FROM silver.customers\n"
        "WHERE customer_id IS NOT NULL\n",
    )
    write(
        tmp_path,
        "gold",
        "renamed",
        "SELECT customer_id AS account_id, order_count FROM silver.customers\n",
    )
    return tmp_path


@pytest.fixture
def models(project):
    return discover_models(project / "transform")


@pytest.fixture
def schemas(models):
    return schemas_from_models(models)


def index(project, models, schemas, model="bronze.customers", column="customer_id"):
    return find_column_references(
        models, model, column, schemas=schemas, project_dir=project
    )


def sites_in(report, path):
    return [s for s in report.sites if s.path == path]


# ---------------------------------------------------------------------------
# The offset assumption the whole module rests on
# ---------------------------------------------------------------------------


def test_directives_make_query_offsets_differ_from_file_offsets(models):
    """The premise for the line map: only line *numbers* survive stripping."""
    bronze = next(m for m in models if m.full_name == "bronze.customers")
    assert len(bronze.sql) != len(bronze.query)
    assert bronze.sql.count("\n") == bronze.query.count("\n")


def test_every_site_lands_on_the_identifier_in_the_file(project, models, schemas):
    """Each offset is verified against the file, not merely recorded."""
    report = index(project, models, schemas)
    assert report.sites
    for site in report.sites:
        text = (project / site.path).read_text()[site.start : site.end]
        assert text.strip('"').lower() in ("customer_id", site.text.strip('"').lower())


# ---------------------------------------------------------------------------
# What the index finds
# ---------------------------------------------------------------------------


def test_definition_site_is_the_alias_when_the_column_is_aliased(
    project, models, schemas
):
    report = index(project, models, schemas)
    definition = [s for s in report.sites if s.kind == "definition"]
    assert len(definition) == 1
    assert definition[0].model == "bronze.customers"
    assert definition[0].line == 5
    assert definition[0].needs_alias is False
    body = (project / definition[0].path).read_text()
    assert body[definition[0].start : definition[0].end] == "customer_id"


def test_definition_site_without_an_alias_asks_for_one(project, models, schemas):
    """Rewriting a bare projection would point at a column that is not there."""
    write(
        project,
        "silver",
        "plain",
        "SELECT customer_id FROM bronze.customers\n",
    )
    fresh = discover_models(project / "transform")
    report = find_column_references(
        fresh, "silver.plain", "customer_id", project_dir=project
    )
    definition = [s for s in report.sites if s.kind == "definition"]
    assert len(definition) == 1
    assert definition[0].needs_alias is True

    edits = plan_rename(report, "customer_id", "cust_id", force=True)
    edit = next(e for e in edits if e.path == "transform/silver/plain.sql")
    assert edit.new_text == "customer_id AS cust_id"


def test_projection_where_join_and_group_by_are_all_found(project, models, schemas):
    report = index(project, models, schemas)
    silver = sites_in(report, "transform/silver/customers.sql")
    assert sorted(s.clause for s in silver) == ["group", "join", "select", "where"]
    assert all(s.kind == "reference" and s.resolved for s in silver)


def test_a_where_only_consumer_is_found(project, models, schemas):
    """The case the old lineage walker could not see at all."""
    write(
        project,
        "gold",
        "filtered",
        "SELECT order_count FROM silver.customers WHERE customer_id > 10\n",
    )
    fresh = discover_models(project / "transform")
    report = find_column_references(
        fresh, "bronze.customers", "customer_id", project_dir=project
    )
    filtered = sites_in(report, "transform/gold/filtered.sql")
    assert [s.clause for s in filtered] == ["where"]


def test_the_rename_follows_a_re_export_two_levels_down(project, models, schemas):
    report = index(project, models, schemas)
    assert "gold.customer_report" in report.models
    gold = sites_in(report, "transform/gold/customer_report.sql")
    assert sorted(s.clause for s in gold) == ["select", "where"]


def test_a_downstream_alias_is_reported_and_never_edited(project, models, schemas):
    report = index(project, models, schemas)
    renamed = sites_in(report, "transform/gold/renamed.sql")
    kinds = {s.kind for s in renamed}
    assert kinds == {"reference", "alias"}

    edits = plan_rename(report, "customer_id", "cust_id", force=True, schemas=schemas)
    for edit in edits:
        if edit.path == "transform/gold/renamed.sql":
            assert edit.kind == "reference"
    body = apply_rename(project, edits, dry_run=True)["transform/gold/renamed.sql"]
    assert body.strip().startswith("SELECT cust_id AS account_id")


def test_the_chain_stops_at_a_re_alias(project, models, schemas):
    """gold.renamed exports account_id, so its own children keep that name."""
    write(
        project,
        "gold",
        "downstream_of_alias",
        "SELECT account_id FROM gold.renamed\n",
    )
    fresh = discover_models(project / "transform")
    report = find_column_references(
        fresh, "bronze.customers", "customer_id", project_dir=project
    )
    assert not sites_in(report, "transform/gold/downstream_of_alias.sql")


def test_the_column_is_followed_through_a_cte_chain(project, models, schemas):
    """A CTE that re-exports the column passes it to whatever reads the CTE."""
    write(
        project,
        "gold",
        "through_cte",
        "WITH base AS (\n"
        "    SELECT customer_id, order_count FROM silver.customers\n"
        "),\n"
        "ranked AS (\n"
        "    SELECT customer_id, order_count FROM base WHERE customer_id > 0\n"
        ")\n"
        "SELECT r.customer_id FROM ranked r\n",
    )
    fresh = discover_models(project / "transform")
    report = find_column_references(
        fresh, "bronze.customers", "customer_id", schemas=schemas, project_dir=project
    )
    lines = sorted(s.line for s in sites_in(report, "transform/gold/through_cte.sql"))
    assert lines == [2, 5, 5, 7]

    edits = plan_rename(report, "customer_id", "cust_id", force=True, schemas=schemas)
    body = apply_rename(project, edits, dry_run=True)["transform/gold/through_cte.sql"]
    assert "customer_id" not in body
    assert body.count("cust_id") == 4


def test_a_star_inside_a_cte_over_the_column_blocks(project, models, schemas):
    write(
        project,
        "gold",
        "star_cte",
        "WITH base AS (SELECT * FROM silver.customers)\n"
        "SELECT order_count FROM base\n",
    )
    fresh = discover_models(project / "transform")
    report = find_column_references(
        fresh, "bronze.customers", "customer_id", schemas=schemas, project_dir=project
    )
    star = [b for b in report.blocked if b.reason == "select_star"]
    assert [b.path for b in star] == ["transform/gold/star_cte.sql"]


def test_an_unqualified_reference_resolves_from_the_one_relation_in_scope(
    project, models
):
    report = find_column_references(
        models, "bronze.customers", "customer_id", project_dir=project
    )
    gold = sites_in(report, "transform/gold/customer_report.sql")
    assert gold and all(s.resolved for s in gold)


def test_an_unqualified_reference_two_relations_could_own_is_a_blocker(project):
    """No schema, two candidates: report it rather than pick one."""
    write(project, "bronze", "a", "SELECT 1 AS customer_id, 2 AS x\n")
    write(project, "bronze", "b", "SELECT 3 AS customer_id, 4 AS y\n")
    write(
        project,
        "silver",
        "joined",
        "SELECT x, y FROM bronze.a JOIN bronze.b ON customer_id = customer_id\n",
    )
    fresh = [
        m
        for m in discover_models(project / "transform")
        if m.full_name in ("bronze.a", "bronze.b", "silver.joined")
    ]
    report = find_column_references(
        fresh, "bronze.a", "customer_id", project_dir=project
    )
    reasons = {b.reason for b in report.blocked}
    assert "unresolved_reference" in reasons
    unresolved = [s for s in report.sites if not s.resolved and s.kind == "reference"]
    assert unresolved
    edits = plan_rename(report, "customer_id", "cust_id", force=True)
    assert not [e for e in edits if e.path == "transform/silver/joined.sql"]


def test_a_select_star_consumer_produces_a_blocker(project, models, schemas):
    write(project, "gold", "everything", "SELECT * FROM silver.customers\n")
    fresh = discover_models(project / "transform")
    report = find_column_references(
        fresh, "bronze.customers", "customer_id", schemas=schemas, project_dir=project
    )
    star = [b for b in report.blocked if b.reason == "select_star"]
    assert len(star) == 1
    assert star[0].path == "transform/gold/everything.sql"


def test_a_star_over_an_unrelated_relation_is_not_a_blocker(project, models, schemas):
    """Scoped to relations that carry the column, or every project blocks."""
    write(project, "gold", "unrelated", "SELECT * FROM bronze.orders\n")
    fresh = discover_models(project / "transform")
    report = find_column_references(
        fresh, "bronze.customers", "customer_id", schemas=schemas, project_dir=project
    )
    assert not [b for b in report.blocked if b.reason == "select_star"]


def test_union_by_name_and_columns_expressions_block(project, schemas):
    write(
        project,
        "gold",
        "by_name",
        "SELECT customer_id FROM silver.customers\n"
        "UNION BY NAME\n"
        "SELECT customer_id FROM silver.customers\n",
    )
    fresh = discover_models(project / "transform")
    report = find_column_references(
        fresh, "bronze.customers", "customer_id", schemas=schemas, project_dir=project
    )
    assert "union_by_name" in {b.reason for b in report.blocked}


def test_a_metrics_yaml_mention_is_a_yaml_site_and_a_blocker(project, models, schemas):
    (project / "metrics").mkdir()
    (project / "metrics" / "revenue.yml").write_text(
        "metrics:\n"
        "  - name: revenue\n"
        "    model: silver.customers\n"
        "    dimensions: [customer_id]\n"
    )
    report = index(project, models, schemas)
    yaml_sites = [s for s in report.sites if s.kind == "yaml"]
    assert len(yaml_sites) == 1
    assert yaml_sites[0].path == "metrics/revenue.yml"
    assert yaml_sites[0].line == 4
    assert "yaml_mention" in {b.reason for b in report.blocked}

    edits = plan_rename(report, "customer_id", "cust_id", force=True, schemas=schemas)
    body = apply_rename(project, edits, dry_run=True)["metrics/revenue.yml"]
    assert "dimensions: [cust_id]" in body


def test_a_contracts_yaml_mention_is_found_too(project, models, schemas):
    (project / "contracts").mkdir()
    (project / "contracts" / "c.yml").write_text(
        "contracts:\n  - model: silver.customers\n    not_null: [customer_id]\n"
    )
    report = index(project, models, schemas)
    assert [s.path for s in report.sites if s.kind == "yaml"] == ["contracts/c.yml"]


def test_an_unknown_model_is_an_error(models):
    with pytest.raises(RenameError, match="Unknown model"):
        find_column_references(models, "gold.nope", "x")


def test_a_column_the_model_does_not_output_is_a_blocker(project, models, schemas):
    report = index(project, models, schemas, column="not_a_column")
    assert [b.reason for b in report.blocked] == ["no_definition"]


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def test_edits_are_grouped_per_file_and_descend_by_offset(project, models, schemas):
    report = index(project, models, schemas)
    edits = plan_rename(report, "customer_id", "cust_id", schemas=schemas)
    by_file = {}
    for edit in edits:
        by_file.setdefault(edit.path, []).append(edit.start)
    for path, starts in by_file.items():
        assert starts == sorted(starts, reverse=True), path


def test_blockers_refuse_a_plan_unless_forced(project, models, schemas):
    write(project, "gold", "everything", "SELECT * FROM silver.customers\n")
    fresh = discover_models(project / "transform")
    report = find_column_references(
        fresh, "bronze.customers", "customer_id", schemas=schemas, project_dir=project
    )
    with pytest.raises(RenameError, match="blocker"):
        plan_rename(report, "customer_id", "cust_id", schemas=schemas)
    assert plan_rename(report, "customer_id", "cust_id", force=True, schemas=schemas)


def test_a_new_name_that_is_not_an_identifier_is_refused(project, models, schemas):
    report = index(project, models, schemas)
    for bad in ("2cool", "cust id", "cust-id", "", "drop table x"):
        with pytest.raises(RenameError, match="not a valid column name"):
            plan_rename(report, "customer_id", bad, schemas=schemas)


def test_renaming_to_the_current_name_is_refused(project, models, schemas):
    report = index(project, models, schemas)
    with pytest.raises(RenameError, match="already the column"):
        plan_rename(report, "customer_id", "customer_id", schemas=schemas)


def test_a_collision_with_an_existing_column_is_refused(project, models, schemas):
    report = index(project, models, schemas)
    with pytest.raises(RenameError, match="already has a column"):
        plan_rename(report, "customer_id", "region", schemas=schemas)


def test_plan_accepts_a_bare_list_of_sites(project, models, schemas):
    report = index(project, models, schemas)
    edits = plan_rename(
        list(report), "customer_id", "cust_id", target_model=report.target
    )
    assert edits


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def test_dry_run_leaves_every_file_untouched(project, models, schemas):
    report = index(project, models, schemas)
    edits = plan_rename(report, "customer_id", "cust_id", schemas=schemas)
    before = {
        path: (project / path).read_text() for path in {e.path for e in edits}
    }
    result = apply_rename(project, edits, dry_run=True)
    for path, body in before.items():
        assert (project / path).read_text() == body
        assert result[path] != body


def test_apply_rewrites_every_site(project, models, schemas):
    report = index(project, models, schemas)
    edits = plan_rename(report, "customer_id", "cust_id", schemas=schemas)
    apply_rename(project, edits)
    for path in {e.path for e in edits}:
        body = (project / path).read_text()
        assert "customer_id" not in body
        assert "cust_id" in body
    # The models that were never part of the plan are byte-identical.
    assert "customer_ref" in (project / "transform/bronze/orders.sql").read_text()


def test_the_project_still_discovers_and_binds_after_a_rename(project, models, schemas):
    """The real check: the renamed SQL resolves through the DuckDB binder."""
    from havn.engine.database import ensure_meta_table
    from havn.engine.transform.bind import bind_models

    report = index(project, models, schemas)
    apply_rename(project, plan_rename(report, "customer_id", "cust_id", schemas=schemas))

    conn = duckdb.connect(str(project / "warehouse.duckdb"))
    try:
        ensure_meta_table(conn)
        conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
        conn.execute(
            "CREATE TABLE landing.customers (id INTEGER, name VARCHAR, region VARCHAR)"
        )
        conn.execute("CREATE TABLE landing.orders (order_id INTEGER, cust INTEGER, amount DOUBLE)")
        fresh = discover_models(project / "transform")
        assert {m.full_name for m in fresh} == {
            "bronze.customers",
            "bronze.orders",
            "silver.customers",
            "gold.customer_report",
            "gold.renamed",
        }
        result = bind_models(conn, fresh, project_dir=project)
        if not result.available:
            pytest.skip("bind pass unavailable on this backend")
        assert result.ok, result.errors
        assert [
            name for name, _type in result.schemas["silver.customers"]
        ][0] == "cust_id"
    finally:
        conn.close()


def test_a_stale_file_fails_the_apply_before_anything_is_written(
    project, models, schemas
):
    report = index(project, models, schemas)
    edits = plan_rename(report, "customer_id", "cust_id", schemas=schemas)
    target = project / "transform" / "silver" / "customers.sql"
    target.write_text("SELECT 1 AS x\n")
    with pytest.raises(RenameError, match="changed since the plan"):
        apply_rename(project, edits)
    assert "customer_id" in (project / "transform/bronze/customers.sql").read_text()


def test_a_read_only_file_rolls_every_other_file_back(project, models, schemas):
    """A partial write is worse than no write, so it is undone."""
    report = index(project, models, schemas)
    edits = plan_rename(report, "customer_id", "cust_id", schemas=schemas)
    paths = sorted({e.path for e in edits})
    assert len(paths) > 1
    before = {path: (project / path).read_text() for path in paths}

    # The last path in write order, so an earlier file is already written when
    # this one is refused.
    blocked = project / paths[-1]
    blocked.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    try:
        with pytest.raises(RenameError, match="read-only"):
            apply_rename(project, edits)
    finally:
        blocked.chmod(stat.S_IRUSR | stat.S_IWUSR)

    for path, body in before.items():
        assert (project / path).read_text() == body
    # No temporary file was left behind.
    leftovers = list((project / "transform").rglob(".*havn-rename"))
    assert leftovers == []


def test_apply_reports_a_missing_file_rather_than_writing_the_rest(
    project, models, schemas
):
    report = index(project, models, schemas)
    edits = plan_rename(report, "customer_id", "cust_id", schemas=schemas)
    os.remove(project / "transform" / "silver" / "customers.sql")
    with pytest.raises(RenameError, match="file not found"):
        apply_rename(project, edits)
    assert "customer_id" in (project / "transform/bronze/customers.sql").read_text()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_dry_run_lists_the_sites_and_writes_nothing(project):
    body = (project / "transform" / "silver" / "customers.sql").read_text()
    result = runner.invoke(
        app,
        ["rename-column", "bronze.customers", "customer_id", "cust_id",
         "--dry-run", "-p", str(project)],
    )
    assert result.exit_code == 0, result.output
    assert "transform/silver/customers.sql" in result.output
    assert "nothing written" in result.output
    assert (project / "transform" / "silver" / "customers.sql").read_text() == body


def test_cli_refuses_when_blocked_and_names_the_way_past(project):
    write(project, "gold", "everything", "SELECT * FROM silver.customers\n")
    result = runner.invoke(
        app,
        ["rename-column", "bronze.customers", "customer_id", "cust_id",
         "-p", str(project)],
    )
    assert result.exit_code == 1
    assert "blocked" in result.output
    assert "--force" in result.output


def test_cli_applies_after_confirmation(project):
    result = runner.invoke(
        app,
        ["rename-column", "bronze.customers", "customer_id", "cust_id",
         "-p", str(project)],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert "cust_id" in (project / "transform" / "gold" / "customer_report.sql").read_text()


def test_cli_cancels_on_a_no(project):
    body = (project / "transform" / "bronze" / "customers.sql").read_text()
    result = runner.invoke(
        app,
        ["rename-column", "bronze.customers", "customer_id", "cust_id",
         "-p", str(project)],
        input="n\n",
    )
    assert result.exit_code == 1
    assert (project / "transform" / "bronze" / "customers.sql").read_text() == body


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture
def client(project):
    from fastapi.testclient import TestClient

    import havn.server.app as server_app
    from havn.server.deps import reset_shared_conn

    duckdb.connect(str(project / "warehouse.duckdb")).close()
    reset_shared_conn()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = False
    yield TestClient(server_app.app)
    reset_shared_conn()


def test_api_references_lists_sites_and_blockers(client, project):
    write(project, "gold", "everything", "SELECT * FROM silver.customers\n")
    r = client.get(
        "/api/rename/references",
        params={"model": "bronze.customers", "column": "customer_id"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "bronze.customers"
    assert any(s["kind"] == "definition" for s in body["sites"])
    assert any(s["clause"] == "join" for s in body["sites"])
    assert {b["reason"] for b in body["blocked"]} == {"select_star"}


def test_api_references_404s_on_an_unknown_model(client):
    r = client.get(
        "/api/rename/references", params={"model": "gold.nope", "column": "x"}
    )
    assert r.status_code == 404


def test_api_plan_returns_edits_and_the_resulting_contents(client):
    r = client.post(
        "/api/rename/plan",
        json={"model": "bronze.customers", "column": "customer_id", "new_name": "cust_id"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["edits"]
    paths = {f["path"] for f in body["files"]}
    assert "transform/silver/customers.sql" in paths
    for entry in body["files"]:
        assert "cust_id" in entry["content"]
        assert entry["file_hash"]


def test_api_plan_reports_a_refusal_instead_of_raising(client):
    r = client.post(
        "/api/rename/plan",
        json={"model": "bronze.customers", "column": "customer_id", "new_name": "region"},
    )
    assert r.status_code == 200
    assert "already has a column" in r.json()["error"]
    assert r.json()["edits"] == []


def test_api_apply_writes_every_file(client, project):
    plan = client.post(
        "/api/rename/plan",
        json={"model": "bronze.customers", "column": "customer_id", "new_name": "cust_id"},
    ).json()
    hashes = {f["path"]: f["file_hash"] for f in plan["files"]}
    r = client.post(
        "/api/rename/apply",
        json={
            "model": "bronze.customers",
            "column": "customer_id",
            "new_name": "cust_id",
            "hashes": hashes,
        },
    )
    assert r.status_code == 200, r.text
    assert "cust_id" in (project / "transform" / "silver" / "customers.sql").read_text()


def test_api_apply_409s_on_a_stale_hash(client, project):
    plan = client.post(
        "/api/rename/plan",
        json={"model": "bronze.customers", "column": "customer_id", "new_name": "cust_id"},
    ).json()
    hashes = {f["path"]: f["file_hash"] for f in plan["files"]}
    hashes["transform/silver/customers.sql"] = "0000000000000000"
    r = client.post(
        "/api/rename/apply",
        json={
            "model": "bronze.customers",
            "column": "customer_id",
            "new_name": "cust_id",
            "hashes": hashes,
        },
    )
    assert r.status_code == 409
    assert r.json()["stale"] == ["transform/silver/customers.sql"]
    assert "customer_id" in (project / "transform" / "bronze" / "customers.sql").read_text()


def test_api_apply_refuses_a_blocked_rename_without_force(client, project):
    write(project, "gold", "everything", "SELECT * FROM silver.customers\n")
    r = client.post(
        "/api/rename/apply",
        json={
            "model": "bronze.customers",
            "column": "customer_id",
            "new_name": "cust_id",
            "hashes": {},
        },
    )
    assert r.status_code == 400
    assert "blocker" in r.json()["detail"]


def test_batch_file_write_is_all_or_nothing(client, project):
    first = "transform/bronze/customers.sql"
    second = "transform/silver/customers.sql"
    original = (project / second).read_text()
    r = client.put(
        "/api/files",
        json={
            "files": [
                {"path": first, "content": "SELECT 1 AS a\n"},
                {"path": second, "content": "SELECT 2 AS b\n", "expected_hash": "bad"},
            ]
        },
    )
    assert r.status_code == 409
    assert r.json()["stale"] == [second]
    assert (project / second).read_text() == original
    assert "customer_id" in (project / first).read_text()


def test_batch_file_write_saves_both_files(client, project):
    r = client.put(
        "/api/files",
        json={
            "files": [
                {"path": "transform/bronze/customers.sql", "content": "SELECT 1 AS a\n"},
                {"path": "transform/silver/customers.sql", "content": "SELECT 2 AS b\n"},
            ]
        },
    )
    assert r.status_code == 200
    assert (project / "transform" / "bronze" / "customers.sql").read_text() == "SELECT 1 AS a\n"
    assert (project / "transform" / "silver" / "customers.sql").read_text() == "SELECT 2 AS b\n"
