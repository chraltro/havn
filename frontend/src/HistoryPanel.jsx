import React, { useState, useEffect } from "react";
import { api } from "./api";

const PAGE_SIZE = 50;

function timeAgo(dateStr) {
  if (!dateStr) return "";
  const now = Date.now();
  const then = new Date(dateStr).getTime();
  const diffMs = now - then;
  if (diffMs < 0) return "just now";
  const seconds = Math.floor(diffMs / 1000);
  if (seconds < 10) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function formatDuration(ms) {
  if (ms == null) return "";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatRows(n) {
  if (n == null || n === 0) return "";
  return n.toLocaleString();
}

function statusStyle(status) {
  if (status === "success") {
    return { background: "color-mix(in srgb, var(--havn-green) 12%, transparent)", color: "var(--havn-green)" };
  }
  if (status === "error" || status === "failed") {
    return { background: "color-mix(in srgb, var(--havn-red) 12%, transparent)", color: "var(--havn-red)" };
  }
  if (status === "running") {
    return { background: "color-mix(in srgb, var(--havn-accent) 12%, transparent)", color: "var(--havn-accent)" };
  }
  // skipped, cancelled, or unknown
  return { background: "color-mix(in srgb, var(--havn-text-dim) 12%, transparent)", color: "var(--havn-text-secondary)" };
}

function statusIcon(status) {
  if (status === "success") return "\u2713";
  if (status === "error" || status === "failed") return "\u2717";
  if (status === "running") return "\u25CB";
  return "\u2013";
}

export default function HistoryPanel({ onOpenFile }) {
  // Grouped runs view
  const [runs, setRuns] = useState([]);
  const [expandedRun, setExpandedRun] = useState(null);
  const [runDetail, setRunDetail] = useState(null);
  const [comparison, setComparison] = useState(null);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState("all");
  const [viewMode, setViewMode] = useState("all"); // "grouped" | "all"

  // Flat history fallback
  const [flatHistory, setFlatHistory] = useState([]);
  const [flatLoading, setFlatLoading] = useState(false);
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);

  const [error, setError] = useState(null);
  const [targetFilter, setTargetFilter] = useState("");

  // Sorting
  const [sortKey, setSortKey] = useState("started_at");
  const [sortDir, setSortDir] = useState("desc"); // "asc" | "desc"

  function toggleSort(key) {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "started_at" ? "desc" : "asc");
    }
  }

  function sortItems(items, key, dir) {
    return [...items].sort((a, b) => {
      let va = a[key], vb = b[key];
      if (va == null) va = key === "started_at" ? "" : -Infinity;
      if (vb == null) vb = key === "started_at" ? "" : -Infinity;
      if (typeof va === "string") {
        const cmp = va.localeCompare(vb);
        return dir === "asc" ? cmp : -cmp;
      }
      return dir === "asc" ? va - vb : vb - va;
    });
  }

  useEffect(() => {
    loadRuns();
    loadFlatHistory();
  }, []);

  // Auto-refresh when pipeline completes
  useEffect(() => {
    const handler = () => { loadRuns(); loadFlatHistory(); };
    window.addEventListener("havn-data-changed", handler);
    return () => window.removeEventListener("havn-data-changed", handler);
  }, []);

  async function loadRuns() {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getPipelineRuns(100);
      setRuns(data);
      // If grouped runs exist, switch to grouped view
      if (data && data.length > 0) {
        setViewMode("grouped");
      }
    } catch (e) {
      // Endpoint might not exist yet — fall back to flat view
      setViewMode("all");
      await loadFlatHistory();
    } finally {
      setLoading(false);
    }
  }

  async function loadFlatHistory() {
    setFlatLoading(true);
    setError(null);
    try {
      const data = await api.getHistory(500);
      setFlatHistory(data);
    } catch (e) {
      setError(e.message || "Failed to load history");
    } finally {
      setFlatLoading(false);
    }
  }

  async function toggleExpand(pipelineRunId) {
    if (expandedRun === pipelineRunId) {
      setExpandedRun(null);
      setRunDetail(null);
      setComparison(null);
      return;
    }
    setExpandedRun(pipelineRunId);
    setRunDetail(null);
    setComparison(null);
    try {
      const [detail, comp] = await Promise.all([
        api.getPipelineRunDetail(pipelineRunId),
        api.getRunComparison(pipelineRunId).catch(() => null),
      ]);
      setRunDetail(detail);
      setComparison(comp);
    } catch (e) {
      console.error("Failed to load run detail:", e);
    }
  }

  function handleRefresh() {
    if (viewMode === "grouped") {
      loadRuns();
    } else {
      loadFlatHistory();
    }
  }

  function switchView(mode) {
    setViewMode(mode);
    if (mode === "all" && flatHistory.length === 0) {
      loadFlatHistory();
    }
  }

  // Filter runs
  const filteredRuns = runs.filter((run) => {
    if (statusFilter === "success" && run.status !== "success") return false;
    if (statusFilter === "failed" && run.status !== "error" && run.status !== "failed") return false;
    if (targetFilter && !(run.target || "").toLowerCase().includes(targetFilter.toLowerCase())) return false;
    return true;
  });

  // Filter flat history
  const filteredFlat = flatHistory.filter((row) => {
    if (statusFilter === "success" && row.status !== "success") return false;
    if (statusFilter === "failed" && row.status !== "error" && row.status !== "failed") return false;
    if (targetFilter && !(row.target || "").toLowerCase().includes(targetFilter.toLowerCase())) return false;
    return true;
  });
  const visibleFlat = filteredFlat.slice(0, visibleCount);
  const hasMoreFlat = visibleCount < filteredFlat.length;

  const isLoading = loading || flatLoading;

  // Build comparison lookup by target
  const compLookup = {};
  if (comparison && comparison.models) {
    for (const m of comparison.models) {
      compLookup[m.target] = m;
    }
  }

  function openModelFile(target) {
    if (!onOpenFile || !target) return;
    let t = target;
    if (t.includes(":")) t = t.split(":").pop();
    if (/^(bronze|silver|gold)\.\w+$/.test(t)) {
      const [schema, model] = t.split(".");
      onOpenFile(`transform/${schema}/${model}.sql`, 1, 1);
    } else {
      onOpenFile(t, 1, 1);
    }
  }

  return (
    <div style={styles.container}>
      {/* Header */}
      <div style={styles.header}>
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <div style={styles.viewToggle}>
            <button
              onClick={() => switchView("grouped")}
              style={viewMode === "grouped" ? styles.viewBtnActive : styles.viewBtn}
            >
              Grouped
            </button>
            <button
              onClick={() => switchView("all")}
              style={viewMode === "all" ? styles.viewBtnActive : styles.viewBtn}
            >
              All entries
            </button>
          </div>
          <select
            style={styles.filterSelect}
            value={statusFilter}
            aria-label="Filter by status"
            onChange={(e) => { setStatusFilter(e.target.value); setVisibleCount(PAGE_SIZE); }}
          >
            <option value="all">All Statuses</option>
            <option value="success">Success</option>
            <option value="failed">Failed</option>
          </select>
          <input
            style={styles.filterInput}
            placeholder="Search target..."
            aria-label="Search target"
            value={targetFilter}
            onChange={(e) => { setTargetFilter(e.target.value); setVisibleCount(PAGE_SIZE); }}
          />
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <span style={{ fontSize: "11px", color: "var(--havn-text-dim)" }}>
            {viewMode === "grouped"
              ? `${filteredRuns.length} run${filteredRuns.length !== 1 ? "s" : ""}`
              : `${filteredFlat.length} entr${filteredFlat.length !== 1 ? "ies" : "y"}`}
          </span>
          <button onClick={handleRefresh} style={styles.refreshBtn}>
            Refresh
          </button>
        </div>
      </div>

      {/* Loading */}
      {isLoading && <div style={styles.loadingMsg}>Loading...</div>}

      {/* Error */}
      {!isLoading && error && <div style={styles.errorMsg}>{error}</div>}

      {/* Empty */}
      {!isLoading && !error && viewMode === "grouped" && filteredRuns.length === 0 && (
        <div style={styles.emptyState}>
          {runs.length === 0 ? (
            <>
              <div style={styles.emptyTitle}>No pipeline runs yet</div>
              <div style={styles.emptyHint}>
                Run <code style={styles.code}>havn transform</code> or <code style={styles.code}>havn jobs run</code> to build models and see run history here.
              </div>
            </>
          ) : (
            <>
              <div style={styles.emptyTitle}>No matching runs</div>
              <div style={styles.emptyHint}>Try adjusting your filters.</div>
            </>
          )}
        </div>
      )}

      {/* Grouped runs view */}
      {!isLoading && viewMode === "grouped" && filteredRuns.length > 0 && (
        <div style={styles.listWrap}>
          {/* Sort header */}
          <div style={styles.sortHeader}>
            {[
              { key: "run_type", label: "Type", flex: "0 0 80px" },
              { key: "target", label: "Target", flex: "1 1 auto" },
              { key: "model_count", label: "Models", flex: "0 0 70px" },
              { key: "total_duration_ms", label: "Duration", flex: "0 0 70px" },
              { key: "status", label: "Status", flex: "0 0 80px" },
              { key: "started_at", label: "Time", flex: "0 0 80px" },
            ].map((col) => (
              <span
                key={col.key}
                style={{ ...styles.sortCol, flex: col.flex, cursor: "pointer" }}
                onClick={() => toggleSort(col.key)}
              >
                {col.label}
                {sortKey === col.key && (
                  <span style={{ marginLeft: "3px", opacity: 0.7 }}>{sortDir === "asc" ? "\u25B4" : "\u25BE"}</span>
                )}
              </span>
            ))}
          </div>
          {sortItems(filteredRuns, sortKey, sortDir).map((run) => {
            const isExpanded = expandedRun === run.pipeline_run_id;
            const hasErrors = run.error_count > 0;

            return (
              <div key={run.pipeline_run_id}>
                {/* Pipeline run row */}
                <div
                  style={styles.runRow}
                  onClick={() => toggleExpand(run.pipeline_run_id)}
                  onMouseEnter={(e) => { e.currentTarget.style.background = "color-mix(in srgb, var(--havn-accent) 6%, transparent)"; }}
                  onMouseLeave={(e) => { e.currentTarget.style.background = ""; }}
                >
                  <span style={styles.expandArrow}>{isExpanded ? "\u25BC" : "\u25B6"}</span>
                  <span style={styles.runTypeBadge}>{run.run_type}</span>
                  <span style={styles.runTarget}>{run.target || ""}</span>
                  <span style={styles.runModels}>{run.model_count} model{run.model_count !== 1 ? "s" : ""}</span>
                  <span style={styles.runDuration}>{formatDuration(run.total_duration_ms)}</span>
                  <span style={{
                    ...styles.statusBadge,
                    ...statusStyle(run.status),
                  }}>
                    {statusIcon(run.status)} {run.status}
                  </span>
                  {hasErrors && (
                    <span style={styles.errorCount}>{run.error_count} err</span>
                  )}
                  <span
                    style={styles.timeAgo}
                    title={run.started_at ? run.started_at.slice(0, 19).replace("T", " ") : ""}
                  >{timeAgo(run.started_at)}</span>
                </div>

                {/* Expanded detail */}
                {isExpanded && (
                  <div style={styles.detailContainer}>
                    {!runDetail && (
                      <div style={{ padding: "8px 16px 8px 40px", color: "var(--havn-text-dim)", fontSize: "11px" }}>Loading...</div>
                    )}
                    {runDetail && runDetail.length === 0 && (
                      <div style={{ padding: "8px 16px 8px 40px", color: "var(--havn-text-dim)", fontSize: "11px" }}>No model details available.</div>
                    )}
                    {runDetail && runDetail.length > 0 && runDetail.map((row, idx) => {
                      const isLast = idx === runDetail.length - 1;
                      const comp = compLookup[row.target];
                      const rowSuccess = row.status === "success";
                      const rowFailed = row.status === "error" || row.status === "failed";

                      // Delta calculations
                      let rowDelta = null;
                      let durDelta = null;
                      let isNew = comp?.is_new || false;

                      if (comp && !isNew) {
                        if (row.rows_affected != null && comp.prev_rows_affected != null) {
                          rowDelta = row.rows_affected - comp.prev_rows_affected;
                        }
                        if (row.duration_ms != null && comp.prev_duration_ms != null) {
                          durDelta = row.duration_ms - comp.prev_duration_ms;
                        }
                      }

                      return (
                        <div key={row.run_id || idx} style={styles.detailRow}>
                          <span style={styles.treeLine}>{isLast ? "\u2514" : "\u251C"}</span>
                          <span
                            style={styles.detailTarget}
                            onClick={(e) => { e.stopPropagation(); openModelFile(row.target); }}
                            onMouseEnter={(e) => { e.currentTarget.style.color = "var(--havn-accent)"; e.currentTarget.style.textDecoration = "underline"; }}
                            onMouseLeave={(e) => { e.currentTarget.style.color = ""; e.currentTarget.style.textDecoration = ""; }}
                            title={`Open ${row.target}`}
                          >
                            {row.target}
                          </span>
                          <span style={{
                            ...styles.detailStatus,
                            color: rowFailed ? "var(--havn-red)" : rowSuccess ? "var(--havn-green)" : "var(--havn-text-secondary)",
                          }}>
                            {rowFailed ? "\u2717" : rowSuccess ? "\u2713" : "\u2013"}
                          </span>
                          <span style={styles.detailDuration}>
                            {formatDuration(row.duration_ms)}
                          </span>
                          <span style={styles.detailRows}>
                            {row.rows_affected ? `${formatRows(row.rows_affected)} rows` : ""}
                          </span>

                          {/* Deltas */}
                          {isNew && (
                            <span style={{ ...styles.delta, color: "var(--havn-accent)" }}>(new)</span>
                          )}
                          {!isNew && rowDelta !== null && rowDelta !== 0 && (
                            <span style={{
                              ...styles.delta,
                              color: rowDelta > 0 ? "var(--havn-green)" : "var(--havn-red)",
                            }}>
                              ({rowDelta > 0 ? "+" : ""}{rowDelta.toLocaleString()})
                            </span>
                          )}
                          {!isNew && durDelta !== null && Math.abs(durDelta) >= 100 && (
                            <span style={{
                              ...styles.delta,
                              color: durDelta < 0 ? "var(--havn-green)" : "var(--havn-red)",
                            }}>
                              {durDelta < 0
                                ? `${formatDuration(Math.abs(durDelta))} faster`
                                : `${formatDuration(durDelta)} slower`}
                            </span>
                          )}

                          {/* Error message */}
                          {rowFailed && row.error && (
                            <span style={styles.detailError} title={row.error}>
                              {row.error.length > 80 ? row.error.slice(0, 80) + "..." : row.error}
                            </span>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Flat history view (fallback) */}
      {!isLoading && viewMode === "all" && (
        <div style={styles.tableWrap}>
          {filteredFlat.length === 0 && !error && (
            <div style={styles.emptyState}>
              {flatHistory.length === 0 ? (
                <>
                  <div style={styles.emptyTitle}>No pipeline runs yet</div>
                  <div style={styles.emptyHint}>
                    Run <code style={styles.code}>havn transform</code> or <code style={styles.code}>havn jobs run</code> to build models and see run history here.
                  </div>
                </>
              ) : (
                <>
                  <div style={styles.emptyTitle}>No matching entries</div>
                  <div style={styles.emptyHint}>Try adjusting your filters.</div>
                </>
              )}
            </div>
          )}
          {filteredFlat.length > 0 && (
            <>
              <table style={styles.table}>
                <thead>
                  <tr>
                    {[
                      { key: "run_type", label: "Type" },
                      { key: "target", label: "Target" },
                      { key: "status", label: "Status" },
                      { key: "started_at", label: "Time" },
                      { key: "duration_ms", label: "Duration" },
                      { key: "rows_affected", label: "Rows" },
                      { key: null, label: "Error" },
                    ].map((col, i) => (
                      <th
                        key={i}
                        style={{ ...styles.th, cursor: col.key ? "pointer" : "default" }}
                        onClick={() => col.key && toggleSort(col.key)}
                      >
                        {col.label}
                        {col.key && sortKey === col.key && (
                          <span style={{ marginLeft: "3px", opacity: 0.7 }}>{sortDir === "asc" ? "\u25B4" : "\u25BE"}</span>
                        )}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {sortItems(visibleFlat, sortKey, sortDir).map((row) => (
                    <tr
                      key={row.run_id}
                      onMouseEnter={(e) => { e.currentTarget.style.background = "color-mix(in srgb, var(--havn-accent) 8%, transparent)"; }}
                      onMouseLeave={(e) => { e.currentTarget.style.background = ""; }}
                    >
                      <td style={styles.td}>
                        <span style={styles.typeBadge}>{row.run_type}</span>
                      </td>
                      <td style={{ ...styles.td, fontFamily: "var(--havn-font-mono)", fontWeight: 500 }}>
                        {onOpenFile && row.target ? (
                          <span
                            style={styles.fileLink}
                            onClick={() => openModelFile(row.target)}
                            onMouseEnter={(e) => { e.currentTarget.style.color = "var(--havn-accent)"; e.currentTarget.style.textDecoration = "underline"; }}
                            onMouseLeave={(e) => { e.currentTarget.style.color = ""; e.currentTarget.style.textDecoration = ""; }}
                            title={`Open ${row.target}`}
                          >
                            {row.target}
                          </span>
                        ) : row.target}
                      </td>
                      <td style={styles.td}>
                        <span style={{
                          ...styles.statusBadge,
                          ...statusStyle(row.status),
                        }}>{statusIcon(row.status)} {row.status}</span>
                      </td>
                      <td
                        style={{ ...styles.td, color: "var(--havn-text-secondary)" }}
                        title={row.started_at ? row.started_at.slice(0, 19).replace("T", " ") : ""}
                      >
                        {timeAgo(row.started_at)}
                      </td>
                      <td style={{ ...styles.td, textAlign: "right", fontFamily: "var(--havn-font-mono)" }}>
                        {formatDuration(row.duration_ms)}
                      </td>
                      <td style={{ ...styles.td, textAlign: "right", fontFamily: "var(--havn-font-mono)" }}>
                        {formatRows(row.rows_affected)}
                      </td>
                      <td style={{ ...styles.td, color: "var(--havn-red)", maxWidth: "300px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {row.error || ""}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {hasMoreFlat && (
                <div style={{ textAlign: "center", padding: "12px" }}>
                  <button
                    onClick={() => setVisibleCount((c) => c + PAGE_SIZE)}
                    style={styles.refreshBtn}
                  >
                    Load More ({filteredFlat.length - visibleCount} remaining)
                  </button>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

const styles = {
  container: { display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" },
  header: { display: "flex", alignItems: "center", justifyContent: "space-between", padding: "8px 12px", borderBottom: "1px solid var(--havn-border)", gap: "8px", flexWrap: "wrap" },
  refreshBtn: { background: "var(--havn-btn-bg)", border: "1px solid var(--havn-btn-border)", borderRadius: "var(--havn-radius-lg)", color: "var(--havn-text)", padding: "4px 12px", cursor: "pointer", fontSize: "11px", fontWeight: 500 },
  loadingMsg: { padding: "24px", color: "var(--havn-text-secondary)", textAlign: "center" },
  emptyState: { padding: "48px 24px", textAlign: "center" },
  emptyTitle: { color: "var(--havn-text-secondary)", fontSize: "13px", fontWeight: 600, marginBottom: "6px" },
  emptyHint: { color: "var(--havn-text-dim)", fontSize: "12px", lineHeight: "1.5" },
  code: { fontFamily: "var(--havn-font-mono)", fontSize: "11px", background: "var(--havn-bg-tertiary)", padding: "1px 5px", borderRadius: "var(--havn-radius)" },
  errorMsg: { padding: "24px", color: "var(--havn-red)", textAlign: "center" },

  // View toggle
  viewToggle: { display: "flex", border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius)", overflow: "hidden" },
  viewBtn: { padding: "4px 10px", background: "none", border: "none", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: "11px", fontWeight: 500 },
  viewBtnActive: { padding: "4px 10px", background: "var(--havn-btn-bg)", border: "none", color: "var(--havn-text)", cursor: "pointer", fontSize: "11px", fontWeight: 600 },

  // Filter
  filterSelect: { padding: "4px 8px", background: "var(--havn-bg-tertiary)", color: "var(--havn-text)", border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius)", fontSize: "11px" },
  filterInput: { padding: "4px 10px", background: "var(--havn-bg-tertiary)", color: "var(--havn-text)", border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius)", fontSize: "11px", width: "160px" },

  // Sort header
  sortHeader: {
    display: "flex", alignItems: "center", gap: "10px", padding: "6px 12px 6px 34px",
    borderBottom: "2px solid var(--havn-border-light)", fontSize: "11px", fontWeight: 600,
    color: "var(--havn-text-secondary)", position: "sticky", top: 0,
    background: "var(--havn-bg-secondary)", zIndex: 1, userSelect: "none",
  },
  sortCol: { fontSize: "11px", fontWeight: 600, whiteSpace: "nowrap" },

  // Grouped runs list
  listWrap: { flex: 1, overflow: "auto" },
  runRow: {
    display: "flex", alignItems: "center", gap: "10px", padding: "8px 12px",
    borderBottom: "1px solid var(--havn-border)", cursor: "pointer",
    fontSize: "12px", transition: "background 0.1s",
  },
  expandArrow: { fontSize: "9px", color: "var(--havn-text-dim)", width: "12px", flexShrink: 0 },
  runTypeBadge: { flex: "0 0 80px", background: "var(--havn-btn-bg)", padding: "2px 8px", borderRadius: "var(--havn-radius)", fontSize: "11px", fontWeight: 500, textTransform: "capitalize", boxSizing: "border-box" },
  runTarget: { flex: "1 1 auto", fontFamily: "var(--havn-font-mono)", fontWeight: 500, color: "var(--havn-text)", minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  runModels: { flex: "0 0 70px", color: "var(--havn-text-secondary)", fontSize: "11px", whiteSpace: "nowrap" },
  runDuration: { flex: "0 0 70px", fontFamily: "var(--havn-font-mono)", color: "var(--havn-text-secondary)", fontSize: "11px" },
  statusBadge: { flex: "0 0 80px", padding: "2px 8px", borderRadius: "var(--havn-radius)", fontSize: "11px", fontWeight: 600, whiteSpace: "nowrap", boxSizing: "border-box" },
  errorCount: { fontSize: "11px", color: "var(--havn-red)", fontWeight: 600, flexShrink: 0 },
  timeAgo: { flex: "0 0 80px", textAlign: "right", color: "var(--havn-text-dim)", fontSize: "11px", whiteSpace: "nowrap" },

  // Expanded detail rows
  detailContainer: { background: "color-mix(in srgb, var(--havn-bg-tertiary) 50%, transparent)", borderBottom: "1px solid var(--havn-border)" },
  detailRow: {
    display: "flex", alignItems: "center", gap: "8px", padding: "5px 12px 5px 28px",
    fontSize: "12px",
  },
  treeLine: { color: "var(--havn-text-dim)", fontFamily: "var(--havn-font-mono)", fontSize: "12px", width: "14px", flexShrink: 0 },
  detailTarget: { fontFamily: "var(--havn-font-mono)", fontWeight: 500, cursor: "pointer", transition: "color 0.15s", minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  detailStatus: { fontSize: "12px", flexShrink: 0, width: "14px", textAlign: "center" },
  detailDuration: { fontFamily: "var(--havn-font-mono)", color: "var(--havn-text-secondary)", fontSize: "11px", flexShrink: 0, minWidth: "40px" },
  detailRows: { fontFamily: "var(--havn-font-mono)", color: "var(--havn-text-secondary)", fontSize: "11px", flexShrink: 0, minWidth: "60px" },
  delta: { fontSize: "11px", fontFamily: "var(--havn-font-mono)", flexShrink: 0 },
  detailError: { color: "var(--havn-red)", fontSize: "11px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "300px" },

  // Flat table view
  tableWrap: { flex: 1, overflow: "auto" },
  table: { width: "100%", borderCollapse: "collapse", fontSize: "12px" },
  th: { textAlign: "left", padding: "6px 12px", borderBottom: "2px solid var(--havn-border-light)", color: "var(--havn-text-secondary)", fontSize: "11px", fontWeight: 600, position: "sticky", top: 0, background: "var(--havn-bg-secondary)", userSelect: "none" },
  td: { padding: "5px 12px", borderBottom: "1px solid var(--havn-border)", color: "var(--havn-text)", fontSize: "12px" },
  typeBadge: { background: "var(--havn-btn-bg)", padding: "2px 8px", borderRadius: "var(--havn-radius)", fontSize: "11px", fontWeight: 500, textTransform: "capitalize" },
  fileLink: { cursor: "pointer", transition: "color 0.15s" },
};
