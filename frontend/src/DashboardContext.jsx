import React, { createContext, useContext, useState, useCallback, useRef, useEffect, useMemo } from "react";
import { api } from "./api";

const DashboardContext = createContext(null);

const MAX_UNDO = 30;

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

  // Push current dashboard state onto undo stack before a mutation
  const pushUndo = useCallback(() => {
    setUndoStack(prev => {
      const snapshot = JSON.parse(JSON.stringify(dashboard));
      const next = [...prev, snapshot];
      if (next.length > MAX_UNDO) next.shift();
      return next;
    });
    setRedoStack([]);
  }, [dashboard]);

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
      return full;
    } catch (e) {
      console.error("Failed to load dashboard:", e);
      return null;
    }
  }, []);

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

  // Batch query all widgets
  const refreshAll = useCallback(async () => {
    if (!dashboard) return;
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
    }
  }, [dashboard, _buildFilters, parameters, widgetData]);

  // Set a global filter and re-query
  const setFilter = useCallback((filterId, value) => {
    setGlobalFilters(prev => {
      if (value === null || value === undefined || value === "") {
        const next = { ...prev };
        delete next[filterId];
        return next;
      }
      return { ...prev, [filterId]: value };
    });
  }, []);

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

  // Auto-refresh on filter/parameter/crossFilter changes (debounced)
  const filterDebounceRef = useRef(null);
  useEffect(() => {
    if (!dashboard || !dashboard.widgets?.some(w => w.sql_query)) return;
    if (filterDebounceRef.current) clearTimeout(filterDebounceRef.current);
    filterDebounceRef.current = setTimeout(() => refreshAll(), 300);
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
          refreshAll();
        }
      }, autoRefresh * 1000);
    }
    return () => {
      if (refreshTimerRef.current) clearInterval(refreshTimerRef.current);
    };
  }, [autoRefresh, dashboard]); // eslint-disable-line react-hooks/exhaustive-deps

  // Update dashboard on server
  const saveDashboard = useCallback(async (updates) => {
    if (!dashboard) return;
    pushUndo();
    try {
      const result = await api.updateDashboard(dashboard.id, updates);
      setDashboard(prev => ({ ...prev, ...updates, ...result }));
      setIsDirty(false);
      setLastSavedAt(new Date());
    } catch (e) {
      console.error("Failed to save dashboard:", e);
    }
  }, [dashboard, pushUndo]);

  // Add widget
  const addWidget = useCallback(async (widgetData) => {
    if (!dashboard) return null;
    pushUndo();
    try {
      const w = await api.addWidget(dashboard.id, widgetData);
      setDashboard(prev => ({
        ...prev,
        widgets: [...(prev.widgets || []), w],
      }));
      setIsDirty(true);
      return w;
    } catch (e) {
      console.error("Failed to add widget:", e);
      return null;
    }
  }, [dashboard, pushUndo]);

  // Update widget
  const updateWidget = useCallback(async (widgetId, updates) => {
    if (!dashboard) return;
    pushUndo();
    try {
      const w = await api.updateWidget(dashboard.id, widgetId, updates);
      setDashboard(prev => ({
        ...prev,
        widgets: (prev.widgets || []).map(ww => ww.id === widgetId ? { ...ww, ...w } : ww),
      }));
      setIsDirty(true);
    } catch (e) {
      console.error("Failed to update widget:", e);
    }
  }, [dashboard, pushUndo]);

  // Delete widget
  const removeWidget = useCallback(async (widgetId) => {
    if (!dashboard) return;
    pushUndo();
    try {
      await api.deleteWidget(dashboard.id, widgetId);
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
    } catch (e) {
      console.error("Failed to delete widget:", e);
    }
  }, [dashboard, pushUndo]);

  // Batch position update
  const updatePositions = useCallback(async (positions) => {
    if (!dashboard) return;
    pushUndo();
    try {
      await api.updateWidgetPositions(dashboard.id, positions);
      setDashboard(prev => ({
        ...prev,
        widgets: (prev.widgets || []).map(w => {
          const update = positions.find(p => p.id === w.id);
          return update ? { ...w, position: update.position } : w;
        }),
      }));
      setIsDirty(true);
    } catch (e) {
      console.error("Failed to update positions:", e);
    }
  }, [dashboard, pushUndo]);

  // Undo: pop from undoStack, push current to redoStack, apply popped state and persist
  const undo = useCallback(async () => {
    if (undoStack.length === 0 || !dashboard) return;
    const prev = undoStack[undoStack.length - 1];
    setUndoStack(s => s.slice(0, -1));
    setRedoStack(s => {
      const next = [...s, JSON.parse(JSON.stringify(dashboard))];
      if (next.length > MAX_UNDO) next.shift();
      return next;
    });
    setDashboard(prev);
    try {
      await api.updateDashboard(prev.id, prev);
      setIsDirty(false);
      setLastSavedAt(new Date());
    } catch (e) {
      console.error("Failed to persist undo:", e);
    }
  }, [undoStack, dashboard]);

  // Redo: pop from redoStack, push current to undoStack, apply popped state and persist
  const redo = useCallback(async () => {
    if (redoStack.length === 0 || !dashboard) return;
    const next = redoStack[redoStack.length - 1];
    setRedoStack(s => s.slice(0, -1));
    setUndoStack(s => {
      const n = [...s, JSON.parse(JSON.stringify(dashboard))];
      if (n.length > MAX_UNDO) n.shift();
      return n;
    });
    setDashboard(next);
    try {
      await api.updateDashboard(next.id, next);
      setIsDirty(false);
      setLastSavedAt(new Date());
    } catch (e) {
      console.error("Failed to persist redo:", e);
    }
  }, [redoStack, dashboard]);

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
      console.error("Failed to save view:", e);
    }
  }, [dashboard, globalFilters, parameters, savedViews]);

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
    const updated = savedViews.filter(v => v.id !== viewId);
    setSavedViews(updated);
    try {
      await api.updateDashboard(dashboard.id, { settings: { ...dashboard.settings, saved_views: updated } });
      setDashboard(prev => ({ ...prev, settings: { ...prev.settings, saved_views: updated } }));
    } catch (e) {
      console.error("Failed to delete view:", e);
    }
  }, [dashboard, savedViews]);

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
  };

  return (
    <DashboardContext.Provider value={value}>
      {children}
    </DashboardContext.Provider>
  );
}

export function useDashboard() {
  const ctx = useContext(DashboardContext);
  if (!ctx) throw new Error("useDashboard must be used within DashboardProvider");
  return ctx;
}

export default DashboardContext;
