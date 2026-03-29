import React, { useState, useEffect, useCallback, useRef } from "react";
import MonacoEditor from "@monaco-editor/react";
import { api } from "./api";

const LINE_HEIGHT = 19;
const MIN_CELL_LINES = 3;
const MAX_CELL_LINES = 40;

function CodeArea({ value, onChange, onKeyDown, minLines = 3, language = "plaintext" }) {
  const lineCount = (value || "").split("\n").length;
  const editorLines = Math.max(minLines || MIN_CELL_LINES, Math.min(lineCount + 1, MAX_CELL_LINES));
  const [height, setHeight] = useState(editorLines * LINE_HEIGHT + 8);

  // Update height when value changes (recompute from line count)
  useEffect(() => {
    const lc = (value || "").split("\n").length;
    const lines = Math.max(minLines || MIN_CELL_LINES, Math.min(lc + 1, MAX_CELL_LINES));
    setHeight(lines * LINE_HEIGHT + 8);
  }, [value, minLines]);

  const handleMount = (editor, monaco) => {
    // Forward Ctrl+Enter to parent handler
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => {
      if (onKeyDown) {
        onKeyDown({ preventDefault: () => {}, ctrlKey: true, metaKey: true, key: "Enter" });
      }
    });
    // Auto-resize based on content (accounts for word wrap)
    const updateHeight = () => {
      const contentHeight = editor.getContentHeight();
      const newHeight = Math.max((minLines || MIN_CELL_LINES) * LINE_HEIGHT + 8, Math.min(contentHeight + 8, MAX_CELL_LINES * LINE_HEIGHT));
      setHeight(newHeight);
    };
    editor.onDidContentSizeChange(updateHeight);
    updateHeight();
    editor.updateOptions({ scrollbar: { vertical: "hidden", horizontal: "auto", alwaysConsumeMouseWheel: false } });
  };

  return (
    <div style={{ height, border: "1px solid var(--havn-border)", borderRadius: 4, overflow: "hidden" }}>
      <MonacoEditor
        height={height}
        language={language}
        value={value}
        onChange={(val) => {
          if (onChange) {
            // Mimic textarea onChange event shape
            onChange({ target: { value: val || "" } });
          }
        }}
        onMount={handleMount}
        theme="vs-dark"
        options={{
          minimap: { enabled: false },
          lineNumbers: "on",
          lineNumbersMinChars: 3,
          fontSize: 13,
          lineHeight: LINE_HEIGHT,
          scrollBeyondLastLine: false,
          wordWrap: "on",
          automaticLayout: true,
          renderLineHighlight: "none",
          overviewRulerLanes: 0,
          hideCursorInOverviewRuler: true,
          folding: false,
          glyphMargin: false,
          padding: { top: 4, bottom: 4 },
          tabSize: 2,
          scrollbar: { vertical: "hidden", horizontal: "auto", alwaysConsumeMouseWheel: false },
        }}
      />
    </div>
  );
}

function CellInsertButton({ onInsert }) {
  const [open, setOpen] = useState(false);
  const types = [
    { type: "sql", label: "SQL" },
    { type: "code", label: "Python" },
    { type: "markdown", label: "Markdown" },
    { type: "ingest", label: "Ingest" },
  ];
  return (
    <div style={{ display: "flex", justifyContent: "center", height: 20, alignItems: "center", position: "relative" }}>
      <button
        onClick={() => setOpen(!open)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        style={{
          background: "none", border: "1px solid transparent", color: "var(--havn-text-dim)",
          cursor: "pointer", fontSize: 14, padding: "0 8px", borderRadius: 4, lineHeight: 1,
          opacity: open ? 1 : 0.3, transition: "opacity 0.15s",
        }}
        onMouseEnter={(e) => e.target.style.opacity = 1}
        onMouseLeave={(e) => { if (!open) e.target.style.opacity = 0.3; }}
        title="Insert cell"
      >+</button>
      {open && (
        <div style={{
          position: "absolute", top: 20, zIndex: 10,
          display: "flex", gap: 2, background: "var(--havn-bg)", border: "1px solid var(--havn-border)",
          borderRadius: 6, padding: 4, boxShadow: "0 2px 8px rgba(0,0,0,0.3)",
        }}>
          {types.map(t => (
            <button
              key={t.type}
              onClick={() => { onInsert(t.type); setOpen(false); }}
              style={{
                background: "none", border: "1px solid var(--havn-border)", borderRadius: 4,
                color: "var(--havn-text)", cursor: "pointer", fontSize: 11, padding: "3px 8px",
              }}
            >{t.label}</button>
          ))}
        </div>
      )}
    </div>
  );
}

const SOURCE_TYPES = [
  { value: "csv", label: "CSV File" },
  { value: "parquet", label: "Parquet File" },
  { value: "json", label: "JSON File" },
  { value: "url", label: "URL" },
  { value: "database", label: "Database Connection" },
];

const DEFAULT_INGEST = {
  source_type: "csv",
  source_path: "",
  target_schema: "landing",
  target_table: "",
  connection: "",
  options: {},
};

function CellOutput({ outputs }) {
  if (!outputs || outputs.length === 0) return null;
  return (
    <div style={cs.outputArea}>
      {outputs.map((out, i) => {
        if (out.type === "table") {
          return (
            <div key={i} style={cs.tableWrap}>
              <table style={cs.table}>
                <thead>
                  <tr>
                    {out.columns.map((col, j) => (
                      <th key={j} style={cs.th}>{col}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {out.rows.map((row, ri) => (
                    <tr key={ri}>
                      {row.map((v, ci) => (
                        <td key={ci} style={cs.td}>
                          {v === null ? <span style={cs.null}>NULL</span> : String(v)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
              {out.truncated && (
                <div style={cs.truncated}>Results truncated ({out.total_rows} rows shown)</div>
              )}
            </div>
          );
        }
        if (out.type === "error") {
          return <pre key={i} style={cs.error}>{out.text}</pre>;
        }
        return <pre key={i} style={cs.text}>{out.text}</pre>;
      })}
    </div>
  );
}

// --- Ingest cell form editor ---

function IngestCellEditor({ cell, notebookName, onUpdate, onDelete, externalRunning }) {
  // Parse existing source JSON or use defaults
  const parseSource = (src) => {
    if (!src) return { ...DEFAULT_INGEST };
    try {
      return { ...DEFAULT_INGEST, ...JSON.parse(src) };
    } catch {
      return { ...DEFAULT_INGEST };
    }
  };

  const [spec, setSpec] = useState(() => parseSource(cell.source));
  const [showSource, setShowSource] = useState(false);
  const [rawSource, setRawSource] = useState(cell.source || JSON.stringify(DEFAULT_INGEST, null, 2));
  const [outputs, setOutputs] = useState(cell.outputs || []);
  const [running, setRunning] = useState(false);
  const [duration, setDuration] = useState(cell.duration_ms);
  const [connections, setConnections] = useState([]);
  const [showOptions, setShowOptions] = useState(false);
  const isRunning = running || externalRunning;

  useEffect(() => {
    setOutputs(cell.outputs || []);
    setDuration(cell.duration_ms);
  }, [cell.outputs, cell.duration_ms]);

  useEffect(() => {
    const parsed = parseSource(cell.source);
    setSpec(parsed);
    setRawSource(cell.source || JSON.stringify(DEFAULT_INGEST, null, 2));
  }, [cell.id]);

  // Fetch connections when database type is selected
  useEffect(() => {
    if (spec.source_type === "database") {
      api.listConfiguredConnectors().then(setConnections).catch(() => {});
    }
  }, [spec.source_type]);

  const updateSpec = useCallback((updates) => {
    setSpec((prev) => {
      const next = { ...prev, ...updates };
      const json = JSON.stringify(next);
      setRawSource(JSON.stringify(next, null, 2));
      onUpdate({ ...cell, source: json });
      return next;
    });
  }, [cell, onUpdate]);

  // Auto-populate table name from file path
  function handlePathChange(path) {
    const updates = { source_path: path };
    if (!spec.target_table || spec.target_table === fileNameToTable(spec.source_path)) {
      updates.target_table = fileNameToTable(path);
    }
    updateSpec(updates);
  }

  function fileNameToTable(path) {
    if (!path) return "";
    const name = path.split("/").pop().split("\\").pop();
    return name.replace(/\.[^.]+$/, "").replace(/[^a-zA-Z0-9_]/g, "_").toLowerCase();
  }

  async function runCell() {
    setRunning(true);
    try {
      const json = showSource ? rawSource : JSON.stringify(spec);
      const result = await api.runCell(notebookName, json, { cell_type: "ingest" });
      setOutputs(result.outputs);
      setDuration(result.duration_ms);
      onUpdate({ ...cell, source: json, outputs: result.outputs, duration_ms: result.duration_ms });
    } catch (e) {
      setOutputs([{ type: "error", text: e.message }]);
    }
    setRunning(false);
  }

  function handleRawSourceChange(val) {
    setRawSource(val);
    try {
      const parsed = JSON.parse(val);
      setSpec({ ...DEFAULT_INGEST, ...parsed });
      onUpdate({ ...cell, source: val });
    } catch {
      // Invalid JSON — just update raw, don't sync spec
    }
  }

  const isFile = ["csv", "parquet", "json"].includes(spec.source_type);
  const isUrl = spec.source_type === "url";
  const isDb = spec.source_type === "database";

  return (
    <div style={cs.codeCell}>
      <div style={cs.cellHeader}>
        <button onClick={runCell} disabled={isRunning} style={cs.runBtn}>
          {isRunning ? "..." : "\u25B6"}
        </button>
        <span style={{ ...cs.cellType, color: "var(--havn-blue, var(--havn-text-dim))" }}>INGEST</span>
        <span style={{ flex: 1 }} />
        <button
          onClick={() => setShowSource(!showSource)}
          style={cs.toggleBtn}
          title={showSource ? "Show form" : "Show source"}
        >
          {showSource ? "Form" : "{ }"}
        </button>
        {duration != null && <span style={cs.duration}>{duration}ms</span>}
        <button data-havn-danger="" onClick={onDelete} style={cs.deleteBtn} title="Delete cell">&times;</button>
      </div>

      {showSource ? (
        <CodeArea
          value={rawSource}
          onChange={(e) => handleRawSourceChange(e.target.value)}
          onKeyDown={(e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); runCell(); }
          }}
          minLines={4}
        />
      ) : (
        <div style={ig.form}>
          {/* Source Type */}
          <div style={ig.row}>
            <label style={ig.label}>Source</label>
            <select
              value={spec.source_type}
              onChange={(e) => updateSpec({ source_type: e.target.value, connection: "", source_path: "" })}
              style={ig.select}
            >
              {SOURCE_TYPES.map((t) => (
                <option key={t.value} value={t.value}>{t.label}</option>
              ))}
            </select>
          </div>

          {/* File path for CSV/Parquet/JSON */}
          {isFile && (
            <div style={ig.row}>
              <label style={ig.label}>File Path</label>
              <input
                value={spec.source_path}
                onChange={(e) => handlePathChange(e.target.value)}
                placeholder="data/earthquakes.csv"
                style={ig.input}
              />
            </div>
          )}

          {/* URL input */}
          {isUrl && (
            <div style={ig.row}>
              <label style={ig.label}>URL</label>
              <input
                value={spec.source_path}
                onChange={(e) => handlePathChange(e.target.value)}
                placeholder="https://example.com/data.csv"
                style={ig.input}
              />
            </div>
          )}

          {/* Database connection picker */}
          {isDb && (
            <div style={ig.row}>
              <label style={ig.label}>Connection</label>
              <select
                value={spec.connection}
                onChange={(e) => updateSpec({ connection: e.target.value })}
                style={ig.select}
              >
                <option value="">Select connection...</option>
                {connections.map((c) => (
                  <option key={c.name} value={c.name}>{c.name} ({c.type})</option>
                ))}
              </select>
            </div>
          )}
          {isDb && (
            <div style={ig.row}>
              <label style={ig.label}>Source Table</label>
              <input
                value={spec.source_path}
                onChange={(e) => handlePathChange(e.target.value)}
                placeholder="public.users"
                style={ig.input}
              />
            </div>
          )}

          {/* Target schema + table */}
          <div style={ig.row}>
            <label style={ig.label}>Target</label>
            <div style={ig.targetRow}>
              <input
                value={spec.target_schema}
                onChange={(e) => updateSpec({ target_schema: e.target.value })}
                placeholder="landing"
                style={{ ...ig.input, flex: "0 0 120px" }}
              />
              <span style={ig.dot}>.</span>
              <input
                value={spec.target_table}
                onChange={(e) => updateSpec({ target_table: e.target.value })}
                placeholder="table_name"
                style={ig.input}
              />
            </div>
          </div>

          {/* Advanced options (collapsible) */}
          <div style={ig.row}>
            <button onClick={() => setShowOptions(!showOptions)} style={ig.optionsToggle}>
              {showOptions ? "\u25BC" : "\u25B6"} Options
            </button>
          </div>
          {showOptions && (
            <div style={ig.optionsArea}>
              <textarea
                value={JSON.stringify(spec.options || {}, null, 2)}
                onChange={(e) => {
                  try {
                    updateSpec({ options: JSON.parse(e.target.value) });
                  } catch { /* ignore invalid JSON while typing */ }
                }}
                placeholder='{"delimiter": ",", "header": true}'
                style={{ ...cs.codeInput, minHeight: "48px" }}
                rows={3}
                spellCheck={false}
              />
            </div>
          )}
        </div>
      )}

      <CellOutput outputs={outputs} />
    </div>
  );
}

// --- SQL cell ---

function SqlCell({ cell, notebookName, onUpdate, onDelete, externalRunning }) {
  const [source, setSource] = useState(cell.source || "");
  const [outputs, setOutputs] = useState(cell.outputs || []);
  const [running, setRunning] = useState(false);
  const [duration, setDuration] = useState(cell.duration_ms);
  const isRunning = running || externalRunning;

  useEffect(() => {
    setOutputs(cell.outputs || []);
    setDuration(cell.duration_ms);
  }, [cell.outputs, cell.duration_ms]);

  useEffect(() => { setSource(cell.source || ""); }, [cell.id]);

  async function runCell() {
    setRunning(true);
    try {
      const result = await api.runCell(notebookName, source, { cell_type: "sql" });
      setOutputs(result.outputs);
      setDuration(result.duration_ms);
      onUpdate({ ...cell, source, outputs: result.outputs, duration_ms: result.duration_ms });
    } catch (e) {
      setOutputs([{ type: "error", text: e.message }]);
    }
    setRunning(false);
  }

  return (
    <div style={cs.codeCell}>
      <div style={cs.cellHeader}>
        <button onClick={runCell} disabled={isRunning} style={cs.runBtn}>
          {isRunning ? "..." : "\u25B6"}
        </button>
        <span style={cs.cellType}>SQL</span>
        <span style={{ flex: 1 }} />
        {duration != null && <span style={cs.duration}>{duration}ms</span>}
        <button data-havn-danger="" onClick={onDelete} style={cs.deleteBtn} title="Delete cell">&times;</button>
      </div>
      <CodeArea
        value={source}
        language="sql"
        onChange={(e) => {
          setSource(e.target.value);
          onUpdate({ ...cell, source: e.target.value });
        }}
        onKeyDown={(e) => {
          if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); runCell(); }
        }}
      />
      <CellOutput outputs={outputs} />
    </div>
  );
}

// --- Code cell ---

function CodeCell({ cell, notebookName, onUpdate, onDelete, externalRunning }) {
  const [source, setSource] = useState(cell.source || "");
  const [outputs, setOutputs] = useState(cell.outputs || []);
  const [running, setRunning] = useState(false);
  const [duration, setDuration] = useState(cell.duration_ms);
  const isRunning = running || externalRunning;

  useEffect(() => {
    setOutputs(cell.outputs || []);
    setDuration(cell.duration_ms);
  }, [cell.outputs, cell.duration_ms]);

  useEffect(() => { setSource(cell.source || ""); }, [cell.id]);

  async function runCell() {
    setRunning(true);
    try {
      const result = await api.runCell(notebookName, source, { cell_type: "code" });
      setOutputs(result.outputs);
      setDuration(result.duration_ms);
      onUpdate({ ...cell, source, outputs: result.outputs, duration_ms: result.duration_ms });
    } catch (e) {
      setOutputs([{ type: "error", text: e.message }]);
    }
    setRunning(false);
  }

  return (
    <div style={cs.codeCell}>
      <div style={cs.cellHeader}>
        <button onClick={runCell} disabled={isRunning} style={cs.runBtn}>
          {isRunning ? "..." : "\u25B6"}
        </button>
        <span style={cs.cellType}>PY</span>
        <span style={{ flex: 1 }} />
        {duration != null && <span style={cs.duration}>{duration}ms</span>}
        <button data-havn-danger="" onClick={onDelete} style={cs.deleteBtn} title="Delete cell">&times;</button>
      </div>
      <CodeArea
        value={source}
        language="python"
        onChange={(e) => {
          setSource(e.target.value);
          onUpdate({ ...cell, source: e.target.value });
        }}
        onKeyDown={(e) => {
          if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); runCell(); }
        }}
      />
      <CellOutput outputs={outputs} />
    </div>
  );
}

// --- Dispatcher ---

function NotebookCell({ cell, notebookName, onUpdate, onDelete, externalRunning }) {
  if (cell.type === "markdown") {
    return (
      <MarkdownCell cell={cell} onUpdate={onUpdate} onDelete={onDelete} />
    );
  }
  if (cell.type === "sql") {
    return (
      <SqlCell cell={cell} notebookName={notebookName} onUpdate={onUpdate} onDelete={onDelete} externalRunning={externalRunning} />
    );
  }
  if (cell.type === "ingest") {
    return (
      <IngestCellEditor cell={cell} notebookName={notebookName} onUpdate={onUpdate} onDelete={onDelete} externalRunning={externalRunning} />
    );
  }
  return (
    <CodeCell cell={cell} notebookName={notebookName} onUpdate={onUpdate} onDelete={onDelete} externalRunning={externalRunning} />
  );
}

function mdInlineFormat(text) {
  const parts = [];
  let remaining = text;
  let key = 0;
  const re = /(\*\*(.+?)\*\*)|(\*(.+?)\*)|(`(.+?)`)|(\[(.+?)\]\((.+?)\))/g;
  let lastIndex = 0;
  let match;
  while ((match = re.exec(remaining)) !== null) {
    if (match.index > lastIndex) parts.push(remaining.slice(lastIndex, match.index));
    if (match[2]) parts.push(<strong key={key++}>{match[2]}</strong>);
    else if (match[4]) parts.push(<em key={key++}>{match[4]}</em>);
    else if (match[6]) parts.push(<code key={key++} style={{ background: "var(--havn-bg-secondary)", padding: "1px 4px", borderRadius: 3, fontSize: "0.9em" }}>{match[6]}</code>);
    else if (match[8]) parts.push(<a key={key++} href={match[9]} target="_blank" rel="noopener noreferrer" style={{ color: "var(--havn-accent)" }}>{match[8]}</a>);
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < remaining.length) parts.push(remaining.slice(lastIndex));
  return parts.length > 0 ? parts : text;
}

function renderCellMarkdown(text) {
  if (!text) return null;
  const lines = text.split("\n");
  const elements = [];
  let listItems = [];
  function flushList() {
    if (listItems.length > 0) {
      elements.push(<ul key={`list-${elements.length}`} style={{ margin: "4px 0", paddingLeft: 20 }}>{listItems}</ul>);
      listItems = [];
    }
  }
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const hm = line.match(/^(#{1,3})\s+(.*)$/);
    if (hm) {
      flushList();
      const level = hm[1].length;
      const Tag = `h${level}`;
      elements.push(<Tag key={i} style={{ margin: "8px 0 4px", fontSize: level === 1 ? "1.4em" : level === 2 ? "1.15em" : "1em", fontWeight: 600 }}>{mdInlineFormat(hm[2])}</Tag>);
      continue;
    }
    const li = line.match(/^[-*]\s+(.*)$/);
    if (li) { listItems.push(<li key={i}>{mdInlineFormat(li[1])}</li>); continue; }
    flushList();
    if (line.trim() === "") elements.push(<br key={i} />);
    else elements.push(<p key={i} style={{ margin: "4px 0", lineHeight: 1.6 }}>{mdInlineFormat(line)}</p>);
  }
  flushList();
  return elements;
}

function MarkdownCell({ cell, onUpdate, onDelete }) {
  const [source, setSource] = useState(cell.source || "");
  const [editing, setEditing] = useState(false);
  useEffect(() => { setSource(cell.source || ""); }, [cell.id]);

  if (!editing) {
    // Rendered mode: no container chrome, just the content
    return (
      <div
        style={{ position: "relative", flex: 1, minWidth: 0 }}
        onDoubleClick={() => setEditing(true)}
      >
        <div style={{ position: "absolute", top: 4, right: 4, display: "flex", gap: 4, opacity: 0.3, transition: "opacity 0.15s" }}
          onMouseEnter={(e) => e.currentTarget.style.opacity = 1}
          onMouseLeave={(e) => e.currentTarget.style.opacity = 0.3}
        >
          <button onClick={(e) => { e.stopPropagation(); setEditing(true); }}
            style={{ background: "none", border: "none", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: 11, padding: "2px 6px" }}>Edit</button>
          <button data-havn-danger="" onClick={(e) => { e.stopPropagation(); onDelete(); }}
            style={{ background: "none", border: "none", color: "var(--havn-text-dim)", cursor: "pointer", fontSize: 14, padding: "0 4px" }}>&times;</button>
        </div>
        <div style={{ padding: "4px 0", minHeight: 24, cursor: "text", color: "var(--havn-text)", fontSize: 14, lineHeight: 1.6 }}>
          {source.trim() ? renderCellMarkdown(source) : (
            <span style={{ color: "var(--havn-text-dim)", fontStyle: "italic" }}>Double-click to edit...</span>
          )}
        </div>
      </div>
    );
  }

  return (
    <div style={cs.mdCell}>
      <div style={cs.cellHeader}>
        <span style={cs.cellType}>MD</span>
        <span style={{ flex: 1 }} />
        <button
          onClick={() => setEditing(false)}
          style={{ ...cs.deleteBtn, color: "var(--havn-text-secondary)", fontSize: 11 }}
          title="Show rendered"
        >Preview</button>
        <button data-havn-danger="" onClick={onDelete} style={cs.deleteBtn} title="Delete cell">&times;</button>
      </div>
      <CodeArea
        value={source}
        language="markdown"
        onChange={(e) => {
          setSource(e.target.value);
          onUpdate({ ...cell, source: e.target.value });
        }}
        minLines={2}
      />
    </div>
  );
}

export default function NotebookPanel({ openPath }) {
  const [notebooks, setNotebooks] = useState([]);
  const [active, setActive] = useState(null);
  const [notebook, setNotebook] = useState(null);
  const [newName, setNewName] = useState("");
  const [runningAll, setRunningAll] = useState(false);
  const [cellsWidth, setCellsWidth] = useState(() => parseInt(localStorage.getItem("havn_notebook_width") || "900", 10));
  const resizingRef = useRef(false);
  const [runningCellId, setRunningCellId] = useState(null);
  const [nbError, setNbError] = useState(null);

  useEffect(() => { loadList(); }, []);

  // Open a notebook when openPath changes (e.g. clicked from file tree)
  useEffect(() => {
    if (openPath && openPath !== active) {
      openNotebook(openPath);
    }
  }, [openPath]);

  async function loadList() {
    try {
      const data = await api.listNotebooks();
      setNotebooks(data);
    } catch (e) { setNbError(e.message || "Failed to load notebooks"); }
  }

  async function openNotebook(name) {
    setNbError(null);
    try {
      const nb = await api.getNotebook(name);
      setActive(name);
      setNotebook(nb);
    } catch (e) {
      setNbError(e.message || "Failed to open notebook");
    }
  }

  async function createNotebook() {
    const name = newName.trim().replace(/\s+/g, "_") || "untitled";
    setNbError(null);
    try {
      const nb = await api.createNotebook(name, name);
      setNewName("");
      await loadList();
      setActive(name);
      setNotebook(nb);
    } catch (e) {
      setNbError(e.message || "Failed to create notebook");
    }
  }

  async function saveNotebook() {
    if (!active || !notebook) return;
    try {
      await api.saveNotebook(active, notebook);
    } catch (e) {
      setNbError(e.message || "Failed to save notebook");
    }
  }

  async function runAll() {
    if (!active || !notebook || runningAll) return;
    setRunningAll(true);
    setNbError(null);
    try {
      await saveNotebook();
      const cells = [...notebook.cells];
      let firstCode = true;
      for (let i = 0; i < cells.length; i++) {
        const cell = cells[i];
        if (cell.type === "markdown") continue;
        setRunningCellId(cell.id);
        const cellType = cell.type || "code";
        try {
          const result = await api.runCell(active, cell.source, { reset: firstCode, cell_type: cellType });
          firstCode = false;
          cells[i] = { ...cell, outputs: result.outputs, duration_ms: result.duration_ms };
          setNotebook((prev) => ({ ...prev, cells: [...cells] }));
        } catch (e) {
          cells[i] = { ...cell, outputs: [{ type: "error", text: e.message }] };
          setNotebook((prev) => ({ ...prev, cells: [...cells] }));
          break;
        }
      }
    } catch (e) {
      setNbError(e.message || "Failed to run notebook");
    }
    setRunningCellId(null);
    setRunningAll(false);
  }

  function addCell(type, afterIndex) {
    if (!notebook) return;
    const id = "cell_" + Math.random().toString(36).slice(2, 8);
    let source = "";
    if (type === "markdown") source = "## Heading";
    else if (type === "ingest") source = JSON.stringify(DEFAULT_INGEST, null, 2);
    else if (type === "sql") source = "SELECT 1";
    const cell = { id, type, source, outputs: [] };
    if (afterIndex != null) {
      const cells = [...notebook.cells];
      cells.splice(afterIndex + 1, 0, cell);
      setNotebook({ ...notebook, cells });
    } else {
      setNotebook({ ...notebook, cells: [...notebook.cells, cell] });
    }
  }

  function updateCell(idx, updated) {
    const cells = [...notebook.cells];
    cells[idx] = updated;
    setNotebook({ ...notebook, cells });
  }

  function deleteCell(idx) {
    const cells = notebook.cells.filter((_, i) => i !== idx);
    setNotebook({ ...notebook, cells });
  }

  const [dragFrom, setDragFrom] = useState(null);
  const [dragOver, setDragOver] = useState(null);
  function moveCell(fromIdx, toIdx) {
    if (fromIdx === toIdx) return;
    const cells = [...notebook.cells];
    const [moved] = cells.splice(fromIdx, 1);
    cells.splice(toIdx, 0, moved);
    setNotebook({ ...notebook, cells });
  }

  if (!notebook) {
    return (
      <div style={s.container}>
        <div style={s.listHeader}>
          <div style={s.newRow}>
            <input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="Notebook name..."
              style={s.input}
              onKeyDown={(e) => e.key === "Enter" && createNotebook()}
            />
            <button onClick={createNotebook} style={s.btn}>New</button>
          </div>
        </div>
        {nbError && <div style={{ color: "var(--havn-red)", fontSize: "12px", padding: "6px 12px" }}>{nbError}</div>}
        <div style={s.list}>
          {notebooks.length === 0 && (
            <div style={s.empty}>No notebooks yet. Create one above.</div>
          )}
          {notebooks.map((nb) => (
            <div key={nb.path} data-havn-notebook="" onClick={() => openNotebook(nb.path)} style={s.nbItem}>
              <span style={s.nbName}>{nb.title || nb.name}</span>
              <span style={s.nbMeta}><span style={s.nbPath}>{nb.path}</span> · {nb.cells} cells</span>
            </div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div style={s.container}>
      <div style={s.nbHeader}>
        <button onClick={() => { setNotebook(null); setActive(null); }} style={s.backBtn}>&larr; Back</button>
        <span style={s.nbTitle}>{notebook.title || active}</span>
        <div style={s.nbActions}>
          <button onClick={() => addCell("sql")} style={s.btn}>+ SQL</button>
          <button onClick={() => addCell("code")} style={s.btn}>+ Code</button>
          <button onClick={() => addCell("ingest")} style={s.btn}>+ Ingest</button>
          <button onClick={() => addCell("markdown")} style={s.btn}>+ Markdown</button>
          <button onClick={saveNotebook} style={s.btn}>Save</button>
          <button onClick={runAll} disabled={runningAll} style={s.runAllBtn}>
            {runningAll ? "Running..." : "Run All"}
          </button>
        </div>
      </div>
      {/* Table of Contents from markdown headings */}
      {(() => {
        const headings = [];
        notebook.cells.forEach((cell, i) => {
          if (cell.type === "markdown") {
            for (const line of (cell.source || "").split("\n")) {
              const m = line.match(/^(#{1,3})\s+(.+)$/);
              if (m) headings.push({ level: m[1].length, text: m[2], cellIndex: i });
            }
          }
        });
        if (headings.length < 2) return null;
        return (
          <div style={{ ...s.toc, maxWidth: cellsWidth }}>
            <span style={s.tocLabel}>Contents</span>
            {headings.map((h, i) => (
              <button
                key={i}
                style={{ ...s.tocItem, paddingLeft: 8 + (h.level - 1) * 12 }}
                onClick={() => {
                  const el = document.querySelector(`[data-cell-index="${h.cellIndex}"]`);
                  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
                }}
              >{h.text}</button>
            ))}
          </div>
        );
      })()}
      <div style={{ ...s.cells, maxWidth: cellsWidth + 40, position: "relative" }}>
        {/* Width resize handle — sits in the extra 40px padding, clear of scrollbar */}
        <div
          style={{ position: "absolute", top: "50%", right: 6, width: 4, height: 40, marginTop: -20, cursor: "col-resize", zIndex: 5, borderRadius: 2, background: "var(--havn-text-dim)", opacity: 0.25, transition: "opacity 0.15s" }}
          onMouseEnter={(e) => e.currentTarget.style.opacity = 0.7}
          onMouseLeave={(e) => { if (!resizingRef.current) e.currentTarget.style.opacity = 0.25; }}
          onMouseDown={(e) => {
            e.preventDefault();
            resizingRef.current = true;
            const startX = e.clientX;
            const startW = cellsWidth;
            const handle = e.currentTarget;
            handle.style.opacity = 0.7;
            const onMove = (ev) => {
              if (!resizingRef.current) return;
              const delta = (ev.clientX - startX) * 2;
              const newW = Math.max(600, Math.min(1600, startW + delta));
              setCellsWidth(newW);
            };
            const onUp = () => {
              resizingRef.current = false;
              handle.style.opacity = 0.25;
              localStorage.setItem("havn_notebook_width", String(cellsWidth));
              document.removeEventListener("mousemove", onMove);
              document.removeEventListener("mouseup", onUp);
            };
            document.addEventListener("mousemove", onMove);
            document.addEventListener("mouseup", onUp);
          }}
        />
        <div style={{ maxWidth: cellsWidth, margin: "0 auto" }}>
        {notebook.cells.length === 0 && (
          <div style={s.empty}>No cells yet. Add a SQL, code, ingest, or markdown cell above.</div>
        )}
        {notebook.cells.map((cell, i) => (
          <React.Fragment key={cell.id || i}>
            {dragFrom != null && dragOver === i && dragFrom !== i && (
              <div style={{ height: 3, background: "var(--havn-accent)", borderRadius: 2, margin: "2px 24px" }} />
            )}
            <div
              data-cell-index={i}
              style={{ ...cs.cellWrap, opacity: dragFrom === i ? 0.3 : 1 }}
              onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; if (dragOver !== i) setDragOver(i); }}
              onDrop={(e) => { e.preventDefault(); if (dragFrom != null && dragFrom !== i) moveCell(dragFrom, i); setDragFrom(null); setDragOver(null); }}
            >
              <div
                draggable
                onDragStart={(e) => { setDragFrom(i); e.dataTransfer.effectAllowed = "move"; }}
                onDragEnd={() => { setDragFrom(null); setDragOver(null); }}
                style={cs.dragHandle}
                title="Drag to reorder"
              >⠿</div>
              <NotebookCell
                cell={cell}
                notebookName={active}
                onUpdate={(updated) => updateCell(i, updated)}
                onDelete={() => deleteCell(i)}
                externalRunning={runningCellId === cell.id}
              />
            </div>
            <CellInsertButton onInsert={(type) => addCell(type, i)} />
          </React.Fragment>
        ))}
        </div>
      </div>
    </div>
  );
}

const s = {
  container: { display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" },
  listHeader: { display: "flex", alignItems: "center", justifyContent: "space-between", padding: "8px 12px", borderBottom: "1px solid var(--havn-border)" },
  title: { fontSize: "13px", fontWeight: 600 },
  newRow: { display: "flex", gap: "8px", flex: 1 },
  input: { flex: 1, padding: "6px 10px", background: "var(--havn-bg-tertiary)", border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius-lg)", color: "var(--havn-text)", fontSize: "13px" },
  btn: { padding: "4px 12px", background: "var(--havn-btn-bg)", border: "1px solid var(--havn-btn-border)", borderRadius: "var(--havn-radius-lg)", color: "var(--havn-text)", cursor: "pointer", fontSize: "11px", fontWeight: 500 },
  list: { flex: 1, overflow: "auto", padding: "8px" },
  empty: { color: "var(--havn-text-dim)", textAlign: "center", padding: "24px" },
  nbItem: { padding: "10px 12px", borderRadius: "var(--havn-radius-lg)", cursor: "pointer", display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "4px", border: "1px solid var(--havn-border)" },
  nbName: { fontWeight: 500, fontSize: "13px" },
  nbMeta: { color: "var(--havn-text-secondary)", fontSize: "12px" },
  nbPath: { fontFamily: "var(--havn-font-mono)", fontSize: "11px", color: "var(--havn-text-dim)" },
  nbHeader: { display: "flex", alignItems: "center", gap: "12px", padding: "8px 12px", borderBottom: "1px solid var(--havn-border)" },
  backBtn: { padding: "4px 12px", background: "var(--havn-btn-bg)", border: "1px solid var(--havn-btn-border)", borderRadius: "var(--havn-radius-lg)", color: "var(--havn-text)", cursor: "pointer", fontSize: "11px", fontWeight: 500 },
  nbTitle: { fontWeight: 600, fontSize: "14px", flex: 1 },
  nbActions: { display: "flex", gap: "6px" },
  runAllBtn: { padding: "4px 12px", background: "var(--havn-green)", border: "1px solid var(--havn-green-border)", borderRadius: "var(--havn-radius-lg)", color: "#fff", cursor: "pointer", fontSize: "11px", fontWeight: 500 },
  cells: { flex: 1, overflowY: "auto", overflowX: "hidden", padding: "12px 16px", margin: "0 auto", width: "100%", boxSizing: "border-box" },
  toc: { display: "flex", alignItems: "center", gap: 4, padding: "6px 16px", borderBottom: "1px solid var(--havn-border)", flexWrap: "wrap", maxWidth: "900px", margin: "0 auto", width: "100%", boxSizing: "border-box" },
  tocLabel: { fontSize: 10, fontWeight: 600, color: "var(--havn-text-dim)", textTransform: "uppercase", letterSpacing: 0.5, marginRight: 4 },
  tocItem: { background: "none", border: "none", color: "var(--havn-accent)", cursor: "pointer", fontSize: 11, padding: "2px 6px", borderRadius: 4 },
};

const cs = {
  cellWrap: { position: "relative", marginBottom: "10px", display: "flex", gap: 0 },
  dragHandle: { display: "flex", alignItems: "center", padding: "0 4px", cursor: "grab", color: "var(--havn-text-dim)", fontSize: 14, opacity: 0.3, transition: "opacity 0.15s", userSelect: "none", flexShrink: 0 },
  codeCell: { border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius-lg)", background: "var(--havn-bg-tertiary)", overflow: "hidden", flex: 1, minWidth: 0 },
  mdCell: { border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius-lg)", background: "var(--havn-bg-tertiary)", overflow: "hidden", flex: 1, minWidth: 0 },
  cellHeader: { display: "flex", alignItems: "center", gap: "8px", padding: "4px 8px", minHeight: "32px", borderBottom: "1px solid var(--havn-border)", background: "var(--havn-bg-secondary)" },
  cellType: { fontSize: "9px", fontWeight: 700, color: "var(--havn-text-dim)", letterSpacing: "0.5px", textTransform: "uppercase" },
  runBtn: { width: "28px", height: "24px", background: "var(--havn-green)", border: "none", borderRadius: "var(--havn-radius)", color: "#fff", cursor: "pointer", fontSize: "11px", fontWeight: 600 },
  toggleBtn: { padding: "2px 8px", background: "none", border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius)", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: "10px", fontWeight: 600 },
  duration: { color: "var(--havn-text-secondary)", fontSize: "11px" },
  codeInput: { width: "100%", padding: "8px 12px", background: "transparent", border: "none", color: "var(--havn-text)", fontFamily: "var(--havn-font-mono)", fontSize: "13px", resize: "vertical", outline: "none", boxSizing: "border-box", lineHeight: 1.5 },
  mdInput: { width: "100%", padding: "8px 12px", background: "transparent", border: "none", color: "var(--havn-text)", fontSize: "13px", resize: "vertical", outline: "none", boxSizing: "border-box", lineHeight: 1.5 },
  outputArea: { borderTop: "1px solid var(--havn-border)", padding: "8px 12px", maxHeight: "300px", overflow: "auto", background: "color-mix(in srgb, var(--havn-bg) 50%, var(--havn-bg-tertiary))" },
  tableWrap: { overflow: "auto" },
  table: { width: "100%", borderCollapse: "collapse", fontSize: "12px", fontFamily: "var(--havn-font-mono)" },
  th: { textAlign: "left", padding: "4px 8px", borderBottom: "1px solid var(--havn-border-light)", color: "var(--havn-text-secondary)", fontWeight: 600 },
  td: { padding: "3px 8px", borderBottom: "1px solid var(--havn-border)", color: "var(--havn-text)" },
  null: { color: "var(--havn-text-dim)", fontStyle: "italic" },
  truncated: { padding: "4px", color: "var(--havn-yellow)", fontSize: "11px" },
  error: { color: "var(--havn-red)", fontSize: "12px", fontFamily: "var(--havn-font-mono)", margin: 0, whiteSpace: "pre-wrap" },
  text: { color: "var(--havn-text)", fontSize: "12px", fontFamily: "var(--havn-font-mono)", margin: 0, whiteSpace: "pre-wrap" },
  deleteBtn: { width: "22px", height: "22px", background: "none", border: "none", color: "var(--havn-text-dim)", cursor: "pointer", fontSize: "14px", lineHeight: "22px", textAlign: "center", borderRadius: "var(--havn-radius)", flexShrink: 0 },
};

// Ingest form styles
const ig = {
  form: { padding: "8px 12px" },
  row: { display: "flex", alignItems: "center", gap: "8px", marginBottom: "6px" },
  label: { width: "70px", flexShrink: 0, fontSize: "11px", fontWeight: 600, color: "var(--havn-text-secondary)", textTransform: "uppercase", letterSpacing: "0.3px" },
  input: { flex: 1, padding: "5px 8px", background: "var(--havn-bg)", border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius)", color: "var(--havn-text)", fontFamily: "var(--havn-font-mono)", fontSize: "12px", outline: "none", boxSizing: "border-box" },
  select: { flex: 1, padding: "5px 8px", background: "var(--havn-bg)", border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius)", color: "var(--havn-text)", fontSize: "12px", outline: "none" },
  targetRow: { display: "flex", alignItems: "center", gap: "4px", flex: 1 },
  dot: { color: "var(--havn-text-dim)", fontSize: "14px", fontWeight: 700 },
  optionsToggle: { background: "none", border: "none", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: "11px", padding: "2px 0", fontWeight: 500 },
  optionsArea: { marginLeft: "78px", marginBottom: "4px" },
};
