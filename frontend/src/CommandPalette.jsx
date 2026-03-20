import React, { useState, useEffect, useRef, useMemo } from "react";

const MRU_KEY = "havn_palette_mru";
const MAX_MRU = 20;
const MAX_RESULTS = 20;

function getMRU() {
  try { return JSON.parse(localStorage.getItem(MRU_KEY) || "[]"); } catch { return []; }
}

function addMRU(id) {
  const mru = getMRU().filter((x) => x !== id);
  mru.unshift(id);
  localStorage.setItem(MRU_KEY, JSON.stringify(mru.slice(0, MAX_MRU)));
}

/**
 * Simple fuzzy match: checks if all characters of query appear in order in target.
 * Returns a score (lower is better) or -1 if no match.
 */
function fuzzyMatch(query, target) {
  const q = query.toLowerCase();
  const t = target.toLowerCase();
  let qi = 0;
  let score = 0;
  let lastIdx = -1;
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) {
      // Bonus for consecutive matches
      score += (ti === lastIdx + 1) ? 0 : (ti - (lastIdx + 1));
      lastIdx = ti;
      qi++;
    }
  }
  if (qi < q.length) return -1;
  return score;
}

/** Flatten file tree into a list of file paths */
function flattenFiles(nodes) {
  const result = [];
  function walk(node) {
    if (node.type === "file") result.push(node.path);
    if (node.children) node.children.forEach(walk);
  }
  nodes.forEach(walk);
  return result;
}

export default function CommandPalette({ isOpen, onClose, files, tables, streams, onOpenFile, onNavigate, onRunStream }) {
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const inputRef = useRef(null);
  const listRef = useRef(null);

  // Reset on open
  useEffect(() => {
    if (isOpen) {
      setQuery("");
      setSelectedIndex(0);
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [isOpen]);

  // Build all searchable items
  const allItems = useMemo(() => {
    const items = [];

    // Files
    const filePaths = flattenFiles(files || []);
    for (const path of filePaths) {
      const name = path.split("/").pop();
      const ext = name.split(".").pop();
      const icon = ext === "sql" ? "SQL" : ext === "py" ? "PY" : ext === "yml" ? "YML" : ext === "dpnb" ? "NB" : "F";
      items.push({
        id: `file:${path}`,
        name,
        secondary: path,
        category: "Files",
        icon,
        action: () => { onOpenFile(path); onClose(); },
      });
    }

    // Tables
    for (const t of (tables || [])) {
      const key = `${t.schema}.${t.name}`;
      items.push({
        id: `table:${key}`,
        name: key,
        secondary: t.type === "VIEW" ? "view" : "table",
        category: "Tables",
        icon: t.type === "VIEW" ? "V" : "T",
        action: () => { onNavigate("Tables"); onClose(); },
      });
    }

    // Models (transform SQL files)
    for (const path of filePaths) {
      if (path.startsWith("transform/") && path.endsWith(".sql")) {
        const modelName = path.replace(/^transform\//, "").replace(/\.sql$/, "").replace(/\//g, ".");
        items.push({
          id: `model:${path}`,
          name: modelName,
          secondary: path,
          category: "Models",
          icon: "M",
          action: () => { onOpenFile(path); onClose(); },
        });
      }
    }

    // Commands
    const commands = [
      { name: "Run Transform", secondary: "Build all SQL models", icon: "\u25B6", action: () => { onNavigate("Editor"); onClose(); } },
      { name: "Run Diff", secondary: "Preview transform changes", icon: "\u0394", action: () => { onNavigate("Diff"); onClose(); } },
      { name: "Query Table", secondary: "Open SQL query runner", icon: "Q", action: () => { onNavigate("Query"); onClose(); } },
      { name: "View DAG", secondary: "Model dependency graph", icon: "D", action: () => { onNavigate("DAG"); onClose(); } },
      { name: "Settings", secondary: "Configuration and preferences", icon: "\u2699", action: () => { onNavigate("Settings"); onClose(); } },
      { name: "View History", secondary: "Pipeline run history", icon: "H", action: () => { onNavigate("History"); onClose(); } },
      { name: "Data Quality", secondary: "Contracts, assertions, freshness", icon: "\u2713", action: () => { onNavigate("Quality"); onClose(); } },
      { name: "Git", secondary: "Commit, push, branch", icon: "G", action: () => { onNavigate("Git"); onClose(); } },
    ];
    for (const cmd of commands) {
      items.push({ id: `cmd:${cmd.name}`, category: "Commands", ...cmd });
    }

    // Streams
    for (const name of Object.keys(streams || {})) {
      items.push({
        id: `stream:${name}`,
        name: `Run Stream: ${name}`,
        secondary: streams[name]?.description || "pipeline stream",
        category: "Commands",
        icon: "\u25B6",
        action: () => { onRunStream(name); onClose(); },
      });
    }

    return items;
  }, [files, tables, streams, onOpenFile, onNavigate, onRunStream, onClose]);

  // Filter and sort results
  const results = useMemo(() => {
    if (!query.trim()) {
      // Show MRU items first, then commands
      const mru = getMRU();
      const mruItems = mru.map((id) => allItems.find((item) => item.id === id)).filter(Boolean);
      const commandItems = allItems.filter((item) => item.category === "Commands" && !mru.includes(item.id));
      return [...mruItems, ...commandItems].slice(0, MAX_RESULTS);
    }

    const scored = [];
    for (const item of allItems) {
      const nameScore = fuzzyMatch(query, item.name);
      const secondaryScore = item.secondary ? fuzzyMatch(query, item.secondary) : -1;
      const best = nameScore >= 0 && secondaryScore >= 0
        ? Math.min(nameScore, secondaryScore)
        : Math.max(nameScore, secondaryScore);
      if (best >= 0) {
        scored.push({ item, score: best });
      }
    }

    // Sort: MRU first (among matches), then by score
    const mru = getMRU();
    scored.sort((a, b) => {
      const aMru = mru.indexOf(a.item.id);
      const bMru = mru.indexOf(b.item.id);
      if (aMru >= 0 && bMru < 0) return -1;
      if (aMru < 0 && bMru >= 0) return 1;
      return a.score - b.score;
    });

    return scored.slice(0, MAX_RESULTS).map((s) => s.item);
  }, [query, allItems]);

  // Scroll selected item into view
  useEffect(() => {
    if (!listRef.current) return;
    const el = listRef.current.querySelector(`[data-palette-index="${selectedIndex}"]`);
    if (el) el.scrollIntoView({ block: "nearest" });
  }, [selectedIndex]);

  // Clamp selectedIndex when results change
  useEffect(() => {
    setSelectedIndex(0);
  }, [query]);

  function handleKeyDown(e) {
    if (e.key === "Escape") { onClose(); return; }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelectedIndex((i) => Math.min(i + 1, results.length - 1));
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelectedIndex((i) => Math.max(i - 1, 0));
      return;
    }
    if (e.key === "Enter" && results[selectedIndex]) {
      e.preventDefault();
      const item = results[selectedIndex];
      addMRU(item.id);
      item.action();
      return;
    }
  }

  if (!isOpen) return null;

  // Group results by category for headers
  const grouped = [];
  let lastCategory = null;
  for (let i = 0; i < results.length; i++) {
    const item = results[i];
    if (item.category !== lastCategory) {
      grouped.push({ type: "header", category: item.category });
      lastCategory = item.category;
    }
    grouped.push({ type: "item", item, index: i });
  }

  return (
    <div style={cpStyles.overlay} onClick={onClose} aria-label="Command palette">
      <div style={cpStyles.modal} onClick={(e) => e.stopPropagation()}>
        <div style={cpStyles.inputRow}>
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="var(--havn-text-dim)" strokeWidth="1.5" style={{ flexShrink: 0 }}>
            <circle cx="6.5" cy="6.5" r="5"/>
            <path d="M10.5 10.5L14.5 14.5"/>
          </svg>
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Search files, tables, commands..."
            style={cpStyles.input}
            aria-label="Command palette search"
          />
          <kbd style={cpStyles.kbd}>esc</kbd>
        </div>
        <div style={cpStyles.list} ref={listRef}>
          {grouped.length === 0 && (
            <div style={cpStyles.empty}>No results</div>
          )}
          {grouped.map((entry, gi) => {
            if (entry.type === "header") {
              return (
                <div key={`h-${entry.category}`} style={cpStyles.categoryHeader}>
                  {entry.category}
                </div>
              );
            }
            const { item, index } = entry;
            const isSelected = index === selectedIndex;
            return (
              <div
                key={item.id}
                data-palette-index={index}
                style={isSelected ? cpStyles.resultActive : cpStyles.result}
                onClick={() => { addMRU(item.id); item.action(); }}
                onMouseEnter={() => setSelectedIndex(index)}
              >
                <span style={cpStyles.resultIcon}>{item.icon}</span>
                <span style={cpStyles.resultName}>{item.name}</span>
                {item.secondary && <span style={cpStyles.resultSecondary}>{item.secondary}</span>}
                <span style={cpStyles.resultBadge}>{item.category}</span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

const cpStyles = {
  overlay: {
    position: "fixed", inset: 0, background: "rgba(0,0,0,0.45)",
    display: "flex", alignItems: "flex-start", justifyContent: "center",
    zIndex: 2000, paddingTop: "min(20vh, 120px)",
    backdropFilter: "blur(2px)",
  },
  modal: {
    width: "500px", maxWidth: "90vw", maxHeight: "400px",
    background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)",
    borderRadius: "8px", overflow: "hidden", display: "flex", flexDirection: "column",
    boxShadow: "0 8px 32px rgba(0,0,0,0.4)",
  },
  inputRow: {
    display: "flex", alignItems: "center", gap: "8px", padding: "10px 14px",
    borderBottom: "1px solid var(--havn-border)",
  },
  input: {
    flex: 1, background: "none", border: "none", color: "var(--havn-text)",
    fontSize: "14px", fontFamily: "var(--havn-font)", outline: "none",
  },
  kbd: {
    fontSize: "10px", fontFamily: "var(--havn-font-mono)", color: "var(--havn-text-dim)",
    background: "var(--havn-btn-bg)", border: "1px solid var(--havn-btn-border)",
    borderRadius: "3px", padding: "1px 5px", flexShrink: 0,
  },
  list: {
    flex: 1, overflow: "auto", padding: "4px 0",
  },
  empty: {
    padding: "24px 16px", color: "var(--havn-text-dim)", fontSize: "13px", textAlign: "center",
  },
  categoryHeader: {
    padding: "6px 14px 2px", fontSize: "10px", fontWeight: 600,
    color: "var(--havn-text-dim)", textTransform: "uppercase", letterSpacing: "0.5px",
  },
  result: {
    display: "flex", alignItems: "center", gap: "8px", padding: "6px 14px",
    cursor: "pointer", fontSize: "13px", transition: "background 0.08s",
  },
  resultActive: {
    display: "flex", alignItems: "center", gap: "8px", padding: "6px 14px",
    cursor: "pointer", fontSize: "13px", background: "var(--havn-accent)",
    color: "#fff", borderRadius: "0",
  },
  resultIcon: {
    width: "22px", height: "20px", display: "flex", alignItems: "center", justifyContent: "center",
    fontSize: "10px", fontWeight: 700, fontFamily: "var(--havn-font-mono)",
    background: "var(--havn-bg-tertiary)", border: "1px solid var(--havn-border)",
    borderRadius: "3px", flexShrink: 0, color: "var(--havn-text-secondary)",
  },
  resultName: {
    fontFamily: "var(--havn-font-mono)", fontSize: "13px", overflow: "hidden",
    textOverflow: "ellipsis", whiteSpace: "nowrap",
  },
  resultSecondary: {
    fontSize: "11px", color: "var(--havn-text-dim)", overflow: "hidden",
    textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1,
  },
  resultBadge: {
    fontSize: "9px", fontWeight: 600, color: "var(--havn-text-dim)",
    background: "var(--havn-bg-tertiary)", border: "1px solid var(--havn-border)",
    borderRadius: "8px", padding: "1px 6px", flexShrink: 0, textTransform: "uppercase",
    letterSpacing: "0.3px",
  },
};
