import React, { useState, useEffect, useRef, useCallback } from "react";
import { api } from "./api";
import ResizeHandle from "./ResizeHandle";
import useResizable from "./useResizable";
import SortableTable from "./SortableTable";
import { safeGetItem, safeSetItem } from "./safeStorage";

/*
 * The editor workbench for a single SQL model: lineage above the code, an
 * inspector (Preview / Checks / Columns / Runs) beside it, failed assertions
 * marked on their own line, and an action bar that says what a build touches.
 * Everything besides the preview comes from one GET /api/models/workbench.
 */

const TABS = ["preview", "checks", "columns", "runs"];
const TAB_LABELS = { preview: "Preview", checks: "Checks", columns: "Columns", runs: "Runs" };
const INSPECTOR_KEY = "havn_workbench_inspector_open";

/* ------------------------------------------------------------------ */
/* Pure helpers (exported for tests)                                   */
/* ------------------------------------------------------------------ */

const ASSERT_RE = /^\s*(?:@assert\b\s*\(?|--\s*assert:)\s*(.*)$/i;
const GRAIN_RE = /^\s*(?:@grain\b|--\s*grain:)/i;

/** 1-based line number of the directive that declares `expr`, or 0. */
export function findAssertionLine(text, expr) {
  const lines = (text || "").split("\n");
  const want = (expr || "").replace(/\s+/g, " ").trim();
  if (!want) return 0;
  if (/^grain\(/.test(want)) {
    const i = lines.findIndex((l) => GRAIN_RE.test(l));
    return i + 1;
  }
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(ASSERT_RE);
    if (!m) continue;
    const body = m[1].replace(/\s+/g, " ").trim();
    if (body === want || body.startsWith(want + ",") || body.startsWith(want + " ") || body.startsWith(want + ")")) {
      return i + 1;
    }
  }
  return 0;
}

/** Index (0-based) of the line after the model's leading directive block. */
function directiveBlockEnd(lines) {
  let last = -1;
  for (let i = 0; i < lines.length; i++) {
    const t = lines[i].trim();
    if (t === "") continue;
    if (t.startsWith("@") || /^--\s*(config|depends_on|assert|description|col|grain)\b/i.test(t)) {
      last = i;
      continue;
    }
    break;
  }
  return last + 1;
}

/** `text` with an `@col name: ` line added after the directive block. */
export function insertColDoc(text, name) {
  const lines = (text || "").split("\n");
  const at = directiveBlockEnd(lines);
  lines.splice(at, 0, `@col ${name}: `);
  return { text: lines.join("\n"), line: at + 1 };
}

export function timeAgo(dateStr) {
  if (!dateStr) return "";
  const d = new Date(dateStr.includes("T") || dateStr.endsWith("Z") ? dateStr : dateStr.replace(" ", "T"));
  const s = Math.floor((Date.now() - d.getTime()) / 1000);
  if (Number.isNaN(s)) return dateStr;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function fmtMs(ms) {
  if (ms == null) return "";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)}s`;
}

/** The status sentence for the action bar: { tone, text }. */
export function buildStatus(data, dirty) {
  const n = data?.downstream_all?.length || 0;
  const blast = n > 0 ? ` · ${n} downstream model${n === 1 ? "" : "s"} depend on this` : "";
  if (dirty) return { tone: "warn", text: `Unsaved changes${blast}` };
  if (!data) return { tone: "dim", text: "" };
  const st = data.state || {};
  if (!st.built) return { tone: "dim", text: `Not built yet${blast}` };
  if (st.changed_since_build) return { tone: "warn", text: `Changed since last build${blast}` };
  const rows = st.row_count != null ? ` · ${Number(st.row_count).toLocaleString()} rows` : "";
  const failing = (data.checks || []).filter((c) => c.passed === false);
  if (failing.length) {
    const hard = failing.some((c) => c.severity !== "warn");
    return {
      tone: hard ? "bad" : "warn",
      text: `Built ${timeAgo(st.last_run_at)}${rows} · ${failing.length} check${failing.length === 1 ? "" : "s"} failing`,
    };
  }
  return { tone: "ok", text: `Up to date · built ${timeAgo(st.last_run_at)}${rows}` };
}

function checkSummary(checks) {
  const failed = checks.filter((c) => c.passed === false);
  const errors = failed.filter((c) => c.severity !== "warn");
  if (errors.length) return "fail";
  if (failed.length) return "warn";
  if (checks.length && checks.every((c) => c.passed === true)) return "pass";
  return "unknown";
}

/* ------------------------------------------------------------------ */
/* Editor decorations for failed assertions                            */
/* ------------------------------------------------------------------ */

const DECORATION_CSS = `
.havn-assert-fail-line { background: color-mix(in srgb, var(--havn-red) 12%, transparent); }
.havn-assert-warn-line { background: color-mix(in srgb, var(--havn-yellow) 12%, transparent); }
.havn-assert-fail-after { color: var(--havn-red); font-style: italic; opacity: .9; }
.havn-assert-warn-after { color: var(--havn-yellow); font-style: italic; opacity: .9; }
`;

function useAssertionDecorations(editor, content, checks) {
  const collectionRef = useRef(null);
  useEffect(() => {
    if (!editor || typeof editor.createDecorationsCollection !== "function") return;
    // Switching to a non-model file and back remounts Monaco, so for a render
    // this can still be the disposed instance; it has no model then.
    if (!editor.getModel?.() && editor.getModel) return;
    const decos = [];
    for (const c of checks || []) {
      if (c.passed !== false) continue;
      const line = findAssertionLine(content, c.expression);
      if (!line) continue;
      const kind = c.severity === "warn" ? "warn" : "fail";
      // Anchor at the end of the line so the injected text lands after the code.
      const endCol = editor.getModel?.()?.getLineMaxColumn?.(line) ?? 1;
      const detail = c.detail || "assertion failed";
      const range = { startLineNumber: line, startColumn: endCol, endLineNumber: line, endColumn: endCol };
      // Two decorations: Monaco drops injected text on a whole-line one.
      decos.push({
        range,
        options: {
          isWholeLine: true,
          className: `havn-assert-${kind}-line`,
          hoverMessage: { value: `**${kind === "warn" ? "Warning" : "Assertion failed"}** in the last build: ${detail}` },
        },
      });
      decos.push({
        range,
        options: {
          showIfCollapsed: true,
          after: { content: `   ${kind === "warn" ? "!" : "✗"} ${detail}`, inlineClassName: `havn-assert-${kind}-after` },
        },
      });
    }
    try {
      if (!collectionRef.current) collectionRef.current = editor.createDecorationsCollection([]);
      collectionRef.current.set(decos);
    } catch {
      // A disposed editor throws; the next mount decorates the new one.
      collectionRef.current = null;
    }
  }, [editor, content, checks]);
  useEffect(() => () => {
    try { collectionRef.current?.clear(); } catch { /* editor already disposed */ }
    collectionRef.current = null;
  }, [editor]);
}

/* ------------------------------------------------------------------ */
/* Component                                                           */
/* ------------------------------------------------------------------ */

export default function ModelWorkbench({
  children,
  activeFile,
  content,
  dirty,
  running,
  editor,
  preview,
  previewError,
  previewRunning,
  previewLabel,
  onPreview,
  onPreviewSql,
  onClearPreview,
  onSave,
  onBuild,
  onBuildDownstream,
  onOpenFile,
  onOpenDag,
  onEditContent,
}) {
  const [data, setData] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [tab, setTab] = useState("preview");
  const [inspectorOpen, setInspectorOpen] = useState(() => safeGetItem(INSPECTOR_KEY) !== "0");
  const [width, onResize, onResizeStart] = useResizable("havn_workbench_inspector_width", 440, 280, 900);

  // Drop responses for a file the user has already switched away from.
  const fileRef = useRef(activeFile);
  fileRef.current = activeFile;
  const load = useCallback(async () => {
    if (!activeFile) return;
    try {
      const d = await api.getModelWorkbench(activeFile);
      if (fileRef.current !== activeFile) return;
      setData(d);
      setLoadError(null);
    } catch (e) {
      if (fileRef.current !== activeFile) return;
      setData(null);
      setLoadError(e.message);
    }
  }, [activeFile]);

  // Reload on file switch, when a save lands (dirty -> clean), and when a
  // run finishes (running -> idle).
  useEffect(() => { setData(null); setLoadError(null); load(); }, [load]);
  const prevDirty = useRef(dirty);
  const prevRunning = useRef(running);
  useEffect(() => {
    if ((prevDirty.current && !dirty) || (prevRunning.current && !running)) load();
    prevDirty.current = dirty;
    prevRunning.current = running;
  }, [dirty, running, load]);

  // A new preview (from ⌘↵ or "Show rows") brings the Preview tab forward.
  useEffect(() => {
    if (previewRunning) {
      setTab("preview");
      setInspectorOpen(true);
    }
  }, [previewRunning]);

  const toggleInspector = () => {
    setInspectorOpen((v) => {
      safeSetItem(INSPECTOR_KEY, v ? "0" : "1");
      return !v;
    });
  };

  const checks = data?.checks || [];
  useAssertionDecorations(editor, content, checks);

  const failedCount = checks.filter((c) => c.passed === false).length;
  const status = buildStatus(data, dirty);
  const downstreamN = data?.downstream_all?.length || 0;
  const summary = checkSummary(checks);

  function goToLine(line) {
    if (!editor || !line) return;
    editor.revealLineInCenter(line);
    editor.setPosition({ lineNumber: line, column: 1 });
    editor.focus();
  }

  function addColDoc(name) {
    const { text, line } = insertColDoc(content, name);
    onEditContent?.(text);
    // Let the editor pick up the new value before moving the cursor.
    setTimeout(() => {
      if (!editor) return;
      const col = `@col ${name}: `.length + 1;
      editor.revealLineInCenter(line);
      editor.setPosition({ lineNumber: line, column: col });
      editor.focus();
    }, 0);
  }

  return (
    <div style={s.root}>
      <style>{DECORATION_CSS}</style>

      {/* Lineage strip */}
      <div style={s.lineageBar}>
      <div style={s.lineage} aria-label="Model lineage">
        <span style={s.lineageLabel}>Lineage</span>
        {data?.upstream?.length ? data.upstream.map((u) => (
          <Chip key={u.name} name={u.name} onClick={u.path ? () => onOpenFile(u.path) : undefined}
                title={u.path ? `Open ${u.path}` : `${u.name} (source table)`} />
        )) : data && <span style={s.dim}>no upstream models</span>}
        <span style={s.arrow} aria-hidden="true">→</span>
        <span style={{ ...s.chip, ...s.chipCurrent }} title={data?.description || data?.model}>
          <Dot tone={summary === "fail" ? "bad" : summary === "warn" ? "warn" : summary === "pass" ? "ok" : "dim"} />
          {data?.model || activeFile}
        </span>
        <span style={s.arrow} aria-hidden="true">→</span>
        {data?.downstream?.length ? data.downstream.map((d) => (
          <Chip key={d.name} name={d.name} dashed onClick={d.path ? () => onOpenFile(d.path) : undefined}
                title={`Rebuilt by "Build + downstream". Open ${d.path || d.name}`} />
        )) : data && <span style={s.dim}>nothing downstream</span>}
        {downstreamN > (data?.downstream?.length || 0) && (
          <span style={s.dim}>+{downstreamN - data.downstream.length} further</span>
        )}
      </div>
        <span style={{ display: "flex", gap: 12, flexShrink: 0, paddingLeft: 8 }}>
          <button style={s.link} onClick={onOpenDag}>Open DAG</button>
          <button style={s.link} onClick={toggleInspector} aria-expanded={inspectorOpen} aria-controls="havn-workbench-inspector">
            {inspectorOpen ? "Hide inspector" : "Show inspector"}
          </button>
        </span>
      </div>

      {/* Editor + inspector */}
      <div style={s.split}>
        <div style={s.editorPane}>{children}</div>
        {inspectorOpen && (
          <>
            <ResizeHandle direction="horizontal" onResize={(d) => onResize(-d)} onResizeStart={onResizeStart} />
            <section id="havn-workbench-inspector" style={{ ...s.inspector, width }} aria-label="Model inspector">
              <div role="tablist" style={s.tabs}>
                {TABS.map((t) => (
                  <button
                    key={t}
                    role="tab"
                    aria-selected={tab === t}
                    onClick={() => setTab(t)}
                    style={tab === t ? { ...s.tab, ...s.tabOn } : s.tab}
                  >
                    {TAB_LABELS[t]}
                    {t === "checks" && checks.length > 0 && (
                      <span style={{ ...s.count, color: summary === "fail" ? "var(--havn-red)" : summary === "warn" ? "var(--havn-yellow)" : "var(--havn-text-dim)" }}>
                        {failedCount ? `${failedCount} failing` : checks.length}
                      </span>
                    )}
                    {t === "columns" && data?.columns?.length > 0 && <span style={s.count}>{data.columns.length}</span>}
                  </button>
                ))}
              </div>
              <div role="tabpanel" style={s.tabBody}>
                {tab === "preview" && (
                  <PreviewTab preview={preview} error={previewError} running={previewRunning} label={previewLabel}
                              onPreview={onPreview} onClear={onClearPreview} />
                )}
                {tab !== "preview" && loadError && <div style={s.empty}>{loadError}</div>}
                {tab === "checks" && data && (
                  <ChecksTab checks={checks} content={content} built={data.state?.built}
                             onShowRows={(c) => onPreviewSql(c.failing_sql, `Failing rows · ${c.expression}`)}
                             onGoTo={(c) => goToLine(findAssertionLine(content, c.expression))} />
                )}
                {tab === "columns" && data && <ColumnsTab columns={data.columns} onAddDoc={addColDoc} />}
                {tab === "runs" && data && <RunsTab runs={data.runs} />}
              </div>
            </section>
          </>
        )}
      </div>

      {/* Action bar */}
      <div style={s.actions}>
        <span style={s.status} title={status.text}>
          <Dot tone={status.tone} />
          <span style={s.ellipsis}>{status.text}</span>
        </span>
        <button style={s.btn} onClick={onSave} disabled={!dirty}>Save <kbd style={s.kbd}>⌘S</kbd></button>
        <button style={s.btn} onClick={onPreview} disabled={previewRunning}>Preview <kbd style={s.kbd}>⌘↵</kbd></button>
        <button style={s.btn} onClick={() => onBuild(data?.model)} disabled={running}
                title={running ? "A run is already in progress" : "Save and build only this model"}>
          Build model
        </button>
        <button style={s.btnPrimary} onClick={() => data && onBuildDownstream(`${data.model}+`)}
                disabled={running || !data}
                title={running ? "A run is already in progress"
                  : downstreamN ? `Save, then build this model and: ${data.downstream_all.join(", ")}` : "Save and build this model"}>
          {downstreamN ? `Build + downstream (${downstreamN})` : "Build + downstream"}
        </button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Pieces                                                              */
/* ------------------------------------------------------------------ */

function Dot({ tone }) {
  const color = { ok: "var(--havn-green)", warn: "var(--havn-yellow)", bad: "var(--havn-red)" }[tone] || "var(--havn-text-dim)";
  return <span aria-hidden="true" style={{ width: 7, height: 7, borderRadius: "50%", background: color, flexShrink: 0, display: "inline-block" }} />;
}

function Chip({ name, dashed, onClick, title }) {
  const style = { ...s.chip, ...(dashed ? s.chipDashed : null), cursor: onClick ? "pointer" : "default" };
  return onClick
    ? <button type="button" style={style} onClick={onClick} title={title}>{name}</button>
    : <span style={style} title={title}>{name}</span>;
}

function PreviewTab({ preview, error, running, label, onPreview, onClear }) {
  if (running) return <div style={s.empty}>Running…</div>;
  if (error) {
    return (
      <div>
        <PreviewHeader label={label} onClear={onClear} />
        <div style={s.error}>{error}</div>
      </div>
    );
  }
  if (!preview) {
    return (
      <div style={s.empty}>
        <div>Preview runs the SQL in the editor, including unsaved changes, against the active environment.</div>
        <button style={{ ...s.btn, marginTop: 10 }} onClick={onPreview}>Preview <kbd style={s.kbd}>⌘↵</kbd></button>
      </div>
    );
  }
  const n = preview.rows.length;
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0 }}>
      <PreviewHeader
        label={label}
        meta={`${n.toLocaleString()} row${n === 1 ? "" : "s"}${preview.truncated ? " (limited)" : ""} · ${preview.columns.length} col${preview.columns.length === 1 ? "" : "s"}`}
        onClear={onClear}
      />
      <div style={{ flex: 1, overflow: "auto", minHeight: 0 }}>
        <SortableTable columns={preview.columns} rows={preview.rows} />
      </div>
    </div>
  );
}

function PreviewHeader({ label, meta, onClear }) {
  return (
    <div style={s.previewHead}>
      <span style={s.ellipsis}><b style={{ fontWeight: 500, color: "var(--havn-text)" }}>{label || "Preview"}</b>{meta ? ` · ${meta}` : ""}</span>
      <button onClick={onClear} style={s.link} aria-label="Clear preview">Clear</button>
    </div>
  );
}

function ChecksTab({ checks, content, built, onShowRows, onGoTo }) {
  if (!checks.length) {
    return (
      <div style={s.empty}>
        No checks on this model yet. Add one under <code style={s.code}>@config</code>, for example
        <pre style={s.pre}>@assert no_nulls(id){"\n"}@assert unique(id){"\n"}@assert amount &gt;= 0</pre>
      </div>
    );
  }
  return (
    <div>
      {!built && <div style={{ ...s.dim, padding: "4px 0 8px" }}>Not built yet. Results appear after the first build.</div>}
      {checks.map((c) => {
        const icon = c.passed === true ? "✓" : c.passed === false ? (c.severity === "warn" ? "!" : "✗") : "○";
        const color = c.passed === true ? "var(--havn-green)" : c.passed === false
          ? (c.severity === "warn" ? "var(--havn-yellow)" : "var(--havn-red)") : "var(--havn-text-dim)";
        const line = findAssertionLine(content, c.expression);
        return (
          <div key={c.expression} style={s.checkRow}>
            <span style={{ color, fontWeight: 600, width: 16, textAlign: "center" }}
                  aria-label={c.passed === true ? "passed" : c.passed === false ? "failed" : "not run"}>{icon}</span>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div style={s.mono}>{c.expression}{c.severity === "warn" && <span style={s.badge}>warn</span>}</div>
              <div style={s.dim}>
                {c.passed == null ? "Not run since this check was added" : c.detail}
                {c.checked_at && ` · ${timeAgo(c.checked_at)}`}
              </div>
            </div>
            <span style={{ display: "flex", gap: 8, flexShrink: 0 }}>
              {line > 0 && <button style={s.link} onClick={() => onGoTo(c)}>Line {line}</button>}
              {c.passed === false && c.failing_sql && <button style={s.link} onClick={() => onShowRows(c)}>Show rows</button>}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function ColumnsTab({ columns, onAddDoc }) {
  if (!columns?.length) return <div style={s.empty}>No columns known yet. Build the model to record its schema.</div>;
  const missing = columns.filter((c) => !c.description).length;
  return (
    <div>
      {missing > 0 && <div style={{ ...s.dim, padding: "4px 0 8px" }}>{missing} of {columns.length} columns undocumented</div>}
      {columns.map((c) => (
        <div key={c.name} style={s.colRow}>
          <span style={{ ...s.mono, color: "var(--havn-text)" }}>{c.name}</span>
          <span style={{ ...s.mono, color: "var(--havn-purple)", fontSize: 11 }}>{c.type || "not built"}</span>
          {c.description
            ? <span style={{ color: "var(--havn-text-secondary)" }}>{c.description}</span>
            : <button style={{ ...s.link, textAlign: "left" }} onClick={() => onAddDoc(c.name)}>+ add @col</button>}
        </div>
      ))}
    </div>
  );
}

function RunsTab({ runs }) {
  if (!runs?.length) return <div style={s.empty}>This model has not been built yet.</div>;
  return (
    <div>
      {runs.map((r, i) => {
        const ok = r.status === "success";
        const skipped = r.status === "skipped";
        return (
          <div key={i} style={s.checkRow}>
            <span style={{ width: 16, textAlign: "center", fontWeight: 600, color: ok ? "var(--havn-green)" : skipped ? "var(--havn-text-dim)" : "var(--havn-red)" }}>
              {ok ? "✓" : skipped ? "–" : "✗"}
            </span>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div>{r.status}<span style={s.dim}> · {timeAgo(r.started_at)}</span></div>
              <div style={s.dim}>
                {[fmtMs(r.duration_ms), r.rows_affected != null && !skipped ? `${Number(r.rows_affected).toLocaleString()} rows` : null].filter(Boolean).join(" · ")}
              </div>
              {r.error && <div style={{ ...s.error, padding: "4px 0 0" }}>{r.error}</div>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Styles                                                              */
/* ------------------------------------------------------------------ */

const btnBase = {
  padding: "4px 12px",
  fontSize: "12px",
  borderRadius: "var(--havn-radius)",
  border: "1px solid var(--havn-btn-border)",
  background: "var(--havn-btn-bg)",
  color: "var(--havn-text)",
  cursor: "pointer",
  whiteSpace: "nowrap",
  fontFamily: "inherit",
};

const s = {
  root: { display: "flex", flexDirection: "column", height: "100%", minHeight: 0 },
  lineageBar: {
    display: "flex", alignItems: "center", padding: "6px 12px",
    borderBottom: "1px solid var(--havn-border)", flexShrink: 0, fontSize: 12,
  },
  lineage: { display: "flex", alignItems: "center", gap: 6, overflowX: "auto", flex: 1, minWidth: 0 },
  lineageLabel: { fontSize: 10, textTransform: "uppercase", letterSpacing: ".06em", color: "var(--havn-text-dim)", marginRight: 2, flexShrink: 0 },
  chip: {
    display: "inline-flex", alignItems: "center", gap: 6, padding: "2px 9px", borderRadius: 20,
    border: "1px solid var(--havn-border-light)", background: "transparent", color: "var(--havn-text-secondary)",
    fontFamily: "var(--havn-font-mono)", fontSize: 11.5, whiteSpace: "nowrap", flexShrink: 0,
  },
  chipCurrent: { borderColor: "var(--havn-accent)", color: "var(--havn-accent)", fontWeight: 500 },
  chipDashed: { borderStyle: "dashed" },
  arrow: { color: "var(--havn-text-dim)", flexShrink: 0 },
  dim: { color: "var(--havn-text-secondary)", fontSize: 12 },
  link: { background: "none", border: "none", padding: 0, color: "var(--havn-accent)", cursor: "pointer", fontSize: 12, fontFamily: "inherit", whiteSpace: "nowrap" },
  split: { display: "flex", flex: 1, minHeight: 0 },
  editorPane: { flex: 1, minWidth: 0, display: "flex", flexDirection: "column" },
  inspector: { display: "flex", flexDirection: "column", minHeight: 0, flexShrink: 0, borderLeft: "1px solid var(--havn-border)", background: "var(--havn-bg)" },
  tabs: { display: "flex", gap: 2, padding: "0 8px", borderBottom: "1px solid var(--havn-border)", flexShrink: 0 },
  tab: {
    background: "none", border: "none", borderBottomWidth: 2, borderBottomStyle: "solid", borderBottomColor: "transparent", padding: "8px 8px",
    fontSize: 12, color: "var(--havn-text-secondary)", cursor: "pointer", fontFamily: "inherit", whiteSpace: "nowrap",
  },
  tabOn: { color: "var(--havn-text)", borderBottomColor: "var(--havn-accent)" },
  count: { marginLeft: 5, fontFamily: "var(--havn-font-mono)", fontSize: 10.5, color: "var(--havn-text-dim)" },
  tabBody: { flex: 1, overflow: "auto", padding: "8px 12px", minHeight: 0, display: "flex", flexDirection: "column" },
  empty: { color: "var(--havn-text-secondary)", fontSize: 12, padding: "16px 4px", lineHeight: 1.5 },
  error: { padding: "8px 0", color: "var(--havn-red)", fontFamily: "var(--havn-font-mono)", fontSize: 12, whiteSpace: "pre-wrap" },
  previewHead: {
    display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8,
    fontSize: 12, color: "var(--havn-text-secondary)", padding: "2px 0 8px", flexShrink: 0,
  },
  checkRow: { display: "flex", gap: 8, alignItems: "flex-start", padding: "8px 0", borderBottom: "1px solid var(--havn-border)", fontSize: 12.5 },
  colRow: {
    display: "grid", gridTemplateColumns: "minmax(0, 1.1fr) minmax(0, .7fr) minmax(0, 1.4fr)", gap: 10,
    padding: "6px 0", borderBottom: "1px solid var(--havn-border)", fontSize: 12, alignItems: "baseline",
  },
  mono: { fontFamily: "var(--havn-font-mono)", fontSize: 12, overflowWrap: "anywhere" },
  badge: { marginLeft: 6, fontSize: 10, padding: "0 5px", borderRadius: 4, border: "1px solid var(--havn-yellow)", color: "var(--havn-yellow)", fontFamily: "var(--havn-font)" },
  code: { fontFamily: "var(--havn-font-mono)" },
  pre: { fontFamily: "var(--havn-font-mono)", fontSize: 11.5, background: "var(--havn-bg-secondary)", padding: "8px 10px", borderRadius: "var(--havn-radius)", marginTop: 8 },
  actions: {
    display: "flex", alignItems: "center", gap: 8, padding: "6px 12px", flexShrink: 0,
    borderTop: "1px solid var(--havn-border)", background: "var(--havn-bg-tertiary)", flexWrap: "wrap",
  },
  status: { display: "flex", alignItems: "center", gap: 8, marginRight: "auto", fontSize: 12, color: "var(--havn-text-secondary)", minWidth: 0, flex: "1 1 200px" },
  ellipsis: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0 },
  btn: btnBase,
  btnPrimary: { ...btnBase, background: "var(--havn-accent)", borderColor: "var(--havn-accent)", color: "#fff", fontWeight: 500 },
  kbd: { fontFamily: "var(--havn-font-mono)", fontSize: 10, opacity: 0.7, marginLeft: 4 },
};
