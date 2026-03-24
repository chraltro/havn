import React, { useState, useRef, useEffect, useMemo } from "react";
import { useDashboard } from "./DashboardContext";
import SortableTable from "./SortableTable";
import ChartPanel from "./ChartPanel";
import DashboardChart from "./DashboardCharts";

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
  const { editMode, widgetData, refreshWidget, setCrossFilter, updateWidget } = useDashboard();
  const [showMenu, setShowMenu] = useState(false);
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleValue, setTitleValue] = useState(widget.title || "");
  const menuRef = useRef(null);
  const data = widgetData[widget.id];

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
      setCrossFilter(widget.id, column, value);
    }
  };

  return (
    <div
      style={{
        ...st.container,
        ...style,
        border: hasError ? "1px solid var(--havn-red)" : "1px solid var(--havn-border)",
      }}
    >
      {/* Title bar */}
      <div style={st.titleBar}>
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
        </div>
        <div style={st.actions}>
          {isLoading && <span style={st.spinner}>↻</span>}
          {editMode && (
            <button
              style={st.menuBtn}
              onClick={() => setShowMenu(!showMenu)}
              title="Widget options"
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
              <button style={{ ...st.menuItem, color: "var(--havn-red)" }} onClick={() => { setShowMenu(false); onDelete?.(widget); }}>
                Delete
              </button>
            </div>
          )}
        </div>
      </div>

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
            <span style={{ opacity: 0.4, fontSize: 13 }}>No query configured</span>
          </div>
        )}

        {hasData && !hasError && renderWidgetContent(widget, data, handleChartClick)}
      </div>

      {/* Footer — row count + freshness */}
      {hasData && data.row_count > 0 && (widget.position?.h || 4) >= 3 && (
        <div style={st.footer}>
          <span>{data.row_count} row{data.row_count !== 1 ? "s" : ""}</span>
          {data._fetchedAt && <span> · {timeAgo(data._fetchedAt)}</span>}
        </div>
      )}

      {/* Resize handle (edit mode only) */}
      {editMode && (
        <div
          className="dashboard-resize-handle"
          style={st.resizeHandle}
          title="Drag to resize"
        >
          ⌟
        </div>
      )}
    </div>
  );
}

function PaginatedTable({ columns, rows }) {
  const [page, setPage] = useState(0);
  const pageSize = 50;
  const totalPages = Math.ceil(rows.length / pageSize);
  const pagedRows = rows.slice(page * pageSize, (page + 1) * pageSize);

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, overflow: "hidden" }}>
      <div style={{ flex: 1, overflow: "auto" }}>
        <SortableTable columns={columns} rows={pagedRows} />
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

function renderWidgetContent(widget, data, onChartClick) {
  const { widget_type, chart_type, config } = widget;
  const { columns, rows } = data;

  if (widget_type === "table") {
    return <PaginatedTable columns={columns} rows={rows} />;
  }

  if (widget_type === "kpi") {
    return <KPIDisplay columns={columns} rows={rows} config={config} />;
  }

  if (widget_type === "chart") {
    // Dashboard-specific chart types (gauge, treemap, etc.) use ResponsiveChart
    if (DASHBOARD_CHART_TYPES.has(chart_type)) {
      return <ResponsiveChart type={chart_type} columns={columns} rows={rows} config={config} />;
    }
    // Standard chart types (bar, line, etc.) use ChartPanel
    return (
      <div style={{ flex: 1, minHeight: 0, overflow: "hidden", position: "relative" }}>
        <ChartPanel
          columns={columns}
          rows={rows}
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

  // Default: table
  return (
    <div style={{ overflow: "auto", flex: 1 }}>
      <SortableTable columns={columns} rows={rows} />
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

  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", flex: 1, gap: 4 }}>
      <div style={{ fontSize: 36, fontWeight: 700, color: "var(--havn-text)", lineHeight: 1.1 }}>
        {prefix}{fmtBig(value)}{suffix}
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
    padding: "2px 4px",
    borderRadius: 4,
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
};
