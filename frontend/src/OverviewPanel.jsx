import React, { useState, useEffect } from "react";
import { api } from "./api";
import { useHintTriggerFn } from "./HintSystem";

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
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function formatRows(n) {
  if (n == null) return "-";
  return n.toLocaleString();
}

export default function OverviewPanel({ onNavigate, onSelectTable, onOpenFile, onRunStream, streams, showConfirm, onClearSample, refreshKey }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);
  const [runningDemo, setRunningDemo] = useState(false);
  const [clearingProject, setClearingProject] = useState(false);
  const [gitStatus, setGitStatus] = useState(null);
  const [showFailed, setShowFailed] = useState(false);
  const setHintTrigger = useHintTriggerFn();

  useEffect(() => {
    load();
    loadGitStatus();
  }, [refreshKey]);

  async function load() {
    setLoading(true);
    setLoadError(null);
    try {
      const overview = await api.getOverview();
      setData(overview);
      if (overview.project_name) {
        document.title = `${overview.project_name} | havn`;
      }
      // Set hint trigger if warehouse has data but no runs
      const runs = overview.recent_runs || [];
      if (runs.length === 0 && overview.has_data) {
        setHintTrigger("overviewNoRuns", true);
      }
    } catch (e) {
      console.error("Failed to load overview:", e);
      setLoadError(e.message || "Failed to load overview");
    } finally {
      setLoading(false);
    }
  }

  async function loadGitStatus() {
    try {
      const gs = await api.getGitStatus();
      setGitStatus(gs);
      if (gs && gs.is_git_repo) {
        setHintTrigger("gitDetected", true);
        if (gs.dirty) setHintTrigger("gitDirty", true);
      }
    } catch (e) {
      // Git status not critical
    }
  }

  if (loading) {
    return <div style={st.container}><div style={st.center}>Loading overview...</div></div>;
  }

  if (!data) {
    return (
      <div style={st.container}>
        <div style={st.center}>
          <div style={{ textAlign: "center" }}>
            <div style={{ marginBottom: "8px", color: "var(--havn-text-secondary)" }}>
              {loadError || "Failed to load overview."}
            </div>
            <button
              onClick={load}
              style={{
                padding: "6px 16px",
                background: "var(--havn-btn-bg)",
                border: "1px solid var(--havn-btn-border)",
                borderRadius: "var(--havn-radius-lg)",
                color: "var(--havn-text)",
                cursor: "pointer",
                fontSize: "12px",
                fontWeight: 500,
              }}
            >
              Retry
            </button>
          </div>
        </div>
      </div>
    );
  }

  const recentRuns = data.recent_runs || [];
  const successCount = recentRuns.filter((r) => r.status === "success").length;
  const failedRuns = recentRuns.filter((r) => r.status !== "success");
  const errorCount = failedRuns.length;
  const schemas = data.schemas || [];
  const lastRun = recentRuns[0];

  return (
    <div style={st.container}>
      <div style={st.scrollArea}>
        {/* Hero / CTA section */}
        {!data.has_data && (
          <div style={st.hero}>
            <img src="/logo.svg" alt="havn" width="48" height="48" style={{ marginBottom: "12px" }} />
            <div style={st.heroTitle}>Get started with havn</div>
            <div style={st.heroDesc}>
              Connect a data source to start building your warehouse. Import a CSV, connect a database, or set up a recurring connector.
            </div>
            <div style={st.heroBtns}>
              <button onClick={() => onNavigate("Data Sources")} style={st.heroCta}>
                Connect Your Data
              </button>
              {streams && Object.keys(streams).length > 0 && onRunStream && (
                <button
                  onClick={() => {
                    setRunningDemo(true);
                    onRunStream(null, true);
                  }}
                  disabled={runningDemo}
                  style={st.heroSecondary}
                >
                  {runningDemo ? "Running..." : "Run Sample Pipeline"}
                </button>
              )}
            </div>
          </div>
        )}

        {/* Sample project banner */}
        {data.is_sample && onClearSample && (
          <div style={st.sampleBanner}>
            <span>You're running the sample earthquake project.</span>
            <button
              onClick={async () => {
                if (!showConfirm) { onClearSample(); return; }
                const ok = await showConfirm(
                  "Start Fresh",
                  "This will delete all sample files and the warehouse database. Your config files, .env, and .gitignore will be kept.",
                  "Clear Sample Data",
                  true
                );
                if (ok) {
                  setClearingProject(true);
                  try {
                    await onClearSample();
                    await load();
                  } finally {
                    setClearingProject(false);
                  }
                }
              }}
              disabled={clearingProject}
              style={st.sampleBannerBtn}
            >
              {clearingProject ? "Clearing..." : "Start fresh"}
            </button>
          </div>
        )}

        {/* Stats bar */}
        <div style={st.statsBar}>
          <div style={st.statItem}>
            <span style={st.statLabel}>Tables</span>
            <span style={st.statValue}>{data.total_tables}</span>
          </div>
          <span style={st.statDivider} />
          <div style={st.statItem}>
            <span style={st.statLabel}>Rows</span>
            <span style={st.statValue}>{formatRows(data.total_rows)}</span>
          </div>
          <span style={st.statDivider} />
          <div style={st.statItem}>
            <span style={st.statLabel}>Connectors</span>
            <span style={st.statValue}>{data.connectors}</span>
          </div>
          <span style={st.statDivider} />
          <div
            style={{ ...st.statItem, cursor: errorCount > 0 ? "pointer" : "default" }}
            onClick={() => { if (errorCount > 0) setShowFailed(!showFailed); }}
            title={errorCount > 0 ? "Click to show failures" : ""}
          >
            <span style={st.statLabel}>Runs</span>
            <span style={{
              ...st.statValue,
              color: errorCount > 0 ? "var(--havn-red)" : recentRuns.length > 0 ? "var(--havn-green)" : "var(--havn-text)",
            }}>
              {recentRuns.length > 0 ? `${successCount}/${recentRuns.length}` : "-"}
            </span>
            {errorCount > 0 && (
              <span style={{ fontSize: "11px", color: "var(--havn-red)", marginLeft: "2px", cursor: "pointer" }}>
                {"\u25BE"}
              </span>
            )}
          </div>
        </div>

        {/* Failed runs detail */}
        {showFailed && failedRuns.length > 0 && (
          <div style={st.failedCard}>
            <div style={st.failedHeader}>
              <span style={st.failedTitle}>Failed Runs</span>
              <button onClick={() => setShowFailed(false)} style={st.failedClose}>{"\u00D7"}</button>
            </div>
            {failedRuns.map((run) => (
              <div key={run.run_id} style={st.failedItem}>
                <span style={st.failedDot} />
                <span style={st.failedType}>{run.run_type}</span>
                <span
                  style={st.failedTarget}
                  onClick={() => {
                    const target = run.target || "";
                    if (run.run_type === "contract") {
                      // Pass contract name or model as filter, e.g. "daily_sales_positive:silver.fct_daily_sales"
                      const filterVal = target.includes(":") ? target.split(":")[0] : target;
                      onNavigate("Quality:Contracts:" + filterVal);
                    } else if (run.run_type === "transform" && target.includes(".")) {
                      const [s, t] = target.split(".", 2);
                      onSelectTable(s, t);
                    } else if (run.run_type === "ingest" || run.run_type === "export") {
                      onOpenFile(run.run_type + "/" + target);
                    }
                  }}
                >{run.target}</span>
                <span style={st.failedTime}>{timeAgo(run.started_at)}</span>
                {run.error && (
                  <span style={st.failedError} title={run.error}>{run.error}</span>
                )}
              </div>
            ))}
          </div>
        )}

        {/* Git status */}
        {gitStatus && gitStatus.is_git_repo && (
          <div data-havn-hint="git-status" style={{ ...st.card, marginBottom: 16, padding: "10px 16px", display: "flex", alignItems: "center", gap: 12, fontSize: 12 }}>
            <span style={{ fontWeight: 600, color: "var(--havn-text-secondary)" }}>git</span>
            <span style={{ fontFamily: "var(--havn-font-mono)", color: "var(--havn-accent)" }}>{gitStatus.branch}</span>
            <span style={{ color: gitStatus.dirty ? "var(--havn-yellow, #eab308)" : "var(--havn-green)", fontWeight: 500 }}>
              {gitStatus.dirty ? `${gitStatus.changed_files?.length || 0} uncommitted` : "clean"}
            </span>
            {gitStatus.last_message && (
              <span style={{ color: "var(--havn-text-dim)", marginLeft: "auto", maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {gitStatus.last_message}
              </span>
            )}
          </div>
        )}

        <div style={st.columns}>
          {/* Left column */}
          <div style={st.column}>
            {/* Pipeline health */}
            <div style={st.cardPrimary} data-havn-hint="pipeline-health">
              <div style={st.cardHeader}>
                <span style={st.cardTitle}>Pipeline Health</span>
                {lastRun && (
                  <span style={st.cardSubtitle}>Last run {timeAgo(lastRun.started_at)}</span>
                )}
              </div>
              {recentRuns.length === 0 ? (
                <div style={st.emptyState}>
                  No pipeline runs yet. Run a stream or transform to see activity here.
                </div>
              ) : (
                <div style={st.runList}>
                  {(() => {
                    const typeOrder = { seed: 0, ingest: 1, transform: 2, export: 3, contract: 4 };
                    const sorted = [...recentRuns].slice(0, 12).sort((a, b) => (typeOrder[a.run_type] ?? 9) - (typeOrder[b.run_type] ?? 9));
                    let lastType = null;
                    return sorted.map((run) => {
                      const showSep = lastType !== null && lastType !== run.run_type;
                      lastType = run.run_type;
                      return (
                        <React.Fragment key={run.run_id}>
                          {showSep && <div style={st.runGroupSep} />}
                          <div style={{
                            ...st.runItem,
                            ...(run.run_type === "ingest" || run.run_type === "export" ? { background: "color-mix(in srgb, var(--havn-accent) 4%, transparent)" } : {}),
                          }}>
                      <span style={{
                        ...st.statusDot,
                        background: run.status === "success" ? "var(--havn-green)" : "var(--havn-red)",
                      }} />
                      <span style={st.runType}>{run.run_type}</span>
                      <span
                        style={{ ...st.runTarget, cursor: "pointer" }}
                        onClick={() => {
                          const target = run.target || "";
                          if (run.run_type === "contract") {
                            const filterVal = target.includes(":") ? target.split(":")[0] : target;
                            onNavigate("Quality:Contracts:" + filterVal);
                          } else if (run.run_type === "transform" && target.includes(".")) {
                            const [s, t] = target.split(".", 2);
                            onSelectTable(s, t);
                          } else if (run.run_type === "ingest") {
                            onOpenFile("ingest/" + target);
                          } else if (run.run_type === "export") {
                            onOpenFile("export/" + target);
                          }
                        }}
                      >{run.target}</span>
                      <span style={st.runMeta}>
                        {run.rows_affected > 0 && <span>{formatRows(run.rows_affected)} rows</span>}
                        {run.duration_ms > 0 && <span>{run.duration_ms}ms</span>}
                      </span>
                      <span style={st.runTime}>{timeAgo(run.started_at)}</span>
                          </div>
                        </React.Fragment>
                      );
                    });
                  })()}
                </div>
              )}
              {recentRuns.length > 0 && (
                <button onClick={() => onNavigate("Runs")} style={st.cardLink}>
                  View all runs
                </button>
              )}
            </div>
          </div>

          {/* Right column */}
          <div style={st.column}>
            {/* Warehouse summary */}
            <div style={st.card}>
              <div style={st.cardHeader}>
                <span style={st.cardTitle}>Warehouse</span>
              </div>
              {schemas.length === 0 ? (
                <div style={st.emptyState}>
                  No data in the warehouse yet.
                </div>
              ) : (
                <div style={st.schemaList}>
                  {schemas.map((s) => (
                    <div key={s.name} style={st.schemaItem}>
                      <span
                        style={{ ...st.schemaName, cursor: "pointer" }}
                        onClick={() => onNavigate("Tables")}
                      >{s.name}</span>
                      <span style={st.schemaStat}>{s.tables} table{s.tables !== 1 ? "s" : ""}</span>
                      {s.views > 0 && <span style={st.schemaStat}>{s.views} view{s.views !== 1 ? "s" : ""}</span>}
                      <span style={st.schemaRows}>{formatRows(s.total_rows)} rows</span>
                    </div>
                  ))}
                </div>
              )}
              {schemas.length > 0 && (
                <button onClick={() => onNavigate("Tables")} style={st.cardLink}>
                  Browse tables
                </button>
              )}
            </div>

            {/* Quick actions */}
            <div style={st.quickActionsCard}>
              <div style={st.quickActions}>
                <button onClick={() => onNavigate("Data Sources")} style={st.quickAction}>
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><line x1="8" y1="3" x2="8" y2="13" /><line x1="3" y1="8" x2="13" y2="8" /></svg>
                  <span>Add Data Source</span>
                </button>
                <button onClick={() => onNavigate("Query")} style={st.quickAction}>
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="5,3 11,8 5,13" /></svg>
                  <span>Run a Query</span>
                </button>
                <button onClick={() => onNavigate("Editor")} style={st.quickAction}>
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><path d="M11.5,2.5 L13.5,4.5 L5.5,12.5 L2.5,13.5 L3.5,10.5 Z" /><line x1="9.5" y1="4.5" x2="11.5" y2="6.5" /></svg>
                  <span>Edit Transforms</span>
                </button>
                <button onClick={() => onNavigate("DAG")} style={st.quickAction}>
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><circle cx="4" cy="4" r="2" /><circle cx="12" cy="8" r="2" /><circle cx="4" cy="12" r="2" /><line x1="6" y1="4.5" x2="10" y2="7.5" /><line x1="6" y1="11.5" x2="10" y2="8.5" /></svg>
                  <span>View DAG</span>
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

const st = {
  container: { display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" },
  center: { display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "var(--havn-text-secondary)", fontSize: "13px" },
  scrollArea: { flex: 1, overflow: "auto", padding: "20px 24px" },

  // Hero
  hero: {
    background: "linear-gradient(135deg, var(--havn-bg-secondary), var(--havn-bg-tertiary))",
    border: "1px solid var(--havn-border)",
    borderRadius: "var(--havn-radius-lg)",
    padding: "32px",
    textAlign: "center",
    marginBottom: "20px",
  },
  heroTitle: { fontSize: "20px", fontWeight: 700, color: "var(--havn-text)", marginBottom: "8px" },
  heroDesc: { fontSize: "14px", color: "var(--havn-text-secondary)", lineHeight: 1.6, maxWidth: "480px", margin: "0 auto 20px" },
  heroBtns: { display: "flex", flexDirection: "column", alignItems: "center", gap: "10px" },
  heroCta: {
    padding: "10px 28px",
    background: "var(--havn-green)",
    border: "1px solid var(--havn-green-border)",
    borderRadius: "var(--havn-radius-lg)",
    color: "#fff",
    cursor: "pointer",
    fontSize: "14px",
    fontWeight: 600,
  },
  heroSecondary: {
    padding: "10px 28px",
    background: "none",
    border: "1px solid var(--havn-border)",
    borderRadius: "var(--havn-radius-lg)",
    color: "var(--havn-text-secondary)",
    cursor: "pointer",
    fontSize: "14px",
    fontWeight: 600,
  },

  // Sample banner
  sampleBanner: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "10px 16px",
    background: "var(--havn-bg-secondary)",
    border: "1px solid var(--havn-border)",
    borderRadius: "var(--havn-radius-lg)",
    marginBottom: "16px",
    fontSize: "13px",
    color: "var(--havn-text-secondary)",
  },
  sampleBannerBtn: {
    padding: "5px 14px",
    background: "none",
    border: "1px solid var(--havn-border)",
    borderRadius: "var(--havn-radius)",
    color: "var(--havn-text-secondary)",
    cursor: "pointer",
    fontSize: "12px",
    fontWeight: 500,
    flexShrink: 0,
    marginLeft: "12px",
  },

  // Stats — compact horizontal bar instead of hero metric cards
  statsBar: {
    display: "flex",
    alignItems: "center",
    gap: "0",
    background: "var(--havn-bg-secondary)",
    border: "1px solid var(--havn-border)",
    borderRadius: "var(--havn-radius-lg)",
    padding: "10px 20px",
    marginBottom: "20px",
  },
  statItem: {
    display: "flex",
    alignItems: "center",
    gap: "6px",
    padding: "0 16px",
  },
  statValue: { fontSize: "14px", fontWeight: 600, color: "var(--havn-text)", fontFamily: "var(--havn-font-mono)" },
  statLabel: { fontSize: "12px", color: "var(--havn-text-secondary)", fontWeight: 500 },
  statDivider: {
    width: "1px",
    height: "18px",
    background: "var(--havn-border)",
    flexShrink: 0,
  },

  // Failed runs
  failedCard: {
    background: "var(--havn-bg-secondary)",
    border: "1px solid var(--havn-red, #c53030)",
    borderRadius: "var(--havn-radius-lg)",
    marginBottom: "20px",
    overflow: "hidden",
  },
  failedHeader: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "8px 16px",
    borderBottom: "1px solid var(--havn-border)",
  },
  failedTitle: { fontSize: "12px", fontWeight: 600, color: "var(--havn-red, #c53030)" },
  failedClose: { background: "none", border: "none", color: "var(--havn-text-dim)", cursor: "pointer", fontSize: "16px", lineHeight: 1, padding: "0 4px" },
  failedItem: {
    display: "flex",
    alignItems: "center",
    gap: "8px",
    padding: "8px 16px",
    fontSize: "12px",
    borderBottom: "1px solid var(--havn-border)",
    flexWrap: "wrap",
  },
  failedDot: { width: "6px", height: "6px", borderRadius: "50%", background: "var(--havn-red, #c53030)", flexShrink: 0 },
  failedType: {
    fontSize: "10px",
    fontWeight: 600,
    color: "var(--havn-text-secondary)",
    background: "var(--havn-bg-tertiary)",
    padding: "1px 6px",
    borderRadius: "var(--havn-radius)",
    textTransform: "uppercase",
    flexShrink: 0,
  },
  failedTarget: {
    fontFamily: "var(--havn-font-mono)",
    color: "var(--havn-accent)",
    cursor: "pointer",
    fontWeight: 500,
  },
  failedTime: { color: "var(--havn-text-dim)", fontSize: "11px", flexShrink: 0 },
  failedError: {
    color: "var(--havn-red, #c53030)",
    fontSize: "11px",
    width: "100%",
    paddingLeft: "14px",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },

  // Layout — asymmetric: Pipeline Health gets more space
  columns: { display: "grid", gridTemplateColumns: "3fr 2fr", gap: "16px" },
  column: { display: "flex", flexDirection: "column", gap: "16px" },

  // Cards — base style
  card: {
    background: "var(--havn-bg-secondary)",
    border: "1px solid var(--havn-border)",
    borderRadius: "var(--havn-radius-lg)",
    overflow: "hidden",
  },
  // Primary card — subtle accent treatment for Pipeline Health
  cardPrimary: {
    background: "var(--havn-bg-secondary)",
    border: "1px solid var(--havn-border)",
    borderRadius: "var(--havn-radius-lg)",
    overflow: "hidden",
    borderTop: "2px solid var(--havn-accent)",
  },
  cardHeader: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "12px 16px",
    borderBottom: "1px solid var(--havn-border)",
  },
  cardTitle: { fontSize: "13px", fontWeight: 600, color: "var(--havn-text)" },
  cardSubtitle: { fontSize: "11px", color: "var(--havn-text-dim)" },
  cardLink: {
    display: "block",
    width: "100%",
    padding: "8px 16px",
    background: "none",
    border: "none",
    color: "var(--havn-accent)",
    cursor: "pointer",
    fontSize: "12px",
    fontWeight: 500,
    textAlign: "left",
  },

  emptyState: {
    padding: "20px 16px",
    color: "var(--havn-text-dim)",
    fontSize: "13px",
    textAlign: "center",
    lineHeight: 1.5,
  },

  // Runs
  runList: { padding: "0" },
  runGroupSep: { display: "none" },
  runItem: {
    display: "flex",
    alignItems: "center",
    gap: "8px",
    padding: "7px 16px",
    fontSize: "12.5px",
    borderBottom: "1px solid var(--havn-border)",
  },
  statusDot: { width: "6px", height: "6px", borderRadius: "50%", flexShrink: 0 },
  runType: {
    fontSize: "10px",
    fontWeight: 600,
    color: "var(--havn-text-secondary)",
    background: "var(--havn-bg-tertiary)",
    padding: "1px 6px",
    borderRadius: "var(--havn-radius)",
    textTransform: "uppercase",
    flexShrink: 0,
  },
  runTarget: {
    fontFamily: "var(--havn-font-mono)",
    color: "var(--havn-accent)",
    flex: 1,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  runMeta: {
    display: "flex",
    gap: "8px",
    color: "var(--havn-text-dim)",
    fontSize: "11px",
    fontFamily: "var(--havn-font-mono)",
    flexShrink: 0,
  },
  runTime: { color: "var(--havn-text-dim)", fontSize: "11px", flexShrink: 0 },

  // Schemas
  schemaList: { padding: "0" },
  schemaItem: {
    display: "flex",
    alignItems: "center",
    gap: "10px",
    padding: "7px 16px",
    fontSize: "12.5px",
    borderBottom: "1px solid var(--havn-border)",
  },
  schemaName: {
    fontWeight: 600,
    fontFamily: "var(--havn-font-mono)",
    color: "var(--havn-accent)",
    minWidth: "70px",
  },
  schemaStat: { color: "var(--havn-text-secondary)", fontSize: "12px" },
  schemaRows: {
    marginLeft: "auto",
    fontFamily: "var(--havn-font-mono)",
    color: "var(--havn-text-dim)",
    fontSize: "12px",
  },

  // Quick actions — minimal, no card chrome
  quickActionsCard: {
    overflow: "hidden",
  },
  quickActions: {
    display: "flex",
    flexDirection: "column",
    gap: "0",
  },
  quickAction: {
    display: "flex",
    alignItems: "center",
    gap: "10px",
    padding: "9px 12px",
    background: "none",
    border: "none",
    borderBottom: "1px solid var(--havn-border)",
    color: "var(--havn-text-secondary)",
    cursor: "pointer",
    fontSize: "12.5px",
    fontWeight: 500,
    textAlign: "left",
  },
};
