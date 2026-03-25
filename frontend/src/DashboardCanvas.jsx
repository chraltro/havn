import React, { useState, useCallback, useRef, useEffect, useMemo } from "react";
import { api } from "./api";
import { useDashboard } from "./DashboardContext";
import DashboardWidget from "./DashboardWidget";
import DashboardFilterManager from "./DashboardFilterManager";
import DashboardFilterBar from "./DashboardFilterBar";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const GRID_COLS = 24;
const ROW_HEIGHT = 64;
const GAP = 12;
const MIN_W = 3;
const MIN_H = 2;

const AUTO_REFRESH_OPTIONS = [
  { label: "Off", value: 0 },
  { label: "30s", value: 30 },
  { label: "1m", value: 60 },
  { label: "5m", value: 300 },
  { label: "15m", value: 900 },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function _relativeTime(date) {
  if (!date) return "";
  const now = new Date();
  const seconds = Math.floor((now - date) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

// ---------------------------------------------------------------------------
// Main Canvas Component
// ---------------------------------------------------------------------------

export default function DashboardCanvas({ onBack, onEditWidget, showConfirm, embedMode }) {
  const {
    dashboard,
    editMode,
    setEditMode,
    saveDashboard,
    refreshAll,
    addWidget,
    removeWidget,
    updateWidget,
    updatePositions,
    autoRefresh,
    setAutoRefresh,
    undo,
    redo,
    canUndo,
    canRedo,
    isDirty,
    lastSavedAt,
  } = useDashboard();

  const [editingName, setEditingName] = useState(false);
  const [nameValue, setNameValue] = useState("");
  const [dragState, setDragState] = useState(null);
  const [resizeState, setResizeState] = useState(null);
  const [localPositions, setLocalPositions] = useState({});  // Override during drag/resize
  const latestLocalPos = useRef({});  // Ref to avoid stale closures in pointer handlers
  useEffect(() => { latestLocalPos.current = localPositions; }, [localPositions]);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [showFilterManager, setShowFilterManager] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [settingsDesc, setSettingsDesc] = useState("");
  const [showRefreshMenu, setShowRefreshMenu] = useState(false);
  const [showEmbedModal, setShowEmbedModal] = useState(false);
  const [embedCopied, setEmbedCopied] = useState(false);
  const [temporalColumns, setTemporalColumns] = useState([]);
  const [activePageId, setActivePageId] = useState(null);
  const [pageContextMenu, setPageContextMenu] = useState(null);
  const [showNewPage, setShowNewPage] = useState(false);
  const [newPageName, setNewPageName] = useState("");
  const gridRef = useRef(null);
  const refreshMenuRef = useRef(null);

  // Load temporal columns when settings panel opens
  useEffect(() => {
    if (!showSettings) return;
    api.getAutocomplete().then(data => {
      const TEMPORAL_RE = /^(DATE|TIMESTAMP|TIME|INTERVAL)/i;
      const cols = [];
      const seen = new Set();
      for (const item of (data?.columns || [])) {
        const colName = item.name;
        const colType = item.type || "";
        if (colName && TEMPORAL_RE.test(colType) && !seen.has(colName)) {
          seen.add(colName);
          cols.push(colName);
        }
      }
      setTemporalColumns(cols);
    }).catch(() => {});
  }, [showSettings]);

  // --- Responsive layout: track container width for column breakpoints ---
  const [effectiveCols, setEffectiveCols] = useState(GRID_COLS);
  useEffect(() => {
    const el = gridRef.current;
    if (!el) return;
    const ro = new ResizeObserver(entries => {
      const w = entries[0].contentRect.width;
      if (w < 768) setEffectiveCols(1);
      else if (w < 1024) setEffectiveCols(12);
      else setEffectiveCols(GRID_COLS);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [dashboard]);

  // Multi-page support
  const pages = dashboard?.settings?.pages || [{ id: "main", name: "Main" }];
  const currentPageId = activePageId || pages[0]?.id || "main";

  // Initialize activePageId when dashboard loads
  useEffect(() => {
    if (dashboard && !activePageId) {
      const ps = dashboard.settings?.pages;
      if (ps && ps.length > 0) setActivePageId(ps[0].id);
      else setActivePageId("main");
    }
  }, [dashboard?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const allWidgets = dashboard?.widgets || [];
  // Filter widgets to only show those on the active page
  const widgets = allWidgets.filter(w => {
    const wPage = w.config?.page_id;
    // Widgets with no page_id belong to the first page
    if (!wPage) return currentPageId === pages[0]?.id;
    return wPage === currentPageId;
  });

  // Fullscreen change listener
  useEffect(() => {
    const handler = () => setIsFullscreen(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", handler);
    return () => document.removeEventListener("fullscreenchange", handler);
  }, []);

  // Close refresh menu on outside click
  useEffect(() => {
    if (!showRefreshMenu) return;
    const handler = (e) => {
      if (refreshMenuRef.current && !refreshMenuRef.current.contains(e.target)) setShowRefreshMenu(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showRefreshMenu]);

  // Keyboard shortcuts
  useEffect(() => {
    const handler = (e) => {
      // Undo/Redo work even in inputs
      if ((e.ctrlKey || e.metaKey) && e.key === "z" && !e.shiftKey) {
        e.preventDefault();
        undo();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key === "z" && e.shiftKey) {
        e.preventDefault();
        redo();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key === "y") {
        e.preventDefault();
        redo();
        return;
      }
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
      if (e.key === "e" && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        setEditMode(prev => !prev);
      }
      if (e.key === "f" && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        toggleFullscreen();
      }
      if (e.key === "Escape") {
        if (isFullscreen) document.exitFullscreen?.();
        else if (editMode) setEditMode(false);
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [editMode, isFullscreen, undo, redo]); // eslint-disable-line react-hooks/exhaustive-deps

  // Warn on unsaved changes before page unload
  useEffect(() => {
    if (!isDirty) return;
    const handler = (e) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [isDirty]);

  function toggleFullscreen() {
    if (document.fullscreenElement) {
      document.exitFullscreen?.();
    } else {
      document.documentElement.requestFullscreen?.();
    }
  }

  // Embed helpers
  function getEmbedUrl() {
    return `${window.location.origin}/#embed=true&dashboard=${dashboard?.id || ""}`;
  }
  function getEmbedSnippet() {
    return `<iframe src="${getEmbedUrl()}" width="100%" height="600" frameborder="0"></iframe>`;
  }
  function copyEmbedSnippet() {
    navigator.clipboard.writeText(getEmbedSnippet()).then(() => {
      setEmbedCopied(true);
      setTimeout(() => setEmbedCopied(false), 2000);
    });
  }

  // Page management
  function addPage() {
    if (!newPageName.trim()) return;
    const newId = "page_" + Date.now();
    const newPages = [...pages, { id: newId, name: newPageName.trim() }];
    saveDashboard({ settings: { ...(dashboard.settings || {}), pages: newPages } });
    setActivePageId(newId);
    setShowNewPage(false);
    setNewPageName("");
  }

  function renamePage(pageId) {
    const page = pages.find(p => p.id === pageId);
    if (!page) return;
    const name = prompt("Rename page:", page.name);
    if (!name || !name.trim()) return;
    const newPages = pages.map(p => p.id === pageId ? { ...p, name: name.trim() } : p);
    saveDashboard({ settings: { ...(dashboard.settings || {}), pages: newPages } });
  }

  function deletePage(pageId) {
    if (pages.length <= 1) return; // can't delete last page
    const newPages = pages.filter(p => p.id !== pageId);
    saveDashboard({ settings: { ...(dashboard.settings || {}), pages: newPages } });
    if (activePageId === pageId) setActivePageId(newPages[0]?.id);
  }

  // Name editing
  function startEditName() {
    setNameValue(dashboard?.name || "");
    setEditingName(true);
  }

  function saveName() {
    setEditingName(false);
    if (nameValue.trim() && nameValue.trim() !== dashboard?.name) {
      saveDashboard({ name: nameValue.trim() });
    }
  }

  // Add new widget — opens editor with optional preset
  function handleAddWidget(preset) {
    const maxY = widgets.reduce((max, w) => {
      const pos = w.position || {};
      return Math.max(max, (pos.y || 1) + (pos.h || 4));
    }, 1);

    const defaults = {
      widget_type: "chart",
      chart_type: "bar",
      title: "",
      sql_query: "",
      config: { page_id: currentPageId },
      position: { x: 1, y: maxY, w: 8, h: 4 },
      filters: [],
      cache_ttl: 0,
      sort_order: widgets.length,
      _fromVisual: true,
    };

    // Apply preset overrides
    if (preset === "kpi") Object.assign(defaults, { widget_type: "kpi", chart_type: null, position: { x: 1, y: maxY, w: 6, h: 3 } });
    else if (preset === "table") Object.assign(defaults, { widget_type: "table", chart_type: null, position: { x: 1, y: maxY, w: 12, h: 5 } });
    else if (preset === "text") Object.assign(defaults, { widget_type: "text", chart_type: null, position: { x: 1, y: maxY, w: 6, h: 3 } });
    else if (preset === "line") Object.assign(defaults, { chart_type: "line", position: { x: 1, y: maxY, w: 12, h: 5 } });
    else if (preset === "bar") Object.assign(defaults, { chart_type: "bar", position: { x: 1, y: maxY, w: 8, h: 4 } });
    else if (preset === "image") Object.assign(defaults, { widget_type: "image", chart_type: null, position: { x: 1, y: maxY, w: 8, h: 4 } });
    else if (preset === "divider") Object.assign(defaults, { widget_type: "divider", chart_type: null, position: { x: 1, y: maxY, w: 24, h: 1 } });

    if (onEditWidget) onEditWidget(defaults);
  }

  // Duplicate widget
  async function handleDuplicate(widget) {
    const maxY = widgets.reduce((max, w) => {
      const pos = w.position || {};
      return Math.max(max, (pos.y || 1) + (pos.h || 4));
    }, 1);

    await addWidget({
      widget_type: widget.widget_type,
      chart_type: widget.chart_type,
      title: `${widget.title} (copy)`,
      sql_query: widget.sql_query,
      config: widget.config || {},
      position: { x: 1, y: maxY, w: widget.position?.w || 8, h: widget.position?.h || 4 },
      filters: widget.filters || [],
      cache_ttl: widget.cache_ttl || 0,
      sort_order: widgets.length,
    });
  }

  // Delete widget with confirmation
  async function handleDelete(widget) {
    if (showConfirm) {
      const ok = await showConfirm(
        "Delete Widget",
        `Delete "${widget.title || "Untitled"}"? This cannot be undone.`,
        "Delete",
        true
      );
      if (!ok) return;
    }
    await removeWidget(widget.id);
  }

  // --- Move (pointer events, not HTML5 drag) ---

  function handleMoveStart(e, widget) {
    if (!editMode) return;
    if (widget.config?.locked) return;
    // Only start from the drag handle (grip icon area)
    e.preventDefault();
    e.stopPropagation();
    const rect = gridRef.current?.getBoundingClientRect();
    if (!rect) return;

    setDragState({
      widgetId: widget.id,
      startX: e.clientX,
      startY: e.clientY,
      origPos: { ...widget.position },
      gridRect: rect,
    });
  }

  useEffect(() => {
    if (!dragState) return;

    function onMove(e) {
      const rect = dragState.gridRect;
      const scrollTop = gridRef.current?.scrollTop || 0;
      const colWidth = (rect.width - GAP * (GRID_COLS - 1)) / GRID_COLS;
      const dCols = Math.round((e.clientX - dragState.startX) / (colWidth + GAP));
      const dRows = Math.round((e.clientY - dragState.startY) / (ROW_HEIGHT + GAP));

      const orig = dragState.origPos;
      const newX = Math.max(1, Math.min(GRID_COLS - (orig.w || MIN_W) + 1, (orig.x || 1) + dCols));
      const newY = Math.max(1, (orig.y || 1) + dRows);

      // Local state only during drag — no API calls
      setLocalPositions(prev => ({ ...prev, [dragState.widgetId]: { ...orig, x: newX, y: newY } }));
    }

    function onUp() {
      // Persist to server on drop — read from ref to avoid stale closure
      const finalPos = latestLocalPos.current[dragState.widgetId];
      if (finalPos) {
        updatePositions([{ id: dragState.widgetId, position: finalPos }]);
      }
      setLocalPositions(prev => { const n = { ...prev }; delete n[dragState.widgetId]; return n; });
      setDragState(null);
    }

    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
    return () => {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
    };
  }, [dragState, updatePositions]);

  // --- Resize (pointer events) ---

  function handleResizeStart(e, widget) {
    if (!editMode) return;
    if (widget.config?.locked) return;
    e.preventDefault();
    e.stopPropagation();
    const rect = gridRef.current?.getBoundingClientRect();
    if (!rect) return;

    setResizeState({
      widgetId: widget.id,
      startX: e.clientX,
      startY: e.clientY,
      startW: widget.position?.w || 6,
      startH: widget.position?.h || 4,
      origPos: { ...widget.position },
      gridRect: rect,
    });
  }

  useEffect(() => {
    if (!resizeState) return;

    function onMove(e) {
      const rect = resizeState.gridRect;
      const colWidth = (rect.width - GAP * (GRID_COLS - 1)) / GRID_COLS;
      const dCols = Math.round((e.clientX - resizeState.startX) / (colWidth + GAP));
      const dRows = Math.round((e.clientY - resizeState.startY) / (ROW_HEIGHT + GAP));

      const newW = Math.max(MIN_W, resizeState.startW + dCols);
      const newH = Math.max(MIN_H, resizeState.startH + dRows);

      const orig = resizeState.origPos;
      const clampedW = Math.min(newW, GRID_COLS - (orig.x || 1) + 1);
      setLocalPositions(prev => ({ ...prev, [resizeState.widgetId]: { ...orig, w: clampedW, h: newH } }));
    }

    function onUp() {
      // Read from ref to avoid stale closure
      const finalPos = latestLocalPos.current[resizeState.widgetId];
      if (finalPos) {
        updatePositions([{ id: resizeState.widgetId, position: finalPos }]);
      }
      setLocalPositions(prev => { const n = { ...prev }; delete n[resizeState.widgetId]; return n; });
      setResizeState(null);
    }

    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
    return () => {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
    };
  }, [resizeState, updatePositions]);

  // Compute grid height
  const maxRow = widgets.reduce((max, w) => {
    const pos = w.position || {};
    return Math.max(max, (pos.y || 1) + (pos.h || 4));
  }, 2);

  if (!dashboard) return null;

  return (
    <div
      style={{ ...st.outer, ...(isFullscreen ? st.fullscreen : {}), ...(embedMode ? st.fullscreen : {}) }}
      {...(dashboard?.settings?.darkMode ? { "data-havn-dark": "true" } : {})}
    >
      {/* Toolbar — hidden in embed mode */}
      {!embedMode && <div style={st.toolbar}>
        <div style={st.toolbarLeft}>
          {!isFullscreen && (
            <button style={st.backBtn} onClick={onBack} title="Back to list">
              ←
            </button>
          )}
          {editingName ? (
            <input
              style={st.nameInput}
              value={nameValue}
              onChange={(e) => setNameValue(e.target.value)}
              onBlur={saveName}
              onKeyDown={(e) => e.key === "Enter" && saveName()}
              autoFocus
            />
          ) : (
            <h2
              style={st.dashName}
              onClick={editMode ? startEditName : undefined}
              title={editMode ? "Click to rename" : undefined}
            >
              {dashboard.name}
            </h2>
          )}
        </div>
        <div style={st.toolbarRight}>
          {/* Save indicator */}
          <span
            style={st.saveIndicator}
            title={lastSavedAt ? `Last saved ${_relativeTime(lastSavedAt)}` : "Not yet saved"}
          >
            <span style={{
              display: "inline-block",
              width: 8,
              height: 8,
              borderRadius: "50%",
              background: isDirty ? "var(--havn-yellow, #e5c07b)" : "var(--havn-green, #98c379)",
              marginRight: 5,
            }} />
            {isDirty ? "Unsaved" : "Saved"}
          </span>

          {/* Edited by */}
          {dashboard?.updated_by && (
            <span style={{ fontSize: 12, color: "var(--havn-text-secondary)", marginRight: 4 }}>
              Edited by {dashboard.updated_by}
            </span>
          )}

          {/* Undo/Redo */}
          {editMode && (
            <>
              <button
                style={{ ...st.toolBtn, ...(canUndo ? {} : st.toolBtnDisabled) }}
                onClick={undo}
                disabled={!canUndo}
                title="Undo (Ctrl+Z)"
              >
                ↶
              </button>
              <button
                style={{ ...st.toolBtn, ...(canRedo ? {} : st.toolBtnDisabled) }}
                onClick={redo}
                disabled={!canRedo}
                title="Redo (Ctrl+Shift+Z)"
              >
                ↷
              </button>
            </>
          )}

          {/* Auto-refresh */}
          <div style={{ position: "relative" }}>
            <button
              style={st.toolBtn}
              onClick={() => setShowRefreshMenu(!showRefreshMenu)}
              title="Auto-refresh"
            >
              ↻ {autoRefresh > 0 ? `${autoRefresh}s` : "Off"}
            </button>
            {showRefreshMenu && (
              <div ref={refreshMenuRef} style={st.dropdown}>
                {AUTO_REFRESH_OPTIONS.map(opt => (
                  <button
                    key={opt.value}
                    style={{
                      ...st.dropdownItem,
                      fontWeight: autoRefresh === opt.value ? 600 : 400,
                      color: autoRefresh === opt.value ? "var(--havn-accent)" : "var(--havn-text)",
                    }}
                    onClick={() => { setAutoRefresh(opt.value); setShowRefreshMenu(false); }}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
            )}
          </div>

          <button style={st.toolBtn} onClick={refreshAll} title="Refresh all widgets">
            ↻
          </button>

          <button
            style={{
              ...st.toolBtn,
              background: editMode ? "var(--havn-green, #16a34a)" : undefined,
              color: editMode ? "#fff" : undefined,
            }}
            onClick={() => {
              if (editMode) saveDashboard({});
              setEditMode(!editMode);
            }}
            title={editMode ? "Save & exit edit mode (E)" : "Edit mode (E)"}
          >
            {editMode ? "Save" : "Edit"}
          </button>

          {editMode && (
            <>
              <AddWidgetMenu onAdd={(preset) => handleAddWidget(preset)} />
              <button style={st.toolBtn} onClick={() => setShowFilterManager(true)} title="Manage dashboard filters">
                Filters
              </button>
              <button style={st.toolBtn} onClick={() => { setSettingsDesc(dashboard?.description || ""); setShowSettings(true); }} title="Dashboard settings">
                ⚙
              </button>
            </>
          )}

          <button style={st.toolBtn} onClick={() => setShowEmbedModal(true)} title="Embed this dashboard">
            Embed
          </button>

          <button style={st.toolBtn} onClick={toggleFullscreen} title="Fullscreen (F)">
            {isFullscreen ? "⊡" : "⛶"}
          </button>
        </div>
      </div>}

      {/* Page tab bar */}
      {pages.length > 0 && (
        <div style={st.pageTabBar}>
          {pages.map(page => (
            <button
              key={page.id}
              style={{
                ...st.pageTab,
                ...(currentPageId === page.id ? st.pageTabActive : {}),
              }}
              onClick={() => setActivePageId(page.id)}
              onContextMenu={(e) => {
                if (!editMode) return;
                e.preventDefault();
                setPageContextMenu({ pageId: page.id, x: e.clientX, y: e.clientY });
              }}
            >
              {page.name}
            </button>
          ))}
          {editMode && !showNewPage && (
            <button style={st.pageAddBtn} onClick={() => setShowNewPage(true)} title="Add page">
              +
            </button>
          )}
          {editMode && showNewPage && (
            <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              <input
                style={{ padding: "3px 8px", fontSize: 12, border: "1px solid var(--havn-border)", borderRadius: 4, background: "var(--havn-bg)", color: "var(--havn-text)", width: 120 }}
                placeholder="Page name"
                value={newPageName}
                onChange={(e) => setNewPageName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") addPage();
                  if (e.key === "Escape") { setShowNewPage(false); setNewPageName(""); }
                }}
                autoFocus
              />
              <button style={st.pageAddBtn} onClick={addPage} disabled={!newPageName.trim()}>OK</button>
              <button style={{ ...st.pageAddBtn, color: "var(--havn-text-secondary)" }} onClick={() => { setShowNewPage(false); setNewPageName(""); }}>&times;</button>
            </span>
          )}
          {/* Page context menu */}
          {pageContextMenu && editMode && (
            <PageContextMenu
              x={pageContextMenu.x}
              y={pageContextMenu.y}
              onRename={() => { renamePage(pageContextMenu.pageId); setPageContextMenu(null); }}
              onDelete={pages.length > 1 ? () => { deletePage(pageContextMenu.pageId); setPageContextMenu(null); } : null}
              onClose={() => setPageContextMenu(null)}
            />
          )}
        </div>
      )}

      {/* Canvas Grid */}
      <div
        ref={gridRef}
        style={{
          ...st.grid,
          gridTemplateColumns: `repeat(${effectiveCols}, 1fr)`,
          ...(editMode ? st.gridEdit : {}),
          background: dashboard?.settings?.canvasBgColor || undefined,
        }}
      >
        {(() => {
          // For single-column layout, sort by sort_order and stack
          const sorted = effectiveCols === 1
            ? [...widgets].sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0))
            : widgets;
          return sorted.map(w => {
          const pos = localPositions[w.id] || w.position || { x: 1, y: 1, w: 6, h: 4 };
          const isNarrow = effectiveCols === 1;
          return (
            <div
              key={w.id}
              style={{
                gridColumn: isNarrow ? "1 / -1" : `${pos.x} / span ${Math.min(pos.w, effectiveCols)}`,
                gridRow: isNarrow ? undefined : `${pos.y} / span ${pos.h}`,
                minHeight: isNarrow ? pos.h * ROW_HEIGHT : 0,
                minWidth: 0,
                position: "relative",
                userSelect: (dragState || resizeState) ? "none" : undefined,
                ...(editMode ? { outline: "1px dashed rgba(128,128,128,0.2)", outlineOffset: -1 } : {}),
              }}
            >
              <DashboardWidget
                widget={w}
                onEdit={() => onEditWidget?.(w)}
                onDuplicate={() => handleDuplicate(w)}
                onDelete={() => handleDelete(w)}
                onMoveStart={(e) => handleMoveStart(e, w)}
                style={{ height: "100%" }}
              />
              {/* Resize handle — pointer events, no browser drag ghost */}
              {editMode && !w.config?.locked && (
                <div
                  style={{
                    position: "absolute",
                    bottom: 0,
                    right: 0,
                    width: 20,
                    height: 20,
                    cursor: "nwse-resize",
                    zIndex: 10,
                  }}
                  onPointerDown={(e) => handleResizeStart(e, w)}
                />
              )}
            </div>
          );
        });
        })()}

        {/* Empty state */}
        {widgets.length === 0 && (
          <div style={{
            gridColumn: "1 / -1",
            gridRow: "1 / span 6",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            gap: 12,
          }}>
            <div style={{ fontSize: 40, opacity: 0.25 }}>📊</div>
            {editMode ? (
              <>
                <div style={{ fontSize: 15, fontWeight: 500, color: "var(--havn-text)" }}>
                  Click <strong>+ Add Widget</strong> to create your first visualization
                </div>
                <div style={{ fontSize: 12, color: "var(--havn-text-secondary)" }}>
                  Pick a table, select columns, and choose a chart type
                </div>
              </>
            ) : (
              <>
                <div style={{ fontSize: 15, fontWeight: 500, color: "var(--havn-text)" }}>
                  No widgets yet
                </div>
                <button
                  style={{ ...st.addBtn, marginTop: 4 }}
                  onClick={() => setEditMode(true)}
                >
                  Start editing
                </button>
              </>
            )}
          </div>
        )}
      </div>

      {/* Fullscreen: show filter bar floating at top */}
      {isFullscreen && (dashboard?.filters?.length > 0 || false) && (
        <div style={st.fullscreenFilterBar}>
          <DashboardFilterBar />
        </div>
      )}

      {/* Filter manager modal */}
      {showFilterManager && (
        <DashboardFilterManager onClose={() => setShowFilterManager(false)} />
      )}

      {/* Settings panel */}
      {showSettings && (
        <div style={st.settingsOverlay} onClick={(e) => e.target === e.currentTarget && setShowSettings(false)}>
          <div style={st.settingsPanel}>
            <div style={st.settingsHeader}>
              <h3 style={{ margin: 0, fontSize: 16, fontWeight: 600, color: "var(--havn-text)" }}>Dashboard Settings</h3>
              <button style={{ background: "none", border: "none", color: "var(--havn-text-secondary)", fontSize: 20, cursor: "pointer" }} onClick={() => setShowSettings(false)}>×</button>
            </div>
            <div style={st.settingsBody}>
              <label style={st.settingsLabel}>Name</label>
              <input style={st.settingsInput} value={nameValue || dashboard?.name || ""} onChange={(e) => setNameValue(e.target.value)}
                onBlur={() => { if (nameValue.trim() && nameValue !== dashboard?.name) saveDashboard({ name: nameValue.trim() }); }} />

              <label style={st.settingsLabel}>Description</label>
              <textarea style={{ ...st.settingsInput, minHeight: 60, resize: "vertical" }} value={settingsDesc}
                onChange={(e) => setSettingsDesc(e.target.value)}
                onBlur={() => saveDashboard({ description: settingsDesc })}
                placeholder="What does this dashboard show?" />

              <label style={st.settingsLabel}>Canvas color</label>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <input
                  type="color"
                  value={dashboard?.settings?.canvasBgColor || "#1e1e2e"}
                  style={{ width: 36, height: 32, border: "1px solid var(--havn-border)", borderRadius: 4, padding: 1, cursor: "pointer", background: "none" }}
                  onChange={(e) => saveDashboard({ settings: { ...dashboard.settings, canvasBgColor: e.target.value } })}
                />
                {dashboard?.settings?.canvasBgColor && (
                  <button
                    style={{ background: "none", border: "1px solid var(--havn-border)", color: "var(--havn-text-secondary)", cursor: "pointer", padding: "4px 8px", borderRadius: 4, fontSize: 11 }}
                    onClick={() => saveDashboard({ settings: { ...dashboard.settings, canvasBgColor: undefined } })}
                  >
                    Reset
                  </button>
                )}
              </div>

              <label style={{ ...st.settingsLabel, marginTop: 12, display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                <input
                  type="checkbox"
                  checked={!!dashboard?.settings?.darkMode}
                  onChange={(e) => saveDashboard({ settings: { ...dashboard.settings, darkMode: e.target.checked } })}
                  style={{ accentColor: "var(--havn-accent)" }}
                />
                Dark mode
              </label>

              <label style={{ ...st.settingsLabel, marginTop: 12 }}>Default time column</label>
              <select
                style={st.settingsInput}
                value={dashboard?.settings?.default_time_column || ""}
                onChange={(e) => saveDashboard({ settings: { ...dashboard.settings, default_time_column: e.target.value || undefined } })}
              >
                <option value="">None</option>
                {temporalColumns.map(c => (
                  <option key={c} value={c}>{c}</option>
                ))}
              </select>
              <div style={{ fontSize: 10, color: "var(--havn-text-secondary)", marginTop: 2 }}>
                Auto-select this column when adding widgets via the visual builder
              </div>

              <div style={{ marginTop: 16, fontSize: 12, color: "var(--havn-text-secondary)" }}>
                <div>Created by: {dashboard?.created_by || "—"}</div>
                <div>Created: {dashboard?.created_at || "—"}</div>
                <div>Updated: {dashboard?.updated_at || "—"}</div>
                <div>Widgets: {widgets.length}</div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Embed modal */}
      {showEmbedModal && (
        <div style={st.settingsOverlay} onClick={(e) => e.target === e.currentTarget && setShowEmbedModal(false)}>
          <div style={st.settingsPanel}>
            <div style={st.settingsHeader}>
              <h3 style={{ margin: 0, fontSize: 16, fontWeight: 600, color: "var(--havn-text)" }}>Embed Dashboard</h3>
              <button style={{ background: "none", border: "none", color: "var(--havn-text-secondary)", fontSize: 20, cursor: "pointer" }} onClick={() => setShowEmbedModal(false)}>×</button>
            </div>
            <div style={st.settingsBody}>
              <label style={st.settingsLabel}>Embed URL</label>
              <input style={st.settingsInput} readOnly value={getEmbedUrl()} onClick={(e) => e.target.select()} />

              <label style={{ ...st.settingsLabel, marginTop: 16 }}>iframe Snippet</label>
              <textarea
                style={{ ...st.settingsInput, minHeight: 60, resize: "vertical", fontFamily: "var(--havn-font-mono)", fontSize: 12 }}
                readOnly
                value={getEmbedSnippet()}
                onClick={(e) => e.target.select()}
              />

              <button
                style={{ ...st.addBtn, marginTop: 12, width: "100%", textAlign: "center" }}
                onClick={copyEmbedSnippet}
              >
                {embedCopied ? "Copied!" : "Copy iframe snippet"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// Page context menu (right-click on page tab)
function PageContextMenu({ x, y, onRename, onDelete, onClose }) {
  const ref = useRef(null);
  useEffect(() => {
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) onClose(); };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [onClose]);

  return (
    <div ref={ref} style={{ position: "fixed", left: x, top: y, background: "var(--havn-bg)", border: "1px solid var(--havn-border)", borderRadius: 6, padding: 4, zIndex: 300, minWidth: 120, boxShadow: "0 4px 12px rgba(0,0,0,0.2)" }}>
      <button
        style={{ display: "block", width: "100%", background: "none", border: "none", color: "var(--havn-text)", cursor: "pointer", padding: "6px 10px", fontSize: 13, textAlign: "left", borderRadius: 4 }}
        onClick={onRename}
        onMouseEnter={(e) => e.currentTarget.style.background = "var(--havn-bg-secondary, rgba(128,128,128,0.1))"}
        onMouseLeave={(e) => e.currentTarget.style.background = "none"}
      >
        Rename
      </button>
      {onDelete && (
        <button
          style={{ display: "block", width: "100%", background: "none", border: "none", color: "var(--havn-red)", cursor: "pointer", padding: "6px 10px", fontSize: 13, textAlign: "left", borderRadius: 4 }}
          onClick={onDelete}
          onMouseEnter={(e) => e.currentTarget.style.background = "var(--havn-bg-secondary, rgba(128,128,128,0.1))"}
          onMouseLeave={(e) => e.currentTarget.style.background = "none"}
        >
          Delete
        </button>
      )}
    </div>
  );
}

// Quick-add widget menu
function AddWidgetMenu({ onAdd }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  const templates = [
    { id: null, label: "Custom", desc: "Full visual builder", icon: "+" },
    { id: "bar", label: "Bar Chart", desc: "Compare categories", icon: "▦" },
    { id: "line", label: "Time Series", desc: "Trend over time", icon: "╱" },
    { id: "kpi", label: "KPI Card", desc: "Big number + delta", icon: "#" },
    { id: "table", label: "Data Table", desc: "Rows and columns", icon: "≡" },
    { id: "text", label: "Text Note", desc: "Markdown content", icon: "¶" },
    { id: "image", label: "Image", desc: "Embed an image", icon: "🖼" },
    { id: "divider", label: "Divider", desc: "Horizontal separator", icon: "—" },
  ];

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button style={st.addBtn} onClick={() => setOpen(!open)}>
        + Add Widget
      </button>
      {open && (
        <div style={st.addMenu}>
          {templates.map(t => (
            <button key={t.id || "custom"} style={st.addMenuItem}
              onClick={() => { setOpen(false); onAdd(t.id); }}
              onMouseEnter={(e) => e.currentTarget.style.background = "var(--havn-bg-secondary, rgba(128,128,128,0.1))"}
              onMouseLeave={(e) => e.currentTarget.style.background = "none"}>
              <span style={st.addMenuIcon}>{t.icon}</span>
              <div>
                <div style={st.addMenuLabel}>{t.label}</div>
                <div style={st.addMenuDesc}>{t.desc}</div>
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

const st = {
  outer: {
    display: "flex",
    flexDirection: "column",
    height: "100%",
    overflow: "hidden",
  },
  fullscreen: {
    position: "fixed",
    inset: 0,
    zIndex: 9999,
    background: "var(--havn-bg)",
  },
  toolbar: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "8px 16px",
    borderBottom: "1px solid var(--havn-border)",
    flexShrink: 0,
    background: "var(--havn-bg)",
  },
  toolbarLeft: {
    display: "flex",
    alignItems: "center",
    gap: 10,
  },
  toolbarRight: {
    display: "flex",
    alignItems: "center",
    gap: 6,
  },
  backBtn: {
    background: "none",
    border: "none",
    color: "var(--havn-text-secondary)",
    cursor: "pointer",
    fontSize: 18,
    padding: "2px 8px",
  },
  dashName: {
    margin: 0,
    fontSize: 16,
    fontWeight: 600,
    color: "var(--havn-text)",
    cursor: "default",
  },
  nameInput: {
    fontSize: 16,
    fontWeight: 600,
    color: "var(--havn-text)",
    background: "var(--havn-bg-secondary, var(--havn-bg))",
    border: "1px solid var(--havn-accent)",
    borderRadius: 4,
    padding: "2px 8px",
    outline: "none",
  },
  toolBtn: {
    background: "none",
    border: "1px solid var(--havn-border)",
    color: "var(--havn-text-secondary)",
    cursor: "pointer",
    padding: "5px 10px",
    borderRadius: 5,
    fontSize: 13,
  },
  toolBtnDisabled: {
    opacity: 0.35,
    cursor: "default",
  },
  saveIndicator: {
    fontSize: 12,
    color: "var(--havn-text-secondary)",
    display: "flex",
    alignItems: "center",
    marginRight: 4,
    userSelect: "none",
  },
  addBtn: {
    background: "var(--havn-accent)",
    border: "none",
    color: "#fff",
    cursor: "pointer",
    padding: "5px 12px",
    borderRadius: 5,
    fontSize: 13,
    fontWeight: 500,
  },
  dropdown: {
    position: "absolute",
    top: "100%",
    right: 0,
    background: "var(--havn-bg)",
    border: "1px solid var(--havn-border)",
    borderRadius: 6,
    padding: 4,
    zIndex: 100,
    minWidth: 80,
    boxShadow: "0 4px 12px rgba(0,0,0,0.2)",
    marginTop: 4,
  },
  dropdownItem: {
    display: "block",
    width: "100%",
    background: "none",
    border: "none",
    cursor: "pointer",
    padding: "5px 10px",
    fontSize: 13,
    textAlign: "left",
    borderRadius: 4,
  },
  grid: {
    display: "grid",
    gridTemplateColumns: `repeat(${GRID_COLS}, 1fr)`,  // default; overridden inline by effectiveCols
    gridAutoRows: `${ROW_HEIGHT}px`,
    gap: GAP,
    padding: 16,
    flex: 1,
    overflow: "auto",
    position: "relative",
  },
  gridEdit: {},
  addMenu: {
    position: "absolute",
    top: "100%",
    right: 0,
    marginTop: 4,
    background: "var(--havn-bg)",
    border: "1px solid var(--havn-border)",
    borderRadius: 8,
    padding: 4,
    zIndex: 200,
    minWidth: 200,
    boxShadow: "0 8px 24px rgba(0,0,0,0.2)",
  },
  addMenuItem: {
    display: "flex",
    alignItems: "center",
    gap: 10,
    width: "100%",
    background: "none",
    border: "none",
    color: "var(--havn-text)",
    cursor: "pointer",
    padding: "8px 12px",
    borderRadius: 6,
    textAlign: "left",
    fontSize: 13,
    transition: "background 0.1s",
  },
  addMenuIcon: {
    width: 28,
    height: 28,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    background: "var(--havn-bg-secondary, var(--havn-bg))",
    border: "1px solid var(--havn-border)",
    borderRadius: 6,
    fontSize: 14,
    fontWeight: 700,
    color: "var(--havn-accent)",
    flexShrink: 0,
  },
  addMenuLabel: { fontWeight: 600, fontSize: 13, color: "var(--havn-text)" },
  addMenuDesc: { fontSize: 11, color: "var(--havn-text-secondary)", marginTop: 1 },
  fullscreenFilterBar: { position: "fixed", top: 0, left: 0, right: 0, zIndex: 10000, background: "var(--havn-bg)", borderBottom: "1px solid var(--havn-border)" },
  settingsOverlay: { position: "fixed", inset: 0, background: "rgba(0,0,0,0.4)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 2000 },
  settingsPanel: { background: "var(--havn-bg)", border: "1px solid var(--havn-border)", borderRadius: 12, width: 420, maxWidth: "90vw" },
  settingsHeader: { display: "flex", alignItems: "center", justifyContent: "space-between", padding: "12px 20px", borderBottom: "1px solid var(--havn-border)" },
  settingsBody: { padding: 20 },
  settingsLabel: { display: "block", fontSize: 12, fontWeight: 600, color: "var(--havn-text-secondary)", marginBottom: 4, marginTop: 12 },
  settingsInput: { width: "100%", padding: "7px 10px", border: "1px solid var(--havn-border)", borderRadius: 6, background: "var(--havn-bg-secondary, var(--havn-bg))", color: "var(--havn-text)", fontSize: 13, outline: "none", boxSizing: "border-box" },
  pageTabBar: {
    display: "flex",
    alignItems: "center",
    gap: 2,
    padding: "0 16px",
    borderBottom: "1px solid var(--havn-border)",
    flexShrink: 0,
    background: "var(--havn-bg)",
    overflowX: "auto",
  },
  pageTab: {
    background: "none",
    border: "none",
    borderBottom: "2px solid transparent",
    color: "var(--havn-text-secondary)",
    cursor: "pointer",
    padding: "8px 14px",
    fontSize: 13,
    fontWeight: 500,
    whiteSpace: "nowrap",
  },
  pageTabActive: {
    color: "var(--havn-accent)",
    borderBottomColor: "var(--havn-accent)",
    fontWeight: 600,
  },
  pageAddBtn: {
    background: "none",
    border: "1px dashed var(--havn-border)",
    color: "var(--havn-text-secondary)",
    cursor: "pointer",
    padding: "4px 10px",
    borderRadius: 4,
    fontSize: 14,
    marginLeft: 4,
  },
};
