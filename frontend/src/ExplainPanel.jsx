import React, { useState } from "react";

/**
 * Visual query plan viewer.
 *
 * Renders a DuckDB EXPLAIN / EXPLAIN ANALYZE plan as an interactive tree
 * with collapsible nodes, operator color-coding, timing highlights,
 * and a cost bar showing relative time per node.
 */

// Operator category mapping for color-coding
const OP_CATEGORIES = {
  SEQ_SCAN: "scan",
  TABLE_SCAN: "scan",
  INDEX_SCAN: "scan",
  PARQUET_SCAN: "scan",
  CSV_SCAN: "scan",
  CHUNK_SCAN: "scan",
  ARROW_SCAN: "scan",
  DELIM_SCAN: "scan",
  HASH_JOIN: "join",
  NESTED_LOOP_JOIN: "join",
  PIECEWISE_MERGE_JOIN: "join",
  CROSS_PRODUCT: "join",
  POSITIONAL_JOIN: "join",
  ORDER_BY: "sort",
  TOP_N: "sort",
  STREAMING_TOP_N: "sort",
  FILTER: "filter",
  PROJECTION: "project",
  HASH_GROUP_BY: "aggregate",
  PERFECT_HASH_GROUP_BY: "aggregate",
  UNGROUPED_AGGREGATE: "aggregate",
  STREAMING_WINDOW: "aggregate",
  WINDOW: "aggregate",
};

function getCategory(operator) {
  const upper = (operator || "").toUpperCase().trim();
  if (OP_CATEGORIES[upper]) return OP_CATEGORIES[upper];
  if (upper.includes("SCAN")) return "scan";
  if (upper.includes("JOIN")) return "join";
  if (upper.includes("SORT") || upper.includes("ORDER")) return "sort";
  if (upper.includes("FILTER")) return "filter";
  if (upper.includes("GROUP") || upper.includes("AGGREGATE") || upper.includes("WINDOW")) return "aggregate";
  if (upper.includes("PROJECTION")) return "project";
  return "other";
}

const CATEGORY_COLORS = {
  scan: "var(--havn-accent)",
  join: "var(--havn-green)",
  sort: "var(--havn-yellow)",
  filter: "var(--havn-purple)",
  aggregate: "#e08050",
  project: "var(--havn-text-secondary)",
  other: "var(--havn-text-dim)",
};

const CATEGORY_BG = {
  scan: "rgba(var(--havn-accent-rgb, 100,180,180), 0.10)",
  join: "rgba(var(--havn-green-rgb, 80,180,100), 0.10)",
  sort: "rgba(var(--havn-yellow-rgb, 200,180,60), 0.10)",
  filter: "rgba(var(--havn-purple-rgb, 160,100,200), 0.10)",
  aggregate: "rgba(224, 128, 80, 0.10)",
  project: "rgba(128, 128, 128, 0.06)",
  other: "rgba(128, 128, 128, 0.04)",
};

function formatRows(n) {
  if (n == null) return null;
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "K";
  return String(n);
}

function formatTime(ms) {
  if (ms == null) return null;
  if (ms >= 1000) return (ms / 1000).toFixed(2) + "s";
  if (ms >= 1) return ms.toFixed(1) + "ms";
  return ms.toFixed(3) + "ms";
}

function timeColor(ms) {
  if (ms == null) return "var(--havn-text-dim)";
  if (ms >= 1000) return "var(--havn-red)";
  if (ms >= 100) return "var(--havn-yellow)";
  return "var(--havn-green)";
}

/** Single plan node in the tree */
function PlanNodeView({ node, depth = 0, totalTimeMs = 0, isAnalyze = false }) {
  const [collapsed, setCollapsed] = useState(false);
  const category = getCategory(node.operator);
  const color = CATEGORY_COLORS[category];
  const bg = CATEGORY_BG[category];
  const hasChildren = node.children && node.children.length > 0;

  const rows = isAnalyze ? node.actual_rows : node.estimated_rows;
  const rowLabel = isAnalyze ? "rows" : "est.";
  const timePct = node.time_percentage;

  // Extra info lines
  const extraEntries = Object.entries(node.extra_info || {}).filter(
    ([k]) => !k.startsWith("_") && k !== "Estimated Cardinality"
  );

  return (
    <div style={{ marginLeft: depth > 0 ? 20 : 0 }}>
      <div
        style={{
          display: "flex",
          alignItems: "flex-start",
          gap: "8px",
          padding: "6px 10px",
          marginBottom: "2px",
          background: bg,
          borderLeft: `3px solid ${color}`,
          borderRadius: "var(--havn-radius)",
          cursor: hasChildren ? "pointer" : "default",
        }}
        onClick={() => hasChildren && setCollapsed(!collapsed)}
      >
        {/* Expand/collapse indicator */}
        <span style={{ width: "14px", flexShrink: 0, fontSize: "10px", color: "var(--havn-text-dim)", marginTop: "2px", textAlign: "center" }}>
          {hasChildren ? (collapsed ? "\u25B8" : "\u25BE") : "\u00B7"}
        </span>

        {/* Main content */}
        <div style={{ flex: 1, minWidth: 0 }}>
          {/* Operator + table */}
          <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
            <span style={{ fontWeight: 600, fontSize: "12px", fontFamily: "var(--havn-font-mono)", color }}>{node.operator}</span>
            {node.table && (
              <span style={{ fontSize: "11px", color: "var(--havn-text-secondary)", fontFamily: "var(--havn-font-mono)", background: "var(--havn-bg-tertiary)", padding: "1px 6px", borderRadius: "3px" }}>
                {node.table}
              </span>
            )}
          </div>

          {/* Extra info */}
          {extraEntries.length > 0 && (
            <div style={{ marginTop: "3px" }}>
              {extraEntries.map(([key, value]) => (
                <div key={key} style={{ fontSize: "10px", color: "var(--havn-text-dim)", fontFamily: "var(--havn-font-mono)", lineHeight: 1.5 }}>
                  <span style={{ color: "var(--havn-text-secondary)" }}>{key}:</span>{" "}
                  {Array.isArray(value) ? value.join(", ") : String(value)}
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Stats column */}
        <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: "2px", flexShrink: 0 }}>
          {rows != null && (
            <span style={{ fontSize: "11px", color: "var(--havn-text-secondary)", fontFamily: "var(--havn-font-mono)" }}>
              {formatRows(rows)} {rowLabel}
            </span>
          )}
          {isAnalyze && node.actual_time_ms != null && (
            <span style={{ fontSize: "11px", fontWeight: 600, fontFamily: "var(--havn-font-mono)", color: timeColor(node.actual_time_ms) }}>
              {formatTime(node.actual_time_ms)}
            </span>
          )}
        </div>

        {/* Cost bar (only for ANALYZE with timing data) */}
        {isAnalyze && timePct != null && totalTimeMs > 0 && (
          <div style={{ width: "60px", flexShrink: 0, display: "flex", flexDirection: "column", alignItems: "flex-end", gap: "2px" }}>
            <div style={{ width: "100%", height: "4px", background: "var(--havn-bg-tertiary)", borderRadius: "2px", overflow: "hidden" }}>
              <div style={{ width: `${Math.max(timePct, 1)}%`, height: "100%", background: timeColor(node.actual_time_ms), borderRadius: "2px" }} />
            </div>
            <span style={{ fontSize: "9px", color: "var(--havn-text-dim)" }}>{timePct.toFixed(0)}%</span>
          </div>
        )}
      </div>

      {/* Children */}
      {hasChildren && !collapsed && (
        <div>
          {node.children.map((child, i) => (
            <PlanNodeView key={i} node={child} depth={depth + 1} totalTimeMs={totalTimeMs} isAnalyze={isAnalyze} />
          ))}
        </div>
      )}
    </div>
  );
}

/** Legend showing operator category colors */
function PlanLegend() {
  const items = [
    ["Scan", "scan"],
    ["Join", "join"],
    ["Sort", "sort"],
    ["Filter", "filter"],
    ["Aggregate", "aggregate"],
    ["Projection", "project"],
  ];
  return (
    <div style={{ display: "flex", gap: "12px", padding: "6px 10px", flexWrap: "wrap" }}>
      {items.map(([label, cat]) => (
        <div key={cat} style={{ display: "flex", alignItems: "center", gap: "4px" }}>
          <div style={{ width: "8px", height: "8px", borderRadius: "2px", background: CATEGORY_COLORS[cat] }} />
          <span style={{ fontSize: "10px", color: "var(--havn-text-dim)" }}>{label}</span>
        </div>
      ))}
    </div>
  );
}

/**
 * Parse DuckDB EXPLAIN text output into a structured tree.
 *
 * DuckDB EXPLAIN output uses box-drawing characters and indentation like:
 *   ┌───────────────────────────┐
 *   │    HASH_GROUP_BY          │
 *   │   ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ │
 *   │             #0            │
 *   │          count            │
 *   └─────────────┬─────────────┘
 *   ┌─────────────┴─────────────┐
 *   │         SEQ_SCAN          │
 *   │   ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ │
 *   │          orders           │
 *   └───────────────────────────┘
 */
export function parseDuckDBPlan(rawText) {
  if (!rawText || typeof rawText !== "string") return null;

  const lines = rawText.split("\n");
  const nodes = [];
  let current = null;

  for (const line of lines) {
    const trimmed = line.replace(/[│┌┐└┘┬┴─╶╴├┤┼]/g, "").trim();
    if (!trimmed || /^[─ ]+$/.test(trimmed)) continue;

    // Detect operator names: typically ALL_CAPS_WITH_UNDERSCORES
    if (/^[A-Z][A-Z_0-9 ]+$/.test(trimmed) && !trimmed.startsWith("#") && trimmed.length > 1) {
      if (current) nodes.push(current);
      current = { operator: trimmed.trim(), children: [], extra_info: {}, table: null, estimated_rows: null, actual_rows: null, actual_time_ms: null, time_percentage: null };
    } else if (current) {
      // Table name (lowercase, no spaces, or schema.table)
      if (!current.table && /^[a-z_][a-z0-9_.]*$/.test(trimmed)) {
        current.table = trimmed;
      }
      // Estimated cardinality
      else if (/^~?\d[\d,]*$/.test(trimmed.replace(/[~,]/g, ""))) {
        const num = parseInt(trimmed.replace(/[~,]/g, ""), 10);
        if (!isNaN(num)) current.estimated_rows = num;
      }
      // EC marker
      else if (trimmed.startsWith("EC:") || trimmed.startsWith("EC=")) {
        const num = parseInt(trimmed.replace(/[^0-9]/g, ""), 10);
        if (!isNaN(num)) current.estimated_rows = num;
      }
      // Column references or other extra info
      else if (trimmed.startsWith("#") || trimmed.startsWith("[")) {
        const key = current.table ? "Columns" : "Projection";
        const prev = current.extra_info[key];
        current.extra_info[key] = prev ? prev + ", " + trimmed : trimmed;
      }
      // Filters, expressions, etc.
      else if (trimmed.includes("=") || trimmed.includes(">") || trimmed.includes("<") || trimmed.includes("BETWEEN") || trimmed.includes("AND") || trimmed.includes("OR")) {
        const key = "Filter";
        const prev = current.extra_info[key];
        current.extra_info[key] = prev ? prev + " " + trimmed : trimmed;
      }
      // Anything else is extra info
      else if (trimmed.length > 0 && trimmed !== "─") {
        const key = "Info";
        const prev = current.extra_info[key];
        current.extra_info[key] = prev ? prev + ", " + trimmed : trimmed;
      }
    }
  }
  if (current) nodes.push(current);

  if (nodes.length === 0) return null;

  // Build tree: DuckDB EXPLAIN lists nodes top-down (root first, children below).
  // The box-drawing connects parent to child with └──┬──┘ / ┌──┴──┐ patterns.
  // Simple heuristic: build a linear chain (parent -> child -> grandchild).
  function buildTree(nodeList) {
    if (nodeList.length === 0) return null;
    if (nodeList.length === 1) return nodeList[0];
    const root = nodeList[0];
    let parent = root;
    for (let i = 1; i < nodeList.length; i++) {
      parent.children = [nodeList[i]];
      parent = nodeList[i];
    }
    return root;
  }

  return buildTree(nodes);
}

/**
 * ExplainPanel — main export.
 *
 * Props:
 *   plan     — structured plan dict from API (with operator, children, etc.)
 *   raw      — raw text from DuckDB EXPLAIN
 *   isAnalyze — whether this is an EXPLAIN ANALYZE result
 */
export default function ExplainPanel({ plan, raw, isAnalyze = false }) {
  const [viewMode, setViewMode] = useState("visual");

  if (!plan && !raw) return null;

  // If no structured plan provided, try to parse from raw text
  const effectivePlan = plan || (raw ? parseDuckDBPlan(raw) : null);
  const totalTimeMs = effectivePlan?._total_time_ms || 0;

  return (
    <div style={st.container}>
      {/* Header */}
      <div style={st.header}>
        <span style={st.title}>
          {isAnalyze ? "Query Plan (Analyzed)" : "Query Plan"}
        </span>
        {isAnalyze && totalTimeMs > 0 && (
          <span style={{ fontSize: "11px", color: "var(--havn-text-secondary)", fontFamily: "var(--havn-font-mono)" }}>
            Total: {formatTime(totalTimeMs)}
          </span>
        )}
        <div style={st.viewToggle}>
          <button onClick={() => setViewMode("visual")} style={viewMode === "visual" ? st.viewBtnActive : st.viewBtn}>
            Visual
          </button>
          <button onClick={() => setViewMode("raw")} style={viewMode === "raw" ? st.viewBtnActive : st.viewBtn}>
            Raw
          </button>
        </div>
      </div>

      {/* Legend */}
      {viewMode === "visual" && <PlanLegend />}

      {/* Content */}
      <div style={st.body}>
        {viewMode === "visual" ? (
          effectivePlan ? (
            <div style={{ padding: "6px 8px" }}>
              <PlanNodeView node={effectivePlan} totalTimeMs={totalTimeMs} isAnalyze={isAnalyze} />
            </div>
          ) : (
            <div style={st.fallback}>No structured plan available. Switch to Raw view.</div>
          )
        ) : (
          <pre style={st.rawPre}>{raw || "No raw plan available."}</pre>
        )}
      </div>
    </div>
  );
}

const st = {
  container: { display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" },
  header: {
    display: "flex",
    alignItems: "center",
    gap: "10px",
    padding: "6px 12px",
    borderBottom: "1px solid var(--havn-border)",
    flexShrink: 0,
  },
  title: { fontWeight: 600, fontSize: "12px", color: "var(--havn-text)" },
  viewToggle: { display: "flex", marginLeft: "auto", gap: "1px", background: "var(--havn-border)", borderRadius: "var(--havn-radius-lg)", overflow: "hidden" },
  viewBtn: { padding: "3px 10px", background: "var(--havn-btn-bg)", border: "none", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: "11px", fontWeight: 500 },
  viewBtnActive: { padding: "3px 10px", background: "var(--havn-bg-secondary)", border: "none", color: "var(--havn-text)", cursor: "pointer", fontSize: "11px", fontWeight: 600 },
  body: { flex: 1, overflow: "auto" },
  rawPre: {
    margin: 0,
    padding: "10px 12px",
    fontFamily: "var(--havn-font-mono)",
    fontSize: "11px",
    color: "var(--havn-text)",
    lineHeight: 1.5,
    whiteSpace: "pre-wrap",
    wordBreak: "break-all",
  },
  fallback: { padding: "16px", color: "var(--havn-text-dim)", fontSize: "12px", textAlign: "center" },
};
