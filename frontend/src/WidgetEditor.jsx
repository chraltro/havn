import React, { useState, useEffect, useMemo, useRef, useCallback } from "react";
import { api } from "./api";
import { useDashboard } from "./DashboardContext";
import { DASHBOARD_CHART_TYPES, analyzeColumns, detectBestChart, fmtNum, COLORS } from "./chartUtils";
import { formatNumber } from "./chartStyleDefaults";
import ChartPanel from "./ChartPanel";
import DashboardChart from "./DashboardCharts";
import SortableTable from "./SortableTable";

/**
 * Widget Editor — visual query builder + chart configurator.
 *
 * Two modes:
 *   Visual (default) — pick table, check columns, add aggregations/filters/sort
 *   SQL (advanced)   — raw SQL editor for power users
 *
 * Flow: Pick Table → Select Columns → Configure (agg/filter/sort) → Choose Chart → Save
 */

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const NUMERIC_TYPES = /^(INTEGER|BIGINT|SMALLINT|TINYINT|HUGEINT|FLOAT|DOUBLE|DECIMAL|NUMERIC|REAL)/i;
const TEMPORAL_TYPES = /^(DATE|TIMESTAMP|TIME|INTERVAL)/i;

function isNumeric(type) { return NUMERIC_TYPES.test(type); }
function isTemporal(type) { return TEMPORAL_TYPES.test(type); }
function typeIcon(type) {
  if (isNumeric(type)) return "#";
  if (isTemporal(type)) return "⏱";
  if (/^BOOLEAN/i.test(type)) return "◉";
  return "Aa";
}
function typeColor(type) {
  if (isNumeric(type)) return "var(--havn-accent)";
  if (isTemporal(type)) return "var(--havn-yellow)";
  if (/^BOOLEAN/i.test(type)) return "var(--havn-purple)";
  return "var(--havn-green)";
}

const AGG_OPTIONS = [
  { value: "", label: "Raw value" },
  { value: "SUM", label: "Sum" },
  { value: "AVG", label: "Average" },
  { value: "COUNT", label: "Count" },
  { value: "MIN", label: "Min" },
  { value: "MAX", label: "Max" },
  { value: "COUNT_DISTINCT", label: "Count distinct" },
];

const FILTER_OPS = [
  { value: "=", label: "equals" },
  { value: "!=", label: "not equals" },
  { value: ">", label: "greater than" },
  { value: "<", label: "less than" },
  { value: ">=", label: "at least" },
  { value: "<=", label: "at most" },
  { value: "LIKE", label: "contains" },
  { value: "IS NULL", label: "is null" },
  { value: "IS NOT NULL", label: "is not null" },
];

const DATE_GROUPING_OPTIONS = [
  { value: "", label: "Raw" },
  { value: "day", label: "Day" },
  { value: "week", label: "Week" },
  { value: "month", label: "Month" },
  { value: "quarter", label: "Quarter" },
  { value: "year", label: "Year" },
];

// ---------------------------------------------------------------------------
// SQL Generator — builds SQL from visual config
// ---------------------------------------------------------------------------

function buildSQL(config) {
  const { table, columns, aggregations, dateGrouping, filters, orderBy, limit, calculatedFields } = config;
  if (!table || columns.length === 0) return "";

  const fqn = `${table.schema}.${table.name}`;
  const hasAgg = columns.some(c => aggregations[c.name]);
  const hasDateGroup = columns.some(c => dateGrouping?.[c.name]);

  // SELECT clause
  const selectParts = columns.map(col => {
    const agg = aggregations[col.name];
    const dg = dateGrouping?.[col.name];

    // Date grouping: wrap in DATE_TRUNC
    if (dg && isTemporal(col.type)) {
      const truncExpr = `DATE_TRUNC('${dg}', ${col.name})`;
      const alias = `${col.name}_${dg}`;
      return `${truncExpr} AS ${alias}`;
    }
    if (agg === "COUNT_DISTINCT") return `COUNT(DISTINCT ${col.name}) AS ${col.name}_count_distinct`;
    if (agg) return `${agg}(${col.name}) AS ${col.name}_${agg.toLowerCase()}`;
    return col.name;
  });

  // Calculated fields: add to SELECT
  const calcFields = (calculatedFields || []).filter(cf => cf.name && cf.expression);
  for (const cf of calcFields) {
    selectParts.push(`${cf.expression} AS ${cf.name}`);
  }

  // GROUP BY — all non-aggregated columns (using expressions, not aliases)
  // Calculated fields that use aggregation functions are excluded from GROUP BY
  const AGG_RE = /\b(SUM|AVG|COUNT|MIN|MAX|TOTAL|GROUP_CONCAT)\s*\(/i;
  const groupExprs = (hasAgg || hasDateGroup)
    ? [
        ...columns.filter(c => !aggregations[c.name]).map(col => {
          const dg = dateGrouping?.[col.name];
          if (dg && isTemporal(col.type)) return `DATE_TRUNC('${dg}', ${col.name})`;
          return col.name;
        }),
        ...calcFields.filter(cf => !AGG_RE.test(cf.expression)).map(cf => cf.expression),
      ]
    : [];

  // WHERE
  const whereParts = filters
    .filter(f => f.column && f.op)
    .map(f => {
      if (f.op === "IS NULL") return `${f.column} IS NULL`;
      if (f.op === "IS NOT NULL") return `${f.column} IS NOT NULL`;
      if (f.op === "LIKE") return `${f.column} ILIKE '%${(f.value || "").replace(/'/g, "''")}%'`;
      return `${f.column} ${f.op} '${(f.value || "").replace(/'/g, "''")}'`;
    });

  // ORDER BY — use expressions for date-truncated columns
  const orderParts = orderBy
    .filter(o => o.column && o.dir)
    .map(o => {
      // Find the column to check if it has date grouping
      const col = columns.find(c => c.name === o.column);
      const dg = col && dateGrouping?.[col.name];
      if (dg && col && isTemporal(col.type)) {
        return `DATE_TRUNC('${dg}', ${o.column}) ${o.dir}`;
      }
      return `${o.column} ${o.dir}`;
    });

  let sql = `SELECT\n    ${selectParts.join(",\n    ")}\nFROM ${fqn}`;
  if (whereParts.length) sql += `\nWHERE ${whereParts.join("\n  AND ")}`;
  if (groupExprs.length) sql += `\nGROUP BY ${groupExprs.join(", ")}`;
  if (orderParts.length) sql += `\nORDER BY ${orderParts.join(", ")}`;
  if (limit) sql += `\nLIMIT ${limit}`;

  return sql;
}

// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------

export default function WidgetEditor({ widget, onClose, onSave }) {
  const { updateWidget, addWidget, refreshWidget } = useDashboard();
  const isNew = !widget?.id;

  // Restore visual state from config if available
  const savedVisual = widget?.config?._visual || null;
  const hasVisualState = !!savedVisual;

  // Mode: "visual" or "sql"
  const [mode, setMode] = useState(
    hasVisualState || widget?._fromVisual || !widget?.sql_query ? "visual" : "sql"
  );

  // Widget metadata
  const [widgetType, setWidgetType] = useState(widget?.widget_type || "chart");
  const [chartType, setChartType] = useState(widget?.chart_type || "bar");
  const [title, setTitle] = useState(widget?.title || "");
  const [widgetConfig, setWidgetConfig] = useState(widget?.config || {});
  const [cacheTtl, setCacheTtl] = useState(widget?.cache_ttl || 0);

  // SQL mode
  const [sqlQuery, setSqlQuery] = useState(widget?.sql_query || "");

  // Visual builder state — restore from saved if available
  const [allTables, setAllTables] = useState([]);
  const [selectedTable, setSelectedTable] = useState(
    savedVisual?.table || null
  );
  const [tableColumns, setTableColumns] = useState([]);
  const [selectedColumns, setSelectedColumns] = useState(
    savedVisual?.selectedColumns || []
  );
  const [aggregations, setAggregations] = useState(
    savedVisual?.aggregations || {}
  );
  const [dateGrouping, setDateGrouping] = useState(
    savedVisual?.dateGrouping || {}
  );
  const [vFilters, setVFilters] = useState(
    savedVisual?.filters || []
  );
  const [orderBy, setOrderBy] = useState(savedVisual?.orderBy || []);
  const [rowLimit, setRowLimit] = useState(savedVisual?.rowLimit ?? 100);
  const [calculatedFields, setCalculatedFields] = useState(
    savedVisual?.calculatedFields || []
  );
  const [columnDisplayNames, setColumnDisplayNames] = useState(
    savedVisual?.columnDisplayNames || {}
  );
  const [tableSearch, setTableSearch] = useState("");
  const [sampleData, setSampleData] = useState(null);

  // Preview
  const [previewData, setPreviewData] = useState(null);
  const [previewError, setPreviewError] = useState(null);
  const [previewing, setPreviewing] = useState(false);
  const [suggestions, setSuggestions] = useState([]);

  // Load tables on mount
  useEffect(() => {
    api.listTables().then(tables => {
      setAllTables(tables || []);
    }).catch(() => {});
  }, []);

  // Load columns when table selected
  useEffect(() => {
    if (!selectedTable) return;
    api.describeTable(selectedTable.schema, selectedTable.name).then(desc => {
      setTableColumns(desc.columns || []);
    }).catch(() => {});
    // Load sample
    api.sampleTable(selectedTable.schema, selectedTable.name, 5).then(data => {
      setSampleData(data);
    }).catch(() => {});
  }, [selectedTable]);

  // Auto-run preview when visual config changes (debounced)
  const previewTimerRef = useRef(null);
  useEffect(() => {
    if (mode !== "visual" || !selectedTable || selectedColumns.length === 0) return;
    if (previewTimerRef.current) clearTimeout(previewTimerRef.current);
    previewTimerRef.current = setTimeout(() => {
      const sql = buildSQL({
        table: selectedTable,
        columns: selectedColumns,
        aggregations,
        dateGrouping,
        filters: vFilters,
        orderBy,
        limit: rowLimit,
        calculatedFields,
      });
      if (sql) runPreview(sql);
    }, 600);
    return () => { if (previewTimerRef.current) clearTimeout(previewTimerRef.current); };
  }, [selectedTable, selectedColumns, aggregations, dateGrouping, vFilters, orderBy, rowLimit, calculatedFields, mode]);

  async function runPreview(overrideSql) {
    const sql = overrideSql || (mode === "sql" ? sqlQuery : buildSQL({
      table: selectedTable,
      columns: selectedColumns,
      aggregations,
      filters: vFilters,
      orderBy,
      limit: rowLimit,
      calculatedFields,
    }));
    if (!sql.trim()) return;
    setPreviewing(true);
    setPreviewError(null);
    try {
      const result = await api.runQuery(sql);
      setPreviewData(result);
      if (result.columns?.length > 0 && result.rows?.length > 0) {
        const analysis = analyzeColumns(result.columns, result.rows);
        const best = detectBestChart(analysis, result.rows.length);
        const suggs = [best.type];
        if (best.type === "bar") suggs.push("hbar", "stacked", "treemap");
        if (best.type === "line") suggs.push("area", "sparkline");
        if (best.type === "donut") suggs.push("pie", "funnel");
        if (best.type === "kpi") suggs.push("gauge", "progress");
        if (best.type === "scatter") suggs.push("bubble");
        setSuggestions([...new Set(suggs)]);
        // Auto-select best chart type if first time
        if (!chartType || chartType === "bar") setChartType(best.type);
      }
    } catch (e) {
      setPreviewError(e.message || "Query failed");
      setPreviewData(null);
    } finally {
      setPreviewing(false);
    }
  }

  // Generate final SQL
  function getFinalSQL() {
    if (mode === "sql") return sqlQuery;
    return buildSQL({
      table: selectedTable,
      columns: selectedColumns,
      aggregations,
      dateGrouping,
      filters: vFilters,
      orderBy,
      limit: rowLimit,
      calculatedFields,
    });
  }

  // Auto-generate title
  function autoTitle() {
    if (title) return;
    if (!selectedTable) return;
    const hasAgg = selectedColumns.some(c => aggregations[c.name]);
    const aggCols = selectedColumns.filter(c => aggregations[c.name]);
    const groupCols = selectedColumns.filter(c => !aggregations[c.name]);
    if (aggCols.length && groupCols.length) {
      setTitle(`${aggCols[0].name} by ${groupCols[0].name}`);
    } else {
      setTitle(`${selectedTable.name}`);
    }
  }

  async function handleSave() {
    autoTitle();
    const finalSQL = getFinalSQL();
    // Save visual builder state in config so it can be restored on edit
    const configWithVisual = {
      ...widgetConfig,
      ...(mode === "visual" && selectedTable ? {
        _visual: {
          table: selectedTable,
          selectedColumns,
          aggregations,
          dateGrouping,
          filters: vFilters,
          orderBy,
          rowLimit,
          calculatedFields,
          columnDisplayNames,
        }
      } : {}),
    };
    const data = {
      widget_type: widgetType,
      chart_type: widgetType === "chart" ? chartType : null,
      title: title || (selectedTable ? selectedTable.name : "Widget"),
      sql_query: finalSQL,
      config: configWithVisual,
      cache_ttl: cacheTtl,
    };
    if (isNew) {
      data.position = widget?.position || { x: 1, y: 1, w: 8, h: 4 };
      data.filters = [];
      data.sort_order = 0;
      const w = await addWidget(data);
      if (w) refreshWidget(w.id);
    } else {
      await updateWidget(widget.id, data);
      refreshWidget(widget.id);
    }
    onSave?.();
    onClose();
  }

  // Column toggle
  function toggleColumn(col) {
    setSelectedColumns(prev => {
      const exists = prev.find(c => c.name === col.name);
      if (exists) {
        const next = prev.filter(c => c.name !== col.name);
        // Clean up agg/order/dateGrouping for removed column
        setAggregations(a => { const n = { ...a }; delete n[col.name]; return n; });
        setDateGrouping(d => { const n = { ...d }; delete n[col.name]; return n; });
        setOrderBy(o => o.filter(x => x.column !== col.name));
        return next;
      }
      // Auto-configure temporal columns: default to "month" grouping + ASC sort
      if (isTemporal(col.type)) {
        setDateGrouping(d => ({ ...d, [col.name]: "month" }));
        setOrderBy(o => {
          if (o.some(x => x.column === col.name)) return o;
          return [...o, { column: col.name, dir: "ASC" }];
        });
        // Auto-suggest line chart when temporal + numeric combo
        const willHaveNumeric = prev.some(c => isNumeric(c.type)) ||
          tableColumns.some(c => isNumeric(c.type) && prev.some(p => p.name === c.name));
        if (willHaveNumeric && (chartType === "bar" || chartType === "hbar")) {
          setChartType("line");
        }
      }
      return [...prev, col];
    });
  }

  // Select all / none
  function selectAllColumns() {
    setSelectedColumns([...tableColumns]);
  }
  function selectNoColumns() {
    setSelectedColumns([]);
    setAggregations({});
    setDateGrouping({});
    setOrderBy([]);
  }

  // Quick column templates
  function quickSelectNumeric() {
    const nums = tableColumns.filter(c => isNumeric(c.type));
    setSelectedColumns(prev => {
      const existing = new Set(prev.map(c => c.name));
      return [...prev, ...nums.filter(c => !existing.has(c.name))];
    });
  }

  // Quick presets for aggregations
  function applyPreset(preset) {
    if (preset === "count_all") {
      const textCol = tableColumns.find(c => !isNumeric(c.type) && !isTemporal(c.type));
      const numCol = tableColumns.find(c => isNumeric(c.type));
      if (textCol) {
        setSelectedColumns([textCol, numCol || textCol]);
        if (numCol) setAggregations({ [numCol.name]: "COUNT" });
        else setAggregations({ [textCol.name]: "COUNT" });
      }
    } else if (preset === "sum_by") {
      const numCols = tableColumns.filter(c => isNumeric(c.type));
      const textCol = tableColumns.find(c => !isNumeric(c.type) && !isTemporal(c.type));
      if (numCols.length && textCol) {
        setSelectedColumns([textCol, numCols[0]]);
        setAggregations({ [numCols[0].name]: "SUM" });
      }
    } else if (preset === "trend") {
      const dateCol = tableColumns.find(c => isTemporal(c.type));
      const numCol = tableColumns.find(c => isNumeric(c.type));
      if (dateCol && numCol) {
        setSelectedColumns([dateCol, numCol]);
        setAggregations({ [numCol.name]: "SUM" });
        setDateGrouping({ [dateCol.name]: "month" });
        setOrderBy([{ column: dateCol.name, dir: "ASC" }]);
        setChartType("line");
      }
    } else if (preset === "top_10") {
      const numCol = tableColumns.find(c => isNumeric(c.type));
      const textCol = tableColumns.find(c => !isNumeric(c.type) && !isTemporal(c.type));
      if (numCol && textCol) {
        setSelectedColumns([textCol, numCol]);
        setOrderBy([{ column: numCol.name, dir: "DESC" }]);
        setRowLimit(10);
      }
    }
  }

  // Group tables by schema
  const groupedTables = useMemo(() => {
    const filtered = tableSearch
      ? allTables.filter(t => `${t.schema}.${t.name}`.toLowerCase().includes(tableSearch.toLowerCase()))
      : allTables;
    const groups = {};
    for (const t of filtered) {
      if (!groups[t.schema]) groups[t.schema] = [];
      groups[t.schema].push(t);
    }
    return groups;
  }, [allTables, tableSearch]);

  // Chart type groups
  const typeGroups = useMemo(() => {
    const groups = {};
    for (const t of DASHBOARD_CHART_TYPES) {
      if (!groups[t.group]) groups[t.group] = [];
      groups[t.group].push(t);
    }
    return groups;
  }, []);

  return (
    <div style={st.overlay} onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div style={st.panel}>
        {/* Header */}
        <div style={st.header}>
          <div style={st.headerLeft}>
            <h3 style={st.heading}>{isNew ? "Add Widget" : "Edit Widget"}</h3>
            <div style={st.modeToggle}>
              <button
                style={{ ...st.modeBtn, ...(mode === "visual" ? st.modeBtnActive : {}) }}
                onClick={() => setMode("visual")}
              >
                Visual
              </button>
              <button
                style={{ ...st.modeBtn, ...(mode === "sql" ? st.modeBtnActive : {}) }}
                onClick={() => {
                  if (mode === "visual") setSqlQuery(getFinalSQL());
                  setMode("sql");
                }}
              >
                SQL
              </button>
            </div>
          </div>
          <button style={st.closeBtn} onClick={onClose}>×</button>
        </div>

        <div style={st.body}>
          {/* Left: Config */}
          <div style={st.configPane}>
            {mode === "visual" ? (
              <VisualBuilder
                groupedTables={groupedTables}
                tableSearch={tableSearch}
                setTableSearch={setTableSearch}
                selectedTable={selectedTable}
                setSelectedTable={(t) => { setSelectedTable(t); setSelectedColumns([]); setAggregations({}); setVFilters([]); setOrderBy([]); }}
                tableColumns={tableColumns}
                selectedColumns={selectedColumns}
                toggleColumn={toggleColumn}
                selectAllColumns={selectAllColumns}
                selectNoColumns={selectNoColumns}
                quickSelectNumeric={quickSelectNumeric}
                aggregations={aggregations}
                setAggregations={setAggregations}
                dateGrouping={dateGrouping}
                setDateGrouping={setDateGrouping}
                vFilters={vFilters}
                setVFilters={setVFilters}
                orderBy={orderBy}
                setOrderBy={setOrderBy}
                rowLimit={rowLimit}
                setRowLimit={setRowLimit}
                sampleData={sampleData}
                applyPreset={applyPreset}
                widgetType={widgetType}
                setWidgetType={setWidgetType}
                chartType={chartType}
                setChartType={setChartType}
                typeGroups={typeGroups}
                suggestions={suggestions}
                title={title}
                setTitle={setTitle}
                widgetConfig={widgetConfig}
                setWidgetConfig={setWidgetConfig}
                cacheTtl={cacheTtl}
                setCacheTtl={setCacheTtl}
                previewData={previewData}
                calculatedFields={calculatedFields}
                setCalculatedFields={setCalculatedFields}
                columnDisplayNames={columnDisplayNames}
                setColumnDisplayNames={setColumnDisplayNames}
              />
            ) : (
              <SQLBuilder
                sqlQuery={sqlQuery}
                setSqlQuery={setSqlQuery}
                runPreview={() => runPreview()}
                previewing={previewing}
                widgetType={widgetType}
                setWidgetType={setWidgetType}
                chartType={chartType}
                setChartType={setChartType}
                typeGroups={typeGroups}
                suggestions={suggestions}
                title={title}
                setTitle={setTitle}
                widgetConfig={widgetConfig}
                setWidgetConfig={setWidgetConfig}
                cacheTtl={cacheTtl}
                setCacheTtl={setCacheTtl}
                previewData={previewData}
              />
            )}

            {/* Save bar */}
            <div style={st.saveBar}>
              <button style={st.cancelBtn} onClick={onClose}>Cancel</button>
              <button style={st.saveBtn} onClick={handleSave}>
                {isNew ? "Add to Dashboard" : "Save Changes"}
              </button>
            </div>
          </div>

          {/* Right: Live Preview */}
          <div style={st.previewPane}>
            <div style={st.previewHeader}>
              <span style={st.previewLabel}>Preview</span>
              {mode === "visual" && selectedTable && selectedColumns.length > 0 && (
                <button style={st.previewSqlBtn} onClick={() => { setSqlQuery(getFinalSQL()); setMode("sql"); }}>
                  View SQL
                </button>
              )}
            </div>
            <PreviewArea
              previewing={previewing}
              previewError={previewError}
              previewData={previewData}
              widgetType={widgetType}
              chartType={chartType}
              widgetConfig={widgetConfig}
              mode={mode}
              hasTable={!!selectedTable}
              hasColumns={selectedColumns.length > 0}
            />
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Visual Builder — fluid scrollable form (no accordion steps)
// ---------------------------------------------------------------------------

function VisualBuilder({
  groupedTables, tableSearch, setTableSearch,
  selectedTable, setSelectedTable, tableColumns, selectedColumns,
  toggleColumn, selectAllColumns, selectNoColumns, quickSelectNumeric,
  aggregations, setAggregations, dateGrouping, setDateGrouping,
  vFilters, setVFilters,
  orderBy, setOrderBy, rowLimit, setRowLimit, sampleData,
  applyPreset, widgetType, setWidgetType, chartType, setChartType,
  typeGroups, suggestions, title, setTitle, widgetConfig, setWidgetConfig,
  cacheTtl, setCacheTtl, previewData,
  calculatedFields, setCalculatedFields,
  columnDisplayNames, setColumnDisplayNames,
}) {
  return (
    <div style={st.builderScroll}>
      {/* Text widgets skip the entire data section */}
      {widgetType === "text" && (
        <div style={st.formSection}>
          <div style={st.formLabel}>Content</div>
          <textarea
            style={{ ...st.searchInput, minHeight: 120, resize: "vertical", fontFamily: "var(--havn-font-mono)" }}
            value={widgetConfig?.content || ""}
            onChange={(e) => setWidgetConfig({ ...widgetConfig, content: e.target.value })}
            placeholder={"# Title\n\nWrite **markdown** here...\n\n- Bullet points\n- *Italic*, **bold**, `code`"}
          />
        </div>
      )}

      {/* ---- Table picker (hidden for text widgets) ---- */}
      {widgetType !== "text" && <div style={st.formSection}>
        <div style={st.formLabel}>Table</div>
        <input
          style={st.searchInput}
          placeholder="Search tables..."
          value={tableSearch}
          onChange={(e) => setTableSearch(e.target.value)}
        />
        <div style={st.tableList}>
          {Object.entries(groupedTables).map(([schema, tables]) => (
            <div key={schema}>
              <div style={st.schemaLabel}>{schema}</div>
              {tables.map(t => (
                <button
                  key={`${t.schema}.${t.name}`}
                  style={{
                    ...st.tableItem,
                    ...(selectedTable?.name === t.name && selectedTable?.schema === t.schema ? st.tableItemActive : {}),
                  }}
                  onClick={() => setSelectedTable(t)}
                >
                  <span style={st.tableIcon}>{t.type === "view" ? "◇" : "▦"}</span>
                  {t.name}
                </button>
              ))}
            </div>
          ))}
          {Object.keys(groupedTables).length === 0 && (
            <div style={st.emptyMsg}>No tables found</div>
          )}
        </div>
      </div>}

      {/* ---- Columns (merged selection + configuration) ---- */}
      {widgetType !== "text" && selectedTable && (
        <div style={st.formSection}>
          <div style={st.formLabel}>Columns</div>

          {/* Quick presets */}
          {tableColumns.length > 0 && (
            <div style={st.presets}>
              <span style={st.presetsLabel}>Quick start:</span>
              {tableColumns.some(c => !isNumeric(c.type) && !isTemporal(c.type)) && tableColumns.some(c => isNumeric(c.type)) && (
                <button style={st.presetBtn} onClick={() => applyPreset("sum_by")}>Sum by category</button>
              )}
              {tableColumns.some(c => isTemporal(c.type)) && tableColumns.some(c => isNumeric(c.type)) && (
                <button style={st.presetBtn} onClick={() => applyPreset("trend")}>Trend over time</button>
              )}
              {tableColumns.some(c => isNumeric(c.type)) && (
                <button style={st.presetBtn} onClick={() => applyPreset("top_10")}>Top 10</button>
              )}
              <button style={st.presetBtn} onClick={() => applyPreset("count_all")}>Count by group</button>
            </div>
          )}

          {/* Bulk actions */}
          <div style={st.bulkActions}>
            <button style={st.bulkBtn} onClick={selectAllColumns}>All</button>
            <button style={st.bulkBtn} onClick={selectNoColumns}>None</button>
            {tableColumns.some(c => isNumeric(c.type)) && (
              <button style={st.bulkBtn} onClick={quickSelectNumeric}>+ Numeric</button>
            )}
          </div>

          {/* Merged column list: checkbox + icon + name + inline dropdown */}
          <div style={st.columnList}>
            {tableColumns.map(col => {
              const checked = selectedColumns.some(c => c.name === col.name);
              const showAggDropdown = checked && isNumeric(col.type);
              const showDateDropdown = checked && isTemporal(col.type);
              const showTextAggDropdown = checked && !isNumeric(col.type) && !isTemporal(col.type);
              return (
                <div key={col.name} style={{ ...st.columnRow, ...(checked ? st.columnRowChecked : {}) }}>
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggleColumn(col)}
                    style={st.checkbox}
                  />
                  <span style={{ ...st.colTypeIcon, color: typeColor(col.type) }}>{typeIcon(col.type)}</span>
                  <span style={st.colName}>{col.name}</span>
                  {showDateDropdown && (
                    <select
                      style={st.inlineSelect}
                      value={dateGrouping[col.name] || ""}
                      onChange={(e) => setDateGrouping(prev => ({ ...prev, [col.name]: e.target.value }))}
                      onClick={(e) => e.stopPropagation()}
                    >
                      {DATE_GROUPING_OPTIONS.map(opt => (
                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                      ))}
                    </select>
                  )}
                  {showAggDropdown && (
                    <select
                      style={st.inlineSelect}
                      value={aggregations[col.name] || ""}
                      onChange={(e) => setAggregations(prev => ({ ...prev, [col.name]: e.target.value }))}
                      onClick={(e) => e.stopPropagation()}
                    >
                      {AGG_OPTIONS.map(opt => (
                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                      ))}
                    </select>
                  )}
                  {showTextAggDropdown && (
                    <select
                      style={st.inlineSelect}
                      value={aggregations[col.name] || ""}
                      onChange={(e) => setAggregations(prev => ({ ...prev, [col.name]: e.target.value }))}
                      onClick={(e) => e.stopPropagation()}
                    >
                      {AGG_OPTIONS
                        .filter(opt => opt.value === "" || opt.value === "COUNT" || opt.value === "COUNT_DISTINCT" || opt.value === "MIN" || opt.value === "MAX")
                        .map(opt => (
                          <option key={opt.value} value={opt.value}>{opt.label}</option>
                        ))}
                    </select>
                  )}
                  {checked && (
                    <input
                      style={{ ...st.inlineSelect, minWidth: 70, fontSize: 11, padding: "2px 4px" }}
                      value={columnDisplayNames[col.name] || ""}
                      onChange={(e) => setColumnDisplayNames(prev => {
                        const next = { ...prev };
                        if (e.target.value) next[col.name] = e.target.value;
                        else delete next[col.name];
                        return next;
                      })}
                      placeholder="Display name..."
                      onClick={(e) => e.stopPropagation()}
                      title="Rename column as it appears in charts"
                    />
                  )}
                  {!checked && <span style={st.colType}>{col.type}</span>}
                </div>
              );
            })}
          </div>

          {/* Group-by summary */}
          {(selectedColumns.some(c => aggregations[c.name]) || selectedColumns.some(c => dateGrouping[c.name])) && (
            <div style={st.aggNote}>
              Group by: {selectedColumns.filter(c => !aggregations[c.name]).map(c => {
                const dg = dateGrouping[c.name];
                if (dg) return `${c.name} (${dg})`;
                return c.name;
              }).join(", ") || "(none)"}
            </div>
          )}
        </div>
      )}

      {/* ---- Calculated Fields ---- */}
      {widgetType !== "text" && selectedTable && (
        <div style={st.formSection}>
          <div style={{ ...st.formLabel, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            Calculated Fields
            <button style={st.addSmallBtn} onClick={() => setCalculatedFields(prev => [...prev, { name: "", expression: "" }])}>
              + Add
            </button>
          </div>
          {calculatedFields.length === 0 && (
            <div style={st.dimHint}>No calculated fields</div>
          )}
          {calculatedFields.map((cf, ci) => (
            <div key={ci} style={{ ...st.filterRow, flexWrap: "wrap" }}>
              <input
                style={{ ...st.filterValueInput, flex: "0 0 120px" }}
                value={cf.name}
                placeholder="Field name"
                onChange={(e) => setCalculatedFields(prev => prev.map((x, i) => i === ci ? { ...x, name: e.target.value } : x))}
              />
              <input
                style={{ ...st.filterValueInput, flex: 1 }}
                value={cf.expression}
                placeholder="price * quantity"
                onChange={(e) => setCalculatedFields(prev => prev.map((x, i) => i === ci ? { ...x, expression: e.target.value } : x))}
              />
              <button style={st.removeBtn} onClick={() => setCalculatedFields(prev => prev.filter((_, i) => i !== ci))}>×</button>
            </div>
          ))}
        </div>
      )}

      {/* ---- Filters ---- */}
      {widgetType !== "text" && selectedTable && (
        <div style={st.formSection}>
          <div style={{ ...st.formLabel, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            Filters
            <button style={st.addSmallBtn} onClick={() => setVFilters(prev => [...prev, { column: "", op: "=", value: "" }])}>
              + Add
            </button>
          </div>
          {vFilters.length === 0 && (
            <div style={st.dimHint}>No filters applied</div>
          )}
          {vFilters.map((f, fi) => (
            <div key={fi} style={st.filterRow}>
              <select
                style={st.filterSelect}
                value={f.column}
                onChange={(e) => setVFilters(prev => prev.map((x, i) => i === fi ? { ...x, column: e.target.value } : x))}
              >
                <option value="">Column...</option>
                {tableColumns.map(c => <option key={c.name} value={c.name}>{c.name}</option>)}
              </select>
              <select
                style={st.filterOpSelect}
                value={f.op}
                onChange={(e) => setVFilters(prev => prev.map((x, i) => i === fi ? { ...x, op: e.target.value } : x))}
              >
                {FILTER_OPS.map(op => <option key={op.value} value={op.value}>{op.label}</option>)}
              </select>
              {f.op !== "IS NULL" && f.op !== "IS NOT NULL" && (
                <input
                  style={st.filterValueInput}
                  value={f.value || ""}
                  placeholder="value"
                  onChange={(e) => setVFilters(prev => prev.map((x, i) => i === fi ? { ...x, value: e.target.value } : x))}
                />
              )}
              <button style={st.removeBtn} onClick={() => setVFilters(prev => prev.filter((_, i) => i !== fi))}>×</button>
            </div>
          ))}
        </div>
      )}

      {/* ---- Sort + Limit ---- */}
      {widgetType !== "text" && selectedTable && (
        <div style={st.formSection}>
          <div style={{ ...st.formLabel, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            Sort
            <button style={st.addSmallBtn} onClick={() => setOrderBy(prev => [...prev, { column: "", dir: "ASC" }])}>
              + Add
            </button>
          </div>
          {orderBy.map((o, oi) => (
            <div key={oi} style={st.filterRow}>
              <select
                style={st.filterSelect}
                value={o.column}
                onChange={(e) => setOrderBy(prev => prev.map((x, i) => i === oi ? { ...x, column: e.target.value } : x))}
              >
                <option value="">Column...</option>
                {selectedColumns.map(c => <option key={c.name} value={c.name}>{c.name}</option>)}
              </select>
              <select
                style={st.filterOpSelect}
                value={o.dir}
                onChange={(e) => setOrderBy(prev => prev.map((x, i) => i === oi ? { ...x, dir: e.target.value } : x))}
              >
                <option value="ASC">Ascending</option>
                <option value="DESC">Descending</option>
              </select>
              <button style={st.removeBtn} onClick={() => setOrderBy(prev => prev.filter((_, i) => i !== oi))}>×</button>
            </div>
          ))}

          <div style={{ ...st.formLabel, marginTop: 10 }}>Limit</div>
          <div style={st.limitRow}>
            {[10, 25, 50, 100, 500, 1000].map(n => (
              <button
                key={n}
                style={{ ...st.limitBtn, ...(rowLimit === n ? st.limitBtnActive : {}) }}
                onClick={() => setRowLimit(n)}
              >
                {n}
              </button>
            ))}
            <button
              style={{ ...st.limitBtn, ...(rowLimit === 0 ? st.limitBtnActive : {}) }}
              onClick={() => setRowLimit(0)}
            >
              All
            </button>
          </div>
        </div>
      )}

      {/* ---- Visualization (chart type + config) ---- */}
      {selectedTable && (
        <div style={st.formSection}>
          <ChartAndTypeConfig
            widgetType={widgetType}
            setWidgetType={setWidgetType}
            chartType={chartType}
            setChartType={setChartType}
            typeGroups={typeGroups}
            suggestions={suggestions}
            title={title}
            setTitle={setTitle}
            widgetConfig={widgetConfig}
            setWidgetConfig={setWidgetConfig}
            cacheTtl={cacheTtl}
            setCacheTtl={setCacheTtl}
            previewData={previewData}
          />
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// SQL Builder (advanced mode)
// ---------------------------------------------------------------------------

function SQLBuilder({
  sqlQuery, setSqlQuery, runPreview, previewing,
  widgetType, setWidgetType, chartType, setChartType,
  typeGroups, suggestions, title, setTitle,
  widgetConfig, setWidgetConfig, cacheTtl, setCacheTtl,
  previewData,
}) {
  return (
    <div style={st.builderScroll}>
      <div style={st.section}>
        <div style={st.sectionTitle2}>SQL Query</div>
        <textarea
          style={st.textarea}
          value={sqlQuery}
          onChange={(e) => setSqlQuery(e.target.value)}
          placeholder={"SELECT category, SUM(revenue) AS total\nFROM gold.orders\nGROUP BY category\nORDER BY total DESC\nLIMIT 20"}
          rows={8}
          onKeyDown={(e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
              e.preventDefault();
              runPreview();
            }
          }}
        />
        <button style={st.runPreviewBtn} onClick={runPreview} disabled={previewing}>
          {previewing ? "Running..." : "Run Preview"} <span style={{ fontSize: 10, opacity: 0.5 }}>Ctrl+Enter</span>
        </button>
      </div>

      <ChartAndTypeConfig
        widgetType={widgetType}
        setWidgetType={setWidgetType}
        chartType={chartType}
        setChartType={setChartType}
        typeGroups={typeGroups}
        suggestions={suggestions}
        title={title}
        setTitle={setTitle}
        widgetConfig={widgetConfig}
        setWidgetConfig={setWidgetConfig}
        cacheTtl={cacheTtl}
        setCacheTtl={setCacheTtl}
        previewData={previewData}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Chart Style Panel (collapsible sections)
// ---------------------------------------------------------------------------

function ChartStylePanel({ widgetConfig, setWidgetConfig }) {
  const [openSections, setOpenSections] = useState({});
  const toggle = (key) => setOpenSections(prev => ({ ...prev, [key]: !prev[key] }));
  const upd = (patch) => setWidgetConfig({ ...widgetConfig, ...patch });

  const sectionHeader = (key, label) => (
    <div
      onClick={() => toggle(key)}
      style={{ ...st.sectionTitle2, cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "space-between", paddingRight: 16, userSelect: "none" }}
    >
      <span>{label}</span>
      <span style={{ fontSize: 10, opacity: 0.6 }}>{openSections[key] ? "\u25B2" : "\u25BC"}</span>
    </div>
  );

  const refLines = widgetConfig.referenceLines || [];

  return (
    <div style={st.section}>
      {/* Palette */}
      {sectionHeader("palette", "Palette")}
      {openSections.palette && (
        <div style={{ padding: "4px 16px 10px" }}>
          <select
            style={{ ...st.select, margin: 0, width: "100%" }}
            value={widgetConfig.palette || "default"}
            onChange={(e) => upd({ palette: e.target.value })}
          >
            <option value="default">Default</option>
            <option value="colorblind">Colorblind</option>
            <option value="categorical">Categorical</option>
            <option value="sequential">Sequential</option>
            <option value="diverging">Diverging</option>
          </select>
        </div>
      )}

      {/* Axis configuration */}
      {sectionHeader("axis", "Axis Configuration")}
      {openSections.axis && (
        <div style={{ padding: "4px 16px 10px", display: "flex", flexDirection: "column", gap: 6 }}>
          <div style={{ display: "flex", gap: 8 }}>
            <div style={{ flex: 1 }}>
              <label style={{ ...st.fieldLabel, marginLeft: 0 }}>Y-axis min</label>
              <input
                type="number"
                style={{ ...st.input, margin: 0, width: "100%", maxWidth: "none" }}
                value={widgetConfig.yMin ?? ""}
                onChange={(e) => upd({ yMin: e.target.value === "" ? undefined : Number(e.target.value) })}
                placeholder="Auto"
              />
            </div>
            <div style={{ flex: 1 }}>
              <label style={{ ...st.fieldLabel, marginLeft: 0 }}>Y-axis max</label>
              <input
                type="number"
                style={{ ...st.input, margin: 0, width: "100%", maxWidth: "none" }}
                value={widgetConfig.yMax ?? ""}
                onChange={(e) => upd({ yMax: e.target.value === "" ? undefined : Number(e.target.value) })}
                placeholder="Auto"
              />
            </div>
          </div>
          <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--havn-text)" }}>
            <input type="checkbox" checked={!!widgetConfig.logScale} onChange={(e) => upd({ logScale: e.target.checked })} style={{ accentColor: "var(--havn-accent)" }} />
            Log scale
          </label>
          <div>
            <label style={{ ...st.fieldLabel, marginLeft: 0, marginBottom: 3 }}>Label rotation</label>
            <div style={{ display: "flex", gap: 4 }}>
              {["auto", "0", "45", "90"].map(v => (
                <label key={v} style={{ display: "flex", alignItems: "center", gap: 3, fontSize: 12, color: "var(--havn-text)", cursor: "pointer" }}>
                  <input
                    type="radio"
                    name="labelRotation"
                    checked={(widgetConfig.labelRotation || "auto") === v}
                    onChange={() => upd({ labelRotation: v })}
                    style={{ accentColor: "var(--havn-accent)" }}
                  />
                  {v === "auto" ? "Auto" : v + "\u00B0"}
                </label>
              ))}
            </div>
          </div>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
            <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--havn-text)" }}>
              <input type="checkbox" checked={!!widgetConfig.hideXLabels} onChange={(e) => upd({ hideXLabels: e.target.checked })} style={{ accentColor: "var(--havn-accent)" }} />
              Hide X labels
            </label>
            <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--havn-text)" }}>
              <input type="checkbox" checked={!!widgetConfig.hideYLabels} onChange={(e) => upd({ hideYLabels: e.target.checked })} style={{ accentColor: "var(--havn-accent)" }} />
              Hide Y labels
            </label>
          </div>
          <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--havn-text)" }}>
            <input type="checkbox" checked={widgetConfig.showGridlines !== false} onChange={(e) => upd({ showGridlines: e.target.checked })} style={{ accentColor: "var(--havn-accent)" }} />
            Show gridlines
          </label>
          {widgetConfig.showGridlines !== false && (
            <div>
              <label style={{ ...st.fieldLabel, marginLeft: 0, marginBottom: 3 }}>Gridline style</label>
              <div style={{ display: "flex", gap: 4 }}>
                {["solid", "dashed", "dotted"].map(v => (
                  <label key={v} style={{ display: "flex", alignItems: "center", gap: 3, fontSize: 12, color: "var(--havn-text)", cursor: "pointer" }}>
                    <input
                      type="radio"
                      name="gridlineStyle"
                      checked={(widgetConfig.gridlineStyle || "dashed") === v}
                      onChange={() => upd({ gridlineStyle: v })}
                      style={{ accentColor: "var(--havn-accent)" }}
                    />
                    {v.charAt(0).toUpperCase() + v.slice(1)}
                  </label>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Data labels */}
      {sectionHeader("dataLabels", "Data Labels")}
      {openSections.dataLabels && (
        <div style={{ padding: "4px 16px 10px" }}>
          <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--havn-text)" }}>
            <input type="checkbox" checked={!!widgetConfig.showDataLabels} onChange={(e) => upd({ showDataLabels: e.target.checked })} style={{ accentColor: "var(--havn-accent)" }} />
            Show data labels
          </label>
        </div>
      )}

      {/* Reference lines */}
      {sectionHeader("refLines", "Reference Lines")}
      {openSections.refLines && (
        <div style={{ padding: "4px 16px 10px", display: "flex", flexDirection: "column", gap: 6 }}>
          {refLines.map((rl, idx) => (
            <div key={idx} style={{ display: "flex", alignItems: "center", gap: 4, flexWrap: "wrap" }}>
              <input
                type="number"
                style={{ ...st.input, margin: 0, width: 64, maxWidth: "none", flex: "none" }}
                value={rl.value ?? ""}
                placeholder="Value"
                onChange={(e) => {
                  const updated = [...refLines];
                  updated[idx] = { ...rl, value: e.target.value === "" ? undefined : Number(e.target.value) };
                  upd({ referenceLines: updated });
                }}
              />
              <input
                style={{ ...st.input, margin: 0, width: 80, maxWidth: "none", flex: 1 }}
                value={rl.label || ""}
                placeholder="Label"
                onChange={(e) => {
                  const updated = [...refLines];
                  updated[idx] = { ...rl, label: e.target.value };
                  upd({ referenceLines: updated });
                }}
              />
              <input
                type="color"
                value={rl.color || "#ef4444"}
                style={{ width: 28, height: 28, border: "1px solid var(--havn-border)", borderRadius: 4, padding: 1, cursor: "pointer", background: "none" }}
                onChange={(e) => {
                  const updated = [...refLines];
                  updated[idx] = { ...rl, color: e.target.value };
                  upd({ referenceLines: updated });
                }}
              />
              <select
                style={{ ...st.filterOpSelect, width: 72 }}
                value={rl.style || "dashed"}
                onChange={(e) => {
                  const updated = [...refLines];
                  updated[idx] = { ...rl, style: e.target.value };
                  upd({ referenceLines: updated });
                }}
              >
                <option value="solid">Solid</option>
                <option value="dashed">Dashed</option>
              </select>
              <button
                style={st.removeBtn}
                onClick={() => upd({ referenceLines: refLines.filter((_, j) => j !== idx) })}
                title="Remove"
              >&times;</button>
            </div>
          ))}
          <button
            style={st.addSmallBtn}
            onClick={() => upd({ referenceLines: [...refLines, { value: 0, label: "", color: "#ef4444", style: "dashed" }] })}
          >
            + Add reference line
          </button>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Shared: Chart type + widget type config
// ---------------------------------------------------------------------------

function ChartAndTypeConfig({
  widgetType, setWidgetType, chartType, setChartType,
  typeGroups, suggestions, title, setTitle,
  widgetConfig, setWidgetConfig, cacheTtl, setCacheTtl,
  previewData,
}) {
  return (
    <>
      {/* Title */}
      <div style={st.section}>
        <div style={st.sectionTitle2}>Title</div>
        <input style={st.input} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Widget title" />
      </div>

      {/* Widget type */}
      <div style={st.section}>
        <div style={st.sectionTitle2}>Display as</div>
        <div style={st.typeRow}>
          {[
            { id: "chart", label: "Chart", icon: "📊" },
            { id: "kpi", label: "KPI Card", icon: "🔢" },
            { id: "table", label: "Table", icon: "▦" },
            { id: "text", label: "Text", icon: "📝" },
          ].map(t => (
            <button
              key={t.id}
              style={{ ...st.typeBtn, ...(widgetType === t.id ? st.typeBtnActive : {}) }}
              onClick={() => setWidgetType(t.id)}
            >
              <span style={{ fontSize: 16 }}>{t.icon}</span>
              <span>{t.label}</span>
            </button>
          ))}
        </div>
      </div>

      {/* Chart type (only for chart widget) */}
      {widgetType === "chart" && (
        <div style={st.section}>
          <div style={st.sectionTitle2}>Chart type</div>
          {suggestions.length > 0 && (
            <div style={st.suggestions}>
              <span style={{ fontSize: 11, color: "var(--havn-text-secondary)" }}>Recommended: </span>
              {suggestions.map(s => (
                <button key={s} style={{ ...st.suggChip, ...(chartType === s ? st.suggChipActive : {}) }}
                  onClick={() => setChartType(s)}>
                  {s}
                </button>
              ))}
            </div>
          )}
          {Object.entries(typeGroups).map(([group, types]) => (
            <div key={group} style={{ marginBottom: 6 }}>
              <div style={st.groupLabel}>{group}</div>
              <div style={st.chartTypeGrid}>
                {types.map(t => (
                  <button
                    key={t.id}
                    style={{ ...st.chartTypeBtn, ...(chartType === t.id ? st.chartTypeBtnActive : {}) }}
                    onClick={() => setChartType(t.id)}
                    title={t.desc || t.label}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Axis labels (for chart types) */}
      {widgetType === "chart" && (
        <div style={st.section}>
          <div style={st.sectionTitle2}>Axis labels</div>
          <div style={{ display: "flex", gap: 8, padding: "0 16px" }}>
            <div style={{ flex: 1 }}>
              <label style={st.fieldLabel}>X-axis</label>
              <input
                style={{ ...st.input, margin: 0, width: "100%" }}
                value={widgetConfig?.xAxisLabel || ""}
                onChange={(e) => setWidgetConfig({ ...widgetConfig, xAxisLabel: e.target.value })}
                placeholder="Auto (column name)"
              />
            </div>
            <div style={{ flex: 1 }}>
              <label style={st.fieldLabel}>Y-axis</label>
              <input
                style={{ ...st.input, margin: 0, width: "100%" }}
                value={widgetConfig?.yAxisLabel || ""}
                onChange={(e) => setWidgetConfig({ ...widgetConfig, yAxisLabel: e.target.value })}
                placeholder="Auto (column name)"
              />
            </div>
          </div>
        </div>
      )}

      {/* Null value handling */}
      {(widgetType === "chart" || widgetType === "table") && (
        <div style={st.section}>
          <div style={st.sectionTitle2}>Null values</div>
          <select
            style={st.select}
            value={widgetConfig?.nullHandling || "gap"}
            onChange={(e) => setWidgetConfig({ ...widgetConfig, nullHandling: e.target.value })}
          >
            <option value="gap">Show gap</option>
            <option value="zero">Show as zero</option>
            <option value="na">Show as N/A</option>
          </select>
        </div>
      )}

      {/* Chart Style (collapsible) */}
      {widgetType === "chart" && (
        <ChartStylePanel widgetConfig={widgetConfig} setWidgetConfig={setWidgetConfig} />
      )}

      {/* KPI config */}
      {widgetType === "kpi" && previewData?.columns && (
        <div style={st.section}>
          <div style={st.sectionTitle2}>KPI Settings</div>
          <label style={st.fieldLabel}>Value column</label>
          <select style={st.select} value={widgetConfig?.value_column || ""} onChange={(e) => setWidgetConfig({ ...widgetConfig, value_column: e.target.value })}>
            <option value="">Auto (first)</option>
            {previewData.columns.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
          <label style={{ ...st.fieldLabel, marginTop: 8 }}>Compare to</label>
          <select style={st.select} value={widgetConfig?.comparison_column || ""} onChange={(e) => setWidgetConfig({ ...widgetConfig, comparison_column: e.target.value })}>
            <option value="">None</option>
            {previewData.columns.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <div style={{ flex: 1 }}>
              <label style={st.fieldLabel}>Prefix</label>
              <input style={st.input} value={widgetConfig?.prefix || ""} onChange={(e) => setWidgetConfig({ ...widgetConfig, prefix: e.target.value })} placeholder="$" />
            </div>
            <div style={{ flex: 1 }}>
              <label style={st.fieldLabel}>Suffix</label>
              <input style={st.input} value={widgetConfig?.suffix || ""} onChange={(e) => setWidgetConfig({ ...widgetConfig, suffix: e.target.value })} placeholder="%" />
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <div style={{ flex: 1 }}>
              <label style={st.fieldLabel}>Format</label>
              <select style={st.select} value={widgetConfig?.kpiValueFormat || ""} onChange={(e) => setWidgetConfig({ ...widgetConfig, kpiValueFormat: e.target.value || undefined })}>
                <option value="">Auto</option>
                <option value="plain">Plain</option>
                <option value="compact">Compact (K/M/B)</option>
                <option value="percent">Percentage</option>
                <option value="currency">Currency</option>
              </select>
            </div>
            <div style={{ flex: 1 }}>
              <label style={st.fieldLabel}>Decimals</label>
              <input type="number" min={0} max={4} style={st.input} value={widgetConfig?.kpiDecimals ?? ""} onChange={(e) => setWidgetConfig({ ...widgetConfig, kpiDecimals: e.target.value === "" ? undefined : Number(e.target.value) })} placeholder="Auto" />
            </div>
          </div>
          <div style={{ marginTop: 10 }}>
            <label style={st.fieldLabel}>Subtitle</label>
            <input style={st.input} value={widgetConfig?.kpiSubtitle || ""} onChange={(e) => setWidgetConfig({ ...widgetConfig, kpiSubtitle: e.target.value })} placeholder="e.g. vs last quarter" />
          </div>
          <div style={{ marginTop: 10 }}>
            <label style={st.fieldLabel}>Conditional colors</label>
            {(widgetConfig?.kpiConditionalRules || []).map((rule, idx) => (
              <div key={idx} style={{ display: "flex", alignItems: "center", gap: 4, marginTop: 4 }}>
                <select
                  style={{ ...st.filterOpSelect, width: 60 }}
                  value={rule.op || ">"}
                  onChange={(e) => {
                    const rules = [...(widgetConfig.kpiConditionalRules || [])];
                    rules[idx] = { ...rule, op: e.target.value };
                    setWidgetConfig({ ...widgetConfig, kpiConditionalRules: rules });
                  }}
                >
                  <option value=">">&gt;</option>
                  <option value="<">&lt;</option>
                  <option value=">=">&ge;</option>
                  <option value="<=">&le;</option>
                  <option value="=">=</option>
                </select>
                <input
                  type="number"
                  style={{ ...st.input, margin: 0, width: 64, maxWidth: "none", flex: "none" }}
                  value={rule.value ?? ""}
                  placeholder="Value"
                  onChange={(e) => {
                    const rules = [...(widgetConfig.kpiConditionalRules || [])];
                    rules[idx] = { ...rule, value: e.target.value === "" ? undefined : Number(e.target.value) };
                    setWidgetConfig({ ...widgetConfig, kpiConditionalRules: rules });
                  }}
                />
                <input
                  type="color"
                  value={rule.color || "#22c55e"}
                  style={{ width: 28, height: 28, border: "1px solid var(--havn-border)", borderRadius: 4, padding: 1, cursor: "pointer", background: "none" }}
                  onChange={(e) => {
                    const rules = [...(widgetConfig.kpiConditionalRules || [])];
                    rules[idx] = { ...rule, color: e.target.value };
                    setWidgetConfig({ ...widgetConfig, kpiConditionalRules: rules });
                  }}
                />
                <button
                  style={st.removeBtn}
                  onClick={() => {
                    const rules = (widgetConfig.kpiConditionalRules || []).filter((_, i) => i !== idx);
                    setWidgetConfig({ ...widgetConfig, kpiConditionalRules: rules });
                  }}
                  title="Remove"
                >&times;</button>
              </div>
            ))}
            <button
              style={{ ...st.addSmallBtn, marginTop: 6 }}
              onClick={() => setWidgetConfig({ ...widgetConfig, kpiConditionalRules: [...(widgetConfig?.kpiConditionalRules || []), { op: ">", value: 0, color: "#22c55e" }] })}
            >
              + Add rule
            </button>
          </div>
        </div>
      )}

      {/* Text content */}
      {widgetType === "text" && (
        <div style={st.section}>
          <div style={st.sectionTitle2}>Content</div>
          <textarea
            style={st.textarea}
            value={widgetConfig?.content || ""}
            onChange={(e) => setWidgetConfig({ ...widgetConfig, content: e.target.value })}
            placeholder="# Title\n\nWrite **markdown** here..."
            rows={6}
          />
        </div>
      )}

    </>
  );
}

// ---------------------------------------------------------------------------
// Preview Area
// ---------------------------------------------------------------------------

function PreviewArea({ previewing, previewError, previewData, widgetType, chartType, widgetConfig, mode, hasTable, hasColumns }) {
  if (previewing) return <div style={st.previewCenter}>Running query...</div>;
  if (previewError) return <div style={{ ...st.previewCenter, color: "var(--havn-red)" }}>{previewError}</div>;

  if (!previewData) {
    if (mode === "visual" && !hasTable) return <div style={st.previewCenter}><div style={st.previewEmpty}><span style={{ fontSize: 32 }}>👈</span><div>Pick a table to get started</div></div></div>;
    if (mode === "visual" && !hasColumns) return <div style={st.previewCenter}><div style={st.previewEmpty}><span style={{ fontSize: 32 }}>✓</span><div>Now select some columns</div></div></div>;
    if (mode === "sql") return <div style={st.previewCenter}><div style={st.previewEmpty}><span style={{ fontSize: 32 }}>⌨</span><div>Write a query and press Ctrl+Enter</div></div></div>;
    return <div style={st.previewCenter}>Loading...</div>;
  }

  const { columns, rows } = previewData;

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
      <div style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column" }}>
        {widgetType === "table" ? (
          <div style={{ flex: 1, overflow: "auto" }}>
            <SortableTable columns={columns} rows={rows} />
          </div>
        ) : widgetType === "text" ? (
          <div style={{ flex: 1, padding: 16, color: "var(--havn-text)" }}>
            {widgetConfig?.content || "No content"}
          </div>
        ) : widgetType === "kpi" ? (
          <div style={{ display: "flex", alignItems: "center", justifyContent: "center", flex: 1 }}>
            <KPIPreview columns={columns} rows={rows} config={widgetConfig} />
          </div>
        ) : (
          <div style={{ flex: 1, padding: 8, overflow: "hidden" }}>
            {["gauge", "treemap", "heatmap", "funnel", "waterfall", "histogram", "radar", "bubble", "sparkline", "progress", "bullet", "sankey"].includes(chartType) ? (
              <DashboardChart type={chartType} columns={columns} rows={rows} width={420} height={300} config={widgetConfig} />
            ) : (
              <ChartPanel columns={columns} rows={rows} forcedType={chartType} compact={true} />
            )}
          </div>
        )}
      </div>
      <div style={st.previewMeta}>
        {previewData.row_count ?? rows?.length ?? 0} rows, {columns?.length ?? 0} columns
        {previewData.truncated && <span style={{ color: "var(--havn-yellow)", marginLeft: 8 }}>(truncated)</span>}
      </div>
    </div>
  );
}

function KPIPreview({ columns, rows, config }) {
  if (!rows?.length) return <span style={{ opacity: 0.4 }}>No data</span>;
  const valCol = config?.value_column || columns[0];
  const idx = columns.indexOf(valCol);
  const value = idx >= 0 ? rows[0][idx] : rows[0][0];
  const n = Number(value);
  const prefix = config?.prefix || "";
  const suffix = config?.suffix || "";
  const displayValue = config?.kpiValueFormat
    ? formatNumber(value, config.kpiValueFormat, config.kpiDecimals ?? null, config.currency)
    : (isNaN(n) ? String(value) : fmtNum(n));

  // Conditional color rules
  let valueColor = "var(--havn-text)";
  if (config?.kpiConditionalRules?.length > 0 && !isNaN(n)) {
    for (const rule of config.kpiConditionalRules) {
      const rv = Number(rule.value);
      if (isNaN(rv)) continue;
      let match = false;
      switch (rule.op) {
        case ">": match = n > rv; break;
        case "<": match = n < rv; break;
        case ">=": match = n >= rv; break;
        case "<=": match = n <= rv; break;
        case "=": match = n === rv; break;
        default: break;
      }
      if (match) { valueColor = rule.color; break; }
    }
  }

  return (
    <div style={{ textAlign: "center" }}>
      <div style={{ fontSize: 48, fontWeight: 700, color: valueColor }}>
        {prefix}{displayValue}{suffix}
      </div>
      {config?.kpiSubtitle && (
        <div style={{ fontSize: 13, color: "var(--havn-text-secondary)", marginTop: 4 }}>
          {config.kpiSubtitle}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const st = {
  overlay: { position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 2000 },
  panel: { background: "var(--havn-bg)", border: "1px solid var(--havn-border)", borderRadius: 12, width: "92vw", maxWidth: 1100, height: "85vh", maxHeight: 760, display: "flex", flexDirection: "column", overflow: "hidden" },
  header: { display: "flex", alignItems: "center", justifyContent: "space-between", padding: "10px 20px", borderBottom: "1px solid var(--havn-border)", flexShrink: 0 },
  headerLeft: { display: "flex", alignItems: "center", gap: 16 },
  heading: { margin: 0, fontSize: 16, fontWeight: 600, color: "var(--havn-text)" },
  modeToggle: { display: "flex", border: "1px solid var(--havn-border)", borderRadius: 6, overflow: "hidden" },
  modeBtn: { background: "none", border: "none", color: "var(--havn-text-secondary)", cursor: "pointer", padding: "4px 14px", fontSize: 12, fontWeight: 500, outline: "none" },
  modeBtnActive: { background: "var(--havn-accent)", color: "#fff" },
  closeBtn: { background: "none", border: "none", color: "var(--havn-text-secondary)", fontSize: 22, cursor: "pointer", padding: "0 4px" },
  body: { display: "flex", flex: 1, overflow: "hidden" },
  configPane: { width: "42%", display: "flex", flexDirection: "column", borderRight: "1px solid var(--havn-border)" },
  builderScroll: { flex: 1, overflow: "auto", padding: "0" },
  previewPane: { flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" },
  previewHeader: { display: "flex", alignItems: "center", justifyContent: "space-between", padding: "8px 16px", borderBottom: "1px solid var(--havn-border)", flexShrink: 0 },
  previewLabel: { fontSize: 12, fontWeight: 600, color: "var(--havn-text-secondary)" },
  previewSqlBtn: { background: "none", border: "1px solid var(--havn-border)", color: "var(--havn-text-secondary)", borderRadius: 4, padding: "2px 8px", fontSize: 11, cursor: "pointer" },
  previewCenter: { display: "flex", alignItems: "center", justifyContent: "center", flex: 1, color: "var(--havn-text-secondary)", fontSize: 13 },
  previewEmpty: { textAlign: "center", display: "flex", flexDirection: "column", alignItems: "center", gap: 8, opacity: 0.6 },
  previewMeta: { padding: "6px 16px", fontSize: 11, color: "var(--havn-text-secondary)", borderTop: "1px solid var(--havn-border)", flexShrink: 0 },

  // Fluid form sections (replaces accordion sections)
  formSection: { padding: "10px 16px", borderBottom: "1px solid var(--havn-border)" },
  formLabel: { fontSize: 12, fontWeight: 600, color: "var(--havn-text-secondary)", marginBottom: 6, textTransform: "uppercase", letterSpacing: 0.3 },
  dimHint: { fontSize: 11, color: "var(--havn-text-secondary)", opacity: 0.6, padding: "2px 0" },

  // Legacy section styles (used by SQLBuilder and ChartAndTypeConfig)
  section: { borderBottom: "1px solid var(--havn-border)" },
  sectionTitle2: { fontSize: 12, fontWeight: 600, color: "var(--havn-text-secondary)", marginBottom: 6, padding: "10px 16px 0" },

  // Table picker
  searchInput: { width: "100%", padding: "7px 10px", border: "1px solid var(--havn-border)", borderRadius: 6, background: "var(--havn-bg-secondary, var(--havn-bg))", color: "var(--havn-text)", fontSize: 13, outline: "none", marginBottom: 6, boxSizing: "border-box" },
  tableList: { maxHeight: 160, overflow: "auto" },
  schemaLabel: { fontSize: 10, fontWeight: 700, color: "var(--havn-text-secondary)", textTransform: "uppercase", letterSpacing: 0.5, padding: "4px 0 2px", marginTop: 2 },
  tableItem: { display: "flex", alignItems: "center", gap: 6, width: "100%", background: "none", border: "none", color: "var(--havn-text)", cursor: "pointer", padding: "4px 8px", fontSize: 13, textAlign: "left", borderRadius: 4 },
  tableItemActive: { background: "var(--havn-accent)", color: "#fff" },
  tableIcon: { fontSize: 12, opacity: 0.5 },
  emptyMsg: { padding: 12, color: "var(--havn-text-secondary)", fontSize: 13, textAlign: "center" },

  // Column picker (merged with configuration)
  presets: { display: "flex", alignItems: "center", gap: 4, flexWrap: "wrap", marginBottom: 6 },
  presetsLabel: { fontSize: 11, color: "var(--havn-text-secondary)" },
  presetBtn: { background: "var(--havn-bg-secondary, var(--havn-bg))", border: "1px solid var(--havn-accent)", borderRadius: 12, padding: "3px 10px", fontSize: 11, color: "var(--havn-accent)", cursor: "pointer", whiteSpace: "nowrap", outline: "none" },
  bulkActions: { display: "flex", gap: 4, marginBottom: 6 },
  bulkBtn: { background: "none", border: "1px solid var(--havn-border)", borderRadius: 4, padding: "2px 8px", fontSize: 11, color: "var(--havn-text-secondary)", cursor: "pointer" },
  columnList: { maxHeight: 240, overflow: "auto", border: "1px solid var(--havn-border)", borderRadius: 6 },
  columnRow: { display: "flex", alignItems: "center", gap: 8, padding: "5px 10px", borderBottom: "1px solid var(--havn-border)" },
  columnRowChecked: { background: "rgba(99,102,241,0.08)" },
  checkbox: { accentColor: "var(--havn-accent)", margin: 0, cursor: "pointer", flexShrink: 0 },
  colTypeIcon: { fontSize: 11, fontWeight: 700, width: 18, textAlign: "center", flexShrink: 0 },
  colName: { fontSize: 13, color: "var(--havn-text)", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  colType: { fontSize: 10, color: "var(--havn-text-secondary)", flexShrink: 0 },
  inlineSelect: { padding: "2px 4px", border: "1px solid var(--havn-border)", borderRadius: 4, background: "var(--havn-bg-secondary, var(--havn-bg))", color: "var(--havn-text)", fontSize: 11, minWidth: 80, flexShrink: 0 },
  aggNote: { fontSize: 11, color: "var(--havn-text-secondary)", marginTop: 6, fontStyle: "italic" },

  // Configure section
  addSmallBtn: { background: "none", border: "1px solid var(--havn-border)", borderRadius: 4, padding: "2px 8px", fontSize: 11, color: "var(--havn-accent)", cursor: "pointer" },
  filterRow: { display: "flex", alignItems: "center", gap: 4, marginBottom: 4 },
  filterSelect: { padding: "3px 6px", border: "1px solid var(--havn-border)", borderRadius: 4, background: "var(--havn-bg-secondary, var(--havn-bg))", color: "var(--havn-text)", fontSize: 12, flex: 1, minWidth: 0 },
  filterOpSelect: { padding: "3px 6px", border: "1px solid var(--havn-border)", borderRadius: 4, background: "var(--havn-bg-secondary, var(--havn-bg))", color: "var(--havn-text)", fontSize: 12, width: 90 },
  filterValueInput: { padding: "3px 6px", border: "1px solid var(--havn-border)", borderRadius: 4, background: "var(--havn-bg-secondary, var(--havn-bg))", color: "var(--havn-text)", fontSize: 12, flex: 1, minWidth: 0 },
  removeBtn: { background: "none", border: "none", color: "var(--havn-red)", cursor: "pointer", fontSize: 16, padding: "0 4px", lineHeight: 1, flexShrink: 0 },
  limitRow: { display: "flex", gap: 4, flexWrap: "wrap" },
  limitBtn: { padding: "4px 10px", border: "1px solid var(--havn-border)", borderRadius: 4, background: "none", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: 12, outline: "none" },
  limitBtnActive: { background: "var(--havn-accent)", color: "#fff", borderColor: "var(--havn-accent)" },

  // Chart & type config
  input: { width: "100%", padding: "6px 10px", border: "1px solid var(--havn-border)", borderRadius: 5, background: "var(--havn-bg-secondary, var(--havn-bg))", color: "var(--havn-text)", fontSize: 13, outline: "none", boxSizing: "border-box", margin: "0 16px", maxWidth: "calc(100% - 32px)" },
  select: { width: "calc(100% - 32px)", margin: "0 16px", padding: "6px 10px", border: "1px solid var(--havn-border)", borderRadius: 5, background: "var(--havn-bg-secondary, var(--havn-bg))", color: "var(--havn-text)", fontSize: 13, outline: "none" },
  fieldLabel: { display: "block", fontSize: 11, fontWeight: 500, color: "var(--havn-text-secondary)", marginLeft: 16 },
  textarea: { width: "calc(100% - 32px)", margin: "0 16px", padding: "8px 10px", border: "1px solid var(--havn-border)", borderRadius: 5, background: "var(--havn-bg-secondary, var(--havn-bg))", color: "var(--havn-text)", fontSize: 13, fontFamily: "var(--havn-font-mono)", outline: "none", resize: "vertical", boxSizing: "border-box" },
  runPreviewBtn: { margin: "8px 16px", padding: "7px 16px", background: "var(--havn-accent)", color: "#fff", border: "none", borderRadius: 5, cursor: "pointer", fontSize: 12, fontWeight: 500 },
  typeRow: { display: "flex", gap: 6, padding: "0 16px" },
  typeBtn: { flex: 1, padding: "8px 0", border: "1px solid var(--havn-border)", borderRadius: 6, background: "none", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: 11, fontWeight: 500, display: "flex", flexDirection: "column", alignItems: "center", gap: 2, outline: "none" },
  typeBtnActive: { background: "var(--havn-accent)", color: "#fff", borderColor: "var(--havn-accent)" },
  suggestions: { display: "flex", alignItems: "center", gap: 4, padding: "0 16px", marginBottom: 8, flexWrap: "wrap" },
  suggChip: { padding: "2px 10px", border: "1px solid var(--havn-accent)", borderRadius: 12, background: "none", color: "var(--havn-accent)", cursor: "pointer", fontSize: 11, outline: "none" },
  suggChipActive: { background: "var(--havn-accent)", color: "#fff" },
  groupLabel: { fontSize: 10, fontWeight: 600, color: "var(--havn-text-secondary)", textTransform: "uppercase", letterSpacing: 0.5, padding: "0 16px", marginBottom: 3 },
  chartTypeGrid: { display: "flex", flexWrap: "wrap", gap: 4, padding: "0 16px" },
  chartTypeBtn: { padding: "4px 8px", border: "1px solid var(--havn-border)", borderRadius: 4, background: "none", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: 11, outline: "none" },
  chartTypeBtnActive: { background: "var(--havn-accent)", color: "#fff", borderColor: "var(--havn-accent)" },

  // Save bar
  saveBar: { display: "flex", gap: 8, justifyContent: "flex-end", padding: "10px 16px", borderTop: "1px solid var(--havn-border)", flexShrink: 0 },
  cancelBtn: { background: "none", border: "1px solid var(--havn-border)", color: "var(--havn-text-secondary)", borderRadius: 6, padding: "8px 18px", cursor: "pointer", fontSize: 13 },
  saveBtn: { background: "var(--havn-accent)", color: "#fff", border: "none", borderRadius: 6, padding: "8px 18px", cursor: "pointer", fontSize: 13, fontWeight: 600 },
};
