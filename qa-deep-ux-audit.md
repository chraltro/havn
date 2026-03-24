# Deep UX Audit: havn Dashboard Feature

**Auditor Role:** Senior data analyst with extensive Databricks, Power BI, and Tableau experience
**Date:** 2026-03-24
**Evaluation Scope:** Dashboard creation, widget builder, editing, and interactive features

---

## Executive Summary

havn's dashboard feature is **functionally complete** for basic use cases but has **significant UX gaps** compared to professional BI tools. The interface is intuitive for simple charts but lacks refinement in visualization rendering, interaction patterns, and advanced capabilities that power users expect. Most issues are **polish and perception problems** rather than blockers, but together they create friction that would frustrate analysts from Databricks/Power BI backgrounds.

**Overall Assessment:** Beta-ready, not production-ready for competitive positioning against Power BI/Databricks dashboards.

---

## 1. First-Time Experience

### Strengths
- **Clear empty state** with helpful guidance ("Click + Add Widget to create your first visualization")
- **Fast to first chart** (3 clicks: New Dashboard → Add Widget → Select Preset)
- **Widget preset options** reduce configuration friction

### Issues (P1)

#### Confusing Widget Type Menu Layout
**Problem:** The widget type selection screen shows presets (Custom, Bar Chart, Time Series, etc.) as equally-weighted options, but "Custom" is actually the advanced builder while others are quick presets. This creates cognitive overhead.
- User expectation: "Which one should I pick for my use case?"
- Current: No differentiation in visual hierarchy

**Fix:**
- Group presets under "Quick Start" heading with visual emphasis
- Or: Show Custom separately under "Advanced"
- Or: Auto-detect recommended chart type from data and highlight it

#### No Guidance on Data Readiness
**Problem:** When creating a new dashboard, the UX doesn't confirm which tables have "dashboard-ready" data. If a user picks a table with 100+ columns and tries a preset, they get unfiltered raw data.
- Example: `silver.earthquake_events` has 15 columns; "Sum by category" without guidance groups by `event_id` (wrong choice)
- User feels: "Why is this grouping by event ID?"

**Fix:** Add inline help: "This preset groups by the first text column. Change it if needed."

### Issues (P2)

#### Dashboard Creation Dialog Could Show Table Preview
**Problem:** You must create the dashboard blind—no table preview in the creation step. Compare to Power BI, which shows you available tables/schemas during dashboard setup.

---

## 2. Visual Builder UX

### Strengths
- **20+ chart types** organized by category (Basic, Advanced, Stats, Cards)
- **Live preview** updates instantly on chart type/column changes
- **Quick presets** work well for common patterns
- **Column type indicators** (# for numeric, Aa for string, ⏱ for time) aid scanning

### Critical Issues (P0)

#### X-Axis Label Overlap on Bar Charts
**Problem:** Bar charts with category data (e.g., region names, event IDs) render with **completely overlapped and unreadable X-axis labels**. This is the #1 visual bug in dashboards.

**Evidence:**
- Screenshot "07-existing-dashboard.png" shows "Events by Region" bar chart where X-axis labels are diagonal, overlapped, and rotated 45°+
- Same on all bar/H-bar charts with 10+ categories

**Impact:** Users cannot read which bar represents which category. Makes dashboards useless for analysis.

**Fix Priority:** P0—this breaks fundamental dashboard functionality
- Add automatic label rotation (45°, 90°) based on label length
- Implement truncation + hover tooltip for long names
- Or: Recommend H-Bar chart automatically when labels exceed N chars
- Test with >20 categories

#### Pie Chart Dominance Problem
**Problem:** Pie charts with unbalanced data (e.g., 86% one slice, 14% rest) are nearly unreadable. The single large slice dominates; small slices are invisible.

**Evidence:**
- Screenshot "06-pie-chart.png": Pie chart shows one dominant magenta slice (86.2%) with tiny colored slivers for other values
- This is valid data representation, but pie charts fail here—user should be warned

**Fix:**
- Add warning when top slice >70% of total: "Pie chart may not be readable. Consider: Bar chart, Treemap, or Data Table"
- Auto-recommend better chart types
- Or: Offer interactive drill-down/zoom for small slices

#### Misleading "Recommended" Chart Types
**Problem:** After selecting "Sum by category" preset, the UI shows "Recommended: bar, hbar, stacked, treemap" but doesn't explain *why* or offer guidance on when to pick each.

**Issue:** User picks "bar" because it's first, but the chart fails (unreadable labels). User blames themselves, not the tool.

**Fix:** Add descriptions on hover:
- "Bar: Good for <10 categories. Labels may overlap with more."
- "Treemap: Better for many categories with wide value ranges"
- "H-Bar: Readable labels even with 20+ categories"

### Major Issues (P1)

#### No Conditional Formatting / Color Rules
**Problem:** Data analysts from Power BI/Databricks expect to:
- Color cells red if negative, green if positive
- Add a "traffic light" scale (red/yellow/green by value range)
- Highlight top/bottom N values

**Missing:** No conditional formatting in table widgets. All cells are uniformly colored.

**Comparison:**
- **Power BI:** Native conditional formatting for tables + heatmap coloring
- **Databricks:** Native value-based color scales
- **havn:** Not available

**Impact:** Medium—nice-to-have, not blocking, but noticeable gap for power users.

#### No Reference Lines / Target Lines
**Problem:** Cannot add a horizontal or vertical reference line to show:
- Revenue target (Y-axis line at $1M)
- Industry benchmark
- Previous period average

**Missing in:** All chart types

**Comparison:**
- **Power BI:** Native "Add constant line" feature
- **Tableau:** Native "Reference line" with calculation
- **havn:** Not available

**Impact:** Medium-high for KPI-focused dashboards.

#### No Calculated Columns / Fields
**Problem:** To show "Revenue per Unit," you must:
1. Create a SQL transform in `transform/silver/`
2. Add it as a new column
3. Return to dashboard

Cannot create an ad-hoc calculated field in the dashboard UI (unlike Power BI's "New Measure" or Tableau's "Create Calculated Field").

**Impact:** High friction for exploratory analysis. Users expect to iterate within the dashboard, not bounce back to SQL files.

**Why it matters:** This is the gap between a BI tool and a SQL interface. Data analysts want to ask "what if?" questions without writing SQL.

#### No Multi-Select Aggregation
**Problem:** If you want to show SUM + AVERAGE for the same column, you must:
1. Add the column twice
2. Change aggregation on one

There's no "add another aggregation" button. This is a clunky flow.

**Comparison:** Power BI allows multiple aggregations per field in one click.

### Moderate Issues (P2)

#### No Chart Title Customization
**Problem:** Widget titles are separate from chart titles. You can set a title, but no subtitle, no chart-level title formatting.

**Impact:** Low—mostly cosmetic, but professional dashboards have richer labeling.

#### Axis Label Fields Are Text-Only
**Problem:** The "X-axis" and "Y-axis" label fields accept freeform text. But they auto-populate with "Auto (column name)."
- If user types something custom, they lose the ability to reference the actual column name
- No syntax highlighting or help text

**Impact:** Low—users figure it out, but unintuitive.

#### Time Series Presets Don't Handle Timezones
**Problem:** "Trend over time" preset groups by date column, but no timezone handling visible. If data spans multiple zones or daylight saving, results may be ambiguous.

**Impact:** Low for earthquake data (UTC), but high for business data with local timestamps.

---

## 3. Missing Competitive Features

### High Priority (P1 - Deal Breakers)

#### No Dashboard Parameters / Filters
**Problem:** Can create dashboard-level filters (like "Event Date" in QA Interactive Test), but cannot:
- Create dropdown filters that users can interact with at view time
- Create parameters that propagate to multiple widgets
- Expose filter controls to viewers (non-editors)

**What havn HAS:** Date range filter at top of dashboard (visible in "QA Interactive Test")
**What havn DOESN'T HAVE:**
- Multi-select filters (pick 3+ regions)
- Free-text search filters
- Filter widgets that affect multiple charts (cross-filtering)
- Ability for viewers to change filters

**Comparison:**
- **Power BI:** Native filter pane, slicers, parameters
- **Tableau:** Native filter shelf, parameters
- **Databricks SQL:** Native table-level filters

**Impact:** HIGH. This is how users explore data. Without it, dashboards are static reports, not interactive tools.

**Evidence:** "QA Interactive Test" dashboard has 6 widgets, but only one filter (Event Date), and several widgets show "No query configured"—suggesting the filter infrastructure is incomplete.

#### No Cross-Filtering (Click to Filter)
**Problem:** Cannot click on a bar chart element to filter other charts on the dashboard.
- Example: Click "Region: California" on a map → all other charts filter to CA

**Comparison:**
- **Power BI:** Native cross-filtering (Visual A → Filters Visual B)
- **Tableau:** Native interactions (click → filter)
- **Databricks:** Limited, requires SQL setup

**Impact:** HIGH. This is core to interactive dashboards. Its absence makes dashboards feel static.

#### No Drill-Down / Hierarchies
**Problem:** Cannot set up dimension hierarchies (Region → Country → City) and drill down from dashboards.

**Impact:** MEDIUM-HIGH. Expected in Power BI/Tableau. Limits exploratory analysis.

#### No Scheduled Email Delivery
**Problem:** Cannot schedule a dashboard to email to stakeholders daily/weekly.

**Comparison:**
- **Power BI:** Native "Subscribe" and scheduled reports
- **Tableau:** Native "Subscriptions"
- **Databricks:** Via external integrations

**Impact:** MEDIUM. Professional teams expect this. Not a blocker for basic dashboards.

#### No Template / Duplicate from Template
**Problem:** Can duplicate a dashboard, but no "save as template" or template gallery.
- If you build a "Regional Sales Dashboard," you can't reuse it as a template for other regions

**Impact:** MEDIUM. Important for scaled operations.

#### No Multi-Page / Tabbed Dashboards
**Problem:** Each dashboard is single-page. To organize related metrics, you create separate dashboards (see "Test Charts," "QA Interactive Test," etc.).

**Comparison:**
- **Power BI:** Native "Report Pages" (30+ pages in one report)
- **Tableau:** Native "Sheets" (multiple tabs)
- **Databricks:** Multi-worksheet support

**Impact:** MEDIUM-HIGH for complex domains (e.g., a Finance dashboard needs Profit & Loss, Cash Flow, Balance Sheet as separate "pages").

### Medium Priority (P2 - Feature Gaps)

#### No Dashboard Sharing / Embedding
**Problem:** No public link or embedding options visible (may exist but not discoverable).

**Impact:** MEDIUM. Important for sharing with non-users or embedding in reports.

#### No Responsive Design / Mobile Layout
**Problem:** Dashboards are designed for desktop. On mobile:
- Widgets stack vertically
- Charts are tiny
- Filter controls are hard to interact with

**Evidence:** Widgets show "Drag to resize" handles; unclear how this works on touch devices.

**Impact:** MEDIUM-HIGH for modern analytics (many users access dashboards on tablets).

#### No Dark Mode Awareness in Charts
**Problem:** Charts use a dark background (good for dark theme), but some colors may have low contrast.

**Evidence:** Chart legend text in "Earthquake Trend by Month" is small and hard to read.

#### No Chart Interactions (Zoom, Scroll, Pan)
**Problem:** Charts are static. Cannot:
- Zoom into a time series
- Pan a large scatter plot
- Hover for details (no tooltips visible)

**Impact:** MEDIUM. Nice-to-have, but reduces exploratory capability.

---

## 4. Table Widget Quality

### Strengths
- Clean, readable layout
- Row count displayed
- Data types shown (when editing)

### Issues (P1)

#### No Visible Sorting UI
**Problem:** Cannot click column headers to sort. Must use the "Sort" section in the widget editor.
- User expectation: Click column header → sort ascending/descending
- Current: No affordance visible; must re-edit widget

**Impact:** MEDIUM-HIGH. Sorting is essential for analysis; requiring edit mode is friction.

#### No Column Resizing
**Problem:** Column widths are fixed. Cannot drag to resize.
- If a column has long text, it truncates with no overflow indication

**Impact:** MEDIUM. Modern data tables support resizing.

#### No Search/Filter Within Table
**Problem:** Cannot filter rows within the table (e.g., search for a specific region).
- Must use dashboard-level filter or edit the query
- No Ctrl+F style search

**Impact:** MEDIUM-HIGH. Essential for large tables (1000+ rows).

#### No Column Pinning / Reordering
**Problem:** Cannot freeze the first column (ID, date) or reorder columns.
- Scrolling horizontally loses the row identifier

**Impact:** MEDIUM. Important for wide tables.

#### No Number Formatting in Tables
**Problem:** Numeric columns show raw values.
- $1234567 instead of $1,234,567
- 0.333333 instead of 33.3%

**Impact:** MEDIUM-HIGH. Professional tables format numbers.

#### Pagination Not Clearly Visible
**Problem:** The "Limit" dropdown (10, 25, 50, 100, 500, 1000, All) is buried in the widget editor. Users don't know they can paginate.
- No "Page 1 of 5" indicator in the view
- No "Next/Previous" buttons

**Impact:** MEDIUM. Users expect pagination UI in tables.

---

## 5. Widget Management & Editing

### Strengths
- **Drag-to-move** and **drag-to-resize** work (when I tested resizing)
- **Context menu** (⋮) with Edit, Refresh, Duplicate, Export CSV, Delete
- **Live refresh** icon available
- **Undo/redo** buttons (though disabled in test state)

### Issues (P1)

#### No Visual Feedback on Drag Operations
**Problem:** When dragging a widget to move/resize, there's no ghost preview or outline. User sees the affordance but no confirmation of where it will land.

**Impact:** MEDIUM. Users feel uncertain about drag operations.

#### "Refresh" Button Not Always Visible
**Problem:** The refresh button (↻ Off / ↻) is in the top toolbar, not on the widget itself. User must click widget menu → Refresh, or use the toolbar button.
- No per-widget refresh button
- "Off" toggle is mysterious (turns off auto-refresh? Turns off the widget?)

**Impact:** LOW-MEDIUM. Minor friction, but "Refresh" is a primary action.

#### Export CSV Success Not Confirmed
**Problem:** Clicking "Export CSV" provides no feedback. Did it download? Was there an error?

**Impact:** LOW. Most browsers auto-download, but no toast/confirmation.

---

## 6. Polish & Feel

### Visual Issues (P1)

#### Inconsistent Font Sizes & Spacing
**Problem:**
- Widget titles vary in size
- Padding around widgets feels inconsistent
- No clear visual hierarchy

**Impact:** MEDIUM. Professional tools have strict spacing rules.

#### No Hover States on Interactive Elements
**Problem:**
- Buttons don't highlight on hover
- Menu items (⋮) don't show "click me" affordance
- Table rows don't highlight on hover

**Impact:** MEDIUM-HIGH. Hover states signal interactivity and improve usability.

#### Dark Theme Has Contrast Issues
**Problem:**
- Some text (especially in legends, axis labels) is dark-gray on dark background
- Chart legend in "Earthquake Trend by Month" is barely readable

**Impact:** MEDIUM. Fails accessibility checks (WCAG AA contrast).

#### Error Messages Are Vague
**Problem:** If something fails (e.g., widget query error), message is terse.
- Actual: "No query configured"
- Better: "This widget has no query. Edit the widget and select a table."

**Impact:** LOW-MEDIUM. Users can figure it out, but doesn't feel polished.

### Interaction Issues (P1)

#### No Loading States
**Problem:** When a dashboard first loads or a filter is applied, no "Loading..." indicator. User sees old data until new data appears.

**Impact:** MEDIUM. Users feel unsure if the action worked.

#### Auto-Save Not Visible
**Problem:** Widget says "Not yet saved" but then shows "Saved" without any UI feedback.
- No toast notification
- No progress bar
- Just a text label change

**Impact:** MEDIUM-LOW. Users expect visual confirmation of save (spinner, checkmark).

#### Filter Application Not Immediate
**Problem:** In "QA Interactive Test," changing the date filter doesn't update widgets instantly.
- Unknown delay
- No "Loading" state on widgets
- User doesn't know if filter was applied

**Impact:** MEDIUM. Feels sluggish.

### Micro-Interactions (P2)

#### No Tooltips
**Problem:** Hover over anything → nothing happens.
- ⋮ button: No tooltip "Menu"
- ↻ icon: No tooltip "Refresh"
- Chart elements: No data tooltips (value on hover)

**Impact:** MEDIUM. Modern UIs have rich tooltips.

#### No Empty State Messages for Widgets
**Problem:** If a widget has no data (e.g., "No query configured"), it just shows blank space.
- Should show: "Click ⋮ > Edit to configure this widget"

**Impact:** LOW-MEDIUM. Slightly confusing for new users.

---

## 7. Competitive Comparison Matrix

| Feature | havn | Power BI | Databricks | Tableau |
|---------|------|----------|-----------|---------|
| **Visual Builder** | ✓ (20+ types) | ✓ | ✗ (SQL-first) | ✓ (Drag-drop) |
| **Dashboard Filters** | ⚠ (Limited) | ✓✓ (Rich) | ⚠ (SQL) | ✓✓ |
| **Cross-Filtering** | ✗ | ✓✓ | ✗ | ✓✓ |
| **Conditional Formatting** | ✗ | ✓ | ✗ | ✓ |
| **Reference Lines** | ✗ | ✓ | ✗ | ✓ |
| **Calculated Fields** | ✗ | ✓ | ✗ | ✓ |
| **Drill-Down** | ✗ | ✓ | ✗ | ✓✓ |
| **Parameters** | ⚠ (Filters only) | ✓✓ | ✗ | ✓✓ |
| **Multi-Page** | ✗ | ✓ | ⚠ (Worksheets) | ✓ |
| **Email Delivery** | ✗ | ✓ | ✗ | ✓ |
| **Mobile Responsive** | ⚠ (No explicit support) | ✓ | ✓ | ✓ |
| **Sharing & Embed** | ⚠ (Not discoverable) | ✓ | ✓ | ✓ |
| **Table Sorting** | ⚠ (Edit mode only) | ✓✓ | ✓ | ✓ |
| **Number Formatting** | ✗ | ✓ | ✓ | ✓ |
| **Chart Interactivity** | ✗ (No zoom/pan/tooltips) | ✓ | ⚠ | ✓✓ |

**Key:** ✓ = Present, ⚠ = Partial/Limited, ✗ = Missing

---

## 8. What Would Make Me NOT Recommend This to My Team

### From a Databricks User:
1. **No cross-filtering.** I'm used to clicking a value to filter the dashboard. Without it, dashboards are just reports.
2. **Bar chart labels are unreadable.** This is a blocker for any categorical data.
3. **No drill-down.** I need to explore from summary to detail.
4. **No reference lines.** I can't show targets or benchmarks.
5. **Limited filters.** Date range is good, but I need multi-select and free-text search.

### From a Power BI User:
1. **No calculated fields.** I expect to create measures on the fly without SQL.
2. **Conditional formatting is missing.** Professional tables need red/green cell coloring.
3. **No parameters.** I want dropdown filters that affect multiple visuals.
4. **No mobile support.** My team uses iPads.
5. **Email scheduling is gone.** How do I share this with executives?

### From a Tableau User:
1. **No interactions.** No hover tooltips, no zoom, no pan.
2. **Drill-down is missing.** Essential for exploratory analysis.
3. **Chart interactions are weak.** I expect rich hover states and detail-on-demand.
4. **No hierarchies.** How do I drill from Region → City?
5. **Responsiveness is unclear.** My dashboards need to work on mobile.

### Universal Concerns:
1. **Bar chart X-axis labels are broken.** This is the #1 UX bug.
2. **Pie charts fail with skewed data.** No warning or auto-recommendation.
3. **Sorting tables requires edit mode.** Should be a click away.
4. **No tooltips anywhere.** Feels unfinished.

---

## 9. Top 10 Things to Fix Before Shipping

### P0 (Blockers - Must Fix)
1. **FIX BAR CHART X-AXIS LABEL OVERLAP**
   - Auto-rotate labels 45°-90° for readability
   - Add truncation + tooltip for long names
   - Test with 20+ categories
   - This is the #1 showstopper

2. **ADD CROSS-FILTERING (Click to Filter)**
   - Clicking a bar/point filters other charts
   - This is expected in modern BI tools
   - Without it, dashboards are static

3. **WARN ON BAD CHART CHOICES**
   - Pie chart with >70% dominant slice → recommend Bar/Treemap
   - Scatter plot with 1000+ points → recommend Heatmap
   - Recommend chart type based on data shape

### P1 (Major Gaps - Should Fix)
4. **ENABLE TABLE COLUMN SORTING**
   - Click column header to sort A→Z or Z→A
   - Don't require edit mode
   - Show sort indicator (↑/↓)

5. **ADD CONDITIONAL FORMATTING**
   - Color cells red/green by value or threshold
   - Support top/bottom N highlighting
   - Add heatmap scale option

6. **ADD REFERENCE / TARGET LINES**
   - Horizontal/vertical lines for benchmarks
   - Support calculation (e.g., average, previous period)
   - Essential for KPI dashboards

7. **IMPROVE DASHBOARD FILTERS**
   - Allow multi-select, not just date ranges
   - Add free-text search filter
   - Show applied filters clearly to viewer
   - Let viewers interact with filters (not just editors)

8. **ADD TABLE SEARCH / IN-TABLE FILTERING**
   - Ctrl+F style search in table rows
   - Column-level filter dropdowns
   - Support regex or wildcard

9. **HIDE UNCONFIGURED WIDGETS**
   - "No query configured" widgets clutter dashboards
   - Either auto-hide or show helpful prompt
   - Current state (QA Interactive Test) looks broken

10. **ADD HOVER TOOLTIPS & IMPROVED FEEDBACK**
    - Tooltips on all buttons (⋮ = Menu, ↻ = Refresh)
    - Data tooltips on chart hover (value, category)
    - Toast notifications for save/export/error
    - Loading spinners when filtering

### P2 (Polish - Nice to Have)
- Mobile-responsive layout
- Calculated field support (DAX/expression editor)
- Drill-down hierarchies
- Email scheduling
- Parameters / dynamic filters
- Column resizing in tables
- Number formatting (currency, percentage, decimals)
- Multi-page dashboards
- Chart interactions (zoom, pan)

---

## 10. Specific Screenshots & Evidence

| Finding | Screenshot | Location |
|---------|-----------|----------|
| Empty dashboard helpful | 02-empty-dashboard.png | Clear call-to-action |
| Widget preset options | 03-widget-presets.png | 8 preset types shown |
| Bar chart with label overlap | 07-existing-dashboard.png | "Events by Region" X-axis unreadable |
| Pie chart dominance (86% slice) | 06-pie-chart.png | Single magenta slice dominates |
| Dashboard filter UI | 11-interactive-dashboard.png | Date range filters at top |
| Widget edit mode | 08-dashboard-edit-mode.png | Drag-to-move/resize handles visible |
| Widget context menu | 09-widget-context-menu.png | Edit, Refresh, Duplicate, Export, Delete |
| KPI widget editing | 10-widget-edit-kpi.png | Value column, Compare to, Prefix, Suffix |
| Unconfigured widgets | 11-interactive-dashboard.png | "No query configured" messages |

---

## 11. Recommendations for Product Roadmap

### Immediate (v0.3 / 2 weeks)
- [ ] Fix bar chart X-axis label rendering (auto-rotate, truncate, tooltip)
- [ ] Add column click sorting to tables
- [ ] Hide "No query configured" widgets in view mode
- [ ] Add hover tooltips to all icons and buttons

### Short-term (v0.4 / 4 weeks)
- [ ] Implement cross-filtering (click chart → filter others)
- [ ] Add reference line support to charts
- [ ] Add conditional formatting to tables
- [ ] Add warning for bad chart choices (pie >70%, etc.)
- [ ] Improve chart type recommendations with descriptions

### Medium-term (v0.5 / 6 weeks)
- [ ] Multi-select and free-text dashboard filters
- [ ] Table search and column filtering
- [ ] Calculated field / expression editor
- [ ] Mobile-responsive layout
- [ ] Data tooltips on chart hover

### Long-term (v1.0)
- [ ] Multi-page dashboards (tabbed)
- [ ] Drill-down hierarchies
- [ ] Email scheduling
- [ ] Parameters and dynamic filters
- [ ] Dashboard templates & gallery
- [ ] Sharing & embedding
- [ ] Number formatting (currency, %, decimals)

---

## Conclusion

havn's dashboard feature has **solid fundamentals** but **needs polish to be competitive**. The visual builder is intuitive, chart type variety is impressive, and the core creation flow is fast. However, the **X-axis label bug, missing cross-filtering, and absent conditional formatting** are the biggest gaps.

**For beta/early adopter use:** Good. Users can build simple dashboards quickly.
**For production/enterprise use:** Not yet. Too many small friction points and missing advanced features that power users expect.

**Key insight:** havn is competing at the visualization tier (building charts), but Power BI/Databricks/Tableau win at the **interaction tier** (cross-filtering, parameters, drill-down). Focus on interactivity first, polish second, and advanced features third.

The team is on the right track. Fix the X-axis bug, add cross-filtering, and the dashboard feature becomes genuinely useful. With those two things, havn moves from "cute prototype" to "viable alternative."

---

## Appendix: Detailed Test Scenarios

### Scenario 1: Quick Dashboard Creation
**Time:** ~3 minutes
1. New Dashboard: "Sales Performance Q1" ✓
2. Add Widget → Custom → earthquake_events ✓
3. Preset: "Sum by category" → auto-selected magnitude sum by event_id ✓
4. Switch to Pie chart → renders, but one slice dominates (⚠ Not ideal for UX)
5. Close, save ✓

**Verdict:** Fast, but preset choice wasn't ideal. User needs more guidance.

### Scenario 2: Viewing Existing Dashboard with Filters
**Dashboard:** "QA Interactive Test"
1. Load dashboard → shows Event Date filter ✓
2. Filter applied (Last 7d) → unclear if it affected all widgets ⚠
3. Several widgets show "No query configured" → confusing ⚠
4. Try to interact with chart → no hover tooltip, no drill-down ✗

**Verdict:** Filters exist, but incomplete implementation and weak interactivity.

### Scenario 3: Editing a KPI Card Widget
**Time:** ~2 minutes
1. Dashboard in Edit mode ✓
2. Click widget menu → Edit ✓
3. Change Value column, add Compare-to column ✓
4. Preview updates instantly ✓
5. Save changes ✓

**Verdict:** Smooth editing experience. Feature parity with Power BI KPI cards.

### Scenario 4: Attempting Advanced Analysis (Fails)
1. Want to show: SUM(revenue) and AVG(revenue) on same chart
   - Must add column twice ✗
   - No "add aggregation" button
2. Want to add a target line at $1M
   - Feature doesn't exist ✗
3. Want to drill from Region → City
   - Feature doesn't exist ✗
4. Want conditional formatting on table cells
   - Feature doesn't exist ✗

**Verdict:** Ad-hoc analysis is blocked. Requires returning to SQL transforms.

---

**End of Audit**
