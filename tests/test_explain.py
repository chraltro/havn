"""Tests for query plan parsing and visualization endpoints."""

from __future__ import annotations

import json

import duckdb
import pytest

from havn.engine.explain import (
    PlanNode,
    _parse_json_plan,
    enrich_plan_dict,
    explain_analyze_query,
    explain_query,
    plan_to_dict,
)


@pytest.fixture()
def conn():
    """Create an in-memory DuckDB connection with test tables."""
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE test(id INT, name VARCHAR, value DOUBLE)")
    c.execute("INSERT INTO test VALUES (1, 'a', 1.5), (2, 'b', 2.5), (3, 'c', 3.5)")
    c.execute("CREATE TABLE orders(id INT, test_id INT, amount DOUBLE)")
    c.execute(
        "INSERT INTO orders VALUES (1, 1, 100), (2, 2, 200), (3, 1, 150)"
    )
    yield c
    c.close()


# ── EXPLAIN parsing ──────────────────────────────────────────────────────


class TestExplainQuery:
    def test_simple_select(self, conn):
        plan, raw = explain_query(conn, "SELECT * FROM test")
        assert isinstance(plan, PlanNode)
        assert raw  # raw text is non-empty
        assert plan.operator != "EMPTY"

    def test_filter_query(self, conn):
        plan, raw = explain_query(conn, "SELECT * FROM test WHERE id > 1")
        d = plan_to_dict(plan)
        # Should contain a scan somewhere in the tree
        operators = _collect_operators(d)
        assert any("SCAN" in op.upper() for op in operators)

    def test_join_query(self, conn):
        plan, raw = explain_query(
            conn,
            "SELECT t.name, SUM(o.amount) "
            "FROM test t JOIN orders o ON t.id = o.test_id "
            "GROUP BY t.name",
        )
        d = plan_to_dict(plan)
        operators = _collect_operators(d)
        assert any("JOIN" in op.upper() for op in operators)
        assert any("SCAN" in op.upper() for op in operators)

    def test_subquery(self, conn):
        plan, raw = explain_query(
            conn,
            "SELECT * FROM (SELECT id, COUNT(*) AS cnt FROM test GROUP BY id) sub WHERE cnt > 0",
        )
        d = plan_to_dict(plan)
        assert d["operator"]  # has an operator

    def test_cte_query(self, conn):
        plan, raw = explain_query(
            conn,
            "WITH ranked AS (SELECT *, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM test) "
            "SELECT * FROM ranked WHERE rn <= 2",
        )
        d = plan_to_dict(plan)
        assert d["operator"]

    def test_aggregation(self, conn):
        plan, raw = explain_query(
            conn,
            "SELECT name, COUNT(*), SUM(value) FROM test GROUP BY name",
        )
        d = plan_to_dict(plan)
        operators = _collect_operators(d)
        assert any("GROUP" in op.upper() or "AGGREGATE" in op.upper() for op in operators)


# ── EXPLAIN ANALYZE parsing ──────────────────────────────────────────────


class TestExplainAnalyzeQuery:
    def test_simple_analyze(self, conn):
        plan, raw = explain_analyze_query(conn, "SELECT * FROM test")
        assert isinstance(plan, PlanNode)
        assert raw

    def test_captures_timing(self, conn):
        plan, raw = explain_analyze_query(conn, "SELECT * FROM test WHERE id > 1")
        d = plan_to_dict(plan)
        # At least one node should have timing data
        has_timing = _any_node(d, lambda n: n.get("actual_time_ms") is not None)
        assert has_timing, f"No timing data found in plan: {json.dumps(d, indent=2)}"

    def test_captures_actual_rows(self, conn):
        plan, raw = explain_analyze_query(conn, "SELECT * FROM test")
        d = plan_to_dict(plan)
        has_rows = _any_node(d, lambda n: n.get("actual_rows") is not None)
        assert has_rows, f"No actual rows found in plan: {json.dumps(d, indent=2)}"

    def test_join_analyze(self, conn):
        plan, raw = explain_analyze_query(
            conn,
            "SELECT t.name, o.amount FROM test t JOIN orders o ON t.id = o.test_id",
        )
        d = plan_to_dict(plan)
        operators = _collect_operators(d)
        assert any("JOIN" in op.upper() for op in operators)


# ── Serialization ────────────────────────────────────────────────────────


class TestPlanToDict:
    def test_basic_serialization(self):
        node = PlanNode(
            operator="SEQ_SCAN",
            table="test",
            estimated_rows=100,
            extra_info={"Type": "Sequential Scan"},
        )
        d = plan_to_dict(node)
        assert d["operator"] == "SEQ_SCAN"
        assert d["table"] == "test"
        assert d["estimated_rows"] == 100
        assert d["extra_info"]["Type"] == "Sequential Scan"
        assert "children" not in d  # no children = no key

    def test_nested_serialization(self):
        child = PlanNode(operator="SEQ_SCAN", table="orders", estimated_rows=50)
        parent = PlanNode(
            operator="HASH_JOIN",
            estimated_rows=200,
            extra_info={"Join Type": "INNER"},
            children=[child],
        )
        d = plan_to_dict(parent)
        assert len(d["children"]) == 1
        assert d["children"][0]["operator"] == "SEQ_SCAN"

    def test_json_serializable(self):
        node = PlanNode(
            operator="PROJECTION",
            actual_rows=10,
            actual_time_ms=1.234,
            extra_info={"Projections": ["id", "name"]},
            children=[PlanNode(operator="SEQ_SCAN", table="test")],
        )
        d = plan_to_dict(node)
        # Must be JSON serializable
        serialized = json.dumps(d)
        assert "PROJECTION" in serialized

    def test_enrich_plan_dict(self):
        node = PlanNode(
            operator="HASH_JOIN",
            actual_time_ms=5.0,
            children=[
                PlanNode(operator="SEQ_SCAN", actual_time_ms=3.0),
                PlanNode(operator="SEQ_SCAN", actual_time_ms=2.0),
            ],
        )
        d = enrich_plan_dict(plan_to_dict(node))
        assert d["_total_time_ms"] == 10.0
        assert d["time_percentage"] == 50.0
        assert d["children"][0]["time_percentage"] == 30.0
        assert d["children"][1]["time_percentage"] == 20.0


# ── JSON plan parsing ────────────────────────────────────────────────────


class TestParseJsonPlan:
    def test_parse_simple_json(self):
        raw = json.dumps([{
            "name": "SEQ_SCAN",
            "children": [],
            "extra_info": {
                "Table": "test",
                "Type": "Sequential Scan",
                "Estimated Cardinality": "100",
            },
        }])
        plan = _parse_json_plan(raw, is_analyze=False)
        assert plan.operator == "SEQ_SCAN"
        assert plan.table == "test"
        assert plan.estimated_rows == 100

    def test_parse_nested_json(self):
        raw = json.dumps([{
            "name": "HASH_JOIN",
            "children": [
                {
                    "name": "SEQ_SCAN",
                    "children": [],
                    "extra_info": {"Table": "test", "Estimated Cardinality": "10"},
                },
                {
                    "name": "SEQ_SCAN",
                    "children": [],
                    "extra_info": {"Table": "orders", "Estimated Cardinality": "20"},
                },
            ],
            "extra_info": {"Join Type": "INNER", "Estimated Cardinality": "15"},
        }])
        plan = _parse_json_plan(raw, is_analyze=False)
        assert plan.operator == "HASH_JOIN"
        assert len(plan.children) == 2
        assert plan.children[0].table == "test"
        assert plan.children[1].table == "orders"

    def test_parse_analyze_json(self):
        raw = json.dumps({
            "children": [{
                "operator_name": "EXPLAIN_ANALYZE",
                "operator_cardinality": 0,
                "operator_timing": 0.0001,
                "extra_info": {},
                "children": [{
                    "operator_name": "SEQ_SCAN",
                    "operator_cardinality": 50,
                    "operator_timing": 0.005,
                    "extra_info": {"Table": "test", "Estimated Cardinality": "100"},
                    "children": [],
                }],
            }],
        })
        plan = _parse_json_plan(raw, is_analyze=True)
        # Should skip EXPLAIN_ANALYZE wrapper
        assert plan.operator == "SEQ_SCAN"
        assert plan.table == "test"
        assert plan.actual_rows == 50
        assert plan.actual_time_ms == 5.0  # 0.005s = 5ms
        assert plan.estimated_rows == 100


# ── API endpoint tests ───────────────────────────────────────────────────


@pytest.fixture()
def api_project(tmp_path):
    """Create a minimal project for API testing."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "project.yml").write_text("name: test\ndatabase:\n  path: warehouse.duckdb\n")
    (proj / "transform").mkdir()
    # Create the warehouse with test data
    db_path = proj / "warehouse.duckdb"
    c = duckdb.connect(str(db_path))
    c.execute("CREATE SCHEMA IF NOT EXISTS landing")
    c.execute("CREATE TABLE landing.users(id INT, name VARCHAR)")
    c.execute("INSERT INTO landing.users VALUES (1, 'alice'), (2, 'bob')")
    c.close()
    return proj


@pytest.fixture()
def client(api_project):
    """Create a test HTTP client."""
    import havn.server.app as app_mod
    from havn.server.deps import reset_shared_conn

    app_mod.PROJECT_DIR = api_project
    app_mod.AUTH_ENABLED = False
    reset_shared_conn()

    from fastapi.testclient import TestClient

    with TestClient(app_mod.app) as c:
        yield c

    reset_shared_conn()


class TestExplainAPI:
    def test_explain_endpoint(self, client):
        resp = client.post(
            "/api/query/explain",
            json={"sql": "SELECT * FROM landing.users"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "plan" in data
        assert "raw" in data
        assert isinstance(data["plan"], dict)
        assert "operator" in data["plan"]
        assert isinstance(data["raw"], str)

    def test_explain_analyze_endpoint(self, client):
        resp = client.post(
            "/api/query/explain-analyze",
            json={"sql": "SELECT * FROM landing.users"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "plan" in data
        assert "raw" in data
        assert isinstance(data["plan"], dict)
        assert "operator" in data["plan"]

    def test_profile_endpoint_returns_structured(self, client):
        """Legacy /api/query/profile now returns structured plan too."""
        resp = client.post(
            "/api/query/profile",
            json={"sql": "SELECT * FROM landing.users"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["plan"], dict)
        assert "operator" in data["plan"]

    def test_explain_invalid_sql(self, client):
        resp = client.post(
            "/api/query/explain",
            json={"sql": "SELECTT * FROMM nowhere"},
        )
        assert resp.status_code == 400

    def test_explain_analyze_invalid_sql(self, client):
        resp = client.post(
            "/api/query/explain-analyze",
            json={"sql": "NOT VALID SQL"},
        )
        assert resp.status_code == 400


# ── Helpers ──────────────────────────────────────────────────────────────


def _collect_operators(d: dict) -> list[str]:
    """Recursively collect all operator names from a plan dict."""
    ops = [d.get("operator", "")]
    for child in d.get("children", []):
        ops.extend(_collect_operators(child))
    return ops


def _any_node(d: dict, predicate) -> bool:
    """Check if any node in the plan tree matches a predicate."""
    if predicate(d):
        return True
    for child in d.get("children", []):
        if _any_node(child, predicate):
            return True
    return False
