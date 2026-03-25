import React, { useState, useEffect, useRef } from "react";
import { api } from "./api";

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

const FAVORITES_KEY = "havn_favorite_dashboards";

function loadFavorites() {
  try {
    return JSON.parse(localStorage.getItem(FAVORITES_KEY) || "[]");
  } catch {
    return [];
  }
}

function saveFavorites(ids) {
  localStorage.setItem(FAVORITES_KEY, JSON.stringify(ids));
}

export default function DashboardListPanel({ onOpenDashboard, showConfirm }) {
  const [dashboards, setDashboards] = useState([]);
  const [templates, setTemplates] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [showTemplates, setShowTemplates] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [creating, setCreating] = useState(false);
  const [searchTerm, setSearchTerm] = useState("");
  const [favorites, setFavorites] = useState(loadFavorites);
  const fileInputRef = useRef(null);

  function toggleFavorite(id) {
    setFavorites(prev => {
      const next = prev.includes(id) ? prev.filter(f => f !== id) : [...prev, id];
      saveFavorites(next);
      return next;
    });
  }

  useEffect(() => { load(); }, []);

  async function load() {
    setLoading(true);
    try {
      const [dashes, tmpls] = await Promise.all([
        api.listDashboards(),
        api.listDashboardTemplates(),
      ]);
      setDashboards(dashes || []);
      setTemplates(tmpls || []);
    } catch (e) {
      console.error("Failed to load dashboards:", e);
    } finally {
      setLoading(false);
    }
  }

  async function handleCreate(templateId) {
    if (!newName.trim()) return;
    setCreating(true);
    try {
      const dash = await api.createDashboard(newName.trim(), newDesc.trim(), templateId);
      setShowCreate(false);
      setShowTemplates(false);
      setNewName("");
      setNewDesc("");
      if (dash?.id) onOpenDashboard(dash.id);
    } catch (e) {
      console.error("Failed to create dashboard:", e);
    } finally {
      setCreating(false);
    }
  }

  async function handleClone(id, name) {
    try {
      const dash = await api.cloneDashboard(id, `${name} (copy)`);
      if (dash?.id) {
        load();
      }
    } catch (e) {
      console.error("Clone failed:", e);
    }
  }

  async function handleDelete(id, name) {
    if (showConfirm) {
      const ok = await showConfirm("Delete Dashboard", `Delete "${name || "Untitled"}"? This cannot be undone.`, "Delete", true);
      if (!ok) return;
    }
    try {
      await api.deleteDashboard(id);
      setDashboards(prev => prev.filter(d => d.id !== id));
    } catch (e) {
      console.error("Delete failed:", e);
    }
  }

  async function handleExport(id) {
    try {
      const data = await api.exportDashboard(id);
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `dashboard-${id}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      console.error("Export failed:", e);
    }
  }

  async function handleImport(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      const data = JSON.parse(text);
      const dash = await api.importDashboard({
        dashboard: data.dashboard || data,
        widgets: data.widgets || [],
      });
      setShowImport(false);
      if (dash?.id) {
        load();
      }
    } catch (err) {
      console.error("Import failed:", err);
    }
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  if (loading) {
    return <div style={st.container}><div style={st.center}>Loading dashboards...</div></div>;
  }

  return (
    <div style={st.container}>
      {/* Header */}
      <div style={st.header}>
        <h2 style={st.heading}>Dashboards</h2>
        <input
          style={st.searchInput}
          type="text"
          placeholder="Search dashboards..."
          value={searchTerm}
          onChange={(e) => setSearchTerm(e.target.value)}
        />
        <div style={st.headerActions}>
          <button style={st.importBtn} onClick={() => fileInputRef.current?.click()}>
            Import
          </button>
          <input
            ref={fileInputRef}
            type="file"
            accept=".json"
            style={{ display: "none" }}
            onChange={handleImport}
          />
          <button style={st.primaryBtn} onClick={() => setShowCreate(true)}>
            + New Dashboard
          </button>
        </div>
      </div>

      {/* Create dialog */}
      {showCreate && (
        <div style={st.dialog}>
          <div style={st.dialogInner}>
            <h3 style={{ margin: "0 0 12px", color: "var(--havn-text)" }}>New Dashboard</h3>
            <input
              style={st.input}
              placeholder="Dashboard name"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              autoFocus
              onKeyDown={(e) => e.key === "Enter" && handleCreate()}
            />
            <textarea
              style={{ ...st.input, marginTop: 8, minHeight: 80, resize: "vertical", fontFamily: "inherit" }}
              placeholder="Describe what this dashboard tracks, who it's for, key metrics..."
              value={newDesc}
              onChange={(e) => setNewDesc(e.target.value)}
              rows={4}
            />
            {templates.length > 0 && (
              <div style={{ marginTop: 12 }}>
                <div style={{ fontSize: 12, color: "var(--havn-text-secondary)", marginBottom: 6 }}>
                  Or start from a template:
                </div>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {templates.map(t => (
                    <button
                      key={t.id}
                      style={st.templateChip}
                      onClick={() => handleCreate(t.id)}
                      disabled={creating || !newName.trim()}
                    >
                      {t.name} ({t.widget_count} widgets)
                    </button>
                  ))}
                </div>
              </div>
            )}
            <div style={{ display: "flex", gap: 8, marginTop: 16, justifyContent: "flex-end" }}>
              <button style={st.cancelBtn} onClick={() => { setShowCreate(false); setNewName(""); setNewDesc(""); }}>
                Cancel
              </button>
              <button
                style={st.primaryBtn}
                onClick={() => handleCreate()}
                disabled={creating || !newName.trim()}
              >
                {creating ? "Creating..." : "Create"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Dashboard cards */}
      {dashboards.length === 0 ? (
        <div style={st.empty}>
          <div style={{ fontSize: 40, marginBottom: 12, opacity: 0.3 }}>📊</div>
          <div style={{ fontSize: 16, fontWeight: 500, color: "var(--havn-text)" }}>
            No dashboards yet
          </div>
          <div style={{ fontSize: 13, color: "var(--havn-text-secondary)", marginTop: 4 }}>
            Create your first dashboard to visualize your warehouse data
          </div>
          <button
            style={{ ...st.primaryBtn, marginTop: 16 }}
            onClick={() => setShowCreate(true)}
          >
            + Create Dashboard
          </button>
        </div>
      ) : (() => {
        const term = searchTerm.trim().toLowerCase();
        const filtered = term
          ? dashboards.filter(d => (d.name || "").toLowerCase().includes(term))
          : dashboards;
        // Sort: favorites first, then original order
        const sorted = [...filtered].sort((a, b) => {
          const aFav = favorites.includes(a.id) ? 0 : 1;
          const bFav = favorites.includes(b.id) ? 0 : 1;
          return aFav - bFav;
        });
        if (sorted.length === 0) {
          return (
            <div style={{ textAlign: "center", padding: 40, color: "var(--havn-text-secondary)" }}>
              No matching dashboards
            </div>
          );
        }
        return (
          <div style={st.grid}>
            {sorted.map(d => (
              <DashboardCard
                key={d.id}
                dashboard={d}
                isFavorite={favorites.includes(d.id)}
                onToggleFavorite={() => toggleFavorite(d.id)}
                onOpen={() => onOpenDashboard(d.id)}
                onClone={() => handleClone(d.id, d.name)}
                onExport={() => handleExport(d.id)}
                onDelete={() => handleDelete(d.id, d.name)}
              />
            ))}
          </div>
        );
      })()}
    </div>
  );
}

function DashboardCard({ dashboard, isFavorite, onToggleFavorite, onOpen, onClone, onExport, onDelete }) {
  const [showActions, setShowActions] = useState(false);
  const actionsRef = useRef(null);

  useEffect(() => {
    if (!showActions) return;
    const handler = (e) => {
      if (actionsRef.current && !actionsRef.current.contains(e.target)) setShowActions(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showActions]);

  return (
    <div style={st.card} onClick={onOpen}
      onMouseEnter={(e) => { e.currentTarget.style.borderColor = "var(--havn-accent)"; e.currentTarget.style.boxShadow = "0 2px 8px rgba(0,0,0,0.1)"; }}
      onMouseLeave={(e) => { e.currentTarget.style.borderColor = "var(--havn-border)"; e.currentTarget.style.boxShadow = "none"; }}>
      <div style={st.cardHeader}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, overflow: "hidden", flex: 1 }}>
          <button
            style={st.starBtn}
            onClick={(e) => { e.stopPropagation(); onToggleFavorite(); }}
            title={isFavorite ? "Remove from favorites" : "Add to favorites"}
          >
            {isFavorite ? "\u2605" : "\u2606"}
          </button>
          <span style={st.cardName}>{dashboard.name}</span>
        </div>
        <button
          style={st.cardMenuBtn}
          onClick={(e) => { e.stopPropagation(); setShowActions(!showActions); }}
        >
          ⋯
        </button>
        {showActions && (
          <div ref={actionsRef} style={st.cardMenu} onClick={(e) => e.stopPropagation()}>
            <button style={st.cardMenuItem} onClick={() => { setShowActions(false); onClone(); }}>Clone</button>
            <button style={st.cardMenuItem} onClick={() => { setShowActions(false); onExport(); }}>Export</button>
            <button style={{ ...st.cardMenuItem, color: "var(--havn-red)" }} onClick={() => { setShowActions(false); onDelete(); }}>Delete</button>
          </div>
        )}
      </div>
      {dashboard.description && (
        <div style={st.cardDesc}>{dashboard.description}</div>
      )}
      <div style={st.cardMeta}>
        <span>{dashboard.widget_count || 0} widget{dashboard.widget_count !== 1 ? "s" : ""}</span>
        <span>Updated {timeAgo(dashboard.updated_at)}</span>
      </div>
    </div>
  );
}

const st = {
  container: {
    padding: 24,
    height: "100%",
    overflow: "auto",
  },
  center: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    height: "100%",
    color: "var(--havn-text-secondary)",
  },
  header: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: 20,
  },
  heading: {
    margin: 0,
    fontSize: 20,
    fontWeight: 600,
    color: "var(--havn-text)",
  },
  headerActions: {
    display: "flex",
    gap: 8,
  },
  searchInput: {
    flex: 1,
    maxWidth: 280,
    padding: "7px 12px",
    border: "1px solid var(--havn-border)",
    borderRadius: 6,
    background: "var(--havn-bg-secondary, var(--havn-bg))",
    color: "var(--havn-text)",
    fontSize: 13,
    outline: "none",
    boxSizing: "border-box",
  },
  starBtn: {
    background: "none",
    border: "none",
    cursor: "pointer",
    fontSize: 16,
    padding: 0,
    color: "var(--havn-yellow, #e5c07b)",
    lineHeight: 1,
    flexShrink: 0,
  },
  primaryBtn: {
    background: "var(--havn-accent)",
    color: "#fff",
    border: "none",
    borderRadius: 6,
    padding: "8px 16px",
    cursor: "pointer",
    fontSize: 13,
    fontWeight: 500,
  },
  importBtn: {
    background: "none",
    color: "var(--havn-text-secondary)",
    border: "1px solid var(--havn-border)",
    borderRadius: 6,
    padding: "8px 16px",
    cursor: "pointer",
    fontSize: 13,
  },
  cancelBtn: {
    background: "none",
    color: "var(--havn-text-secondary)",
    border: "1px solid var(--havn-border)",
    borderRadius: 6,
    padding: "8px 16px",
    cursor: "pointer",
    fontSize: 13,
  },
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
    gap: 16,
  },
  card: {
    background: "var(--havn-bg-secondary, var(--havn-bg))",
    border: "1px solid var(--havn-border)",
    borderRadius: "var(--havn-radius, 8px)",
    padding: 16,
    cursor: "pointer",
    transition: "border-color 0.15s, box-shadow 0.15s",
    position: "relative",
  },
  cardHeader: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: 6,
  },
  cardName: {
    fontSize: 15,
    fontWeight: 600,
    color: "var(--havn-text)",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  cardMenuBtn: {
    background: "none",
    border: "none",
    color: "var(--havn-text-secondary)",
    cursor: "pointer",
    fontSize: 16,
    padding: "2px 6px",
    borderRadius: 4,
  },
  cardMenu: {
    position: "absolute",
    top: 40,
    right: 12,
    background: "var(--havn-bg)",
    border: "1px solid var(--havn-border)",
    borderRadius: 6,
    padding: 4,
    zIndex: 100,
    minWidth: 100,
    boxShadow: "0 4px 12px rgba(0,0,0,0.2)",
  },
  cardMenuItem: {
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
  },
  cardDesc: {
    fontSize: 13,
    color: "var(--havn-text-secondary)",
    marginBottom: 8,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  cardMeta: {
    display: "flex",
    justifyContent: "space-between",
    fontSize: 12,
    color: "var(--havn-text-secondary)",
    opacity: 0.7,
  },
  empty: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    height: "60%",
    textAlign: "center",
  },
  dialog: {
    position: "fixed",
    inset: 0,
    background: "rgba(0,0,0,0.5)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: 1000,
  },
  dialogInner: {
    background: "var(--havn-bg)",
    border: "1px solid var(--havn-border)",
    borderRadius: 10,
    padding: 24,
    width: 420,
    maxWidth: "90vw",
  },
  input: {
    width: "100%",
    padding: "8px 12px",
    border: "1px solid var(--havn-border)",
    borderRadius: 6,
    background: "var(--havn-bg-secondary, var(--havn-bg))",
    color: "var(--havn-text)",
    fontSize: 14,
    outline: "none",
    boxSizing: "border-box",
  },
  templateChip: {
    background: "var(--havn-bg-secondary, var(--havn-bg))",
    border: "1px solid var(--havn-border)",
    borderRadius: 4,
    padding: "4px 10px",
    fontSize: 12,
    color: "var(--havn-text)",
    cursor: "pointer",
  },
};
