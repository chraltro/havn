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
# Cache functionality
# ---------------------------------------------------------------------------

def test_widget_query_with_cache(client):
    """Widget with cache_ttl > 0 should cache results."""
    resp = client.post("/api/dashboards", json={"name": "Cache Test"})
    dash_id = resp.json()["id"]

    resp = client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "chart",
        "title": "Cached Widget",
        "sql_query": "SELECT COUNT(*) AS cnt FROM landing.sales",
        "position": {"x": 1, "y": 1, "w": 6, "h": 4},
        "cache_ttl": 300,
    })
    wid = resp.json()["id"]

    # First query — populates cache
    resp = client.post(f"/api/dashboards/{dash_id}/widgets/{wid}/query", json={})
    assert resp.status_code == 200
    assert int(resp.json()["rows"][0][0]) == 100

    # Second query — should hit cache (same result)
    resp = client.post(f"/api/dashboards/{dash_id}/widgets/{wid}/query", json={})
    assert resp.status_code == 200
    assert int(resp.json()["rows"][0][0]) == 100


def test_clear_cache(client):
    """Clear cache endpoint should succeed and report cleared entries."""
    resp = client.post("/api/dashboards", json={"name": "Clear Cache Test"})
    dash_id = resp.json()["id"]

    resp = client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "chart",
        "title": "Cached",
        "sql_query": "SELECT 1",
        "position": {"x": 1, "y": 1, "w": 6, "h": 4},
        "cache_ttl": 300,
    })
    wid = resp.json()["id"]

    # Populate cache
    client.post(f"/api/dashboards/{dash_id}/widgets/{wid}/query", json={})

    # Clear cache
    resp = client.delete(f"/api/dashboards/{dash_id}/cache")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert "cleared" in resp.json()


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def test_list_templates_empty(client):
    """Templates endpoint returns empty list when none exist."""
    resp = client.get("/api/dashboards/templates")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Widget query edge cases
# ---------------------------------------------------------------------------

def test_query_widget_not_found(client):
    """Querying a non-existent widget returns 404."""
    resp = client.post("/api/dashboards", json={"name": "Widget 404"})
    dash_id = resp.json()["id"]

    resp = client.post(f"/api/dashboards/{dash_id}/widgets/nonexistent/query", json={})
    assert resp.status_code == 404


def test_update_widget_not_found(client):
    """Updating a non-existent widget returns 404."""
    resp = client.post("/api/dashboards", json={"name": "Widget Update 404"})
    dash_id = resp.json()["id"]

    resp = client.put(f"/api/dashboards/{dash_id}/widgets/nonexistent", json={
        "title": "Does not exist",
    })
    assert resp.status_code == 404


def test_delete_widget_not_found(client):
    """Deleting a non-existent widget returns 404."""
    resp = client.post("/api/dashboards", json={"name": "Widget Delete 404"})
    dash_id = resp.json()["id"]

    resp = client.delete(f"/api/dashboards/{dash_id}/widgets/nonexistent")
    assert resp.status_code == 404


def test_update_widget_no_fields(client):
    """Updating a widget with no fields returns 400."""
    resp = client.post("/api/dashboards", json={"name": "No Fields"})
    dash_id = resp.json()["id"]

    resp = client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "chart",
        "title": "Test",
        "position": {"x": 1, "y": 1, "w": 6, "h": 4},
    })
    wid = resp.json()["id"]

    resp = client.put(f"/api/dashboards/{dash_id}/widgets/{wid}", json={})
    assert resp.status_code == 400


def test_delete_dashboard_not_found(client):
    """Deleting a non-existent dashboard returns 404."""
    resp = client.delete("/api/dashboards/nonexistent")
    assert resp.status_code == 404


def test_clone_nonexistent_dashboard(client):
    """Cloning a non-existent dashboard returns 404."""
    resp = client.post("/api/dashboards/nonexistent/clone?name=Copy")
    assert resp.status_code == 404


def test_update_dashboard_filters_and_layout(client):
    """Updating dashboard filters, layout, and settings persists correctly."""
    resp = client.post("/api/dashboards", json={"name": "Filters Test"})
    dash_id = resp.json()["id"]

    filters = [{"id": "f1", "label": "Category", "type": "dropdown", "column": "category"}]
    layout = {"columns": 24, "rowHeight": 80, "gap": 16}
    settings = {"parameters": [{"name": "max_id", "default": 10}]}

    resp = client.put(f"/api/dashboards/{dash_id}", json={
        "filters": filters,
        "layout": layout,
        "settings": settings,
    })
    assert resp.status_code == 200

    # Verify persisted
    full = client.get(f"/api/dashboards/{dash_id}").json()
    assert full["filters"] == filters
    assert full["layout"] == layout
    assert full["settings"] == settings


def test_batch_query_with_filters(client):
    """Batch query passes filters to all widgets."""
    resp = client.post("/api/dashboards", json={"name": "Batch Filter"})
    dash_id = resp.json()["id"]

    client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "table",
        "title": "Filtered Sales",
        "sql_query": "SELECT id, revenue, category FROM landing.sales",
        "position": {"x": 1, "y": 1, "w": 12, "h": 6},
    })

    # Batch query with category filter
    resp = client.post(f"/api/dashboards/{dash_id}/query-batch", json={
        "filters": {"category": "A"},
    })
    assert resp.status_code == 200
    results = resp.json()["results"]
    # Should have exactly one result
    assert len(results) == 1
    # Category A = even IDs (2,4,...,100) = 50 rows
    for wid, result in results.items():
        assert result["row_count"] == 50


def test_widget_all_types(client):
    """Create widgets of all supported types."""
    resp = client.post("/api/dashboards", json={"name": "All Types"})
    dash_id = resp.json()["id"]

    types = ["chart", "kpi", "table", "text", "filter", "image"]
    for i, wtype in enumerate(types):
        resp = client.post(f"/api/dashboards/{dash_id}/widgets", json={
            "widget_type": wtype,
            "title": f"Widget {wtype}",
            "position": {"x": 1, "y": 1 + i * 4, "w": 6, "h": 4},
        })
        assert resp.status_code == 200, f"Failed to create widget type: {wtype}"
        assert resp.json()["widget_type"] == wtype

    # Verify all widgets created
    full = client.get(f"/api/dashboards/{dash_id}").json()
    assert len(full["widgets"]) == len(types)


def test_export_format(client):
    """Export contains expected structure and version field."""
    resp = client.post("/api/dashboards", json={"name": "Export Format"})
    dash_id = resp.json()["id"]

    client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "chart",
        "chart_type": "bar",
        "title": "Revenue",
        "sql_query": "SELECT category, SUM(revenue) FROM landing.sales GROUP BY 1",
        "position": {"x": 1, "y": 1, "w": 12, "h": 4},
        "config": {"xAxisLabel": "Category", "yAxisLabel": "Revenue"},
    })

    exported = client.get(f"/api/dashboards/{dash_id}/export").json()
    assert "dashboard" in exported
    assert "widgets" in exported
    assert "version" in exported
    assert exported["version"] == 1
    assert exported["dashboard"]["name"] == "Export Format"
    assert len(exported["widgets"]) == 1
    assert exported["widgets"][0]["chart_type"] == "bar"
    assert exported["widgets"][0]["config"]["xAxisLabel"] == "Category"


def test_dashboard_name_validation(client):
    """Dashboard name must be 1-200 chars."""
    # Empty name
    resp = client.post("/api/dashboards", json={"name": ""})
    assert resp.status_code == 422

    # Name too long
    resp = client.post("/api/dashboards", json={"name": "x" * 201})
    assert resp.status_code == 422

    # Valid boundary — 200 chars
    resp = client.post("/api/dashboards", json={"name": "x" * 200})
    assert resp.status_code == 200


def test_widget_invalid_type_rejected(client):
    """Widget with invalid type should be rejected by Pydantic validation."""
    resp = client.post("/api/dashboards", json={"name": "Bad Type"})
    dash_id = resp.json()["id"]

    resp = client.post(f"/api/dashboards/{dash_id}/widgets", json={
        "widget_type": "invalid_type",
        "title": "Bad",
        "position": {"x": 1, "y": 1, "w": 6, "h": 4},
    })
    assert resp.status_code == 422
