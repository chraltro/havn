import React, { createContext, useContext, useState, useCallback, useRef, useEffect, useMemo } from "react";
import { api } from "./api";

const DashboardContext = createContext(null);

const MAX_UNDO = 30;

// Undo covers what PUT /api/dashboards/{id} can write back: the dashboard's
// own fields. Widgets are saved through their own endpoints the moment they
// change, and that PUT has no widgets field, so restoring a snapshot could
// never bring back a deleted widget or revert an edit, only claim "Saved".
// A widget change leaves a marker instead, so Undo says it cannot be undone
// rather than silently skipping back to an older change.
const UNDOABLE_FIELDS = ["name", "description", "layout", "filters", "settings"];
const WIDGET_CHANGE = { widgetChange: true };

function undoableFields(d) {
  const out = {};
  for (const k of UNDOABLE_FIELDS) if (d && k in d) out[k] = d[k];
  return JSON.parse(JSON.stringify(out));
}

// Saved views are not on the undo stack: a restored snapshot keeps the views
// the dashboard has now instead of deleting ones saved since.
function withCurrentViews(snapshot, current) {
  if (!("settings" in snapshot)) return snapshot;
  return { ...snapshot, settings: { ...(snapshot.settings || {}), saved_views: current?.settings?.saved_views } };
}

export function DashboardProvider({ children }) {
  const [dashboard, setDashboard] = useState(null);
  const [editMode, setEditMode] = useState(false);
  const [globalFilters, setGlobalFilters] = useState({});
  const [crossFilter, setCrossFilter] = useState(null);
  const [widgetData, setWidgetData] = useState({});
  const [parameters, setParameters] = useState({});
  const [autoRefresh, setAutoRefresh] = useState(0);
  const refreshTimerRef = useRef(null);

  // Undo/Redo stacks
  const [undoStack, setUndoStack] = useState([]);
  const [redoStack, setRedoStack] = useState([]);

  // Dirty / save tracking
  const [isDirty, setIsDirty] = useState(false);
  const [lastSavedAt, setLastSavedAt] = useState(null);

  // Saved filter views
  const [savedViews, setSavedViews] = useState([]);

  // Toast notifications
  const [toasts, setToasts] = useState([]);
  const toastIdRef = useRef(0);
  const showToast = useCallback((message, type = "info", duration) => {
    const id = ++toastIdRef.current;
    setToasts(prev => [...prev, { id, message, type }]);
    // Errors stay up longer: they are read, not glanced at.
    setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), duration ?? (type === "error" ? 6000 : 3000));
  }, []);

  // A failed write: the console for debugging, a toast so the user knows.
  const reportError = useCallback((what, e) => {
    console.error(`${what}:`, e);
    showToast(`${what}: ${e?.message || e}`, "error");
  }, [showToast]);

  // Push the dashboard-level fields onto the undo stack. Called once a write
  // has succeeded, so a failed save leaves nothing behind to "undo".
  const pushUndo = useCallback(() => {
    setUndoStack(prev => {
      const next = [...prev, undoableFields(dashboard)];
      if (next.length > MAX_UNDO) next.shift();
      return next;
    });
    setRedoStack([]);
  }, [dashboard]);

  // Mark a widget change on the undo stack (see UNDOABLE_FIELDS). A run of
  // widget changes shares one marker.
  const pushWidgetChange = useCallback(() => {
    setUndoStack(prev => {
      if (prev[prev.length - 1]?.widgetChange) return prev;
      const next = [...prev, WIDGET_CHANGE];
      if (next.length > MAX_UNDO) next.shift();
      return next;
    });
    setRedoStack([]);
  }, []);

  // Load a dashboard by ID
  const loadDashboard = useCallback(async (id) => {
    try {
      const full = await api.getDashboard(id);
      setDashboard(full);
      setWidgetData({});
      setGlobalFilters({});
      // Auto-enter edit mode for new/empty dashboards
      if (!full.widgets || full.widgets.length === 0) setEditMode(true);
      setCrossFilter(null);
      // Initialize parameters from dashboard settings
      const params = full.settings?.parameters || [];
      const defaults = {};
      for (const p of params) {
        defaults[p.name] = p.default ?? "";
      }
      setParameters(defaults);
      // Initialize saved views
      setSavedViews(full.settings?.saved_views || []);
      // Reset undo/redo and dirty state on load
      setUndoStack([]);
      setRedoStack([]);
      setIsDirty(false);
      setLastSavedAt(null);
      // Restore filters from URL if present
      try {
        const urlParams = new URLSearchParams(window.location.search);
        const restored = {};
        for (const [key, val] of urlParams.entries()) {
          if (!key.startsWith("f_")) continue;
          const filterId = key.slice(2);
          try { restored[filterId] = JSON.parse(val); } catch { restored[filterId] = val; }
        }
        if (Object.keys(restored).length > 0) setGlobalFilters(restored);
      } catch (_) {}
      return full;
    } catch (e) {
      reportError("Couldn't open the dashboard", e);
      return null;
    }
  }, [reportError]);

  // Close/unload dashboard
  const closeDashboard = useCallback(() => {
    setDashboard(null);
    setWidgetData({});
    setEditMode(false);
    setGlobalFilters({});
    setCrossFilter(null);
    setParameters({});
    setSavedViews([]);
    setUndoStack([]);
    setRedoStack([]);
    setIsDirty(false);
    setLastSavedAt(null);
  }, []);

  // Build combined filters for a query
  const _buildFilters = useCallback(() => {
    const filters = { ...globalFilters };
    if (crossFilter) {
      filters[crossFilter.column] = crossFilter.value;
    }
    return filters;
  }, [globalFilters, crossFilter]);

  // Query a single widget
  const refreshWidget = useCallback(async (widgetId) => {
    if (!dashboard) return;
    setWidgetData(prev => ({
      ...prev,
      [widgetId]: { ...prev[widgetId], loading: true, error: null },
    }));
    const startTime = Date.now();
    try {
      const result = await api.queryWidget(dashboard.id, widgetId, _buildFilters(), parameters);
      const duration = Date.now() - startTime;
      setWidgetData(prev => ({
        ...prev,
        [widgetId]: { ...result, loading: false, error: result.error || null, _fetchedAt: new Date().toISOString(), _queryDuration: duration },
      }));
    } catch (e) {
      const duration = Date.now() - startTime;
      setWidgetData(prev => ({
        ...prev,
        [widgetId]: { columns: [], rows: [], row_count: 0, loading: false, error: e.message, _queryDuration: duration },
      }));
    }
  }, [dashboard, _buildFilters, parameters]);

  // Batch query all widgets. Responses can arrive out of order when filters
  // change mid-flight, so each call takes a sequence number and only the
  // latest one is allowed to write results.
  const batchSeqRef = useRef(0);
  const refreshAll = useCallback(async () => {
    if (!dashboard) return;
    const seq = ++batchSeqRef.current;
    // Mark all as loading
    const loadingState = {};
    for (const w of dashboard.widgets || []) {
      if (w.sql_query) {
        loadingState[w.id] = { ...widgetData[w.id], loading: true, error: null };
      }
    }
    setWidgetData(prev => ({ ...prev, ...loadingState }));

    const batchStart = Date.now();
    try {
      const batch = await api.queryDashboardBatch(dashboard.id, _buildFilters(), parameters);
      if (seq !== batchSeqRef.current) return; // a newer refresh owns the state
      const batchDuration = Date.now() - batchStart;
      const results = batch.results || {};
      setWidgetData(prev => {
        const next = { ...prev };
        for (const [wid, result] of Object.entries(results)) {
          next[wid] = { ...result, loading: false, _fetchedAt: new Date().toISOString(), _queryDuration: batchDuration };
        }
        return next;
      });
    } catch (e) {
      console.error("Batch query failed:", e);
      if (seq !== batchSeqRef.current) return;
      // Clear the spinners and surface the failure — otherwise every widget
      // stays "loading" forever after a network blip.
      setWidgetData(prev => {
        const next = { ...prev };
        for (const w of dashboard.widgets || []) {
          if (w.sql_query && next[w.id]) {
            next[w.id] = { ...next[w.id], loading: false, error: e.message || "Batch query failed" };
          }
        }
        return next;
      });
    }
  }, [dashboard, _buildFilters, parameters, widgetData]);

  // Sync filter state to URL search params
  const syncFiltersToUrl = useCallback((filters) => {
    try {
      const url = new URL(window.location.href);
      // Clear old filter params
      for (const key of [...url.searchParams.keys()]) {
        if (key.startsWith("f_")) url.searchParams.delete(key);
      }
      // Set new filter params
      for (const [k, v] of Object.entries(filters)) {
        if (v !== null && v !== undefined && v !== "") {
          url.searchParams.set(`f_${k}`, typeof v === "object" ? JSON.stringify(v) : String(v));
        }
      }
      window.history.replaceState(null, "", url.toString());
    } catch (_) { /* ignore in non-browser env */ }
  }, []);

  // Restore filters from URL on dashboard load
  const restoreFiltersFromUrl = useCallback(() => {
    try {
      const params = new URLSearchParams(window.location.search);
      const restored = {};
      for (const [key, val] of params.entries()) {
        if (!key.startsWith("f_")) continue;
        const filterId = key.slice(2);
        try { restored[filterId] = JSON.parse(val); } catch { restored[filterId] = val; }
      }
      if (Object.keys(restored).length > 0) setGlobalFilters(restored);
    } catch (_) {}
  }, []);

  // Set a global filter and re-query
  const setFilter = useCallback((filterId, value) => {
    setGlobalFilters(prev => {
      const next = { ...prev };
      if (value === null || value === undefined || value === "") {
        delete next[filterId];
      } else {
        next[filterId] = value;
      }
      syncFiltersToUrl(next);
      return next;
    });
  }, [syncFiltersToUrl]);

  // Set cross-filter from a chart click
  const setCrossFilterValue = useCallback((widgetId, column, value) => {
    setCrossFilter(prev => {
      // Toggle off if same filter clicked again
      if (prev && prev.sourceWidgetId === widgetId && prev.column === column && prev.value === value) {
        return null;
      }
      return { sourceWidgetId: widgetId, column, value };
    });
  }, []);

  const clearCrossFilter = useCallback(() => setCrossFilter(null), []);

  // Set parameter value
  const setParameter = useCallback((name, value) => {
    setParameters(prev => ({ ...prev, [name]: value }));
  }, []);

  // Timers call through this ref: a closure captured when the timer was set
  // would run a refreshAll with stale filters and parameters.
  const refreshAllRef = useRef(refreshAll);
  refreshAllRef.current = refreshAll;

  // Auto-refresh on filter/parameter/crossFilter changes (debounced)
  const filterDebounceRef = useRef(null);
  useEffect(() => {
    if (!dashboard || !dashboard.widgets?.some(w => w.sql_query)) return;
    if (filterDebounceRef.current) clearTimeout(filterDebounceRef.current);
    filterDebounceRef.current = setTimeout(() => refreshAllRef.current(), 300);
    return () => { if (filterDebounceRef.current) clearTimeout(filterDebounceRef.current); };
  }, [globalFilters, crossFilter, parameters]); // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-refresh timer
  useEffect(() => {
    if (refreshTimerRef.current) {
      clearInterval(refreshTimerRef.current);
      refreshTimerRef.current = null;
    }
    if (autoRefresh > 0 && dashboard) {
      refreshTimerRef.current = setInterval(() => {
        if (!document.hidden) {
          refreshAllRef.current();
        }
      }, autoRefresh * 1000);
    }
    return () => {
      if (refreshTimerRef.current) clearInterval(refreshTimerRef.current);
    };
  }, [autoRefresh, dashboard]); // eslint-disable-line react-hooks/exhaustive-deps

  // Update dashboard on server. Resolves true once saved, false on failure.
  const saveDashboard = useCallback(async (updates) => {
    if (!dashboard) return false;
    try {
      const result = await api.updateDashboard(dashboard.id, updates);
      if (Object.keys(updates || {}).length > 0) pushUndo();
      setDashboard(prev => ({ ...prev, ...updates, ...result }));
      setIsDirty(false);
      setLastSavedAt(new Date());
      return true;
    } catch (e) {
      reportError("Couldn't save the dashboard", e);
      return false;
    }
  }, [dashboard, pushUndo, reportError]);

  // Add widget. Resolves to the new widget, or null on failure.
  const addWidget = useCallback(async (widgetData) => {
    if (!dashboard) return null;
    try {
      const w = await api.addWidget(dashboard.id, widgetData);
      pushWidgetChange();
      setDashboard(prev => ({
        ...prev,
        widgets: [...(prev.widgets || []), w],
      }));
      setIsDirty(true);
      return w;
    } catch (e) {
      reportError("Couldn't add the widget", e);
      return null;
    }
  }, [dashboard, pushWidgetChange, reportError]);

  // Update widget. Resolves to the saved widget, or null on failure.
  const updateWidget = useCallback(async (widgetId, updates) => {
    if (!dashboard) return null;
    try {
      const w = await api.updateWidget(dashboard.id, widgetId, updates);
      pushWidgetChange();
      setDashboard(prev => ({
        ...prev,
        widgets: (prev.widgets || []).map(ww => ww.id === widgetId ? { ...ww, ...w } : ww),
      }));
      setIsDirty(true);
      return w;
    } catch (e) {
      reportError("Couldn't save the widget", e);
      return null;
    }
  }, [dashboard, pushWidgetChange, reportError]);

  // Delete widget. Resolves true once deleted.
  const removeWidget = useCallback(async (widgetId) => {
    if (!dashboard) return false;
    try {
      await api.deleteWidget(dashboard.id, widgetId);
      pushWidgetChange();
      setDashboard(prev => ({
        ...prev,
        widgets: (prev.widgets || []).filter(w => w.id !== widgetId),
      }));
      setWidgetData(prev => {
        const next = { ...prev };
        delete next[widgetId];
        return next;
      });
      setIsDirty(true);
      return true;
    } catch (e) {
      reportError("Couldn't delete the widget", e);
      return false;
    }
  }, [dashboard, pushWidgetChange, reportError]);

  // Batch position update. The canvas only previews a drag locally, so on
  // failure the widget simply stays where the server has it.
  const updatePositions = useCallback(async (positions) => {
    if (!dashboard) return false;
    try {
      await api.updateWidgetPositions(dashboard.id, positions);
      pushWidgetChange();
      setDashboard(prev => ({
        ...prev,
        widgets: (prev.widgets || []).map(w => {
          const update = positions.find(p => p.id === w.id);
          return update ? { ...w, position: update.position } : w;
        }),
      }));
      setIsDirty(true);
      return true;
    } catch (e) {
      reportError("Couldn't save the widget layout", e);
      return false;
    }
  }, [dashboard, pushWidgetChange, reportError]);

  // Undo/redo restore a snapshot's dashboard-level fields and write them back.
  // One restore at a time, so two quick presses cannot land out of order.
  const restoringRef = useRef(false);

  // Undo: pop from undoStack, push current to redoStack, apply popped state and persist
  const undo = useCallback(async () => {
    if (undoStack.length === 0 || !dashboard || restoringRef.current) return;
    const entry = undoStack[undoStack.length - 1];
    setUndoStack(s => s.slice(0, -1));
    if (entry.widgetChange) {
      showToast("Widget changes are saved as you make them and can't be undone.", "info", 5000);
      return;
    }
    const current = undoableFields(dashboard);
    const restored = withCurrentViews(entry, dashboard);
    setRedoStack(s => {
      const next = [...s, current];
      if (next.length > MAX_UNDO) next.shift();
      return next;
    });
    setDashboard(prev => ({ ...prev, ...restored }));
    restoringRef.current = true;
    try {
      await api.updateDashboard(dashboard.id, restored);
      setIsDirty(false);
      setLastSavedAt(new Date());
    } catch (e) {
      // The server still has the current fields: show them again and keep the
      // entry so the undo can be retried.
      setDashboard(prev => ({ ...prev, ...current }));
      setUndoStack(s => [...s, entry]);
      setRedoStack(s => s.slice(0, -1));
      reportError("Couldn't undo", e);
    } finally {
      restoringRef.current = false;
    }
  }, [undoStack, dashboard, showToast, reportError]);

  // Redo: pop from redoStack, push current to undoStack, apply popped state and persist
  const redo = useCallback(async () => {
    if (redoStack.length === 0 || !dashboard || restoringRef.current) return;
    const entry = redoStack[redoStack.length - 1];
    const current = undoableFields(dashboard);
    const restored = withCurrentViews(entry, dashboard);
    setRedoStack(s => s.slice(0, -1));
    setUndoStack(s => {
      const n = [...s, current];
      if (n.length > MAX_UNDO) n.shift();
      return n;
    });
    setDashboard(prev => ({ ...prev, ...restored }));
    restoringRef.current = true;
    try {
      await api.updateDashboard(dashboard.id, restored);
      setIsDirty(false);
      setLastSavedAt(new Date());
    } catch (e) {
      setDashboard(prev => ({ ...prev, ...current }));
      setRedoStack(s => [...s, entry]);
      setUndoStack(s => s.slice(0, -1));
      reportError("Couldn't redo", e);
    } finally {
      restoringRef.current = false;
    }
  }, [redoStack, dashboard, reportError]);

  const canUndo = undoStack.length > 0;
  const canRedo = redoStack.length > 0;

  // Saved filter views
  const saveView = useCallback(async (name) => {
    if (!dashboard) return;
    const view = {
      id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
      name,
      filters: { ...globalFilters },
      parameters: { ...parameters },
      created_at: new Date().toISOString(),
    };
    const updated = [...savedViews, view];
    setSavedViews(updated);
    try {
      await api.updateDashboard(dashboard.id, { settings: { ...dashboard.settings, saved_views: updated } });
      setDashboard(prev => ({ ...prev, settings: { ...prev.settings, saved_views: updated } }));
    } catch (e) {
      setSavedViews(prev => prev.filter(v => v.id !== view.id));
      reportError("Couldn't save the view", e);
    }
  }, [dashboard, globalFilters, parameters, savedViews, reportError]);

  const loadView = useCallback((viewId) => {
    const view = savedViews.find(v => v.id === viewId);
    if (!view) return;
    // Apply filters
    setGlobalFilters(view.filters || {});
    // Apply parameters
    if (view.parameters) {
      setParameters(view.parameters);
    }
  }, [savedViews]);

  const deleteView = useCallback(async (viewId) => {
    if (!dashboard) return;
    const index = savedViews.findIndex(v => v.id === viewId);
    if (index < 0) return;
    const removed = savedViews[index];
    const updated = savedViews.filter(v => v.id !== viewId);
    setSavedViews(updated);
    try {
      await api.updateDashboard(dashboard.id, { settings: { ...dashboard.settings, saved_views: updated } });
      setDashboard(prev => ({ ...prev, settings: { ...prev.settings, saved_views: updated } }));
    } catch (e) {
      // Put the view back where it was.
      setSavedViews(prev => prev.some(v => v.id === viewId) ? prev : [...prev.slice(0, index), removed, ...prev.slice(index)]);
      reportError("Couldn't delete the view", e);
    }
  }, [dashboard, savedViews, reportError]);

  const value = {
    dashboard,
    editMode,
    setEditMode,
    globalFilters,
    crossFilter,
    widgetData,
    parameters,
    autoRefresh,
    setAutoRefresh,
    loadDashboard,
    closeDashboard,
    saveDashboard,
    setFilter,
    setCrossFilter: setCrossFilterValue,
    clearCrossFilter,
    setParameter,
    refreshWidget,
    refreshAll,
    addWidget,
    updateWidget,
    removeWidget,
    updatePositions,
    undo,
    redo,
    canUndo,
    canRedo,
    isDirty,
    lastSavedAt,
    savedViews,
    saveView,
    loadView,
    deleteView,
    toasts,
    showToast,
  };

  return (
    <DashboardContext.Provider value={value}>
      {children}
      {/* Toast notifications */}
      {toasts.length > 0 && (
        <div style={{
          position: "fixed", bottom: 20, right: 20, zIndex: 10001,
          display: "flex", flexDirection: "column", gap: 8, pointerEvents: "none",
        }}>
          {toasts.map(t => (
            <div key={t.id} role={t.type === "error" ? "alert" : "status"} style={{
              background: t.type === "error" ? "var(--havn-red)" : t.type === "success" ? "var(--havn-green)" : "var(--havn-bg-secondary, #1e1e2e)",
              color: t.type === "error" || t.type === "success" ? "#fff" : "var(--havn-text)",
              border: "1px solid var(--havn-border)",
              borderRadius: 8, padding: "8px 16px", fontSize: 13,
              boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
              animation: "fadeInUp 0.2s ease-out",
              pointerEvents: "auto",
            }}>
              {t.message}
            </div>
          ))}
        </div>
      )}
    </DashboardContext.Provider>
  );
}

export function useDashboard() {
  const ctx = useContext(DashboardContext);
  if (!ctx) throw new Error("useDashboard must be used within DashboardProvider");
  return ctx;
}

export default DashboardContext;
