import React, { useState, useEffect, useMemo, useRef } from "react";
import { format as formatSQL } from "sql-formatter";
import { api } from "./api";
import SortableTable from "./SortableTable";
import ChartPanel from "./ChartPanel";
import ExplainPanel from "./ExplainPanel";
import { useHintTriggerFn } from "./HintSystem";
import ResizeHandle from "./ResizeHandle";
import useResizable from "./useResizable";
import { isSystemSchema, schemaCompare } from "./schemaOrder";
import { MOD_KEY } from "./platform";
import { buildCompletions, applyCompletionText, caretCoordinates, completionFits } from "./queryAutocomplete";
import { clickable, useDismiss } from "./a11y";

const MAX_HISTORY = 50;
const DEFAULT_LIMIT = 1000;
const EDITOR_MIN_HEIGHT = 80;
const RESULTS_MIN_HEIGHT = 160; // room kept below the editor for toolbar + results
const AC_WIDTH = 360;
const AC_MAX_HEIGHT = 220;
// DuckDB dialect: the generic "sql" dialect tokenises DuckDB operators
// character by character, so Format turned `->>` into `- > >`, a lambda's
// `->` into `- >`, `//` into `/ /` and `**` into `* *`, all of which then fail
// to parse. It also rejected `$name` parameters outright.
const FMT_OPTS = {
  language: "duckdb",
  keywordCase: "upper",
  indentStyle: "standard",
  paramTypes: { positional: true, numbered: ["$"], named: ["$"] },
};
function fmt(sql) { try { return formatSQL(sql, FMT_OPTS); } catch { return sql; } }

// The panel unmounts on every tab switch (App renders it conditionally), so
// anything kept only in component state was lost: switch away mid-run and the
// result vanished even though the query finished. This module-level session
// outlives the component, so coming back restores the last result, the error,
// the view and the LIMIT toggle, and reattaches to a run still in flight.
const session = {
  results: null,
  error: null,
  viewMode: "table",
  limitEnabled: true,
  runSeq: 0,          // bumps per result, so the table resets its sort/filters/column state
  running: null,      // { promise, controller } while a run is in flight
};

// LIMIT inside a comment or string literal is not a LIMIT clause: a query like
// `SELECT * FROM big -- TODO limit` must still get the automatic row limit.
function hasLimitClause(sql) {
  return /\bLIMIT\b/i.test(stripSqlLiterals(sql));
}


function getHistory() {
  try {
    return JSON.parse(localStorage.getItem("havn_query_history") || "[]");
  } catch { return []; }
}

function saveHistory(h) {
  localStorage.setItem("havn_query_history", JSON.stringify(h.slice(0, MAX_HISTORY)));
}

function getSavedParams() {
  try {
    return JSON.parse(localStorage.getItem("havn_query_params") || "{}");
  } catch { return {}; }
}

// Blank out strings/comments so param detection can't be fooled by $tokens inside them
function stripSqlLiterals(sql) {
  return sql
    .replace(/\$\$[\s\S]*?\$\$/g, "''")
    .replace(/'(?:[^']|'')*'/g, "''")
    .replace(/"[^"]*"/g, '""')
    .replace(/--[^\n]*/g, "")
    .replace(/\/\*[\s\S]*?\*\//g, "");
}

// Find named $param placeholders (DuckDB prepared-statement syntax).
function detectParams(sql) {
  const names = [];
  const re = /\$([A-Za-z_]\w*)/g;
  let m;
  const stripped = stripSqlLiterals(sql);
  while ((m = re.exec(stripped)) !== null) {
    if (!names.includes(m[1])) names.push(m[1]);
  }
  return names;
}

// Numbers and true/false are sent typed; everything else stays a string (cast in SQL: $day::DATE)
function coerceParamValue(raw) {
  const v = raw.trim();
  if (/^-?\d+$/.test(v) && Number.isSafeInteger(Number(v))) return Number(v);
  if (/^-?(\d+\.\d*|\.\d+)$/.test(v)) return Number(v);
  if (/^(true|false)$/i.test(v)) return v.toLowerCase() === "true";
  return raw;
}

function formatElapsed(ms) {
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`;
  const m = Math.floor(ms / 60000);
  return `${m}m ${Math.round((ms % 60000) / 1000)}s`;
}

function timeAgo(ts) {
  if (!ts) return "";
  const s = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/**
 * Schema sidebar — lists schemas/tables. Click table name to insert
 * `schema.table` at cursor. Expand to see columns.
 */
function SchemaSidebar({ tables, onInsert, maskingPolicies, onOpenModel }) {
  const [expanded, setExpanded] = useState({});
  const [search, setSearch] = useState("");
  // System schemas (_havn, information_schema, ...) start collapsed so the
  // user's own data is what the list shows; a search still reaches them.
  const [openSystem, setOpenSystem] = useState({});

  const schemas = {};
  for (const t of tables) {
    if (!schemas[t.schema]) schemas[t.schema] = [];
    schemas[t.schema].push(t);
  }
  const schemaNames = Object.keys(schemas).sort(schemaCompare);

  // Filter tables by search query (matches table name or column names)
  const query = search.trim().toLowerCase();
  const filteredSchemas = {};
  if (query) {
    for (const schema of schemaNames) {
      const matching = schemas[schema].filter((t) => {
        if (t.name.toLowerCase().includes(query)) return true;
        // Check column names if table is expanded
        const key = `${t.schema}.${t.name}`;
        const cols = expanded[key];
        if (cols && cols.length > 0) {
          return cols.some((c) => c.name.toLowerCase().includes(query));
        }
        return false;
      });
      if (matching.length > 0) filteredSchemas[schema] = matching;
    }
  }
  const displaySchemas = query ? filteredSchemas : schemas;
  const displaySchemaNames = Object.keys(displaySchemas).sort(schemaCompare);

  async function toggleExpand(key) {
    if (expanded[key]) {
      setExpanded((prev) => ({ ...prev, [key]: null }));
      return;
    }
    const [schema, table] = key.split(".");
    try {
      const info = await api.describeTable(schema, table);
      setExpanded((prev) => ({ ...prev, [key]: info.columns || [] }));
    } catch {
      setExpanded((prev) => ({ ...prev, [key]: [] }));
    }
  }

  if (tables.length === 0) {
    return <div style={sbSt.empty}>No tables in warehouse</div>;
  }

  return (
    <div style={sbSt.container}>
      <div style={sbSt.header}>Tables</div>
      <div style={sbSt.searchRow}>
        <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="var(--havn-text-dim)" strokeWidth="1.5" style={{ flexShrink: 0 }}>
          <circle cx="6.5" cy="6.5" r="5"/>
          <path d="M10.5 10.5L14.5 14.5"/>
        </svg>
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Filter tables..."
          aria-label="Filter tables"
          style={sbSt.searchInput}
        />
        {search && (
          <button onClick={() => setSearch("")} style={sbSt.searchClear} aria-label="Clear table filter" title="Clear filter">&times;</button>
        )}
      </div>
      <div style={sbSt.list}>
        {displaySchemaNames.length === 0 && query && (
          <div style={sbSt.empty}>No tables matching '{search}'</div>
        )}
        {displaySchemaNames.map((schema) => {
          const sys = isSystemSchema(schema);
          const open = !sys || query || openSystem[schema];
          return (
          <div key={schema}>
            {sys ? (
              <button
                type="button"
                onClick={() => setOpenSystem((prev) => ({ ...prev, [schema]: !prev[schema] }))}
                style={{ ...sbSt.schemaRow, ...sbSt.schemaRowButton }}
                aria-expanded={Boolean(open)}
                title="System schema (introspection)"
              >
                <span style={{ ...sbSt.schemaName, ...sbSt.schemaNameSystem }}>{open ? "▾" : "▸"} {schema}</span>
                <span style={sbSt.schemaCount}>{displaySchemas[schema].length}</span>
              </button>
            ) : (
              <div style={sbSt.schemaRow}>
                <span style={sbSt.schemaName}>{schema}</span>
                <span style={sbSt.schemaCount}>{displaySchemas[schema].length}</span>
              </div>
            )}
            {open && displaySchemas[schema].map((t) => {
              const key = `${t.schema}.${t.name}`;
              const cols = expanded[key];
              return (
                <div key={key}>
                  <div style={sbSt.tableRow}>
                    <button
                      onClick={() => toggleExpand(key)}
                      style={sbSt.expandBtn}
                      aria-expanded={Boolean(cols)}
                      aria-label={`${cols ? "Hide" : "Show"} columns of ${key}`}
                    >
                      {cols ? "\u25BE" : "\u25B8"}
                    </button>
                    <button onClick={(e) => { if ((e.ctrlKey || e.metaKey) && onOpenModel) { e.preventDefault(); onOpenModel(key); } else { onInsert(key); } }} style={sbSt.tableName} title={`Insert ${key} (${MOD_KEY}+click to open model)`}>
                      {t.name}
                    </button>
                  </div>
                  {cols && cols.length > 0 && (
                    <div style={sbSt.colList}>
                      {cols.map((c) => {
                        const maskMethod = maskingPolicies && maskingPolicies[`${key}.${c.name}`];
                        return (
                          <div
                            key={c.name}
                            style={sbSt.colItem}
                            onClick={() => onInsert(c.name)}
                            {...clickable(() => onInsert(c.name), `Insert column ${c.name}`)}
                            title={maskMethod ? `Insert ${c.name} (masked: ${maskMethod})` : `Insert ${c.name}`}
                          >
                            {maskMethod && <span style={sbSt.maskIcon} title={`Masked: ${maskMethod}`}>&#x1F6E1;</span>}
                            <span style={sbSt.colName}>{c.name}</span>
                            <span style={sbSt.colType}>{c.type}</span>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          );
        })}
      </div>
    </div>
  );
}

const sbSt = {
  // Only the list scrolls, so the header and the filter box stay in reach
  // however far down a long table list the user is.
  container: { width: "100%", height: "100%", display: "flex", flexDirection: "column", overflow: "hidden", background: "var(--havn-bg-tertiary)", fontSize: "12px" },
  header: { padding: "8px 10px 4px", fontWeight: 600, fontSize: "11px", color: "var(--havn-text-secondary)", textTransform: "uppercase", letterSpacing: "0.3px", flexShrink: 0 },
  searchRow: { display: "flex", alignItems: "center", gap: "6px", padding: "2px 8px 6px", borderBottom: "1px solid var(--havn-border)", flexShrink: 0 },
  searchInput: { flex: 1, padding: "3px 6px", background: "var(--havn-bg)", border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius)", color: "var(--havn-text)", fontSize: "11px", fontFamily: "var(--havn-font-mono)", outline: "none", minWidth: 0 },
  searchClear: { background: "none", border: "none", color: "var(--havn-text-dim)", cursor: "pointer", fontSize: "14px", padding: "0 2px", lineHeight: 1, flexShrink: 0 },
  list: { padding: "4px 0", flex: 1, minHeight: 0, overflow: "auto" },
  empty: { padding: "16px 10px", color: "var(--havn-text-dim)", fontSize: "12px", textAlign: "center" },
  schemaRow: { display: "flex", alignItems: "center", justifyContent: "space-between", padding: "5px 10px 2px", marginTop: "4px" },
  schemaName: { fontWeight: 600, color: "var(--havn-accent)", fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.3px" },
  schemaRowButton: { width: "100%", background: "none", border: "none", cursor: "pointer", font: "inherit", textAlign: "left" },
  schemaNameSystem: { color: "var(--havn-text-dim)", fontStyle: "italic" },
  schemaCount: { fontSize: "10px", color: "var(--havn-text-dim)" },
  tableRow: { display: "flex", alignItems: "center", padding: "2px 6px 2px 8px" },
  expandBtn: { background: "none", border: "none", color: "var(--havn-text-dim)", cursor: "pointer", fontSize: "10px", padding: "2px 4px", width: "18px" },
  tableName: { background: "none", border: "none", color: "var(--havn-text)", cursor: "pointer", fontSize: "12px", fontFamily: "var(--havn-font-mono)", padding: "2px 4px", textAlign: "left", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  colList: { paddingLeft: "30px", paddingBottom: "2px" },
  colItem: { display: "flex", justifyContent: "space-between", gap: "6px", padding: "1px 8px", cursor: "pointer", borderRadius: "var(--havn-radius)" },
  colName: { fontFamily: "var(--havn-font-mono)", color: "var(--havn-text-secondary)", fontSize: "11px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  colType: { color: "var(--havn-text-dim)", fontSize: "10px", flexShrink: 0 },
  maskIcon: { fontSize: "9px", opacity: 0.7, flexShrink: 0, marginRight: "2px" },
};

export default function QueryPanel({ addOutput, onOpenModel }) {
  // Use ref-based value to preserve browser undo stack (controlled textarea breaks Ctrl+Z)
  const sqlRef = useRef("");
  const [sql, _setSql] = useState("");
  const setSql = (val) => {
    const v = typeof val === "function" ? val(sqlRef.current) : val;
    sqlRef.current = v;
    _setSql(v);
    // Suggestions describe the old text; replacing it (format, history, a
    // completion's fallback path) would apply them at the wrong offset.
    acRequestRef.current++;
    setAcItems([]);
    // Sync textarea value for programmatic changes (format, history, autocomplete)
    if (textareaRef.current && textareaRef.current.value !== v) {
      textareaRef.current.value = v;
    }
  };
  const [limitEnabled, setLimitEnabled] = useState(session.limitEnabled);
  const queryAbortRef = useRef(session.running?.controller ?? null);
  const [results, setResults] = useState(session.results);
  const [runSeq, setRunSeq] = useState(session.runSeq);
  const [queryRunning, setQueryRunning] = useState(!!session.running);
  // Explain and Export have their own busy flags: sharing queryRunning turned
  // the Run button into a Cancel that could not cancel an Explain.
  const [explaining, setExplaining] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [formatting, setFormatting] = useState(false);
  const [error, setError] = useState(session.error);
  const [history, setHistory] = useState(getHistory);
  const [tables, setTables] = useState([]);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [viewMode, setViewMode] = useState(session.viewMode);
  const [explainResult, setExplainResult] = useState(null);
  const textareaRef = useRef(null);
  const historyRef = useRef(null);
  const [maskingPolicies, setMaskingPolicies] = useState({});
  const [paramValues, setParamValues] = useState(getSavedParams);
  const setHintTrigger = useHintTriggerFn();
  const [sidebarWidth, onSidebarResize, onSidebarResizeStart] = useResizable("havn_query_sidebar_width", 200, 120, 400);
  const [editorHeight, onEditorResize, onEditorResizeStart, resetEditorHeight] = useResizable("havn_query_editor_height", 300, EDITOR_MIN_HEIGHT, 600);
  // The stored editor height is capped by the space actually available, so the
  // results (and the resize handle under the editor) can never be pushed off
  // a short window. A 600px editor in a 500px panel used to hide both, with no
  // way to drag it back.
  const queryAreaRef = useRef(null);
  const [queryAreaHeight, setQueryAreaHeight] = useState(0);
  useEffect(() => {
    const el = queryAreaRef.current;
    if (!el || typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(([entry]) => setQueryAreaHeight(entry.contentRect.height));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const visibleEditorHeight = queryAreaHeight
    ? Math.min(editorHeight, Math.max(EDITOR_MIN_HEIGHT, queryAreaHeight - RESULTS_MIN_HEIGHT))
    : editorHeight;

  // Autocomplete state
  const [acItems, setAcItems] = useState([]);
  const [acIndex, setAcIndex] = useState(0);
  const [acPos, setAcPos] = useState({ left: 0, top: 0 });
  const acListRef = useRef(null);
  const acSuppressRef = useRef(false); // true while a completion is being inserted
  const lastInputAtRef = useRef(0);
  const acNavigatedRef = useRef(false); // the user moved through the list with the arrows
  const colCacheRef = useRef({}); // schema.table -> columns[]
  const acRequestRef = useRef(0); // monotonic id to drop stale autocomplete responses

  useEffect(() => {
    api.listTables().then(setTables).catch((e) => console.warn("Failed to load tables:", e.message));
    api.listMaskingPolicies().then(policies => {
      const map = {};
      for (const p of policies) map[`${p.schema_name}.${p.table_name}.${p.column_name}`] = p.method;
      setMaskingPolicies(map);
    }).catch(() => {});
    setHintTrigger("queryPanelOpened", true);
  }, []);

  // Restore the last editor draft (tab switches/reloads keep the query); prefill takes priority
  useEffect(() => {
    if (window.__havn_prefill_query) return;
    try {
      const draft = localStorage.getItem("havn_query_draft");
      if (draft && !sqlRef.current) setSql(draft);
    } catch { /* localStorage unavailable */ }
  }, []);

  // Persist the draft (debounced) as the user types
  useEffect(() => {
    const t = setTimeout(() => {
      try { localStorage.setItem("havn_query_draft", sql); } catch { /* ignore */ }
    }, 400);
    return () => clearTimeout(t);
  }, [sql]);
  // Unmounting cancels the debounce above, which dropped the last 400ms of
  // typing whenever the user switched tabs straight away. Save on the way out.
  useEffect(() => () => {
    try { localStorage.setItem("havn_query_draft", sqlRef.current); } catch { /* ignore */ }
  }, []);

  // Mirror the UI state that should survive a remount. Results and errors are
  // written to the session where they are produced instead: a run can finish
  // while this instance is gone, and an effect here could overwrite its
  // result with this instance's stale copy.
  useEffect(() => {
    session.viewMode = viewMode;
    session.limitEnabled = limitEnabled;
  }, [viewMode, limitEnabled]);

  // Back on the panel while a run started earlier is still going: show it as
  // running, and pick up its result (and the history it added) when it lands.
  useEffect(() => {
    const running = session.running;
    if (!running) return undefined;
    let alive = true;
    running.promise.finally(() => {
      if (!alive) return;
      setResults(session.results);
      setError(session.error);
      setRunSeq(session.runSeq);
      setHistory(getHistory());
      queryAbortRef.current = null;
      setQueryRunning(false);
    });
    return () => { alive = false; };
  }, []);

  // Pick up prefilled query — polls briefly to catch async prefills (e.g. onboarding sets it after 200ms)
  useEffect(() => {
    let attempts = 0;
    function check() {
      const pf = window.__havn_prefill_query;
      if (!pf) return false;
      delete window.__havn_prefill_query;
      const query = typeof pf === "string" ? pf : pf.sql;
      const autoRun = typeof pf === "object" && pf.run;
      const next = fmt(query);
      // "Query this table" replaces the editor. Whatever the user had there
      // (or, while the panel is mounting, the saved draft the prefill skips
      // restoring) goes to History first, so an unrun draft is not just gone.
      let previous = sqlRef.current.trim();
      if (!previous) {
        try { previous = (localStorage.getItem("havn_query_draft") || "").trim(); } catch { /* ignore */ }
      }
      if (previous && previous !== next.trim()) {
        const kept = [{ sql: previous, ts: new Date().toISOString() }, ...getHistory().filter((h) => h.sql !== previous)];
        setHistory(kept);
        saveHistory(kept);
      }
      setSql(next);
      if (autoRun) setTimeout(() => runQuery(), 0);
      return true;
    }
    if (check()) return;
    // Poll for up to 500ms in case prefill is set after mount
    const interval = setInterval(() => {
      attempts++;
      if (check() || attempts >= 5) clearInterval(interval);
    }, 100);
    return () => clearInterval(interval);
  }, []);

  // Close the history dropdown on Escape or an outside click; Escape puts
  // focus back on the History button rather than dropping it on <body>.
  const historyBtnRef = useRef(null);
  useDismiss(historyOpen, () => { setHistoryOpen(false); setConfirmClear(false); }, [historyRef], historyBtnRef);

  // The query to execute: the textarea selection if one exists, else the full editor content
  function getEditorQuery() {
    const ta = textareaRef.current;
    const full = sqlRef.current;
    if (ta && ta.selectionStart !== ta.selectionEnd) {
      const sel = full.substring(ta.selectionStart, ta.selectionEnd).trim();
      if (sel) return { query: sel, isSelection: true };
    }
    return { query: full.trim(), isSelection: false };
  }

  // Collect typed values for the $params used by queryText; missing names block execution
  function collectParams(queryText) {
    const names = detectParams(queryText);
    if (names.length === 0) return { params: undefined, missing: [] };
    const params = {};
    const missing = [];
    for (const name of names) {
      const raw = paramValues[name];
      if (raw === undefined || String(raw).trim() === "") {
        missing.push(name);
        continue;
      }
      params[name] = coerceParamValue(String(raw));
    }
    return { params, missing };
  }

  function reportMissingParams(missing) {
    const plural = missing.length > 1 ? "s" : "";
    setError(`Missing value${plural} for parameter${plural}: ${missing.map((n) => `$${n}`).join(", ")}. Fill in the Parameters row above the toolbar.`);
  }

  function setParamValue(name, value) {
    setParamValues((prev) => {
      const next = { ...prev, [name]: value };
      try { localStorage.setItem("havn_query_params", JSON.stringify(next)); } catch { /* ignore */ }
      return next;
    });
  }

  // `opts.limit: false` runs without the automatic LIMIT whatever the toggle
  // says (the toggle's new state is not visible to this call yet).
  async function runQuery(opts = {}) {
    if (queryRunning || session.running) return;
    const { query, isSelection } = getEditorQuery();
    if (!query) return;
    const { params, missing } = collectParams(query);
    if (missing.length > 0) {
      reportMissingParams(missing);
      return;
    }
    setQueryRunning(true);
    setError(null);
    setExplainResult(null);
    setViewMode((m) => (m === "plan" ? "table" : m));
    // Fresh AbortController so the Cancel button can abort the in-flight fetch.
    const controller = new AbortController();
    queryAbortRef.current = controller;
    // Default limit goes in the request body; the server wraps the query safely
    const applyLimit = (opts.limit ?? limitEnabled) && !hasLimitClause(query);
    addOutput("info", `Running query${isSelection ? " (selection)" : ""}${applyLimit ? ` (limit ${DEFAULT_LIMIT})` : ""}...`);
    // The run writes its outcome to the module session, not only to this
    // component: if the user switches tabs mid-run, this instance unmounts
    // and the next one picks the result up from there.
    const run = (async () => {
      const started = performance.now();
      try {
        const data = await api.runQuery(query, controller.signal, {
          params,
          limit: applyLimit ? DEFAULT_LIMIT : undefined,
        });
        data.elapsedMs = Math.round(performance.now() - started);
        data.autoLimited = applyLimit;
        session.results = data;
        session.error = null;
        session.runSeq += 1;
        const entry = { sql: query, ts: new Date().toISOString() };
        if (params && Object.keys(params).length > 0) entry.params = params;
        const newHistory = [entry, ...getHistory().filter((h) => h.sql !== query)];
        setHistory(newHistory);
        saveHistory(newHistory);
        addOutput("info", `Query: ${data.rows.length} row${data.rows.length !== 1 ? "s" : ""} (${data.columns.length} cols)${data.truncated ? " — results capped for display. Use Export CSV for full results." : ""}`);
      } catch (e) {
        if (e.name === "AbortError" || controller.signal.aborted) {
          addOutput("info", "Query cancelled.");
        } else {
          // Suppress "Warehouse not found" in the OUTPUT panel (not actionable); inline error still shows
          const msg = e.message || "";
          const isEmptyWarehouse =
            msg.includes("Warehouse not found") || msg.includes("warehouse not initialized");
          // The previous query's rows must not stay on screen under this
          // query's error, where they read as this query's result.
          session.results = null;
          session.error = msg;
          if (!isEmptyWarehouse) {
            addOutput("error", `Query error: ${msg}`);
          }
        }
      } finally {
        session.running = null;
      }
    })();
    session.running = { promise: run, controller };
    await run;
    setResults(session.results);
    setError(session.error);
    setRunSeq(session.runSeq);
    queryAbortRef.current = null;
    setQueryRunning(false);
  }

  // Replace the whole buffer as an edit the browser can undo. setSql assigns
  // textarea.value, which wipes the undo history, so a Format (or a History
  // pick) the user did not want could not be taken back with Ctrl+Z.
  function replaceAllUndoable(text) {
    const ta = textareaRef.current;
    if (ta && typeof document.execCommand === "function") {
      acSuppressRef.current = true;
      let ok = false;
      try {
        ta.focus();
        ta.select();
        ok = document.execCommand("insertText", false, text);
      } catch {
        ok = false;
      } finally {
        acSuppressRef.current = false;
      }
      // execCommand fires the textarea's input event, which already synced
      // sqlRef and state through onInput.
      if (ok && ta.value === text) {
        hideCompletions();
        return;
      }
    }
    setSql(text);
  }

  async function explainQuery() {
    if (explaining) return;
    const { query } = getEditorQuery();
    if (!query) return;
    const { params, missing } = collectParams(query);
    if (missing.length > 0) {
      reportMissingParams(missing);
      return;
    }
    setExplaining(true);
    setError(null);
    setExplainResult(null);
    try {
      const data = await api.explainQuery(query, params);
      setExplainResult({ plan: data.plan, raw: data.raw, isAnalyze: false });
      setViewMode("plan");
      addOutput("info", "EXPLAIN plan generated");
    } catch (e) {
      session.error = e.message;
      setError(e.message);
      // The old plan was cleared above; staying on the Plan view would leave
      // the results area blank under the error.
      setViewMode((m) => (m === "plan" ? "table" : m));
      addOutput("error", `Explain error: ${e.message}`);
    } finally {
      setExplaining(false);
    }
  }

  async function formatQuery() {
    const before = sqlRef.current;
    if (!before.trim() || formatting) return;
    // Step 1: sql-formatter for layout (line breaks, indentation)
    const layoutFormatted = fmt(before);
    // Step 2: SQLFluff for keyword casing and DuckDB-specific fixes
    let next = layoutFormatted;
    setFormatting(true);
    try {
      const result = await api.formatSql(layoutFormatted);
      if (result.formatted && result.formatted.trim()) next = result.formatted;
    } catch { /* keep the layout-only result */ }
    finally { setFormatting(false); }
    // The round trip to the server takes a moment. Anything typed meanwhile
    // would be overwritten by a format of the older text, so drop it instead.
    if (sqlRef.current !== before) return;
    if (next !== before) replaceAllUndoable(next);
  }

  function handleKeyDown(e) {
    // Autocomplete navigation
    if (acItems.length > 0) {
      if (e.key === "ArrowDown") { e.preventDefault(); acNavigatedRef.current = true; setAcIndex((i) => Math.min(i + 1, acItems.length - 1)); return; }
      if (e.key === "ArrowUp")   { e.preventDefault(); acNavigatedRef.current = true; setAcIndex((i) => Math.max(i - 1, 0)); return; }
      const isTab = e.key === "Tab" && !e.shiftKey;
      const isEnter = e.key === "Enter" && !e.ctrlKey && !e.metaKey && !e.shiftKey;
      if (isTab || isEnter) {
        const ta = textareaRef.current;
        const item = acItems[acIndex];
        // Skip the list when: a selection is active (the key is meant for it),
        // the list is a keystroke behind while columns load (it would replace
        // the wrong characters), or Enter follows a word that is already a
        // keyword or alias and the user did not pick an item with the arrows.
        const usable = ta && ta.selectionStart === ta.selectionEnd && completionFits(sqlRef.current, ta.selectionStart, item);
        if (usable && (isTab || !item.soft || acNavigatedRef.current)) {
          e.preventDefault();
          applyCompletion(item);
          return;
        }
        hideCompletions();
        // Tab was meant to complete; don't send focus out of the editor instead.
        if (isTab) { e.preventDefault(); return; }
      }
      if (e.key === "Escape") { e.preventDefault(); hideCompletions(); return; }
    }
    // Moving the caret leaves any list, shown or still loading, describing the wrong word.
    if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(e.key)) hideCompletions();
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      runQuery();
    }
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === "F") {
      e.preventDefault();
      formatQuery();
    }
  }

  // Any resize of the editor (its own handles, the app's sidebars and output
  // panel, the window) rewraps the text and moves the caret away from the list.
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta || typeof ResizeObserver === "undefined") return;
    let first = true;
    const ro = new ResizeObserver(() => {
      if (first) { first = false; return; } // the initial observation is not a resize
      hideCompletions();
    });
    ro.observe(ta);
    return () => ro.disconnect();
  }, []);

  // Keep the highlighted suggestion in view while arrowing through the list.
  useEffect(() => {
    // Scroll only the list: scrollIntoView would also scroll the panel's
    // overflow:hidden ancestors, shifting the editor with no way back.
    const list = acListRef.current;
    const el = list?.children[acIndex];
    if (!el) return;
    if (el.offsetTop < list.scrollTop) list.scrollTop = el.offsetTop;
    else if (el.offsetTop + el.offsetHeight > list.scrollTop + list.clientHeight) {
      list.scrollTop = el.offsetTop + el.offsetHeight - list.clientHeight;
    }
  }, [acIndex, acItems]);

  function hideCompletions() {
    acRequestRef.current++; // a describeTable still in flight must not reopen the list
    acNavigatedRef.current = false;
    setAcItems([]);
  }

  function applyCompletion(item) {
    const ta = textareaRef.current;
    if (!ta || !item) return;
    if (ta.selectionStart !== ta.selectionEnd || !completionFits(sqlRef.current, ta.selectionStart, item)) { hideCompletions(); return; }
    const { value, cursor } = applyCompletionText(sqlRef.current, ta.selectionStart, item);
    hideCompletions();
    ta.focus();
    // Insert through the browser's editing command so Ctrl+Z undoes the
    // completion; assigning .value would wipe the undo history.
    ta.setSelectionRange(Math.max(0, ta.selectionStart - item.replace), ta.selectionStart);
    acSuppressRef.current = true;
    let inserted = false;
    try { inserted = document.execCommand("insertText", false, item.insert); } catch { /* unsupported */ }
    acSuppressRef.current = false;
    if (!inserted || ta.value !== value) setSql(value);
    ta.selectionStart = ta.selectionEnd = cursor;
  }

  async function getColumns(schema, table) {
    const key = `${schema}.${table}`;
    if (!colCacheRef.current[key]) {
      try {
        const info = await api.describeTable(schema, table);
        colCacheRef.current[key] = info.columns || [];
      } catch {
        colCacheRef.current[key] = []; // don't re-ask on every keystroke for a name that isn't a table
      }
    }
    return colCacheRef.current[key];
  }

  // Where the list goes: under the caret, or above it when the caret is near
  // the bottom of the panel. It floats over the toolbar instead of pushing it.
  function completionPosition(ta, cursor) {
    const wrapper = ta.parentElement;
    const area = wrapper.parentElement;
    const caret = caretCoordinates(ta, cursor);
    const top = caret.top - ta.scrollTop;
    const left = Math.max(4, Math.min(caret.left - ta.scrollLeft, wrapper.clientWidth - AC_WIDTH - 4));
    const areaRect = area.getBoundingClientRect();
    const wrapperTop = wrapper.getBoundingClientRect().top;
    const below = areaRect.bottom - (wrapperTop + top + caret.height) - 8;
    const above = wrapperTop + top - areaRect.top - 8;
    // Never taller than the room on its side: past the panel edge it would be clipped.
    if (below < AC_MAX_HEIGHT && above > below) {
      return { left, bottom: wrapper.clientHeight - top + 2, maxHeight: Math.max(60, Math.min(AC_MAX_HEIGHT, above)) };
    }
    return { left, top: top + caret.height + 2, maxHeight: Math.max(60, Math.min(AC_MAX_HEIGHT, below)) };
  }

  async function computeAutocomplete(value, cursor) {
    // Stamp this invocation so a slow describeTable response from an earlier
    // keystroke can't overwrite suggestions from a newer one.
    const reqId = ++acRequestRef.current;
    const items = await buildCompletions(value, cursor ?? value.length, tables, getColumns);
    if (acRequestRef.current !== reqId) return;
    const ta = textareaRef.current;
    if (!items.length || !ta || document.activeElement !== ta) { setAcItems([]); return; }
    setAcPos(completionPosition(ta, ta.selectionStart));
    acNavigatedRef.current = false;
    setAcItems(items);
    setAcIndex(0);
  }

  function insertAtCursor(text) {
    const ta = textareaRef.current;
    if (!ta) {
      setSql((prev) => prev + (prev ? " " : "") + text);
      return;
    }
    const start = ta.selectionStart;
    const end = ta.selectionEnd;
    const expected = sqlRef.current.substring(0, start) + text + sqlRef.current.substring(end);
    // Through the browser's editing command, like a completion, so a table
    // inserted from the sidebar is one Ctrl+Z away instead of wiping undo.
    ta.focus();
    ta.setSelectionRange(start, end);
    acSuppressRef.current = true;
    let inserted = false;
    try { inserted = document.execCommand("insertText", false, text); } catch { /* unsupported */ }
    acSuppressRef.current = false;
    if (inserted && ta.value === expected) {
      hideCompletions();
      return;
    }
    setSql(expected);
    setTimeout(() => {
      ta.focus();
      ta.selectionStart = ta.selectionEnd = start + text.length;
    }, 0);
  }

  // Generate starter query suggestions from available tables
  function getSuggestions() {
    if (tables.length === 0) return [];
    const suggestions = [];
    const TIER = ["gold", "silver", "bronze", "landing"];
    for (const tier of TIER) {
      const t = tables.find((t) => t.schema === tier);
      if (t) {
        suggestions.push({ sql: fmt(`SELECT * FROM ${t.schema}.${t.name} LIMIT 10`), label: `Preview ${t.schema}.${t.name}` });
        if (suggestions.length === 1) {
          suggestions.push({ sql: fmt(`SELECT COUNT(*) AS total FROM ${t.schema}.${t.name}`), label: `Count ${t.schema}.${t.name}` });
        }
      }
      if (suggestions.length >= 3) break;
    }
    if (suggestions.length < 4) {
      suggestions.push({
        sql: fmt("SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema NOT IN ('information_schema', '_havn') ORDER BY table_schema, table_name"),
        label: "List all tables",
      });
    }
    return suggestions;
  }

  const suggestions = getSuggestions();
  const detectedParams = detectParams(sql);
  // Masking is per table.column; the result grid only knows column names.
  const maskedColumns = useMemo(() => {
    const acc = {};
    for (const [key, method] of Object.entries(maskingPolicies || {})) {
      const col = key.split(".").pop();
      if (!acc[col]) acc[col] = method;
    }
    return acc;
  }, [maskingPolicies]);

  return (
    <div
      style={st.container}
      // Run from anywhere in the panel (a parameter box, the results), not
      // only from the editor. The editor and inputs that handle the keys
      // themselves mark the event, and are left alone.
      onKeyDown={(e) => {
        if (e.defaultPrevented || e.key !== "Enter" || !(e.ctrlKey || e.metaKey)) return;
        e.preventDefault();
        runQuery();
      }}
    >
      <div style={st.main}>
        {/* Schema sidebar */}
        <div data-havn-hint="query-sidebar" data-havn-guide="query-tables" style={{ display: "flex", flexDirection: "column", width: sidebarWidth, flexShrink: 0 }}>
          <SchemaSidebar tables={tables} onInsert={insertAtCursor} maskingPolicies={maskingPolicies} onOpenModel={onOpenModel} />
        </div>
        <ResizeHandle direction="horizontal" label="Resize table list" onResize={onSidebarResize} onResizeStart={() => { hideCompletions(); onSidebarResizeStart(); }} />

        {/* Query area */}
        <div ref={queryAreaRef} style={st.queryArea}>
          {/* SQL textarea */}
          <div style={{ ...st.editorWrapper, height: visibleEditorHeight }}>
            <textarea
              ref={textareaRef}
              defaultValue=""
              onInput={(e) => {
                const v = e.target.value; const cur = e.target.selectionStart;
                sqlRef.current = v; _setSql(v);
                lastInputAtRef.current = performance.now();
                // Undo/redo restores text; reopening the list would make the next Enter redo the completion.
                const historyEdit = /^history/.test(e.nativeEvent?.inputType || "");
                if (historyEdit) hideCompletions();
                else if (!acSuppressRef.current) computeAutocomplete(v, cur);
              }}
              onKeyDown={handleKeyDown}
              onBlur={hideCompletions}
              onMouseDown={hideCompletions}
              onScroll={() => {
                // Typing can scroll the textarea itself; only a scroll by the user closes the list.
                if (performance.now() - lastInputAtRef.current > 150) hideCompletions();
              }}
              placeholder="SELECT * FROM ..."
              style={{ ...st.textarea, height: "100%", resize: "none" }}
              spellCheck={false}
              aria-label="SQL query editor"
              aria-autocomplete="list"
              aria-controls={acItems.length > 0 ? "query-ac-list" : undefined}
              aria-activedescendant={acItems.length > 0 ? `query-ac-${acIndex}` : undefined}
            />
            {acItems.length > 0 && (
              <div
                id="query-ac-list"
                ref={acListRef}
                style={{ ...st.acDropdown, ...acPos }}
                role="listbox"
                aria-label="Autocomplete suggestions"
                // Keep focus (and the caret) in the textarea while clicking a suggestion.
                onMouseDown={(e) => e.preventDefault()}
              >
                {acItems.map((item, i) => (
                  <div
                    key={`${item.kind}:${item.insert}`}
                    id={`query-ac-${i}`}
                    role="option"
                    aria-selected={i === acIndex}
                    onClick={() => applyCompletion(item)}
                    onMouseEnter={() => setAcIndex(i)}
                    style={i === acIndex ? st.acItemActive : st.acItem}
                  >
                    <span style={st.acKind}>{item.kind === "column" ? "col" : "tbl"}</span>
                    <span style={st.acLabel}>{item.label}</span>
                    {item.detail && <span style={st.acDetail}>{item.detail}</span>}
                  </div>
                ))}
              </div>
            )}
          </div>
          <ResizeHandle
            direction="vertical"
            label="Resize editor"
            onResize={onEditorResize}
            onResizeStart={() => { hideCompletions(); onEditorResizeStart(visibleEditorHeight); }}
            onDoubleClick={resetEditorHeight}
          />
          <div style={st.shortcutHint}>
            <span style={st.shortcutKey}>{MOD_KEY}+Enter</span> run selection or all &nbsp;·&nbsp;
            <span style={st.shortcutKey}>{MOD_KEY}+Shift+F</span> format &nbsp;·&nbsp;
            <span style={st.shortcutKey}>Tab</span> autocomplete
          </div>
          {/* Parameter inputs for $name placeholders in the query */}
          {detectedParams.length > 0 && (
            <div style={st.paramRow}>
              <span
                style={st.paramLabel}
                title={"Named parameters are bound server-side as prepared-statement values (no string interpolation). Numbers and true/false are sent typed; everything else is sent as text. Cast in SQL when needed, e.g. $day::DATE."}
              >
                Parameters
              </span>
              {detectedParams.map((name) => (
                <label key={name} style={st.paramItem}>
                  <span style={st.paramName}>${name}</span>
                  <input
                    value={paramValues[name] ?? ""}
                    onChange={(e) => setParamValue(name, e.target.value)}
                    // Fill in the value, press Enter, see rows: no trip back
                    // to the editor or the mouse just to run.
                    onKeyDown={(e) => {
                      if (e.key === "Enter") { e.preventDefault(); runQuery(); }
                    }}
                    placeholder="value"
                    style={st.paramInput}
                    spellCheck={false}
                    aria-label={`Value for parameter ${name}`}
                  />
                </label>
              ))}
            </div>
          )}
          {/* Toolbar */}
          <div style={st.toolbar}>
            {queryRunning ? (
              <button onClick={() => { if (queryAbortRef.current) queryAbortRef.current.abort(); }} style={st.cancelBtn} aria-label="Cancel query">
                Cancel
              </button>
            ) : (
              <button onClick={() => runQuery()} disabled={!sql.trim()} style={!sql.trim() ? st.runBtnDisabled : st.runBtn} aria-label="Run query" title="Run the query, or only the selected text if you have a selection">
                Run <span style={st.shortcut}>{MOD_KEY}+Enter</span>
              </button>
            )}
            <button onClick={explainQuery} disabled={explaining || !sql.trim()} style={st.fmtBtn} title="Show query execution plan">
              {explaining ? "Explaining…" : "Explain"}
            </button>
            <button onClick={formatQuery} disabled={formatting || !sql.trim()} style={st.fmtBtn} title={`Format SQL (${MOD_KEY}+Shift+F)`}>
              Format <span style={st.shortcut}>{MOD_KEY}+Shift+F</span>
            </button>
            <button
              onClick={() => setLimitEnabled(!limitEnabled)}
              style={{
                ...st.fmtBtn,
                background: limitEnabled ? "color-mix(in srgb, var(--havn-accent) 15%, transparent)" : undefined,
                borderColor: limitEnabled ? "var(--havn-accent)" : undefined,
                color: limitEnabled ? "var(--havn-accent)" : undefined,
              }}
              title={limitEnabled ? `Auto-limit queries to ${DEFAULT_LIMIT} rows (click to disable)` : `Enable auto-limit to ${DEFAULT_LIMIT} rows`}
              aria-pressed={limitEnabled}
            >
              LIMIT {DEFAULT_LIMIT}
            </button>

            <button
              onClick={async () => {
                if (exporting) return;
                const { query } = getEditorQuery();
                if (!query) return;
                const { params, missing } = collectParams(query);
                if (missing.length > 0) {
                  reportMissingParams(missing);
                  return;
                }
                setExporting(true);
                addOutput("info", "Exporting to CSV (no row limit)...");
                try {
                  await api.exportCsv(query, params);
                  addOutput("success", "CSV export downloaded");
                } catch (e) {
                  // Shown here too: the output panel is often collapsed, and
                  // a click that silently does nothing reads as a dead button.
                  session.error = `Export failed: ${e.message}`;
                  setError(session.error);
                  addOutput("error", `Export failed: ${e.message}`);
                } finally {
                  setExporting(false);
                }
              }}
              disabled={exporting || !sql.trim()}
              style={st.fmtBtn}
              title="Export full query results as CSV (no row limit)"
            >
              {exporting ? "Exporting…" : "Export CSV"}
            </button>

            {/* History dropdown */}
            <div ref={historyRef} style={st.historyWrapper}>
              <button ref={historyBtnRef} onClick={() => { setHistoryOpen(!historyOpen); setConfirmClear(false); }} style={st.historyBtn} title="Query history" aria-label="Query history" aria-expanded={historyOpen} aria-haspopup="true">
                History {history.length > 0 && <span style={st.historyCount}>{history.length}</span>}
              </button>
              {historyOpen && (
                <div style={st.historyDropdown}>
                  <div style={st.historyHeader}>
                    <span>Recent Queries</span>
                    {history.length > 0 && (
                      // Two clicks: one stray click used to wipe every saved query.
                      <button
                        onClick={() => {
                          if (!confirmClear) { setConfirmClear(true); return; }
                          setHistory([]); saveHistory([]); setConfirmClear(false);
                        }}
                        onBlur={() => setConfirmClear(false)}
                        style={confirmClear ? { ...st.historyClear, color: "var(--havn-red)" } : st.historyClear}
                        title="Clear query history"
                      >
                        {confirmClear ? `Clear all ${history.length}?` : "Clear"}
                      </button>
                    )}
                  </div>
                  {history.length === 0 ? (
                    <div style={st.historyEmpty}>No history yet</div>
                  ) : (
                    // All of it: the badge counts up to MAX_HISTORY entries, and
                    // listing only 20 made the rest unreachable.
                    history.map((h, i) => (
                      <button
                        key={`${h.ts}:${i}`}
                        onClick={() => {
                          setHistoryOpen(false);
                          // Undoable, so a misclick does not cost the current draft.
                          replaceAllUndoable(h.sql);
                          if (h.params) {
                            for (const [k, v] of Object.entries(h.params)) setParamValue(k, String(v));
                          }
                        }}
                        title={h.sql}
                        style={st.historyItem}
                        onMouseEnter={(e) => e.currentTarget.style.background = "var(--havn-btn-bg)"}
                        onMouseLeave={(e) => e.currentTarget.style.background = "none"}
                      >
                        <span style={st.historySQL}>{h.sql.substring(0, 80)}{h.sql.length > 80 ? "..." : ""}</span>
                        <span style={st.historyMeta}>
                          {h.params && Object.keys(h.params).length > 0 && (
                            <span style={st.historyParams}>
                              {Object.entries(h.params).map(([k, v]) => `$${k}=${v}`).join("  ")}
                            </span>
                          )}
                          {h.ts && <span style={st.historyTime}>{timeAgo(h.ts)}</span>}
                        </span>
                      </button>
                    ))
                  )}
                </div>
              )}
            </div>

          </div>

          {/* Starter suggestions when textarea is empty */}
          {!sql.trim() && suggestions.length > 0 && (
            <div style={st.suggestions}>
              <span style={st.suggestLabel}>Try:</span>
              {suggestions.map((s, i) => (
                // "Preview gold.x" should show rows, not just write the SQL and
                // wait for a second click. Every suggestion is a read-only SELECT.
                <button key={i} onClick={() => { setSql(s.sql); runQuery(); }} style={st.suggestBtn} title={s.sql}>
                  {s.label}
                </button>
              ))}
            </div>
          )}

          {/* Error */}
          {error && (
            <div style={st.error} role="alert">
              <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: "8px" }}>
                <div style={{ display: "flex", alignItems: "flex-start", gap: "8px", minWidth: 0 }}>
                  <span style={st.errorLabel}>Error</span>
                  <span style={st.errorMessage}>{error}</span>
                </div>
                <button
                  onClick={() => { session.error = null; setError(null); }}
                  style={st.errorDismiss}
                  aria-label="Dismiss error"
                  title="Dismiss"
                >{"\u00D7"}</button>
              </div>
            </div>
          )}

          {/* Result tabs */}
          {(results || explainResult) && (
            <div style={{ display: "flex", gap: 0, borderBottom: "1px solid var(--havn-border)", padding: "0 12px" }}>
              {results && (
                <button onClick={() => setViewMode("table")} style={{ padding: "6px 14px", fontSize: 12, fontWeight: 500, cursor: "pointer", background: "none", border: "none", borderBottom: viewMode === "table" ? "2px solid var(--havn-accent)" : "2px solid transparent", color: viewMode === "table" ? "var(--havn-accent)" : "var(--havn-text-secondary)" }}>
                  Table ({results.rows.length})
                </button>
              )}
              {results && (
                <button onClick={() => setViewMode("chart")} style={{ padding: "6px 14px", fontSize: 12, fontWeight: 500, cursor: "pointer", background: "none", border: "none", borderBottom: viewMode === "chart" ? "2px solid var(--havn-accent)" : "2px solid transparent", color: viewMode === "chart" ? "var(--havn-accent)" : "var(--havn-text-secondary)" }}>
                  Chart
                </button>
              )}
              {explainResult && (
                <button onClick={() => setViewMode("plan")} style={{ padding: "6px 14px", fontSize: 12, fontWeight: 500, cursor: "pointer", background: "none", border: "none", borderBottom: viewMode === "plan" ? "2px solid var(--havn-accent)" : "2px solid transparent", color: viewMode === "plan" ? "var(--havn-accent)" : "var(--havn-text-secondary)" }}>
                  Plan
                </button>
              )}
            </div>
          )}

          {/* Results area */}
          {viewMode === "plan" && explainResult && (
            <div style={st.results}>
              <ExplainPanel plan={explainResult.plan} raw={explainResult.raw} isAnalyze={explainResult.isAnalyze} />
            </div>
          )}
          {viewMode === "table" && results && (
            // The previous result stays up while a new run is going, dimmed
            // and marked, so it is not mistaken for the new query's rows.
            <div style={queryRunning ? { ...st.results, ...st.resultsStale } : st.results} aria-busy={queryRunning}>
              <div style={st.resultsHeader}>
                <span style={st.resultsCount}>{results.rows.length.toLocaleString()} row{results.rows.length !== 1 ? "s" : ""}</span>
                <span style={st.resultsSep}>&middot;</span>
                <span>{results.columns.length} col{results.columns.length !== 1 ? "s" : ""}</span>
                {typeof results.elapsedMs === "number" && (
                  <>
                    <span style={st.resultsSep}>&middot;</span>
                    <span title="Time from Run to rows on screen, including the transfer">{formatElapsed(results.elapsedMs)}</span>
                  </>
                )}
                {results.truncated && (
                  results.autoLimited ? (
                    <span style={st.resultsTruncated}>
                      First {results.rows.length.toLocaleString()} rows (LIMIT {DEFAULT_LIMIT} is on)
                      {!queryRunning && (
                        <button
                          type="button"
                          style={st.resultsTruncatedBtn}
                          onClick={() => { setLimitEnabled(false); runQuery({ limit: false }); }}
                          title={`Turn off the automatic LIMIT ${DEFAULT_LIMIT} and run again`}
                        >
                          Run without limit
                        </button>
                      )}
                    </span>
                  ) : (
                    <span
                      style={st.resultsTruncated}
                      title="The browser shows at most this many rows. Export CSV writes every row."
                    >
                      First {results.rows.length.toLocaleString()} rows shown, more not loaded (Export CSV has all of them)
                    </span>
                  )
                )}
                {queryRunning && <span style={st.resultsRunning}>Running query…</span>}
              </div>
              <div style={st.resultsBody}>
                {/* Keyed per result: sort, filters and pinned columns belong to
                    the result they were set on, not to the next query's columns. */}
                <SortableTable key={runSeq} fill columns={results.columns} rows={results.rows} columnTypes={results.column_types} maskedColumns={maskedColumns} />
              </div>
            </div>
          )}
          {viewMode === "chart" && results && (
            <div style={queryRunning ? { ...st.results, ...st.resultsStale } : st.results} aria-busy={queryRunning}>
              <div style={st.resultsBody}>
                <ChartPanel key={runSeq} columns={results.columns} rows={results.rows} />
              </div>
            </div>
          )}

          {/* Empty state — no results yet */}
          {!results && !explainResult && !error && (
            <div style={st.emptyState}>
              <div style={st.emptyText} role="status">
                {queryRunning
                  ? "Running query…"
                  : sql.trim() ? `Press ${MOD_KEY}+Enter to run your query` : "Write a SQL query above to get started"}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

const st = {
  container: { display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" },
  main: { display: "flex", flex: 1, overflow: "hidden" },

  queryArea: { flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" },
  toolbar: { display: "flex", flexWrap: "wrap", alignItems: "center", gap: "8px", padding: "6px 12px", borderBottom: "1px solid var(--havn-border)", background: "var(--havn-bg-secondary)", flexShrink: 0 },
  runBtn: {
    padding: "5px 16px",
    background: "var(--havn-accent)",
    border: "1px solid var(--havn-accent)",
    borderRadius: "var(--havn-radius-lg)",
    color: "var(--havn-bg)",
    cursor: "pointer",
    fontSize: "12px",
    whiteSpace: "nowrap",
    fontWeight: 600,
    display: "flex",
    alignItems: "center",
    gap: "6px",
  },
  runBtnDisabled: {
    padding: "5px 16px",
    background: "var(--havn-btn-bg)",
    border: "1px solid var(--havn-btn-border)",
    borderRadius: "var(--havn-radius-lg)",
    color: "var(--havn-text-dim)",
    cursor: "default",
    fontSize: "12px",
    whiteSpace: "nowrap",
    fontWeight: 600,
    display: "flex",
    alignItems: "center",
    gap: "6px",
    opacity: 0.6,
  },
  cancelBtn: {
    padding: "5px 16px",
    background: "var(--havn-red)",
    border: "1px solid var(--havn-red)",
    borderRadius: "var(--havn-radius-lg)",
    color: "#fff",
    cursor: "pointer",
    fontSize: "12px",
    whiteSpace: "nowrap",
    fontWeight: 600,
    display: "flex",
    alignItems: "center",
    gap: "6px",
  },
  shortcut: { fontSize: "10px", opacity: 0.7 },
  fmtBtn: { padding: "5px 10px", background: "var(--havn-btn-bg)", border: "1px solid var(--havn-btn-border)", borderRadius: "var(--havn-radius-lg)", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: "12px", fontWeight: 500, display: "flex", alignItems: "center", gap: "6px", whiteSpace: "nowrap" },

  historyWrapper: { position: "relative", marginLeft: "auto" },
  historyBtn: { background: "var(--havn-btn-bg)", border: "1px solid var(--havn-btn-border)", borderRadius: "var(--havn-radius-lg)", color: "var(--havn-text-secondary)", cursor: "pointer", padding: "5px 10px", fontSize: "12px", fontWeight: 500, display: "flex", alignItems: "center", gap: "4px", whiteSpace: "nowrap" },
  historyCount: { fontSize: "10px", background: "var(--havn-bg-tertiary)", padding: "0 5px", borderRadius: "8px", color: "var(--havn-text-dim)", fontWeight: 500 },
  historyDropdown: { position: "absolute", top: "100%", right: 0, marginTop: "4px", background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius)", zIndex: 100, width: "400px", maxHeight: "300px", overflow: "auto", boxShadow: "0 4px 12px rgba(0,0,0,0.3)" },
  historyHeader: { display: "flex", alignItems: "center", justifyContent: "space-between", padding: "6px 12px", fontSize: "11px", fontWeight: 600, color: "var(--havn-text-secondary)", borderBottom: "1px solid var(--havn-border)" },
  historyClear: { background: "none", border: "none", color: "var(--havn-text-dim)", cursor: "pointer", fontSize: "10px", fontWeight: 500, padding: "0 2px", textDecoration: "underline" },
  historyEmpty: { padding: "16px", color: "var(--havn-text-dim)", fontSize: "12px", textAlign: "center" },
  historyItem: { display: "block", width: "100%", padding: "6px 12px", background: "none", border: "none", borderBottom: "1px solid var(--havn-border)", color: "var(--havn-text)", cursor: "pointer", textAlign: "left", fontSize: "12px" },
  historySQL: { fontFamily: "var(--havn-font-mono)", fontSize: "11px", color: "var(--havn-text)", display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  historyMeta: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: "8px", marginTop: "2px" },
  historyParams: { fontFamily: "var(--havn-font-mono)", fontSize: "10px", color: "var(--havn-accent)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  historyTime: { fontSize: "10px", color: "var(--havn-text-dim)", flexShrink: 0, marginLeft: "auto" },

  viewToggle: { display: "flex", marginLeft: "auto", gap: "1px", background: "var(--havn-border)", borderRadius: "var(--havn-radius-lg)", overflow: "hidden" },
  viewBtn: { padding: "3px 10px", background: "var(--havn-btn-bg)", border: "none", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: "11px", fontWeight: 500 },
  viewBtnActive: { padding: "3px 10px", background: "var(--havn-bg-secondary)", border: "none", color: "var(--havn-text)", cursor: "pointer", fontSize: "11px", fontWeight: 600 },

  editorWrapper: { flexShrink: 0, borderBottom: "1px solid var(--havn-border)", position: "relative" },
  textarea: { width: "100%", height: "100%", padding: "12px 14px", background: "var(--havn-bg)", border: "none", color: "var(--havn-text)", fontFamily: "var(--havn-font-mono)", fontSize: "13px", resize: "none", outline: "none", boxSizing: "border-box", lineHeight: 1.6, display: "block" },
  shortcutHint: { height: "22px", display: "flex", alignItems: "center", padding: "0 10px", gap: "2px", fontSize: "10px", color: "var(--havn-text-dim)", background: "var(--havn-bg)", borderBottom: "1px solid var(--havn-border)", flexShrink: 0 },
  shortcutKey: { background: "var(--havn-btn-bg)", border: "1px solid var(--havn-btn-border)", borderRadius: "3px", padding: "0 4px", fontSize: "10px", fontFamily: "var(--havn-font-mono)", color: "var(--havn-text-secondary)" },
  acDropdown: { position: "absolute", zIndex: 30, width: `${AC_WIDTH}px`, maxWidth: "calc(100% - 8px)", maxHeight: `${AC_MAX_HEIGHT}px`, overflowY: "auto", background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius)", boxShadow: "0 6px 20px rgba(0, 0, 0, 0.3)", padding: "3px 0", boxSizing: "border-box" },
  acItem: { display: "flex", alignItems: "center", gap: "8px", padding: "5px 10px", cursor: "pointer", fontSize: "12px" },
  acItemActive: { display: "flex", alignItems: "center", gap: "8px", padding: "5px 10px", cursor: "pointer", fontSize: "12px", background: "var(--havn-btn-bg)" },
  acKind: { fontSize: "9px", fontFamily: "var(--havn-font-mono)", color: "var(--havn-text-dim)", background: "var(--havn-bg-tertiary)", border: "1px solid var(--havn-border)", borderRadius: "3px", padding: "0 4px", flexShrink: 0, width: "22px", textAlign: "center" },
  acLabel: { fontFamily: "var(--havn-font-mono)", color: "var(--havn-text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0 },
  acDetail: { marginLeft: "auto", paddingLeft: "12px", fontFamily: "var(--havn-font-mono)", fontSize: "11px", color: "var(--havn-text-dim)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", flexShrink: 1, maxWidth: "45%" },

  paramRow: { display: "flex", alignItems: "center", gap: "10px", padding: "5px 12px", flexWrap: "wrap", background: "var(--havn-bg-secondary)", borderBottom: "1px solid var(--havn-border)", flexShrink: 0 },
  paramLabel: { fontSize: "10px", fontWeight: 600, color: "var(--havn-text-secondary)", textTransform: "uppercase", letterSpacing: "0.3px", cursor: "help" },
  paramItem: { display: "flex", alignItems: "center", gap: "5px" },
  paramName: { fontFamily: "var(--havn-font-mono)", fontSize: "11px", color: "var(--havn-accent)", fontWeight: 500 },
  paramInput: { width: "120px", padding: "3px 6px", background: "var(--havn-bg)", border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius)", color: "var(--havn-text)", fontSize: "11px", fontFamily: "var(--havn-font-mono)", outline: "none" },

  suggestions: { display: "flex", alignItems: "center", gap: "6px", padding: "8px 12px", flexWrap: "wrap", borderBottom: "1px solid var(--havn-border)" },
  suggestLabel: { fontSize: "11px", color: "var(--havn-text-dim)", fontWeight: 500 },
  suggestBtn: { padding: "3px 10px", background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius-lg)", color: "var(--havn-accent)", cursor: "pointer", fontSize: "11px", fontFamily: "var(--havn-font-mono)", fontWeight: 500 },

  // Capped and scrollable: a long DuckDB error (a binder error lists every
  // candidate column) used to push the results off the bottom of the panel.
  error: { padding: "10px 12px", background: "color-mix(in srgb, var(--havn-red) 8%, transparent)", borderLeft: "3px solid var(--havn-red)", margin: "0", color: "var(--havn-text)", fontSize: "12px", fontFamily: "var(--havn-font-mono)", whiteSpace: "pre-wrap", lineHeight: 1.5, maxHeight: "35%", overflow: "auto", flexShrink: 0 },
  errorLabel: { fontWeight: 600, color: "var(--havn-red)", fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.3px", flexShrink: 0 },
  errorMessage: { color: "var(--havn-red)", wordBreak: "break-word" },
  errorDismiss: { background: "none", border: "none", color: "var(--havn-text-dim)", cursor: "pointer", fontSize: "16px", lineHeight: 1, flexShrink: 0, padding: "0 2px" },

  results: { flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" },
  resultsHeader: { display: "flex", alignItems: "center", flexWrap: "wrap", gap: "6px", padding: "6px 12px", fontSize: "11px", color: "var(--havn-text-dim)", borderBottom: "1px solid var(--havn-border)", flexShrink: 0, fontFamily: "var(--havn-font-mono)" },
  resultsCount: { color: "var(--havn-text-secondary)", fontWeight: 500 },
  resultsSep: { opacity: 0.4 },
  resultsTruncated: { marginLeft: "4px", color: "var(--havn-yellow)", fontSize: "11px", fontWeight: 500, display: "inline-flex", alignItems: "center", gap: "8px", flexWrap: "wrap" },
  resultsTruncatedBtn: { background: "none", border: "1px solid currentColor", borderRadius: "var(--havn-radius)", color: "inherit", cursor: "pointer", font: "inherit", fontSize: "10px", padding: "1px 6px" },
  resultsRunning: { marginLeft: "auto", color: "var(--havn-accent)", fontWeight: 500 },
  resultsStale: { opacity: 0.55 },
  // No scrolling here: the table (fill mode) is the one scroll container, so
  // its header stays pinned. A second scroller around it defeated that.
  resultsBody: { flex: 1, minHeight: 0, overflow: "hidden", display: "flex", flexDirection: "column" },

  emptyState: { flex: 1, display: "flex", alignItems: "center", justifyContent: "center", padding: "32px" },
  emptyText: { fontSize: "13px", color: "var(--havn-text-dim)", fontFamily: "var(--havn-font-mono)" },
};
