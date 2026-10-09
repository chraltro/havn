import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import DashboardContext from "./DashboardContext";
import DashboardWidget from "./DashboardWidget";
import DashboardFilterBar from "./DashboardFilterBar";
import { useTheme } from "./ThemeProvider";
import { COLOR_THEMES } from "./themes";

/*
 * The published, read-only view of a dashboard at /p/<key>.
 *
 * No editor chrome and no SQL: the page gets a SQL-free definition from
 * /api/published/<key> and asks the server to run the dashboard's saved
 * queries with the viewer's filter values. Widgets render through the same
 * DashboardWidget the editor uses, fed by a read-only DashboardContext.
 *
 * ?embed=1 trims the header for iframes; ?theme=<color theme id> picks a
 * theme (e.g. havn-light to match a light host page).
 */

const ROW_HEIGHT = 64;
const GAP = 12;
const GRID_COLS = 24;
const NARROW_PX = 760;
const AUTO_REFRESH_MS = 5 * 60 * 1000;

export function publishedKeyFromPath(pathname) {
  const m = /^\/p\/([^/?#]+)\/?$/.exec(pathname || "");
  return m ? decodeURIComponent(m[1]) : null;
}

function fmtWhen(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (isNaN(d)) return null;
  return d.toLocaleString(undefined, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function relativeAge(iso, now = Date.now()) {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (isNaN(t)) return null;
  const mins = Math.max(0, Math.round((now - t) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

/** Widgets stacked for a phone: reading order (top to bottom, left to right). */
export function stackOrder(widgets) {
  return [...widgets].sort((a, b) => {
    const pa = a.position || {}, pb = b.position || {};
    return (pa.y || 0) - (pb.y || 0) || (pa.x || 0) - (pb.x || 0) || (a.sort_order || 0) - (b.sort_order || 0);
  });
}

/** Phone layout groups: runs of KPI widgets become one two-up row each. */
export function stackGroups(widgets) {
  const out = [];
  for (const w of stackOrder(widgets)) {
    const last = out[out.length - 1];
    if (w.widget_type === "kpi" && last?.kpis) last.kpis.push(w);
    else if (w.widget_type === "kpi") out.push({ key: `k-${w.id}`, kpis: [w] });
    else out.push({ key: w.id, widget: w });
  }
  return out;
}

function stackedHeight(w) {
  const h = (w.position?.h || 4) * ROW_HEIGHT;
  if (w.widget_type === "kpi") return Math.max(130, Math.min(h, 180));
  if (w.widget_type === "divider") return 24;
  if (w.widget_type === "text") return Math.max(100, Math.min(h, 320));
  return Math.max(240, Math.min(h, 420));
}

export default function PublishedDashboard({ shareKey }) {
  const params = useMemo(() => new URLSearchParams(window.location.search), []);
  const embed = params.get("embed") === "1";
  const { setColorThemeId } = useTheme();

  useEffect(() => {
    const t = params.get("theme");
    if (t && COLOR_THEMES[t]) setColorThemeId(t);
  }, [params, setColorThemeId]);

  const [state, setState] = useState({ status: "loading" });
  const [widgetData, setWidgetData] = useState({});
  const [globalFilters, setGlobalFilters] = useState({});
  const [parameters, setParameters] = useState({});
  const [freshness, setFreshness] = useState(null);
  const [loadedAt, setLoadedAt] = useState(null);
  const [activePage, setActivePage] = useState(null);
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    try {
      const data = await api.getPublished(shareKey);
      const defaults = {};
      for (const p of data.dashboard.settings?.parameters || []) defaults[p.name] = p.default ?? "";
      setParameters(defaults);
      setFreshness(data.freshness);
      document.title = `${data.dashboard.name} · havn`;
      setState({ status: "ready", data });
    } catch (e) {
      const msg = e?.message || "This link could not be opened.";
      if (msg === "Authentication required") setState({ status: "signin" });
      else setState({ status: "error", message: msg });
    }
  }, [shareKey]);

  useEffect(() => { load(); }, [load]);

  const dashboard = useMemo(() => {
    if (state.status !== "ready") return null;
    const d = state.data.dashboard;
    return {
      ...d,
      // DashboardWidget only checks that a query exists; the SQL stays on the server.
      widgets: d.widgets.map(w => ({ ...w, sql_query: w.has_query ? "saved" : null })),
    };
  }, [state]);

  const runQueries = useCallback(async (widgetIds) => {
    if (!dashboard) return;
    const seq = ++seqRef.current;
    const targets = (widgetIds || dashboard.widgets.filter(w => w.has_query).map(w => w.id));
    setWidgetData(prev => {
      const next = { ...prev };
      for (const id of targets) next[id] = { ...prev[id], loading: true, error: null };
      return next;
    });
    const started = Date.now();
    try {
      const res = await api.queryPublished(shareKey, globalFilters, parameters, widgetIds);
      if (seq !== seqRef.current && !widgetIds) return;
      const fetchedAt = new Date().toISOString();
      setWidgetData(prev => {
        const next = { ...prev };
        for (const [id, r] of Object.entries(res.results || {})) {
          next[id] = { ...r, loading: false, error: r.error || null, _fetchedAt: fetchedAt, _queryDuration: Date.now() - started };
        }
        return next;
      });
      if (res.freshness) setFreshness(res.freshness);
      setLoadedAt(new Date());
    } catch (e) {
      if (e?.message === "Authentication required") { setState({ status: "signin" }); return; }
      setWidgetData(prev => {
        const next = { ...prev };
        for (const id of targets) next[id] = { columns: [], rows: [], row_count: 0, loading: false, error: e?.message || "Could not load" };
        return next;
      });
    }
  }, [dashboard, shareKey, globalFilters, parameters]);

  const runRef = useRef(runQueries);
  runRef.current = runQueries;

  // Initial load and re-run when filters change (debounced like the editor).
  useEffect(() => {
    if (!dashboard) return undefined;
    const t = setTimeout(() => runRef.current(), 250);
    return () => clearTimeout(t);
  }, [dashboard, globalFilters, parameters]);

  // Keep a wall-mounted or long-open view current.
  useEffect(() => {
    if (!dashboard) return undefined;
    const id = setInterval(() => { if (!document.hidden) runRef.current(); }, AUTO_REFRESH_MS);
    return () => clearInterval(id);
  }, [dashboard]);

  const setFilter = useCallback((col, value) => {
    setGlobalFilters(prev => {
      const next = { ...prev };
      if (value === null || value === undefined || value === "" || (Array.isArray(value) && value.length === 0)) delete next[col];
      else next[col] = value;
      return next;
    });
  }, []);

  const optionsCache = useRef(new Map());
  const loadFilterOptions = useCallback(async (filter) => {
    if (filter.options) return filter.options;
    if (optionsCache.current.has(filter.id)) return optionsCache.current.get(filter.id);
    const res = await api.publishedFilterOptions(shareKey, filter.id);
    optionsCache.current.set(filter.id, res.options || []);
    return res.options || [];
  }, [shareKey]);

  const ctx = useMemo(() => ({
    dashboard,
    readOnly: true,
    editMode: false,
    setEditMode: () => {},
    globalFilters,
    crossFilter: null,
    widgetData,
    parameters,
    setFilter,
    setParameter: (name, value) => setParameters(prev => ({ ...prev, [name]: value })),
    setCrossFilter: () => {},
    clearCrossFilter: () => {},
    refreshWidget: (id) => runRef.current([id]),
    refreshAll: () => runRef.current(),
    updateWidget: () => {},
    savedViews: [],
    saveView: () => {},
    loadView: () => {},
    deleteView: () => {},
    showToast: () => {},
    loadFilterOptions,
  }), [dashboard, globalFilters, widgetData, parameters, setFilter, loadFilterOptions]);

  // Width-driven layout: the page may live in a narrow iframe on a wide screen.
  const bodyRef = useRef(null);
  const [width, setWidth] = useState(typeof window !== "undefined" ? window.innerWidth : 1200);
  useEffect(() => {
    const el = bodyRef.current;
    if (!el || typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(entries => setWidth(entries[0].contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, [state.status]);
  const narrow = width < NARROW_PX;

  if (state.status === "loading") return <Centered><span style={s.muted}>Loading dashboard…</span></Centered>;
  if (state.status === "signin") return <SignIn onSignedIn={load} />;
  if (state.status === "error") {
    return (
      <Centered>
        <div style={s.messageCard}>
          <div style={s.messageTitle}>This dashboard isn't available</div>
          <div style={s.muted}>{state.message.replace(/^Error \(\d+\): /, "")}</div>
          <div style={{ ...s.muted, marginTop: 12, fontSize: 12 }}>Ask the person who shared it for a new link.</div>
        </div>
      </Centered>
    );
  }

  const pages = dashboard.settings?.pages || [];
  const pageId = activePage || pages[0]?.id || null;
  const visible = dashboard.widgets.filter(w => {
    if (pages.length < 2) return true;
    const wp = w.config?.page_id;
    return wp ? wp === pageId : pageId === pages[0]?.id;
  });
  const hasControls = (dashboard.filters || []).length > 0 || (dashboard.settings?.parameters || []).length > 0;
  const asOf = freshness?.as_of;

  return (
    <DashboardContext.Provider value={ctx}>
      <div style={s.page}>
        <header style={{ ...s.header, ...(embed ? s.headerEmbed : {}), ...(narrow ? s.headerNarrow : {}) }}>
          <div style={{ minWidth: 0 }}>
            <h1 style={{ ...s.title, ...(embed || narrow ? s.titleSmall : {}) }}>{dashboard.name}</h1>
            {!embed && dashboard.description && <p style={s.description}>{dashboard.description}</p>}
          </div>
          <div style={{ ...s.meta, ...(narrow ? s.metaNarrow : {}) }}>
            <span title={asOf ? `Oldest build among the models this dashboard reads: ${fmtWhen(asOf)}` : "No build record for the tables this dashboard reads"}>
              <span style={{ ...s.dot, background: asOf ? "var(--havn-green)" : "var(--havn-text-dim, #8A94A7)" }} />
              {asOf ? <>Data as of <strong style={s.strong}>{fmtWhen(asOf)}</strong> · {relativeAge(asOf)}</> : <>Data freshness unknown</>}
            </span>
            <button style={s.refresh} onClick={() => runRef.current()} title="Reload the data">
              ↻ <span>{loadedAt ? `Loaded ${loadedAt.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}` : "Refresh"}</span>
            </button>
          </div>
        </header>

        {pages.length > 1 && (
          <nav style={s.pageTabs} aria-label="Dashboard pages">
            {pages.map(p => (
              <button key={p.id} style={{ ...s.pageTab, ...(p.id === pageId ? s.pageTabActive : {}) }} onClick={() => setActivePage(p.id)}>
                {p.name}
              </button>
            ))}
          </nav>
        )}

        {hasControls && (
          <div style={{ ...s.filters, ...(narrow ? s.filtersNarrow : {}) }}>
            <DashboardFilterBar />
          </div>
        )}

        <main ref={bodyRef} style={{ ...s.body, ...(narrow ? s.bodyNarrow : {}) }}>
          {visible.length === 0 && <Centered><span style={s.muted}>This dashboard has no widgets yet.</span></Centered>}
          {narrow ? (
            <div style={s.stack}>
              {stackGroups(visible).map(group => group.kpis ? (
                // Headline numbers pair up on a phone instead of each taking a screen's worth of height.
                <div key={group.key} style={s.kpiPair}>
                  {group.kpis.map(w => (
                    <div key={w.id} style={{ height: 124, minWidth: 0 }}>
                      <DashboardWidget widget={w} style={{ height: "100%" }} />
                    </div>
                  ))}
                </div>
              ) : (
                <div key={group.key} style={{ height: stackedHeight(group.widget), minWidth: 0 }}>
                  <DashboardWidget widget={group.widget} style={{ height: "100%" }} />
                </div>
              ))}
            </div>
          ) : (
            <div style={s.grid}>
              {visible.map(w => {
                const pos = w.position || { x: 1, y: 1, w: 6, h: 4 };
                return (
                  <div key={w.id} style={{ gridColumn: `${pos.x || 1} / span ${Math.min(pos.w || 6, GRID_COLS)}`, gridRow: `${pos.y || 1} / span ${pos.h || 4}`, minWidth: 0, minHeight: 0 }}>
                    <DashboardWidget widget={w} style={{ height: "100%" }} />
                  </div>
                );
              })}
            </div>
          )}
        </main>

        {!embed && (
          <footer style={s.footer}>
            <span>View only{state.data.share.mode === "signed_in" && state.data.viewer?.username ? ` · signed in as ${state.data.viewer.username}` : ""}</span>
            <span>Published with havn</span>
          </footer>
        )}
      </div>
    </DashboardContext.Provider>
  );
}

function Centered({ children }) {
  return <div style={s.centered}>{children}</div>;
}

function SignIn({ onSignedIn }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await api.login(username, password);
      api.setToken(res.token);
      await onSignedIn();
    } catch (err) {
      setError(err?.message?.includes("401") || err?.message === "Authentication required" ? "Wrong username or password." : (err?.message || "Sign-in failed."));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Centered>
      <form style={s.messageCard} onSubmit={submit}>
        <div style={s.messageTitle}>Sign in to view this dashboard</div>
        <div style={{ ...s.muted, marginBottom: 14 }}>It is shared with people who have a havn account.</div>
        <label style={s.label} htmlFor="pub-user">Username</label>
        <input id="pub-user" style={s.input} value={username} onChange={e => setUsername(e.target.value)} autoComplete="username" autoFocus />
        <label style={s.label} htmlFor="pub-pass">Password</label>
        <input id="pub-pass" type="password" style={s.input} value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" />
        {error && <div style={s.error} role="alert">{error}</div>}
        <button type="submit" style={s.primary} disabled={busy || !username || !password}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </Centered>
  );
}

const s = {
  page: { minHeight: "100vh", display: "flex", flexDirection: "column", background: "var(--havn-bg)", color: "var(--havn-text)", fontFamily: "var(--havn-font)" },
  header: { display: "flex", alignItems: "flex-end", justifyContent: "space-between", gap: 24, padding: "28px 32px 18px", borderBottom: "1px solid var(--havn-border)" },
  headerEmbed: { padding: "12px 16px 10px", alignItems: "center" },
  headerNarrow: { flexDirection: "column", alignItems: "stretch", gap: 10, padding: "18px 16px 14px" },
  title: { margin: 0, fontSize: 26, fontWeight: 600, letterSpacing: "-0.01em", lineHeight: 1.15, overflowWrap: "anywhere" },
  titleSmall: { fontSize: 19 },
  description: { margin: "6px 0 0", fontSize: 14, color: "var(--havn-text-secondary)", maxWidth: 720, lineHeight: 1.5 },
  meta: { display: "flex", alignItems: "center", gap: 14, fontSize: 12.5, color: "var(--havn-text-secondary)", flexShrink: 0, flexWrap: "wrap", justifyContent: "flex-end" },
  metaNarrow: { justifyContent: "space-between" },
  strong: { color: "var(--havn-text)", fontWeight: 500 },
  dot: { display: "inline-block", width: 7, height: 7, borderRadius: "50%", marginRight: 7, verticalAlign: "1px" },
  refresh: { background: "none", border: "1px solid var(--havn-border)", color: "var(--havn-text-secondary)", borderRadius: 6, padding: "5px 10px", fontSize: 12, cursor: "pointer", fontFamily: "inherit", display: "inline-flex", gap: 6, alignItems: "center" },
  pageTabs: { display: "flex", gap: 4, padding: "8px 28px 0", borderBottom: "1px solid var(--havn-border)", overflowX: "auto" },
  pageTab: { background: "none", border: "none", borderBottom: "2px solid transparent", color: "var(--havn-text-secondary)", padding: "8px 12px", fontSize: 13, cursor: "pointer", fontFamily: "inherit", whiteSpace: "nowrap" },
  pageTabActive: { color: "var(--havn-text)", borderBottomColor: "var(--havn-accent)", fontWeight: 500 },
  filters: { padding: "4px 20px 0" },
  filtersNarrow: { padding: "4px 4px 0" },
  body: { flex: 1, padding: "16px 32px 28px", minWidth: 0 },
  bodyNarrow: { padding: "12px 16px 20px" },
  grid: { display: "grid", gridTemplateColumns: `repeat(${GRID_COLS}, minmax(0, 1fr))`, gridAutoRows: `${ROW_HEIGHT}px`, gap: GAP },
  stack: { display: "flex", flexDirection: "column", gap: GAP },
  kpiPair: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))", gap: GAP },
  footer: { display: "flex", justifyContent: "space-between", gap: 12, padding: "14px 32px", borderTop: "1px solid var(--havn-border)", fontSize: 12, color: "var(--havn-text-dim, var(--havn-text-secondary))", flexWrap: "wrap" },
  centered: { minHeight: "60vh", display: "flex", alignItems: "center", justifyContent: "center", padding: 16, background: "var(--havn-bg)", color: "var(--havn-text)", fontFamily: "var(--havn-font)" },
  muted: { color: "var(--havn-text-secondary)", fontSize: 14, lineHeight: 1.5 },
  messageCard: { width: "100%", maxWidth: 380, background: "var(--havn-bg-secondary, var(--havn-bg))", border: "1px solid var(--havn-border)", borderRadius: 10, padding: 24, display: "flex", flexDirection: "column" },
  messageTitle: { fontSize: 17, fontWeight: 600, marginBottom: 6 },
  label: { fontSize: 12, color: "var(--havn-text-secondary)", margin: "10px 0 4px" },
  input: { background: "var(--havn-bg)", border: "1px solid var(--havn-border)", borderRadius: 6, color: "var(--havn-text)", padding: "8px 10px", fontSize: 14, fontFamily: "inherit" },
  error: { color: "var(--havn-red)", fontSize: 13, marginTop: 10 },
  primary: { marginTop: 16, background: "var(--havn-accent)", color: "var(--havn-bg)", border: "none", borderRadius: 6, padding: "9px 12px", fontSize: 14, fontWeight: 600, cursor: "pointer", fontFamily: "inherit" },
};
