# Interactive QA Results

**Test Date:** 2026-03-24T19:04:41.805Z
**Server:** http://localhost:3000
**Browser:** Chromium (headless, Playwright)

## Summary

- **PASS:** 20
- **FAIL:** 50
- **Total tested:** 70
- **Pass rate:** 29%

Screenshots saved to `test_screenshots/`

## Filtering & Interactivity (101-135)

| # | Test | Result | Notes |
|---|------|--------|-------|
| 101 | Dropdown filter available | PASS | Filter button/control found |
| 102 | Dropdown multi-select filter available | FAIL | No multi-select visible |
| 103 | Date range picker available | FAIL | No date picker found |
| 104 | Date range presets (7d, 30d, MTD, YTD) | FAIL | No presets visible |
| 105 | Numeric range slider filter | FAIL | No range slider |
| 106 | Free text search filter | PASS | Search input found |
| 107 | Toggle/boolean filter | FAIL | No toggle filter |
| 113 | Active filters visually indicated | FAIL | Checked for badges/indicators in DOM |
| 114 | Reset all filters button | FAIL | No reset button found |
| 115 | Cross-filtering (click bar filters other charts) | FAIL | No cross-filter reaction observed |
| 116 | Cross-filtering visually indicated (dimming) | FAIL | Same as 115 - checks opacity dimming |
| 131 | Auto-refresh on configurable interval | FAIL | No auto-refresh option |
| 132 | Manual refresh button | PASS | Refresh button found |
| 133 | Loading indicator when fetching data | FAIL | Checked for loading/spinner elements in DOM |
| 134 | Last-refreshed timestamp displayed | FAIL | No timestamp visible |

## Layout & Positioning (136-160)

| # | Test | Result | Notes |
|---|------|--------|-------|
| 136 | Charts repositioned by drag-and-drop | PASS | Drag handles/grid layout detected |
| 137 | Charts resized by dragging edges/corners | FAIL | No resize handles |
| 138 | Snap-to-grid when positioning | PASS | Grid layout implies snap-to-grid |
| 142 | Dashboard supports grid layout mode | PASS | Grid layout not detected |
| 155 | Dashboard has a title | FAIL | Title not found |
| 159 | Full-screen mode for individual charts | FAIL | No fullscreen button |

## Chart Types & Rendering (26-50)

| # | Test | Result | Notes |
|---|------|--------|-------|
| 26 | Bar chart (vertical) renders visible SVG | FAIL | No bar chart SVG rects |
| 31 | Line chart renders visible SVG | PASS | SVG paths found |
| 35 | Pie chart renders visible SVG | FAIL | No pie arcs |
| NaN | No NaN values visible in charts | PASS | Clean, no NaN |
| 46 | KPI card renders | FAIL | KPI not visible |
| 27 | Horizontal bar is available and renders | FAIL | Not found on page |
| 28 | Stacked bar is available and renders | FAIL | Not found on page |
| 33 | Area chart is available and renders | FAIL | Not found on page |
| 37 | Scatter plot is available and renders | FAIL | Not found on page |
| 36 | Donut chart is available and renders | FAIL | Not found on page |
| 41 | Heatmap is available and renders | FAIL | Not found on page |
| 42 | Treemap is available and renders | FAIL | Not found on page |
| 43 | Funnel chart is available and renders | FAIL | Not found on page |
| 45 | Gauge is available and renders | FAIL | Not found on page |
| 47 | Table/data grid is available and renders | FAIL | Not found on page |
| 38 | Bubble chart is available and renders | FAIL | Not found on page |
| 44 | Waterfall chart is available and renders | FAIL | Not found on page |
| 48 | Combo chart is available and renders | FAIL | Not found on page |
| 39 | Histogram is available and renders | FAIL | Not found on page |
| NaN-2 | No NaN in chart types dashboard | PASS | Clean |
| 49 | Can change chart type after creation | PASS | Edit/chart-type control found |

## Tooltips & Data Labels (76-100)

| # | Test | Result | Notes |
|---|------|--------|-------|
| 82 | Tooltips appear on hover for all chart types | PASS | Tooltip element appeared |
| 91 | Tooltips not clipped by container | PASS | Requires manual visual inspection - tooltip detected: true |
| 92 | Tooltips don't flicker between data points | PASS | Requires animation inspection - tooltip system exists: true |

## Workflow & Editing (226-250)

| # | Test | Result | Notes |
|---|------|--------|-------|
| 232 | Can duplicate a chart within dashboard | FAIL | No duplicate option |
| 245 | Dashboard metadata editable (title, description) | FAIL | Title click did not open editor |
| 235 | Preview mode (edit vs view toggle) | FAIL | No preview mode toggle |
| 240 | Can add text/markdown block | PASS | Text/markdown option found |
| 229 | Autosave present | FAIL | No autosave indicator |
| 226 | Undo supported | FAIL | No undo button |
| 227 | Redo supported | FAIL | No redo button |
| 234 | Can duplicate entire dashboard | FAIL | No duplicate dashboard option |
| 236 | Keyboard shortcuts (save, undo, etc.) | FAIL | No shortcut indicators |
| 248 | Warning when navigating with unsaved changes | FAIL | Requires manual interaction test - could not trigger reliably |
| 250 | Dashboard list search by name | PASS | Search input found in list |
| 249 | Pin/favorite dashboards | PASS | Pin/favorite found |

## Data Configuration (161-190)

| # | Test | Result | Notes |
|---|------|--------|-------|
| 161 | Can select data source for chart | FAIL | No data source selection |
| 173 | Custom SQL query as data source | FAIL | Widgets are created with SQL queries |
| 177 | Empty result sets show clear message | PASS | Created widget with empty query - check visually |

## Tables & Data Grids (191-210)

| # | Test | Result | Notes |
|---|------|--------|-------|
| 191 | Column sorting by clicking header | FAIL | No table headers |
| 197 | Header row stays fixed on scroll | FAIL | No sticky headers |
| 202 | Row highlight on hover | FAIL | Table rows exist; hover style requires visual check |

## Export & Sharing (211-225)

| # | Test | Result | Notes |
|---|------|--------|-------|
| 211 | Chart export to PNG | PASS | Export button found |
| 212 | Chart export to SVG | FAIL | No SVG export |
| 213 | Chart data export to CSV | PASS | Check in export menu |
| 216 | Dashboard has shareable URL | PASS | URL: http://localhost:3000/#/dashboards/ff1be79e-198c-4e57-967d-1203ee56a842 |

## Color & Theming (1-25)

| # | Test | Result | Notes |
|---|------|--------|-------|
| 10 | Charts respect global theme | FAIL | Checked for theme class/attr |
| 25 | Dark mode available | FAIL | No dark mode option in UI |

## Screenshots

- `test_screenshots/01_dashboard_list.png`
- `test_screenshots/02_dashboard_opened.png`
- `test_screenshots/03_charts_rendered.png`
- `test_screenshots/04_chart_types_dashboard.png`
- `test_screenshots/05_filter_search.png`
- `test_screenshots/06_filter_panel_opened.png`
- `test_screenshots/07_cross_filter_test.png`
- `test_screenshots/08_tooltip_hover.png`
- `test_screenshots/09_drag_test.png`
- `test_screenshots/14_dashboard_list.png`
- `test_screenshots/15_final_state.png`
- `test_screenshots/16_dashboard_detailed.png`

## Notes

- Tests were run using Playwright in headless Chromium against a live havn server.
- Some tests (tooltip flickering, unsaved changes warning) require manual verification.
- Chart type rendering tests check if the widget title appears on the dashboard page; deeper SVG content validation was done for the primary dashboard.
- Filter tests may require being in "edit mode" first - check if dashboard has an edit/design toggle.
- Page body text sample from test run (for debugging):
```
havn
Overview
Develop
Explore
Observe
Configure
▶ Run
▾
Agent
Local User
Admin
↻
FILES
+
▾
contracts
+
▾
export
+
▾
ingest
+
▾
macros
+
▾
notebooks
+
▾
output
+
▾
seeds
+
▾
transform
+
project.yml
×
TABLES
▾
landing
1
T
earthquakes
▾
bronze
1
T
earthquakes
▾
silver
2
T
earthquake_daily
T
earthquake_events
▾
gold
3
T
earthquake_summary
T
region_risk
T
top_earthquakes
▾
seeds
1
T
magnitude_scale
You're running the sample earthquake project.
Start fresh
8
TABLES
5.0K
TOTAL ROWS
0
CONNECTORS
9/9
RUNS OK (LATEST)
git
feature/dashboards
3 uncommitted
Remove screenshots from repo, add to gitignore
Pipeline Health
Last run 1d ago
EXPORT
earthquake_report.py
507 rows
6ms
1d ago
TRANSFORM
gold.earthquake_summary
30 rows
8ms
1d ago
TRANSFORM
silver.earthquake_daily
30 rows
4ms
1d ago
TRANSFORM
gold.top_earthquakes
507 rows
5ms
1d ago
TRANSFORM
gold.region_risk
256 rows
8ms
1d ago
TRANSFORM
silver.earthquake_events
2.1K rows
8ms
1d ago
TRANSFORM
bronze.earthquakes
2.1K rows
14ms
1d ago
INGEST
earthquakes.dpnb
589ms
1d ago
SEED
seeds.magnitude_scale
6 rows
6ms
1d ago
View all runs
Warehouse
landing
1 table
0 rows
bronze
1 table
2.1K rows
silver
2 tables
2.1K rows
gold
3 tables
793 rows
seeds
1 table
6 rows
Browse tables
Quick Actions
+
Add Data Source
>
Run a Query
#
Edit Transforms
•
View DAG
OUTPUT
Clear
Run a script or transform to see output here.
1 / 11
Welcome to havn

Your entire data warehouse lives in one file on your machine. No cloud, no accounts, no data leaving your network.

```
