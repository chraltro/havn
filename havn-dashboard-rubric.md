# havn Dashboard Designer: QA Rubric

Exhaustive checklist for evaluating havn's dashboard designer as a platform. Each item is a binary PASS/FAIL capability check. The question is always: "Can a data analyst do this?" If not, it's a FAIL.

---

## 1. Color and Theming (1-25)

1. Analyst can pick a custom color for any individual series or data segment.
2. Analyst can pick a custom color for any individual bar, slice, or data point.
3. Default color palette contains at least 12 visually distinguishable colors.
4. A built-in colorblind-safe palette is available as a one-click option.
5. At least 3 built-in palettes are available (e.g. categorical, sequential, diverging).
6. Analyst can create and save a custom color palette.
7. Analyst can import a palette by hex codes (paste a list).
8. Analyst can set conditional color rules (e.g. red if value < 0, green if > target).
9. Conditional color supports gradient/scale, not just discrete thresholds.
10. All charts on a dashboard respect a global theme when one is set.
11. Changing the global theme propagates to all existing charts without manual updates.
12. Analyst can override the global theme on a per-chart basis.
13. Background color is configurable per chart.
14. Background color is configurable for the dashboard canvas.
15. Font color adjusts automatically for contrast when background changes.
16. Legend colors are editable per legend entry.
17. Legend color mapping is sticky: category "Norway" always gets the same color across charts.
18. Border/outline color on chart elements is configurable.
19. Gridline color is configurable.
20. Axis line color is configurable.
21. Tooltip background and text color are configurable.
22. KPI card background and text color are configurable.
23. Table header row color is configurable.
24. Table alternating row colors are configurable (zebra striping).
25. Dark mode is available as a theme option.

## 2. Chart Types and Selection (26-50)

26. Bar chart (vertical) is available.
27. Bar chart (horizontal) is available.
28. Stacked bar chart is available.
29. Grouped bar chart is available.
30. 100% stacked bar chart is available.
31. Line chart is available.
32. Multi-line chart (multiple series) is available.
33. Area chart is available.
34. Stacked area chart is available.
35. Pie chart is available.
36. Donut chart is available.
37. Scatter plot is available.
38. Bubble chart (scatter with size encoding) is available.
39. Histogram is available.
40. Box plot / whisker chart is available.
41. Heatmap is available.
42. Treemap is available.
43. Funnel chart is available.
44. Waterfall chart is available.
45. Gauge / KPI dial is available.
46. KPI card (single big number with optional trend) is available.
47. Table / data grid is available.
48. Combo chart (bar + line on same chart) is available.
49. Analyst can change chart type after creation without rebuilding from scratch.
50. Chart type recommendations are shown based on the selected data shape (e.g. "you have one measure and one time dimension, consider a line chart").

## 3. Axis Configuration (51-75)

51. Analyst can set axis title text.
52. Analyst can hide axis titles.
53. Analyst can set axis min value manually.
54. Analyst can set axis max value manually.
55. Analyst can set axis step/interval manually.
56. Analyst can toggle logarithmic scale.
57. Analyst can set number format on the axis (plain, thousands, millions, percentage).
58. Analyst can set currency symbol on the axis.
59. Analyst can set decimal precision on axis labels.
60. Analyst can control label rotation (0, 45, 90 degrees).
61. Analyst can hide axis labels entirely.
62. Axis labels auto-adapt to avoid overlap at current chart width.
63. Truncated axis labels show full text on hover.
64. Date axes support configurable granularity (hour, day, week, month, quarter, year).
65. Date axes auto-adapt display format to the selected range.
66. Date axis format is manually overridable (e.g. "MMM YYYY" vs "YYYY-MM-DD").
67. Analyst can toggle gridlines on/off per axis.
68. Analyst can set gridline style (solid, dashed, dotted).
69. Analyst can add a reference/target line at a specific value.
70. Reference line supports a label.
71. Reference line supports dashed/dotted/solid style.
72. Analyst can add a reference band (shaded range between two values).
73. Y-axis supports inverted direction (high at bottom).
74. Dual y-axis is supported for combo charts.
75. Axis tick marks are configurable (inside, outside, none).

## 4. Data Labels and Tooltips (76-100)

76. Analyst can toggle data labels on/off per chart.
77. Data label position is configurable (above, center, below, outside for pie).
78. Data label format is configurable (value, percentage, both, category + value).
79. Data labels support number formatting (thousands, decimals, currency).
80. Data labels auto-hide when they would overlap or collide.
81. Analyst can toggle data labels for specific series only.
82. Tooltips appear on hover for all chart types.
83. Tooltip shows the data point value by default.
84. Tooltip shows the category/x-axis value.
85. Tooltip shows all series values at that x-position (shared tooltip for line charts).
86. Analyst can add extra fields to the tooltip beyond what's visualized.
87. Analyst can remove default fields from the tooltip.
88. Analyst can reorder tooltip fields.
89. Tooltip values respect the chart's number format settings.
90. Tooltip works on touch devices (tap instead of hover).
91. Tooltips don't get clipped by chart container boundaries.
92. Tooltips don't flicker when moving between adjacent data points.
93. KPI cards show trend direction (up/down arrow or indicator).
94. KPI cards show comparison period value (e.g. "vs. last month").
95. KPI cards show percentage change.
96. KPI cards support sparkline (mini trend chart).
97. KPI card value format is configurable (number, currency, percentage).
98. KPI card decimal precision is configurable.
99. KPI card conditional formatting (color changes based on value) is supported.
100. KPI card supports a subtitle or description line.

## 5. Filtering and Interactivity (101-135)

101. Dropdown single-select filter is available.
102. Dropdown multi-select filter is available.
103. Date range picker is available.
104. Date range picker has presets (today, last 7d, last 30d, MTD, QTD, YTD, custom).
105. Numeric range slider filter is available.
106. Free text search filter is available.
107. Toggle/switch filter (boolean) is available.
108. Dropdown filter with more than 50 items has a search box.
109. Dropdown filter shows item count next to each option.
110. Filters support a default value that loads on dashboard open.
111. Analyst can set "all" as the default for multi-select filters.
112. Dependent/cascading filters are supported (filter B options update based on filter A).
113. Active filters are visually indicated (badge, highlight, or tag).
114. A "reset all filters" button is available.
115. Cross-filtering between charts works (click a bar, other charts filter).
116. Cross-filtering is visually indicated (dimming non-selected elements).
117. Cross-filtering can be turned on or off per chart.
118. Click-to-drill-down is supported (e.g. click "2024" to see months).
119. Drill-down path is configurable by the analyst.
120. A breadcrumb shows current drill-down level with ability to navigate back.
121. Right-click context menu on chart elements offers useful actions (filter, exclude, drill).
122. Analyst can set a chart to be non-interactive (display only, no click events).
123. Zoom on time-series charts is supported (brush/select a date range to zoom).
124. Zoom can be reset to full range.
125. Pan is supported after zoom.
126. Click on a data point can open a detail view or linked page.
127. Filters persist when switching between dashboard tabs/pages.
128. Filter state is reflected in the URL (shareable filtered views).
129. Analyst can create a "saved view" (named set of filter values).
130. Saved views are selectable from a dropdown.
131. Dashboard auto-refreshes on a configurable interval for live data.
132. Manual refresh button is available.
133. Loading indicator shows when data is being fetched.
134. Last-refreshed timestamp is displayed.
135. Slow queries (>2s) show a visual indicator or warning.

## 6. Layout and Positioning (136-160)

136. Charts can be repositioned by drag-and-drop.
137. Charts can be resized by dragging edges or corners.
138. Snap-to-grid is available when positioning.
139. Alignment guides appear when dragging near other chart edges.
140. Charts can be aligned (left, center, right, top, bottom) relative to each other.
141. Charts can be distributed evenly (equal spacing).
142. Dashboard supports a grid layout mode.
143. Dashboard supports a free-form / canvas layout mode.
144. Analyst can set dashboard width (fixed px or fluid percentage).
145. Dashboard has a responsive mode for narrower viewports.
146. Charts reflow sensibly at mobile widths.
147. Analyst can define different layouts for desktop vs. tablet vs. mobile.
148. Analyst can group charts into collapsible sections.
149. Dashboard supports tabbed pages (multiple pages within one dashboard).
150. Analyst can set padding/margin between charts.
151. Analyst can set chart container border radius.
152. Analyst can set chart container shadow.
153. Analyst can set chart container border.
154. Analyst can add section headers / dividers between chart groups.
155. Analyst can add a dashboard title.
156. Analyst can add a dashboard subtitle or description.
157. Chart z-index / layering order is controllable.
158. Analyst can lock a chart's position (prevent accidental moves).
159. Full-screen mode for individual charts (expand to fill viewport).
160. Analyst can set a minimum height for each chart.

## 7. Data Configuration (161-190)

161. Analyst can select data source / table for a chart.
162. Analyst can switch data source on an existing chart without rebuilding.
163. Analyst can select which columns map to which encoding (x, y, color, size, label).
164. Analyst can add multiple measures to a single chart.
165. Analyst can add a calculated/derived field (simple expressions: +, -, *, /, %).
166. Calculated fields support conditional logic (IF/CASE).
167. Aggregation type is selectable per measure (SUM, AVG, COUNT, MIN, MAX, MEDIAN, COUNT DISTINCT).
168. Analyst can change aggregation after chart creation.
169. Analyst can set sort order on the category axis (ascending, descending, alphabetical, custom).
170. Analyst can sort by the measure value (e.g. top-10 bars).
171. Analyst can limit to top/bottom N items.
172. Analyst can add a "group by" dimension.
173. Analyst can write a custom SQL/DuckDB query as a data source.
174. Custom query results are previewable before binding to a chart.
175. Analyst can see a data preview (first N rows) for any bound data source.
176. Null values are handled visibly (shown as gap, zero, or "N/A" with a configurable choice).
177. Empty result sets show a clear message, not a blank chart.
178. Empty state message is customizable.
179. Date/time fields are auto-detected and offered for time-series treatment.
180. Analyst can set a date field as the "default time axis" for the dashboard.
181. Analyst can set data type overrides (treat a number as a category, etc).
182. Analyst can rename columns/fields as they appear in charts (display names).
183. Data source errors (connection failed, query error) show a clear message in the chart.
184. Analyst can set a per-chart query timeout.
185. Analyst can preview the generated SQL for any chart.
186. Analyst can join/blend two data sources for a single chart.
187. Analyst can create a parameter (user-input variable) that feeds into queries.
188. Parameters are exposed as dashboard controls.
189. Analyst can set number locale (1.000,00 vs 1,000.00).
190. Analyst can set date locale (DD/MM/YYYY vs MM/DD/YYYY).

## 8. Tables and Data Grids (191-210)

191. Column sorting by clicking header is supported.
192. Multi-column sort is supported (shift-click).
193. Column reordering by drag is supported.
194. Column resizing by dragging border is supported.
195. Column visibility toggle (show/hide columns) is supported.
196. Column pinning / freezing (left or right) is supported.
197. Header row stays fixed on vertical scroll.
198. Conditional formatting on cells is supported (color scale, icon, bar).
199. Cell values are formattable (number, currency, percentage, date format).
200. Text wrapping in cells is configurable.
201. Row striping (zebra) is available.
202. Row highlight on hover is present.
203. Inline search / filter per column is supported.
204. Pagination is supported with configurable page size.
205. Virtual scroll is supported for large datasets as an alternative to pagination.
206. Total / summary row (sum, average, count) is available.
207. Cell click to copy value is supported.
208. Selected rows can be exported.
209. Table supports row grouping / collapsible groups.
210. Table supports nested/hierarchical rows.

## 9. Export and Sharing (211-225)

211. Individual chart export to PNG is supported.
212. Individual chart export to SVG is supported.
213. Individual chart data export to CSV is supported.
214. Full dashboard export to PDF is supported.
215. Full dashboard export to PNG/image is supported.
216. Dashboard has a shareable URL.
217. Shared URL respects current filter state.
218. Dashboard can be embedded in an iframe.
219. Embed code is provided with copy button.
220. Dashboard can be set to view-only mode (no editing allowed).
221. Dashboard supports scheduled email delivery (send PDF/screenshot to recipients on a schedule).
222. Dashboard supports alert/notification (notify when a metric crosses a threshold).
223. Print-friendly layout is available (removes interactive elements, fits pages).
224. Analyst can add their own logo or branding to exports.
225. Export resolution / DPI is configurable for images.

## 10. Workflow and Editing (226-250)

226. Undo is supported in the editor.
227. Redo is supported in the editor.
228. Undo history is at least 20 steps deep.
229. Autosave is present.
230. Save indicator shows saved / unsaved state.
231. Version history is available (see and restore previous versions).
232. Analyst can duplicate a chart within the dashboard.
233. Analyst can copy a chart from one dashboard to another.
234. Analyst can duplicate an entire dashboard.
235. Dashboard editor has a preview mode (see it as end user would).
236. Keyboard shortcuts exist for common actions (save, undo, duplicate, delete).
237. Analyst can select multiple charts and move/align/delete them as a group.
238. Analyst can set a dashboard as "template" for others to clone.
239. Comments or annotations can be added to specific charts.
240. Analyst can add a text/markdown block to the dashboard.
241. Text block supports basic formatting (bold, italic, links, lists).
242. Analyst can add an image block to the dashboard.
243. Analyst can add a divider/spacer element.
244. Analyst can add a shape (rectangle, circle) as a decorative/grouping element.
245. Dashboard metadata is editable (title, description, tags, owner).
246. Dashboard has a "last edited by" indicator.
247. Dashboard supports access control (who can view, who can edit).
248. Analyst receives a warning when navigating away with unsaved changes.
249. Analyst can pin/favorite dashboards for quick access.
250. Dashboard list/home view supports search by name and tag.
