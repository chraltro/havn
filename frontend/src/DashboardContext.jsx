import React, { createContext, useContext, useState, useCallback, useRef, useEffect } from "react";
import { api } from "./api";

const DashboardContext = createContext(null);

export function DashboardProvider({ children }) {
  const [dashboard, setDashboard] = useState(null);
  const [editMode, setEditMode] = useState(false);
  const [globalFilters, setGlobalFilters] = useState({});
  const [crossFilter, setCrossFilter] = useState(null);
  const [widgetData, setWidgetData] = useState({});
  const [parameters, setParameters] = useState({});
  const [autoRefresh, setAutoRefresh] = useState(0);
  const refreshTimerRef = useRef(null);

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
    try {
      const result = await api.queryWidget(dashboard.id, widgetId, _buildFilters(), parameters);
      setWidgetData(prev => ({
        ...prev,
        [widgetId]: { ...result, loading: false, error: result.error || null, _fetchedAt: new Date().toISOString() },
      }));
    } catch (e) {
      setWidgetData(prev => ({
        ...prev,
        [widgetId]: { columns: [], rows: [], row_count: 0, loading: false, error: e.message },
      }));
    }
  }, [dashboard, _buildFilters, parameters]);

  // Batch query all widgets
  const refreshAll = useCallback(async () => {
    if (!dashboard) return;
    // Mark all as loading (use functional update to avoid widgetData dependency)
    setWidgetData(prev => {
      const loadingState = {};
      for (const w of dashboard.widgets || []) {
        if (w.sql_query) {
          loadingState[w.id] = { ...prev[w.id], loading: true, error: null };
        }
      }
      return { ...prev, ...loadingState };
    });

    try {
      const batch = await api.queryDashboardBatch(dashboard.id, _buildFilters(), parameters);
      const results = batch.results || {};
      setWidgetData(prev => {
        const next = { ...prev };
        for (const [wid, result] of Object.entries(results)) {
          next[wid] = { ...result, loading: false, _fetchedAt: new Date().toISOString() };
        }
        return next;
      });
    } catch (e) {
      console.error("Batch query failed:", e);
    }
  }, [dashboard, _buildFilters, parameters]);

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
    try {
      const result = await api.updateDashboard(dashboard.id, updates);
      setDashboard(prev => ({ ...prev, ...updates, ...result }));
    } catch (e) {
      console.error("Failed to save dashboard:", e);
    }
  }, [dashboard]);

  // Add widget
  const addWidget = useCallback(async (widgetData) => {
    if (!dashboard) return null;
    try {
      const w = await api.addWidget(dashboard.id, widgetData);
      setDashboard(prev => ({
        ...prev,
        widgets: [...(prev.widgets || []), w],
      }));
      return w;
    } catch (e) {
      console.error("Failed to add widget:", e);
      return null;
    }
  }, [dashboard]);

  // Update widget
  const updateWidget = useCallback(async (widgetId, updates) => {
    if (!dashboard) return;
    try {
      const w = await api.updateWidget(dashboard.id, widgetId, updates);
      setDashboard(prev => ({
        ...prev,
        widgets: (prev.widgets || []).map(ww => ww.id === widgetId ? { ...ww, ...w } : ww),
      }));
    } catch (e) {
      console.error("Failed to update widget:", e);
    }
  }, [dashboard]);

  // Delete widget
  const removeWidget = useCallback(async (widgetId) => {
    if (!dashboard) return;
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
    } catch (e) {
      console.error("Failed to delete widget:", e);
    }
  }, [dashboard]);

  // Batch position update
  const updatePositions = useCallback(async (positions) => {
    if (!dashboard) return;
    try {
      await api.updateWidgetPositions(dashboard.id, positions);
      setDashboard(prev => ({
        ...prev,
        widgets: (prev.widgets || []).map(w => {
          const update = positions.find(p => p.id === w.id);
          return update ? { ...w, position: update.position } : w;
        }),
      }));
    } catch (e) {
      console.error("Failed to update positions:", e);
    }
  }, [dashboard]);

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
