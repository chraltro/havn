"""Tests for the dashboard API endpoints."""

from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def project(tmp_path):
    """Create a minimal test project."""
    (tmp_path / "project.yml").write_text("""
name: test
database:
  path: warehouse.duckdb
""")
    (tmp_path / "transform" / "bronze").mkdir(parents=True)
    (tmp_path / "ingest").mkdir()
    (tmp_path / "export").mkdir()

    # Create warehouse with test data
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
    conn.execute("CREATE TABLE landing.sales AS SELECT i AS id, i * 10.0 AS revenue, CASE WHEN i % 2 = 0 THEN 'A' ELSE 'B' END AS category FROM range(1, 101) t(i)")
    conn.close()
    return tmp_path


@pytest.fixture
def client(project):
    import havn.server.app as server_app
    from havn.server.deps import reset_shared_conn

    reset_shared_conn()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = False
    return TestClient(server_app.app)


# ---------------------------------------------------------------------------
# Dashboard CRUD
# ---------------------------------------------------------------------------

def test_list_dashboards_empty(client):
    resp = client.get("/api/dashboards")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_dashboard(client):
    resp = client.post("/api/dashboards", json={"name": "Test Dashboard", "description": "My test"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Test Dashboard"
    assert data["description"] == "My test"
    assert data["id"]
    assert data["widget_count"] == 0


def test_get_dashboard(client):
    # Create
    resp = client.post("/api/dashboards", json={"name": "Fetch Test"})
    dash_id = resp.json()["id"]

    # Get
    resp = client.get(f"/api/dashboards/{dash_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Fetch Test"
    assert data["widgets"] == []
    assert "layout" in data
    assert "filters" in data
    assert "settings" in data


def test_update_dashboard(client):
    resp = client.post("/api/dashboards", json={"name": "Update Test"})
    dash_id = resp.json()["id"]

    resp = client.put(f"/api/dashboards/{dash_id}", json={"name": "Updated Name", "description": "New desc"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated Name"


def test_delete_dashboard(client):
    resp = client.post("/api/dashboards", json={"name": "Delete Me"})
    dash_id = resp.json()["id"]

    resp = client.delete(f"/api/dashboards/{dash_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"

    # Verify gone
    resp = client.get(f"/api/dashboards/{dash_id}")
    assert resp.status_code == 404


def test_clone_dashboard(client):
    # Create with a widget
    resp = client.post("/api/dashboards", json={"name": "Original"})
    dash_id = resp.json()["id"]

    client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "chart",
        "chart_type": "bar",
        "title": "Sales",
        "sql_query": "SELECT 1",
        "position": {"x": 1, "y": 1, "w": 6, "h": 4},
    })

    # Clone
    resp = client.post(f"/api/dashboards/{dash_id}/clone?name=Cloned")
    assert resp.status_code == 200
    clone = resp.json()
    assert clone["name"] == "Cloned"
    assert clone["widget_count"] == 1
    assert clone["id"] != dash_id


def test_dashboard_not_found(client):
    resp = client.get("/api/dashboards/nonexistent")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Widget CRUD
# ---------------------------------------------------------------------------

def test_add_widget(client):
    resp = client.post("/api/dashboards", json={"name": "Widget Test"})
    dash_id = resp.json()["id"]

    resp = client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "chart",
        "chart_type": "bar",
        "title": "Revenue Chart",
        "sql_query": "SELECT category, SUM(revenue) FROM landing.sales GROUP BY category",
        "position": {"x": 1, "y": 1, "w": 12, "h": 4},
        "cache_ttl": 60,
    })
    assert resp.status_code == 200
    w = resp.json()
    assert w["title"] == "Revenue Chart"
    assert w["widget_type"] == "chart"
    assert w["chart_type"] == "bar"
    assert w["cache_ttl"] == 60


def test_update_widget(client):
    resp = client.post("/api/dashboards", json={"name": "UW Test"})
    dash_id = resp.json()["id"]

    resp = client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "chart",
        "title": "Old",
        "position": {"x": 1, "y": 1, "w": 6, "h": 4},
    })
    wid = resp.json()["id"]

    resp = client.put(f"/api/dashboards/{dash_id}/widgets/{wid}", json={
        "title": "New Title",
        "chart_type": "line",
    })
    assert resp.status_code == 200
    assert resp.json()["title"] == "New Title"
    assert resp.json()["chart_type"] == "line"


def test_delete_widget(client):
    resp = client.post("/api/dashboards", json={"name": "DW Test"})
    dash_id = resp.json()["id"]

    resp = client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "text",
        "title": "Note",
        "position": {"x": 1, "y": 1, "w": 6, "h": 2},
    })
    wid = resp.json()["id"]

    resp = client.delete(f"/api/dashboards/{dash_id}/widgets/{wid}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"

    # Verify widget gone from dashboard
    resp = client.get(f"/api/dashboards/{dash_id}")
    assert len(resp.json()["widgets"]) == 0


def test_batch_position_update(client):
    resp = client.post("/api/dashboards", json={"name": "Pos Test"})
    dash_id = resp.json()["id"]

    # Add two widgets
    w1 = client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "chart", "title": "W1", "position": {"x": 1, "y": 1, "w": 6, "h": 4},
    }).json()["id"]
    w2 = client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "chart", "title": "W2", "position": {"x": 7, "y": 1, "w": 6, "h": 4},
    }).json()["id"]

    # Update positions
    resp = client.patch(f"/api/dashboards/{dash_id}/widgets/positions", json={
        "positions": [
            {"id": w1, "position": {"x": 1, "y": 5, "w": 12, "h": 6}},
            {"id": w2, "position": {"x": 13, "y": 5, "w": 12, "h": 6}},
        ]
    })
    assert resp.status_code == 200
    assert resp.json()["updated"] == 2


# ---------------------------------------------------------------------------
# Widget Query Execution
# ---------------------------------------------------------------------------

def test_widget_query(client):
    resp = client.post("/api/dashboards", json={"name": "Query Test"})
    dash_id = resp.json()["id"]

    resp = client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "chart",
        "title": "Sales Total",
        "sql_query": "SELECT SUM(revenue) AS total_revenue FROM landing.sales",
        "position": {"x": 1, "y": 1, "w": 6, "h": 4},
    })
    wid = resp.json()["id"]

    resp = client.post(f"/api/dashboards/{dash_id}/widgets/{wid}/query", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert "columns" in data
    assert "rows" in data
    assert data["row_count"] == 1
    assert data["columns"] == ["total_revenue"]
    # SUM of i*10 for i=1..100 = 10 * 5050 = 50500
    # _serialize converts numbers to their native types; floats may come as strings
    assert float(data["rows"][0][0]) == 50500.0


def test_widget_query_with_filters(client):
    resp = client.post("/api/dashboards", json={"name": "Filter Query Test"})
    dash_id = resp.json()["id"]

    resp = client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "table",
        "title": "Filtered Sales",
        "sql_query": "SELECT id, revenue, category FROM landing.sales",
        "position": {"x": 1, "y": 1, "w": 12, "h": 6},
    })
    wid = resp.json()["id"]

    # Query with category filter
    resp = client.post(f"/api/dashboards/{dash_id}/widgets/{wid}/query", json={
        "filters": {"category": "A"},
    })
    assert resp.status_code == 200
    data = resp.json()
    # Category A = even IDs (2,4,6,...,100) = 50 rows
    assert data["row_count"] == 50
    # All rows should have category A
    cat_idx = data["columns"].index("category")
    assert all(row[cat_idx] == "A" for row in data["rows"])


def test_batch_query(client):
    resp = client.post("/api/dashboards", json={"name": "Batch Test"})
    dash_id = resp.json()["id"]

    w1 = client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "chart",
        "title": "W1",
        "sql_query": "SELECT COUNT(*) AS cnt FROM landing.sales",
        "position": {"x": 1, "y": 1, "w": 6, "h": 4},
    }).json()["id"]

    w2 = client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "kpi",
        "title": "W2",
        "sql_query": "SELECT SUM(revenue) AS total FROM landing.sales",
        "position": {"x": 7, "y": 1, "w": 6, "h": 4},
    }).json()["id"]

    resp = client.post(f"/api/dashboards/{dash_id}/query-batch", json={})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert w1 in results
    assert w2 in results
    assert int(results[w1]["rows"][0][0]) == 100
    assert float(results[w2]["rows"][0][0]) == 50500.0


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------

def test_export_import(client):
    # Create dashboard with widget
    resp = client.post("/api/dashboards", json={"name": "Export Test"})
    dash_id = resp.json()["id"]

    client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "chart",
        "chart_type": "line",
        "title": "Trend",
        "sql_query": "SELECT id, revenue FROM landing.sales ORDER BY id",
        "position": {"x": 1, "y": 1, "w": 24, "h": 6},
    })

    # Export
    resp = client.get(f"/api/dashboards/{dash_id}/export")
    assert resp.status_code == 200
    exported = resp.json()
    assert "dashboard" in exported
    assert "widgets" in exported
    assert len(exported["widgets"]) == 1

    # Import
    resp = client.post("/api/dashboards/import", json=exported)
    assert resp.status_code == 200
    imported = resp.json()
    assert imported["name"] == "Export Test"
    assert imported["widget_count"] == 1
    assert imported["id"] != dash_id  # New ID


# ---------------------------------------------------------------------------
# Cascade delete
# ---------------------------------------------------------------------------

def test_cascade_delete(client):
    resp = client.post("/api/dashboards", json={"name": "Cascade Test"})
    dash_id = resp.json()["id"]

    # Add 3 widgets
    for i in range(3):
        client.post(f"/api/dashboards/{dash_id}/widgets", json={
            "widget_type": "chart",
            "title": f"Widget {i}",
            "sql_query": f"SELECT {i}",
            "position": {"x": 1 + i * 8, "y": 1, "w": 8, "h": 4},
        })

    # Verify 3 widgets
    resp = client.get(f"/api/dashboards/{dash_id}")
    assert len(resp.json()["widgets"]) == 3

    # Delete dashboard — should cascade
    resp = client.delete(f"/api/dashboards/{dash_id}")
    assert resp.status_code == 200

    # Dashboard gone
    resp = client.get(f"/api/dashboards/{dash_id}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_widget_query_empty_sql(client):
    resp = client.post("/api/dashboards", json={"name": "Empty SQL"})
    dash_id = resp.json()["id"]

    resp = client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "text",
        "title": "Note",
        "position": {"x": 1, "y": 1, "w": 6, "h": 2},
    })
    wid = resp.json()["id"]

    # Query a widget with no SQL
    resp = client.post(f"/api/dashboards/{dash_id}/widgets/{wid}/query", json={})
    assert resp.status_code == 200
    assert resp.json()["row_count"] == 0


def test_widget_query_invalid_sql(client):
    resp = client.post("/api/dashboards", json={"name": "Bad SQL"})
    dash_id = resp.json()["id"]

    resp = client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "chart",
        "title": "Bad",
        "sql_query": "SELECT FROM WHERE INVALID",
        "position": {"x": 1, "y": 1, "w": 6, "h": 4},
    })
    wid = resp.json()["id"]

    resp = client.post(f"/api/dashboards/{dash_id}/widgets/{wid}/query", json={})
    assert resp.status_code == 400


def test_widget_with_parameters(client):
    """Widget query with parameter substitution."""
    resp = client.post("/api/dashboards", json={"name": "Param Test"})
    dash_id = resp.json()["id"]

    resp = client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "chart",
        "title": "Parameterized",
        "sql_query": "SELECT * FROM landing.sales WHERE id <= ${max_id}",
        "position": {"x": 1, "y": 1, "w": 8, "h": 4},
    })
    wid = resp.json()["id"]

    resp = client.post(f"/api/dashboards/{dash_id}/widgets/{wid}/query", json={
        "parameters": {"max_id": 5},
    })
    assert resp.status_code == 200
    assert resp.json()["row_count"] == 5


def test_batch_query_empty_dashboard(client):
    """Batch query on dashboard with no widgets."""
    resp = client.post("/api/dashboards", json={"name": "Empty Batch"})
    dash_id = resp.json()["id"]

    resp = client.post(f"/api/dashboards/{dash_id}/query-batch", json={})
    assert resp.status_code == 200
    assert resp.json()["results"] == {}


def test_invalid_position_rejected(client):
    """Widget with invalid position should be rejected."""
    resp = client.post("/api/dashboards", json={"name": "Bad Pos"})
    dash_id = resp.json()["id"]

    resp = client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "chart",
        "title": "Bad",
        "position": {"x": 0, "y": -1},  # Missing w, h; negative values
    })
    assert resp.status_code == 422


def test_export_delete_import_roundtrip(client):
    """Full cycle: create → export → delete → import → verify."""
    resp = client.post("/api/dashboards", json={"name": "Roundtrip"})
    dash_id = resp.json()["id"]
    client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "kpi", "title": "Metric",
        "sql_query": "SELECT 42", "position": {"x": 1, "y": 1, "w": 6, "h": 3},
    })

    # Export
    exported = client.get(f"/api/dashboards/{dash_id}/export").json()

    # Delete original
    client.delete(f"/api/dashboards/{dash_id}")
    assert client.get(f"/api/dashboards/{dash_id}").status_code == 404

    # Import
    imported = client.post("/api/dashboards/import", json=exported).json()
    assert imported["widget_count"] == 1

    # Verify imported dashboard works
    full = client.get(f"/api/dashboards/{imported['id']}").json()
    assert full["name"] == "Roundtrip"
    assert len(full["widgets"]) == 1
    assert full["widgets"][0]["title"] == "Metric"


# ---------------------------------------------------------------------------
# Widget query cache
# ---------------------------------------------------------------------------

def _make_cached_widget(client, sql="SELECT COUNT(*) AS n FROM landing.sales", ttl=300):
    dash_id = client.post("/api/dashboards", json={"name": "Cache Test"}).json()["id"]
    wid = client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "chart",
        "title": "Cached",
        "sql_query": sql,
        "position": {"x": 1, "y": 1, "w": 6, "h": 4},
        "cache_ttl": ttl,
    }).json()["id"]
    return dash_id, wid


def test_widget_query_cache_actually_caches(client):
    """Regression: cache writes ran on a read-only cursor and failed silently,
    so the cache never populated. A second query must be served from cache."""
    dash_id, wid = _make_cached_widget(client)

    r1 = client.post(f"/api/dashboards/{dash_id}/widgets/{wid}/query", json={})
    assert r1.status_code == 200
    assert r1.json()["rows"] == [[100]]

    # Change the underlying data; a cache hit still returns the old count.
    from havn.server.deps import _get_shared_conn
    cur = _get_shared_conn().cursor()
    try:
        cur.execute("DELETE FROM landing.sales WHERE id > 50")
    finally:
        cur.close()

    r2 = client.post(f"/api/dashboards/{dash_id}/widgets/{wid}/query", json={})
    assert r2.status_code == 200
    assert r2.json()["rows"] == [[100]], "expected cached result, cache was not populated"


def test_widget_query_cache_invalidated_by_sql_edit(client):
    """Regression: the cache key ignored the SQL, so editing a widget's query
    served stale results from the old query until the TTL expired."""
    dash_id, wid = _make_cached_widget(client)

    r1 = client.post(f"/api/dashboards/{dash_id}/widgets/{wid}/query", json={})
    assert r1.json()["rows"] == [[100]]

    resp = client.put(f"/api/dashboards/{dash_id}/widgets/{wid}", json={
        "sql_query": "SELECT COUNT(*) AS n FROM landing.sales WHERE category = 'A'",
    })
    assert resp.status_code == 200

    r2 = client.post(f"/api/dashboards/{dash_id}/widgets/{wid}/query", json={})
    assert r2.json()["rows"] == [[50]], "stale cache served after SQL edit"


def test_batch_query_uses_cache(client):
    dash_id, wid = _make_cached_widget(client)
    r1 = client.post(f"/api/dashboards/{dash_id}/query-batch", json={})
    assert r1.status_code == 200
    assert r1.json()["results"][wid]["rows"] == [[100]]

    from havn.server.deps import _get_shared_conn
    cur = _get_shared_conn().cursor()
    try:
        cur.execute("DELETE FROM landing.sales")
    finally:
        cur.close()

    r2 = client.post(f"/api/dashboards/{dash_id}/query-batch", json={})
    assert r2.json()["results"][wid]["rows"] == [[100]], "batch did not hit cache"
