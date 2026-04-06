import React, { useState, useEffect, useCallback, useMemo } from "react";
import { api } from "./api";
import { useAuth } from "./AuthContext";

/* ------------------------------------------------------------------ */
/* Formatters                                                          */
/* ------------------------------------------------------------------ */

function timeAgo(dateStr) {
  if (!dateStr) return "";
  const diffMs = Date.now() - new Date(dateStr).getTime();
  if (isNaN(diffMs) || diffMs < 0) return "just now";
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

function fmtDuration(ms) {
  if (ms == null) return "-";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  const min = Math.floor(ms / 60000);
  const sec = Math.floor((ms % 60000) / 1000);
  return `${min}m ${sec}s`;
}

const STATUS_COLORS = {
  open: { bg: "color-mix(in srgb, var(--havn-green) 15%, transparent)", fg: "var(--havn-green)" },
  merged: { bg: "color-mix(in srgb, var(--havn-purple, #a855f7) 15%, transparent)", fg: "var(--havn-purple, #a855f7)" },
  closed: { bg: "var(--havn-bg-tertiary)", fg: "var(--havn-text-secondary)" },
  running: { bg: "color-mix(in srgb, var(--havn-accent) 15%, transparent)", fg: "var(--havn-accent)" },
  success: { bg: "color-mix(in srgb, var(--havn-green) 15%, transparent)", fg: "var(--havn-green)" },
  error: { bg: "color-mix(in srgb, var(--havn-red) 15%, transparent)", fg: "var(--havn-red)" },
};

function StatusBadge({ status }) {
  const c = STATUS_COLORS[status] || STATUS_COLORS.closed;
  return (
    <span style={{ padding: "2px 8px", borderRadius: 4, fontSize: 11, fontWeight: 500, background: c.bg, color: c.fg }}>
      {status}
    </span>
  );
}

function DiffStatusBadge({ status }) {
  if (status === "added") return <span style={{ ...s.diffBadge, color: "var(--havn-green)", background: "color-mix(in srgb, var(--havn-green) 10%, transparent)" }}>added</span>;
  if (status === "removed") return <span style={{ ...s.diffBadge, color: "var(--havn-red)", background: "color-mix(in srgb, var(--havn-red) 10%, transparent)" }}>removed</span>;
  if (status === "modified") return <span style={{ ...s.diffBadge, color: "var(--havn-yellow, #eab308)", background: "color-mix(in srgb, var(--havn-yellow, #eab308) 10%, transparent)" }}>modified</span>;
  return <span style={{ ...s.diffBadge, color: "var(--havn-text-dim)", background: "var(--havn-bg-tertiary)" }}>{status}</span>;
}

/* ------------------------------------------------------------------ */
/* Root panel                                                          */
/* ------------------------------------------------------------------ */

const LIST_SUB_TABS = ["Open", "Merged", "Closed", "All"];

export default function ReviewsPanel({ showConfirm }) {
  const [prs, setPrs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState(null);
  const [stateStatus, setStateStatus] = useState(null);
  const [creating, setCreating] = useState(false);
  const [tab, setTab] = useState("Open");
  const [filterText, setFilterText] = useState("");
  const [error, setError] = useState(null);

  const loadPrs = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.listPrs();
      setPrs(Array.isArray(data) ? data : []);
    } catch (e) {
      setError(e.message || "Failed to load PRs");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadStateStatus = useCallback(async () => {
    try {
      const status = await api.getPrStateStatus();
      setStateStatus(status);
    } catch {
      setStateStatus(null);
    }
  }, []);

  useEffect(() => {
    loadPrs();
    loadStateStatus();
  }, [loadPrs, loadStateStatus]);

  const counts = useMemo(() => {
    const c = { open: 0, merged: 0, closed: 0, needs_review: 0 };
    for (const p of prs) {
      if (p.status in c) c[p.status] += 1;
      if (p.status === "open" && p.approvers.length === 0 && p.change_requesters.length === 0) {
        c.needs_review += 1;
      }
    }
    return c;
  }, [prs]);

  const filteredPrs = useMemo(() => {
    const statusKey = tab.toLowerCase();
    let data = statusKey === "all" ? prs : prs.filter((p) => p.status === statusKey);
    if (filterText) {
      const q = filterText.toLowerCase();
      data = data.filter(
        (p) =>
          (p.title || "").toLowerCase().includes(q) ||
          (p.author || "").toLowerCase().includes(q) ||
          (p.head_ref || "").toLowerCase().includes(q),
      );
    }
    return data;
  }, [prs, tab, filterText]);

  if (selectedId) {
    return (
      <PrDetail
        prId={selectedId}
        onBack={() => { setSelectedId(null); loadPrs(); }}
      />
    );
  }

  return (
    <div style={s.container}>
      <div style={s.header}>
        <div style={s.cards}>
          <div style={s.card}>
            <div style={s.cardLabel}>Open</div>
            <div style={{ ...s.cardValue, color: "var(--havn-green)" }}>{counts.open}</div>
          </div>
          <div style={s.card}>
            <div style={s.cardLabel}>Needs Review</div>
            <div style={{ ...s.cardValue, color: counts.needs_review > 0 ? "var(--havn-yellow)" : "var(--havn-text-dim)" }}>{counts.needs_review}</div>
          </div>
          <div style={s.card}>
            <div style={s.cardLabel}>Merged</div>
            <div style={{ ...s.cardValue, color: "var(--havn-text)" }}>{counts.merged}</div>
          </div>
          <div style={s.card}>
            <div style={s.cardLabel}>Closed</div>
            <div style={{ ...s.cardValue, color: "var(--havn-text-dim)" }}>{counts.closed}</div>
          </div>
        </div>
        {stateStatus && !stateStatus.is_git_repo && (
          <div style={{
            marginTop: 4,
            marginBottom: 8,
            padding: "12px 14px",
            background: "color-mix(in srgb, var(--havn-accent) 8%, transparent)",
            border: "1px solid color-mix(in srgb, var(--havn-accent) 30%, transparent)",
            borderRadius: 6,
            display: "flex",
            alignItems: "center",
            gap: 12,
          }}>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: "var(--havn-text)" }}>
                Not a git repository yet
              </div>
              <div style={{ fontSize: 11, color: "var(--havn-text-dim)", marginTop: 2 }}>
                Pull requests and PR state sharing require git. Initialize here
                to start tracking changes.
              </div>
            </div>
            <button
              style={{
                padding: "6px 14px",
                background: "var(--havn-accent)",
                color: "#fff",
                border: "1px solid var(--havn-accent)",
                borderRadius: "var(--havn-radius-lg)",
                cursor: "pointer",
                fontSize: 12,
                fontWeight: 500,
              }}
              onClick={async () => {
                try {
                  await api.initGit("main");
                  await loadStateStatus();
                  await loadPrs();
                } catch (e) {
                  setError(e.message || "Failed to initialize git");
                }
              }}
            >
              Initialize git
            </button>
          </div>
        )}
        {stateStatus && stateStatus.is_git_repo && (stateStatus.dirty || stateStatus.unpushed_count > 0) && (
          <div style={s.banner}>
            <strong>PR state has unpushed changes.</strong>{" "}
            {stateStatus.dirty && "Uncommitted .havn/prs/ files. "}
            {stateStatus.unpushed_count > 0 && `${stateStatus.unpushed_count} commit(s) ahead of remote. `}
            Commit and push .havn/prs/ to share with your team.
          </div>
        )}
        <div style={s.tabs}>
          {LIST_SUB_TABS.map((t) => (
            <div
              key={t}
              style={{ ...s.tab, ...(tab === t ? s.tabActive : {}) }}
              onClick={() => setTab(t)}
            >
              {t}
            </div>
          ))}
        </div>
      </div>

      <div style={s.content}>
        {error && <div style={s.errorBox}>{error}</div>}

        <div style={s.toolbar}>
          <input
            style={s.filterInput}
            placeholder="Filter by title, author, or branch\u2026"
            aria-label="Filter pull requests"
            value={filterText}
            onChange={(e) => setFilterText(e.target.value)}
          />
          <button style={s.btn} onClick={loadPrs} disabled={loading}>
            {loading ? "Loading\u2026" : "Refresh"}
          </button>
          <button style={s.btnPrimary} onClick={() => setCreating(true)}>+ New PR</button>
          <span style={s.count}>{filteredPrs.length} of {prs.length}</span>
        </div>

        {creating && (
          <CreatePrRow
            onCancel={() => setCreating(false)}
            onCreated={() => { setCreating(false); loadPrs(); loadStateStatus(); }}
            onError={setError}
          />
        )}

        {loading && prs.length === 0 ? (
          <div style={s.emptyState}>
            <div style={s.emptyText}>Loading pull requests\u2026</div>
          </div>
        ) : filteredPrs.length === 0 ? (
          <div style={s.emptyState}>
            <div style={s.emptyTitle}>No pull requests</div>
            <div style={s.emptyText}>
              Create one with <code style={s.emptyCode}>+ New PR</code> above, or from the CLI:
              <br />
              <code style={s.emptyCode}>havn pr create --branch feature/x --title "..."</code>
            </div>
          </div>
        ) : (
          <table style={s.table}>
            <thead>
              <tr>
                <th style={s.th}>ID</th>
                <th style={s.th}>Title</th>
                <th style={s.th}>Branch</th>
                <th style={s.th}>Author</th>
                <th style={s.th}>Reviews</th>
                <th style={s.th}>Status</th>
                <th style={s.th}>Created</th>
              </tr>
            </thead>
            <tbody>
              {filteredPrs.map((pr) => (
                <tr key={pr.id} onClick={() => setSelectedId(pr.id)} style={s.trClickable}>
                  <td style={s.td}><code style={s.codeSm}>{pr.id}</code></td>
                  <td style={s.td}>
                    <div style={{ fontWeight: 500 }}>{pr.title}</div>
                    {pr.description && (
                      <div style={s.descPreview}>{pr.description.slice(0, 80)}{pr.description.length > 80 ? "\u2026" : ""}</div>
                    )}
                  </td>
                  <td style={s.td}>
                    <code style={s.codeSm}>{pr.head_ref}</code>
                    <span style={{ color: "var(--havn-text-dim)" }}> \u2192 </span>
                    <code style={s.codeSm}>{pr.base_ref}</code>
                  </td>
                  <td style={s.td}>{pr.author}</td>
                  <td style={s.td}>
                    {pr.approvers.length > 0 && (
                      <span style={{ color: "var(--havn-green)" }}>\u2713 {pr.approvers.length}</span>
                    )}
                    {pr.change_requesters.length > 0 && (
                      <span style={{ color: "var(--havn-red)", marginLeft: 6 }}>
                        \u2717 {pr.change_requesters.length}
                      </span>
                    )}
                    {pr.approvers.length === 0 && pr.change_requesters.length === 0 && (
                      <span style={{ color: "var(--havn-text-dim)" }}>-</span>
                    )}
                  </td>
                  <td style={s.td}><StatusBadge status={pr.status} /></td>
                  <td style={s.td}>
                    <span style={{ color: "var(--havn-text-dim)", fontSize: 11 }}>{timeAgo(pr.created_at)}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Create PR row                                                       */
/* ------------------------------------------------------------------ */

function CreatePrRow({ onCancel, onCreated, onError }) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [headRef, setHeadRef] = useState("");
  const [baseRef, setBaseRef] = useState("main");
  const [author, setAuthor] = useState("local");
  const [requireApproval, setRequireApproval] = useState(true);
  const [saving, setSaving] = useState(false);
  const [branches, setBranches] = useState([]);

  useEffect(() => {
    (async () => {
      try {
        const gitStatus = await api.getGitStatus();
        if (gitStatus && gitStatus.is_git_repo) {
          const bs = await api.getGitBranches();
          if (Array.isArray(bs)) {
            setBranches(bs.filter((b) => !b.is_remote).map((b) => b.name));
          }
        }
      } catch {
        // silent — branches field becomes a plain text input
      }
    })();
  }, []);

  const handleSave = async () => {
    if (!title.trim() || !headRef.trim()) {
      onError("Title and head branch are required");
      return;
    }
    setSaving(true);
    try {
      await api.createPr({
        title: title.trim(),
        description,
        head_ref: headRef.trim(),
        base_ref: baseRef.trim(),
        author,
        require_approval: requireApproval,
      });
      onCreated();
    } catch (e) {
      onError(e.message || "Failed to create PR");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={s.createCard}>
      <div style={s.createTitle}>New Pull Request</div>
      <div style={s.formGrid}>
        <label style={s.label}>
          Title
          <input style={s.input} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Short, descriptive title" />
        </label>
        <label style={s.label}>
          Head branch
          {branches.length > 0 ? (
            <select style={s.input} value={headRef} onChange={(e) => setHeadRef(e.target.value)}>
              <option value="">-- select branch --</option>
              {branches.map((b) => <option key={b} value={b}>{b}</option>)}
            </select>
          ) : (
            <input style={s.input} value={headRef} onChange={(e) => setHeadRef(e.target.value)} placeholder="feature/my-branch" />
          )}
        </label>
        <label style={s.label}>
          Base branch
          <input style={s.input} value={baseRef} onChange={(e) => setBaseRef(e.target.value)} placeholder="main" />
        </label>
        <label style={s.label}>
          Author
          <input style={s.input} value={author} onChange={(e) => setAuthor(e.target.value)} />
        </label>
        <label style={{ ...s.label, gridColumn: "1 / -1" }}>
          Description
          <textarea
            style={{ ...s.input, minHeight: 60, fontFamily: "inherit" }}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What does this PR change and why?"
          />
        </label>
        <label style={{ ...s.label, flexDirection: "row", alignItems: "center", gap: 8 }}>
          <input type="checkbox" checked={requireApproval} onChange={(e) => setRequireApproval(e.target.checked)} />
          <span>Require approval before merging</span>
        </label>
      </div>
      <div style={s.formActions}>
        <button style={s.btn} onClick={onCancel}>Cancel</button>
        <button style={s.btnPrimary} onClick={handleSave} disabled={saving}>
          {saving ? "Creating\u2026" : "Create PR"}
        </button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* PR Detail                                                           */
/* ------------------------------------------------------------------ */

const DETAIL_TABS = ["Changes", "Data Impact", "Lineage", "Comments"];

function PrDetail({ prId, onBack }) {
  const [pr, setPr] = useState(null);
  const [build, setBuild] = useState(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState("Changes");
  const [files, setFiles] = useState([]);
  const [lineage, setLineage] = useState(null);
  const [error, setError] = useState(null);
  const [action, setAction] = useState(null); // "building", "merging", etc.
  const [commentDraft, setCommentDraft] = useState("");
  const [changeRequestReason, setChangeRequestReason] = useState("");
  const [showChangeRequestForm, setShowChangeRequestForm] = useState(false);
  const auth = useAuth();
  const currentUser = auth?.currentUser?.username || "local";

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [p, b, d] = await Promise.all([
        api.getPr(prId),
        api.getPrBuild(prId).catch(() => null),
        api.getPrDiff(prId).catch(() => ({ files: [] })),
      ]);
      setPr(p);
      setBuild(b && b.id ? b : null);
      setFiles(d?.files || []);
    } catch (e) {
      setError(e.message || "Failed to load PR");
    } finally {
      setLoading(false);
    }
  }, [prId]);

  const loadLineage = useCallback(async () => {
    try {
      const li = await api.getPrLineageImpact(prId);
      setLineage(li);
    } catch (e) {
      setLineage(null);
    }
  }, [prId]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  useEffect(() => {
    if (tab === "Lineage" && !lineage) loadLineage();
  }, [tab, lineage, loadLineage]);

  // Poll while build is running
  useEffect(() => {
    if (!build || build.status !== "running") return;
    const id = setInterval(async () => {
      try {
        const b = await api.getPrBuild(prId);
        setBuild(b && b.id ? b : null);
        if (b && b.status !== "running") clearInterval(id);
      } catch {
        // ignore
      }
    }, 2000);
    return () => clearInterval(id);
  }, [build, prId]);

  const handleBuild = async () => {
    setAction("building");
    setError(null);
    try {
      await api.buildPr(prId);
      // Kick off polling — refresh the build record immediately
      setTimeout(async () => {
        const b = await api.getPrBuild(prId);
        setBuild(b && b.id ? b : null);
      }, 500);
    } catch (e) {
      setError(e.message || "Build failed to start");
    } finally {
      setAction(null);
    }
  };

  const handleApprove = async () => {
    setAction("approving");
    try {
      await api.approvePr(prId, currentUser);
      await loadAll();
    } catch (e) {
      setError(e.message);
    } finally {
      setAction(null);
    }
  };

  const handleRequestChanges = async () => {
    setAction("requesting");
    try {
      await api.requestPrChanges(prId, currentUser, changeRequestReason);
      setChangeRequestReason("");
      setShowChangeRequestForm(false);
      await loadAll();
    } catch (e) {
      setError(e.message);
    } finally {
      setAction(null);
    }
  };

  const handleMerge = async () => {
    const ok = await showConfirm("Merge Pull Request", `Merge ${prId} into ${pr.base_ref}?`, "Merge");
    if (!ok) return;
    setAction("merging");
    try {
      const result = await api.mergePr(prId, currentUser);
      if (result.success) {
        await loadAll();
      }
    } catch (e) {
      setError(e.message || "Merge failed");
    } finally {
      setAction(null);
    }
  };

  const handleClose = async () => {
    const ok = await showConfirm("Close Pull Request", `Close ${prId} without merging? This action cannot be undone.`, "Close", true);
    if (!ok) return;
    setAction("closing");
    try {
      await api.closePr(prId, currentUser);
      await loadAll();
    } catch (e) {
      setError(e.message);
    } finally {
      setAction(null);
    }
  };

  const handleAiReview = async () => {
    try {
      const prompt = await api.getPrReviewPrompt(prId);
      // Dispatch event — AgentSidebar listens for this and sends via WebSocket
      window.dispatchEvent(new CustomEvent("havn-agent-send", {
        detail: { prompt, source: "pr-review", prId },
      }));
    } catch (e) {
      setError(e.message || "Failed to load review prompt");
    }
  };

  const handleAddComment = async () => {
    if (!commentDraft.trim()) return;
    try {
      await api.addPrComment(prId, { body: commentDraft, author: currentUser });
      setCommentDraft("");
      await loadAll();
    } catch (e) {
      setError(e.message);
    }
  };

  if (loading || !pr) {
    return (
      <div style={s.container}>
        <div style={s.header}>
          <button style={s.btn} onClick={onBack}>\u2190 Back</button>
        </div>
        <div style={s.content}>
          <div style={s.emptyState}>{error || "Loading\u2026"}</div>
        </div>
      </div>
    );
  }

  const canMerge = pr.status === "open" && (!pr.require_approval || pr.approvers.length > 0) && pr.change_requesters.length === 0;

  return (
    <div style={s.container}>
      <div style={s.header}>
        <div style={s.headerRow}>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <button style={s.btn} onClick={onBack}>\u2190 Back</button>
            <div>
              <div style={{ ...s.title, display: "flex", alignItems: "center", gap: 8 }}>
                <code style={s.codeSm}>{pr.id}</code>
                {pr.title}
                <StatusBadge status={pr.status} />
              </div>
              <div style={s.subtitle}>
                {pr.author} \u00b7 <code style={s.codeSm}>{pr.head_ref}</code> \u2192 <code style={s.codeSm}>{pr.base_ref}</code> \u00b7 {timeAgo(pr.created_at)}
              </div>
            </div>
          </div>
          <div style={s.headerActions}>
            {pr.status === "open" && (
              <>
                <button style={s.btn} onClick={handleBuild} disabled={action === "building"}>
                  {action === "building" ? "Building\u2026" : "Build & Diff"}
                </button>
                <button style={s.btn} onClick={handleAiReview}>AI Review</button>
                <button style={s.btn} onClick={() => setShowChangeRequestForm((v) => !v)} disabled={action != null}>Request Changes</button>
                <button style={s.btn} onClick={handleApprove} disabled={action != null}>Approve</button>
                <button style={s.btnPrimary} onClick={handleMerge} disabled={!canMerge || action != null}>
                  {action === "merging" ? "Merging\u2026" : "Merge"}
                </button>
                <button style={s.btnDanger} onClick={handleClose} disabled={action != null}>Close</button>
              </>
            )}
          </div>
        </div>
        <div style={s.tabs}>
          {DETAIL_TABS.map((t) => (
            <button key={t} style={tab === t ? { ...s.tab, ...s.tabActive } : s.tab} onClick={() => setTab(t)}>
              {t}
              {t === "Comments" && pr.comments.length > 0 && ` (${pr.comments.length})`}
            </button>
          ))}
        </div>
      </div>

      <div style={s.content}>
        {error && <div style={s.errorBox}>{error}</div>}
        {pr.description && (
          <div style={s.descBox}>{pr.description}</div>
        )}
        {showChangeRequestForm && (
          <div style={s.createCard}>
            <div style={s.createTitle}>Request changes</div>
            <textarea
              style={{ ...s.input, minHeight: 60, fontFamily: "inherit" }}
              value={changeRequestReason}
              onChange={(e) => setChangeRequestReason(e.target.value)}
              placeholder="What needs to change? (optional)"
            />
            <div style={s.formActions}>
              <button style={s.btn} onClick={() => { setShowChangeRequestForm(false); setChangeRequestReason(""); }}>
                Cancel
              </button>
              <button style={s.btnPrimary} onClick={handleRequestChanges} disabled={action != null}>
                {action === "requesting" ? "Submitting\u2026" : "Request Changes"}
              </button>
            </div>
          </div>
        )}

        {tab === "Changes" && (
          <ChangesTab files={files} />
        )}
        {tab === "Data Impact" && (
          <DataImpactTab build={build} onBuild={handleBuild} building={action === "building"} />
        )}
        {tab === "Lineage" && (
          <LineageTab lineage={lineage} />
        )}
        {tab === "Comments" && (
          <CommentsTab
            pr={pr}
            commentDraft={commentDraft}
            setCommentDraft={setCommentDraft}
            onAddComment={handleAddComment}
          />
        )}
      </div>
    </div>
  );
}

/* --- Changes tab --- */
function ChangesTab({ files }) {
  if (!files || files.length === 0) {
    return <div style={s.emptyState}>No files changed.</div>;
  }
  return (
    <div>
      <div style={s.sectionTitle}>Files changed ({files.length})</div>
      <table style={s.table}>
        <thead>
          <tr><th style={s.th}>Path</th></tr>
        </thead>
        <tbody>
          {files.map((f) => (
            <tr key={f}>
              <td style={s.td}><code style={s.codeSm}>{f}</code></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* --- Data Impact tab --- */
function DataImpactTab({ build, onBuild, building }) {
  if (!build) {
    return (
      <div style={s.emptyState}>
        <div style={s.emptyTitle}>No build yet</div>
        <div style={s.emptyText}>
          Run "Build &amp; Diff" to clone the warehouse into an isolated worktree, build the PR branch's models, and compare every table to main.
        </div>
        <button style={{ ...s.btnPrimary, marginTop: 16 }} onClick={onBuild} disabled={building}>
          {building ? "Building\u2026" : "Build & Diff"}
        </button>
      </div>
    );
  }
  if (build.status === "running") {
    return <div style={s.emptyState}>Build in progress\u2026</div>;
  }
  if (build.status === "error") {
    return (
      <div>
        <div style={s.errorBox}>
          <strong>Build failed.</strong> {build.error}
        </div>
        <button style={s.btn} onClick={onBuild} disabled={building}>Retry</button>
      </div>
    );
  }

  const diff = build.data_diff || {};
  const entries = Object.entries(diff);
  const added = entries.filter(([, v]) => v.status === "added");
  const removed = entries.filter(([, v]) => v.status === "removed");
  const modified = entries.filter(([, v]) => v.status === "modified");
  const unchanged = entries.filter(([, v]) => v.status === "unchanged");

  return (
    <div>
      <div style={s.summaryRow}>
        <div style={s.summaryChip}><strong>{added.length}</strong> added</div>
        <div style={s.summaryChip}><strong>{modified.length}</strong> modified</div>
        <div style={s.summaryChip}><strong>{removed.length}</strong> removed</div>
        <div style={s.summaryChip}><strong>{unchanged.length}</strong> unchanged</div>
        <span style={s.count}>Build took {fmtDuration(build.duration_ms)}</span>
      </div>
      {entries.length === 0 ? (
        <div style={s.emptyState}>No tables in the warehouse.</div>
      ) : (
        <table style={s.table}>
          <thead>
            <tr>
              <th style={s.th}>Table</th>
              <th style={s.th}>Status</th>
              <th style={s.th}>Main rows</th>
              <th style={s.th}>PR rows</th>
              <th style={s.th}>Delta</th>
              <th style={s.th}>Schema changes</th>
            </tr>
          </thead>
          <tbody>
            {entries
              .filter(([, v]) => v.status !== "unchanged")
              .map(([fqn, v]) => {
                const delta = (v.pr_rows || 0) - (v.main_rows || 0);
                const sign = delta >= 0 ? "+" : "";
                return (
                  <tr key={fqn}>
                    <td style={s.td}><code style={s.codeSm}>{fqn}</code></td>
                    <td style={s.td}><DiffStatusBadge status={v.status} /></td>
                    <td style={s.td}>{(v.main_rows ?? 0).toLocaleString()}</td>
                    <td style={s.td}>{(v.pr_rows ?? 0).toLocaleString()}</td>
                    <td style={s.td}>
                      <span style={{ color: delta === 0 ? "var(--havn-text-dim)" : delta > 0 ? "var(--havn-green)" : "var(--havn-red)" }}>
                        {sign}{delta.toLocaleString()}
                      </span>
                    </td>
                    <td style={s.td}>
                      {(v.schema_changes || []).length === 0 ? (
                        <span style={{ color: "var(--havn-text-dim)" }}>-</span>
                      ) : (
                        <div style={{ fontSize: 11 }}>
                          {v.schema_changes.map((sc, i) => (
                            <div key={i}>
                              {sc.type === "added" && <span style={{ color: "var(--havn-green)" }}>+ {sc.column} ({sc.data_type})</span>}
                              {sc.type === "removed" && <span style={{ color: "var(--havn-red)" }}>- {sc.column}</span>}
                              {sc.type === "type_changed" && <span style={{ color: "var(--havn-yellow, #eab308)" }}>~ {sc.column}: {sc.from} \u2192 {sc.to}</span>}
                            </div>
                          ))}
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
          </tbody>
        </table>
      )}
    </div>
  );
}

/* --- Lineage tab --- */
function LineageTab({ lineage }) {
  if (!lineage) {
    return <div style={s.emptyState}>Loading lineage\u2026</div>;
  }
  const { changed = [], impacted = [] } = lineage;
  if (changed.length === 0 && impacted.length === 0) {
    return <div style={s.emptyState}>No SQL models in the diff — nothing to trace.</div>;
  }
  return (
    <div>
      <div style={s.sectionTitle}>Changed models ({changed.length})</div>
      {changed.length === 0 ? (
        <div style={{ color: "var(--havn-text-dim)", marginBottom: 16 }}>None</div>
      ) : (
        <div style={s.chipList}>
          {changed.map((m) => <code key={m} style={s.chipChanged}>{m}</code>)}
        </div>
      )}
      <div style={{ ...s.sectionTitle, marginTop: 24 }}>Downstream impact ({impacted.length})</div>
      {impacted.length === 0 ? (
        <div style={{ color: "var(--havn-text-dim)" }}>No downstream models affected.</div>
      ) : (
        <div style={s.chipList}>
          {impacted.map((m) => <code key={m} style={s.chipImpacted}>{m}</code>)}
        </div>
      )}
    </div>
  );
}

/* --- Comments tab --- */
function CommentsTab({ pr, commentDraft, setCommentDraft, onAddComment }) {
  return (
    <div>
      {pr.comments.length === 0 ? (
        <div style={{ ...s.emptyState, paddingBottom: 12 }}>No comments yet.</div>
      ) : (
        <div style={{ marginBottom: 20 }}>
          {pr.comments.map((c) => (
            <div key={c.id} style={s.commentCard}>
              <div style={s.commentHeader}>
                <strong>{c.author}</strong>
                {c.comment_type === "ai_review" && <span style={s.aiBadge}>AI</span>}
                <span style={{ color: "var(--havn-text-dim)", fontSize: 11 }}>{timeAgo(c.created_at)}</span>
              </div>
              <div style={s.commentBody}>{c.body}</div>
            </div>
          ))}
        </div>
      )}
      {pr.status === "open" && (
        <div style={s.commentForm}>
          <textarea
            style={{ ...s.input, minHeight: 60, fontFamily: "inherit" }}
            value={commentDraft}
            onChange={(e) => setCommentDraft(e.target.value)}
            placeholder="Leave a comment\u2026"
          />
          <button style={s.btnPrimary} onClick={onAddComment} disabled={!commentDraft.trim()}>
            Post comment
          </button>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Styles — mirror QualityPanel's visual language                      */
/* ------------------------------------------------------------------ */

const s = {
  container: { height: "100%", display: "flex", flexDirection: "column", background: "var(--havn-bg)", overflow: "hidden" },
  header: { display: "flex", flexDirection: "column", padding: "8px 12px", borderBottom: "1px solid var(--havn-border)", flexShrink: 0 },
  // Stat cards row — mirrors QualityPanel's `cards` layout
  cards: { display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 10, marginBottom: 12 },
  card: { background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)", borderRadius: 8, padding: "10px 14px" },
  cardLabel: { fontSize: 10, color: "var(--havn-text-dim)", textTransform: "uppercase", letterSpacing: "0.5px", fontWeight: 600, marginBottom: 2 },
  cardValue: { fontSize: 22, fontWeight: 700, color: "var(--havn-text)", lineHeight: 1.2 },
  // Header detail variants (used in detail view only)
  headerRow: { display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16, marginBottom: 8 },
  headerActions: { display: "flex", gap: 6, flexWrap: "wrap" },
  title: { fontSize: 15, fontWeight: 600, color: "var(--havn-text)" },
  subtitle: { fontSize: 12, color: "var(--havn-text-dim)", marginTop: 2 },
  banner: {
    marginTop: 4,
    marginBottom: 8,
    padding: "8px 12px",
    background: "color-mix(in srgb, var(--havn-yellow, #eab308) 10%, transparent)",
    border: "1px solid color-mix(in srgb, var(--havn-yellow, #eab308) 40%, transparent)",
    borderRadius: 4,
    fontSize: 12,
    color: "var(--havn-text)",
  },
  tabs: { display: "flex", gap: 0 },
  tab: { padding: "8px 20px", cursor: "pointer", fontSize: 13, color: "var(--havn-text-secondary)", background: "none", border: "none", borderBottom: "2px solid transparent" },
  tabActive: { color: "var(--havn-text)", borderBottom: "2px solid var(--havn-accent)" },
  content: { flex: 1, overflow: "auto", padding: 20 },
  toolbar: { display: "flex", gap: 8, marginBottom: 12, alignItems: "center", flexWrap: "wrap" },
  filterInput: { padding: "5px 10px", background: "var(--havn-bg-tertiary)", color: "var(--havn-text)", border: "1px solid var(--havn-border-light)", borderRadius: 6, fontSize: 12, width: 260 },
  filterSelect: { padding: "5px 8px", background: "var(--havn-bg-tertiary)", color: "var(--havn-text)", border: "1px solid var(--havn-border-light)", borderRadius: 6, fontSize: 12 },
  count: { fontSize: 12, color: "var(--havn-text-dim)", marginLeft: "auto" },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 13 },
  th: { textAlign: "left", padding: "8px 12px", borderBottom: "1px solid var(--havn-border-light)", color: "var(--havn-text-secondary)", fontSize: 11, textTransform: "uppercase", letterSpacing: "0.3px" },
  td: { padding: "8px 12px", borderBottom: "1px solid var(--havn-border)", color: "var(--havn-text)", verticalAlign: "top" },
  trClickable: { cursor: "pointer" },
  codeSm: { fontFamily: "var(--havn-font-mono)", fontSize: 11, background: "var(--havn-bg-tertiary)", padding: "1px 5px", borderRadius: 3 },
  descPreview: { fontSize: 11, color: "var(--havn-text-dim)", marginTop: 2 },
  descBox: { padding: "10px 14px", background: "var(--havn-bg-tertiary)", borderRadius: 6, marginBottom: 16, color: "var(--havn-text)", whiteSpace: "pre-wrap", fontSize: 13 },
  errorBox: {
    padding: "8px 12px",
    marginBottom: 12,
    background: "color-mix(in srgb, var(--havn-red) 10%, transparent)",
    color: "var(--havn-red)",
    border: "1px solid color-mix(in srgb, var(--havn-red) 30%, transparent)",
    borderRadius: 4,
    fontSize: 12,
  },
  btn: { padding: "4px 12px", background: "var(--havn-btn-bg)", color: "var(--havn-text)", border: "1px solid var(--havn-btn-border)", borderRadius: "var(--havn-radius-lg)", cursor: "pointer", fontSize: 11, fontWeight: 500 },
  btnPrimary: { padding: "4px 12px", background: "var(--havn-green)", color: "#fff", border: "1px solid var(--havn-green-border)", borderRadius: "var(--havn-radius-lg)", cursor: "pointer", fontSize: 11, fontWeight: 500 },
  btnDanger: { padding: "4px 12px", background: "transparent", color: "var(--havn-red)", border: "1px solid color-mix(in srgb, var(--havn-red) 40%, transparent)", borderRadius: "var(--havn-radius-lg)", cursor: "pointer", fontSize: 11, fontWeight: 500 },
  emptyState: { padding: "40px 20px", color: "var(--havn-text-dim)", textAlign: "center", fontSize: 13, lineHeight: 1.6 },
  emptyTitle: { fontSize: 14, fontWeight: 600, color: "var(--havn-text-secondary)", marginBottom: 6 },
  emptyText: { fontSize: 12, color: "var(--havn-text-dim)", lineHeight: 1.6, maxWidth: 440, margin: "0 auto" },
  emptyCode: { fontSize: 11, fontFamily: "var(--havn-font-mono)", background: "var(--havn-bg-tertiary)", padding: "1px 5px", borderRadius: 3 },
  createCard: {
    marginBottom: 16,
    padding: 16,
    background: "var(--havn-bg-secondary)",
    border: "1px solid var(--havn-border)",
    borderRadius: 8,
  },
  createTitle: { fontSize: 14, fontWeight: 600, marginBottom: 12, color: "var(--havn-text)" },
  formGrid: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 },
  label: { display: "flex", flexDirection: "column", gap: 4, fontSize: 12, color: "var(--havn-text-secondary)" },
  input: {
    width: "100%",
    padding: "6px 10px",
    background: "var(--havn-bg-tertiary)",
    border: "1px solid var(--havn-border-light)",
    borderRadius: 6,
    color: "var(--havn-text)",
    fontSize: 13,
    boxSizing: "border-box",
  },
  formActions: { display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 16 },
  sectionTitle: { fontSize: 12, fontWeight: 600, color: "var(--havn-text-secondary)", textTransform: "uppercase", letterSpacing: "0.3px", marginBottom: 8 },
  summaryRow: { display: "flex", gap: 8, marginBottom: 16, alignItems: "center", flexWrap: "wrap" },
  summaryChip: {
    padding: "4px 10px",
    background: "var(--havn-bg-tertiary)",
    borderRadius: 4,
    fontSize: 12,
    color: "var(--havn-text)",
  },
  diffBadge: { padding: "2px 8px", borderRadius: 4, fontSize: 11, fontWeight: 500 },
  chipList: { display: "flex", flexWrap: "wrap", gap: 6 },
  chipChanged: {
    fontFamily: "var(--havn-font-mono)",
    fontSize: 11,
    padding: "3px 8px",
    borderRadius: 4,
    background: "color-mix(in srgb, var(--havn-accent) 12%, transparent)",
    color: "var(--havn-accent)",
    border: "1px solid color-mix(in srgb, var(--havn-accent) 30%, transparent)",
  },
  chipImpacted: {
    fontFamily: "var(--havn-font-mono)",
    fontSize: 11,
    padding: "3px 8px",
    borderRadius: 4,
    background: "var(--havn-bg-tertiary)",
    color: "var(--havn-text-secondary)",
    border: "1px solid var(--havn-border)",
  },
  commentCard: {
    padding: "10px 14px",
    marginBottom: 10,
    background: "var(--havn-bg-secondary)",
    border: "1px solid var(--havn-border)",
    borderRadius: 6,
  },
  commentHeader: { display: "flex", alignItems: "center", gap: 8, marginBottom: 6, fontSize: 12, color: "var(--havn-text)" },
  commentBody: { fontSize: 13, color: "var(--havn-text)", whiteSpace: "pre-wrap", lineHeight: 1.5 },
  aiBadge: {
    fontSize: 10,
    padding: "1px 6px",
    borderRadius: 3,
    background: "color-mix(in srgb, var(--havn-accent) 20%, transparent)",
    color: "var(--havn-accent)",
    fontWeight: 600,
  },
  commentForm: { display: "flex", flexDirection: "column", gap: 8, alignItems: "flex-end" },
};
