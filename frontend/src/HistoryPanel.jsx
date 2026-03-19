import React, { useState, useEffect } from "react";
import { api } from "./api";

const PAGE_SIZE = 50;

export default function HistoryPanel({ onOpenFile }) {
  const [history, setHistory] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [statusFilter, setStatusFilter] = useState("all");
  const [targetFilter, setTargetFilter] = useState("");
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);

  useEffect(() => {
    loadHistory();
  }, []);

  async function loadHistory() {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getHistory(500);
      setHistory(data);
    } catch (e) {
      setError(e.message || "Failed to load history");
    }
    setLoading(false);
  }

  const filtered = history.filter((row) => {
    if (statusFilter !== "all" && row.status !== statusFilter) return false;
    if (targetFilter && !(row.target || "").toLowerCase().includes(targetFilter.toLowerCase())) return false;
    return true;
  });

  const visible = filtered.slice(0, visibleCount);
  const hasMore = visibleCount < filtered.length;

  return (
    <div style={styles.container}>
      <div style={styles.header}>
        <select
          style={styles.filterSelect}
          value={statusFilter}
          onChange={(e) => { setStatusFilter(e.target.value); setVisibleCount(PAGE_SIZE); }}
        >
          <option value="all">All Statuses</option>
          <option value="success">Success</option>
          <option value="error">Error</option>
        </select>
        <input
          style={styles.filterInput}
          placeholder="Search target..."
          value={targetFilter}
          onChange={(e) => { setTargetFilter(e.target.value); setVisibleCount(PAGE_SIZE); }}
        />
        <span style={{ fontSize: "11px", color: "var(--havn-text-dim)", marginLeft: "auto", marginRight: 8 }}>
          {filtered.length} run{filtered.length !== 1 ? "s" : ""}
        </span>
        <button onClick={loadHistory} style={styles.refreshBtn}>
          Refresh
        </button>
      </div>
      {loading && <div style={styles.loading}>Loading...</div>}
      {!loading && error && (
        <div style={styles.error}>{error}</div>
      )}
      {!loading && !error && history.length === 0 && (
        <div style={styles.empty}>No runs yet. Execute a pipeline to see history here.</div>
      )}
      {!loading && history.length > 0 && (
        <div style={styles.tableWrap}>
          <table style={styles.table}>
            <thead>
              <tr>
                <th style={styles.th}>Type</th>
                <th style={styles.th}>Target</th>
                <th style={styles.th}>Status</th>
                <th style={styles.th}>Time</th>
                <th style={styles.th}>Duration</th>
                <th style={styles.th}>Rows</th>
                <th style={styles.th}>Error</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((row) => (
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
                        onClick={() => onOpenFile(row.target, 1, 1)}
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
                      background: row.status === "success"
                        ? "color-mix(in srgb, var(--havn-green) 12%, transparent)"
                        : "color-mix(in srgb, var(--havn-red) 12%, transparent)",
                      color: row.status === "success" ? "var(--havn-green)" : "var(--havn-red)",
                    }}>{row.status}</span>
                  </td>
                  <td style={{ ...styles.td, color: "var(--havn-text-secondary)" }}>
                    {row.started_at ? row.started_at.slice(0, 19).replace("T", " ") : ""}
                  </td>
                  <td style={{ ...styles.td, textAlign: "right", fontFamily: "var(--havn-font-mono)" }}>
                    {row.duration_ms != null ? `${row.duration_ms}ms` : ""}
                  </td>
                  <td style={{ ...styles.td, textAlign: "right", fontFamily: "var(--havn-font-mono)" }}>
                    {row.rows_affected || ""}
                  </td>
                  <td style={{ ...styles.td, color: "var(--havn-red)", maxWidth: "300px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {row.error || ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {hasMore && (
            <div style={{ textAlign: "center", padding: "12px" }}>
              <button
                onClick={() => setVisibleCount((c) => c + PAGE_SIZE)}
                style={styles.refreshBtn}
              >
                Load More ({filtered.length - visibleCount} remaining)
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const styles = {
  container: { display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" },
  header: { display: "flex", alignItems: "center", justifyContent: "space-between", padding: "8px 12px", borderBottom: "1px solid var(--havn-border)" },
  headerTitle: { fontWeight: 600, fontSize: "13px" },
  refreshBtn: { background: "var(--havn-btn-bg)", border: "1px solid var(--havn-btn-border)", borderRadius: "var(--havn-radius-lg)", color: "var(--havn-text)", padding: "4px 12px", cursor: "pointer", fontSize: "11px", fontWeight: 500 },
  loading: { padding: "24px", color: "var(--havn-text-secondary)", textAlign: "center" },
  empty: { padding: "24px", color: "var(--havn-text-dim)", textAlign: "center" },
  error: { padding: "24px", color: "var(--havn-red)", textAlign: "center" },
  tableWrap: { flex: 1, overflow: "auto" },
  table: { width: "100%", borderCollapse: "collapse", fontSize: "12px" },
  filterSelect: { padding: "4px 8px", background: "var(--havn-bg-tertiary)", color: "var(--havn-text)", border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius)", fontSize: "11px" },
  filterInput: { padding: "4px 10px", background: "var(--havn-bg-tertiary)", color: "var(--havn-text)", border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius)", fontSize: "11px", width: "180px" },
  th: { textAlign: "left", padding: "6px 12px", borderBottom: "2px solid var(--havn-border-light)", color: "var(--havn-text-secondary)", fontWeight: 600, position: "sticky", top: 0, background: "var(--havn-bg-secondary)" },
  td: { padding: "5px 12px", borderBottom: "1px solid var(--havn-border)", color: "var(--havn-text)", fontSize: "12px" },
  typeBadge: { background: "var(--havn-btn-bg)", padding: "2px 8px", borderRadius: "var(--havn-radius)", fontSize: "11px", fontWeight: 500, textTransform: "capitalize" },
  statusBadge: { padding: "2px 8px", borderRadius: "var(--havn-radius)", fontSize: "11px", fontWeight: 600 },
  fileLink: { cursor: "pointer", transition: "color 0.15s" },
};
