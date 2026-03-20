import React, { useState, useEffect, useCallback } from "react";
import { api } from "./api";

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

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

const STATUS_LABELS = { M: "Modified", A: "Added", D: "Deleted", R: "Renamed", C: "Copied", U: "Untracked" };
const STATUS_COLORS = {
  M: "var(--havn-yellow, #eab308)",
  A: "var(--havn-green)",
  D: "var(--havn-red)",
  R: "var(--havn-purple)",
  C: "var(--havn-purple)",
  U: "var(--havn-text-dim)",
};

/* ------------------------------------------------------------------ */
/* GitPanel                                                            */
/* ------------------------------------------------------------------ */

export default function GitPanel() {
  // Data state
  const [status, setStatus] = useState(null);
  const [branches, setBranches] = useState([]);
  const [remoteUrl, setRemoteUrl] = useState(null);
  const [log, setLog] = useState([]);
  const [stashes, setStashes] = useState([]);
  const [diffText, setDiffText] = useState("");
  const [diffFile, setDiffFile] = useState(null);

  // UI state
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [commitMsg, setCommitMsg] = useState("");
  const [pulling, setPulling] = useState(false);
  const [pushing, setPushing] = useState(false);
  const [committing, setCommitting] = useState(false);
  const [branchDropdown, setBranchDropdown] = useState(false);
  const [newBranchName, setNewBranchName] = useState("");
  const [creatingBranch, setCreatingBranch] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [showStash, setShowStash] = useState(false);
  const [stashMsg, setStashMsg] = useState("");
  const [actionError, setActionError] = useState(null);

  // Load all data
  const refresh = useCallback(async () => {
    setError(null);
    setActionError(null);
    try {
      const [s, b, r, l, st] = await Promise.all([
        api.getGitStatus(),
        api.getGitBranches(),
        api.getGitRemote(),
        api.getGitLog(30),
        api.getGitStash(),
      ]);
      setStatus(s);
      setBranches(b);
      setRemoteUrl(r?.url || null);
      setLog(l);
      setStashes(st);
    } catch (e) {
      setError(e.message || "Failed to load git status");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // Derived data
  const stagedFiles = (status?.files || []).filter((f) => f.staged);
  const unstagedFiles = (status?.files || []).filter((f) => !f.staged);
  const localBranches = branches.filter((b) => !b.is_remote);
  const currentBranch = status?.branch || "unknown";

  // Actions
  async function handleStage(files) {
    setActionError(null);
    try {
      await api.gitStage(files);
      await refresh();
    } catch (e) { setActionError(e.message); }
  }

  async function handleUnstage(files) {
    setActionError(null);
    try {
      await api.gitUnstage(files);
      await refresh();
    } catch (e) { setActionError(e.message); }
  }

  async function handleDiscard(files) {
    setActionError(null);
    try {
      await api.gitDiscard(files);
      await refresh();
    } catch (e) { setActionError(e.message); }
  }

  async function handleCommit() {
    if (!commitMsg.trim() || stagedFiles.length === 0) return;
    setCommitting(true);
    setActionError(null);
    try {
      await api.gitCommit(commitMsg.trim());
      setCommitMsg("");
      await refresh();
    } catch (e) { setActionError(e.message); }
    finally { setCommitting(false); }
  }

  async function handlePull() {
    setPulling(true);
    setActionError(null);
    try {
      const r = await api.gitPull();
      if (!r.success) setActionError(r.error || "Pull failed");
      else await refresh();
    } catch (e) { setActionError(e.message); }
    finally { setPulling(false); }
  }

  async function handlePush() {
    setPushing(true);
    setActionError(null);
    try {
      const r = await api.gitPush();
      if (!r.success) setActionError(r.error || "Push failed");
      else await refresh();
    } catch (e) { setActionError(e.message); }
    finally { setPushing(false); }
  }

  async function handleCheckout(branch) {
    setActionError(null);
    setBranchDropdown(false);
    try {
      await api.gitCheckout(branch);
      await refresh();
    } catch (e) { setActionError(e.message); }
  }

  async function handleCreateBranch() {
    if (!newBranchName.trim()) return;
    setCreatingBranch(true);
    setActionError(null);
    try {
      await api.gitCreateBranch(newBranchName.trim(), true);
      setNewBranchName("");
      setBranchDropdown(false);
      await refresh();
    } catch (e) { setActionError(e.message); }
    finally { setCreatingBranch(false); }
  }

  async function handleDeleteBranch(name) {
    setActionError(null);
    try {
      await api.gitDeleteBranch(name);
      await refresh();
    } catch (e) { setActionError(e.message); }
  }

  async function handleStashSave() {
    setActionError(null);
    try {
      await api.gitStashSave(stashMsg.trim() || undefined);
      setStashMsg("");
      await refresh();
    } catch (e) { setActionError(e.message); }
  }

  async function handleStashPop() {
    setActionError(null);
    try {
      await api.gitStashPop();
      await refresh();
    } catch (e) { setActionError(e.message); }
  }

  async function handleViewDiff(path) {
    if (diffFile === path) {
      setDiffFile(null);
      setDiffText("");
      return;
    }
    setDiffFile(path);
    try {
      const d = await api.getGitDiff(path);
      setDiffText(d?.diff || "No diff available");
    } catch {
      setDiffText("Failed to load diff");
    }
  }

  // Render
  if (loading) return <div style={st.container}><div style={st.center}>Loading git status...</div></div>;
  if (error) return <div style={st.container}><div style={st.center}><span style={{ color: "var(--havn-red)" }}>{error}</span></div></div>;
  if (!status?.is_git_repo) return <div style={st.container}><div style={st.center}>Not a git repository. Run <code>git init</code> to get started.</div></div>;

  return (
    <div style={st.container}>
      <div style={st.scrollArea}>
        {/* Header bar */}
        <div style={st.headerBar}>
          <div style={st.headerLeft}>
            <div style={{ position: "relative" }}>
              <button onClick={() => setBranchDropdown(!branchDropdown)} style={st.branchBtn} aria-label={`Current branch: ${currentBranch}. Switch branches`} aria-expanded={branchDropdown} aria-haspopup="true">
                <span style={st.branchIcon}>{"\u2387"}</span>
                <span style={st.branchName}>{currentBranch}</span>
                <span style={st.chevron}>{"\u25BE"}</span>
              </button>
              {branchDropdown && (
                <div style={st.dropdown}>
                  <div style={st.dropdownHeader}>Branches</div>
                  <div style={st.dropdownNewBranch}>
                    <input
                      type="text"
                      placeholder="New branch name..."
                      value={newBranchName}
                      onChange={(e) => setNewBranchName(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") handleCreateBranch(); }}
                      style={st.newBranchInput}
                      autoFocus
                      aria-label="New branch name"
                    />
                    <button onClick={handleCreateBranch} disabled={creatingBranch || !newBranchName.trim()} style={st.newBranchBtn}>
                      Create
                    </button>
                  </div>
                  <div style={st.dropdownScroll}>
                    {localBranches.map((b) => (
                      <div key={b.name} style={st.dropdownItem}>
                        <button
                          onClick={() => handleCheckout(b.name)}
                          style={{
                            ...st.dropdownItemBtn,
                            fontWeight: b.is_current ? 600 : 400,
                            color: b.is_current ? "var(--havn-accent)" : "var(--havn-text)",
                          }}
                          disabled={b.is_current}
                        >
                          {b.is_current ? "\u2713 " : "  "}{b.name}
                        </button>
                        {!b.is_current && (
                          <button
                            onClick={(e) => { e.stopPropagation(); handleDeleteBranch(b.name); }}
                            style={st.dropdownDeleteBtn}
                            title="Delete branch"
                            aria-label={`Delete branch ${b.name}`}
                          >{"\u00D7"}</button>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
            {remoteUrl && (
              <span style={st.remoteUrl} title={remoteUrl}>
                {remoteUrl.replace(/^https?:\/\//, "").replace(/\.git$/, "")}
              </span>
            )}
          </div>
          <div style={st.headerRight}>
            <button onClick={handlePull} disabled={pulling} style={st.actionBtn} aria-label="Pull from remote">
              {pulling ? "Pulling..." : "\u2193 Pull"}
            </button>
            <button onClick={handlePush} disabled={pushing} style={st.actionBtn} aria-label="Push to remote">
              {pushing ? "Pushing..." : "\u2191 Push"}
            </button>
            <button onClick={refresh} style={st.actionBtn} title="Refresh" aria-label="Refresh git status">
              {"\u21BB"}
            </button>
          </div>
        </div>

        {/* Error banner */}
        {actionError && (
          <div style={st.errorBanner}>
            <span>{actionError}</span>
            <button onClick={() => setActionError(null)} style={st.errorClose} aria-label="Dismiss error">{"\u00D7"}</button>
          </div>
        )}

        {/* Changes section */}
        <div style={st.section}>
          {/* Staged files */}
          <div style={st.sectionHeader}>
            <span style={st.sectionTitle}>Staged Changes</span>
            <span style={st.sectionCount}>{stagedFiles.length}</span>
            {stagedFiles.length > 0 && (
              <button onClick={() => handleUnstage(stagedFiles.map((f) => f.path))} style={st.sectionAction}>
                Unstage All
              </button>
            )}
          </div>
          {stagedFiles.length === 0 ? (
            <div style={st.emptyHint}>No staged changes</div>
          ) : (
            <div style={st.fileList}>
              {stagedFiles.map((f) => (
                <div key={"s:" + f.path} style={st.fileRow}>
                  <button onClick={() => handleUnstage([f.path])} style={st.stageBtn} title="Unstage">
                    {"\u2212"}
                  </button>
                  <span
                    style={{ ...st.filePath, cursor: "pointer" }}
                    onClick={() => handleViewDiff(f.path)}
                    title="View diff"
                  >{f.path}</span>
                  <span style={{ ...st.statusBadge, color: STATUS_COLORS[f.status] || "var(--havn-text-dim)" }}>
                    {f.status}
                  </span>
                </div>
              ))}
            </div>
          )}

          {/* Unstaged files */}
          <div style={{ ...st.sectionHeader, marginTop: 12 }}>
            <span style={st.sectionTitle}>Changes</span>
            <span style={st.sectionCount}>{unstagedFiles.length}</span>
            {unstagedFiles.length > 0 && (
              <button onClick={() => handleStage(unstagedFiles.map((f) => f.path))} style={st.sectionAction}>
                Stage All
              </button>
            )}
          </div>
          {unstagedFiles.length === 0 ? (
            <div style={st.emptyHint}>Working tree clean</div>
          ) : (
            <div style={st.fileList}>
              {unstagedFiles.map((f) => (
                <div key={"u:" + f.path} style={st.fileRow}>
                  <button onClick={() => handleStage([f.path])} style={{ ...st.stageBtn, color: "var(--havn-green)" }} title="Stage">
                    +
                  </button>
                  <span
                    style={{ ...st.filePath, cursor: "pointer" }}
                    onClick={() => handleViewDiff(f.path)}
                    title="View diff"
                  >{f.path}</span>
                  <span style={{ ...st.statusBadge, color: STATUS_COLORS[f.status] || "var(--havn-text-dim)" }}>
                    {f.status}
                  </span>
                  {f.status !== "U" && (
                    <button onClick={() => handleDiscard([f.path])} style={st.discardBtn} title="Discard changes">
                      {"\u21A9"}
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Diff viewer */}
        {diffFile && (
          <div style={st.diffSection}>
            <div style={st.diffHeader}>
              <span style={st.diffFileName}>{diffFile}</span>
              <button onClick={() => { setDiffFile(null); setDiffText(""); }} style={st.diffClose}>{"\u00D7"}</button>
            </div>
            <pre style={st.diffPre}>
              {diffText.split("\n").map((line, i) => {
                let color = "var(--havn-text-secondary)";
                let bg = "transparent";
                if (line.startsWith("+") && !line.startsWith("+++")) { color = "var(--havn-green)"; bg = "rgba(46,160,67,0.08)"; }
                else if (line.startsWith("-") && !line.startsWith("---")) { color = "var(--havn-red)"; bg = "rgba(248,81,73,0.08)"; }
                else if (line.startsWith("@@")) { color = "var(--havn-purple)"; }
                else if (line.startsWith("diff") || line.startsWith("index")) { color = "var(--havn-text-dim)"; }
                return <div key={i} style={{ color, background: bg, padding: "0 8px", minHeight: "18px" }}>{line || " "}</div>;
              })}
            </pre>
          </div>
        )}

        {/* Commit section */}
        <div style={st.commitSection}>
          <textarea
            value={commitMsg}
            onChange={(e) => setCommitMsg(e.target.value)}
            placeholder="Commit message..."
            style={st.commitTextarea}
            rows={3}
            onKeyDown={(e) => {
              if ((e.ctrlKey || e.metaKey) && e.key === "Enter") handleCommit();
            }}
          />
          <div style={st.commitFooter}>
            <span style={st.commitHint}>
              {stagedFiles.length} file{stagedFiles.length !== 1 ? "s" : ""} staged
              {commitMsg.trim() ? "" : " -- enter a message to commit"}
            </span>
            <button
              onClick={handleCommit}
              disabled={committing || !commitMsg.trim() || stagedFiles.length === 0}
              style={{
                ...st.commitBtn,
                opacity: (!commitMsg.trim() || stagedFiles.length === 0) ? 0.5 : 1,
              }}
            >
              {committing ? "Committing..." : "Commit"}
            </button>
          </div>
        </div>

        {/* History section (collapsible) */}
        <div style={st.collapsible}>
          <button onClick={() => setShowHistory(!showHistory)} style={st.collapsibleHeader}>
            <span style={{ ...st.collapseArrow, transform: showHistory ? "rotate(0deg)" : "rotate(-90deg)" }}>{"\u25BE"}</span>
            <span style={st.collapsibleTitle}>History</span>
            <span style={st.sectionCount}>{log.length}</span>
          </button>
          {showHistory && (
            <div style={st.historyList}>
              {log.length === 0 ? (
                <div style={st.emptyHint}>No commits yet</div>
              ) : log.map((c) => (
                <div key={c.hash} style={st.historyItem}>
                  <span style={st.commitHash}>{c.short_hash}</span>
                  <span style={st.commitMessage}>{c.message}</span>
                  <span style={st.commitMeta}>{c.author} {"\u00B7"} {timeAgo(c.date)}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Stash section (collapsible) */}
        <div style={st.collapsible}>
          <button onClick={() => setShowStash(!showStash)} style={st.collapsibleHeader}>
            <span style={{ ...st.collapseArrow, transform: showStash ? "rotate(0deg)" : "rotate(-90deg)" }}>{"\u25BE"}</span>
            <span style={st.collapsibleTitle}>Stash</span>
            <span style={st.sectionCount}>{stashes.length}</span>
          </button>
          {showStash && (
            <div style={st.stashSection}>
              <div style={st.stashActions}>
                <input
                  type="text"
                  placeholder="Stash message (optional)..."
                  value={stashMsg}
                  onChange={(e) => setStashMsg(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") handleStashSave(); }}
                  style={st.stashInput}
                />
                <button onClick={handleStashSave} style={st.actionBtn} disabled={!status?.dirty}>
                  Stash
                </button>
                <button onClick={handleStashPop} style={st.actionBtn} disabled={stashes.length === 0}>
                  Pop
                </button>
              </div>
              {stashes.length === 0 ? (
                <div style={st.emptyHint}>No stashes</div>
              ) : (
                <div style={st.stashList}>
                  {stashes.map((s) => (
                    <div key={s.index} style={st.stashItem}>
                      <span style={st.stashIndex}>stash@{"{" + s.index + "}"}</span>
                      <span style={st.stashMessage}>{s.message}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Styles                                                              */
/* ------------------------------------------------------------------ */

const st = {
  container: { display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" },
  center: { display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "var(--havn-text-secondary)", fontSize: "13px" },
  scrollArea: { flex: 1, overflow: "auto", padding: "0" },

  // Header bar
  headerBar: {
    display: "flex", alignItems: "center", justifyContent: "space-between",
    padding: "8px 12px", borderBottom: "1px solid var(--havn-border)", background: "var(--havn-bg-secondary)",
  },
  headerLeft: { display: "flex", alignItems: "center", gap: "12px", minWidth: 0 },
  headerRight: { display: "flex", alignItems: "center", gap: "6px", flexShrink: 0 },
  branchBtn: {
    display: "inline-flex", alignItems: "center", gap: "6px",
    padding: "4px 10px", background: "var(--havn-btn-bg)", border: "1px solid var(--havn-btn-border)",
    borderRadius: "var(--havn-radius-lg)", color: "var(--havn-text)", cursor: "pointer", fontSize: "12px", fontWeight: 500,
  },
  branchIcon: { fontSize: "14px", color: "var(--havn-accent)" },
  branchName: { fontFamily: "var(--havn-font-mono)", fontWeight: 600, color: "var(--havn-accent)" },
  chevron: { fontSize: "10px", color: "var(--havn-text-secondary)" },
  remoteUrl: {
    fontSize: "11px", color: "var(--havn-text-dim)", maxWidth: "260px",
    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
  },
  actionBtn: {
    padding: "4px 10px", background: "var(--havn-btn-bg)", border: "1px solid var(--havn-btn-border)",
    borderRadius: "var(--havn-radius-lg)", color: "var(--havn-text)", cursor: "pointer", fontSize: "11px", fontWeight: 500,
  },

  // Branch dropdown
  dropdown: {
    position: "absolute", top: "100%", left: 0, marginTop: "4px", zIndex: 100,
    background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)",
    borderRadius: "var(--havn-radius)", boxShadow: "0 4px 16px rgba(0,0,0,0.3)",
    minWidth: "240px", maxHeight: "320px", display: "flex", flexDirection: "column",
  },
  dropdownHeader: {
    padding: "6px 10px 4px", fontSize: "10px", fontWeight: 600,
    color: "var(--havn-text-dim)", textTransform: "uppercase", letterSpacing: "0.5px",
  },
  dropdownNewBranch: {
    display: "flex", gap: "4px", padding: "4px 8px 6px",
    borderBottom: "1px solid var(--havn-border)",
  },
  newBranchInput: {
    flex: 1, padding: "4px 8px", background: "var(--havn-bg)", border: "1px solid var(--havn-border)",
    borderRadius: "var(--havn-radius)", color: "var(--havn-text)", fontSize: "12px",
    fontFamily: "var(--havn-font-mono)", outline: "none",
  },
  newBranchBtn: {
    padding: "4px 8px", background: "var(--havn-green)", border: "1px solid var(--havn-green-border)",
    borderRadius: "var(--havn-radius)", color: "#fff", cursor: "pointer", fontSize: "11px", fontWeight: 600,
  },
  dropdownScroll: { flex: 1, overflow: "auto", padding: "4px 0" },
  dropdownItem: { display: "flex", alignItems: "center", padding: "0 4px" },
  dropdownItemBtn: {
    flex: 1, padding: "5px 8px", background: "none", border: "none",
    color: "var(--havn-text)", cursor: "pointer", fontSize: "12px",
    fontFamily: "var(--havn-font-mono)", textAlign: "left", borderRadius: "var(--havn-radius)",
  },
  dropdownDeleteBtn: {
    background: "none", border: "none", color: "var(--havn-text-dim)", cursor: "pointer",
    fontSize: "14px", padding: "2px 6px", lineHeight: 1, borderRadius: "var(--havn-radius)",
  },

  // Error banner
  errorBanner: {
    display: "flex", alignItems: "center", justifyContent: "space-between",
    padding: "6px 12px", background: "color-mix(in srgb, var(--havn-red) 12%, transparent)",
    color: "var(--havn-red)", fontSize: "12px", borderBottom: "1px solid var(--havn-border)",
  },
  errorClose: { background: "none", border: "none", color: "var(--havn-red)", cursor: "pointer", fontSize: "14px", padding: "0 4px" },

  // Sections
  section: { padding: "8px 12px" },
  sectionHeader: {
    display: "flex", alignItems: "center", gap: "8px", padding: "4px 0", marginBottom: "4px",
  },
  sectionTitle: { fontSize: "11px", fontWeight: 600, color: "var(--havn-text-secondary)", textTransform: "uppercase", letterSpacing: "0.5px" },
  sectionCount: {
    fontSize: "10px", color: "var(--havn-text-dim)", background: "var(--havn-btn-bg)",
    padding: "1px 6px", borderRadius: "10px", fontWeight: 600,
  },
  sectionAction: {
    marginLeft: "auto", padding: "2px 8px", background: "none", border: "1px solid var(--havn-border-light)",
    borderRadius: "var(--havn-radius)", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: "10px", fontWeight: 500,
  },

  // File list
  fileList: { },
  fileRow: {
    display: "flex", alignItems: "center", gap: "6px", padding: "3px 4px",
    borderRadius: "var(--havn-radius)", fontSize: "12px",
  },
  stageBtn: {
    width: "18px", height: "18px", display: "inline-flex", alignItems: "center", justifyContent: "center",
    background: "none", border: "1px solid var(--havn-border-light)", borderRadius: "3px",
    color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: "12px", fontWeight: 700, flexShrink: 0,
    lineHeight: 1,
  },
  filePath: {
    flex: 1, fontFamily: "var(--havn-font-mono)", fontSize: "12px", color: "var(--havn-text)",
    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
  },
  statusBadge: {
    fontSize: "10px", fontWeight: 700, fontFamily: "var(--havn-font-mono)", flexShrink: 0,
    width: "14px", textAlign: "center",
  },
  discardBtn: {
    background: "none", border: "none", color: "var(--havn-text-dim)", cursor: "pointer",
    fontSize: "12px", padding: "0 2px", flexShrink: 0,
  },
  emptyHint: { padding: "8px 4px", color: "var(--havn-text-dim)", fontSize: "12px" },

  // Diff viewer
  diffSection: {
    margin: "0 12px 8px", border: "1px solid var(--havn-border)",
    borderRadius: "var(--havn-radius-lg)", overflow: "hidden",
  },
  diffHeader: {
    display: "flex", alignItems: "center", justifyContent: "space-between",
    padding: "6px 10px", background: "var(--havn-bg-tertiary)",
    borderBottom: "1px solid var(--havn-border)",
  },
  diffFileName: { fontFamily: "var(--havn-font-mono)", fontSize: "12px", color: "var(--havn-text-secondary)" },
  diffClose: { background: "none", border: "none", color: "var(--havn-text-dim)", cursor: "pointer", fontSize: "14px" },
  diffPre: {
    margin: 0, padding: "4px 0", overflow: "auto", maxHeight: "350px",
    fontFamily: "var(--havn-font-mono)", fontSize: "11px", lineHeight: "18px",
    background: "var(--havn-bg)",
  },

  // Commit
  commitSection: {
    padding: "8px 12px", borderTop: "1px solid var(--havn-border)",
  },
  commitTextarea: {
    width: "100%", padding: "8px 10px", background: "var(--havn-bg)",
    border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius)",
    color: "var(--havn-text)", fontFamily: "var(--havn-font)", fontSize: "12px",
    resize: "vertical", outline: "none", boxSizing: "border-box",
  },
  commitFooter: {
    display: "flex", alignItems: "center", justifyContent: "space-between",
    marginTop: "6px",
  },
  commitHint: { fontSize: "11px", color: "var(--havn-text-dim)" },
  commitBtn: {
    padding: "5px 16px", background: "var(--havn-green)", border: "1px solid var(--havn-green-border)",
    borderRadius: "var(--havn-radius-lg)", color: "#fff", cursor: "pointer",
    fontSize: "12px", fontWeight: 600,
  },

  // Collapsible sections
  collapsible: { borderTop: "1px solid var(--havn-border)" },
  collapsibleHeader: {
    display: "flex", alignItems: "center", gap: "6px", padding: "8px 12px",
    background: "none", border: "none", width: "100%", cursor: "pointer",
    color: "var(--havn-text)", fontSize: "12px", textAlign: "left",
  },
  collapseArrow: {
    fontSize: "10px", color: "var(--havn-text-secondary)", display: "inline-block",
    transition: "transform 0.12s ease",
  },
  collapsibleTitle: { fontWeight: 600, fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.5px" },

  // History
  historyList: { padding: "0 12px 8px", maxHeight: "300px", overflow: "auto" },
  historyItem: {
    display: "flex", alignItems: "baseline", gap: "8px", padding: "4px 0",
    borderBottom: "1px solid var(--havn-border)", fontSize: "12px",
  },
  commitHash: {
    fontFamily: "var(--havn-font-mono)", color: "var(--havn-accent)",
    fontSize: "11px", fontWeight: 600, flexShrink: 0,
  },
  commitMessage: {
    flex: 1, color: "var(--havn-text)", overflow: "hidden",
    textOverflow: "ellipsis", whiteSpace: "nowrap",
  },
  commitMeta: { color: "var(--havn-text-dim)", fontSize: "11px", flexShrink: 0, whiteSpace: "nowrap" },

  // Stash
  stashSection: { padding: "0 12px 8px" },
  stashActions: { display: "flex", gap: "6px", marginBottom: "6px" },
  stashInput: {
    flex: 1, padding: "4px 8px", background: "var(--havn-bg)", border: "1px solid var(--havn-border)",
    borderRadius: "var(--havn-radius)", color: "var(--havn-text)", fontSize: "12px", outline: "none",
  },
  stashList: {},
  stashItem: {
    display: "flex", alignItems: "center", gap: "8px", padding: "4px 0",
    borderBottom: "1px solid var(--havn-border)", fontSize: "12px",
  },
  stashIndex: { fontFamily: "var(--havn-font-mono)", color: "var(--havn-accent)", fontSize: "11px", fontWeight: 600, flexShrink: 0 },
  stashMessage: { color: "var(--havn-text-secondary)", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
};
