import React, { useState, useRef, useEffect, useMemo } from "react";
import { useDashboard } from "./DashboardContext";
import { api } from "./api";
import SortableTable from "./SortableTable";
import ChartPanel from "./ChartPanel";
import DashboardChart from "./DashboardCharts";
import { formatNumber, autoContrast } from "./chartStyleDefaults";

const DASHBOARD_CHART_TYPES = new Set([
  "gauge", "treemap", "heatmap", "funnel", "waterfall", "histogram",
  "radar", "bubble", "sparkline", "progress", "bullet", "sankey",
  "stacked_area", "boxplot", "combo",
]);

// Responsive wrapper for DashboardCharts (uses ResizeObserver)
function ResponsiveChart({ type, columns, rows, config }) {
  const ref = useRef(null);
  const [dims, setDims] = useState({ width: 300, height: 200 });
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(entries => {
      const { width, height } = entries[0].contentRect;
      if (width > 10 && height > 10) setDims({ width: Math.floor(width), height: Math.floor(height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return (
    <div ref={ref} style={{ flex: 1, minHeight: 0, overflow: "hidden" }}>
      <DashboardChart type={type} columns={columns} rows={rows}
        width={dims.width} height={dims.height} config={config} />
    </div>
  );
}

function exportWidgetCSV(widget, data) {
  if (!data?.columns || !data?.rows) return;
  const header = data.columns.join(",");
  const body = data.rows.map(r => r.map(v => {
    const s = String(v ?? "");
    return s.includes(",") || s.includes('"') || s.includes("\n") ? `"${s.replace(/"/g, '""')}"` : s;
  }).join(",")).join("\n");
  const blob = new Blob([header + "\n" + body], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${widget.title || "data"}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

function timeAgo(dateStr) {
  if (!dateStr) return "";
  const now = new Date();
  const d = new Date(dateStr);
  const seconds = Math.floor((now - d) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

// ---------------------------------------------------------------------------
// Markdown renderer (minimal — headings, bold, italic, code, links, lists)
// ---------------------------------------------------------------------------

function renderMarkdown(text) {
  if (!text) return null;
  const lines = text.split("\n");
  const elements = [];
  let inList = false;
  let listItems = [];

  function flushList() {
    if (listItems.length > 0) {
      elements.push(<ul key={`list-${elements.length}`} style={{ margin: "4px 0", paddingLeft: 20 }}>{listItems}</ul>);
      listItems = [];
      inList = false;
    }
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    // Heading
    const hm = line.match(/^(#{1,3})\s+(.*)$/);
    if (hm) {
      flushList();
      const level = hm[1].length;
      const Tag = `h${level}`;
      elements.push(<Tag key={i} style={{ margin: "8px 0 4px", fontSize: level === 1 ? "1.3em" : level === 2 ? "1.1em" : "1em" }}>{inlineFormat(hm[2])}</Tag>);
      continue;
    }
    // List item
    const li = line.match(/^[-*]\s+(.*)$/);
    if (li) {
      inList = true;
      listItems.push(<li key={i}>{inlineFormat(li[1])}</li>);
      continue;
    }
    flushList();
    if (line.trim() === "") {
      elements.push(<br key={i} />);
    } else {
      elements.push(<p key={i} style={{ margin: "4px 0" }}>{inlineFormat(line)}</p>);
    }
  }
  flushList();
  return elements;
}

function inlineFormat(text) {
  // Bold, italic, code, links
  const parts = [];
  let remaining = text;
  let key = 0;
  const re = /(\*\*(.+?)\*\*)|(\*(.+?)\*)|(`(.+?)`)|(\[(.+?)\]\((.+?)\))/g;
  let lastIndex = 0;
  let match;
  while ((match = re.exec(remaining)) !== null) {
    if (match.index > lastIndex) {
      parts.push(remaining.slice(lastIndex, match.index));
    }
    if (match[2]) parts.push(<strong key={key++}>{match[2]}</strong>);
    else if (match[4]) parts.push(<em key={key++}>{match[4]}</em>);
    else if (match[6]) parts.push(<code key={key++} style={{ background: "var(--havn-bg-secondary)", padding: "1px 4px", borderRadius: 3, fontSize: "0.9em" }}>{match[6]}</code>);
    else if (match[8]) parts.push(<a key={key++} href={match[9]} target="_blank" rel="noopener noreferrer" style={{ color: "var(--havn-accent)" }}>{match[8]}</a>);
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < remaining.length) parts.push(remaining.slice(lastIndex));
  return parts.length > 0 ? parts : text;
}

// ---------------------------------------------------------------------------
// Widget Component
// ---------------------------------------------------------------------------

export default function DashboardWidget({
  widget,
  onEdit,
  onDuplicate,
  onDelete,
  onMoveStart,
  onTitleChange,
  style,
}) {
  const { editMode, widgetData, refreshWidget, setCrossFilter, updateWidget, dashboard, showToast } = useDashboard();
  const [showMenu, setShowMenu] = useState(false);
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleValue, setTitleValue] = useState(widget.title || "");
  const [isWidgetFullscreen, setIsWidgetFullscreen] = useState(false);
  const [drillState, setDrillState] = useState(null); // { column, value, level }
  const [drillData, setDrillData] = useState(null);
  const menuRef = useRef(null);
  const rawData = widgetData[widget.id];
  const data = drillState && drillData ? drillData : rawData;

  // Close widget fullscreen on Escape
  useEffect(() => {
    if (!isWidgetFullscreen) return;
    const handler = (e) => {
      if (e.key === "Escape") setIsWidgetFullscreen(false);
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [isWidgetFullscreen]);

  // Close menu on outside click
  useEffect(() => {
    if (!showMenu) return;
    const handler = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) setShowMenu(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showMenu]);

  // Refresh on mount if no data
  useEffect(() => {
    if (widget.sql_query && !data) {
      refreshWidget(widget.id);
    }
  }, [widget.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const isLoading = data?.loading;
  const hasError = data?.error;
  const hasData = data && data.columns && data.rows;

  const handleChartClick = (column, value) => {
    if (column && value !== undefined) {
      if (widget.config?.drill_down?.enabled) {
        // Enter drill-down mode
        setDrillState({ column, value, level: 0 });
        // Fetch drilled-down data
        if (dashboard) {
          api.queryWidget(dashboard.id, widget.id, { [column]: value }).then(result => {
            setDrillData({ ...result, loading: false, error: result.error || null, _fetchedAt: new Date().toISOString(), _queryDuration: rawData?._queryDuration });
          }).catch(() => {});
        }
      } else {
        setCrossFilter(widget.id, column, value);
      }
    }
  };

  const resetDrill = () => {
    setDrillState(null);
    setDrillData(null);
  };

  const widgetBg = widget.config?.bgColor;
  const widgetTextColor = widgetBg ? autoContrast(widgetBg) : undefined;

  return (
    <div
      style={{
        ...st.container,
        ...style,
        border: hasError ? "1px solid var(--havn-red)" : "1px solid var(--havn-border)",
        background: widgetBg || "var(--havn-bg-secondary, var(--havn-bg))",
        ...(widgetTextColor ? { color: widgetTextColor } : {}),
      }}
    >
      {/* Title bar (hidden for dividers) */}
      {widget.widget_type !== "divider" && <div style={st.titleBar}>
        <div
          style={{ ...st.dragHandle, ...(editMode ? { cursor: "grab" } : {}) }}
          onPointerDown={editMode && !editingTitle ? onMoveStart : undefined}
          title={editMode && !editingTitle ? "Drag to move" : undefined}
        >
          {editMode && !editingTitle && <span style={st.gripIcon}>⠿</span>}
          {editingTitle ? (
            <input
              style={st.titleInput}
              value={titleValue}
              onChange={(e) => setTitleValue(e.target.value)}
              onBlur={() => {
                setEditingTitle(false);
                if (titleValue.trim().length > 0 && titleValue.trim() !== (widget.title || "")) {
                  updateWidget(widget.id, { title: titleValue.trim() });
                } else {
                  setTitleValue(widget.title || "");
                }
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") e.target.blur();
                if (e.key === "Escape") { setTitleValue(widget.title || ""); setEditingTitle(false); }
              }}
              autoFocus
              onClick={(e) => e.stopPropagation()}
              onPointerDown={(e) => e.stopPropagation()}
            />
          ) : (
            <span
              style={st.title}
              onDoubleClick={editMode ? () => { setTitleValue(widget.title || ""); setEditingTitle(true); } : undefined}
              title={widget.title || widget.widget_type}
            >
              {widget.title || widget.widget_type}
            </span>
          )}
          {widget.config?.locked && <span title="Locked" style={{ fontSize: 12, opacity: 0.5, marginLeft: 4 }}>&#x1F512;</span>}
        </div>
        <div style={st.actions}>
          {isLoading && <span style={st.spinner}>↻</span>}
          <button
            style={st.menuBtn}
            onClick={() => setIsWidgetFullscreen(true)}
            title="Expand to fullscreen (Escape to close)"
            onMouseEnter={(e) => { e.currentTarget.style.background = "var(--havn-bg)"; e.currentTarget.style.color = "var(--havn-text)"; }}
            onMouseLeave={(e) => { e.currentTarget.style.background = "none"; e.currentTarget.style.color = "var(--havn-text-secondary)"; }}
          >
            ↗
          </button>
          {editMode && (
            <button
              style={st.menuBtn}
              onClick={() => setShowMenu(!showMenu)}
              title="Widget menu — Edit, Refresh, Duplicate, Export, Delete"
              onMouseEnter={(e) => { e.currentTarget.style.background = "var(--havn-bg)"; e.currentTarget.style.color = "var(--havn-text)"; }}
              onMouseLeave={(e) => { e.currentTarget.style.background = "none"; e.currentTarget.style.color = "var(--havn-text-secondary)"; }}
            >
              ⋮
            </button>
          )}
          {showMenu && (
            <div ref={menuRef} style={st.menu}>
              <button style={st.menuItem} onClick={() => { setShowMenu(false); onEdit?.(widget); }}
                onMouseEnter={(e) => e.currentTarget.style.background = "var(--havn-bg-secondary, rgba(128,128,128,0.1))"}
                onMouseLeave={(e) => e.currentTarget.style.background = "none"}>
                Edit
              </button>
              <button style={st.menuItem} onClick={() => { setShowMenu(false); refreshWidget(widget.id); }}
                onMouseEnter={(e) => e.currentTarget.style.background = "var(--havn-bg-secondary, rgba(128,128,128,0.1))"}
                onMouseLeave={(e) => e.currentTarget.style.background = "none"}>
                Refresh
              </button>
              <button style={st.menuItem} onClick={() => { setShowMenu(false); onDuplicate?.(widget); }}
                onMouseEnter={(e) => e.currentTarget.style.background = "var(--havn-bg-secondary, rgba(128,128,128,0.1))"}
                onMouseLeave={(e) => e.currentTarget.style.background = "none"}>
                Duplicate
              </button>
              <button style={st.menuItem} onClick={() => { setShowMenu(false); updateWidget(widget.id, { config: { ...widget.config, locked: !widget.config?.locked } }); }}
                onMouseEnter={(e) => e.currentTarget.style.background = "var(--havn-bg-secondary, rgba(128,128,128,0.1))"}
                onMouseLeave={(e) => e.currentTarget.style.background = "none"}>
                {widget.config?.locked ? "Unlock" : "Lock"}
              </button>
              {hasData && (
                <button style={st.menuItem} onClick={() => { setShowMenu(false); exportWidgetCSV(widget, data); showToast?.("CSV exported", "success"); }}
                  onMouseEnter={(e) => e.currentTarget.style.background = "var(--havn-bg-secondary, rgba(128,128,128,0.1))"}
                  onMouseLeave={(e) => e.currentTarget.style.background = "none"}>
                  Export CSV
                </button>
              )}
              <button style={{ ...st.menuItem, color: "var(--havn-red)" }} onClick={() => { setShowMenu(false); onDelete?.(widget); }}>
                Delete
              </button>
            </div>
          )}
        </div>
      </div>}

      {/* Drill-down breadcrumb */}
      {drillState && (
        <div style={st.drillBreadcrumb}>
          <button style={st.drillBreadcrumbBtn} onClick={resetDrill}>All</button>
          <span style={{ color: "var(--havn-text-secondary)", fontSize: 11 }}>&rsaquo;</span>
          <span style={{ fontSize: 11, color: "var(--havn-text)", fontWeight: 500 }}>{String(drillState.value)}</span>
        </div>
      )}

      {/* Content */}
      <div style={st.content}>
        {isLoading && !hasData && (
          <div style={st.skeleton}>
            <div style={st.skelBar} /><div style={{ ...st.skelBar, width: "60%" }} /><div style={{ ...st.skelBar, width: "80%" }} />
          </div>
        )}

        {hasError && !isLoading && (
          <div style={st.center}>
            <div style={{ color: "var(--havn-red)", fontSize: 13 }}>{data.error}</div>
            <button
              style={st.retryBtn}
              onClick={() => refreshWidget(widget.id)}
            >
              Retry
            </button>
          </div>
        )}

        {!isLoading && !hasError && !hasData && widget.widget_type === "text" && (
          <div style={{ padding: 12, fontSize: 14, color: "var(--havn-text)", overflow: "auto", flex: 1 }}>
            {renderMarkdown(widget.config?.content || widget.sql_query || "*No content*")}
          </div>
        )}

        {!isLoading && !hasError && !hasData && widget.widget_type !== "text" && !widget.sql_query && (
          <div style={st.center}>
            {editMode ? (
              <>
                <span style={{ opacity: 0.5, fontSize: 22 }}>+</span>
                <span style={{ opacity: 0.5, fontSize: 13 }}>No query configured</span>
                <span style={{ opacity: 0.35, fontSize: 11 }}>Click ⋮ &gt; Edit to set up this widget</span>
              </>
            ) : (
              <span style={{ opacity: 0.25, fontSize: 12, fontStyle: "italic" }}>Not configured</span>
            )}
          </div>
        )}

        {hasData && !hasError && data.rows.length === 0 && (
          <div style={st.center}>
            <span style={{ opacity: 0.4, fontSize: 13 }}>{widget.config?.emptyStateMessage || "No data available"}</span>
          </div>
        )}
        {hasData && !hasError && data.rows.length > 0 && renderWidgetContent(widget, data, handleChartClick)}
      </div>

      {/* Footer — row count + freshness + slow query warning */}
      {hasData && data.row_count > 0 && (widget.position?.h || 4) >= 3 && (
        <div style={st.footer}>
          <span>{data.row_count} row{data.row_count !== 1 ? "s" : ""}</span>
          {data._fetchedAt && <span> · {timeAgo(data._fetchedAt)}</span>}
          {data._queryDuration > 2000 && (
            <span style={{ marginLeft: "auto", color: "var(--havn-yellow)", fontWeight: 500 }} title={`Query took ${(data._queryDuration / 1000).toFixed(1)}s`}>
              {"\u26A0"} {(data._queryDuration / 1000).toFixed(1)}s
            </span>
          )}
        </div>
      )}

      {/* Resize handle (edit mode only, hidden for locked widgets) */}
      {editMode && !widget.config?.locked && (
        <div
          className="dashboard-resize-handle"
          style={st.resizeHandle}
          title="Drag to resize"
        >
          ⌟
        </div>
      )}

      {/* Per-widget fullscreen overlay */}
      {isWidgetFullscreen && (
        <div style={st.widgetFullscreenOverlay}>
          <div style={st.widgetFullscreenHeader}>
            <span style={{ fontSize: 16, fontWeight: 600, color: "var(--havn-text)" }}>{widget.title || widget.widget_type}</span>
            <button
              style={st.widgetFullscreenClose}
              onClick={() => setIsWidgetFullscreen(false)}
              title="Close (Escape)"
            >
              ×
            </button>
          </div>
          <div style={st.widgetFullscreenContent}>
            {hasData && !hasError && data.rows.length === 0 && (
              <div style={st.center}>
                <span style={{ opacity: 0.4, fontSize: 14 }}>{widget.config?.emptyStateMessage || "No data available"}</span>
              </div>
            )}
            {hasData && !hasError && data.rows.length > 0 && renderWidgetContent(widget, data, handleChartClick)}
            {isLoading && !hasData && (
              <div style={st.center}><span style={st.spinner}>↻</span> Loading...</div>
            )}
            {hasError && !isLoading && (
              <div style={st.center}>
                <div style={{ color: "var(--havn-red)", fontSize: 14 }}>{data.error}</div>
              </div>
            )}
            {!isLoading && !hasError && !hasData && widget.widget_type === "text" && (
              <div style={{ padding: 24, fontSize: 16, color: "var(--havn-text)", overflow: "auto", flex: 1 }}>
                {renderMarkdown(widget.config?.content || widget.sql_query || "*No content*")}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function PaginatedTable({ columns, rows, conditionalRules, showSummary }) {
  const [page, setPage] = useState(0);
  const pageSize = 50;
  const totalPages = Math.ceil(rows.length / pageSize);
  const pagedRows = rows.slice(page * pageSize, (page + 1) * pageSize);

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, overflow: "hidden" }}>
      <div style={{ flex: 1, overflow: "auto" }}>
        <SortableTable columns={columns} rows={pagedRows} conditionalRules={conditionalRules} showSummary={showSummary} />
      </div>
      {totalPages > 1 && (
        <div style={st.pagination}>
          <button style={st.pageBtn} disabled={page === 0} onClick={() => setPage(p => p - 1)}>‹</button>
          <span style={st.pageInfo}>{page + 1} / {totalPages}</span>
          <button style={st.pageBtn} disabled={page >= totalPages - 1} onClick={() => setPage(p => p + 1)}>›</button>
          <span style={st.pageCount}>{rows.length} rows</span>
        </div>
      )}
    </div>
  );
}

function applyNullHandling(rows, nullHandling) {
  if (!nullHandling || nullHandling === "gap") return rows;
  return rows.map(row => row.map(v => {
    if (v === null || v === undefined) {
      if (nullHandling === "zero") return 0;
      if (nullHandling === "na") return "N/A";
    }
    return v;
  }));
}

function applyDisplayNames(columns, displayNames) {
  if (!displayNames || Object.keys(displayNames).length === 0) return columns;
  return columns.map(c => displayNames[c] || c);
}

/**
 * Pivot data by a color column to create multi-series charts.
 * Given columns [date, region, count] with color="region":
 *   → pivoted columns: [date, "North America", "Europe", "Asia", ...]
 *   → each row: [date_value, count_for_NA, count_for_EU, count_for_AS, ...]
 */
function findColIdx(columns, name) {
  if (!name) return -1;
  let idx = columns.indexOf(name);
  if (idx !== -1) return idx;
  // Match aliased columns like event_date_month for event_date
  idx = columns.findIndex(c => c === name || c.startsWith(name + "_"));
  return idx;
}

function pivotByColor(columns, rows, encoding) {
  if (!encoding?.color) return null;
  const colorIdx = findColIdx(columns, encoding.color);
  if (colorIdx === -1) return null;

  // Determine X and Y columns from encoding or auto-detect
  const xIdx = encoding.x ? findColIdx(columns, encoding.x) : columns.findIndex((_, i) => i !== colorIdx);
  const yIdx = encoding.y ? findColIdx(columns, encoding.y) : columns.findIndex((_, i) => i !== colorIdx && i !== xIdx);
  if (xIdx === -1 || yIdx === -1) return null;

  // Collect unique color values and build pivot map
  const colorValues = [...new Set(rows.map(r => String(r[colorIdx] ?? "")))].sort();
  const pivotMap = new Map(); // xValue -> { colorValue: yValue }

  for (const row of rows) {
    const xVal = row[xIdx];
    const cVal = String(row[colorIdx] ?? "");
    const yVal = row[yIdx];
    const key = String(xVal);
    if (!pivotMap.has(key)) pivotMap.set(key, { _x: xVal });
    pivotMap.get(key)[cVal] = yVal;
  }

  const pivotedColumns = [columns[xIdx], ...colorValues];
  const pivotedRows = [...pivotMap.values()].map(entry =>
    [entry._x, ...colorValues.map(cv => entry[cv] ?? null)]
  );

  return { columns: pivotedColumns, rows: pivotedRows };
}

function renderWidgetContent(widget, data, onChartClick) {
  const { widget_type, chart_type, config } = widget;
  let { columns, rows } = data;

  // Apply color encoding pivot if set
  const encoding = config?.columnEncoding;
  if (encoding?.color && widget_type === "chart") {
    const pivoted = pivotByColor(columns, rows, encoding);
    if (pivoted) {
      columns = pivoted.columns;
      rows = pivoted.rows;
    }
  }

  const displayNames = config?._visual?.columnDisplayNames || {};
  const nullHandling = config?.nullHandling || "gap";
  const displayColumns = applyDisplayNames(columns, displayNames);
  const processedRows = applyNullHandling(rows, nullHandling);

  if (widget_type === "table") {
    return <PaginatedTable columns={displayColumns} rows={processedRows} conditionalRules={config?.conditionalRules} showSummary={config?.showSummary} />;
  }

  if (widget_type === "kpi") {
    return <KPIDisplay columns={columns} rows={rows} config={config} />;
  }

  if (widget_type === "chart") {
    // Dashboard-specific chart types (gauge, treemap, etc.) use ResponsiveChart
    if (DASHBOARD_CHART_TYPES.has(chart_type)) {
      return <ResponsiveChart type={chart_type} columns={displayColumns} rows={processedRows} config={config} />;
    }
    // Standard chart types (bar, line, etc.) use ChartPanel
    return (
      <div style={{ flex: 1, minHeight: 0, overflow: "hidden", position: "relative" }}>
        <ChartPanel
          columns={displayColumns}
          rows={processedRows}
          forcedType={chart_type}
          compact={true}
          xAxisLabel={config?.xAxisLabel}
          yAxisLabel={config?.yAxisLabel}
          onDataClick={onChartClick}
          config={config}
        />
      </div>
    );
  }

  if (widget_type === "text") {
    return (
      <div style={{ padding: 12, fontSize: 14, overflow: "auto", flex: 1 }}>
        {renderMarkdown(config?.content || "")}
      </div>
    );
  }

  if (widget_type === "image") {
    const url = config?.image_url;
    if (!url) return <div style={{ display: "flex", alignItems: "center", justifyContent: "center", flex: 1, opacity: 0.4, fontSize: 13 }}>No image configured</div>;
    return <img src={url} alt={widget.title || "Image"} style={{ objectFit: config?.fit || "contain", width: "100%", height: "100%", display: "block" }} />;
  }

  if (widget_type === "divider") {
    return <hr style={{ border: "none", borderTop: "1px solid var(--havn-border)", margin: "auto 0", width: "100%" }} />;
  }

  // Default: table
  return (
    <div style={{ overflow: "auto", flex: 1 }}>
      <SortableTable columns={displayColumns} rows={processedRows} />
    </div>
  );
}

function KPIDisplay({ columns, rows, config }) {
  if (!rows || rows.length === 0 || !columns || columns.length === 0) {
    return <div style={st.center}><span style={{ opacity: 0.4 }}>No data</span></div>;
  }

  const valueCol = config?.value_column || columns[0];
  const compCol = config?.comparison_column || (columns.length > 1 ? columns[1] : null);
  const valueIdx = columns.indexOf(valueCol);
  const compIdx = compCol ? columns.indexOf(compCol) : -1;

  const value = valueIdx >= 0 ? rows[0][valueIdx] : rows[0][0];
  const comp = compIdx >= 0 ? rows[0][compIdx] : null;

  const prefix = config?.prefix || "";
  const suffix = config?.suffix || "";

  function fmtBig(v) {
    if (v === null || v === undefined) return "—";
    const n = Number(v);
    if (isNaN(n)) return String(v);
    const abs = Math.abs(n);
    if (abs >= 1e9) return (n / 1e9).toFixed(1) + "B";
    if (abs >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (abs >= 1e3) return (n / 1e3).toFixed(1) + "K";
    if (Number.isInteger(n)) return n.toLocaleString();
    return n.toFixed(2);
  }

  // Format value: use formatNumber when kpiValueFormat is configured, else fall back to fmtBig
  const displayValue = config?.kpiValueFormat
    ? formatNumber(value, config.kpiValueFormat, config.kpiDecimals ?? null, config.currency)
    : fmtBig(value);

  let delta = null;
  let deltaColor = "var(--havn-text-secondary)";
  if (comp !== null && comp !== undefined) {
    const nVal = Number(value);
    const nComp = Number(comp);
    if (!isNaN(nVal) && !isNaN(nComp) && nComp !== 0) {
      delta = ((nVal - nComp) / Math.abs(nComp)) * 100;
      deltaColor = delta > 0 ? "var(--havn-green)" : delta < 0 ? "var(--havn-red)" : "var(--havn-text-secondary)";
    }
  }

  // Conditional color rules: evaluate first matching rule
  let valueColor = "var(--havn-text)";
  if (config?.kpiConditionalRules?.length > 0) {
    const nVal = Number(value);
    if (!isNaN(nVal)) {
      for (const rule of config.kpiConditionalRules) {
        const rv = Number(rule.value);
        if (isNaN(rv)) continue;
        let match = false;
        switch (rule.op) {
          case ">": match = nVal > rv; break;
          case "<": match = nVal < rv; break;
          case ">=": match = nVal >= rv; break;
          case "<=": match = nVal <= rv; break;
          case "=": match = nVal === rv; break;
          default: break;
        }
        if (match) {
          valueColor = rule.color;
          break;
        }
      }
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", flex: 1, gap: 4 }}>
      <div style={{ fontSize: 36, fontWeight: 700, color: valueColor, lineHeight: 1.1 }}>
        {prefix}{displayValue}{suffix}
      </div>
      {delta !== null && (
        <div style={{ fontSize: 14, color: deltaColor, fontWeight: 500 }}>
          {delta > 0 ? "▲" : delta < 0 ? "▼" : "—"} {Math.abs(delta).toFixed(1)}%
        </div>
      )}
      {compCol && (
        <div style={{ fontSize: 12, color: "var(--havn-text-secondary)" }}>
          vs {compCol}: {fmtBig(comp)}
        </div>
      )}
      {config?.kpiSubtitle && (
        <div style={{ fontSize: 12, color: "var(--havn-text-secondary)", marginTop: 2 }}>
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
  container: {
    background: "var(--havn-bg-secondary, var(--havn-bg))",
    borderRadius: "var(--havn-radius, 8px)",
    display: "flex",
    flexDirection: "column",
    overflow: "hidden",
    position: "relative",
    height: "100%",
    boxShadow: "0 1px 3px rgba(0,0,0,0.06)",
    transition: "border-color 0.15s, box-shadow 0.15s",
  },
  pagination: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    padding: "4px 10px",
    borderTop: "1px solid var(--havn-border)",
    flexShrink: 0,
    fontSize: 12,
  },
  pageBtn: {
    background: "none",
    border: "1px solid var(--havn-border)",
    borderRadius: 4,
    color: "var(--havn-text)",
    cursor: "pointer",
    padding: "2px 8px",
    fontSize: 13,
    lineHeight: 1,
  },
  pageInfo: { color: "var(--havn-text)", fontWeight: 500, fontSize: 12 },
  pageCount: { color: "var(--havn-text-secondary)", fontSize: 11, marginLeft: "auto" },
  footer: {
    padding: "3px 10px",
    fontSize: 10,
    color: "var(--havn-text-secondary)",
    opacity: 0.6,
    borderTop: "1px solid var(--havn-border)",
    flexShrink: 0,
    display: "flex",
    gap: 0,
  },
  titleBar: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "6px 10px",
    borderBottom: "1px solid var(--havn-border)",
    minHeight: 32,
    flexShrink: 0,
  },
  dragHandle: {
    display: "flex",
    alignItems: "center",
    gap: 6,
    flex: 1,
    minWidth: 0,
    cursor: "default",
  },
  gripIcon: {
    opacity: 0.3,
    fontSize: 14,
    cursor: "grab",
    userSelect: "none",
  },
  title: {
    fontSize: 13,
    fontWeight: 600,
    color: "var(--havn-text)",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    cursor: "default",
  },
  titleInput: {
    fontSize: 13,
    fontWeight: 600,
    color: "var(--havn-text)",
    background: "var(--havn-bg)",
    border: "1px solid var(--havn-accent)",
    borderRadius: 3,
    padding: "1px 6px",
    outline: "none",
    width: "100%",
    boxSizing: "border-box",
  },
  actions: {
    display: "flex",
    alignItems: "center",
    gap: 4,
    position: "relative",
  },
  spinner: {
    fontSize: 14,
    animation: "spin 1s linear infinite",
    opacity: 0.5,
  },
  menuBtn: {
    background: "none",
    border: "none",
    color: "var(--havn-text-secondary)",
    cursor: "pointer",
    fontSize: 16,
    padding: "2px 6px",
    borderRadius: 4,
    transition: "background 0.1s, color 0.1s",
  },
  menu: {
    position: "absolute",
    top: "100%",
    right: 0,
    background: "var(--havn-bg)",
    border: "1px solid var(--havn-border)",
    borderRadius: 6,
    padding: 4,
    zIndex: 100,
    minWidth: 120,
    boxShadow: "0 4px 12px rgba(0,0,0,0.2)",
  },
  menuItem: {
    display: "block",
    width: "100%",
    background: "none",
    border: "none",
    color: "var(--havn-text)",
    cursor: "pointer",
    padding: "6px 10px",
    fontSize: 13,
    textAlign: "left",
    borderRadius: 4,
    transition: "background 0.1s",
  },
  content: {
    flex: 1,
    display: "flex",
    flexDirection: "column",
    minHeight: 0,
    overflow: "hidden",
  },
  center: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    flex: 1,
    gap: 8,
  },
  retryBtn: {
    background: "none",
    border: "1px solid var(--havn-border)",
    color: "var(--havn-text)",
    cursor: "pointer",
    padding: "4px 12px",
    borderRadius: 4,
    fontSize: 12,
    transition: "background 0.1s",
  },
  resizeHandle: {
    position: "absolute",
    bottom: 2,
    right: 2,
    width: 24,
    height: 24,
    cursor: "nwse-resize",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: 12,
    opacity: 0.4,
    color: "var(--havn-text-secondary)",
    userSelect: "none",
    background: "linear-gradient(135deg, transparent 50%, var(--havn-border) 50%, var(--havn-border) 55%, transparent 55%, transparent 65%, var(--havn-border) 65%, var(--havn-border) 70%, transparent 70%)",
    borderRadius: "0 0 var(--havn-radius, 8px) 0",
  },
  widgetFullscreenOverlay: {
    position: "fixed",
    inset: 0,
    zIndex: 9999,
    background: "var(--havn-bg)",
    display: "flex",
    flexDirection: "column",
    overflow: "hidden",
  },
  widgetFullscreenHeader: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "12px 20px",
    borderBottom: "1px solid var(--havn-border)",
    flexShrink: 0,
  },
  widgetFullscreenClose: {
    background: "none",
    border: "1px solid var(--havn-border)",
    color: "var(--havn-text-secondary)",
    cursor: "pointer",
    fontSize: 20,
    width: 36,
    height: 36,
    borderRadius: 6,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
  },
  widgetFullscreenContent: {
    flex: 1,
    display: "flex",
    flexDirection: "column",
    overflow: "hidden",
    padding: 16,
    minHeight: 0,
  },
  skeleton: {
    flex: 1,
    display: "flex",
    flexDirection: "column",
    gap: 8,
    padding: 16,
    justifyContent: "center",
  },
  skelBar: {
    height: 12,
    borderRadius: 4,
    background: "var(--havn-border)",
    opacity: 0.4,
    animation: "pulse 1.5s ease-in-out infinite",
  },
  drillBreadcrumb: {
    display: "flex",
    alignItems: "center",
    gap: 6,
    padding: "3px 10px",
    borderBottom: "1px solid var(--havn-border)",
    background: "var(--havn-bg)",
    flexShrink: 0,
    fontSize: 11,
  },
  drillBreadcrumbBtn: {
    background: "none",
    border: "none",
    color: "var(--havn-accent)",
    cursor: "pointer",
    fontSize: 11,
    padding: 0,
    textDecoration: "underline",
  },
};
